"""Offline analysis: origin-traced average cost basis, realized / unrealized PnL, flows by month.

Inputs (local only): archive/**/<source>.jsonl (from `archive`), the latest holdings snapshot
(archive/*/snapshots or snapshots/), snapshots/analysis_overrides.json (optional manual inputs).
Public market data (no key): daily candles cached in snapshots/price_cache.json, current tickers.
Output: snapshots/analysis_latest.json (read by `web`).

Method (average cost, USDT):
- Each coin's inventory is split by origin (spot_buy, spot_grid_bot, dual_strike, deposit, reward, …)
  with two bases: `cost` = true cost (rewards 0, dual at strike, grid-bot coins at implied cost),
  `mark` = value at receipt (rewards / dual / bot coins at market price when received).
- Disposals reduce every origin proportionally. Realized PnL per coin uses the `mark` basis so that
  income, dual and bot results are attributed to their own categories, not to the coin.
"""
from __future__ import annotations

import bisect
import datetime as dt
import json
import time
from collections import Counter, defaultdict
from pathlib import Path

from . import pnl as pnl_mod
from . import rtokens as rt_mod
from . import valuation as val
from .client import OSLO, SNAPSHOT_DIR, cli_cmd, ms_to_oslo, now_oslo, public_get

DAY = 86_400_000
STABLE = {"USDT", "USDC", "USD1", "USDGO", "Cash+", "FDUSD", "DAI"}
FIAT = pnl_mod.FIAT
INCOME = {"batch_interest_user_in": "earn_interest", "Interest": "earn_interest", "Airdrop Reward-A": "airdrop",
          "Airdrop Reward-B": "airdrop", "Crypto Voucher Distribution": "voucher", "rebate_coupon_activity_user_in": "rebate"}
INTERNAL = {"financial_lock_out", "financial_unlock_in", "financial_pos_out", "financial_user_out",
            "poolx_redem_in", "poolx_sub_out", "Unlock locked order"}
DUAL_LABEL_START = dt.datetime(2026, 7, 6, 16, 9, tzinfo=OSLO)  # before this, dual freezes were unlabeled
FUT_BOT = {"trans_to_strategy", "trans_from_strategy", "user_grid_profit_in"}
FUT_INTERNAL = {"trans_from_exchange", "trans_to_exchange"}
FUT_BONUS = {"user_grants_issue", "user_grants_recycle", "bonus_issue", "bonus_recycle"}
DEFAULT_OVERRIDES = {
    "_doc": "Manual inputs the API cannot provide. Edit and re-run `analyze`. null = not used.",
    "app_total_assets_usdt": None,          # optional: total shown in the Bitget app (typed in the dashboard)
    "app_total_set_at": None,               # ISO time the app total was entered (stale after 24 h)
    "settlement_0704_cost_usdt": None,      # cost allocated to the 'System lock-up' tokens (unlabeled earlier outflows)
    "running_futures_bots_funded_usdt": None,  # amount put into the futures bots that are still running
    "running_spot_bots_funded_usdt": None,
    "mexc_earn_usdt": None,                 # MEXC Earn balance (no API; typed on the MEXC tab), fixed USDT value
    "mexc_earn_set_at": None,               # ISO time it was entered (stale after 30 days)
    "rtoken_baseline_date": None,           # optional YYYY-MM-DD: pin the rToken liquidation baseline (default: peak basket)
}
CATEGORY_NAMES = {
    "spot": "現貨交易（已實現）", "spot_bots": "現貨機器人（已關閉）", "futures_bots": "合約機器人（已關閉）",
    "futures_manual": "手動合約", "dual": "雙幣投資", "earn": "理財／空投／返佣收入", "futures_bonus": "合約體驗金／贈金",
    "fees": "手續費", "unknown": "無法歸類（API 看不到）",
}


# ---------------- inputs ----------------
def load_archive(root: Path) -> dict[str, list]:
    out: dict[str, list] = defaultdict(list)
    for f in sorted(root.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]/*.jsonl")):
        if f.name.startswith("._"):  # macOS AppleDouble
            continue
        with f.open(encoding="utf-8") as fh:
            out[f.stem].extend(json.loads(line) for line in fh if line.strip())
    return dict(out)


def latest_snapshot(roots: list[Path]) -> tuple[dict | None, Path | None]:
    files = [p for r in roots for p in list(r.glob("snapshot_*.json")) + list(r.glob("*/snapshots/snapshot_*.json"))]
    if not files:
        return None, None
    p = max(files, key=lambda p: p.name)
    return json.loads(p.read_text(encoding="utf-8")), p


def _bitget_raw_flows(archive_dir: Path) -> list[dict]:
    out = []
    for kind, name in (("deposit", "deposits"), ("withdrawal", "withdrawals")):
        seen = set()
        for f in sorted(archive_dir.glob(f"[0-9][0-9][0-9][0-9]-[0-9][0-9]/{name}.jsonl")):
            if f.name.startswith("._"):
                continue
            with f.open(encoding="utf-8") as fh:
                lines = fh.readlines()
            for line in lines:
                if not line.strip():
                    continue
                r = json.loads(line)
                key = r.get("orderId") or (r.get("cTime"), r.get("size"))
                if key in seen or str(r.get("status", "success")).lower() != "success":
                    continue
                seen.add(key)
                try:
                    out.append({"kind": kind, "coin": str(r.get("coin")).upper(), "qty": abs(float(r.get("size") or 0)),
                                "ms": int(r.get("cTime") or 0), "chain": r.get("chain"),
                                "addr": str(r.get("toAddress" if kind == "withdrawal" else "fromAddress") or "").lower()})
                except (TypeError, ValueError):
                    continue
    return out


STABLE_1 = {"USDT", "USDC", "USD1", "USDE", "FDUSD", "DAI", "TUSD", "PYUSD", "USDG"}


def cross_transfers(archive_dir: Path, mexc_flows: list[dict], max_hours: float = 6, rel_tol: float = 0.005,
                    price_at=None) -> dict:
    """Pair Bitget withdrawals with MEXC deposits (and vice versa): same coin, amount within 0.5 %, arriving 0–6 h later.
    Informational: the combined net inflow already cancels them (− on one exchange, + on the other)."""
    bg = _bitget_raw_flows(archive_dir)
    mx = []
    for f in mexc_flows:
        if f.get("kind") not in ("deposit", "withdrawal"):
            continue
        try:
            t = dt.datetime.fromisoformat(f["time"])
            ms = int((t if t.tzinfo else t.replace(tzinfo=OSLO)).timestamp() * 1000)
        except (KeyError, TypeError, ValueError):
            continue
        mx.append({**f, "ms": ms, "q": abs(f.get("qty") or 0)})
    used, pairs, learned, matched_bg = set(), [], {}, set()
    for direction, src_kind, dst_kind, src, dst in (
            ("bitget→mexc", "withdrawal", "deposit", bg, mx), ("mexc→bitget", "withdrawal", "deposit", mx, bg)):
        for a in sorted((x for x in src if x["kind"] == src_kind), key=lambda x: x["ms"]):
            qa = a.get("q", a.get("qty")) or 0
            best = None
            for i, b in enumerate(dst):
                if b["kind"] != dst_kind or (direction, id(b)) in used or str(b["coin"]).upper() != str(a["coin"]).upper():
                    continue
                qb = b.get("q", b.get("qty")) or 0
                lag = (b["ms"] - a["ms"]) / 3.6e6
                if 0 <= lag <= max_hours and qa and abs(qa - qb) <= max(rel_tol * qa, 1e-8):
                    if best is None or lag < best[0]:
                        best = (lag, b)
            if best:
                b = best[1]
                used.add((direction, id(b)))
                bgr = a if direction == "bitget→mexc" else b
                if bgr.get("addr"):
                    learned.setdefault(direction, set()).add(bgr["addr"])
                matched_bg.add(id(bgr))
                m = a if direction == "mexc→bitget" else b
                pairs.append({"direction": direction, "coin": a["coin"], "sent": qa, "received": b.get("q", b.get("qty")),
                              "usdt": abs(m.get("usd") or 0) or None, "sent_at": ms_to_oslo(a["ms"]),
                              "lag_min": round(best[0] * 60, 1)})
    tot = lambda d: sum(p["usdt"] or 0 for p in pairs if p["direction"] == d)
    # Before MEXC's API window: Bitget records to/from the addresses learned from matched pairs
    # (MEXC deposit address / MEXC hot wallet). MEXC itself can no longer return these.
    first = min((f["ms"] for f in mx), default=None)
    pre = {"to_mexc": 0.0, "from_mexc": 0.0, "count": 0, "unpriced": [], "first": None}
    if first:
        for r in bg:
            d = "bitget→mexc" if r["kind"] == "withdrawal" else "mexc→bitget"
            if id(r) in matched_bg or r["ms"] >= first or r.get("addr") not in learned.get(d, set()):
                continue
            px = 1.0 if r["coin"] in STABLE_1 else (price_at(r["coin"], r["ms"]) if price_at else 0.0)
            if not px:
                pre["unpriced"].append(f'{r["coin"]} {r["qty"]:g}')
                continue
            pre["to_mexc" if d == "bitget→mexc" else "from_mexc"] += r["qty"] * px
            pre["count"] += 1
            pre["first"] = min(pre["first"] or ms_to_oslo(r["ms"]), ms_to_oslo(r["ms"]))
    pre["net_to_mexc"] = pre["to_mexc"] - pre["from_mexc"]
    pre["before"] = ms_to_oslo(first) if first else None
    return {"count": len(pairs), "bitget_to_mexc_usdt": tot("bitget→mexc"), "mexc_to_bitget_usdt": tot("mexc→bitget"),
            "pairs": pairs, "before_mexc_window": pre,
            "note": "Bitget 與 MEXC 之間的互轉：在各交易所分別算作入金／出金，合併淨入金中正負抵銷（不需調整）。"}


