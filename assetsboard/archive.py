"""Incremental raw-record archive (GET only).

Layout:  <dir>/state.json
         <dir>/YYYY-MM/<source>.jsonl          one JSON record per line, month = record time (Oslo)
         <dir>/YYYY-MM/snapshots/snapshot_*.json + *_spot_qty.txt
Each source resumes from its last archived window end (minus an overlap); records are
deduplicated by a stable key (billId / tradeId / orderId / … or a hash of the record).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

from . import holdings as hold
from . import snapshot as snapshot_mod
from .client import OSLO, Client, data, ms_to_oslo, now_oslo, ok

DAY = 86_400_000
DEEP_START = dt.date(2026, 3, 1)     # first-run start for sources with long history
RECENT_DAYS = 89                      # sources that refuse startTime older than ~90 days
OVERLAP = 2 * DAY                     # re-read this much before the last window end (late/pending records)
FUTURES_TYPES = hold.FUTURES_TYPES
COMMON_COINS = ("USDT", "USDC", "BTC", "ETH", "BGB")

# name, path, extra params, window ms, lookback ("deep"|"recent"), key fields, time fields, limit, cursor param
SOURCES: list[dict] = [
    dict(name="spot_bills", path="/api/v2/spot/account/bills", window=89 * DAY, lookback="recent",
         key=("billId",), time=("cTime",), limit=500),
    dict(name="sub_main_transfers", path="/api/v2/spot/account/sub-main-trans-record", window=89 * DAY,
         lookback="recent", key=("transferId",), time=("ts",)),
    dict(name="spot_fills", path="/api/v2/spot/trade/fills", window=89 * DAY, lookback="recent",
         key=("tradeId",), time=("cTime",)),
    dict(name="spot_orders", path="/api/v2/spot/trade/history-orders", window=89 * DAY, lookback="recent",
         key=("orderId",), time=("cTime",)),
    dict(name="deposits", path="/api/v2/spot/wallet/deposit-records", window=89 * DAY, lookback="deep",
         key=("orderId", "status"), time=("cTime",)),
    dict(name="withdrawals", path="/api/v2/spot/wallet/withdrawal-records", window=89 * DAY, lookback="deep",
         key=("orderId", "status"), time=("cTime",)),
    dict(name="convert", path="/api/v2/convert/convert-record", window=89 * DAY, lookback="recent",
         key=("id",), time=("ts",)),
    dict(name="bgb_convert", path="/api/v2/convert/bgb-convert-records", window=89 * DAY, lookback="recent",
         key=("orderId",), time=("ctime", "cTime")),
    dict(name="tax_spot", path="/api/v2/tax/spot-record", window=30 * DAY - 1000, lookback="deep",
         key=("id",), time=("ts",), limit=500),
    dict(name="tax_future", path="/api/v2/tax/future-record", window=30 * DAY - 1000, lookback="deep",
         key=("id",), time=("ts",), limit=500),
    dict(name="tax_margin", path="/api/v2/tax/margin-record", window=30 * DAY - 1000, lookback="deep",
         key=("id",), time=("ts",), limit=500),
    dict(name="tax_p2p", path="/api/v2/tax/p2p-record", window=30 * DAY - 1000, lookback="deep",
         key=("id",), time=("ts",), limit=500),
    dict(name="p2p_orders", path="/api/v2/p2p/orderList", window=7 * DAY - 1000, lookback="deep",
         key=("orderId",), time=("ctime", "cTime", "createTime"), cursor="lastMinId", cursor_field="minOrderId"),
    *[dict(name="futures_bills", sub=pt, path="/api/v2/mix/account/bill", params={"productType": pt},
           window=30 * DAY, lookback="deep", key=("billId",), time=("cTime",)) for pt in FUTURES_TYPES],
    *[dict(name="futures_positions", sub=pt, path="/api/v2/mix/position/history-position", params={"productType": pt},
           window=89 * DAY, lookback="recent", key=("positionId",), time=("utime", "ctime")) for pt in FUTURES_TYPES],
    *[dict(name="savings_records", sub=p, path="/api/v2/earn/savings/records", params={"periodType": p},
           window=89 * DAY, lookback="deep", key=None, time=("ts",)) for p in ("flexible", "fixed")],
    dict(name="sharkfin_records", path="/api/v2/earn/sharkfin/records", window=89 * DAY, lookback="deep",
         key=None, time=("ts",)),
]
TRANSFERS = dict(name="transfer_records", path="/api/v2/spot/account/transferRecords", window=89 * DAY,
                 lookback="recent", key=("transferId",), time=("ts",))


def _state_key(src: dict) -> str:
    return src["name"] + (f":{src['sub']}" if src.get("sub") else "")


def record_key(rec: dict, fields) -> str:
    if fields and all(rec.get(f) not in (None, "") for f in fields):
        return "|".join(str(rec[f]) for f in fields)
    return "h:" + hashlib.sha1(json.dumps(rec, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def record_ms(rec: dict, fields) -> int | None:
    for f in fields:
        try:
            v = int(rec.get(f))
            if v > 0:
                return v
        except (TypeError, ValueError):
            continue
    return None


def _batch(r: dict) -> tuple[list, dict]:
    d = data(r)
    if isinstance(d, list):
        return d, {}
    if isinstance(d, dict):
        for v in d.values():
            if isinstance(v, list):
                return v, d
        return [], d
    return [], {}


def fetch(c: Client, src: dict, start: int, end: int, max_pages: int = 300) -> tuple[list, list, int]:
    """All rows in [start, end], window by window, following the cursor. Returns rows, errors, calls."""
    rows, errors, calls = [], [], 0
    limit, cursor_param = src.get("limit", 100), src.get("cursor", "idLessThan")
    s = start
    while s < end:
        e = min(end, s + src["window"])
        cur = None
        for page in range(max_pages):
            p = dict(src.get("params") or {}, startTime=s, endTime=e, limit=limit)
            if cur:
                p[cursor_param] = cur
            r = c.raw_get(src["path"], p)
            calls += 1
            if not ok(r):
                errors.append({"window": [ms_to_oslo(s), ms_to_oslo(e)], "code": r.get("code"), "msg": str(r.get("msg"))[:120]})
                break
            batch, meta = _batch(r)
            rows.extend(batch)
            if len(batch) < limit:
                break
            nxt = meta.get(src.get("cursor_field", "endId")) or batch[-1].get((src["key"] or ("id",))[0])
            if not nxt or nxt == cur:
                break
            cur = nxt
        else:
            errors.append({"window": [ms_to_oslo(s), ms_to_oslo(e)], "code": "MAXPAGES", "msg": f">{max_pages} pages"})
        s = e
    return rows, errors, calls


class Store:
    """JSONL files per source per month with an in-memory key index loaded from disk."""

    def __init__(self, root: Path):
        self.root = root
        self.keys: dict[str, set[str]] = {}
        self.earliest: dict[str, int] = {}
        self.count: dict[str, int] = defaultdict(int)

    def _load(self, name: str, src: dict) -> set[str]:
        if name not in self.keys:
            ks: set[str] = set()
            for f in sorted(self.root.glob(f"[0-9][0-9][0-9][0-9]-[0-9][0-9]/{name}.jsonl")):
                with f.open(encoding="utf-8") as fh:
                    for line in fh:
                        if line.strip():
                            rec = json.loads(line)
                            ks.add(record_key(rec, src["key"]))
                            self._seen_time(name, rec, src)
            self.keys[name] = ks
            self.count[name] = len(ks)
        return self.keys[name]

    def _seen_time(self, name: str, rec: dict, src: dict) -> None:
        ms = record_ms(rec, src["time"])
        if ms and (name not in self.earliest or ms < self.earliest[name]):
            self.earliest[name] = ms

    def add(self, src: dict, rows: list) -> tuple[int, int, int | None]:
        """Append unseen rows. Returns (added, duplicates, max record ms)."""
        name = src["name"]
        ks = self._load(name, src)
        by_month: dict[str, list] = defaultdict(list)
        added = dup = 0
        mx = None
        for rec in rows:
            k = record_key(rec, src["key"])
            ms = record_ms(rec, src["time"])
            if ms and (mx is None or ms > mx):
                mx = ms
            if k in ks:
                dup += 1
                continue
            ks.add(k)
            added += 1
            self._seen_time(name, rec, src)
            month = dt.datetime.fromtimestamp(ms / 1000, OSLO).strftime("%Y-%m") if ms else "undated"
            by_month[month].append(rec)
        for month, recs in by_month.items():
            d = self.root / month
            d.mkdir(parents=True, exist_ok=True)
            with (d / f"{name}.jsonl").open("a", encoding="utf-8") as fh:
                for rec in recs:
                    fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
        self.count[name] += added
        return added, dup, mx


def _load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"version": 1, "sources": {}, "runs": []}


def _start_for(src: dict, st: dict, since: dt.date | None, now_ms: int) -> int:
    floor = now_ms - RECENT_DAYS * DAY if src["lookback"] == "recent" else 0
    if since:
        base = int(dt.datetime.combine(since, dt.time(), OSLO).timestamp() * 1000)
    elif st.get("last_end_ms"):
        base = int(st["last_end_ms"]) - OVERLAP
    else:
        base = int(dt.datetime.combine(DEEP_START, dt.time(), OSLO).timestamp() * 1000)
    return max(base, floor)


def _transfer_coins(c: Client, store: Store) -> list[str]:
    coins = set(COMMON_COINS)
    # coins ever seen: current spot + Earn holdings and every coin in the archived tax ledger
    coins |= {str(r.get("coin")) for r in data(c.raw_get("/api/v2/spot/account/assets", {"assetType": "hold_only"}), []) or []}
    coins |= {str(r.get("coin")) for r in data(c.raw_get("/api/v2/earn/account/assets"), []) or []}
    for f in store.root.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]/tax_spot.jsonl"):
        with f.open(encoding="utf-8") as fh:
            coins |= {json.loads(line).get("coin") for line in fh if line.strip()}
    # names like "Cash+" are not valid coin params (signature mismatch, 40009) and are not transferable
    return sorted(x for x in coins if x and x != "None" and x.isalnum())


def run(c: Client, root: Path, since: dt.date | None = None, progress: bool = True, on_step=None) -> dict:
    """on_step(label, done, total) is called after each source (for UI progress)."""
    t0 = time.time()
    root.mkdir(parents=True, exist_ok=True)
    state_path = root / "state.json"
    state = _load_state(state_path)
    store = Store(root)
    t = now_oslo()
    now_ms = int(t.timestamp() * 1000)
    report: dict = {"captured_oslo": t.isoformat(timespec="seconds"), "root": str(root), "sources": {}, "errors": {}}

    def one(src: dict, label: str) -> None:
        sk = label
        st = state["sources"].setdefault(sk, {})
        start = _start_for(src, st, since, now_ms)
        rows, errs, calls = fetch(c, src, start, now_ms)
        added, dup, mx = store.add(src, rows)
        agg = report["sources"].setdefault(src["name"], {"fetched": 0, "added": 0, "duplicates": 0, "calls": 0, "parts": 0, "from": None})
        agg["fetched"] += len(rows); agg["added"] += added; agg["duplicates"] += dup
        agg["calls"] += calls; agg["parts"] += 1
        agg["from"] = min(agg["from"] or start, start)
        if errs:
            report["errors"][sk] = errs
            st["last_errors"] = errs
        else:
            st["last_end_ms"] = now_ms   # only advance when every window succeeded
            st.pop("last_errors", None)
        if mx:
            st["last_record_ms"] = max(int(st.get("last_record_ms") or 0), mx)
        st["last_run_oslo"] = report["captured_oslo"]
        if progress:
            print(f"  {sk:34} +{added:<6} dup {dup:<6} calls {calls:<4} {time.time() - t0:6.0f}s"
                  + (f"  ERR {errs[0]['code']}" if errs else ""), file=sys.stderr, flush=True)

    steps = {"done": 0, "total": len(SOURCES) + 1}

    def step(label: str) -> None:
        steps["done"] += 1
        if on_step:
            on_step(label, steps["done"], steps["total"])

    for src in SOURCES:
        one(src, _state_key(src)); step(_state_key(src))
    coins = _transfer_coins(c, store)
    steps["total"] += len(coins)
    for k in [k for k in state["sources"] if k.startswith("transfer_records:") and k.split(":", 1)[1] not in coins]:
        state["sources"].pop(k)
    for coin in coins:
        one(dict(TRANSFERS, params={"coin": coin}, sub=coin), f"transfer_records:{coin}"); step(f"transfer_records:{coin}")

    for name in report["sources"]:
        store._load(name, next(s for s in SOURCES + [TRANSFERS] if s["name"] == name))
        report["sources"][name]["total"] = store.count[name]
        e = store.earliest.get(name)
        report["sources"][name]["earliest"] = ms_to_oslo(e)[:10] if e else None
        report["sources"][name]["from"] = ms_to_oslo(report["sources"][name]["from"])[:10]

    # holdings snapshot
    if on_step:
        on_step("snapshot", steps["done"], steps["total"])
    doc, qty, h = snapshot_mod.build(c, t)
    if doc is not None:
        jpath, _ = snapshot_mod.write(root / t.strftime("%Y-%m") / "snapshots", doc, qty, t)
        report["snapshot"] = str(jpath.relative_to(root))
    else:
        report["errors"]["snapshot"] = [{"code": h["spot_assets"].get("code"), "msg": h["spot_assets"].get("msg")}]

    state["runs"] = (state.get("runs") or [])[-49:] + [{
        "at": report["captured_oslo"], "since": since.isoformat() if since else None,
        "added": {k: v["added"] for k, v in report["sources"].items()},
        "errors": sorted(report["errors"]),
    }]
    state_path.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")
    return report


def render(rep: dict) -> str:
    out = [f"=== 封存完成 {rep['captured_oslo'][:16]} Oslo → {rep['root']} ===",
           f"{'來源':20} {'本次查詢起':>10} {'新增':>7} {'重複略過':>8} {'累計':>7} {'最早紀錄':>10} {'請求數':>6}"]
    for name, s in rep["sources"].items():
        out.append(f"{name:20} {s['from']:>10} {s['added']:>7} {s['duplicates']:>8} {s['total']:>7} "
                   f"{s['earliest'] or '—':>10} {s['calls']:>6}")
    if rep.get("snapshot"):
        out.append(f"持倉快照：{rep['snapshot']}")
    if rep["errors"]:
        out.append("\n-- 讀取失敗（該來源下次會從上次成功點重試）--")
        for k, errs in rep["errors"].items():
            out.append(f"  {k}: " + "; ".join(f"{e.get('code')} {e.get('msg')}" for e in errs[:3]))
    return "\n".join(out)
