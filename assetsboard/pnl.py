"""Lifetime PnL estimate: current value − net external inflow (GET only).

Data sources (all signed GET, read-only):
- /api/v2/tax/spot-record      full spot ledger since account start (30-day windows); reconciles to spot balances
- /api/v2/spot/wallet/deposit-records / withdrawal-records   chain/dest metadata (89-day windows)
- /api/v2/p2p/orderList        P2P orders (7-day windows)
- /api/v2/spot/account/bills   last ~90 days only: PoolX / dual-investment outstanding estimates
- /api/v2/spot/account/transferRecords  last ~90 days only: transfers to/from 'uta' and other UIDs
- public /api/v2/spot/market/history-candles   price at flow time (1h close)
"""
from __future__ import annotations

import datetime as dt
import time
from collections import Counter, defaultdict

from . import holdings as hold
from . import valuation as val
from .client import OSLO, Client, data, fnum, ms_to_oslo, now_oslo, ok, public_get

DAY = 86400000
STABLE_1 = {"USDT", "USDC", "USD1", "FDUSD", "USDGO", "DAI", "USD"}
FIAT = {"EUR", "USD", "GBP", "PLN", "BRL", "TRY", "NOK", "SEK", "CHF"}
INCOME_TYPES = {"Airdrop Reward-A", "Airdrop Reward-B", "Crypto Voucher Distribution",
                "rebate_coupon_activity_user_in", "Interest", "batch_interest_user_in"}


# ---------- fetching ----------
def _windows(start: int, end: int, step: int):
    s = start
    while s < end:
        e = min(end, s + step)
        yield s, e
        s = e


def _paged(c: Client, path: str, base: dict, start: int, end: int, step: int, id_field: str,
           limit: int = 100, cursor: str = "idLessThan", max_pages: int = 200) -> tuple[list, list]:
    rows, errors = [], []
    for ws, we in _windows(start, end, step):
        cur = None
        for _ in range(max_pages):
            p = dict(base, startTime=ws, endTime=we, limit=limit)
            if cur:
                p[cursor] = cur
            r = c.raw_get(path, p)
            if not ok(r):
                errors.append({"path": path, "window": [ms_to_oslo(ws), ms_to_oslo(we)], "code": r.get("code"), "msg": r.get("msg")})
                break
            d = r.get("data")
            batch = d if isinstance(d, list) else next((v for v in (d or {}).values() if isinstance(v, list)), []) if isinstance(d, dict) else []
            rows.extend(batch)
            if len(batch) < limit:
                break
            nxt = (d.get("endId") if isinstance(d, dict) else None) or batch[-1].get(id_field)
            if not nxt or nxt == cur:
                break
            cur = nxt
    return rows, errors


def collect(c: Client, start_ms: int, end_ms: int) -> dict:
    f: dict = {"errors": []}
    tax, e = _paged(c, "/api/v2/tax/spot-record", {}, start_ms, end_ms, 30 * DAY - 1000, "id", limit=500)
    f["tax_spot"] = list({r["id"]: r for r in tax}.values()); f["errors"] += e
    for k, path in (("deposits", "/api/v2/spot/wallet/deposit-records"), ("withdrawals", "/api/v2/spot/wallet/withdrawal-records")):
        rows, e = _paged(c, path, {}, start_ms, end_ms, 89 * DAY, "orderId")
        f[k] = list({r["orderId"]: r for r in rows}.values()); f["errors"] += e
    p2p, e = _paged(c, "/api/v2/p2p/orderList", {}, start_ms, end_ms, 7 * DAY - 1000, "orderId", cursor="lastMinId")
    f["p2p"] = p2p; f["errors"] += e
    recent = max(start_ms, end_ms - 89 * DAY)
    f["recent_start_ms"] = recent
    bills, e = _paged(c, "/api/v2/spot/account/bills", {}, recent, end_ms, 89 * DAY, "billId", limit=500)
    f["bills_90d"] = bills; f["errors"] += e
    tr = []
    for coin in ("USDT", "USDC", "BTC", "ETH"):
        rows, e = _paged(c, "/api/v2/spot/account/transferRecords", {"coin": coin}, recent, end_ms, 89 * DAY, "transferId")
        tr += rows; f["errors"] += e
    f["transfers_90d"] = list({r.get("transferId"): r for r in tr}.values())
    return f