def _flow_ms(f: dict) -> int | None:
    if f.get("ms"):
        return int(f["ms"])
    try:
        t = dt.datetime.fromisoformat(f["time"])
        return int((t if t.tzinfo else t.replace(tzinfo=OSLO)).timestamp() * 1000)
    except (KeyError, TypeError, ValueError):
        return None


def match_cross(legs: list[dict], max_hours: float = 12, rel_tol: float = 0.02) -> list[dict]:
    """Generic cross-exchange matcher (Bitget / MEXC / Kraken): a withdrawal on one exchange and a deposit on another,
    same coin, received ≤ sent and ≥ sent × (1 − 2 %) (network fee), arriving 0–12 h later. Closest arrival wins.
    Each leg: {exchange, kind, coin, qty>0, ms, usd>0|None, counted: bool (included in that exchange's net inflow)}."""
    deps = sorted((x for x in legs if x["kind"] == "deposit"), key=lambda x: x["ms"])
    used, pairs = set(), []
    for a in sorted((x for x in legs if x["kind"] == "withdrawal"), key=lambda x: x["ms"]):
        best = None
        for i, b in enumerate(deps):
            if i in used or b["exchange"] == a["exchange"] or b["coin"] != a["coin"]:
                continue
            lag = (b["ms"] - a["ms"]) / 3.6e6
            if lag < 0:
                continue
            if lag > max_hours:
                break
            if a["qty"] and a["qty"] * (1 - rel_tol) - 1e-9 <= b["qty"] <= a["qty"] * 1.0005 + 1e-9:
                if best is None or lag < best[0]:
                    best = (lag, i, b)
        if best:
            lag, i, b = best
            used.add(i)
            pairs.append({"direction": f"{a['exchange']}→{b['exchange']}", "coin": a["coin"], "sent": a["qty"], "received": b["qty"],
                          "sent_usdt": a.get("usd"), "received_usdt": b.get("usd"), "sent_at": ms_to_oslo(a["ms"]),
                          "lag_min": round(lag * 60, 1), "both_counted": bool(a.get("counted") and b.get("counted")),
                          "counted": {a["exchange"]: bool(a.get("counted")), b["exchange"]: bool(b.get("counted"))}})
    return pairs


def cross_legs(archive_dir: Path, mx: dict, kr: dict, price_at=None) -> list[dict]:
    legs = []
    px = lambda c, ms: 1.0 if c in STABLE_1 else (price_at(c, ms) if price_at else None)
    for r in _bitget_raw_flows(archive_dir):
        p = px(r["coin"], r["ms"])
        legs.append({"exchange": "bitget", "kind": r["kind"], "coin": r["coin"], "qty": r["qty"], "ms": r["ms"],
                     "usd": r["qty"] * p if p else None, "counted": True})
    if mx.get("present"):
        b_ms = None
        if mx.get("baseline") and mx["baseline"].get("at"):
            b_ms = _flow_ms({"time": mx["baseline"]["at"]})
        for f in mx.get("flows") or []:
            ms = _flow_ms(f)
            if f.get("kind") not in ("deposit", "withdrawal") or not ms:
                continue
            legs.append({"exchange": "mexc", "kind": f["kind"], "coin": str(f.get("coin")).upper(), "qty": abs(f.get("qty") or 0),
                         "ms": ms, "usd": abs(f["usd"]) if f.get("usd") is not None else None, "counted": b_ms is None or ms >= b_ms})
    if kr.get("present"):
        for f in kr.get("flows") or []:
            ms = _flow_ms(f)
            if not ms or f.get("fiat"):
                continue
            legs.append({"exchange": "kraken", "kind": f["kind"], "coin": f["coin"], "qty": abs(f.get("qty") or 0), "ms": ms,
                         "usd": abs(f["usd"]) if f.get("usd") is not None else None, "counted": True})
    return legs


def load_overrides(path: Path) -> dict:
    if not path.exists():
        path.write_text(json.dumps(DEFAULT_OVERRIDES, indent=1, ensure_ascii=False), encoding="utf-8")
        return dict(DEFAULT_OVERRIDES)
    return {**DEFAULT_OVERRIDES, **json.loads(path.read_text(encoding="utf-8"))}


class Prices:
    """Daily close (Oslo-agnostic UTC day index) per coin, cached on disk; public candles only."""

    def __init__(self, path: Path, offline: bool = False):
        self.path, self.offline = path, offline
        doc = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        self.tab: dict[str, dict[int, float]] = {c: {int(k): v for k, v in t.items()} for c, t in (doc.get("prices") or {}).items()}
        self.keys = {c: sorted(t) for c, t in self.tab.items()}
        self.misses: list = []
        self.fetched = 0

    @staticmethod
    def symbol(c: str) -> tuple[str, bool]:
        if c in FIAT:
            return f"USDT{c}", True
        if val.classify(c) == "stock_plus":
            return f"r{c[:-2]}USDT", False
        return f"{c}USDT", False

    def ensure(self, coins, since_ms: int) -> None:
        if self.offline:
            return
        today = int(time.time() * 1000) // DAY
        for c in sorted(set(coins) - STABLE):
            have = self.keys.get(c) or []
            if have and have[-1] >= today - 1 and have[0] <= since_ms // DAY + 1:
                continue
            sym, inv = self.symbol(c)
            tab = dict(self.tab.get(c) or {})
            now = int(time.time() * 1000)
            ends = [now] if have and have[0] <= since_ms // DAY + 1 else [now, now - 199 * DAY]
            code = None
            for end in ends:
                time.sleep(0.12)
                r = public_get("/api/v2/spot/market/history-candles", {"symbol": sym, "granularity": "1day", "endTime": end, "limit": 200})
                self.fetched += 1
                code = r.get("code")
                for k in r.get("data") or []:
                    p = float(k[4])
                    if p > 0:
                        tab[int(k[0]) // DAY] = 1 / p if inv else p
            if tab:
                self.tab[c] = tab
                self.keys[c] = sorted(tab)
            else:
                self.misses.append({"coin": c, "symbol": sym, "code": code})
        self.path.write_text(json.dumps({"updated": now_oslo().isoformat(timespec="seconds"),
                                         "prices": {c: {str(k): v for k, v in t.items()} for c, t in self.tab.items()}}),
                             encoding="utf-8")

    def at(self, c: str, ts) -> float:
        if c in STABLE:
            return 1.0
        ks = self.keys.get(c)
        if not ks:
            return 0.0
        i = bisect.bisect_right(ks, int(ts) // DAY) - 1
        return self.tab[c][ks[max(i, 0)]]

    def range(self, c: str, t0, t1) -> tuple[float, float]:
        if c in STABLE:
            return 1.0, 1.0
        v = [self.tab[c][k] for k in self.keys.get(c, []) if int(t0) // DAY <= k <= int(t1) // DAY] or [self.at(c, t1)]
        return min(v), max(v)

    def last(self, c: str) -> float:
        return self.at(c, int(time.time() * 1000))


# ---------------- inventory ----------------
class Inventory:
    """coin -> origin -> [qty, cost, mark]; proportional (average-cost) disposal."""

    def __init__(self):
        self.inv: dict = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0, 0.0]))
        self.deficit: Counter = Counter()

    def acq(self, c, q, cost, origin, mark=None):
        mark = cost if mark is None else mark
        if q <= 0:
            return
        if self.deficit[c] > 0:  # cover an earlier oversell first (ledger ordering gaps)
            cover = min(q, self.deficit[c]); f = cover / q
            self.deficit[c] -= cover; q -= cover; cost *= 1 - f; mark *= 1 - f
            if q <= 0:
                return
        x = self.inv[c][origin]; x[0] += q; x[1] += cost; x[2] += mark

    def disp(self, c, q) -> tuple[float, float]:
        """Remove q units; returns (cost basis, mark basis) removed."""
        if q <= 0:
            return 0.0, 0.0
        tot = self.qty(c)
        if tot <= 1e-18:
            self.deficit[c] += q
            return 0.0, 0.0
        f = min(1.0, q / tot); bc = bm = 0.0
        for v in self.inv[c].values():
            bc += v[1] * f; bm += v[2] * f
            v[0] *= 1 - f; v[1] *= 1 - f; v[2] *= 1 - f
        if q > tot:
            self.deficit[c] += q - tot
        return bc, bm

    def qty(self, c):
        return sum(v[0] for v in self.inv[c].values())

    def totals(self, c):
        q = c_ = m = 0.0
        for v in self.inv[c].values():
            q += v[0]; c_ += v[1]; m += v[2]
        return q, c_, m


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


