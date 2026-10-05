"""MEXC (optional second exchange): signed GET-only client, holdings snapshot, incremental archive, analysis.

Rules (same as Bitget):
- standard library only; every request is a GET; the paths are whitelisted below, so order / cancel /
  withdraw / transfer endpoints cannot be called even by mistake;
- credentials MEXC_API_KEY / MEXC_API_SECRET via env → macOS Keychain (service "assetsboard") → box-secrets.json;
- if they are missing, everything MEXC-related is skipped (``available()`` is False) and nothing fails.

Spot API v3 (https://api.mexc.com): HMAC-SHA256(secret, totalParams) as lowercase hex in ``signature``,
key in header ``X-MEXC-APIKEY``. Contract API (https://contract.mexc.com): headers ApiKey / Request-Time /
Signature, signature = HMAC-SHA256(secret, accessKey + reqTime + sorted-param-string).
History limits (observed on the live API 2026-10): myTrades ≈ 1 month; deposit/withdraw/internal transfer ≤ 90 days in
≤ 7-day windows; universal transfer ≤ ~90 days (docs claim 6 months); dust log limit ≤ 20 per page.
"""
from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import hmac
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

from .client import (MEXC_SECRET_NAMES, OSLO, cli_cmd, credential_sources, credentials_available, load_secret,
                     ms_to_oslo, now_oslo, stamp)

BASE = "https://api.mexc.com"
CONTRACT = "https://contract.mexc.com"
DAY = 86_400_000
STABLE = {"USDT", "USDC", "USD1", "FDUSD", "TUSD", "DAI", "USDE", "PYUSD"}
QUOTES = ("USDT", "USDC", "BTC", "ETH")

# Whitelisted GET paths — anything else raises before a request is made.
SPOT_SIGNED = {
    "/api/v3/account", "/api/v3/myTrades", "/api/v3/capital/deposit/hisrec", "/api/v3/capital/withdraw/history",
    "/api/v3/capital/transfer", "/api/v3/capital/transfer/internal", "/api/v3/capital/convert",
    "/api/v3/mxDeduct/enable", "/api/v3/kyc/status",
}
SPOT_PUBLIC = {"/api/v3/time", "/api/v3/ping", "/api/v3/ticker/price", "/api/v3/exchangeInfo", "/api/v3/klines"}
CONTRACT_SIGNED = {
    "/api/v1/private/account/assets", "/api/v1/private/position/open_positions",
    "/api/v1/private/position/list/history_positions", "/api/v1/private/account/transfer_record",
    "/api/v1/private/position/funding_records",
}
DEPOSIT_OK = {5, 12}      # SUCCESS, COMPLETED
WITHDRAW_OK = {7}         # SUCCESS


# ---------------------------------------------------------------- signing (pure, unit-tested)
def spot_query(params: dict | None) -> str:
    """Query string in insertion order (the string that is signed and sent)."""
    items = [(k, str(v)) for k, v in (params or {}).items() if v is not None]
    return urllib.parse.urlencode(items, quote_via=urllib.parse.quote)


def spot_signature(secret: str, total_params: str) -> str:
    return hmac.new(secret.encode(), total_params.encode(), hashlib.sha256).hexdigest()


def contract_param_string(params: dict | None) -> str:
    items = sorted((k, str(v)) for k, v in (params or {}).items() if v is not None)
    return "&".join(f"{k}={urllib.parse.quote(v, safe='')}" for k, v in items)


def contract_signature(secret: str, access_key: str, req_time: str, param_string: str) -> str:
    return hmac.new(secret.encode(), f"{access_key}{req_time}{param_string}".encode(), hashlib.sha256).hexdigest()


def available() -> bool:
    return credentials_available(MEXC_SECRET_NAMES)


def sources() -> dict:
    return credential_sources(MEXC_SECRET_NAMES)


def no_creds_msg() -> str:
    return f"MEXC 未設定憑證（MEXC_API_KEY / MEXC_API_SECRET），已略過。設定：{cli_cmd()} setup-keychain --exchange mexc"


