"""IBKR (optional, Mac only): read-only TWS socket client for a local IB Gateway, snapshot, executions archive, analysis.

Rules:
- standard library only: the classic length-prefixed TWS API text protocol (client versions v157..v178, no protobuf).
- connects only to 127.0.0.1 (IBKR_PORT, default 4001 = live Gateway) with a fixed clientId 47 (IBKR_CLIENT_ID).
- outgoing messages are allowlisted by message id (OUT_ALLOWED): startApi + read requests and their cancels.
  placeOrder (3), cancelOrder (4), reqGlobalCancel (58), exerciseOptions (21), FA / display-group updates … are refused
  before anything is written to the socket. The Gateway's own "Read-Only API" setting is a second, independent guard.
- Gateway offline / logged out / clientId busy ⇒ IbkrUnavailable with a Chinese hint; other exchanges are unaffected.
- TWS cannot return deposit/withdrawal history (that needs a Flex Query), so PnL is measured from a baseline
  (archive/ibkr/baseline.json, set automatically on the first snapshot; `set-ibkr-baseline now|clear`).
- executions: reqExecutions only returns about the last 7 days ⇒ archived incrementally (key execId) every run.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import socket
import struct
import sys
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

from .client import OSLO, cli_cmd, ms_to_oslo, now_oslo, stamp

HOST = "127.0.0.1"
CLIENT_VERSIONS = (157, 178)
UNSET_DOUBLE = 1.7976931348623157e308

# outgoing message ids that may be sent (everything else raises before touching the socket)
OUT_ALLOWED = {71: "startApi", 6: "reqAccountUpdates", 7: "reqExecutions", 17: "reqManagedAccts", 49: "reqCurrentTime",
               61: "reqPositions", 62: "reqAccountSummary", 63: "cancelAccountSummary", 64: "cancelPositions",
               92: "reqPnL", 93: "cancelPnL"}
WRITE_IDS = {3: "placeOrder", 4: "cancelOrder", 21: "exerciseOptions", 58: "reqGlobalCancel", 26: "replaceFA",
             69: "updateDisplayGroup", 8: "reqIds"}
SUMMARY_TAGS = ("AccountType,NetLiquidation,TotalCashValue,SettledCash,AccruedCash,BuyingPower,EquityWithLoanValue,"
                "GrossPositionValue,AvailableFunds,ExcessLiquidity,Cushion,Leverage")
INFO_CODES = set(range(2100, 2200)) | {2158, 2119}     # data-farm status messages, not errors


class IbkrUnavailable(RuntimeError):
    pass


class WriteRefused(PermissionError):
    pass


def port() -> int:
    return int(os.environ.get("IBKR_PORT") or 4001)


def client_id() -> int:
    return int(os.environ.get("IBKR_CLIENT_ID") or 47)


def hint() -> str:
    return (f"IBKR：連不上 IB Gateway（127.0.0.1:{port()}）。請在 Mac 上開啟並登入 IB Gateway"
            "（設定 → API：Enable ActiveX and Socket Clients、Read-Only API），然後再執行 "
            f"{cli_cmd()} archive --exchange ibkr。其他交易所不受影響。")


def available(timeout: float = 0.5) -> bool:
    """Gateway port reachable on localhost (no API handshake)."""
    try:
        with socket.create_connection((HOST, port()), timeout=timeout):
            return True
    except OSError:
        return False


def _f(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if abs(v) >= UNSET_DOUBLE * 0.999 else v


def check_out(fields) -> int:
    mid = int(fields[0])
    if mid in WRITE_IDS:
        raise WriteRefused(f"IBKR {WRITE_IDS[mid]}（{mid}）是寫入訊息，assetsboard 一律拒絕（未送出）")
    if mid not in OUT_ALLOWED:
        raise WriteRefused(f"IBKR 訊息 {mid} 不在唯讀白名單內，拒絕（未送出）")
    return mid


def encode(fields) -> bytes:
    out = []
    for f in fields:
        out.append("1" if f is True else "0" if f is False else "" if f is None else str(f))
    body = ("\0".join(out) + "\0").encode()
    return struct.pack(">I", len(body)) + body


class TwsReader:
    """Minimal synchronous read-only TWS API client."""

    def __init__(self, host: str = HOST, port_: int | None = None, cid: int | None = None, timeout: float = 15.0, verbose: bool = False):
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise ValueError("assetsboard 只連線到本機 IB Gateway（127.0.0.1）")
        self.host, self.port, self.cid, self.timeout, self.verbose = host, port_ or port(), cid if cid is not None else client_id(), timeout, verbose
        self.sock: socket.socket | None = None
        self.buf = b""
        self.server_version = None
        self.conn_time = None
        self.accounts: list[str] = []
        self.next_id = None
        self.errors: list[dict] = []
        self.sent: list[str] = []

    # -- transport
    def _send_raw(self, data: bytes):
        self.sock.sendall(data)

    def send(self, *fields):
        mid = check_out(fields)               # refuse before writing anything
        self._send_raw(encode(fields))
        self.sent.append(OUT_ALLOWED[mid])

    def _read_msg(self, deadline: float):
        while True:
            if len(self.buf) >= 4:
                n = struct.unpack(">I", self.buf[:4])[0]
                if len(self.buf) >= 4 + n:
                    msg, self.buf = self.buf[4:4 + n], self.buf[4 + n:]
                    f = msg.decode("utf-8", errors="replace").split("\0")
                    if f and f[-1] == "":
                        f.pop()
                    return f
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            self.sock.settimeout(min(left, 1.0))
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                raise IbkrUnavailable("IB Gateway 關閉了連線（可能未登入、API 未啟用，或 clientId "
                                      f"{self.cid} 已被其他程式使用）")
            self.buf += chunk

    def connect(self):
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=5)
        except OSError as e:
            raise IbkrUnavailable(hint() + f"（{type(e).__name__}）") from None
        v = f"v{CLIENT_VERSIONS[0]}..{CLIENT_VERSIONS[1]}".encode()
        self._send_raw(b"API\0" + struct.pack(">I", len(v)) + v)
        deadline = time.monotonic() + 10
        f = self._read_msg(deadline)
        if not f or len(f) < 2:
            raise IbkrUnavailable("IB Gateway 沒有回應 API 握手（可能尚未登入）")
        self.server_version, self.conn_time = int(f[0]), f[1]
        if self.server_version < CLIENT_VERSIONS[0]:
            raise IbkrUnavailable(f"IB Gateway 版本太舊（server version {self.server_version}）")
        self.send(71, 2, self.cid, "")
        self.collect(lambda: self.next_id is not None and self.accounts, 10)
        if not self.accounts:
            raise IbkrUnavailable("IB Gateway 已連線但沒有回傳帳戶（可能未登入或仍在啟動）")

    def close(self):
        try:
            if self.sock:
                self.sock.close()
        finally:
            self.sock = None

    # -- dispatch
    def collect(self, done, seconds: float, handler=None):
        deadline = time.monotonic() + seconds
        while not done():
            f = self._read_msg(deadline)
            if f is None:
                return False
            self._dispatch(f, handler)
        return True

    def _dispatch(self, f, handler):
        mid = int(f[0]) if f and f[0].lstrip("-").isdigit() else -1
        if mid == 9:
            self.next_id = int(f[2])
        elif mid == 15:
            self.accounts = [a for a in f[2].split(",") if a]
        elif mid == 4:
            code = int(f[3]) if len(f) > 3 and f[3].lstrip("-").isdigit() else 0
            if code not in INFO_CODES:
                self.errors.append({"reqId": int(f[2]) if f[2].lstrip("-").isdigit() else f[2], "code": code, "msg": f[4] if len(f) > 4 else ""})
                if code in (326,):
                    raise IbkrUnavailable(f"IB Gateway：clientId {self.cid} 已被使用（另一個 assetsboard 可能正在執行），稍後再試")
                if code in (502, 504, 1100):
                    raise IbkrUnavailable(f"IB Gateway 錯誤 {code}：{f[4] if len(f) > 4 else ''}")
        if self.verbose:
            print("  <<", f[0], "|".join(f[1:6])[:100], file=sys.stderr)
        if handler:
            handler(mid, f)


def fetch(verbose: bool = False, executions: bool = True) -> dict:
    """One read-only session: account summary, positions, portfolio/account values, PnL, executions."""
    c = TwsReader(verbose=verbose)
    try:
        c.connect()
        acct = c.accounts[0]
        out = {"account": acct, "accounts": c.accounts, "server_version": c.server_version, "summary": {}, "values": {},
               "positions": [], "portfolio": [], "pnl": None, "executions": {}, "commissions": {}, "complete": {}}
        st = {"sum": False, "pos": False, "acc": False, "pnl": False, "exe": False}

        def h(mid, f):
            if mid == 63 and f[2] == "9001":
                out["summary"][f[4]] = {"value": f[5], "currency": f[6] if len(f) > 6 else ""}
            elif mid == 64 and f[2] == "9001":
                st["sum"] = True
            elif mid == 61:
                out["positions"].append({"account": f[2], "conId": f[3], "symbol": f[4], "secType": f[5], "expiry": f[6],
                                         "strike": f[7], "right": f[8], "multiplier": f[9], "exchange": f[10], "currency": f[11],
                                         "localSymbol": f[12], "tradingClass": f[13], "position": _f(f[14]), "avg_cost": _f(f[15])})
            elif mid == 62:
                st["pos"] = True
            elif mid == 6:
                out["values"].setdefault(f[2], {})[f[4]] = f[3]
            elif mid == 7:
                out["portfolio"].append({"conId": f[2], "symbol": f[3], "secType": f[4], "expiry": f[5], "strike": f[6], "right": f[7],
                                         "multiplier": f[8], "primaryExchange": f[9], "currency": f[10], "localSymbol": f[11],
                                         "tradingClass": f[12], "position": _f(f[13]), "market_price": _f(f[14]), "market_value": _f(f[15]),
                                         "avg_cost": _f(f[16]), "unrealized": _f(f[17]), "realized": _f(f[18]), "account": f[19]})
            elif mid == 54:
                st["acc"] = True
            elif mid == 94 and f[1] == "9002":
                out["pnl"] = {"daily": _f(f[2]), "unrealized": _f(f[3]), "realized": _f(f[4]) if len(f) > 4 else None}
                st["pnl"] = True
            elif mid == 11 and f[1] == "9003":
                e = {"orderId": f[2], "conId": f[3], "symbol": f[4], "secType": f[5], "expiry": f[6], "strike": f[7], "right": f[8],
                     "multiplier": f[9], "exchange": f[10], "currency": f[11], "localSymbol": f[12], "tradingClass": f[13],
                     "execId": f[14], "time": f[15], "account": f[16], "exec_exchange": f[17], "side": f[18], "shares": _f(f[19]),
                     "price": _f(f[20]), "permId": f[21], "clientId": f[22], "liquidation": f[23], "cumQty": _f(f[24]),
                     "avgPrice": _f(f[25]), "orderRef": f[26] if len(f) > 26 else ""}
                out["executions"][e["execId"]] = e
            elif mid == 55 and f[2] == "9003":
                st["exe"] = True
            elif mid == 59:
                out["commissions"][f[2]] = {"commission": _f(f[3]), "currency": f[4], "realized": _f(f[5])}

        c.send(62, 1, 9001, "All", SUMMARY_TAGS + ",$LEDGER:ALL")
        out["complete"]["summary"] = c.collect(lambda: st["sum"], 15, h)
        c.send(63, 1, 9001)
        c.send(61, 1)
        out["complete"]["positions"] = c.collect(lambda: st["pos"], 15, h)
        c.send(64, 1)
        c.send(6, 2, True, acct)
        out["complete"]["portfolio"] = c.collect(lambda: st["acc"], 20, h)
        c.send(6, 2, False, acct)
        c.send(92, 9002, acct, "")
        out["complete"]["pnl"] = c.collect(lambda: st["pnl"], 6, h)
        c.send(93, 9002)
        if executions:
            c.send(7, 3, 9003, 0, acct, "", "", "", "", "")
            out["complete"]["executions"] = c.collect(lambda: st["exe"], 15, h)
            c.collect(lambda: False, 1.5, h)    # commission reports may follow execDetailsEnd
        out["errors"] = c.errors
        out["sent"] = c.sent
        return out
    finally:
        c.close()


# ---------------------------------------------------------------- snapshot
def build_snapshot(raw: dict, t: dt.datetime) -> dict:
    s, v = raw["summary"], dict(raw["values"])
    for k, cur_vals in raw["values"].items():     # newer Gateways prefix the per-currency ledger keys ("$LEDGER-CashBalance")
        if k.startswith("$LEDGER-"):
            v[k[len("$LEDGER-"):]] = {**v.get(k[len("$LEDGER-"):], {}), **cur_vals}
    base = (s.get("NetLiquidation") or {}).get("currency") or (v.get("NetLiquidation") or {}) and next(iter(v["NetLiquidation"]), None)
    rates = {cur: _f(x) for cur, x in (v.get("ExchangeRate") or {}).items() if cur != "BASE"}
    rates.setdefault(base, 1.0)
    cash = {cur: _f(x) for cur, x in (v.get("CashBalance") or {}).items() if cur != "BASE" and _f(x)}
    positions = []
    by_con = {p["conId"]: p for p in raw["positions"]}
    for p in raw["portfolio"]:
        r = rates.get(p["currency"])
        positions.append({**{k: p[k] for k in ("conId", "symbol", "secType", "currency", "localSymbol", "primaryExchange", "expiry", "strike", "right", "multiplier")},
                          "position": p["position"], "avg_cost": p["avg_cost"], "market_price": p["market_price"],
                          "market_value": p["market_value"], "unrealized": p["unrealized"], "realized": p["realized"],
                          "fx_to_base": r, "market_value_base": p["market_value"] * r if r and p["market_value"] is not None else None,
                          "unrealized_base": p["unrealized"] * r if r and p["unrealized"] is not None else None})
    for con, p in by_con.items():        # positions without a portfolio line (rare): keep qty / avg cost
        if not any(x["conId"] == con for x in positions) and p["position"]:
            positions.append({**{k: p[k] for k in ("conId", "symbol", "secType", "currency", "localSymbol")}, "position": p["position"],
                              "avg_cost": p["avg_cost"], "market_value": None, "market_value_base": None, "unrealized": None})
    num = lambda tag: _f((s.get(tag) or {}).get("value"))
    return {"exchange": "ibkr", "captured_oslo": t.isoformat(timespec="seconds"), "account": raw["account"],
            "server_version": raw["server_version"], "base_currency": base,
            "net_liquidation": num("NetLiquidation"), "total_cash": num("TotalCashValue"), "gross_position_value": num("GrossPositionValue"),
            "accrued_cash": num("AccruedCash"), "buying_power": num("BuyingPower"), "account_type": (s.get("AccountType") or {}).get("value"),
            "summary": s, "cash_by_currency": cash, "fx_to_base": rates, "positions": positions, "pnl": raw["pnl"],
            "realized_pnl_base": _f((v.get("RealizedPnL") or {}).get("BASE")), "unrealized_pnl_base": _f((v.get("UnrealizedPnL") or {}).get("BASE")),
            "net_dividend_base": _f((v.get("NetDividend") or {}).get("BASE")),
            "complete": raw["complete"], "errors": raw.get("errors"), "executions_seen": len(raw["executions"])}


def write_snapshot(directory: Path, doc: dict, t: dt.datetime) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / f"ibkr_snapshot_{stamp(t)}.json"
    p.write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
    return p


def latest_snapshot(roots: list[Path]):
    files = [p for r in roots if r.exists() for p in list(r.glob("ibkr_snapshot_*.json")) + list(r.glob("**/ibkr_snapshot_*.json"))
             if not p.name.startswith("._")]
    if not files:
        return None, None
    p = max(files, key=lambda p: p.name)
    return json.loads(p.read_text(encoding="utf-8")), p


# ---------------------------------------------------------------- baseline
def baseline_path(root: Path) -> Path:
    return root / "baseline.json"


def load_baseline(root: Path) -> dict | None:
    p = baseline_path(root)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def save_baseline(root: Path, snap: dict | None, usdt_per_base: float | None = None, clear: bool = False) -> dict | None:
    p = baseline_path(root)
    if clear:
        p.unlink(missing_ok=True)
        return None
    old = load_baseline(root) or {}
    b = {"at": snap["captured_oslo"], "value_base": snap["net_liquidation"], "base_currency": snap["base_currency"],
         "usdt_per_base": usdt_per_base, "flows": old.get("flows") or [],
         "note": "TWS API 讀不到出入金；基準後如有入金／出金，請加到 flows：[{\"at\": ISO, \"amount_base\": 正=入金}]，或改用 Flex Query。"}
    root.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(b, indent=1, ensure_ascii=False), encoding="utf-8")
    return b


# ---------------------------------------------------------------- FX
def _get_json(url: str, timeout: int = 15):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (assetsboard)"}), timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:  # noqa: BLE001
        return None


def usdt_per_usd() -> float:
    from . import kraken
    r = kraken.result(kraken.public("Ticker", {"pair": "USDTZUSD"})) or {}
    try:
        p = float(next(iter(r.values()))["c"][0])     # USD per USDT
        return 1 / p if p > 0 else 1.0
    except (StopIteration, KeyError, TypeError, ValueError, IndexError):
        return 1.0


def usd_per(cur: str, snap: dict | None = None, offline: bool = False) -> tuple[float | None, str]:
    """Value of 1 unit of `cur` in USD: IBKR's own ExchangeRate when the account holds USD, else ECB (frankfurter), else Yahoo."""
    if cur == "USD":
        return 1.0, "—"
    rates = (snap or {}).get("fx_to_base") or {}
    if snap and snap.get("base_currency") == cur and rates.get("USD"):
        return 1 / rates["USD"], "IBKR ExchangeRate"
    if offline:
        return None, "離線"
    j = _get_json(f"https://api.frankfurter.app/latest?from={cur}&to=USD")
    if j and (j.get("rates") or {}).get("USD"):
        return float(j["rates"]["USD"]), f"ECB（frankfurter {j.get('date')}）"
    j = _get_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{cur}USD=X?interval=1d&range=5d")
    try:
        return float(j["chart"]["result"][0]["meta"]["regularMarketPrice"]), "Yahoo"
    except (TypeError, KeyError, IndexError, ValueError):
        return None, "無匯率"