# ---------------- main computation ----------------
def compute(arch: dict, snap: dict, prices: Prices, tickers: dict, ov: dict) -> dict:
    rows = sorted(arch.get("tax_spot", []), key=lambda r: (int(r["ts"]), int(r["id"])))
    fut = arch.get("tax_future", [])
    CUT = int(DUAL_LABEL_START.timestamp() * 1000)
    is_r = lambda c: c.startswith("r") and c[1:2].isupper()
    cash_ids = {r["bizOrderId"] for r in rows if r["coin"] == "Cash+"}
    non_cash_ids = {r["bizOrderId"] for r in rows if r["coin"] != "Cash+"}
    px = prices.at

    def cur(c):
        if c in STABLE:
            return 1.0
        return pnl_mod.current_price(c, tickers) or prices.last(c) or 0.0

    # --- dual matching: freeze (blank, after labels exist) ↔ Redemption ---
    freezes = [r for r in rows if r["spotTaxType"] == "" and _f(r["amount"]) < 0 and int(r["ts"]) >= CUT
               and r["coin"] not in FIAT | {"Cash+", "BGB"} and r["bizOrderId"] not in cash_ids]
    freeze_ids = {r["id"] for r in freezes}
    reds = [r for r in rows if r["spotTaxType"] == "Redemption"]
    used_f: set = set(); role: dict = {}
    for r in reds:  # same coin returned = principal back (internal)
        q = _f(r["amount"])
        cs = [f for f in freezes if f["id"] not in used_f and f["coin"] == r["coin"] and int(f["ts"]) < int(r["ts"]) and abs(-_f(f["amount"]) - q) < 1e-6]
        if cs:
            m = max(cs, key=lambda f: int(f["ts"])); used_f.add(m["id"]); role[m["id"]] = role[r["id"]] = ("internal", 0.0)
    dual_conv = []
    for r in reds:
        if r["id"] in role or r["coin"] == "USDT":
            continue
        q = _f(r["amount"]); mk = px(r["coin"], r["ts"])
        cands = [f for f in freezes if f["id"] not in used_f and f["coin"] == "USDT" and 0 < int(r["ts"]) - int(f["ts"]) < 10 * DAY]
        cands = [f for f in cands if mk and abs(-_f(f["amount"]) / q - mk) / mk < 0.25]  # strike must be near market
        if cands:
            m = min(cands, key=lambda f: abs(-_f(f["amount"]) / q - mk) / mk)
            used_f.add(m["id"]); X = -_f(m["amount"]); role[r["id"]] = ("conv", X); role[m["id"]] = ("conv_freeze", X)
            dual_conv.append({"time": ms_to_oslo(r["ts"]), "coin": r["coin"], "qty": q, "usdt": X, "strike": X / q,
                              "market": mk, "freeze_time": ms_to_oslo(m["ts"])})
        elif is_r(r["coin"]):
            role[r["id"]] = ("conv_mkt", q * mk)
        # other Redemption rows (e.g. PoolX/lock products returning the same coin) stay internal

    # --- spot grid-bot episodes ---
    fund = [r for r in rows if r["spotTaxType"] == "Automatic withdrawal"]
    groups: list = []
    for r in (r for r in rows if r["spotTaxType"] == "Automatic deposit"):
        if groups and int(r["ts"]) - int(groups[-1][-1]["ts"]) < 3000:
            groups[-1].append(r)
        else:
            groups.append([r])
    used: set = set(); episodes = []; bot_cost: dict = {}
    for g in groups:
        ts = int(g[0]["ts"]); usdt = sum(_f(x["amount"]) for x in g if x["coin"] in STABLE)
        coins = [x for x in g if x["coin"] not in STABLE]
        mv = {x["id"]: _f(x["amount"]) * px(x["coin"], ts) for x in coins}
        M = usdt + sum(mv.values())
        cands = [f for f in fund if f["id"] not in used and 0 < ts - int(f["ts"]) < 45 * DAY]
        m = min(cands, key=lambda f: abs(-_f(f["amount"]) - M) / max(M, 1e-9) + 0.004 * (ts - int(f["ts"])) / DAY) if cands else None
        matched = m is not None and abs(-_f(m["amount"]) - M) / max(M, 1e-9) < 0.35
        ep = {"close": ms_to_oslo(ts), "close_ms": ts, "usdt_back": usdt, "coins": {x["coin"]: _f(x["amount"]) for x in coins},
              "market_value_back": M, "fund": None, "fund_time": None, "flags": [], "unit_cost": {}, "range": {}}
        if matched:
            used.add(m["id"]); F = -_f(m["amount"]); ep["fund"] = F; ep["fund_time"] = ms_to_oslo(m["ts"])
            implied = F - usdt; tmv = sum(mv.values())
            for x in coins:
                c, q = x["coin"], _f(x["amount"])
                cost = max(implied, 0.0) * mv[x["id"]] / tmv if tmv > 0 else 0.0
                lo, hi = prices.range(c, m["ts"], ts); u = cost / q if q else 0.0
                if q * px(c, ts) >= 1:
                    if implied < 0:
                        ep["flags"].append(f"{c}: 返還 USDT 已超過投入，幣成本記 0")
                    elif u < lo * 0.85 or u > hi * 1.15:
                        ep["flags"].append(f"{c}: 推算成本 {u:.4g} 超出期間價格 {lo:.4g}–{hi:.4g}")
                bot_cost[x["id"]] = cost; ep["unit_cost"][c] = u; ep["range"][c] = [lo, hi]
            ep["pnl_cost"] = usdt + sum(bot_cost[x["id"]] for x in coins) - F     # realized (coins at implied cost)
            ep["pnl_mark"] = M - F                                                  # coins marked at close
        else:
            for x in coins:
                bot_cost[x["id"]] = mv[x["id"]]
            ep["flags"].append("找不到對應的投入紀錄；返還的幣以當時市價計")
        episodes.append(ep)
    unmatched_fund = [{"time": ms_to_oslo(f["ts"]), "usdt": -_f(f["amount"])} for f in fund if f["id"] not in used]

    # --- 07-04 settlement tokens ('System lock-up') ---
    lock = [r for r in rows if r["spotTaxType"] == "System lock-up"]
    lock_mv = {r["id"]: _f(r["amount"]) * px(r["coin"], r["ts"]) for r in lock}
    lock_total = sum(lock_mv.values())
    settle_cost = ov.get("settlement_0704_cost_usdt")

    # --- walk the ledger ---
    inv = Inventory()
    real_coin: Counter = Counter()      # realized PnL per coin (mark basis)
    cat: Counter = Counter()            # category totals
    cat_detail: dict = defaultdict(Counter)
    trades = defaultdict(list)
    for r in rows:
        if r["spotTaxType"] in ("Buy", "Sell"):
            trades[r["bizOrderId"]].append(r)

    def dispose(c, q, proceeds):
        _, bm = inv.disp(c, q)
        if c not in STABLE:  # stablecoins are the unit of account (no PnL on 1:1 moves)
            real_coin[c] += proceeds - bm

    def fee_out(c, q, ts):
        if q <= 0:
            return
        v = q * px(c, ts)
        dispose(c, q, v)
        cat["fees"] -= v; cat_detail["fees"]["trading"] += v

    acct_moves = [(int(r["ts"]), _f(r["amount"])) for r in fut if r["futureTaxType"] in FUT_INTERNAL | {"trans_to_isolated", "trans_from_isolated"}]
    acct_moves += [(int(r["ts"]), _f(r["amount"])) for r in arch.get("tax_margin", [])]

    def _matches_account_move(ts, a):  # spot leg of a spot↔futures/margin transfer (internal)
        return any(abs(t - ts) < 5000 and abs(abs(x) - abs(a)) < 1e-4 and x * a < 0 for t, x in acct_moves)

    done = set()
    for r in rows:
        T, c, a, fee, ts = r["spotTaxType"], r["coin"], _f(r["amount"]), _f(r["fee"]), int(r["ts"])
        p = px(c, ts)
        if T in ("Buy", "Sell"):
            oid = r["bizOrderId"]
            if oid in done:
                continue
            done.add(oid); legs = trades[oid]
            b = next((x for x in legs if x["spotTaxType"] == "Buy"), None); s = next((x for x in legs if x["spotTaxType"] == "Sell"), None)
            if not b or not s:
                continue
            qb, qs = _f(b["amount"]), -_f(s["amount"])
            V = qb if b["coin"] in STABLE else qs if s["coin"] in STABLE else qs * px(s["coin"], ts) if s["coin"] in FIAT else qb * px(b["coin"], ts)
            origin = "eur_purchase" if s["coin"] in FIAT else "spot_buy"
            inv.acq(b["coin"], qb, V, origin); dispose(s["coin"], qs, V)
            fee_out(b["coin"], -_f(b["fee"]), ts); fee_out(s["coin"], -_f(s["fee"]), ts)
            continue
        if T == "Transaction fee deduct":
            fee_out(c, -fee, ts); continue
        if T in INTERNAL:
            continue
        if c == "Cash+" or r["bizOrderId"] in cash_ids:
            if c == "Cash+" and a > 0 and r["bizOrderId"] not in non_cash_ids:
                inv.acq(c, a, 0.0, "reward", a); cat["earn"] += a; cat_detail["earn"]["Cash+ interest"] += a
            elif a > 0:
                inv.acq(c, a, a, "cash+")
            else:
                dispose(c, -a, -a)
            continue
        if T == "System lock-up":
            cost = settle_cost * lock_mv[r["id"]] / lock_total if settle_cost is not None and lock_total else lock_mv[r["id"]]
            inv.acq(c, a, cost, "settlement_0704", lock_mv[r["id"]])
            cat["unknown"] += lock_mv[r["id"]]; cat_detail["unknown"]["07-04 System lock-up tokens (in)"] += lock_mv[r["id"]]
            continue
        if T == "Redemption" or r["id"] in role:
            rl = role.get(r["id"])
            if rl is None or rl[0] == "internal":
                continue
            if rl[0] == "conv_freeze":
                dispose(c, -a, -a * p); cat["dual"] -= -a * p; cat_detail["dual"]["USDT frozen for conversions"] -= -a * p; continue
            if rl[0] in ("conv", "conv_mkt"):
                inv.acq(c, a, rl[1], "dual_strike" if rl[0] == "conv" else "dual_market", a * p)
                cat["dual"] += a * p; cat_detail["dual"][f"{c} received (market)"] += a * p; continue
        if T == "Redemption" and c == "USDT":
            inv.acq(c, a, a, "dual_settle"); cat["dual"] += a; cat_detail["dual"]["USDT settled"] += a; continue
        if T == "Deposit":
            inv.acq(c, a, a * p, "deposit"); continue
        if T == "Withdrawal":
            dispose(c, -a, -a * p)
            if fee:
                fee_v = -fee * p; dispose(c, -fee, fee_v); cat["fees"] -= fee_v; cat_detail["fees"]["withdrawal"] += fee_v
            continue
        if T.startswith("FIAT_BANK") or (T == "" and c in FIAT and a > 0):
            inv.acq(c, a, a * p, "fiat_deposit"); continue
        if T in INCOME or (T == "" and c == "USDT" and 0 < a < 1 and ts >= CUT):
            k = INCOME.get(T, "rwa_distribution")
            inv.acq(c, a, 0.0, "reward", a * p); cat["earn"] += a * p; cat_detail["earn"][k] += a * p; continue
        if T in ("Exchange spending", "Exchange income", "Gains", "Consumption", "FIAT_BALANCE_BUY_OR_SELL_USER_OUT"):
            if a > 0:
                inv.acq(c, a, a * p, "convert")
            else:
                dispose(c, -a, -a * p)
            continue
        if T == "Automatic deposit":
            if c in STABLE:
                inv.acq(c, a, a, "spot_grid_bot")
            else:
                inv.acq(c, a, bot_cost.get(r["id"], a * p), "spot_grid_bot", a * p)
            continue
        if T == "Automatic withdrawal":
            dispose(c, -a, -a * p); continue
        if T in ("Transfer in", "Transfer out"):
            if a > 0:
                inv.acq(c, a, a * p, "transfer_in")
            else:
                dispose(c, -a, -a * p)
            if not _matches_account_move(ts, a):
                k = "transfers in (uta / other UID / bot UIDs)" if a > 0 else "transfers out (uta / other UID / bot UIDs)"
                cat["unknown"] += a * p; cat_detail["unknown"][k] += a * p
            continue
        if T == "" and r["id"] in freeze_ids:  # dual freeze whose principal never came back as the same coin/amount
            dispose(c, -a, -a * p); cat["dual"] -= -a * p; cat_detail["dual"]["frozen (settled as other coin/amount)"] -= -a * p
            continue
        if T == "":
            if a > 0:
                inv.acq(c, a, a * p, "other_in:unlabeled")
            else:
                dispose(c, -a, -a * p)
            k = "pre-July unlabeled" if ts < CUT else "unlabeled"
            cat["unknown"] += a * p; cat_detail["unknown"][k] += a * p
            continue
        if a > 0:
            inv.acq(c, a, a * p, "other_in:" + T)
        else:
            dispose(c, -a, -a * p)

    # spot bots realized = closed matched episodes (coins at implied cost)
    for ep in episodes:
        if ep["fund"] is not None:
            cat["spot_bots"] += ep["pnl_cost"]
        else:
            cat["unknown"] += ep["market_value_back"]; cat_detail["unknown"]["bot returns without funding match"] += ep["market_value_back"]
    cat["unknown"] -= sum(f["usdt"] for f in unmatched_fund)
    cat_detail["unknown"]["bot fundings without a closing match (incl. running bots)"] -= sum(f["usdt"] for f in unmatched_fund)

    # --- futures ledger ---
    fsum: Counter = Counter()
    for r in fut:
        fsum[r["futureTaxType"]] += _f(r["amount"]) + _f(r["fee"])
    fut_fees = -sum(_f(r["fee"]) for r in fut if r["futureTaxType"] not in FUT_INTERNAL | FUT_BOT)
    run_fb_fund = ov.get("running_futures_bots_funded_usdt")
    bots_run = [b for b in snap.get("bots", []) if b.get("usdtValue")]
    run_fb_value = sum(b["usdtValue"] for b in bots_run if b["accountType"] == "futures")
    run_sb_value = sum(b["usdtValue"] for b in bots_run if b["accountType"] == "spot")
    fb_net = fsum["trans_from_strategy"] + fsum["user_grid_profit_in"] + fsum["trans_to_strategy"]
    cat["futures_bots"] = fb_net + (run_fb_fund or 0.0)
    manual = {k: v for k, v in fsum.items() if k not in FUT_INTERNAL | FUT_BOT | FUT_BONUS}
    cat["futures_manual"] = sum(manual.values()) + fut_fees   # amounts net of fees; fees shown separately
    cat["fees"] -= fut_fees; cat_detail["fees"]["futures"] += fut_fees
    cat["futures_bonus"] = sum(fsum[k] for k in FUT_BONUS)
    cat_detail["futures_manual"] = Counter({k: v for k, v in manual.items()})

    # --- reconcile inventory to actual holdings ---
    actual: Counter = Counter()
    for c, q in (snap.get("spot_qty") or {}).items():
        actual[c] += q
    for c, q in (snap.get("earn") or {}).items():
        actual[c] += q
    # PoolX still locked = subscriptions − redemptions in the all-time tax ledger (bills only cover ~90 days)
    poolx: Counter = Counter()
    for r in rows:
        if r["spotTaxType"] in ("poolx_sub_out", "poolx_redem_in"):
            poolx[r["coin"]] -= _f(r["amount"])
    if not any(q > 1e-12 for q in poolx.values()):
        for b in arch.get("spot_bills", []):
            if "POOL_X" in str(b.get("businessType")):
                poolx[b["coin"]] -= _f(b["size"])
    for c, q in poolx.items():
        if q > 1e-12:
            actual[c] += q
    holdings = []
    for c in sorted(actual, key=lambda c: -actual[c] * cur(c)):
        q = actual[c]; p = cur(c)
        if q * p < 0.01 and c not in STABLE:
            continue
        diff = q - inv.qty(c)
        if diff > 1e-12:
            inv.acq(c, diff, 0.0, "earn_accrued", diff * p)
            cat["earn"] += diff * p; cat_detail["earn"]["accrued in Earn (not in spot ledger)"] += diff * p
        elif diff < -1e-12:
            _, bm = inv.disp(c, -diff)
            cat["unknown"] -= bm; cat_detail["unknown"]["ledger shortfall vs holdings"] -= bm
        tq, tc, tm = inv.totals(c)
        origins = [{"origin": o, "qty": v[0], "avg_cost": v[1] / v[0] if v[0] else 0.0, "cost": v[1]}
                   for o, v in sorted(inv.inv[c].items(), key=lambda kv: -kv[1][0]) if v[0] > 1e-12 * max(1.0, q)]
        main = {o["origin"] for o in origins if o["qty"] > 0.01 * tq}
        conf = ("high" if main <= {"spot_buy", "eur_purchase", "reward", "dual_settle", "convert", "cash+"} else
                "low" if main & {"settlement_0704", "earn_accrued", "dual_market", "other_in:unlabeled", "transfer_in"} else "medium")
        value = q * p
        holdings.append({"coin": c, "qty": q, "avg_cost": tc / q if q else 0.0, "total_cost": tc, "mark_cost": tm,
                         "price": p, "value": value, "unrealized": value - tc, "unrealized_pct": (value - tc) / tc * 100 if tc > 0 else None,
                         "unrealized_mark": value - tm, "in_poolx": poolx.get(c, 0.0) if poolx.get(c, 0) > 1e-12 else 0.0,
                         "ledger_diff_qty": diff, "origins": origins, "confidence": conf, "sleeve": val.classify(c)})
    for kind, value, funded in (("futures", run_fb_value, run_fb_fund), ("spot", run_sb_value, ov.get("running_spot_bots_funded_usdt"))):
        if value:
            holdings.append({"coin": f"{'合約' if kind == 'futures' else '現貨'}機器人（運行中）", "qty": None, "avg_cost": None,
                             "total_cost": funded, "mark_cost": funded, "price": None, "value": value,
                             "unrealized": value - funded if funded is not None else None,
                             "unrealized_pct": (value - funded) / funded * 100 if funded else None, "unrealized_mark": None,
                             "in_poolx": 0.0, "ledger_diff_qty": 0.0, "origins": [{"origin": "bot_funding", "qty": None, "avg_cost": None, "cost": funded}],
                             "confidence": "medium" if funded is not None else "low", "sleeve": "bot"})

    for c_, q_ in inv.deficit.items():
        if q_ > 1e-9:  # sold/transferred more than the ledger ever delivered
            v = q_ * (1.0 if c_ in STABLE else px(c_, rows[-1]["ts"]))
            cat["unknown"] += v; cat_detail["unknown"]["units spent beyond ledger inflows (deficit)"] += v

    # --- realized by coin: drop stablecoin noise ---
    coin_rows = sorted(({"coin": c, "realized": v} for c, v in real_coin.items() if abs(v) >= 0.005 and c not in STABLE), key=lambda r: r["realized"])
    cat["spot"] = sum(r["realized"] for r in coin_rows)

    # --- external flows & net inflow ---
    f = {"tax_spot": rows, "deposits": arch.get("deposits", []), "withdrawals": arch.get("withdrawals", []), "p2p": arch.get("p2p_orders", [])}
    ext = pnl_mod.classify_external(f)
    for e in ext:
        e["usd"] = e["qty"] * px(e["coin"], e["ts"])
    inflow = sum(e["usd"] for e in ext if e["kind"] in ("onchain_deposit", "fiat_bank_deposit", "internal_uid_deposit"))
    outflow = -sum(e["usd"] for e in ext if e["kind"] in ("onchain_withdrawal", "fiat_withdrawal", "internal_uid_withdrawal"))
    net_in = inflow - outflow

    # --- monthly flows ---
    month = lambda ts: dt.datetime.fromtimestamp(int(ts) / 1000, OSLO).strftime("%Y-%m")
    mon: dict = defaultdict(Counter)
    for e in ext:
        k = {"onchain_deposit": "deposit", "fiat_bank_deposit": "deposit", "internal_uid_deposit": "deposit",
             "onchain_withdrawal": "withdrawal", "fiat_withdrawal": "withdrawal", "internal_uid_withdrawal": "withdrawal"}.get(e["kind"])
        if k:
            mon[month(e["ts"])][k] += e["usd"]
    for r in rows:
        T = r["spotTaxType"]
        k = {"Transfer in": "transfer_in", "Transfer out": "transfer_out", "Automatic deposit": "bot_return",
             "Automatic withdrawal": "bot_funding"}.get(T)
        if k:
            mon[month(r["ts"])][k] += _f(r["amount"]) * px(r["coin"], r["ts"])
    for r in fut:
        if r["futureTaxType"] in ("trans_to_strategy", "trans_from_strategy"):
            mon[month(r["ts"])]["futures_bot_" + ("funding" if r["futureTaxType"] == "trans_to_strategy" else "return")] += _f(r["amount"])
    for t in arch.get("transfer_records", []):
        if "uta" in (t.get("toType"), t.get("fromType")):
            mon[month(t["ts"])]["uta_out" if t.get("toType") == "uta" else "uta_in"] += _f(t["size"]) * px(t.get("coin", "USDT"), t["ts"])
    months = [{"month": m, **{k: round(v, 6) for k, v in mon[m].items()}} for m in sorted(mon)]

    # --- uta: money moved to the unified account (invisible in Classic mode), kept at cost ---
    uta_out = sum(_f(t["size"]) * px(t.get("coin", "USDT"), t["ts"]) for t in arch.get("transfer_records", []) if t.get("toType") == "uta")
    uta_in = sum(_f(t["size"]) * px(t.get("coin", "USDT"), t["ts"]) for t in arch.get("transfer_records", []) if t.get("fromType") == "uta")
    uta_net = max(0.0, uta_out - uta_in)
    if uta_net:
        cat["unknown"] += uta_net; cat_detail["unknown"]["uta held (at cost, counted in total)"] += uta_net

    # --- overview ---
    totals = snap.get("account_totals_usdt") or {}
    visible = sum(totals.values())
    hold_value = sum(h["value"] for h in holdings if h["sleeve"] != "bot")
    poolx_value = sum(q * cur(c) for c, q in poolx.items() if q > 1e-12)
    fut_equity = sum((snap.get("futures_equity") or {}).values())
    other_acc = sum(v for k, v in totals.items() if k in ("funding", "margin"))
    spot_live = sum(q * cur(c) for c, q in (snap.get("spot_qty") or {}).items())
    earn_live = sum(q * cur(c) for c, q in (snap.get("earn") or {}).items())
    components = {
        "spot_earn_live": hold_value - poolx_value, "poolx_live": poolx_value, "futures": fut_equity,
        "bots_futures": run_fb_value, "bots_spot": run_sb_value, "uta_at_cost": uta_net, "funding_margin": other_acc,
    }
    est_total = sum(components.values())
    # cross-check against Bitget's own per-account valuation (all-account-balance, captured with the snapshot)
    ours = {"spot": spot_live, "earn": earn_live, "futures": fut_equity, "bots": run_fb_value + run_sb_value,
            "funding": totals.get("funding", 0.0), "margin": totals.get("margin", 0.0)}
    crosscheck = [{"account": k, "bitget": totals.get(k), "ours": ours.get(k), "diff": (ours.get(k) or 0.0) - (totals.get(k) or 0.0)}
                  for k in sorted(set(totals) | set(ours), key=lambda k: list(ours).index(k) if k in ours else 99)]
    invisible = [
        {"item": "poolx", "value": poolx_value, "method": "ledger (poolx_sub_out − poolx_redem_in) × live price",
         "coins": {c: q for c, q in poolx.items() if q > 1e-12}},
        {"item": "uta", "value": uta_net, "method": "net transfers spot↔uta at cost (uta balance not readable in Classic mode, 40084)"},
    ]
    unreal = sum(h["unrealized"] for h in holdings if h["unrealized"] is not None)
    unknown_cats = {"unknown"}
    realized = sum(v for k, v in cat.items() if k not in unknown_cats)
    categories = [{"key": k, "name": CATEGORY_NAMES.get(k, k), "realized": cat.get(k, 0.0),
                   "detail": {a: b for a, b in cat_detail.get(k, {}).items() if abs(b) >= 0.005}} for k in CATEGORY_NAMES]
    overview = {
        "total_auto": est_total, "total_components": components,
        "total_bitget_api": visible, "total_bitget_api_plus_invisible": visible + poolx_value + uta_net,
        "crosscheck": crosscheck, "invisible": invisible,
        "total_visible": visible, "account_totals": totals,
        "pnl_auto": est_total - net_in, "pnl_auto_pct": (est_total - net_in) / net_in * 100 if net_in > 0 else None,
        "deposits": inflow, "withdrawals": outflow, "net_inflow": net_in,
        "pnl_visible": visible - net_in, "pnl_visible_pct": (visible - net_in) / net_in * 100 if net_in > 0 else None,
        "realized": realized, "unrealized": unreal, "unknown": cat.get("unknown", 0.0),
        "gains": sum(c["realized"] for c in categories if c["realized"] > 0 and c["key"] not in unknown_cats),
        "losses": sum(c["realized"] for c in categories if c["realized"] < 0 and c["key"] not in unknown_cats),
        "residual": (est_total - net_in) - realized - unreal - cat.get("unknown", 0.0),
        "snapshot_time": snap.get("captured_oslo"), "prices": "live tickers" if tickers else "cached daily close",
        "uta_window": "transfer_records since archive start (~90 days before first archive run)",
    }
    try:   # rToken liquidation progress (never breaks the main analysis)
        rtoken = rt_mod.progress(rows, dict(actual), cur, px, est_total, ext, ov)
    except Exception as e:  # noqa: BLE001
        rtoken = {"present": False, "reason": f"rToken 進度計算失敗：{type(e).__name__}: {e}"[:300]}
    first = rows[0]["ts"] if rows else None
    return {
        "rtoken_progress": rtoken,
        "overview": overview, "holdings": holdings, "categories": categories, "realized_by_coin": coin_rows,
        "dual_conversions": dual_conv, "spot_bot_episodes": episodes, "unmatched_bot_fundings": unmatched_fund,
        "futures": {"by_type": dict(fsum), "bots_net_flow": fb_net, "running_value": run_fb_value, "running_funded": run_fb_fund,
                    "fees": fut_fees},
        "flows_by_month": months, "external_flows": [{k: e.get(k) for k in ("time", "kind", "coin", "qty", "usd", "chain")} for e in ext],
        "first_record": ms_to_oslo(first) if first else None,
        "inventory_deficits": {c: q for c, q in inv.deficit.items() if q > 1e-9},
    }