# ---------------------------------------------------------------- HTTP
def _http_get(url: str, headers: dict, timeout: int = 30):
    req = urllib.request.Request(url, headers=dict(headers, **{"Accept-Encoding": "gzip"}), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip" or raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read()
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        try:
            body = json.loads(raw.decode("utf-8", errors="replace"))
            if isinstance(body, dict):
                body.setdefault("http_status", e.code)
            return body
        except ValueError:
            return {"code": f"HTTP{e.code}", "msg": raw[:300].decode("utf-8", errors="replace")}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        return {"code": "NETWORK", "msg": f"{type(e).__name__}: {e}"[:300]}


def is_error(r) -> bool:
    if not isinstance(r, dict):
        return False
    if r.get("success") is False:
        return True
    if "code" in r and str(r.get("code")) not in ("0", "200") and ("msg" in r or "message" in r):
        return True
    return False


def err_text(r) -> str:
    return f"{r.get('code')} {r.get('msg') or r.get('message') or ''}".strip()[:160] if isinstance(r, dict) else "?"


def _rate_limited(r) -> bool:
    return isinstance(r, dict) and (str(r.get("code")) in ("429", "510", "HTTP429") or r.get("http_status") == 429)


def public(path: str, params: dict | None = None):
    if path not in SPOT_PUBLIC:
        raise ValueError(f"MEXC public path not whitelisted: {path}")
    q = spot_query(params)
    return _http_get(BASE + path + (f"?{q}" if q else ""), {"Content-Type": "application/json"})


class MexcClient:
    """Signed GET-only MEXC client. Records each call (without secrets) in self.calls."""

    def __init__(self, sleep: float = 0.2, verbose: bool = False):
        self.sleep, self.verbose = sleep, verbose
        self.calls: dict[str, dict] = {}
        self._creds: tuple[str, str] | None = None
        self._offset: int | None = None

    def _credentials(self) -> tuple[str, str]:
        if self._creds is None:
            self._creds = tuple(load_secret(n) for n in MEXC_SECRET_NAMES)  # type: ignore[assignment]
        return self._creds  # type: ignore[return-value]

    def _now(self) -> int:
        if self._offset is None:
            r = public("/api/v3/time")
            st = r.get("serverTime") if isinstance(r, dict) else None
            self._offset = int(st) - int(time.time() * 1000) if st else 0
        return int(time.time() * 1000) + self._offset

    def _record(self, name, path, params, r):
        if name:
            self.calls[name] = {"path": path, "params": params, "response": r}
        if self.verbose:
            print(f"  [{'ERR ' + err_text(r) if is_error(r) else 'OK '}] mexc {name or path}"[:100], file=sys.stderr)

    def spot(self, path: str, params: dict | None = None, name: str | None = None):
        if path not in SPOT_SIGNED:
            raise ValueError(f"MEXC path not whitelisted (GET read-only only): {path}")
        key, secret = self._credentials()
        r = None
        for attempt in range(4):
            time.sleep(self.sleep if attempt == 0 else 2.0 * attempt)
            p = dict(params or {}, recvWindow=10000, timestamp=self._now())
            q = spot_query(p)
            url = f"{BASE}{path}?{q}&signature={spot_signature(secret, q)}"
            r = _http_get(url, {"X-MEXC-APIKEY": key, "Content-Type": "application/json"})
            if not _rate_limited(r):
                break
        self._record(name, path, params, r)
        return r

    def contract(self, path: str, params: dict | None = None, name: str | None = None):
        if path not in CONTRACT_SIGNED:
            raise ValueError(f"MEXC contract path not whitelisted (GET read-only only): {path}")
        key, secret = self._credentials()
        r = None
        for attempt in range(4):
            time.sleep(self.sleep if attempt == 0 else 2.0 * attempt)
            ps = contract_param_string(params)
            rt = str(self._now())
            headers = {"ApiKey": key, "Request-Time": rt, "Signature": contract_signature(secret, key, rt, ps),
                       "Content-Type": "application/json", "Recv-Window": "30"}
            r = _http_get(f"{CONTRACT}{path}" + (f"?{ps}" if ps else ""), headers)
            if not _rate_limited(r):
                break
        self._record(name, path, params, r)
        return r

    def errors(self) -> dict[str, str]:
        return {n: err_text(c["response"]) for n, c in self.calls.items() if is_error(c["response"])}


def cdata(r):
    """Contract payload (data) or None on error."""
    return None if is_error(r) or not isinstance(r, dict) else r.get("data")


# ---------------------------------------------------------------- prices
def fetch_tickers() -> dict[str, float]:
    r = public("/api/v3/ticker/price")
    out = {}
    if isinstance(r, list):
        for x in r:
            try:
                out[x["symbol"]] = float(x["price"])
            except (KeyError, TypeError, ValueError):
                pass
    return out


def price_usdt(coin: str, tk: dict[str, float]) -> float | None:
    coin = coin.upper()
    if coin in STABLE:
        return 1.0
    for q in QUOTES:
        p = tk.get(coin + q)
        if p:
            return p * (1.0 if q in STABLE else (price_usdt(q, tk) or 0)) or None
    return None


def kline_close(symbol: str, ms: int) -> float | None:
    """Daily close of the candle containing ms (public)."""
    r = public("/api/v3/klines", {"symbol": symbol, "interval": "1d", "startTime": ms - 2 * DAY, "endTime": ms})
    if isinstance(r, list) and r:
        rows = [k for k in r if int(k[0]) <= ms] or r
        try:
            return float(rows[-1][4])
        except (TypeError, ValueError, IndexError):
            return None
    return None


class HistPrices:
    """Historical USDT prices for flows, cached in snapshots/mexc_price_cache.json."""

    def __init__(self, path: Path, offline: bool = False):
        self.path, self.offline = path, offline
        self.cache = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        self.misses: list[str] = []

    def get(self, coin: str, ms: int, tk: dict) -> float | None:
        coin = coin.upper()
        if coin in STABLE:
            return 1.0
        day = dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%Y-%m-%d")
        k = f"{coin}|{day}"
        if k not in self.cache and not self.offline:
            p = kline_close(coin + "USDT", ms)
            if p is None and coin + "USDC" in tk:
                p = kline_close(coin + "USDC", ms)
            self.cache[k] = p
            time.sleep(0.1)
        p = self.cache.get(k)
        if p is None:
            self.misses.append(k)
            p = price_usdt(coin, tk)   # fallback: today's price (flagged in misses)
        return p

    def save(self):
        self.path.parent.mkdir(exist_ok=True)
        self.path.write_text(json.dumps(self.cache, indent=0, sort_keys=True), encoding="utf-8")


# ---------------------------------------------------------------- holdings / snapshot
def balances(acct) -> dict[str, float]:
    out = {}
    for b in (acct or {}).get("balances") or [] if isinstance(acct, dict) else []:
        try:
            q = float(b.get("free") or 0) + float(b.get("locked") or 0)
        except (TypeError, ValueError):
            continue
        if q > 0:
            out[str(b.get("asset")).upper()] = q
    return out


def permissions(acct) -> dict:
    if not isinstance(acct, dict) or is_error(acct):
        return {"ok": False, "error": err_text(acct)}
    return {"ok": True, "canTrade": acct.get("canTrade"), "canWithdraw": acct.get("canWithdraw"),
            "canDeposit": acct.get("canDeposit"), "accountType": acct.get("accountType"),
            "permissions": acct.get("permissions")}


def build_snapshot(mc: MexcClient, t: dt.datetime, tk: dict | None = None) -> dict:
    acct = mc.spot("/api/v3/account", name="account")
    fut = mc.contract("/api/v1/private/account/assets", name="futures_assets")
    pos = mc.contract("/api/v1/private/position/open_positions", name="futures_positions")
    tk = tk if tk is not None else fetch_tickers()
    spot = balances(acct)
    fut_rows = [x for x in (cdata(fut) or []) if isinstance(x, dict)]
    futures = {}
    for x in fut_rows:
        try:
            eq = float(x.get("equity") or 0)
        except (TypeError, ValueError):
            eq = 0.0
        if eq:
            futures[str(x.get("currency")).upper()] = {"equity": eq, "unrealized": float(x.get("unrealized") or 0),
                                                       "positionMargin": float(x.get("positionMargin") or 0)}
    hold, unpriced = [], []
    for coin, q in sorted(spot.items()):
        p = price_usdt(coin, tk)
        if p is None:
            unpriced.append(coin)
        hold.append({"coin": coin, "qty": q, "price": p, "value": q * p if p is not None else None})
    spot_v = sum(h["value"] or 0 for h in hold)
    fut_v = 0.0
    for c, f in futures.items():
        p = price_usdt(c, tk)
        f["value"] = f["equity"] * p if p is not None else None
        fut_v += f["value"] or 0
    return {
        "exchange": "mexc", "captured_oslo": t.isoformat(timespec="seconds"),
        "spot_ok": not is_error(acct), "futures_ok": not is_error(fut),
        "spot_qty": spot, "holdings": hold, "unpriced": unpriced, "futures": futures,
        "positions": [p for p in (cdata(pos) or []) if isinstance(p, dict)],
        "value": {"spot": spot_v, "futures": fut_v, "total": spot_v + fut_v},
        "permissions": permissions(acct),
        "errors": mc.errors(),
    }


def write_snapshot(directory: Path, doc: dict, t: dt.datetime) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / f"mexc_snapshot_{stamp(t)}.json"
    p.write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
    return p


def latest_snapshot(roots: list[Path]) -> tuple[dict | None, Path | None]:
    files = [p for r in roots if r.exists() for p in list(r.glob("mexc_snapshot_*.json")) + list(r.glob("**/mexc_snapshot_*.json"))
             if not p.name.startswith("._")]  # skip macOS AppleDouble files
    if not files:
        return None, None
    p = max(files, key=lambda p: p.name)
    return json.loads(p.read_text(encoding="utf-8")), p


# ---------------------------------------------------------------- archive (incremental)
# Spot capital sources: time-windowed. lookback = how far back the first run goes.
ARCH_SPOT = [
    dict(name="mexc_deposits", path="/api/v3/capital/deposit/hisrec", window=7 * DAY, lookback=89,
         key=("txId", "coin", "amount", "insertTime"), time=("insertTime",), params={"limit": 1000}, page=None),
    dict(name="mexc_withdrawals", path="/api/v3/capital/withdraw/history", window=7 * DAY, lookback=89,
         key=("id",), time=("applyTime", "updateTime"), params={"limit": 1000}, page=None),
    # live API (2026-10): internal transfers reject windows > 7 days
    dict(name="mexc_internal_transfers", path="/api/v3/capital/transfer/internal", window=7 * DAY, lookback=89,
         key=("tranId", "timestamp"), time=("timestamp",), params={"limit": 100}, page="page"),
    # live API: limit > 20 → 700004 (docs say max 1000); a time window hides older rows, so page without one
    dict(name="mexc_dust_log", path="/api/v3/capital/convert", window=None, lookback=89,
         key=("convertTime", "totalConvert"), time=("convertTime",), params={"limit": 20}, page="page"),
    # live API: startTime older than ~90 days → 33333 "date is incorrect" (docs say 6 months)
    *[dict(name="mexc_universal_transfers", sub=f"{a}-{b}", path="/api/v3/capital/transfer", window=30 * DAY,
           lookback=89, key=("tranId",), time=("timestamp",),
           params={"fromAccountType": a, "toAccountType": b, "size": 100}, page="page")
      for a, b in (("SPOT", "FUTURES"), ("FUTURES", "SPOT"))],
]
ARCH_CONTRACT = [
    dict(name="mexc_futures_transfers", path="/api/v1/private/account/transfer_record", key=("id",), time=("createTime",)),
    dict(name="mexc_futures_positions", path="/api/v1/private/position/list/history_positions", key=("positionId",),
         time=("updateTime", "createTime")),
    dict(name="mexc_futures_funding", path="/api/v1/private/position/funding_records", key=("id",), time=("settleTime",)),
]
TRADES = dict(name="mexc_trades", path="/api/v3/myTrades", key=("id",), time=("time",), lookback=29)
OVERLAP = 2 * DAY


def _rows(r) -> tuple[list, dict]:
    """Normalize list / {"data": [...]} / {"rows": [...]} / [{"rows": [...]}] payloads."""
    if isinstance(r, list):
        if len(r) == 1 and isinstance(r[0], dict) and isinstance(r[0].get("rows"), list):
            return r[0]["rows"], r[0]
        return [x for x in r if isinstance(x, dict)], {}
    if isinstance(r, dict):
        for k in ("rows", "data", "resultList"):
            v = r.get(k)
            if isinstance(v, list):
                return v, r
            if isinstance(v, dict):
                for k2 in ("resultList", "rows", "data"):
                    if isinstance(v.get(k2), list):
                        return v[k2], v
    return [], {}


def _fetch_spot(mc: MexcClient, src: dict, start: int, end: int) -> tuple[list, list, int]:
    rows, errs, calls = [], [], 0
    s = start
    while s < end:
        e = min(end, s + src["window"]) if src.get("window") else end
        for page in range(1, 51):
            p = dict(src.get("params") or {})
            if src.get("window"):
                p.update(startTime=s, endTime=e)
            if src.get("page"):
                p[src["page"]] = page
            r = mc.spot(src["path"], p)
            calls += 1
            if is_error(r):
                errs.append({"window": [ms_to_oslo(s), ms_to_oslo(e)], "code": r.get("code"), "msg": err_text(r)})
                break
            batch, meta = _rows(r)
            rows.extend(batch)
            size = int((src.get("params") or {}).get("size") or (src.get("params") or {}).get("limit") or 100)
            total_pages = meta.get("totalPageNum") or meta.get("totalPage")
            if not src.get("page") or len(batch) < size or (total_pages and page >= int(total_pages)):
                break
        s = e
    return rows, errs, calls


def _fetch_contract(mc: MexcClient, src: dict, start: int) -> tuple[list, list, int]:
    """Newest-first paged lists without a time filter: stop when a page is older than start or short."""
    rows, errs, calls = [], [], 0
    from .archive import record_ms
    for page in range(1, 51):
        r = mc.contract(src["path"], {"page_num": page, "page_size": 100})
        calls += 1
        if is_error(r):
            errs.append({"code": r.get("code"), "msg": err_text(r)})
            break
        batch, meta = _rows(r)
        rows.extend(batch)
        times = [record_ms(x, src["time"]) or 0 for x in batch]
        if len(batch) < 100 or (times and max(times) < start):
            break
        tp = meta.get("totalPage")
        if tp and page >= int(tp):
            break
    return rows, errs, calls


def _fetch_trades(mc: MexcClient, symbol: str, start: int, end: int, depth: int = 0) -> tuple[list, list, int]:
    """myTrades returns ≤100 rows; split the window until each piece has <100."""
    r = mc.spot(TRADES["path"], {"symbol": symbol, "startTime": start, "endTime": end, "limit": 100})
    if is_error(r):
        return [], [{"symbol": symbol, "window": [ms_to_oslo(start), ms_to_oslo(end)], "code": r.get("code"), "msg": err_text(r)}], 1
    batch, _ = _rows(r)
    if len(batch) < 100 or end - start < 60_000 or depth > 20:
        return batch, [], 1
    mid = (start + end) // 2
    a, ea, ca = _fetch_trades(mc, symbol, start, mid, depth + 1)
    b, eb, cb = _fetch_trades(mc, symbol, mid + 1, end, depth + 1)
    return a + b, ea + eb, 1 + ca + cb


def trade_symbols(spot_qty: dict, store_root: Path, exchange_symbols: dict[str, tuple[str, str]], extra_coins=()) -> list[str]:
    coins = {c.upper() for c in spot_qty} | {c.upper() for c in extra_coins}
    syms = set()
    for f in store_root.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]/mexc_trades.jsonl"):
        with f.open(encoding="utf-8") as fh:
            syms |= {json.loads(line).get("symbol") for line in fh if line.strip()}
    for s, (base, quote) in exchange_symbols.items():
        if base in coins and quote in QUOTES and (quote in coins or quote in ("USDT", "USDC")):
            syms.add(s)
    return sorted(x for x in syms if x and x in exchange_symbols)


