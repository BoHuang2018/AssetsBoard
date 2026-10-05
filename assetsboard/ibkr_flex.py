"""IBKR Activity Flex Query: import (XML file or Flex Web Service download), archive, analysis.

Read-only by construction: the Flex Web Service only *downloads* a statement the user defined in IBKR's
Client Portal (SendRequest -> GetStatement). Nothing here can trade, transfer or change account settings.
Standard library only (xml.etree.iterparse, urllib).

Archive: archive/ibkr/flex/<section>.jsonl, deduplicated by tradeID / transactionID (re-importing an
overlapping statement only adds new rows; a revised row with the same id replaces the old one).
"""
from __future__ import annotations

import bisect
import datetime as dt
import hashlib
import json
import statistics
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

from .client import cli_cmd, now_oslo, stamp

ET_TZ = ZoneInfo("America/New_York")      # Flex dateTime is US/Eastern by default (verified against FX bars)
FLEX_BASE = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"
FLEX_NAMES = ("IBKR_FLEX_TOKEN", "IBKR_FLEX_QUERY_ID")
UA = "assetsboard/1.0 (read-only Flex download)"
OUTLIER_PCT = 1.0                          # |FX markup| above this % = bad reference, excluded from the estimate


def _key_trade(a):
    return a.get("tradeID") or a.get("transactionID") or a.get("ibExecID")


SECTIONS = {   # element tag -> (archive name, key function, keep-row predicate)
    "Trade": ("trades", _key_trade, lambda a: a.get("levelOfDetail", "EXECUTION") in ("EXECUTION", "")),
    "CashTransaction": ("cash", lambda a: a.get("transactionID") or "|".join((a.get("type", ""), a.get("dateTime", ""), a.get("symbol", ""), a.get("amount", ""), a.get("currency", ""))),
                        lambda a: a.get("levelOfDetail", "DETAIL") in ("DETAIL", "")),
    "UnbundledCommissionDetail": ("commission_details", lambda a: "|".join((a.get("tradeID", ""), a.get("dateTime", ""), a.get("quantity", ""), a.get("price", ""))), lambda a: True),
    "Transfer": ("transfers", lambda a: a.get("transactionID") or "|".join((a.get("date", ""), a.get("symbol", ""), a.get("quantity", ""))), lambda a: True),
    "CorporateAction": ("corporate_actions", lambda a: a.get("transactionID") or a.get("actionID"), lambda a: a.get("levelOfDetail", "DETAIL") in ("DETAIL", "")),
    "ChangeInNAV": ("nav", lambda a: "|".join((a.get("accountId", ""), a.get("fromDate", ""), a.get("toDate", ""))), lambda a: True),
    "OpenPosition": ("open_positions", lambda a: "|".join((a.get("reportDate", ""), a.get("conid", ""), a.get("side", ""))), lambda a: a.get("levelOfDetail", "SUMMARY") in ("SUMMARY", "")),
}


class FlexError(Exception):
    pass


def f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def flex_dir(root: Path) -> Path:
    return root / "flex"


# ---------------------------------------------------------------- parse / import
def parse(path: Path) -> dict:
    """Stream the XML; return {section: {key: row}} plus statement metadata. Rows keep non-empty attributes only."""
    out = {name: {} for name, _, _ in SECTIONS.values()}
    stmts, account = [], None
    for ev, el in ET.iterparse(path, events=("start", "end")):
        if ev == "start":
            if el.tag == "FlexStatement":
                stmts.append({k: el.get(k) for k in ("accountId", "fromDate", "toDate", "period", "whenGenerated")})
                account = account or el.get("accountId")
            elif el.tag == "FlexStatementResponse":
                pass
            continue
        spec = SECTIONS.get(el.tag)
        if spec:
            name, key, keep = spec
            a = {k: v for k, v in el.attrib.items() if v != ""}
            if keep(a):
                k = key(a)
                if k:
                    out[name][k] = a
        if el.tag not in ("FlexStatement", "FlexStatements", "FlexQueryResponse"):
            el.clear()
    if not stmts:
        raise FlexError("不是 IBKR Activity Flex Query XML（找不到 FlexStatement）")
    return {"sections": out, "statements": stmts, "account": account,
            "from": min(s["fromDate"] for s in stmts if s.get("fromDate")),
            "to": max(s["toDate"] for s in stmts if s.get("toDate"))}