DATA_NOTES = [
    ("uta", "劃轉到 `uta`（統一帳戶）後的資金：經典帳戶模式下 v3 端點回 40084，看不到餘額與去向。", "confirmed"),
    ("PoolX", "PoolX 參與明細沒有端點；只能從現貨帳單 POOL_X 鎖入/贖回推算仍鎖定的數量（僅近 90 天帳單）。", "confirmed"),
    ("雙幣投資", "沒有雙幣訂單／持倉端點；只看得到凍結與釋放帳單，兩者沒有共同訂單 ID，配對與行權價為推算。", "confirmed"),
    ("網格／機器人", "沒有網格歷史或機器人成交端點；bot-assets 回空清單。投入與返還只能用金額與時間配對（推算）。", "confirmed"),
    ("返還紀錄", "機器人返還在稅務帳本中為 Automatic deposit，沒有機器人 ID。", "confirmed"),
    ("Stock+", "…ON 代幣沒有公開現貨行情（40034），以對應 rToken 價格代估。", "inferred"),
    ("90 天限制", "現貨帳單、成交、歷史訂單、劃轉、主子劃轉、閃兌、歷史倉位只能查近約 90 天；請定期執行 archive。", "confirmed"),
    ("稅務帳本", "tax/* 每次查詢最多 30 天，但可回溯到開戶；是 90 天以前唯一的完整來源。", "confirmed"),
    ("成本", "API 不提供現貨平均成本或損益欄位；本頁成本全部由紀錄重建。", "confirmed"),
    ("Launchpool／Onchain", "沒有可讀端點。", "confirmed"),
]


