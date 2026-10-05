"""Holdings snapshot document (shared by `snapshot` and `archive`). GET only."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from . import audit as audit_mod
from . import holdings as hold
from . import valuation as val
from .client import Client, ok, stamp


def build(c: Client, t: dt.datetime) -> tuple[dict | None, dict, dict]:
    """Returns (doc or None if spot read failed, spot qty, raw holdings)."""
    h = hold.collect(c, full=True)
    qty = hold.spot_quantities(h["spot_assets"])
    if not ok(h["spot_assets"]):
        return None, qty, h
    v = val.value_spot(qty, val.fetch_tickers())
    doc = {
        "captured_oslo": t.isoformat(timespec="seconds"),
        "mode": hold.mode(h),
        "spot_qty": qty,
        "account_totals_usdt": hold.account_totals(h),
        "earn": hold.earn_assets(h),
        "bots": hold.bot_assets(h),
        "futures_equity": hold.futures_equity(h),
        "positions": hold.positions(h),
        "open_orders": hold.open_orders(h),
        "subaccounts": hold.subaccounts(h),
        "valuation": v,
        "permissions": audit_mod.permissions(h["account_info"]),
        "calls": c.calls,  # responses already IP-redacted by Client.get
    }
    return doc, qty, h


def write(directory: Path, doc: dict, qty: dict, t: dt.datetime) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    st = stamp(t)
    jpath = directory / f"snapshot_{st}.json"
    qpath = directory / f"snapshot_{st}_spot_qty.txt"
    jpath.write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
    audit_mod.write_spot_qty(qpath, qty, t, "Futures/Earn/bots are in the matching snapshot JSON, not here.")
    return jpath, qpath