# ---------- pricing ----------
class HistPrice:
    """USDT price of a coin at a timestamp using public 1h candles (close of the hour containing ts)."""

    def __init__(self):
        self.cache: dict = {}
        self.misses: list = []

    def _symbol(self, coin: str) -> tuple[str, bool]:
        if coin in FIAT:
            return f"USDT{coin}", True  # inverted pair
        if val.classify(coin) == "stock_plus":
            return f"r{coin[:-2]}USDT", False  # Stock+ (Ondo …ON) proxied by the matching rToken
        return f"{coin}USDT", False

    def __call__(self, coin: str, ts: int) -> float | None:
        if coin in STABLE_1:
            return 1.0
        sym, inv = self._symbol(coin)
        hour = ts - ts % 3600000
        key = (sym, hour)
        if key not in self.cache:
            time.sleep(0.1)
            r = public_get("/api/v2/spot/market/history-candles", {"symbol": sym, "granularity": "1h", "endTime": hour + 3600000, "limit": 3})
            px = None
            for k in r.get("data") or []:
                if int(k[0]) <= ts:
                    px = fnum(k[4])
            if px is None and r.get("data"):
                px = fnum(r["data"][0][4])
            if px and inv:
                px = 1.0 / px
            self.cache[key] = px
            if px is None:
                self.misses.append({"coin": coin, "symbol": sym, "time": ms_to_oslo(ts), "code": r.get("code")})
        return self.cache[key]


def current_price(coin: str, tickers: dict) -> float | None:
    if coin in STABLE_1:
        return 1.0
    if val.classify(coin) == "stock_plus":
        return tickers.get(f"R{coin[:-2]}".upper()) or tickers.get(f"r{coin[:-2]}")
    return val.price_of(coin, tickers)


# ---------- classification ----------
def classify_external(f: dict) -> list[dict]:
    """External flows from the tax ledger. usd>0 inflow, usd<0 outflow."""
    deps = {r["orderId"]: r for r in f["deposits"]}
    wds = {r["orderId"]: r for r in f["withdrawals"]}
    tax = sorted(f["tax_spot"], key=lambda r: int(r["ts"]))
    fiat_buy_ts = [int(r["ts"]) for r in tax if "FIAT" in r["spotTaxType"] and "BUY_OR_SELL" in r["spotTaxType"]]
    out = []
    for r in tax:
        t, coin, amt, ts = r["spotTaxType"], r["coin"], fnum(r["amount"]), int(r["ts"])
        kind = None
        if t == "Deposit":
            d = deps.get(r.get("bizOrderId"), {})
            if d.get("dest") == "internal":
                kind = "fiat_buy_settlement" if any(abs(ts - x) < 30 * 60000 for x in fiat_buy_ts) else "internal_uid_deposit"
            else:
                kind = "onchain_deposit"
            meta = {"chain": d.get("chain"), "dest": d.get("dest"), "from": str(d.get("fromAddress") or "")[:10]}
        elif t == "Withdrawal":
            d = wds.get(r.get("bizOrderId"), {})
            kind = "internal_uid_withdrawal" if d.get("dest") == "internal" else "onchain_withdrawal"
            meta = {"chain": d.get("chain"), "dest": d.get("dest"), "to": str(d.get("toAddress") or "")[:10], "fee": fnum(r.get("fee"))}
        elif "FIAT" in t and amt > 0 and "DEPOSIT" in t:
            kind, meta = "fiat_bank_deposit", {}
        elif t == "" and coin in FIAT and amt > 0:
            kind, meta = "fiat_bank_deposit", {"assumed": "tax type blank; bills show FIAT_BANK_TRANSFER_DEPOSIT_SUCCESS_USER_IN_V2"}
        elif "FIAT" in t and "WITHDRAW" in t:
            kind, meta = "fiat_withdrawal", {}
        if kind:
            out.append({"kind": kind, "coin": coin, "qty": amt, "ts": ts, "time": ms_to_oslo(ts), **meta})
    for o in f.get("p2p") or []:
        out.append({"kind": "p2p_unparsed", "coin": o.get("coin"), "qty": 0.0, "ts": int(o.get("ctime") or o.get("cTime") or 0), "raw_keys": sorted(o)})
    return out