def _load(fd: Path, name: str) -> dict:
    p = fd / f"{name}.jsonl"
    rows = {}
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                rows[r["_key"]] = r
    return rows


def load_section(root: Path, name: str) -> list[dict]:
    return list(_load(flex_dir(root), name).values())


def import_xml(path: Path, root: Path) -> dict:
    path = Path(path)
    p = parse(path)
    fd = flex_dir(root)
    fd.mkdir(parents=True, exist_ok=True)
    rep = {"file": path.name, "account": p["account"], "from": p["from"], "to": p["to"], "statements": len(p["statements"]), "sections": {}}
    for name, rows in p["sections"].items():
        old = _load(fd, name)
        new = upd = 0
        for k, a in rows.items():
            r = {"_key": k, **a}
            if k not in old:
                new += 1
            elif old[k] != r:
                upd += 1
            old[k] = r
        if old:
            tmp = fd / f"{name}.jsonl.tmp"
            tmp.write_text("".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n" for r in old.values()), encoding="utf-8")
            tmp.replace(fd / f"{name}.jsonl")
        rep["sections"][name] = {"in_file": len(rows), "new": new, "updated": upd, "total": len(old)}
    st_p = fd / "state.json"
    st = json.loads(st_p.read_text(encoding="utf-8")) if st_p.exists() else {"imports": []}
    md5 = hashlib.md5(path.read_bytes()).hexdigest()
    st["imports"] = [i for i in st["imports"] if i.get("md5") != md5] + [{
        "file": path.name, "md5": md5, "account": p["account"], "from": p["from"], "to": p["to"],
        "statements": len(p["statements"]), "imported_oslo": now_oslo().isoformat(timespec="seconds")}]
    st_p.write_text(json.dumps(st, indent=1, ensure_ascii=False), encoding="utf-8")
    return rep


def present(root: Path) -> bool:
    return (flex_dir(root) / "nav.jsonl").exists() or (flex_dir(root) / "trades.jsonl").exists()


# ---------------------------------------------------------------- Flex Web Service (download only)
def _http(url: str, opener=None, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with (opener or urllib.request.urlopen)(req, timeout=timeout) as r:
            return r.read()
    except Exception as e:  # noqa: BLE001 - never echo the URL (it carries the token)
        why = str(getattr(e, "reason", "") or getattr(e, "code", "") or "")
        tok = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query)).get("t")
        if tok:
            why = why.replace(tok, "***")
        why = why.replace(url, "<url>")[:160]
        step = urllib.parse.urlparse(url).path.rsplit("/", 1)[-1]
        raise FlexError(f"連線 IBKR Flex Web Service 失敗（{step}：{type(e).__name__}{'：' + why if why else ''}）") from None


def _resp(body: bytes) -> ET.Element:
    try:
        return ET.fromstring(body)
    except ET.ParseError:
        raise FlexError("IBKR Flex Web Service 回傳了無法解析的內容") from None


