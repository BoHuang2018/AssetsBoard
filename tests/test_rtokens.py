"""rToken liquidation progress (offline, synthetic ledger)."""
from __future__ import annotations

import datetime as dt

from assetsboard import rtokens as rt
from assetsboard.client import OSLO

DAY = 86_400_000


def ms(y, m, d, h=12):
    return int(dt.datetime(y, m, d, h, tzinfo=OSLO).timestamp() * 1000)


def trade(oid, ts, got_coin, got, gave_coin, gave, bgb_fee=0.0):
    rows = [{"id": f"{oid}a", "bizOrderId": oid, "ts": str(ts), "coin": got_coin, "amount": str(got), "fee": "0", "spotTaxType": "Buy"},
            {"id": f"{oid}b", "bizOrderId": oid, "ts": str(ts), "coin": gave_coin, "amount": str(-gave), "fee": "0", "spotTaxType": "Sell"}]
    if bgb_fee:
        rows.append({"id": f"{oid}c", "bizOrderId": oid, "ts": str(ts), "coin": "BGB", "amount": "0", "fee": str(-bgb_fee),
                     "spotTaxType": "Transaction fee deduct"})
    return rows


def ledger():
    r = []
    r += trade("1", ms(2026, 9, 1), "rAAA", 2.0, "USDT", 200.0)      # buy 2 rAAA @100
    r += trade("2", ms(2026, 9, 2), "rBBB", 10.0, "USDT", 100.0)     # buy 10 rBBB @10
    r += trade("3", ms(2026, 9, 10), "USDT", 110.0, "rAAA", 1.0, bgb_fee=0.1)   # sell 1 rAAA @110
    r += trade("4", ms(2026, 9, 12), "USDT", 60.0, "rBBB", 5.0)      # sell 5 rBBB @12
    r += trade("5", ms(2026, 9, 13), "ETH", 0.01, "USDT", 30.0)      # unrelated crypto buy
    return r


PRICE_NOW = {"rAAA": 120.0, "rBBB": 12.0}


def run(rows, cur, total=1000.0, ov=None, ext=None):
    return rt.progress(rows, cur, lambda c: PRICE_NOW.get(c, 0.0), lambda c, ts: {"rAAA": 100.0, "rBBB": 10.0, "BGB": 5.0}.get(c, 1.0),
                       total, ext or [], ov or {})


def test_is_rtoken():
    assert rt.is_rtoken("rTSM") and rt.is_rtoken("rSPCX") and rt.is_rtoken("rSNXX")
    assert not rt.is_rtoken("RSR") and not rt.is_rtoken("RUNE") and not rt.is_rtoken("USDT") and not rt.is_rtoken("r")
    assert not rt.is_rtoken("AAPLON")


def test_peak_baseline_and_progress():
    R = run(ledger(), {"rAAA": 1.0, "rBBB": 5.0, "ETH": 0.01})
    assert R["present"]
    # peak basket = 2 rAAA + 10 rBBB, valued at today's prices = 240 + 120 = 360
    assert abs(R["baseline"]["value_now_prices"] - 360) < 1e-9
    assert R["baseline"]["at"].startswith("2026-09-02")
    assert abs(R["current_value"] - 180) < 1e-9
    assert abs(R["liquidated_pct"] - 50) < 1e-9
    assert abs(R["pct_of_bitget"] - 18) < 1e-9
    s = R["sells"]
    assert s["count"] == 2 and abs(s["proceeds_usdt"] - 170) < 1e-9
    assert abs(s["fees_usdt"] - 0.5) < 1e-9              # 0.1 BGB × 5
    assert s["recent"][0]["coin"] == "rBBB" and abs(s["recent"][0]["price"] - 12) < 1e-9
    tok = {t["coin"]: t for t in R["tokens"]}
    assert abs(tok["rAAA"]["sold_pct"] - 50) < 1e-9 and "ETH" not in tok


def test_pinned_baseline():
    R = run(ledger(), {"rAAA": 1.0, "rBBB": 5.0}, ov={"rtoken_baseline_date": "2026-09-11"})
    assert R["baseline"]["pinned"]
    # after the rAAA sale: 1 rAAA + 10 rBBB = 120 + 120
    assert abs(R["baseline"]["value_now_prices"] - 240) < 1e-9
    assert R["sells"]["count"] == 1 and abs(R["liquidated_pct"] - 25) < 1e-9


def test_dual_settlement_and_withdrawals():
    rows = ledger() + [{"id": "d1", "bizOrderId": "x", "ts": str(ms(2026, 9, 20)), "coin": "rBBB", "amount": "1", "fee": "0",
                        "spotTaxType": "Redemption"},
                       {"id": "f1", "bizOrderId": "y", "ts": str(ms(2026, 9, 21)), "coin": "rAAA", "amount": "-0.5", "fee": "0", "spotTaxType": ""},
                       {"id": "f2", "bizOrderId": "z", "ts": str(ms(2026, 9, 22)), "coin": "rAAA", "amount": "0.5", "fee": "0", "spotTaxType": "Redemption"}]
    ext = [{"kind": "onchain_withdrawal", "ts": ms(2026, 9, 25), "usd": -85.0}, {"kind": "onchain_withdrawal", "ts": ms(2026, 8, 1), "usd": -999.0}]
    R = run(rows, {"rAAA": 1.0, "rBBB": 6.0}, ext=ext)
    assert R["other_in"]["count"] == 1 and abs(R["other_in"]["usdt_then"] - 10) < 1e-9   # 1 rBBB @10; rAAA freeze/return is internal
    assert R["other_out"]["count"] == 0
    assert abs(R["net_proceeds_usdt"] - 160) < 1e-9
    F = R["after_baseline_flows"]
    assert abs(F["withdrawn_usdt"] - 85) < 1e-9 and abs(F["withdrawn_vs_net_proceeds_pct"] - 85 / 160 * 100) < 1e-9


def test_no_rtokens():
    R = rt.progress([], {"BTC": 1.0}, lambda c: 1.0, lambda c, t: 1.0, 100.0)
    assert R["present"] is False