def income_summary(f: dict) -> dict[str, dict[str, float]]:
    agg: dict = defaultdict(Counter)
    for r in f["tax_spot"]:
        if r["spotTaxType"] in INCOME_TYPES:
            agg[r["spotTaxType"]][r["coin"]] += fnum(r["amount"])
    return {k: dict(v) for k, v in agg.items()}


def ledger_check(f: dict, spot_qty: dict[str, float]) -> dict[str, list]:
    s: Counter = Counter()
    for r in f["tax_spot"]:
        s[r["coin"]] += fnum(r["amount"]) + fnum(r["fee"])
    bad = []
    for coin in set(s) | set(spot_qty):
        a, b = s.get(coin, 0.0), spot_qty.get(coin, 0.0)
        if abs(a - b) > 1e-6 * max(1.0, abs(b)):
            bad.append([coin, round(a, 10), round(b, 10)])
    return {"coins": len(set(s) | set(spot_qty)), "mismatch": sorted(bad)}


def invisible_estimates(f: dict, tickers: dict, own_uid: str) -> dict:
    """Value that left the API-visible accounts into products the API cannot see (last ~90 days only)."""
    bills = sorted(f["bills_90d"], key=lambda b: int(b["cTime"]))
    poolx: Counter = Counter()
    for b in bills:
        if "POOL_X" in b["businessType"]:
            poolx[b["coin"]] -= fnum(b["size"])  # out is negative in bills -> positive locked
    poolx_rows = [{"coin": k, "qty": v, "usdt": v * (current_price(k, tickers) or 0)} for k, v in poolx.items() if v > 1e-12]
    # dual: freeze matched to a later LOCK_RELEASE of same coin & principal; unmatched in last 14 days = outstanding
    releases = [b for b in bills if b["businessType"] == "LOCK_RELEASE_USER_IN"]
    used, outstanding = set(), []
    now = int(time.time() * 1000)
    for b in bills:
        if b["businessType"] != "FINANCIAL_DUAL_USER_FREEZING_OUT":
            continue
        q = -fnum(b["size"])
        m = next((i for i, r in enumerate(releases) if i not in used and r["coin"] == b["coin"]
                  and int(r["cTime"]) > int(b["cTime"]) and abs(fnum(r["size"]) - q) < 1e-6), None)
        if m is not None:
            used.add(m)
        elif now - int(b["cTime"]) < 14 * DAY:
            outstanding.append({"coin": b["coin"], "qty": q, "time": ms_to_oslo(b["cTime"]),
                                "usdt": q * (current_price(b["coin"], tickers) or 0)})
    uta_out = sum(fnum(t["size"]) for t in f["transfers_90d"] if t.get("toType") == "uta")
    uta_in = sum(fnum(t["size"]) for t in f["transfers_90d"] if t.get("fromType") == "uta")
    uta_rows = sorted(({"time": ms_to_oslo(t["ts"]), "dir": "out" if t.get("toType") == "uta" else "in", "usdt": fnum(t["size"])}
                       for t in f["transfers_90d"] if "uta" in (t.get("toType"), t.get("fromType"))), key=lambda x: x["time"])
    other_uid = [t for t in f["transfers_90d"] if t.get("fromType") == t.get("toType") and t.get("fromType") in ("spot", "usdt_futures")]
    return {
        "poolx": poolx_rows, "poolx_usdt": sum(r["usdt"] for r in poolx_rows),
        "dual_outstanding": outstanding, "dual_usdt": sum(r["usdt"] for r in outstanding),
        "uta_out": uta_out, "uta_in": uta_in, "uta_net": uta_out - uta_in, "uta_rows": uta_rows,
        "other_uid_transfers": len(other_uid),
    }