def exchange_symbols() -> dict[str, tuple[str, str]]:
    r = public("/api/v3/exchangeInfo")
    out = {}
    if isinstance(r, dict):
        for s in r.get("symbols") or []:
            out[s.get("symbol")] = (str(s.get("baseAsset")).upper(), str(s.get("quoteAsset")).upper())
    return out


def _start(src_lookback_days: int, st: dict, since: dt.date | None, now_ms: int) -> int:
    floor = now_ms - src_lookback_days * DAY
    if since:
        base = int(dt.datetime.combine(since, dt.time(), OSLO).timestamp() * 1000)
    elif st.get("last_end_ms"):
        base = int(st["last_end_ms"]) - OVERLAP
    else:
        base = floor
    return max(base, floor)


def archive_run(mc: MexcClient, root: Path, since: dt.date | None = None, progress: bool = True, on_step=None) -> dict:
    """Incremental archive into root (archive/mexc/YYYY-MM/<source>.jsonl), state in root/state.json."""
    from .archive import Store, _load_state, record_ms
    t0 = time.time()
    root.mkdir(parents=True, exist_ok=True)
    state_path = root / "state.json"
    state = _load_state(state_path)
    store = Store(root)
    t = now_oslo()
    now_ms = mc._now()
    rep: dict = {"captured_oslo": t.isoformat(timespec="seconds"), "root": str(root), "sources": {}, "errors": {}, "exchange": "mexc"}

    def account(src, label, rows, errs, calls, start):
        st = state["sources"].setdefault(label, {})
        added, dup, mx = store.add(src, rows)
        agg = rep["sources"].setdefault(src["name"], {"fetched": 0, "added": 0, "duplicates": 0, "calls": 0, "parts": 0, "from": None})
        agg["fetched"] += len(rows); agg["added"] += added; agg["duplicates"] += dup; agg["calls"] += calls; agg["parts"] += 1
        agg["from"] = min(agg["from"] or start, start)
        if errs:
            rep["errors"][label] = errs
            st["last_errors"] = errs
        else:
            st["last_end_ms"] = now_ms
            st.pop("last_errors", None)
        if mx:
            st["last_record_ms"] = max(int(st.get("last_record_ms") or 0), mx)
        st["last_run_oslo"] = rep["captured_oslo"]
        if progress:
            print(f"  {label:40} +{added:<6} dup {dup:<6} calls {calls:<4} {time.time() - t0:6.0f}s"
                  + (f"  ERR {errs[0].get('code')}" if errs else ""), file=sys.stderr, flush=True)

    tk = fetch_tickers()
    snap = build_snapshot(mc, t, tk)
    exsyms = exchange_symbols()
    steps = {"done": 0, "total": len(ARCH_SPOT) + len(ARCH_CONTRACT) + 2}

    def step(label):
        steps["done"] += 1
        if on_step:
            on_step(label, steps["done"], steps["total"])

    for src in ARCH_SPOT:
        label = src["name"] + (f":{src['sub']}" if src.get("sub") else "")
        st = state["sources"].get(label, {})
        start = _start(src["lookback"], st, since, now_ms)
        rows, errs, calls = _fetch_spot(mc, src, start, now_ms)
        account(src, label, rows, errs, calls, start); step(label)
    fut_ok = snap.get("futures_ok")
    for src in ARCH_CONTRACT:
        st = state["sources"].get(src["name"], {})
        start = _start(3650, st, since, now_ms)
        if not fut_ok:
            rep["errors"][src["name"]] = [{"code": "SKIP", "msg": "futures account not readable: " + snap["errors"].get("futures_assets", "")}]
            step(src["name"]); continue
        rows, errs, calls = _fetch_contract(mc, src, start)
        account(src, src["name"], rows, errs, calls, start); step(src["name"])
    # trades: per symbol
    dep_coins = set()
    for f in root.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]/mexc_deposits.jsonl"):
        with f.open(encoding="utf-8") as fh:
            dep_coins |= {coin_of(json.loads(line)) for line in fh if line.strip()}
    syms = trade_symbols(snap.get("spot_qty") or {}, root, exsyms, dep_coins) if snap.get("spot_ok") else []
    steps["total"] += len(syms)
    for sym in syms:
        label = f"mexc_trades:{sym}"
        st = state["sources"].get(label, {})
        start = _start(TRADES["lookback"], st, since, now_ms)
        rows, errs, calls = [], [], 0
        s = start
        while s < now_ms:   # ≤ 7-day pieces keep the split depth small
            e = min(now_ms, s + 7 * DAY)
            r, er, c = _fetch_trades(mc, sym, s, e)
            rows += r; errs += er; calls += c
            s = e + 1
        account(dict(TRADES, sub=sym), label, rows, errs, calls, start); step(label)
    if snap.get("spot_ok"):
        p = write_snapshot(root / t.strftime("%Y-%m") / "snapshots", snap, t)
        rep["snapshot"] = str(p.relative_to(root))
    else:
        rep["errors"]["snapshot"] = [{"code": "ACCOUNT", "msg": snap["errors"].get("account", "")}]
    step("snapshot")
    for name in rep["sources"]:
        src = next((s for s in ARCH_SPOT + ARCH_CONTRACT + [TRADES] if s["name"] == name), TRADES)
        store._load(name, src)
        rep["sources"][name]["total"] = store.count[name]
        e = store.earliest.get(name)
        rep["sources"][name]["earliest"] = ms_to_oslo(e)[:10] if e else None
        rep["sources"][name]["from"] = ms_to_oslo(rep["sources"][name]["from"])[:10]
    state["runs"] = (state.get("runs") or [])[-49:] + [{
        "at": rep["captured_oslo"], "since": since.isoformat() if since else None,
        "added": {k: v["added"] for k, v in rep["sources"].items()}, "errors": sorted(rep["errors"])}]
    state.setdefault("first_run_oslo", rep["captured_oslo"])
    state_path.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")
    return rep


