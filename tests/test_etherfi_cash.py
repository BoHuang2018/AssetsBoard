"""Unit tests for ether.fi Cash helpers (no network)."""
from __future__ import annotations

import json
from pathlib import Path

from assetsboard import etherfi_cash as efc


def test_default_config_disabled(tmp_path: Path):
    cfg = efc.load_config(tmp_path)
    assert cfg["enabled"] is False
    assert (tmp_path / "config.json").exists()
    snap = efc.build_snapshot(tmp_path)
    assert snap["ok"] is False
    assert snap["configured"] is False


def test_analyze_missing_snapshot(tmp_path: Path, monkeypatch):
    root = tmp_path / "etherfi_cash"
    root.mkdir()
    (root / "config.json").write_text(json.dumps({
        "enabled": True,
        "vault": "0x1ECC0F0C22C1814b14d307F686B2283B2b2180bd",
        "label": "test",
    }), encoding="utf-8")
    # offline analyze with no snapshot
    d = efc.analyze(tmp_path, root, offline=True, investment_pnl=100.0)
    assert d["present"] is False
    assert "快照" in d["reason"] or "snapshot" in d["reason"].lower() or "尚無" in d["reason"]


def test_coverage_ratio_from_snapshot(tmp_path: Path):
    root = tmp_path / "etherfi_cash"
    snap_dir = root / "2026-10" / "snapshots"
    snap_dir.mkdir(parents=True)
    doc = {
        "exchange": "etherfi_cash", "captured_oslo": "2026-10-05T12:00:00+02:00",
        "ok": True, "configured": True, "label": "Cash", "vault": "0x1ECC0F0C22C1814b14d307F686B2283B2b2180bd",
        "total_usdt": 250.0, "tokens": [{"symbol": "LIQUIDUSD", "amount": 250.0, "value_usdt": 250.0}],
        "topups_usdt": 400.0, "spend_proxy_usdt": 400.0, "spend_proxy_method": "arb_usdc_topups",
        "topups": [], "possible_spends": [], "notes": [], "errors": {},
    }
    (snap_dir / "etherfi_cash_snapshot_2026-10-05T1200.json").write_text(
        json.dumps(doc), encoding="utf-8")
    (root / "config.json").write_text(json.dumps({
        "enabled": True, "vault": "0x1ECC0F0C22C1814b14d307F686B2283B2b2180bd",
    }), encoding="utf-8")
    d = efc.analyze(tmp_path, root, offline=True, investment_pnl=200.0)
    assert d["present"] is True
    assert d["total"] == 250.0
    assert d["spend_proxy_usdt"] == 400.0
    assert abs(d["coverage_ratio"] - 0.5) < 1e-9
    assert abs(d["coverage_pct"] - 50.0) < 1e-9


def test_norm_addr():
    assert efc._norm_addr("0x1ECC0F0C22C1814b14d307F686B2283B2b2180bd").startswith("0x")
    try:
        efc._norm_addr("nope")
        assert False
    except efc.CashError:
        pass