def run(c: Client, since: dt.date) -> dict:
    start = int(dt.datetime.combine(since, dt.time(), OSLO).timestamp() * 1000)
    end = int(time.time() * 1000)
    h = hold.collect(c, full=True)
    tickers = val.fetch_tickers()
    totals = hold.account_totals(h)
    spot_qty = hold.spot_quantities(h["spot_assets"])
    on_rows = [{"coin": k, "qty": q, "proxy": f"r{k[:-2]}", "usdt": q * (current_price(k, tickers) or 0)}
               for k, q in spot_qty.items() if val.classify(k) == "stock_plus"]
    f = collect(c, start, end)
    hp = HistPrice()
    ext = classify_external(f)
    for e in ext:
        px = hp(e["coin"], e["ts"]) if e["qty"] else 0.0
        e["px"] = px
        e["usd"] = e["qty"] * px if px is not None else None
    by_kind: dict = defaultdict(lambda: {"n": 0, "usd": 0.0, "coins": Counter()})
    for e in ext:
        k = by_kind[e["kind"]]
        k["n"] += 1; k["usd"] += e["usd"] or 0.0; k["coins"][e["coin"]] += e["qty"]
    inflow_kinds = ("onchain_deposit", "fiat_bank_deposit", "internal_uid_deposit")
    outflow_kinds = ("onchain_withdrawal", "fiat_withdrawal", "internal_uid_withdrawal")
    deposits = sum(by_kind[k]["usd"] for k in inflow_kinds if k in by_kind)
    withdrawals = -sum(by_kind[k]["usd"] for k in outflow_kinds if k in by_kind)
    net = deposits - withdrawals
    visible = sum(totals.values())
    on_usdt = sum(r["usdt"] for r in on_rows)
    inv = invisible_estimates(f, tickers, hold.own_user_id(h))
    value_a = visible
    value_b = visible + on_usdt + inv["poolx_usdt"] + inv["dual_usdt"] + max(0.0, inv["uta_net"])
    tax_sorted = sorted(f["tax_spot"], key=lambda r: int(r["ts"]))
    return {
        "captured_oslo": now_oslo().isoformat(timespec="seconds"),
        "since": since.isoformat(),
        "earliest_record": ms_to_oslo(tax_sorted[0]["ts"]) if tax_sorted else None,
        "earliest_record_detail": {k: tax_sorted[0].get(k) for k in ("coin", "spotTaxType", "amount")} if tax_sorted else None,
        "tax_rows": len(tax_sorted),
        "account_totals": totals, "visible_total": visible,
        "stock_plus_spot": on_rows, "stock_plus_usdt": on_usdt,
        "invisible": inv,
        "value_visible": value_a, "value_with_estimates": value_b,
        "flows": ext,
        "by_kind": {k: {"n": v["n"], "usd": v["usd"], "coins": dict(v["coins"])} for k, v in by_kind.items()},
        "deposits_usd": deposits, "withdrawals_usd": withdrawals, "net_inflow_usd": net,
        "pnl_visible": value_a - net, "pnl_with_estimates": value_b - net,
        "pnl_pct_visible": (value_a - net) / net * 100 if net > 0 else None,
        "pnl_pct_with_estimates": (value_b - net) / net * 100 if net > 0 else None,
        "income": income_summary(f),
        "ledger_check": ledger_check(f, spot_qty),
        "p2p_orders": len(f["p2p"]),
        "price_misses": hp.misses,
        "errors": f["errors"] + [{"endpoint": k, "error": v} for k, v in c.errors().items() if k != "uta_assets"],
    }