# ---------------------------------------------------------------- analysis (offline + public prices)
def load_archive(root: Path) -> dict[str, list]:
    out: dict[str, list] = defaultdict(list)
    for f in sorted(root.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]/*.jsonl")):
        if f.name.startswith("._"):  # macOS AppleDouble
            continue
        with f.open(encoding="utf-8") as fh:
            out[f.stem].extend(json.loads(line) for line in fh if line.strip())
    return dict(out)


def coin_of(rec: dict, field: str = "coin") -> str:
    """Live deposit/withdraw records carry the network in the coin ("USDT-PLASMA", "USDC-ARBNEW", "BTC-LIGHTNING")."""
    c = str(rec.get(field) or "").upper()
    return c.split("-", 1)[0] if "-" in c else c


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _int(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None



# ---------------------------------------------------------------- baseline (track PnL from a fixed point on)
SPLIT_QUOTES = ("USDT", "USDC", "USD1", "BTC", "ETH")


def split_symbol(sym: str) -> tuple[str, str] | None:
    for q in SPLIT_QUOTES:
        if sym.endswith(q) and len(sym) > len(q):
            return sym[: -len(q)], q
    return None


def baseline_info(ov: dict) -> dict | None:
    at, v = ov.get("mexc_baseline_at"), ov.get("mexc_baseline_usdt")
    if not at or v in (None, ""):
        return None
    try:
        t = dt.datetime.fromisoformat(at)
        t = t if t.tzinfo else t.replace(tzinfo=OSLO)
        return {"at": t.isoformat(timespec="seconds"), "ms": int(t.timestamp() * 1000), "value": float(v),
                "earn": ov.get("mexc_baseline_earn_usdt"), "snapshot": ov.get("mexc_baseline_snapshot")}
    except (TypeError, ValueError):
        return None


def find_snapshot(roots: list[Path], name: str | None) -> dict | None:
    if not name:
        return None
    name = Path(name).name
    for r in roots:
        if not r.exists():
            continue
        for p in [r / name, *r.glob(f"**/{name}")]:
            if p.exists() and not p.name.startswith("._"):
                return json.loads(p.read_text(encoding="utf-8"))
    return None


