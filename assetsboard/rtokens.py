"""Bitget rToken (tokenized US stocks: rTSM, rSPCX, …) liquidation progress.

Pure, offline computation on the all-time tax ledger (archive/**/tax_spot.jsonl) + current holdings.

- Quantity history per rToken is rebuilt *backwards* from the current holdings (so it always ends at the
  real balance): qty_t = qty_now − Σ ledger changes after t (amount + fee).
- Default baseline = the moment the rToken basket was largest **at today's prices** (price-neutral peak):
  progress then only moves when you sell/buy, not when stocks move.
  `snapshots/analysis_overrides.json` → "rtoken_baseline_date": "YYYY-MM-DD" pins it (end of that Oslo day).
- % liquidated = 1 − (current rToken value) / (baseline basket valued at today's prices).
- Sell proceeds = stablecoin received on rToken→USDT/USDC trades after the baseline; fees include the
  BGB fee-deduct rows of the same order.
- Dual-investment settlements into rTokens after the baseline are listed as acquisitions (other_in);
  unreturned freezes of rTokens are flagged (other_out) because they leave holdings without a sale.
- Withdrawals after the baseline are shown for context only (the ledger cannot tie a withdrawal to a sale).
"""
from __future__ import annotations

import datetime as dt
from collections import Counter, defaultdict
from typing import Callable

from .client import OSLO, ms_to_oslo

STABLE = {"USDT", "USDC", "USD1", "USDGO", "FDUSD", "DAI"}
DUST_USDT = 0.5
OUT_KINDS = ("onchain_withdrawal", "fiat_withdrawal", "internal_uid_withdrawal")
IN_KINDS = ("onchain_deposit", "fiat_bank_deposit", "internal_uid_deposit")


def is_rtoken(coin: str) -> bool:
    """Bitget tokenized stocks: lowercase 'r' + upper-case ticker (rTSM, rSPCX, rSNXX). Not 'RSR', 'RUNE'."""
    c = str(coin or "")
    return len(c) >= 2 and c[0] == "r" and c[1].isupper() and c[1:].isalnum()


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _day_end_ms(day: str) -> int:
    d = dt.date.fromisoformat(day)
    return int(dt.datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=OSLO).timestamp() * 1000)