# ---------------------------------------------------------------- archive (executions, incremental)
EXEC_SRC = dict(name="ibkr_executions", key=("execId",), time=("_ms",))


def exec_ms(t: str) -> int:
    """'20261002  15:30:01 US/Eastern' / '20261002-13:30:01' (UTC) → epoch ms."""
    from zoneinfo import ZoneInfo
    s = " ".join(str(t).replace("-", " ").split())
    parts = s.split(" ")
    alias = {"US/Eastern": "America/New_York", "US/Central": "America/Chicago", "US/Pacific": "America/Los_Angeles",
             "US/Mountain": "America/Denver", "EST": "America/New_York", "EST5EDT": "America/New_York", "MET": "Europe/Paris",
             "CET": "Europe/Paris", "GB-Eire": "Europe/London", "Japan": "Asia/Tokyo", "Hongkong": "Asia/Hong_Kong"}
    tz = dt.timezone.utc
    if len(parts) > 2:
        try:
            tz = ZoneInfo(alias.get(parts[2], parts[2]))
        except Exception:  # noqa: BLE001 - unknown zone name: assume UTC
            tz = dt.timezone.utc
    try:
        d = dt.datetime.strptime(" ".join(parts[:2]), "%Y%m%d %H:%M:%S").replace(tzinfo=tz)
    except ValueError:
        return 0
    return int(d.timestamp() * 1000)


