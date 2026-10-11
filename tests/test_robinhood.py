"""Robinhood manual ledger (offline, fake data)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from assetsboard import robinhood as rh

EXAMPLE = Path(__file__).resolve().parent.parent / "robinhood_ledger.example.csv"


def test_parse_line_and_signs():
    r = rh.parse_line("2026-01-05 buy ACME 0.5 40.00 20.02")
    assert r["type"] == "buy" and r["amount_eur"] == "-20.02" and r["kind"] == "stock" and r["qty"] == "0.5"
    s = rh.parse_line("2026-02-02 market-sell XYZ 100 0.11 10.94")
    assert s["type"] == "sell" and s["kind"] == "crypto" and s["amount_eur"] == "10.94"
    e = rh.parse_line("2026-01-07 token-buy ACME 0.12345~ 40.00 -4.96 note=hidden")
    assert e["qty_estimated"] == "1" and e["note"] == "hidden"
    d = rh.parse_line("2026-01-05 deposit 100 note=bank")
    assert d["type"] == "deposit" and d["kind"] == "cash" and d["amount_eur"] == "100.00"
    assert rh.parse_line("2026-09-25 dividend ACME 0.02")["asset"] == "ACME"
    with pytest.raises(rh.LedgerError):
        rh.parse_line("2026-13-01 buy ACME 1 1 -1")
    with pytest.raises(rh.LedgerError):
        rh.parse_line("2026-01-01 swap ACME 1")


def test_merge_is_idempotent_multiset(tmp_path: Path):
    rows = [rh.parse_line("2026-06-10 reward 10")] * 5 + [rh.parse_line("2026-06-10 deposit 50")]
    added, dup = rh.add_rows(tmp_path, rows)
    assert len(added) == 6 and dup == 0
    added, dup = rh.add_rows(tmp_path, rows + [rh.parse_line("2026-06-11 buy ACME 1 10 -10.01")])
    assert len(added) == 1 and dup == 6
    assert len(rh.load_ledger(tmp_path)) == 7
    # a sixth identical reward is new (multiset, not set)
    added, _ = rh.add_rows(tmp_path, [rh.parse_line("2026-06-10 reward 10")] * 6)
    assert len(added) == 1


def test_derive_example():
    rows = rh.import_csv_text(EXAMPLE.read_text(encoding="utf-8"))
    d = rh.derive(rows, "crypto")
    h = {x["asset"]: x for x in d["holdings"]}
    assert abs(h["ACME"]["qty"] - 0.25) < 1e-12 and abs(h["ACME"]["cost_eur"] - 10.01) < 1e-9
    assert abs(h["BTC"]["qty"] - 0.0002) < 1e-12
    # cash = 100 − 20.02 − 10.05 + 11.98 + 10.94 + 0.03 (reward paid as crypto)
    assert abs(d["cash_eur"] - 92.88) < 1e-9
    assert abs(d["cash_if_rewards_cash_eur"] - 102.88) < 1e-9
    assert d["sold_unknown_assets"] == ["XYZ"] and d["unknown_reward_coins_left"] == 0
    assert abs(d["realized_eur"]["ACME"] - (11.98 - 10.01)) < 1e-9
    assert abs(d["realized_eur"]["XYZ"] - 10.94) < 1e-9
    assert d["net_deposits_eur"] == 100.0
    assert abs(rh.derive(rows, "cash")["cash_eur"] - 102.88) < 1e-9


def test_analyze_offline_with_cached_prices_and_portfolio(tmp_path: Path, monkeypatch):
    root = tmp_path / "archive" / "robinhood"
    rh.add_rows(root, rh.import_csv_text(EXAMPLE.read_text(encoding="utf-8")))
    (root / "prices.json").write_text(json.dumps({"eur_usd": 1.25, "usdt_per_usd": 1.0, "fx_source": "test",
                                                  "usd": {"ACME": 50.0, "BTC": 62500.0}, "src": {}}), encoding="utf-8")
    a = rh.analyze(tmp_path, root, offline=True)
    assert a["present"] and a["value_method"] == "estimate"
    # ACME 0.25 × 40 € + BTC 0.0002 × 50000 € + cash 92.88
    assert abs(a["total_eur"] - (10.0 + 10.0 + 92.88)) < 1e-6
    assert abs(a["pnl_eur"] - (a["total_eur"] - 100.0)) < 1e-9
    assert abs(a["total"] - a["total_eur"] * 1.25) < 1e-6
    assert abs(sum(a["split"].values()) - a["total"]) < 1e-6 and abs(a["split"]["crypto"] - 12.5) < 1e-6
    assert abs(sum(a["pnl_breakdown"].values()) - a["pnl_eur"]) < 1e-6
    # net deposits split: cost of bought crypto → crypto, rest → securities
    btc_cost = sum(h["cost_eur"] for h in a["holdings"] if h["kind"] == "crypto")
    assert abs(a["split_net"]["crypto"] - btc_cost * 1.25) < 1e-6
    assert abs(sum(a["split_net"].values()) - a["net_inflow"]) < 1e-6
    # a fresh Portfolio screenshot wins over the estimate
    monkeypatch.setattr(rh, "now_oslo", lambda: __import__("datetime").datetime(2026, 2, 16, 12, tzinfo=rh.OSLO))
    rh.save_portfolio(root, 120.0, cash_eur=92.88, crypto_eur=11.0, at="2026-02-16T12:00")
    b = rh.analyze(tmp_path, root, offline=True)
    assert b["value_method"] == "portfolio_screenshot" and b["total_eur"] == 120.0 and abs(b["pnl_eur"] - 20.0) < 1e-9


def test_empty(tmp_path: Path):
    assert rh.analyze(tmp_path, tmp_path / "rh", offline=True)["present"] is False


def test_portfolio_implies_reward_crypto(tmp_path: Path, monkeypatch):
    root = tmp_path / "archive" / "robinhood"
    rh.add_rows(root, [rh.parse_line(l) for l in (
        "2026-03-01 deposit 100", "2026-03-01 reward 10", "2026-03-01 reward 10",
        "2026-03-02 buy ACME 1 50 -50.02", "2026-03-03 market-sell XYZ 10 1.2 11.94")])
    (root / "prices.json").write_text(json.dumps({"eur_usd": 1.0, "usdt_per_usd": 1.0, "fx_source": "test",
                                                  "usd": {"ACME": 60.0}, "src": {}}), encoding="utf-8")
    a = rh.analyze(tmp_path, root, offline=True)
    assert a["unknown_reward_coins_left"] == 1 and a["reward_crypto_source"] == "receipt" and a["reward_crypto_eur"] == 10.0
    monkeypatch.setattr(rh, "now_oslo", lambda: __import__("datetime").datetime(2026, 3, 4, 12, tzinfo=rh.OSLO))
    rh.save_portfolio(root, 135.0, cash_eur=61.92, at="2026-03-04T10:58+02:00")
    b = rh.analyze(tmp_path, root, offline=True)
    # implied reward crypto = 135 − 61.92 − 60 (ACME) = 13.08; cost 0
    assert b["value_method"] == "portfolio_screenshot" and b["reward_crypto_source"] == "portfolio_implied"
    assert abs(b["reward_crypto_eur"] - 13.08) < 1e-9 and abs(b["split"]["crypto"] - 13.08) < 1e-9
    assert b["split_net"]["crypto"] == 0.0 and abs(b["split_net"]["securities"] - 100.0) < 1e-9   # reward coins: no deposit
    assert abs(b["pnl_eur"] - 35.0) < 1e-9
    assert abs(sum(b["pnl_breakdown"].values()) - b["pnl_eur"]) < 1e-9 and abs(b["pnl_breakdown"]["other_eur"]) < 1e-9
    # once stale, the estimate keeps the implied crypto value instead of the receipt placeholder
    monkeypatch.setattr(rh, "now_oslo", lambda: __import__("datetime").datetime(2026, 3, 20, 12, tzinfo=rh.OSLO))
    c = rh.analyze(tmp_path, root, offline=True)
    assert c["value_method"] == "estimate" and abs(c["reward_crypto_eur"] - 13.08) < 1e-9


def test_stake_is_not_cash_and_round_trip_trade():
    rows = [rh.parse_line(x) for x in [
        "2026-01-01 deposit 100",
        "2026-01-02 market-buy ETH 0.02 2000 -40.10",
        "2026-01-02 stake ETH 0.02 40.00",
        "2026-01-03 market-buy XYZ 100 0.10 -10.05",
        "2026-01-04 market-sell XYZ 100 0.12 11.95",
        "2026-01-05 token-buy ABCX 1 10 -10.00",
    ]]
    assert rows[2]["type"] == "stake" and rows[2]["kind"] == "crypto" and rows[2]["amount_eur"] == "40.00"
    assert rows[5]["kind"] == "stock"                          # token-* = securities even for leveraged/ETF-like tokens
    d = rh.derive(rows)
    assert abs(d["cash_eur"] - (100 - 40.10 - 10.05 + 11.95 - 10.00)) < 1e-9   # stake moved no cash
    eth = next(h for h in d["holdings"] if h["asset"] == "ETH")
    assert abs(eth["qty"] - 0.02) < 1e-12 and abs(eth["staked_qty"] - 0.02) < 1e-12 and eth["kind"] == "crypto"
    assert abs(d["realized_eur"]["XYZ"] - 1.90) < 1e-9 and not d["unknown_cost_sells"]
    assert {h["asset"] for h in d["holdings"]} == {"ETH", "ABCX"}
    with pytest.raises(rh.LedgerError):
        rh.parse_line("2026-01-02 stake ETH 40.00")


def test_realized_unrealized_rewards_reconcile(tmp_path: Path, monkeypatch):
    root = tmp_path / "archive" / "robinhood"
    rh.add_rows(root, [rh.parse_line(l) for l in (
        "2026-03-01 deposit 100", "2026-03-01 reward 10", "2026-03-01 reward 10",
        "2026-03-02 token-buy ACME 2 25 -50.00", "2026-03-03 token-sell ACME 1 30 30.00",   # avg 25 → realized +5
        "2026-03-03 market-sell XYZ 10 1.2 12.00",                                          # reward coin, cost 0
        "2026-03-04 market-buy ABC 100 0.10 -10.00", "2026-03-05 market-sell ABC 100 0.12 12.00",   # round trip +2
        "2026-03-06 dividend ACME 0.50")])
    (root / "prices.json").write_text(json.dumps({"eur_usd": 1.0, "usdt_per_usd": 1.0, "fx_source": "test",
                                                  "usd": {"ACME": 40.0}, "src": {}}), encoding="utf-8")
    a = rh.analyze(tmp_path, root, offline=True)          # estimate mode
    S = a["pnl_split"]
    assert abs(S["realized_trading_eur"] - 7.0) < 1e-9 and abs(S["dividends_eur"] - 0.5) < 1e-9
    assert abs(S["unrealized_eur"] - (40 - 25)) < 1e-9                       # 1 ACME left at avg cost 25
    assert abs(S["reward_sold_eur"] - 12.0) < 1e-9 and abs(S["reward_held_eur"] - 10.0) < 1e-9   # 1 reward coin left (receipt value)
    assert abs(S["realized_eur"] + S["unrealized_eur"] + S["rewards_eur"] + S["residual_eur"] - a["pnl_eur"]) < 1e-9
    assert abs(S["residual_eur"]) < 1e-9
    abc = next(x for x in S["positions"] if x["asset"] == "ABC")
    assert abc["qty"] == 0.0 and abs(abc["realized_trading_eur"] - 2.0) < 1e-9
    xyz = next(x for x in S["positions"] if x["asset"] == "XYZ")
    assert abs(xyz["reward_realized_eur"] - 12.0) < 1e-9 and abs(xyz["realized_trading_eur"]) < 1e-9
    # screenshot: cash 0.03 higher than the ledger → shows up as residual / cash gap
    monkeypatch.setattr(rh, "now_oslo", lambda: __import__("datetime").datetime(2026, 3, 7, 12, tzinfo=rh.OSLO))
    cash = a["cash_eur"]
    rh.save_portfolio(root, cash + 0.03 + 40 + 10, cash_eur=cash + 0.03, at="2026-03-07T10:00+02:00")
    b = rh.analyze(tmp_path, root, offline=True)
    T = b["pnl_split"]
    assert abs(T["cash_gap_eur"] - 0.03) < 1e-9 and abs(T["residual_eur"] - 0.03) < 1e-9
    assert abs(T["realized_eur"] + T["unrealized_eur"] + T["rewards_eur"] + T["residual_eur"] - b["pnl_eur"]) < 1e-9