def since_baseline(arch: dict, base: dict, snap0: dict | None, flows: list[dict], hp, tk: dict) -> dict:
    """Realized results after the baseline. Spot: FIFO lots seeded from the baseline snapshot (qty × its price),
    deposits add lots at the deposit-day price, withdrawals remove lots without PnL, trades/dust realize PnL.
    Stablecoins are valued at 1 and never produce PnL."""
    b = base["ms"]
    from collections import deque
    lots: dict[str, deque] = defaultdict(deque)
    deficit: dict[str, float] = defaultdict(float)
    for h in (snap0 or {}).get("holdings") or []:
        c = str(h.get("coin")).upper()
        if c not in STABLE and (h.get("qty") or 0) > 0:
            lots[c].append([float(h["qty"]), float(h.get("price") or 0)])

    def add(c, q, unit):
        if c not in STABLE and q > 0:
            lots[c].append([q, unit])

    def take(c, q) -> float:
        """Remove q units FIFO, return their cost (USDT)."""
        if c in STABLE:
            return q
        cost, dq = 0.0, lots[c]
        while q > 1e-15 and dq:
            lot = dq[0]; x = min(q, lot[0])
            cost += x * lot[1]; lot[0] -= x; q -= x
            if lot[0] <= 1e-15:
                dq.popleft()
        if q > 1e-12:
            deficit[c] += q   # sold more than known lots: cost 0 (flagged)
        return cost

    ev = []
    for f in flows:
        if f.get("_ms", 0) >= b and f["kind"] in ("deposit", "withdrawal", "internal_in", "internal_out"):
            ev.append((f["_ms"], "flow", f))
    for x in arch.get("mexc_trades", []):
        ms = _int(x.get("time")) or 0
        if ms >= b:
            ev.append((ms, "trade", x))
    for d in arch.get("mexc_dust_log", []):
        for x in d.get("convertDetails") or []:
            ms = _int(x.get("time")) or _int(d.get("convertTime")) or 0
            if ms >= b:
                ev.append((ms, "dust", x))
    ev.sort(key=lambda e: e[0])
    spot_real, fees_usd, n_trades, per = 0.0, 0.0, 0, defaultdict(float)
    for ms, kind, x in ev:
        if kind == "flow":
            c, q = str(x["coin"]).upper(), abs(x["qty"] or 0)
            if (x["qty"] or 0) > 0:
                add(c, q, (x["usd"] or 0) / q if q else 0)
            else:
                take(c, q)
        elif kind == "trade":
            sp = split_symbol(str(x.get("symbol")))
            if not sp:
                continue
            bc, qc = sp; q = _f(x.get("qty")); qq = _f(x.get("quoteQty"))
            qp = hp.get(qc, ms, tk) if qc not in STABLE else 1.0
            val = qq * (qp or 0); n_trades += 1
            if x.get("isBuyer"):
                pnl = val - take(qc, qq) if qc not in STABLE else 0.0   # paying with a non-stable quote realizes it
                add(bc, q, val / q if q else 0)
            else:
                pnl = val - take(bc, q)
                add(qc, qq, qp or 0)
            fa = str(x.get("commissionAsset") or "").upper(); fq = _f(x.get("commission"))
            fu = fq * ((hp.get(fa, ms, tk) if fa not in STABLE else 1.0) or 0)
            if fa and fa not in STABLE:
                take(fa, fq)
            fees_usd += fu; pnl -= fu
            spot_real += pnl; per[bc] += pnl
        else:  # dust → MX
            c = str(x.get("asset")).upper(); q = _f(x.get("amount")); mx = _f(x.get("convert"))
            mp = hp.get("MX", ms, tk) or 0
            pnl = mx * mp - take(c, q); add("MX", mx, mp)
            spot_real += pnl; per[c] += pnl
    fut = [x for x in arch.get("mexc_futures_positions", []) if (_int(x.get("updateTime")) or 0) >= b]
    fund = [x for x in arch.get("mexc_futures_funding", []) if (_int(x.get("settleTime")) or 0) >= b]
    return {"spot_realized": spot_real, "spot_trades": n_trades, "spot_fees_usdt": fees_usd,
            "spot_realized_by_coin": {k: v for k, v in sorted(per.items(), key=lambda kv: kv[1])},
            "futures_realized": sum(_f(x.get("realised")) for x in fut), "futures_positions": len(fut),
            "funding": sum(_f(x.get("funding")) for x in fund), "funding_records": len(fund),
            "inventory_deficits": {k: v for k, v in deficit.items() if v > 1e-12},
            "opening_lots_from": (snap0 or {}).get("captured_oslo"),
            "method": "FIFO；起點持倉以基準快照的數量×價格為成本；穩定幣以 1 計；手續費自已實現扣除。"}