KIND_NAMES = {
    "onchain_deposit": "鏈上充值", "fiat_bank_deposit": "法幣銀行入金", "internal_uid_deposit": "站內(其他UID)充值",
    "fiat_buy_settlement": "法幣餘額買幣結算（內部，不計）", "onchain_withdrawal": "鏈上提幣",
    "fiat_withdrawal": "法幣出金", "internal_uid_withdrawal": "站內(其他UID)提幣", "p2p_unparsed": "P2P（未解析）",
}


def render(p: dict) -> str:
    L = []
    f2 = lambda x: f"{x:,.2f}"
    L.append(f"=== 盈虧估算（唯讀）  {p['captured_oslo']}（Oslo） ===")
    L.append(f"最早紀錄：{p['earliest_record']}（{p['earliest_record_detail']}）；稅務帳本 {p['tax_rows']} 筆；查詢起點 {p['since']}")
    L.append("\n-- 目前價值（USDT）--")
    for k, v in p["account_totals"].items():
        L.append(f"  {k:8} {f2(v):>12}")
    L.append(f"  {'API可見合計':8} {f2(p['visible_total']):>12}")
    inv = p["invisible"]
    L.append(f"  + Stock+（…ON，以 rToken 代價）{f2(p['stock_plus_usdt'])}")
    poolx_txt = ", ".join("%s %.6g" % (r["coin"], r["qty"]) for r in inv["poolx"])
    L.append(f"  + PoolX 鎖倉（近90天帳單推算）{f2(inv['poolx_usdt'])}  {poolx_txt}")
    L.append(f"  + 雙幣投資未結算（推算）{f2(inv['dual_usdt'])}  {len(inv['dual_outstanding'])} 筆")
    L.append(f"  + 劃轉到 uta 淨額（近90天，成本）{f2(inv['uta_net'])}（出 {f2(inv['uta_out'])} / 回 {f2(inv['uta_in'])}）")
    L.append(f"  含推算合計 {f2(p['value_with_estimates'])}")
    L.append("\n-- 外部資金流（以當時價格折 USDT）--")
    for k, v in sorted(p["by_kind"].items()):
        coins = ", ".join(f"{c} {q:.8g}" for c, q in sorted(v["coins"].items()))
        L.append(f"  {KIND_NAMES.get(k, k):24} {v['n']:4} 筆  {f2(v['usd']):>12} U  [{coins}]")
    L.append(f"  總入金 {f2(p['deposits_usd'])}；總出金 {f2(p['withdrawals_usd'])}；淨入金 {f2(p['net_inflow_usd'])}")
    L.append("\n-- 盈虧 --")
    pct = lambda x: f"{x:+.1f}%" if x is not None else "n/a"
    L.append(f"  僅 API 可見：{p['pnl_visible']:+,.2f} U（{pct(p['pnl_pct_visible'])}）")
    L.append(f"  含推算部分：{p['pnl_with_estimates']:+,.2f} U（{pct(p['pnl_pct_with_estimates'])}）")
    lc = p["ledger_check"]
    L.append(f"\n帳本核對：{lc['coins']} 幣種，不符 {len(lc['mismatch'])}：{lc['mismatch'][:5]}")
    L.append(f"P2P 訂單：{p['p2p_orders']}；價格缺失：{len(p['price_misses'])}；讀取錯誤：{len(p['errors'])}")
    L.append("注意：uta/PoolX/雙幣只看得到近 90 天；Bitget Onchain、Launchpool、卡片等 API 看不到。")
    return "\n".join(L)