def flex_fetch(token: str, query_id: str, out_dir: Path, opener=None, sleep=time.sleep, tries: int = 12) -> Path:
    """SendRequest -> poll GetStatement -> save XML. Returns the saved path. Never prints or stores the token."""
    if not token or not query_id:
        raise FlexError("缺少 IBKR_FLEX_TOKEN 或 IBKR_FLEX_QUERY_ID")
    q = urllib.parse.urlencode({"t": token, "q": query_id, "v": "3"})
    r = _resp(_http(f"{FLEX_BASE}/SendRequest?{q}", opener))
    if (r.findtext("Status") or "").strip() != "Success":
        raise FlexError(f"SendRequest 失敗：{r.findtext('ErrorCode') or '?'} {r.findtext('ErrorMessage') or ''}".strip())
    ref = (r.findtext("ReferenceCode") or "").strip()
    url = (r.findtext("Url") or "").strip()
    pu = urllib.parse.urlparse(url)
    if not (pu.scheme == "https" and (pu.hostname or "").endswith(".interactivebrokers.com")):
        url = f"{FLEX_BASE}/GetStatement"
    urls = [url] + ([f"{FLEX_BASE}/GetStatement"] if url != f"{FLEX_BASE}/GetStatement" else [])
    wait = 5
    for _ in range(tries):
        sleep(wait)
        try:
            body = _http(f"{urls[0]}?{urllib.parse.urlencode({'t': token, 'q': ref, 'v': '3'})}", opener)
        except FlexError:
            if len(urls) == 1:
                raise
            urls.pop(0)          # e.g. IBKR answers with gdcdyn.* which may not resolve: use the documented ndcdyn host
            wait = 1
            continue
        root = _resp(body)
        if root.tag == "FlexQueryResponse":
            out_dir.mkdir(parents=True, exist_ok=True)
            p = out_dir / f"flex_{stamp(now_oslo())}.xml"
            p.write_bytes(body)
            return p
        code = (root.findtext("ErrorCode") or "").strip()
        if code in ("1001", "1004", "1005", "1006", "1007", "1008", "1009", "1018", "1019", "1021"):   # still generating / busy
            wait = min(wait * 2, 60)
            continue
        raise FlexError(f"GetStatement 失敗：{code or '?'} {root.findtext('ErrorMessage') or ''}".strip())
    raise FlexError("IBKR 仍在產生報表，請稍後再試")


# ---------------------------------------------------------------- FX reference (implicit spread)
def _yahoo_sym(pair: str) -> str | None:
    if "." not in pair:
        return None
    b, q = pair.split(".", 1)
    return f"{q}=X" if b == "USD" else f"{b}{q}=X"


def _get_json(url: str):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (assetsboard)"}), timeout=20) as r:
            return json.loads(r.read().decode())
    except Exception:  # noqa: BLE001
        return None


def trade_ts(a: dict) -> float | None:
    s = a.get("dateTime") or ""
    for fmt in ("%Y%m%d;%H%M%S", "%Y%m%d %H%M%S", "%Y-%m-%d;%H:%M:%S", "%Y%m%d"):
        try:
            return dt.datetime.strptime(s, fmt).replace(tzinfo=ET_TZ).timestamp()
        except ValueError:
            pass
    return None