EARN_STALE_DAYS = 30


def earn_manual(ov: dict, now: dt.datetime | None = None) -> dict | None:
    """Manually entered MEXC Earn balance (fixed USDT value, never revalued). None when not set."""
    v = ov.get("mexc_earn_usdt")
    try:
        v = float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        v = None
    if v is None:
        return None
    at = ov.get("mexc_earn_set_at")
    age = None
    if at:
        try:
            age = ((now or now_oslo()) - dt.datetime.fromisoformat(at)).total_seconds() / 86400
        except (TypeError, ValueError):
            age = None
    return {"value": v, "set_at": at, "age_days": age, "stale": age is None or age > EARN_STALE_DAYS,
            "label": "手動填入", "source": "manual"}


def analyze(project: Path, archive_root: Path | None = None, offline: bool = False, tickers: dict | None = None,
            overrides: dict | None = None) -> dict:
    """Per-exchange MEXC result. {"present": False, ...} when there is no MEXC data at all."""
    root = archive_root or project / "archive" / "mexc"
    snap, snap_path = latest_snapshot([root, project / "snapshots"])
    configured = available()
    if snap is None:
        return {"present": False, "configured": configured,
                "reason": "尚無 MEXC 快照" + ("（執行 archive 或 refresh）" if configured else "；" + no_creds_msg())}
    ov = overrides or {}
    arch = load_archive(root) if root.exists() else {}
    tk = tickers if tickers is not None else ({} if offline else fetch_tickers())
    hp = HistPrices(project / "snapshots" / "mexc_price_cache.json", offline=offline)

    # current value: snapshot quantities × live price (fallback: snapshot price)
    hold = []
    for h in snap.get("holdings") or []:
        p = price_usdt(h["coin"], tk) if tk else None
        p = p if p is not None else h.get("price")
        hold.append({**h, "price": p, "value": h["qty"] * p if p is not None else None})
    spot_v = sum(h["value"] or 0 for h in hold)
    fut_v = 0.0
    for c, f in (snap.get("futures") or {}).items():
        p = (price_usdt(c, tk) if tk else None) or (1.0 if c in STABLE else None)
        fut_v += f["equity"] * p if p is not None else 0
    api_total = spot_v + fut_v
    earn = earn_manual(ov)
    total = api_total + (earn["value"] if earn else 0.0)

    flows = []
    for d in arch.get("mexc_deposits", []):
        st = _int(d.get("status"))
        if st not in DEPOSIT_OK:
            continue
        ms = _int(d.get("insertTime")) or 0
        q = _f(d.get("amount")); p = hp.get(coin_of(d), ms, tk)
        flows.append({"time": ms_to_oslo(ms), "ms": ms, "kind": "deposit", "coin": coin_of(d), "qty": q,
                      "usd": q * p if p is not None else None, "network": d.get("network")})
    for w in arch.get("mexc_withdrawals", []):
        if _int(w.get("status")) not in WITHDRAW_OK:
            continue
        ms = _int(w.get("applyTime")) or _int(w.get("updateTime")) or 0
        q = _f(w.get("amount")); p = hp.get(coin_of(w), ms, tk)
        flows.append({"time": ms_to_oslo(ms), "ms": ms, "kind": "withdrawal", "coin": coin_of(w), "qty": -q,
                      "usd": -q * p if p is not None else None, "fee": _f(w.get("transactionFee")), "network": w.get("network")})
    # internal (UID/email/mobile) transfers: own account = the identifier that appears most often
    it = [x for x in arch.get("mexc_internal_transfers", []) if str(x.get("status", "SUCCESS")).upper() == "SUCCESS"]
    cnt = defaultdict(int)
    for x in it:
        cnt[str(x.get("fromAccount"))] += 1; cnt[str(x.get("toAccount"))] += 1
    me = max(cnt, key=cnt.get) if cnt else None
    for x in it:
        ms = _int(x.get("timestamp")) or 0
        q = _f(x.get("amount")); p = hp.get(str(x.get("asset")), ms, tk)
        sign = 1 if str(x.get("toAccount")) == me and str(x.get("fromAccount")) != me else -1 if str(x.get("fromAccount")) == me else 0
        flows.append({"time": ms_to_oslo(ms), "ms": ms, "kind": "internal_in" if sign > 0 else "internal_out" if sign < 0 else "internal_unknown",
                      "coin": x.get("asset"), "qty": sign * q, "usd": sign * q * p if p is not None else None})
    flows.sort(key=lambda f: f["ms"])
    dep = sum(f["usd"] or 0 for f in flows if (f["usd"] or 0) > 0)
    wd = -sum(f["usd"] or 0 for f in flows if (f["usd"] or 0) < 0)
    net = dep - wd
    # ---- baseline: PnL = total − baseline − net external inflow after the baseline (Bitget↔MEXC counts as external here)
    base = baseline_info(ov)
    bl = None
    if base:
        after = [f for f in flows if f["ms"] >= base["ms"]]
        b_dep = sum(f["usd"] or 0 for f in after if (f["usd"] or 0) > 0)
        b_wd = -sum(f["usd"] or 0 for f in after if (f["usd"] or 0) < 0)
        snap0 = find_snapshot([root, project / "snapshots"], base.get("snapshot"))
        sb = since_baseline(arch, base, snap0, [{**f, "_ms": f["ms"]} for f in flows], hp, tk)
        capital = base["value"] + b_dep - b_wd
        pnl_b = total - capital
        explained = sb["spot_realized"] + sb["futures_realized"] + sb["funding"]
        bl = {**base, "deposits": b_dep, "withdrawals": b_wd, "net_inflow": b_dep - b_wd, "capital": capital,
              "pnl": pnl_b, "pnl_pct": pnl_b / capital * 100 if capital > 0 else None,
              "flows_after": len(after), "unpriced_flows": sum(1 for f in after if f["usd"] is None),
              **sb, "other": pnl_b - explained, "label": f"自 {base['at'][:10]} 起計算",
              "baseline_snapshot_found": snap0 is not None}

    # trades (≈ last month only): volume and fees per symbol
    trades = arch.get("mexc_trades", [])
    by_sym = defaultdict(lambda: {"buys": 0, "sells": 0, "quote_bought": 0.0, "quote_sold": 0.0})
    fees = defaultdict(float)
    for x in trades:
        s = by_sym[x.get("symbol")]
        if x.get("isBuyer"):
            s["buys"] += 1; s["quote_bought"] += _f(x.get("quoteQty"))
        else:
            s["sells"] += 1; s["quote_sold"] += _f(x.get("quoteQty"))
        fees[str(x.get("commissionAsset"))] += _f(x.get("commission"))
    fee_usd = sum(q * (price_usdt(c, tk) or 0) for c, q in fees.items()) if tk else None
    fut_real = sum(_f(x.get("realised")) for x in arch.get("mexc_futures_positions", []))
    fut_fund = sum(_f(x.get("funding")) for x in arch.get("mexc_futures_funding", []))

    def earliest(name, fields):
        from .archive import record_ms
        ts = [record_ms(r, fields) for r in arch.get(name, [])]
        ts = [x for x in ts if x]
        return ms_to_oslo(min(ts))[:10] if ts else None

    state_p = root / "state.json"
    state = json.loads(state_p.read_text(encoding="utf-8")) if state_p.exists() else {}
    complete = ov.get("mexc_history_complete")
    early = bool(flows) and any((_int(x.get("settleTime")) or 0) < flows[0]["ms"] for x in arch.get("mexc_futures_funding", []))
    hp.save()
    return {
        "present": True, "configured": configured,
        "snapshot_time": snap.get("captured_oslo"), "snapshot_file": str(snap_path.relative_to(project)) if snap_path else None,
        "prices": "live tickers" if tk else "snapshot prices",
        "total": total, "total_api": api_total, "spot": spot_v, "futures": fut_v,
        "earn": earn["value"] if earn else None, "earn_manual": earn,
        "pnl_without_earn": (api_total - net) if flows else None,
        "holdings": sorted(hold, key=lambda h: -(h["value"] or 0)),
        "unpriced": [h["coin"] for h in hold if h["price"] is None],
        "deposits": dep, "withdrawals": wd,
        # with a baseline: net_inflow = baseline + net external inflow after it (the capital PnL is measured against)
        "net_inflow": bl["capital"] if bl else net,
        "pnl": bl["pnl"] if bl else ((total - net) if flows else None),
        "pnl_pct": bl["pnl_pct"] if bl else (((total - net) / net * 100) if flows and net > 0 else None),
        "pnl_method": "baseline" if bl else "all_history",
        "baseline": bl,
        "all_history": {"deposits": dep, "withdrawals": wd, "net_inflow": net, "pnl": (total - net) if flows else None},
        "history_complete": True if bl else complete,
        "flows": [{k: v for k, v in f.items() if k != "ms"} for f in flows],
        "activity_before_first_flow": early,
        "trades": {"count": len(trades), "earliest": earliest("mexc_trades", ("time",)),
                   "by_symbol": {k: v for k, v in sorted(by_sym.items())}, "fees": dict(fees), "fees_usdt": fee_usd},
        "futures_history": {"realised": fut_real, "funding": fut_fund,
                            "positions": len(arch.get("mexc_futures_positions", []))},
        "coverage": {"deposits_from": earliest("mexc_deposits", ("insertTime",)),
                     "withdrawals_from": earliest("mexc_withdrawals", ("applyTime",)),
                     "first_archive_run": state.get("first_run_oslo"),
                     "query_floor_days": {"deposits/withdrawals": 90, "myTrades": 30, "universal transfer": 90}},
        "permissions": snap.get("permissions"),
        "errors": {**(snap.get("errors") or {}), **{k: v.get("last_errors") for k, v in (state.get("sources") or {}).items() if v.get("last_errors")}},
        "price_misses": sorted(set(hp.misses)),
        "notes": ([
            f"盈虧自 {bl['at'][:16].replace('T', ' ')}（Oslo）起計算：目前總資產 − 基準 {bl['value']:,.2f} − 基準後淨入金 {bl['net_inflow']:,.2f}。"
            "基準之前的紀錄保留在 archive，但不計入盈虧。Bitget↔MEXC 互轉在 MEXC 視為入金／出金，在合併總覽視為內部。",
            "理財為手動填入的固定值：理財收益只有在更新理財金額後才會反映在盈虧中。",
            *(["⚠ 找不到基準快照，現貨已實現（FIFO）的起點持倉成本以 0 計。"] if not bl["baseline_snapshot_found"] else []),
            *([f"⚠ 賣出超過已知持倉（成本以 0 計）：{', '.join(f'{k} {v:g}' for k, v in bl['inventory_deficits'].items())}"] if bl["inventory_deficits"] else []),
            ] if bl else []) + [
            "MEXC 的充提紀錄 API 最多只能查 90 天、成交紀錄約 1 個月；更早的歷史請用網頁匯出。",
            *([] if bl else ["淨入金與盈虧只包含已封存的紀錄；若帳戶在封存起點之前已有資產，盈虧會偏高"
              + ("（已在 analysis_overrides.json 標記 mexc_history_complete=true）" if complete else "（未確認完整）")]),
            "提幣以 amount 計；若手續費另外扣除，該手續費會反映在盈虧中。",
            *(["⚠ 合約資金費紀錄早於最早的充提紀錄：封存起點之前帳戶已有資金，淨入金不完整，盈虧不可靠。"] if early and not complete and not bl else []),
            ("MEXC 理財（Earn）沒有公開的 API：使用手動填入的 "
             f"{earn['value']:,.2f} USDT（{earn['set_at'][:10]} 填入，固定值、不重新估價）"
             + ("。⚠ 已超過 30 天，請在 MEXC 分頁更新。" if earn["stale"] else "。")) if earn else
            "MEXC 理財（Earn）沒有公開的 API，看不到；可在 MEXC 分頁手動填入理財金額。",
            "存入／贖回理財屬於帳戶內部移動：不在任何封存紀錄裡，也不計入淨入金（現貨↔合約劃轉同樣視為內部）。",
        ],
    }


# ---------------------------------------------------------------- perms
def perms_report(mc: MexcClient) -> dict:
    acct = mc.spot("/api/v3/account", name="account")
    p = permissions(acct)
    probes = {
        "SPOT_ACCOUNT_READ（帳戶/成交）": acct,
        "SPOT_DEAL_READ（委託/成交讀取）": mc.spot("/api/v3/mxDeduct/enable", name="mx_deduct"),
        "SPOT_WITHDRAW_READ（充提紀錄讀取）": mc.spot("/api/v3/capital/deposit/hisrec", {"limit": 1}, name="deposit_probe"),
        "SPOT_TRANSFER_READ（劃轉紀錄讀取）": mc.spot("/api/v3/capital/transfer", {"fromAccountType": "SPOT", "toAccountType": "FUTURES", "size": 1}, name="transfer_probe"),
        "CONTRACT 讀取（合約資產）": mc.contract("/api/v1/private/account/assets", name="futures_probe"),
    }
    p["read_probes"] = {k: ("OK" if not is_error(v) else err_text(v)) for k, v in probes.items()}
    return p