def archive_run(root: Path, verbose: bool = False, progress: bool = True, on_step=None) -> dict:
    from .archive import Store, _load_state
    t = now_oslo()
    rep = {"captured_oslo": t.isoformat(timespec="seconds"), "root": str(root), "sources": {}, "errors": {}, "exchange": "ibkr"}
    raw = fetch(verbose=verbose)              # raises IbkrUnavailable
    if on_step:
        on_step("gateway", 1, 2)
    root.mkdir(parents=True, exist_ok=True)
    state_path = root / "state.json"
    state = _load_state(state_path)
    store = Store(root)
    rows = []
    for x, e in raw["executions"].items():
        cr = raw["commissions"].get(x) or {}
        rows.append({**e, "_ms": exec_ms(e["time"]), "commission": cr.get("commission"), "commission_currency": cr.get("currency"),
                     "realized": cr.get("realized")})
    added, dup, mx = store.add(EXEC_SRC, rows)
    store._load(EXEC_SRC["name"], EXEC_SRC)
    e = store.earliest.get(EXEC_SRC["name"])
    rep["sources"]["ibkr_executions"] = {"fetched": len(rows), "added": added, "duplicates": dup, "calls": 1, "from": "近 7 天",
                                         "total": store.count[EXEC_SRC["name"]], "earliest": ms_to_oslo(e)[:10] if e else None}
    snap = build_snapshot(raw, t)
    if snap["net_liquidation"] is None:
        rep["errors"]["snapshot"] = [{"code": "NAV", "msg": "沒有收到 NetLiquidation"}]
    else:
        p = write_snapshot(root / t.strftime("%Y-%m") / "snapshots", snap, t)
        rep["snapshot"] = str(p.relative_to(root))
        if load_baseline(root) is None:
            u, _src = usd_per(snap["base_currency"], snap)
            save_baseline(root, snap, u * usdt_per_usd() if u else None)
            rep["baseline_created"] = snap["captured_oslo"]
    bad = [x for x in raw.get("errors") or [] if x["code"] not in INFO_CODES]
    if bad:
        rep["errors"]["gateway"] = [{"code": x["code"], "msg": x["msg"]} for x in bad[:5]]
    incomplete = [k for k, ok in raw["complete"].items() if not ok]
    if incomplete:
        rep["errors"]["incomplete"] = [{"code": "TIMEOUT", "msg": "未完整收到：" + ", ".join(incomplete)}]
    state["runs"] = (state.get("runs") or [])[-49:] + [{"at": rep["captured_oslo"], "added": {"ibkr_executions": added}, "errors": sorted(rep["errors"])}]
    state_path.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")
    if progress:
        print(f"  ibkr_executions  +{added} dup {dup}；NAV {snap['net_liquidation']} {snap['base_currency']}", file=sys.stderr)
    if on_step:
        on_step("snapshot", 2, 2)
    return rep


