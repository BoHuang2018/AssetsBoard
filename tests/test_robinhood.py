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
    # a fresh Portfolio screenshot wins over the estimate
    monkeypatch.setattr(rh, "now_oslo", lambda: __import__("datetime").datetime(2026, 2, 16, 12, tzinfo=rh.OSLO))
    rh.save_portfolio(root, 120.0, cash_eur=92.88, crypto_eur=11.0, at="2026-02-16T12:00")
    b = rh.analyze(tmp_path, root, offline=True)
    assert b["value_method"] == "portfolio_screenshot" and b["total_eur"] == 120.0 and abs(b["pnl_eur"] - 20.0) < 1e-9


def test_empty(tmp_path: Path):
    assert rh.analyze(tmp_path, tmp_path / "rh", offline=True)["present"] is False