def progress(rows: list[dict], current_qty: dict[str, float], cur_price: Callable[[str], float],
             price_at: Callable[[str, int], float], bitget_total: float | None,
             ext_flows: list[dict] | None = None, overrides: dict | None = None, recent: int = 20) -> dict:
    """rows: tax_spot records; current_qty: coin -> qty now (spot + earn + PoolX); ext_flows: classified
    external flows with 'kind', 'ts', 'usd'."""
    ov = overrides or {}
    rrows = sorted((r for r in rows if is_rtoken(r.get("coin"))), key=lambda r: (int(r["ts"]), str(r.get("id"))))
    now_q = {c: q for c, q in current_qty.items() if is_rtoken(c) and q > 1e-12}
    if not rrows and not now_q:
        return {"present": False, "reason": "沒有 rToken 紀錄"}
    p_now = {c: cur_price(c) or 0.0 for c in set(now_q) | {r["coin"] for r in rrows}}

    # --- backward quantity timeline: states[i] = holdings right after event i (states[-1] = now) ---
    deltas = [(int(r["ts"]), r["coin"], _f(r.get("amount")) + _f(r.get("fee"))) for r in rrows]
    q = Counter({c: v for c, v in now_q.items()})
    states: list[tuple[int, dict]] = []
    for ts, c, d in reversed(deltas):
        states.append((ts, dict(q)))
        q[c] -= d
    start_state = {c: v for c, v in q.items() if v > 1e-12}
    states.reverse()
    clean = lambda s: {c: v for c, v in s.items() if v > 1e-12}
    timeline = [(int(rrows[0]["ts"]) - 1 if rrows else 0, start_state)] + [(ts, clean(s)) for ts, s in states]

    def basket_now(s: dict) -> float:
        return sum(v * p_now.get(c, 0.0) for c, v in s.items())

    def basket_then(s: dict, ts: int) -> float:
        return sum(v * (price_at(c, ts) or 0.0) for c, v in s.items())

    pinned = ov.get("rtoken_baseline_date")
    if pinned:
        cut = _day_end_ms(str(pinned)[:10])
        cand = [t for t in timeline if t[0] <= cut] or timeline[:1]
        b_ts, b_state = cand[-1]
        b_ts = max(b_ts, min(cut, timeline[-1][0]))
        method = f"手動指定（{str(pinned)[:10]} 收盤後）"
    else:
        b_ts, b_state = max(timeline, key=lambda t: (basket_now(t[1]), -t[0]))
        method = "以今日價格計算的 rToken 持倉峰值（價格中性：只反映買賣，不受股價漲跌影響）"
    # nominal peak (prices of that day) for context
    nominal = [(ts, basket_then(s, ts)) for ts, s in timeline if s]
    n_ts, n_val = max(nominal, key=lambda x: x[1]) if nominal else (None, 0.0)

    cur_value = sum(v * p_now.get(c, 0.0) for c, v in now_q.items())
    base_now = basket_now(b_state)
    base_then = basket_then(b_state, b_ts)
    liquidated = (1 - cur_value / base_now) * 100 if base_now > 0 else None

    # --- rToken trades after the baseline ---
    by_order: dict = defaultdict(list)
    for r in rows:
        if r.get("spotTaxType") in ("Buy", "Sell", "Transaction fee deduct") and int(r["ts"]) > b_ts - 1:
            by_order[r.get("bizOrderId")].append(r)
    sells, buys = [], []
    for oid, legs in by_order.items():
        s = next((x for x in legs if x["spotTaxType"] == "Sell"), None)
        b = next((x for x in legs if x["spotTaxType"] == "Buy"), None)
        if not s or not b or int(s["ts"]) <= b_ts:
            continue
        fee = sum(-_f(x.get("fee")) * (1.0 if x["coin"] in STABLE else (price_at(x["coin"], int(x["ts"])) or 0.0)) for x in legs
                  if _f(x.get("fee")) < 0 and not is_rtoken(x["coin"]))
        fee += sum(-_f(x.get("fee")) * (price_at(x["coin"], int(x["ts"])) or 0.0) for x in legs if is_rtoken(x["coin"]) and _f(x.get("fee")) < 0)
        if is_rtoken(s["coin"]) and not is_rtoken(b["coin"]):
            qs = -_f(s["amount"]); got = _f(b["amount"]) + _f(b.get("fee"))
            usdt = got if b["coin"] in STABLE else got * (price_at(b["coin"], int(b["ts"])) or 0.0)
            sells.append({"ts": int(s["ts"]), "time": ms_to_oslo(s["ts"]), "coin": s["coin"], "qty": qs,
                          "price": usdt / qs if qs else None, "usdt": usdt, "fee_usdt": fee, "received": b["coin"]})
        elif is_rtoken(b["coin"]) and not is_rtoken(s["coin"]):
            qb = _f(b["amount"]) + _f(b.get("fee")); paid = -(_f(s["amount"]) + _f(s.get("fee")))
            usdt = paid if s["coin"] in STABLE else paid * (price_at(s["coin"], int(s["ts"])) or 0.0)
            buys.append({"ts": int(b["ts"]), "time": ms_to_oslo(b["ts"]), "coin": b["coin"], "qty": qb,
                         "price": usdt / qb if qb else None, "usdt": usdt, "fee_usdt": fee})
    sells.sort(key=lambda x: x["ts"]); buys.sort(key=lambda x: x["ts"])

    # --- non-trade rToken moves after the baseline: dual-investment settlements in (USDT → rToken),
    #     freezes out (rToken locked in a product). A freeze later returned 1:1 is internal and skipped.
    nontrade = [r for r in rrows if r.get("spotTaxType") not in ("Buy", "Sell", "Transaction fee deduct")]
    used: set = set()
    for i, r in enumerate(nontrade):
        a = _f(r.get("amount"))
        if a <= 0 or i in used:
            continue
        for j in range(i - 1, -1, -1):
            o = nontrade[j]
            if j not in used and o["coin"] == r["coin"] and abs(_f(o.get("amount")) + a) < 1e-9 and int(r["ts"]) - int(o["ts"]) < 60 * 86_400_000:
                used |= {i, j}
                break
    other_in, other_out = [], []
    for i, r in enumerate(nontrade):
        if i in used or int(r["ts"]) <= b_ts:
            continue
        a = _f(r.get("amount")) + _f(r.get("fee")); ts = int(r["ts"])
        if abs(a) < 1e-12:
            continue
        T = r.get("spotTaxType") or "（未標記）"
        item = {"time": ms_to_oslo(ts), "coin": r["coin"], "qty": abs(a), "kind": {"Redemption": "雙幣結算買入", "Interest": "利息"}.get(T, T),
                "usdt_then": abs(a) * (price_at(r["coin"], ts) or 0.0)}
        (other_in if a > 0 else other_out).append(item)
    other_in_usdt = sum(x["usdt_then"] for x in other_in if x["kind"] != "利息")
    proceeds = sum(x["usdt"] for x in sells); fees = sum(x["fee_usdt"] for x in sells)
    rebuy = sum(x["usdt"] for x in buys)

    # --- external flows after the baseline (context) ---
    ext = [e for e in (ext_flows or []) if int(e.get("ts") or 0) > b_ts and e.get("usd") is not None]
    wd = -sum(e["usd"] for e in ext if e["kind"] in OUT_KINDS)
    dep = sum(e["usd"] for e in ext if e["kind"] in IN_KINDS)

    tokens = []
    for c in sorted(set(now_q) | set(b_state), key=lambda c: -(now_q.get(c, 0.0) * p_now.get(c, 0.0))):
        qn, qb = now_q.get(c, 0.0), b_state.get(c, 0.0)
        v = qn * p_now.get(c, 0.0)
        if v < 0.005 and qb * p_now.get(c, 0.0) < 0.005:
            continue
        tokens.append({"coin": c, "qty": qn, "price": p_now.get(c) or None, "value": v,
                       "pct_of_rtokens": v / cur_value * 100 if cur_value else None,
                       "pct_of_bitget": v / bitget_total * 100 if bitget_total else None,
                       "baseline_qty": qb, "baseline_value_now": qb * p_now.get(c, 0.0),
                       "sold_pct": (1 - qn / qb) * 100 if qb > 1e-12 else None, "dust": 0 < v < DUST_USDT})
    sold_n = Counter(x["coin"] for x in sells)
    return {
        "present": True,
        "baseline": {"at": ms_to_oslo(b_ts), "method": method, "pinned": bool(pinned),
                     "value_now_prices": base_now, "value_then": base_then, "n_tokens": len(b_state)},
        "nominal_peak": {"at": ms_to_oslo(n_ts) if n_ts else None, "value": n_val},
        "current_value": cur_value,
        "bitget_total": bitget_total,
        "pct_of_bitget": cur_value / bitget_total * 100 if bitget_total else None,
        "liquidated_pct": liquidated,
        "remaining_pct": 100 - liquidated if liquidated is not None else None,
        "n_held": sum(1 for t in tokens if t["value"] >= DUST_USDT),
        "n_dust": sum(1 for t in tokens if t["dust"]),
        "sells": {"count": len(sells), "proceeds_usdt": proceeds, "fees_usdt": fees,
                  "fee_rate_pct": fees / proceeds * 100 if proceeds else None,
                  "first": sells[0]["time"] if sells else None, "last": sells[-1]["time"] if sells else None,
                  "by_coin": dict(sold_n.most_common()),
                  "recent": [{k: v for k, v in x.items() if k != "ts"} for x in reversed(sells[-recent:])]},
        "rebuys": {"count": len(buys), "usdt": rebuy,
                   "recent": [{k: v for k, v in x.items() if k != "ts"} for x in reversed(buys[-10:])]},
        "other_in": {"count": len(other_in), "usdt_then": other_in_usdt, "items": list(reversed(other_in))[:20],
                     "note": "基準後非交易增加（雙幣「低買」結算成 rToken、利息等），以當日收盤價估值"},
        "other_out": {"count": len(other_out), "usdt_then": sum(x["usdt_then"] for x in other_out), "items": list(reversed(other_out))[:20],
                      "note": "基準後非交易減少且尚未 1:1 返還（可能仍凍結在雙幣等產品中）；這部分不在持倉內，會被算成「已出清」"},
        "net_proceeds_usdt": proceeds - rebuy - other_in_usdt,
        "after_baseline_flows": {"withdrawn_usdt": wd, "deposited_usdt": dep,
                                 "withdrawn_vs_net_proceeds_pct": wd / (proceeds - rebuy - other_in_usdt) * 100 if proceeds - rebuy - other_in_usdt > 0 else None,
                                 "note": "提領不一定來自 rToken 賣出（帳本無法對應），僅供參考"},
        "tokens": tokens,
    }