def fx_refs(trades: list[dict], cache_path: Path, offline: bool = False) -> dict:
    """tradeID -> {"ref": mid, "src": ...}. Yahoo hourly bar (mean of open/close) containing the trade time (ET);
    fallback ECB daily reference rate (frankfurter.dev). Cached; only missing trades are fetched."""
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    todo = [t for t in trades if t["_key"] not in cache]
    if todo and not offline:
        by_pair = defaultdict(list)
        for t in todo:
            by_pair[t.get("symbol", "")].append(t)
        for pair, ts in by_pair.items():
            ys = _yahoo_sym(pair)
            stamps = [trade_ts(t) for t in ts]
            ok = [s for s in stamps if s]
            bars = None
            if ys and ok:
                t0, t1 = int(min(ok)) - 86400, int(max(ok)) + 86400
                j = _get_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(ys)}?interval=1h&period1={t0}&period2={t1}")
                try:
                    r = j["chart"]["result"][0]
                    qd = r["indicators"]["quote"][0]
                    bars = [(T, o, c) for T, o, c in zip(r["timestamp"], qd["open"], qd["close"]) if c]
                except (TypeError, KeyError, IndexError):
                    bars = None
            T = [b[0] for b in bars] if bars else []
            ecb = {}
            for t, s in zip(ts, stamps):
                ref = None
                if bars and s:
                    i = bisect.bisect_right(T, s) - 1
                    if i >= 0 and s - T[i] < 3600:
                        o, c = bars[i][1], bars[i][2]
                        ref, src = ((o + c) / 2 if o else c), "Yahoo 1h"
                if ref is None and "." in pair:
                    b, q = pair.split(".", 1)
                    day = (t.get("tradeDate") or t.get("dateTime", "")[:8])
                    d = f"{day[:4]}-{day[4:6]}-{day[6:8]}"
                    if (d, b, q) not in ecb:
                        jj = _get_json(f"https://api.frankfurter.dev/v1/{d}?from={b}&to={q}")
                        ecb[(d, b, q)] = (jj or {}).get("rates", {}).get(q)
                    if ecb[(d, b, q)]:
                        ref, src = float(ecb[(d, b, q)]), "ECB 每日"
                if ref:
                    cache[t["_key"]] = {"ref": ref, "src": src}
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache, indent=0), encoding="utf-8")
    return cache


# ---------------------------------------------------------------- analysis
NAV_FIELDS = ("mtm", "realized", "changeInUnrealized", "depositsWithdrawals", "assetTransfers", "internalCashTransfers", "dividends",
              "withholdingTax", "withholding871m", "changeInDividendAccruals", "interest", "changeInInterestAccruals", "commissions",
              "forexCommissions", "otherFees", "brokerFees", "transactionTax", "salesTax", "fxTranslation", "corporateActionProceeds",
              "otherIncome", "other", "costAdjustments", "advisorFees", "clientFees", "netFxTrading")


def _ymd(s: str) -> dt.date | None:
    try:
        return dt.datetime.strptime((s or "")[:8], "%Y%m%d").date()
    except ValueError:
        return None


def nav_chain(rows: list[dict]) -> list[dict]:
    """Non-overlapping ChangeInNAV rows (shortest periods win), ordered by date."""
    picked, covered = [], set()
    for r in sorted(rows, key=lambda r: ((_ymd(r.get("toDate")) - _ymd(r.get("fromDate"))).days if _ymd(r.get("toDate")) and _ymd(r.get("fromDate")) else 9999, r.get("fromDate", ""))):
        a, b = _ymd(r.get("fromDate")), _ymd(r.get("toDate"))
        if not a or not b:
            continue
        days = {a + dt.timedelta(n) for n in range((b - a).days + 1)}
        if days & covered:
            continue
        covered |= days
        picked.append(r)
    return sorted(picked, key=lambda r: r["fromDate"])


def _comm_base(t: dict) -> float:
    c = f(t.get("ibCommission"))
    cc, cur = t.get("ibCommissionCurrency"), t.get("currency")
    if not c:
        return 0.0
    if cc and cc == cur:
        return c * f(t.get("fxRateToBase") or 1)
    return c          # commission charged in base currency (the usual case: USD)


def _fee_kind(desc: str) -> str:
    d = (desc or "").upper()
    if "ADR" in d and "FEE" in d and "DIVIDEND" not in d:
        return "ADR 託管費"
    if "DIVIDEND" in d:
        return "股息處理費（ADR）"
    if any(x in d for x in ("MARKET DATA", "SNAPSHOT", "SUBSCRIPTION", "NYSE", "NASDAQ", "OPRA", "BUNDLE")):
        return "市場數據訂閱"
    if "INACTIVITY" in d or "MONTHLY MINIMUM" in d:
        return "帳戶月費"
    return "其他"