def load_executions(root: Path) -> list[dict]:
    out = {}
    for f in sorted(root.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]/ibkr_executions.jsonl")):
        if f.name.startswith("._"):
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                out[r["execId"]] = r
    return sorted(out.values(), key=lambda r: r.get("_ms") or 0)


# ---------------------------------------------------------------- analysis
def analyze(project: Path, archive_root: Path | None = None, offline: bool = False) -> dict:
    root = archive_root or project / "archive" / "ibkr"
    snap, sp = latest_snapshot([root, project / "snapshots"])
    if snap is None:
        return {"present": False, "configured": available(),
                "reason": "尚無 IBKR 快照：在 Mac 開啟並登入 IB Gateway 後執行 " + f"{cli_cmd()} archive --exchange ibkr"}
    base = snap["base_currency"]
    usd, fx_src = usd_per(base, snap, offline)
    u2t = 1.0 if offline else usdt_per_usd()
    k = (usd * u2t) if usd else None                       # USDT per 1 base-currency unit
    nav = snap["net_liquidation"] or 0.0
    pos = [p for p in snap["positions"] if p.get("position")]
    for p in pos:
        p["share"] = (p["market_value_base"] / nav * 100) if nav and p.get("market_value_base") is not None else None
    pos.sort(key=lambda p: -(p.get("market_value_base") or 0))
    cash = snap.get("total_cash")
    bl = load_baseline(root)
    from . import ibkr_flex
    fx_ = ibkr_flex.analyze(project, root, base, offline) if ibkr_flex.present(root) else {"present": False}
    method, note = "baseline", None
    if fx_.get("present") and fx_.get("net_inflow") is not None and fx_.get("base_currency") == base:
        # Flex has the real deposit history; deposits after the Flex period are only known if added to baseline flows
        cut = fx_["coverage_to"] or ""
        later = [x for x in (bl or {}).get("flows") or [] if (x.get("at") or "")[:10].replace("-", "") > cut]
        flows = sum(float(x.get("amount_base") or 0) for x in later)
        net_base = fx_["opening_value"] + fx_["net_inflow"] + flows
        method = "flex" if fx_["complete"] else "flex-hybrid"
        net_usdt = net_base * (k or 0) if k else None
        note = (f"盈虧以 IBKR Flex Query 的完整出入金計算（帳戶自 {fx_['account_start']} 開立，涵蓋至 {cut}）。{cut} 之後的入金要重新匯入 Flex 才會計入。"
                if fx_["complete"] else
                f"Flex 資料從 {fx_['account_start']} 起（期初淨值 {fx_['opening_value']:.2f} {base} 當作投入），帳戶比 Flex 期間更早開立：盈虧只涵蓋這段期間（混合法）。")
    else:
        flows = sum(float(x.get("amount_base") or 0) for x in (bl or {}).get("flows") or [])
        net_base = (bl["value_base"] + flows) if bl and bl.get("value_base") is not None else None
        net_usdt = None
        if bl and bl.get("value_base") is not None:
            net_usdt = bl["value_base"] * (bl.get("usdt_per_base") or k or 0) + flows * (k or 0)
    pnl_base = nav - net_base if net_base is not None else None
    execs = load_executions(root) if root.exists() else []
    comm = defaultdict(float)
    for e in execs:
        if e.get("commission") is not None:
            comm[e.get("commission_currency") or "?"] += e["commission"]
    total_usdt = nav * k if k else None
    return {
        "present": True, "configured": available(), "snapshot_time": snap["captured_oslo"], "account": snap["account"],
        "snapshot_file": str(sp.relative_to(project)) if sp else None, "base_currency": base,
        "nav_base": nav, "cash_base": cash, "cash_share": (cash / nav * 100) if nav and cash is not None else None,
        "gross_position_value_base": snap.get("gross_position_value"), "accrued_cash_base": snap.get("accrued_cash"),
        "cash_by_currency": snap.get("cash_by_currency"), "fx_to_base": snap.get("fx_to_base"),
        "usdt_per_base": k, "fx_source": fx_src, "total": total_usdt,
        "positions": pos, "pnl_today": snap.get("pnl"), "realized_pnl_base": snap.get("realized_pnl_base"),
        "unrealized_pnl_base": snap.get("unrealized_pnl_base"),
        "baseline": bl, "pnl_method": method, "flex": fx_, "net_inflow_base": net_base, "pnl_base": pnl_base,
        "pnl_pct": (pnl_base / net_base * 100) if pnl_base is not None and net_base else None,
        "net_inflow": net_usdt, "pnl": (total_usdt - net_usdt) if total_usdt is not None and net_usdt is not None else None,
        "executions": {"count": len(execs), "first": ms_to_oslo(execs[0]["_ms"])[:10] if execs else None,
                       "commission_by_currency": dict(comm), "recent": execs[-30:]},
        "complete": snap.get("complete"), "errors": snap.get("errors"),
        "notes": [
            f"資料來自本機 IB Gateway（唯讀 API，帳戶 {snap['account']}），在 Mac 讀取後匯出給 box。",
            f"本頁以基礎貨幣 {base} 顯示；合併總覽以 1 {base} ≈ {k:.6g} USDT 換算（匯率：{fx_src}）。" if k else f"⚠ 查不到 {base}→USD 匯率，無法併入合計。",
            note or ("TWS API 讀不到入金／出金紀錄：盈虧以基準（" + ((bl or {}).get("at") or "—")[:16].replace("T", " ") + "）起算，"
                     f"並假設之後沒有出入金（有的話請在 archive/ibkr/baseline.json 的 flows 補上，或匯入 Flex Query：{cli_cmd()} ibkr-import <xml>）。"),
            "成交紀錄 reqExecutions 只提供約最近 7 天，每次執行都會增量封存。",
        ],
    }


def perms_report(verbose: bool = False) -> dict:
    """Run the read calls once and report which answered. Write messages are refused locally (never sent)."""
    refused = []
    for mid in (3, 4, 58, 21):
        try:
            check_out((mid,))
        except WriteRefused:
            refused.append(WRITE_IDS[mid])
    try:
        raw = fetch(verbose=verbose)
    except IbkrUnavailable as e:
        return {"ok": False, "error": str(e), "write_refused_locally": refused}
    return {"ok": True, "account": raw["account"], "accounts": raw["accounts"], "server_version": raw["server_version"],
            "complete": raw["complete"], "counts": {"summary_tags": len(raw["summary"]), "positions": len(raw["positions"]),
                                                    "portfolio": len(raw["portfolio"]), "executions": len(raw["executions"])},
            "errors": raw["errors"], "sent": raw["sent"], "write_refused_locally": refused}