def run(project: Path, offline: bool = False, archive_dir: Path | None = None) -> dict:
    archive_dir = archive_dir or project / "archive"
    snap_dir = project / "snapshots"
    snap_dir.mkdir(exist_ok=True)
    arch = load_archive(archive_dir)
    snap, snap_path = latest_snapshot([archive_dir, snap_dir])
    if not arch.get("tax_spot"):
        raise SystemExit(f"archive/ 內沒有 tax_spot 紀錄，請先執行 `{cli_cmd()} archive`")
    if snap is None:
        raise SystemExit("找不到持倉快照，請先執行 `snapshot` 或 `archive`")
    ov = load_overrides(snap_dir / "analysis_overrides.json")
    prices = Prices(snap_dir / "price_cache.json", offline=offline)
    coins = {r["coin"] for r in arch["tax_spot"]} | set(snap.get("spot_qty") or {}) | set(snap.get("earn") or {})
    first_ms = min(int(r["ts"]) for r in arch["tax_spot"])
    prices.ensure(coins, first_ms)
    tickers = {} if offline else val.fetch_tickers()
    res = compute(arch, snap, prices, tickers, ov)
    state_p = archive_dir / "state.json"
    state = json.loads(state_p.read_text(encoding="utf-8")) if state_p.exists() else {}
    earliest = {}
    for name, recs in arch.items():
        ts = [x for x in (pnl_ts(r) for r in recs) if x]
        earliest[name] = {"rows": len(recs), "earliest": ms_to_oslo(min(ts))[:10] if ts else None,
                          "latest": ms_to_oslo(max(ts))[:10] if ts else None}
    res.update({
        "generated_oslo": now_oslo().isoformat(timespec="seconds"),
        "snapshot_file": str(snap_path.relative_to(project)) if snap_path else None,
        "overrides": {k: v for k, v in ov.items() if not k.startswith("_")},
        "data_quality": {"notes": [{"topic": a, "text": b, "status": s} for a, b, s in DATA_NOTES],
                         "archive_sources": earliest,
                         "archive_errors": {k: v.get("last_errors") for k, v in (state.get("sources") or {}).items() if v.get("last_errors")},
                         "last_archive_run": (state.get("runs") or [{}])[-1].get("at"),
                         "price_misses": prices.misses,
                         "inventory_deficits": res.get("inventory_deficits")},
        "method": "平均成本法（USDT）。獎勵成本 0（另列收到時市值）；雙幣以行權價；網格返還幣以（投入 − 返還 USDT）分攤。",
    })
    # ---- MEXC (optional) and merged overview
    from . import mexc as mexc_mod
    try:
        mx = mexc_mod.analyze(project, archive_dir / "mexc", offline=offline, overrides=ov)
    except Exception as e:  # noqa: BLE001 - never let the optional exchange break Bitget analysis
        mx = {"present": False, "configured": mexc_mod.available(), "reason": f"MEXC 分析失敗：{type(e).__name__}: {e}"[:300]}
    o = res["overview"]
    ex = {"bitget": {"present": True, "total": o["total_auto"], "net_inflow": o["net_inflow"],
                     "pnl": o["total_auto"] - o["net_inflow"],
                     "pnl_pct": (o["total_auto"] - o["net_inflow"]) / o["net_inflow"] * 100 if o["net_inflow"] else None,
                     "snapshot_time": o.get("snapshot_time")},
          "mexc": {k: mx.get(k) for k in ("present", "configured", "reason", "total", "total_api", "earn", "earn_manual",
                                         "net_inflow", "pnl", "pnl_pct", "snapshot_time", "history_complete", "pnl_method")}}
    from . import kraken as kraken_mod
    try:
        kr = kraken_mod.analyze(project, archive_dir / "kraken", offline=offline)
    except Exception as e:  # noqa: BLE001 - optional exchange
        kr = {"present": False, "configured": kraken_mod.available(), "reason": f"Kraken 分析失敗：{type(e).__name__}: {e}"[:300]}
    ex["kraken"] = {k: kr.get(k) for k in ("present", "configured", "reason", "total", "spot", "earn", "futures_at_cost",
                                            "net_inflow", "pnl", "pnl_pct", "snapshot_time", "history_complete", "fiat_deposits")}
    if kr.get("present") and kr.get("pnl") is None:   # no ledger yet: total counts, PnL unknown
        ex["kraken"]["net_inflow"] = None
    from . import cryptocom as cdc_mod
    try:
        cc = cdc_mod.analyze(project, archive_dir / "cryptocom", offline=offline)
    except Exception as e:  # noqa: BLE001 - optional exchange
        cc = {"present": False, "configured": cdc_mod.available(), "reason": f"Crypto.com 分析失敗：{type(e).__name__}: {e}"[:300]}
    if cc.get("present") and cc.get("total") is None:
        cc = {**cc, "present": False, "reason": "Crypto.com 有快照但無法估值（查不到匯率／行情）"}
    ex["cryptocom"] = {k: cc.get(k) for k in ("present", "configured", "reason", "total", "earn", "net_inflow", "pnl", "pnl_pct",
                                              "snapshot_time", "warning", "native_currency", "portfolio_native",
                                              "history_complete", "method", "deposits", "withdrawals", "income_total")}
    if cc.get("method") == "full":
        ex["cryptocom"]["full_history"] = {"label": "完整紀錄（自 " + ((cc.get("full") or {}).get("from") or "")[:10] + "）"}
    elif cc.get("baseline"):
        ex["cryptocom"]["baseline"] = {"at": cc["baseline"].get("at"), "value": cc["baseline"].get("value_usdt"),
                                       "label": "自 " + (cc["baseline"].get("at") or "")[:10] + " 起計算"}
    from . import wallets as wallets_mod
    try:
        wl = wallets_mod.analyze(project, archive_dir / "wallets", offline=offline)
    except Exception as e:  # noqa: BLE001 - optional
        wl = {"present": False, "configured": True, "reason": f"鏈上錢包分析失敗：{type(e).__name__}: {e}"[:300]}
    if wl.get("present") and wl.get("total") is None:
        wl = {**wl, "present": False, "reason": "鏈上錢包有快照但無法估值"}
    ex["wallets"] = {k: wl.get(k) for k in ("present", "configured", "reason", "total", "net_inflow", "pnl", "pnl_pct",
                                             "snapshot_time")}
    if wl.get("baseline"):
        ex["wallets"]["baseline"] = {"at": wl["baseline"].get("at"), "value": wl["baseline"].get("value_usdt"),
                                     "label": "自 " + (wl["baseline"].get("at") or "")[:10] + " 起計算"}
    if wl.get("present"):
        ex["wallets"]["n_wallets"] = len(wl.get("wallets") or [])
        ex["wallets"]["wallet_labels"] = [w.get("label") for w in (wl.get("wallets") or [])]

    # ether.fi Cash analyzed after investment PnL is known (coverage); placeholder for exchange map
    ex["etherfi_cash"] = {"present": False, "configured": False, "group": "spending",
                          "reason": "pending"}

    from . import ibkr as ibkr_mod
    try:
        ib = ibkr_mod.analyze(project, archive_dir / "ibkr", offline=offline)
    except Exception as e:  # noqa: BLE001 - optional
        ib = {"present": False, "configured": False, "reason": f"IBKR 分析失敗：{type(e).__name__}: {e}"[:300]}
    if ib.get("present") and ib.get("total") is None:     # no FX rate: cannot be merged
        ib = {**ib, "present": False, "reason": f"IBKR 有快照但查不到 {ib.get('base_currency')}→USD 匯率，未併入合計"}
    ex["ibkr"] = {k: ib.get(k) for k in ("present", "configured", "reason", "total", "net_inflow", "pnl", "snapshot_time",
                                          "base_currency", "nav_base", "pnl_base", "usdt_per_base", "cash_share")}
    ex["ibkr"]["pnl_pct"] = ib.get("pnl_pct")
    ex["ibkr"]["pnl_method"] = ib.get("pnl_method")
    ex["ibkr"]["baseline"] = ({"at": ib["baseline"].get("at"), "value": ib["baseline"].get("value_base"),
                               "label": "自 " + (ib["baseline"].get("at") or "")[:10] + " 起計算"} if ib.get("baseline") else None)
    if (ib.get("flex") or {}).get("present") and ib.get("pnl_method", "").startswith("flex"):
        fl = ib["flex"]
        ex["ibkr"]["baseline"] = {"at": fl.get("account_start"), "value": fl.get("opening_value"),
                                  "label": ("開戶以來（Flex，至 " if fl.get("complete") else "Flex 期間（自 ") + f"{fl.get('coverage_to') if fl.get('complete') else fl.get('account_start')}）"}
    from . import robinhood as rh_mod
    try:
        rh = rh_mod.analyze(project, archive_dir / "robinhood", offline=offline)
    except Exception as e:  # noqa: BLE001 - optional, manual module
        rh = {"present": False, "configured": False, "reason": f"Robinhood 分析失敗：{type(e).__name__}: {e}"[:300]}
    ex["robinhood"] = {k: rh.get(k) for k in ("present", "configured", "reason", "total", "net_inflow", "pnl", "pnl_pct",
                                               "snapshot_time", "split", "total_eur", "pnl_eur", "net_deposits_eur",
                                               "value_method", "usdt_per_eur", "last_date")}
    for k, v in ex.items():
        if k in ("ibkr", "robinhood"):   # Robinhood: stock tokens (+ cash) → securities; its crypto part is split out below
            v["group"] = "securities"
        elif k == "etherfi_cash":
            v["group"] = "spending"
        else:
            v["group"] = "crypto"
    if mx.get("baseline"):
        ex["mexc"]["baseline"] = {k: mx["baseline"].get(k) for k in ("at", "value", "label", "net_inflow", "capital")}
    tot = sum((v.get("total") or 0) for v in ex.values()
              if v.get("present") and v.get("group") != "spending")  # investment only
    spending_float = sum((v.get("total") or 0) for v in ex.values()
                         if v.get("present") and v.get("group") == "spending")
    for v in ex.values():
        v["share"] = (v["total"] / tot * 100) if v.get("present") and tot else None
    nets = [v.get("net_inflow") for v in ex.values() if v.get("present")]
    res["exchanges"] = ex
    net_sum = sum(n for n in nets if n is not None)
    # cross-exchange transfers (Bitget/MEXC/Kraken): remove both legs from the combined net only when both legs are
    # inside their exchange's net (e.g. MEXC legs before the MEXC baseline are not) — otherwise they already cancel / are excluded.
    xm = {"pairs": [], "adjustment": 0.0}
    try:
        legs = cross_legs(archive_dir, mx, kr, price_at=prices.at)
        xp = match_cross(legs)
        adj = 0.0
        for q in xp:
            if q["both_counted"] and q["sent_usdt"] is not None and q["received_usdt"] is not None:
                adj += q["sent_usdt"] - q["received_usdt"]   # net had −sent (+outflow removed) and +received
        xm = {"pairs": xp, "adjustment": adj, "count": len(xp), "both_counted": sum(1 for q in xp if q["both_counted"]),
              "involving_kraken": sum(1 for q in xp if "kraken" in q["direction"])}
    except Exception as e:  # noqa: BLE001 - informational
        xm = {"pairs": [], "adjustment": 0.0, "error": f"{type(e).__name__}: {e}"[:200]}
    res["combined"] = {"total": tot, "net_inflow": net_sum + xm["adjustment"],
                       "pnl": tot - net_sum - xm["adjustment"], "cross_matched": xm,
                       "exchanges": [k for k, v in ex.items() if v.get("present")]}
    res["combined"]["pnl_pct"] = res["combined"]["pnl"] / res["combined"]["net_inflow"] * 100 if res["combined"]["net_inflow"] else None
    groups = {}
    for g in ("crypto", "securities"):
        # an account with `split` (Robinhood: stock tokens + crypto) contributes each part's value to its group;
        # its net deposits stay with its primary group (rewards/crypto had no deposit of their own).
        m = [v for v in ex.values() if v.get("present") and v.get("group") == g]
        t = 0.0; members = []
        for k, v in ex.items():
            if not v.get("present") or v.get("group") == "spending":
                continue
            part = (v["split"].get(g) or 0.0) if v.get("split") else ((v.get("total") or 0) if v.get("group") == g else 0.0)
            if v.get("group") == g or abs(part) > 0.005:
                t += part; members.append(k)
        n_ok = all(v.get("net_inflow") is not None for v in m)
        net = sum(v.get("net_inflow") or 0 for v in m) + (xm["adjustment"] if g == "crypto" else 0.0)   # cross matches are crypto↔crypto
        groups[g] = {"total": t, "share": t / tot * 100 if tot else None, "exchanges": members,
                     "net_inflow": net if m and n_ok else None, "pnl": (t - net) if m and n_ok else None,
                     "pnl_pct": ((t - net) / net * 100) if m and n_ok and net else None}
    res["combined"]["groups"] = groups
    res["combined"]["method"] = ("Bitget：開戶以來（淨入金）；MEXC：" + mx["baseline"]["label"] + "（基準＋基準後淨入金）。"
                                 "Bitget↔MEXC 互轉在兩邊正負抵銷，合併時視為內部。") if mx.get("baseline") else ""
    if kr.get("present"):
        res["combined"]["method"] += ("Kraken：完整帳本（外部充值含 EUR 銀行入金 − 提領）。" if kr.get("history_complete")
                                      else "Kraken：帳本尚未完整讀取。")
    res["combined"]["method"] += ("交易所之間可配對的互轉（同幣、到帳 ≤ 送出且差距 ≤ 2%、0–12 小時內）視為內部："
                                  "兩邊都計入淨入金時，從合併淨入金移除差額（手續費）"
                                  f"；目前 {xm.get('count', 0)} 筆配對，其中 {xm.get('both_counted', 0)} 筆兩邊都計入，調整 {xm['adjustment']:+,.2f} USDT。")
    res["combined"]["manual_parts"] = ([{"exchange": "mexc", "what": "MEXC 理財（手動填入）", **mx["earn_manual"]}]
                                       if mx.get("present") and mx.get("earn_manual") else [])
    if mx.get("present"):
        try:
            ct = res["combined"]["cross_transfers"] = cross_transfers(archive_dir, mx.get("flows") or [], price_at=prices.at)
            pre = ct.get("before_mexc_window") or {}
            if pre.get("count") and not mx.get("history_complete") and not mx.get("baseline"):
                n = pre["net_to_mexc"]
                mx["pre_window_from_bitget"] = pre
                mx["pnl_if_no_other_flows"] = (mx["pnl"] - n) if mx.get("pnl") is not None else None
                mx["notes"].append(
                    f"⚠ Bitget 紀錄顯示：MEXC 封存起點（{(pre.get('before') or '')[:10]}）之前，Bitget → MEXC {pre['to_mexc']:,.2f}、"
                    f"MEXC → Bitget {pre['from_mexc']:,.2f}（{pre['count']} 筆，自 {(pre.get('first') or '')[:10]}），淨轉入 MEXC {n:,.2f} USDT。"
                    "MEXC API 已查不到這些紀錄，所以 MEXC 的淨入金偏低、盈虧偏高；"
                    f"若封存前沒有其他外部充提，MEXC 盈虧約為 {mx['pnl_if_no_other_flows']:,.2f}。"
                    "合併淨入金同樣偏低（Bitget 把轉出算作出金），合併盈虧最多高估這個金額。請用 MEXC 網頁匯出封存前的充提紀錄確認。")
                res["exchanges"]["mexc"]["pnl_if_no_other_flows"] = mx["pnl_if_no_other_flows"]
                res["combined"]["pnl_overstated_up_to"] = n
        except Exception as e:  # noqa: BLE001 - informational only
            res["combined"]["cross_transfers"] = {"error": f"{type(e).__name__}: {e}"[:200]}
    res["mexc"] = mx
    res["kraken"] = kr
    res["cryptocom"] = cc
    if cc.get("present"):
        res["combined"]["method"] += f"Crypto.com：自 {(cc.get('baseline') or {}).get('at', '')[:10]} 基準起算（基準後入金／出金從交易紀錄辨識）。"
    res["wallets"] = wl
    from . import etherfi_cash as efc_mod
    try:
        efc = efc_mod.analyze(project, archive_dir / "etherfi_cash", offline=offline,
                              investment_pnl=res["combined"].get("pnl"))
    except Exception as e:  # noqa: BLE001
        efc = {"present": False, "configured": False, "group": "spending",
               "reason": f"ether.fi Cash 分析失敗：{type(e).__name__}: {e}"[:300]}
    ex["etherfi_cash"] = {k: efc.get(k) for k in (
        "present", "configured", "reason", "total", "label", "vault", "snapshot_time",
        "topups_usdt", "spend_proxy_usdt", "spend_proxy_method", "coverage_ratio",
        "coverage_pct", "investment_pnl", "n_topups")}
    ex["etherfi_cash"]["group"] = "spending"
    if efc.get("present"):
        ex["etherfi_cash"]["share"] = None  # not part of investment total
        spending_float = efc.get("total") or 0
        res["combined"]["spending_float"] = spending_float
        res["combined"]["etherfi_cash"] = {
            "total": efc.get("total"), "spend_proxy_usdt": efc.get("spend_proxy_usdt"),
            "coverage_ratio": efc.get("coverage_ratio"), "coverage_pct": efc.get("coverage_pct"),
            "spend_proxy_method": efc.get("spend_proxy_method"), "label": efc.get("label"),
        }
        res["combined"]["method"] += (
            f"ether.fi Cash：開銷帳戶 float 不計入投資總額；開銷代理＝儲值 "
            f"{(efc.get('spend_proxy_usdt') or 0):,.2f} USDT（{efc.get('spend_proxy_method')}）；"
            f"涵蓋率＝投資盈虧／開銷代理"
            + (f"＝{(efc.get('coverage_pct')):.0f}%。" if efc.get("coverage_pct") is not None else "。"))
    res["etherfi_cash"] = efc
    if wl.get("present"):
        res["combined"]["method"] += f"鏈上錢包：自 {(wl.get('baseline') or {}).get('at', '')[:10]} 基準起算（公開 RPC／瀏覽器 API，無私鑰；出入金未自動辨識）。"
    res["ibkr"] = ib
    res["robinhood"] = rh
    if rh.get("present"):
        res["combined"]["method"] += (f"Robinhood（無 API，截圖手動）：淨入金＝銀行入金 − 提領；價值"
                                      + ("取自 Portfolio 截圖" if rh.get("value_method") == "portfolio_screenshot" else "為估算（公開美股價 × ECB 匯率）")
                                      + f"，以 1 EUR ≈ {rh['usdt_per_eur']:.4f} USDT 換算；股票代幣＋現金歸證券，加密貨幣部分歸加密。")
    if ib.get("present"):
        res["combined"]["method"] += ((f"IBKR：開戶（{ib['flex'].get('account_start')}）以來的出入金來自 Flex Query（至 {ib['flex'].get('coverage_to')}），"
                                       if ib.get("pnl_method") == "flex" else
                                       f"IBKR：Flex 期間（自 {ib['flex'].get('account_start')}）＋期初淨值，" if ib.get("pnl_method") == "flex-hybrid" else
                                       f"IBKR：自 {(ib['baseline'] or {}).get('at', '')[:10]} 基準起算（TWS 讀不到出入金），")
                                      + f"以 1 {ib['base_currency']} ≈ {ib['usdt_per_base']:.6g} USDT 換算。")
    out = snap_dir / "analysis_latest.json"
    out.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    res["_path"] = str(out)
    return res