def analyze(project: Path, root: Path, base: str = "USD", offline: bool = False) -> dict:
    if not present(root):
        return {"present": False, "reason": f"尚未匯入 Flex Query：{cli_cmd()} ibkr-import <檔案.xml>"}
    trades, cash = load_section(root, "trades"), load_section(root, "cash")
    ucd, navr = load_section(root, "commission_details"), load_section(root, "nav")
    transfers, corp = load_section(root, "transfers"), load_section(root, "corporate_actions")
    st = json.loads((flex_dir(root) / "state.json").read_text(encoding="utf-8")) if (flex_dir(root) / "state.json").exists() else {}
    base = next((r.get("currency") for r in navr if r.get("currency")), base)

    # ---- NAV chain, account start, coverage
    chain = nav_chain(navr)
    live = [r for r in chain if f(r.get("startingValue")) or f(r.get("endingValue"))]
    stmt_from = min((i["from"] for i in st.get("imports", [])), default=None)
    stmt_to = max((i["to"] for i in st.get("imports", [])), default=None)
    opening, start, complete = 0.0, None, False
    if live:
        first = live[0]
        opening = f(first.get("startingValue"))
        start = first["fromDate"]
        complete = opening == 0.0
    comp = defaultdict(float)
    twr = 1.0
    for r in live:
        for k in NAV_FIELDS:
            comp[k] += f(r.get(k))
        twr *= 1 + f(r.get("twr")) / 100
    nav_end = f(live[-1].get("endingValue")) if live else None
    nav_to = live[-1]["toDate"] if live else None
    net_inflow = comp["depositsWithdrawals"] + comp["assetTransfers"] + comp["internalCashTransfers"]
    pnl = (nav_end - opening - net_inflow) if nav_end is not None else None
    gaps = []
    for a, b in zip(live, live[1:]):
        if abs(f(a.get("endingValue")) - f(b.get("startingValue"))) > 0.01:
            gaps.append(b["fromDate"])

    # ---- deposits / withdrawals
    flows = []
    for c in sorted(cash, key=lambda c: c.get("dateTime", "")):
        if c.get("type") == "Deposits/Withdrawals":
            flows.append({"date": c.get("dateTime", "")[:8], "currency": c.get("currency"), "amount": f(c.get("amount")),
                          "base": f(c.get("amount")) * f(c.get("fxRateToBase") or 1), "description": c.get("description")})
    by_cur = defaultdict(float)
    for x in flows:
        by_cur[x["currency"]] += x["amount"]
    monthly = defaultdict(float)
    for x in flows:
        monthly[x["date"][:6]] += x["base"]

    # ---- trades: realized, volume, commissions
    stk = [t for t in trades if t.get("assetCategory") != "CASH"]
    fx = [t for t in trades if t.get("assetCategory") == "CASH"]
    sym = defaultdict(lambda: {"realized": 0.0, "buys": 0, "sells": 0, "volume": 0.0, "commission": 0.0})
    for t in stk:
        s = sym[t.get("symbol", "?")]
        r = f(t.get("fxRateToBase") or 1)
        s["realized"] += f(t.get("fifoPnlRealized")) * r
        s["volume"] += abs(f(t.get("tradeMoney"))) * r
        s["commission"] += _comm_base(t)
        s["buys" if t.get("buySell", "").startswith("BUY") else "sells"] += 1
    realized_rows = sorted(({"symbol": k, **v} for k, v in sym.items() if abs(v["realized"]) > 1e-9), key=lambda x: -x["realized"])
    realized_total = sum(v["realized"] for v in sym.values())
    stk_vol = sum(v["volume"] for v in sym.values())
    stk_comm = -sum(v["commission"] for v in sym.values())
    stk_tax = -sum(f(t.get("taxes")) * f(t.get("fxRateToBase") or 1) for t in stk)
    tdates = sorted(t.get("tradeDate", "") for t in trades if t.get("tradeDate"))

    # commission breakdown (Unbundled Commission Details)
    ucd_stk = [u for u in ucd if u.get("assetCategory") != "CASH"]     # FX commission rows are reported under FX

    def ub(k):
        return -sum(f(u.get(k)) * f(u.get("fxRateToBase") or 1) for u in ucd_stk) + 0.0
    breakdown = {"broker": ub("brokerExecutionCharge") + ub("brokerClearingCharge"),
                 "exchange_clearing": ub("thirdPartyExecutionCharge") + ub("thirdPartyClearingCharge"),
                 "regulatory": ub("thirdPartyRegulatoryCharge"),
                 "reg_detail": {"FINRA TAF": ub("regFINRATradingActivityFee"), "SEC 31": ub("regSection31TransactionFee"), "其他監管": ub("regOther")},
                 "other": ub("other"), "total": ub("totalCommission"), "rows": len(ucd_stk)} if ucd_stk else None

    # ---- FX conversions: commissions + implicit spread
    refs = fx_refs(fx, project / "snapshots" / "ibkr_fx_refs.json", offline) if fx else {}
    fx_rows, est, outl = [], [], []
    fx_vol = fx_comm = 0.0
    for t in fx:
        qty, px, rate = abs(f(t.get("quantity"))), f(t.get("tradePrice")), f(t.get("fxRateToBase") or 1)
        vol = qty * px * rate
        fx_vol += vol
        fx_comm += -_comm_base(t)
        ref = (refs.get(t["_key"]) or {}).get("ref")
        sign = 1 if t.get("buySell", "").startswith("BUY") else -1
        row = {"date": t.get("dateTime"), "pair": t.get("symbol"), "side": t.get("buySell"), "qty": qty, "price": px,
               "volume_base": vol, "auto": t.get("notes", "").find("AFx") >= 0, "commission_base": -_comm_base(t)}
        if ref:
            mk = (px / ref - 1) * 100 * sign
            row.update(ref=ref, src=refs[t["_key"]]["src"], markup_pct=mk, spread_base=qty * (px - ref) * sign * rate)
            (outl if abs(mk) > OUTLIER_PCT else est).append(row)
        fx_rows.append(row)
    spread = sum(r["spread_base"] for r in est)
    est_vol = sum(r["volume_base"] for r in est)
    fx_info = {"trades": len(fx), "auto_conversions": sum(1 for r in fx_rows if r["auto"]), "volume_base": fx_vol,
               "commission": fx_comm, "spread_est": spread, "priced": len(est), "outliers": len(outl),
               "vw_markup_pct": (spread / est_vol * 100) if est_vol else None,
               "median_markup_pct": statistics.median(r["markup_pct"] for r in est) if est else None,
               "pairs": dict(sorted({p: sum(1 for r in fx_rows if r["pair"] == p) for p in {r["pair"] for r in fx_rows}}.items())),
               "sources": {s: sum(1 for r in est if r["src"] == s) for s in {r["src"] for r in est}},
               "total": fx_comm + spread,
               "method": ("每筆換匯成交價 vs 同一時段的市場中間價：Yahoo Finance 1 小時 K 線（該小時開盤與收盤的平均；"
                          "Flex 的時間以美東時間解讀，這個時區與成交價最吻合），查不到時用 ECB 每日參考匯率（frankfurter.dev）。"
                          f"加價 =（成交價 / 參考價 − 1）×（買 +1／賣 −1）；|加價| > {OUTLIER_PCT:g}% 視為參考價不可靠，不計入。"
                          "這是估計值：參考價本身也有誤差（Yahoo 報價不是 IBKR 的 IDEALPRO 中間價）。"),
               "worst": sorted(est, key=lambda r: -r["markup_pct"])[:5]}

    # ---- dividends, interest, other fees
    div = defaultdict(lambda: {"gross": 0.0, "tax": 0.0, "fees": 0.0})
    interest = defaultdict(float)
    fees = []
    for c in cash:
        typ, amt = c.get("type", ""), f(c.get("amount")) * f(c.get("fxRateToBase") or 1)
        s = c.get("symbol") or "—"
        if typ in ("Dividends", "Payment In Lieu Of Dividends"):
            div[s]["gross"] += amt
        elif typ == "Withholding Tax":
            div[s]["tax"] += amt
        elif "Interest" in typ:
            interest[typ] += amt
        elif typ in ("Other Fees", "Commission Adjustments", "Advisor Fees", "Broker Fees"):
            k = _fee_kind(c.get("description"))
            fees.append({"date": c.get("dateTime", "")[:8], "symbol": c.get("symbol"), "kind": k, "amount": amt, "description": c.get("description")})
            if k == "股息處理費（ADR）" and c.get("symbol"):
                div[s]["fees"] += amt
    div_rows = sorted(({"symbol": k, **v, "net": v["gross"] + v["tax"]} for k, v in div.items()), key=lambda x: -x["gross"])
    dsum = {k: sum(r[k] for r in div_rows) for k in ("gross", "tax", "net", "fees")}
    fee_kinds = defaultdict(float)
    for x in fees:
        fee_kinds[x["kind"]] += x["amount"]
    other_fees = -sum(x["amount"] for x in fees)

    # ---- totals / ratios
    costs = {"stock_commission": stk_comm, "fx_commission": fx_comm, "fx_spread_est": spread, "other_fees": other_fees,
             "transaction_tax": stk_tax}
    costs["total"] = sum(costs.values())
    traded = stk_vol + fx_vol
    gross = (pnl + costs["total"]) if pnl is not None else None
    ratios = {"stock_commission_pct_of_stock_volume": (stk_comm / stk_vol * 100) if stk_vol else None,
              "fx_cost_pct_of_fx_volume": (fx_info["total"] / fx_vol * 100) if fx_vol else None,
              "total_cost_pct_of_traded_volume": (costs["total"] / traded * 100) if traded else None,
              "total_cost_pct_of_stock_volume": (costs["total"] / stk_vol * 100) if stk_vol else None,
              "total_cost_pct_of_pnl": (costs["total"] / pnl * 100) if pnl else None,
              "total_cost_pct_of_gross_pnl": (costs["total"] / gross * 100) if gross else None,
              "avg_commission_per_stock_trade": (stk_comm / len(stk)) if stk else None,
              "avg_stock_trade_size": (stk_vol / len(stk)) if stk else None}
    return {
        "present": True, "account": next((i.get("account") for i in st.get("imports", [])), None), "base_currency": base,
        "imports": st.get("imports", []), "statement_from": stmt_from, "statement_to": stmt_to,
        "account_start": start, "complete": complete, "opening_value": opening, "coverage_to": nav_to,
        "nav_end": nav_end, "net_inflow": net_inflow, "pnl": pnl,
        "pnl_pct": (pnl / (opening + net_inflow) * 100) if pnl is not None and (opening + net_inflow) else None,
        "twr_pct": (twr - 1) * 100 if live else None, "nav_components": {k: v for k, v in comp.items() if abs(v) > 1e-9},
        "nav_gaps": gaps, "days": len(live),
        "flows": flows, "flows_by_currency": dict(by_cur), "flows_monthly": dict(sorted(monthly.items())),
        "realized": {"total": realized_total, "rows": realized_rows, "sells": sum(v["sells"] for v in sym.values()),
                     "buys": sum(v["buys"] for v in sym.values()), "symbols_traded": len(sym)},
        "trades": {"stock": len(stk), "fx": len(fx), "first": tdates[0] if tdates else None, "last": tdates[-1] if tdates else None,
                   "stock_volume": stk_vol},
        "dividends": {"rows": div_rows, **dsum}, "interest": dict(interest), "interest_total": sum(interest.values()),
        "fees": fees, "fee_kinds": dict(fee_kinds), "commission_breakdown": breakdown, "fx": fx_info,
        "costs": costs, "ratios": ratios, "transfers": len(transfers), "corporate_actions": len(corp),
    }