def pnl_ts(r: dict) -> int | None:
    for k in ("ts", "cTime", "ctime", "utime", "uTime"):
        try:
            v = int(r.get(k))
            if v > 0:
                return v
        except (TypeError, ValueError):
            continue
    return None


def render(res: dict) -> str:
    o = res["overview"]
    pct = lambda x: "—" if x is None else f"{x:+.1f}%"
    L = [f"=== 分析完成 {res['generated_oslo'][:16]} Oslo（快照 {o['snapshot_time'][:16] if o['snapshot_time'] else '—'}）===",
         f"盈虧（自動估算總額 − 淨入金）：{pct(o['pnl_auto_pct'])}；只算 API 帳戶總額：{pct(o['pnl_visible_pct'])}；對帳殘差 {pct(o['residual'] / o['net_inflow'] * 100 if o['net_inflow'] else None)}",
         f"持倉 {len(res['holdings'])} 項；已實現類別 {len(res['categories'])}；雙幣轉換 {len(res['dual_conversions'])} 筆；現貨機器人關閉 {len(res['spot_bot_episodes'])} 次",
         *([f"Kraken：總額 {res['kraken']['total']:,.2f}；淨入金 {res['kraken']['net_inflow']:,.2f}；盈虧 "
            + ("—" if res['kraken'].get('pnl') is None else f"{res['kraken']['pnl']:+,.2f}")]
           if res.get("kraken", {}).get("present") else [f"Kraken：{res.get('kraken', {}).get('reason', '—')}"]),
         *([f"Crypto.com：總額 {res['cryptocom']['total']:,.2f}（Earn／質押 {res['cryptocom'].get('earn') or 0:,.2f}）；"
            + ("盈虧 —" if res['cryptocom'].get('pnl') is None else
               (f"{'開戶以來' if res['cryptocom'].get('method') == 'full' else '基準'}淨入金 {res['cryptocom']['net_inflow']:,.2f}"
                f"（入 {res['cryptocom'].get('deposits') or 0:,.2f} − 出 {res['cryptocom'].get('withdrawals') or 0:,.2f}）；"
                f"盈虧 {res['cryptocom']['pnl']:+,.2f}；收益 {res['cryptocom'].get('income_total') or 0:,.2f}"))
            + (f"　⚠ {res['cryptocom']['warning']}" if res['cryptocom'].get('warning') else "")]
           if res.get("cryptocom", {}).get("present") else [f"Crypto.com：{res.get('cryptocom', {}).get('reason', '—')}"]),
         *([f"鏈上錢包：總額 {res['wallets']['total']:,.2f}（{len(res['wallets'].get('wallets') or [])} 個）；"
            + ("盈虧 —" if res['wallets'].get('pnl') is None else f"基準淨入金 {res['wallets']['net_inflow']:,.2f}；盈虧 {res['wallets']['pnl']:+,.2f}")]
           if res.get("wallets", {}).get("present") else [f"鏈上錢包：{res.get('wallets', {}).get('reason', '—')}"]),
         *([f"ether.fi Cash（開銷）：float {res['etherfi_cash']['total']:,.2f}；"
            f"開銷代理 {res['etherfi_cash'].get('spend_proxy_usdt') or 0:,.2f}；"
            + ("涵蓋 " + (f"{res['etherfi_cash']['coverage_pct']:.0f}%" if res['etherfi_cash'].get('coverage_pct') is not None else "—"))]
           if res.get("etherfi_cash", {}).get("present") else
           [f"ether.fi Cash：{res.get('etherfi_cash', {}).get('reason', '—')}"]),
         *([f"IBKR：NAV {res['ibkr']['nav_base']:,.2f} {res['ibkr']['base_currency']}（≈ {res['ibkr']['total']:,.2f} USDT）；"
            f"{'淨入金' if str(res['ibkr'].get('pnl_method', '')).startswith('flex') else '基準'} {res['ibkr']['net_inflow_base']:,.2f}；"
            f"盈虧 {res['ibkr']['pnl_base']:+,.2f} {res['ibkr']['base_currency']}（{pct(res['ibkr'].get('pnl_pct'))}）"]
           if res.get("ibkr", {}).get("present") else [f"IBKR：{res.get('ibkr', {}).get('reason', '—')}"]),
         f"合併：總額 {res['combined']['total']:,.2f}；淨入金 {res['combined']['net_inflow']:,.2f}；盈虧 {res['combined']['pnl']:+,.2f}（{pct(res['combined'].get('pnl_pct'))}）",
         *[f"  {lab}：總額 {g['total']:,.2f}（{(g.get('share') or 0):.1f}%）；盈虧 " + ("—" if g.get("pnl") is None else f"{g['pnl']:+,.2f}（{pct(g.get('pnl_pct'))}）")
           for lab, g in (("加密", res["combined"].get("groups", {}).get("crypto")), ("證券", res["combined"].get("groups", {}).get("securities"))) if g and g.get("exchanges")],
         f"已寫入：{res['_path']}（數字請用 `{cli_cmd()} web` 或 AssetsBoard.app 檢視）"]
    return "\n".join(L)
