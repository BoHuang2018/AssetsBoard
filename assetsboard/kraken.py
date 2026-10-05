"""Kraken (optional third exchange): read-only signed client, holdings snapshot, incremental ledger archive, analysis.

Rules:
- standard library only. Kraken private REST calls are POST (that is how Kraken's API works), but only the read
  methods in READ_METHODS can be sent; AddOrder, Withdraw, WalletTransfer, Earn/Allocate … raise *before* any
  request is built (``check_method``). Public market data is GET.
- credentials KRAKEN_API_KEY / KRAKEN_API_SECRET via env → macOS Keychain (service "assetsboard") → box-secrets.json;
  missing credentials ⇒ everything Kraken-related is skipped (``available()`` is False).
- API-Sign = base64(HMAC-SHA512(base64decode(secret), uri_path + SHA256(nonce + postdata))).
- Rate limit (Spot REST): call counter max 15 (Starter) decaying 0.33/s; Ledgers/QueryLedgers/TradesHistory cost 2.
  A local token bucket keeps under it; "EAPI:Rate limit exceeded" waits and retries.
- Kraken keeps the full ledger, so the first archive run pages through all history (ofs, 50 per call) and later runs
  only fetch entries newer than the last one archived.
Asset names: XXBT/XBT→BTC, XXDG→DOGE, ZEUR→EUR, ZUSD→USD, ETH2→ETH; suffixes .S (staked) .M (opt-in rewards)
.B (yield-bearing Earn) .F (Kraken Rewards auto-earn) .P (parachain) .HOLD are balances of the same coin.
"""
from __future__ import annotations

import base64
import datetime as dt
import gzip
import hashlib
import hmac
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

from .client import KRAKEN_SECRET_NAMES, OSLO, cli_cmd, credential_sources, credentials_available, load_secret, ms_to_oslo, now_oslo, stamp

API = "https://api.kraken.com"
DAY = 86_400_000

# ---------------------------------------------------------------- allowlist (read-only)
READ_METHODS = {"Balance", "BalanceEx", "TradeBalance", "Ledgers", "QueryLedgers", "TradesHistory",
                "ClosedOrders", "OpenOrders", "Earn/Allocations"}
# documented write / state-changing methods: named here only so the refusal message is explicit
WRITE_METHODS = {"AddOrder", "AddOrderBatch", "AmendOrder", "EditOrder", "CancelOrder", "CancelAll", "CancelAllOrdersAfter",
                 "CancelOrderBatch", "Withdraw", "WithdrawCancel", "WalletTransfer", "Earn/Allocate", "Earn/Deallocate",
                 "Stake", "Unstake", "CreateSubaccount", "AccountTransfer", "AddExport", "RemoveExport",
                 "DepositAddresses", "GetWebSocketsToken"}
PUBLIC_METHODS = {"Time", "SystemStatus", "Ticker", "AssetPairs", "Assets", "OHLC", "Trades"}
COST = {"Ledgers": 2, "QueryLedgers": 2, "TradesHistory": 2}


class WriteRefused(PermissionError):
    pass


def check_method(method: str) -> str:
    """Return the private URI path for an allowlisted read method; refuse everything else before sending."""
    m = method.strip().strip("/")
    if m.startswith("0/private/"):
        m = m[len("0/private/"):]
    if m in WRITE_METHODS:
        raise WriteRefused(f"Kraken {m} 是寫入／狀態變更端點，assetsboard 一律拒絕（未送出）")
    if m not in READ_METHODS:
        raise WriteRefused(f"Kraken {m} 不在唯讀白名單內，拒絕（未送出）")
    return f"/0/private/{m}"


# ---------------------------------------------------------------- signing (pure, unit-tested)
def sign(uri_path: str, postdata: str, nonce, secret_b64: str) -> str:
    msg = uri_path.encode() + hashlib.sha256((str(nonce) + postdata).encode()).digest()
    return base64.b64encode(hmac.new(base64.b64decode(secret_b64), msg, hashlib.sha512).digest()).decode()


def encode_form(params: dict) -> str:
    return urllib.parse.urlencode([(k, str(v).lower() if isinstance(v, bool) else str(v)) for k, v in params.items() if v is not None])


def secret_problem(key: str, secret: str) -> str | None:
    """Local sanity check of the stored pair (never prints values). Kraken API Key ≈ 56 chars; Private Key = base64 of 64 bytes (88 chars)."""
    if not key or not secret:
        return "Kraken API Key 或 Private Key 是空的"
    if key == secret:
        return ("KRAKEN_API_SECRET 與 KRAKEN_API_KEY 是同一個值（Private Key 沒有存進去，可能兩次都貼了 API Key）。"
                f"請執行：{cli_cmd()} setup-keychain --exchange kraken --only KRAKEN_API_SECRET，貼上 Kraken 建立 Key 時顯示的 Private Key")
    try:
        n = len(base64.b64decode(secret, validate=True))
    except (ValueError, TypeError):
        n = -1
    if n != 64:
        return ("KRAKEN_API_SECRET 不像 Kraken Private Key（應為 88 字元、base64 解碼後 64 bytes）。"
                f"請執行：{cli_cmd()} setup-keychain --exchange kraken --only KRAKEN_API_SECRET")
    return None


def available() -> bool:
    return credentials_available(KRAKEN_SECRET_NAMES)


def sources() -> dict:
    return credential_sources(KRAKEN_SECRET_NAMES)


def no_creds_msg() -> str:
    return (f"Kraken 未設定憑證（KRAKEN_API_KEY／KRAKEN_API_SECRET），略過。在 Mac 的 ~/assetsboard 執行："
            f"{cli_cmd()} setup-keychain --exchange kraken")


def _http(req: urllib.request.Request, timeout: int = 30):
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {"error": [f"EHTTP:{e.code}"]}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        return {"error": [f"ENET:{type(e).__name__}: {e}"[:200]]}


def is_error(r) -> bool:
    return not isinstance(r, dict) or bool(r.get("error")) or "result" not in r


def err_text(r) -> str:
    if not isinstance(r, dict):
        return str(r)[:200]
    return "; ".join(map(str, r.get("error") or ["no result"]))[:200]


def public(method: str, params: dict | None = None):
    if method not in PUBLIC_METHODS:
        raise ValueError(f"Kraken public method not allowlisted: {method}")
    q = ("?" + encode_form(params)) if params else ""
    return _http(urllib.request.Request(f"{API}/0/public/{method}{q}", headers={"User-Agent": "assetsboard"}))


class KrakenClient:
    """Read-only Kraken client (POST to allowlisted private methods). Records calls without secrets."""

    def __init__(self, verbose: bool = False, max_counter: float = 15.0, decay: float = 0.33):
        self.verbose, self.max_counter, self.decay = verbose, max_counter, decay
        self.calls: dict[str, dict] = {}
        self._creds: tuple[str, str] | None = None
        self._counter, self._t = 0.0, time.monotonic()
        self._last_nonce = 0

    def _credentials(self) -> tuple[str, str]:
        if self._creds is None:
            self._creds = tuple(load_secret(n) for n in KRAKEN_SECRET_NAMES)  # type: ignore[assignment]
        return self._creds  # type: ignore[return-value]

    def _nonce(self) -> int:
        n = max(int(time.time() * 1000), self._last_nonce + 1)
        self._last_nonce = n
        return n

    def _wait(self, cost: int) -> None:
        now = time.monotonic()
        self._counter = max(0.0, self._counter - (now - self._t) * self.decay)
        self._t = now
        if self._counter + cost > self.max_counter:
            time.sleep((self._counter + cost - self.max_counter) / self.decay + 0.2)
            self._counter = max(0.0, self.max_counter - cost)
            self._t = time.monotonic()
        self._counter += cost

    def private(self, method: str, params: dict | None = None, name: str | None = None, as_json: bool = False):
        path = check_method(method)            # raises for anything not read-only — nothing is sent
        key, secret = self._credentials()
        bad = secret_problem(key, secret)
        if bad:                                  # don't send requests that can only fail
            r = {"error": [f"ELOCAL:{bad}"]}
            if name:
                self.calls[name] = {"method": method, "params": params, "response": r}
            return r
        r = None
        for attempt in range(4):
            self._wait(COST.get(method, 1))
            nonce = self._nonce()
            body = dict(params or {}, nonce=nonce)
            if as_json:
                data = json.dumps(body, separators=(",", ":"))
                ctype = "application/json"
            else:
                data = encode_form(body)
                ctype = "application/x-www-form-urlencoded; charset=utf-8"
            req = urllib.request.Request(f"{API}{path}", data=data.encode(), method="POST", headers={
                "API-Key": key, "API-Sign": sign(path, data, nonce, secret), "Content-Type": ctype,
                "Accept": "application/json", "User-Agent": "assetsboard"})
            r = _http(req)
            errs = " ".join(map(str, (r or {}).get("error") or [])) if isinstance(r, dict) else ""
            if "Rate limit" in errs or "Throttled" in errs:
                time.sleep(15 * (attempt + 1)); self._counter = self.max_counter; continue
            if "Invalid nonce" in errs:
                time.sleep(1.0); continue
            break
        if name:
            self.calls[name] = {"method": method, "params": params, "response": r}
        if self.verbose:
            print(f"  [{'ERR ' + err_text(r) if is_error(r) else 'OK '}] kraken {name or method}"[:110], file=sys.stderr)
        return r

    def errors(self) -> dict[str, str]:
        return {n: err_text(c["response"]) for n, c in self.calls.items() if is_error(c["response"])}


def result(r):
    return None if is_error(r) else r.get("result")


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------- asset naming
LEGACY = {"XXBT": "BTC", "XBT": "BTC", "XXDG": "DOGE", "XDG": "DOGE", "XETH": "ETH", "XETC": "ETC", "XLTC": "LTC",
          "XXLM": "XLM", "XXMR": "XMR", "XXRP": "XRP", "XZEC": "ZEC", "XMLN": "MLN", "XREP": "REP", "XXTZ": "XTZ",
          "XICN": "ICN", "XNMC": "NMC", "XXVN": "XVN", "ETH2": "ETH",
          "ZEUR": "EUR", "ZUSD": "USD", "ZGBP": "GBP", "ZCAD": "CAD", "ZJPY": "JPY", "ZCHF": "CHF", "ZAUD": "AUD"}
SUFFIX_KIND = {"": "spot", "S": "staked", "M": "opt-in rewards", "B": "earn (yield-bearing)", "F": "auto earn (Kraken Rewards)",
               "P": "parachain", "HOLD": "hold", "T": "xStock"}
SPOT_KINDS = {"spot", "xStock", "hold"}     # everything else (staked, opt-in rewards, earn …) is staking/Earn


def is_earn(kind: str) -> bool:
    return kind not in SPOT_KINDS


FIAT = {"EUR", "USD", "GBP", "CAD", "JPY", "CHF", "AUD"}
STABLE = {"USDT", "USDC", "DAI", "PYUSD", "USD1", "USDG", "USDE", "TUSD"}


def norm(asset: str) -> tuple[str, str]:
    """Kraken asset code -> (coin, balance kind). 'XXBT' -> ('BTC','spot'); 'DOT.S' -> ('DOT','staked'); 'ETH2.S' -> ('ETH','staked')."""
    raw = str(asset or "").strip()
    base_raw, _, suf = raw.partition(".")
    base, suf = base_raw.upper(), suf.upper()
    if len(base_raw) > 1 and base_raw.endswith("x") and base_raw[:-1].isupper():
        coin = base_raw                        # xStocks keep Kraken's spelling: NVDAx, GOOGLx
    else:
        coin = LEGACY.get(base, base)
        if coin.endswith("2") and coin[:-1] == "ETH":
            coin = "ETH"
        elif suf and re.fullmatch(r"[A-Z]{2,}\d\d", coin):   # staking variants: SOL03.S -> SOL
            coin = coin[:-2]
    return coin, SUFFIX_KIND.get(suf, suf.lower() or "spot")


def coin(asset: str) -> str:
    return norm(asset)[0]


# ---------------------------------------------------------------- prices
class Market:
    """Live prices from public AssetPairs + Ticker (all pairs, 2 calls), expressed in USDT."""

    QUOTES = ("USDT", "USD", "EUR", "USDC", "BTC", "ETH")

    def __init__(self, pairs: dict | None = None, ticker: dict | None = None):
        self.last: dict[tuple[str, str], float] = {}
        self.altname: dict[tuple[str, str], str] = {}
        self.aclass: dict[tuple[str, str], str] = {}
        for k, p in (pairs or {}).items():
            b, q = coin(p.get("base")), coin(p.get("quote"))
            if k.endswith(".d"):
                continue
            alt = p.get("altname") or k
            if (b, q) in self.altname and k != alt:
                continue                       # keep the primary pair (NVDAxUSD), not its SPV twin (NVDASPVUSD)
            self.altname[(b, q)] = alt
            if p.get("aclass_base") and p.get("aclass_base") != "currency":
                self.aclass[(b, q)] = p["aclass_base"]
            t = (ticker or {}).get(k)
            try:
                if t and float(t["c"][0]) > 0:
                    self.last[(b, q)] = float(t["c"][0])
            except (KeyError, TypeError, ValueError, IndexError):
                pass

    @classmethod
    def fetch(cls) -> "Market":
        ap, tk = result(public("AssetPairs")) or {}, result(public("Ticker")) or {}
        # tokenized assets (xStocks) are only listed with aclass_base / asset_class=tokenized_asset
        ap_t = result(public("AssetPairs", {"aclass_base": "tokenized_asset"})) or {}
        tk_t = result(public("Ticker", {"asset_class": "tokenized_asset"})) or {}
        return cls({**ap_t, **ap}, {**tk_t, **tk})

    def price(self, c: str, _depth: int = 0) -> float | None:
        c = coin(c)
        if c == "USDT":
            return 1.0
        if _depth > 2:
            return None
        for q in self.QUOTES:
            if q == c:
                continue
            p = self.last.get((c, q))
            if p:
                qp = self.price(q, _depth + 1)
                if qp:
                    return p * qp
            p = self.last.get((q, c))
            if p:
                qp = self.price(q, _depth + 1)
                if qp:
                    return qp / p
        return 1.0 if c in STABLE or c == "USD" else None


class HistPrices:
    """Daily (720 d) / weekly (older) closes from public OHLC, cached in snapshots/kraken_price_cache.json."""

    def __init__(self, path: Path, market: Market, offline: bool = False):
        self.path, self.m, self.offline = path, market, offline
        self.cache: dict = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        self.misses: list[str] = []
        self._refreshed: set[str] = set()

    def _series(self, b: str, q: str, interval: int, day: int | None = None) -> dict[str, float]:
        key = f"{b}/{q}@{interval}"
        cached = self.cache.get(key)
        stale = (cached is not None and day is not None and key not in self._refreshed
                 and (not cached or max(int(d) for d in cached) < day - 1))
        if (key not in self.cache or stale) and not self.offline:
            self._refreshed.add(key)
            alt = self.m.altname.get((b, q))
            ser = {}
            if alt:
                prm = {"pair": alt, "interval": interval}
                if (b, q) in self.m.aclass:
                    prm["asset_class"] = self.m.aclass[(b, q)]
                r = result(public("OHLC", prm)) or {}
                for k, rows in r.items():
                    if k == "last" or not isinstance(rows, list):
                        continue
                    for row in rows:
                        try:
                            ser[str(int(row[0]) // 86400)] = float(row[4])
                        except (TypeError, ValueError, IndexError):
                            pass
                time.sleep(1.1)   # public limit ≈ 1/s
            self.cache[key] = ser
        return self.cache.get(key) or {}

    def _close(self, b: str, q: str, day: int) -> float | None:
        for interval in (1440, 10080):
            ser = self._series(b, q, interval, day)
            if not ser:
                continue
            days = sorted(int(d) for d in ser)
            if days[0] > day + (7 if interval == 10080 else 1):
                continue   # series starts after this date → try the weekly series
            prev = [d for d in days if d <= day]
            if prev:
                return ser[str(prev[-1])]
        return None

    def get(self, c: str, ms: int, _depth: int = 0) -> float | None:
        c = coin(c)
        if c == "USDT":
            return 1.0
        day = int(ms // 1000 // 86400)
        if _depth <= 2:
            for q in ("USDT", "USD", "EUR"):
                if q == c:
                    continue
                if (c, q) in self.m.altname:
                    p = self._close(c, q, day)
                    qp = self.get(q, ms, _depth + 1) if p else None
                    if p and qp:
                        return p * qp
                if (q, c) in self.m.altname:
                    p = self._close(q, c, day)
                    qp = self.get(q, ms, _depth + 1) if p else None
                    if p and qp:
                        return qp / p
        if c in STABLE or c == "USD":
            return 1.0
        if _depth == 0:
            self.misses.append(f"{c}|{ms_to_oslo(ms)[:10]}")
            return self.m.price(c)   # fallback: today's price (flagged)
        return None

    def save(self):
        self.path.parent.mkdir(exist_ok=True)
        self.path.write_text(json.dumps(self.cache, separators=(",", ":"), sort_keys=True), encoding="utf-8")


# ---------------------------------------------------------------- snapshot
def build_snapshot(kc: KrakenClient, t: dt.datetime, market: Market | None = None) -> dict:
    m = market or Market.fetch()
    bx = kc.private("BalanceEx", name="balance_ex")
    bal = result(bx)
    if bal is None:   # older keys / accounts: plain Balance
        b2 = result(kc.private("Balance", name="balance"))
        bal = {k: {"balance": v} for k, v in (b2 or {}).items()} if b2 is not None else None
    alloc = kc.private("Earn/Allocations", {"converted_asset": "USD", "hide_zero_allocations": True}, name="earn_allocations", as_json=True)
    oo = kc.private("OpenOrders", name="open_orders")
    tb = result(kc.private("TradeBalance", {"asset": "ZUSD"}, name="trade_balance")) or {}
    hold = []
    for a, x in sorted((bal or {}).items()):
        try:
            q = float((x or {}).get("balance") or 0)
        except (TypeError, ValueError):
            q = 0.0
        if abs(q) < 1e-12:
            continue
        c, kind = norm(a)
        p = m.price(c)
        hold.append({"asset": a, "coin": c, "kind": kind, "qty": q, "hold_trade": (x or {}).get("hold_trade"),
                     "price": p, "value": q * p if p is not None else None})
    ar = result(alloc) or {}
    items = [{"strategy_id": i.get("strategy_id"), "coin": coin(i.get("native_asset")), "native_asset": i.get("native_asset"),
              "native": (i.get("amount_allocated") or {}).get("total", {}).get("native"),
              "usd": (i.get("amount_allocated") or {}).get("total", {}).get("converted"),
              "rewarded_usd": (i.get("total_rewarded") or {}).get("converted")} for i in ar.get("items") or []]
    # Earn allocations normally show up in Balance (same coin or a .S/.B/.F variant), but some (e.g. xStocks in Earn)
    # do not: add only the part of each allocation that exceeds what Balance already holds for that coin.
    have = defaultdict(float)
    for h in hold:
        have[h["coin"]] += h["qty"]
    alloc_q = defaultdict(float)
    for i in items:
        alloc_q[i["coin"]] += _f(i["native"])
    for c, q in sorted(alloc_q.items()):
        extra = q - have.get(c, 0.0)
        if extra > max(1e-12, 1e-6 * q):
            p = m.price(c)
            hold.append({"asset": f"{c} (Earn)", "coin": c, "kind": "earn allocation (not in Balance)", "qty": extra,
                         "hold_trade": None, "price": p, "value": extra * p if p is not None else None})
    total = sum(h["value"] or 0 for h in hold)
    earn_v = sum(h["value"] or 0 for h in hold if is_earn(h["kind"]))
    return {"exchange": "kraken", "captured_oslo": t.isoformat(timespec="seconds"), "balance_ok": bal is not None,
            "holdings": hold, "unpriced": [h["asset"] for h in hold if h["price"] is None],
            "earn_allocations": {"ok": not is_error(alloc), "converted_asset": ar.get("converted_asset"),
                                 "total_allocated": ar.get("total_allocated"), "total_rewarded": ar.get("total_rewarded"), "items": items},
            "open_orders": len(((result(oo) or {}).get("open") or {})),
            "trade_balance_usd": {"eb": _f(tb.get("eb")) if tb else None, "e": _f(tb.get("e")) if tb else None},
            "value": {"total": total, "spot": total - earn_v, "earn": earn_v}, "errors": kc.errors()}


def write_snapshot(directory: Path, doc: dict, t: dt.datetime) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / f"kraken_snapshot_{stamp(t)}.json"
    p.write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
    return p


def latest_snapshot(roots: list[Path]) -> tuple[dict | None, Path | None]:
    files = [p for r in roots if r.exists() for p in list(r.glob("kraken_snapshot_*.json")) + list(r.glob("**/kraken_snapshot_*.json"))
             if not p.name.startswith("._")]
    if not files:
        return None, None
    p = max(files, key=lambda p: p.name)
    return json.loads(p.read_text(encoding="utf-8")), p


# ---------------------------------------------------------------- archive (incremental, full history)
ARCH = [
    dict(name="kraken_ledgers", method="Ledgers", result_key="ledger", time_field="time", key=("_id",), time=("_ms",)),
    dict(name="kraken_trades", method="TradesHistory", result_key="trades", time_field="time", key=("_id",), time=("_ms",)),
    dict(name="kraken_closed_orders", method="ClosedOrders", result_key="closed", time_field="closetm", key=("_id",), time=("_ms",)),
]
OVERLAP_S = 120


def fetch_paged(kc: KrakenClient, src: dict, start_s: float | None, end_s: float, max_calls: int = 2000) -> tuple[list, list, int]:
    """All rows with start < time ≤ end (Kraken: start exclusive), newest first, paging with ofs (50 per call)."""
    rows, errs, calls, ofs = [], [], 0, 0
    while calls < max_calls:
        params = {"end": f"{end_s:.4f}", "ofs": ofs}
        if start_s:
            params["start"] = f"{start_s:.4f}"
        r = kc.private(src["method"], params)
        calls += 1
        if is_error(r):
            errs.append({"code": "KRAKEN", "msg": err_text(r), "ofs": ofs})
            break
        res = r["result"] or {}
        page = res.get(src["result_key"]) or {}
        count = int(res.get("count") or 0)
        for k, v in page.items():
            try:
                ms = int(float(v.get(src["time_field"]) or v.get("opentm") or 0) * 1000)
            except (TypeError, ValueError):
                ms = 0
            rows.append({**v, "_id": k, "_ms": ms})
        ofs += len(page)
        if not page or ofs >= count:
            break
    return rows, errs, calls


def archive_run(kc: KrakenClient, root: Path, since: dt.date | None = None, progress: bool = True, on_step=None) -> dict:
    """Incremental archive into root (archive/kraken/YYYY-MM/<source>.jsonl), state in root/state.json."""
    from .archive import Store, _load_state
    t0 = time.time()
    root.mkdir(parents=True, exist_ok=True)
    state_path = root / "state.json"
    state = _load_state(state_path)
    store = Store(root)
    t = now_oslo()
    end_s = time.time()
    rep: dict = {"captured_oslo": t.isoformat(timespec="seconds"), "root": str(root), "sources": {}, "errors": {}, "exchange": "kraken"}
    market = Market.fetch()
    total_steps = len(ARCH) + 1
    for i, src in enumerate(ARCH, 1):
        st = state["sources"].setdefault(src["name"], {})
        if since:
            start_s = dt.datetime.combine(since, dt.time(), OSLO).timestamp()
        elif st.get("last_record_ms"):
            start_s = int(st["last_record_ms"]) / 1000 - OVERLAP_S
        else:
            start_s = None   # first run: the whole history
        rows, errs, calls = fetch_paged(kc, src, start_s, end_s)
        added, dup, mx = store.add(src, rows)
        if errs:
            rep["errors"][src["name"]] = errs
            st["last_errors"] = errs
        else:
            st.pop("last_errors", None)
            if start_s is None:
                st["full_history"] = True
            # only advance the cursor when the whole range was fetched
            if mx:
                st["last_record_ms"] = max(int(st.get("last_record_ms") or 0), mx)
        st["last_run_oslo"] = rep["captured_oslo"]
        store._load(src["name"], src)
        e = store.earliest.get(src["name"])
        rep["sources"][src["name"]] = {"fetched": len(rows), "added": added, "duplicates": dup, "calls": calls,
                                      "from": ms_to_oslo(int(start_s * 1000))[:10] if start_s else "全部",
                                      "total": store.count[src["name"]], "earliest": ms_to_oslo(e)[:10] if e else None}
        if progress:
            print(f"  {src['name']:28} +{added:<6} dup {dup:<6} calls {calls:<4} {time.time() - t0:6.0f}s"
                  + (f"  ERR {errs[0]['msg']}" if errs else ""), file=sys.stderr, flush=True)
        if on_step:
            on_step(src["name"], i, total_steps)
    snap = build_snapshot(kc, t, market)
    if snap["balance_ok"]:
        p = write_snapshot(root / t.strftime("%Y-%m") / "snapshots", snap, t)
        rep["snapshot"] = str(p.relative_to(root))
    else:
        rep["errors"]["snapshot"] = [{"code": "BALANCE", "msg": snap["errors"].get("balance_ex") or snap["errors"].get("balance", "")}]
    if on_step:
        on_step("snapshot", total_steps, total_steps)
    state["runs"] = (state.get("runs") or [])[-49:] + [{"at": rep["captured_oslo"], "since": since.isoformat() if since else None,
                                                         "added": {k: v["added"] for k, v in rep["sources"].items()},
                                                         "errors": sorted(rep["errors"])}]
    state.setdefault("first_run_oslo", rep["captured_oslo"])
    state_path.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")
    return rep


# ---------------------------------------------------------------- analysis
def load_archive(root: Path) -> dict[str, list]:
    out: dict[str, dict] = defaultdict(dict)
    for f in sorted(root.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]/*.jsonl")):
        if f.name.startswith("._"):
            continue
        with f.open(encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    r = json.loads(line)
                    out[f.stem][r.get("_id") or line] = r
    return {k: sorted(v.values(), key=lambda r: r.get("_ms") or 0) for k, v in out.items()}


FUTURES_SUBTYPES = {"spottofutures", "spotfromfutures"}
REWARD_TYPES = {"staking", "reward", "dividend", "credit"}


def classify_ledger(rows: list[dict]) -> list[dict]:
    """Tag each ledger row: external_deposit / external_withdrawal / staking_move / futures_out / futures_in /
    reward / trade / fee_only / other. Staking moves: suffixed assets, staking/earn subtypes, or a base-asset
    deposit/withdrawal that mirrors an opposite-sign suffixed row of the same coin and size within 40 days."""
    suffixed = [r for r in rows if "." in str(r.get("asset"))]
    used: set = set()
    out = []
    for r in rows:
        typ, sub = str(r.get("type") or ""), str(r.get("subtype") or "").lower()
        a = str(r.get("asset") or ""); amt = _f(r.get("amount"))
        tag = "other"
        if typ in ("deposit", "withdrawal"):
            if "." in a:
                tag = "staking_move"
            else:
                c = coin(a); mirror = None
                for s in suffixed:
                    if id(s) in used or coin(s.get("asset")) != c:
                        continue
                    sa = _f(s.get("amount"))
                    if sa * amt < 0 and abs(abs(sa) - abs(amt)) <= max(1e-8, 0.002 * abs(amt)) \
                            and abs((s.get("_ms") or 0) - (r.get("_ms") or 0)) <= 40 * DAY:
                        mirror = s; break
                if mirror is not None:
                    used.add(id(mirror)); tag = "staking_move"
                else:
                    tag = "external_deposit" if typ == "deposit" else "external_withdrawal"
        elif typ == "transfer":
            tag = ("futures_out" if amt < 0 else "futures_in") if sub in FUTURES_SUBTYPES else \
                  ("staking_move" if sub or "." in a else ("reward" if amt > 0 else "other"))
        elif "earn" in typ and typ != "earn":     # hybridearndeposit / hybridearnwithdrawal: spot <-> Earn, internal
            tag = "staking_move"
        elif typ == "earn":
            tag = "reward" if sub in ("reward", "") and amt > 0 else "staking_move"
        elif typ in REWARD_TYPES:
            tag = "reward" if amt > 0 else "other"
        elif typ in ("trade", "spend", "receive", "conversion", "sale", "margin", "rollover", "settled"):
            tag = "trade"
        out.append({**r, "_tag": tag})
    return out


def analyze(project: Path, archive_root: Path | None = None, offline: bool = False) -> dict:
    root = archive_root or project / "archive" / "kraken"
    snap, snap_path = latest_snapshot([root, project / "snapshots"])
    configured = available()
    if snap is None:
        return {"present": False, "configured": configured,
                "reason": "尚無 Kraken 快照" + ("（執行 archive 或 refresh）" if configured else "；" + no_creds_msg())}
    market = Market() if offline else Market.fetch()
    live = bool(market.last)
    if not live:   # offline: rebuild a minimal market from the snapshot's own prices
        market.last = {(h["coin"], "USDT"): h["price"] for h in snap.get("holdings") or [] if h.get("price")}
    hp = HistPrices(project / "snapshots" / "kraken_price_cache.json", market, offline=offline or not live)
    arch = load_archive(root) if root.exists() else {}
    hold = []
    now_ms = int(time.time() * 1000)
    for h in snap.get("holdings") or []:
        p = market.price(h["coin"]) if live else h.get("price")
        src = "ticker" if p is not None else None
        if p is None and h["qty"] > 0:       # no live trade (e.g. an inactive xStock): last daily close from OHLC
            n = len(hp.misses)
            p = hp.get(h["coin"], now_ms)
            del hp.misses[n:]
            src = "last daily close" if p is not None else None
        hold.append({**h, "price": p, "price_source": src, "value": h["qty"] * p if p is not None else None})
    led = classify_ledger(arch.get("kraken_ledgers", []))
    flows, fut_net, rewards, fees, by_tag = [], 0.0, 0.0, 0.0, defaultdict(int)
    reward_by_coin = defaultdict(float)
    for r in led:
        by_tag[r["_tag"]] += 1
        ms = int(r.get("_ms") or 0); c = coin(r.get("asset")); amt = _f(r.get("amount")); fee = _f(r.get("fee"))
        if fee:
            fp = hp.get(c, ms)
            fees += fee * (fp or 0)
        if r["_tag"] in ("external_deposit", "external_withdrawal"):
            p = hp.get(c, ms)
            flows.append({"time": ms_to_oslo(ms), "ms": ms, "kind": "deposit" if amt > 0 else "withdrawal", "coin": c,
                          "asset": r.get("asset"), "qty": amt, "fee": fee, "usd": amt * p if p is not None else None,
                          "fiat": c in FIAT, "refid": r.get("refid"), "id": r.get("_id")})
        elif r["_tag"] in ("futures_out", "futures_in"):
            p = hp.get(c, ms)
            fut_net += -amt * (p or 0)
        elif r["_tag"] == "reward":
            p = hp.get(c, ms)
            rewards += amt * (p or 0); reward_by_coin[c] += amt * (p or 0)
    dep = sum(f["usd"] or 0 for f in flows if f["kind"] == "deposit")
    wd = -sum(f["usd"] or 0 for f in flows if f["kind"] == "withdrawal")
    fiat_dep = sum(f["usd"] or 0 for f in flows if f["kind"] == "deposit" and f["fiat"])
    fiat_wd = -sum(f["usd"] or 0 for f in flows if f["kind"] == "withdrawal" and f["fiat"])
    net = dep - wd
    spot_v = sum(h["value"] or 0 for h in hold if not is_earn(h["kind"]))
    earn_v = sum(h["value"] or 0 for h in hold if is_earn(h["kind"]))
    fut_cost = max(0.0, fut_net)
    total = spot_v + earn_v + fut_cost
    trades = arch.get("kraken_trades", [])
    tfee = defaultdict(float)
    for x in trades:
        tfee[str(x.get("pair"))] += _f(x.get("fee"))
    state_p = root / "state.json"
    state = json.loads(state_p.read_text(encoding="utf-8")) if state_p.exists() else {}
    full = bool((state.get("sources") or {}).get("kraken_ledgers", {}).get("full_history"))
    alloc = snap.get("earn_allocations") or {}
    alloc_usd = _f(alloc.get("total_allocated")) if alloc.get("ok") else None
    try:
        costs = trading_costs(arch.get("kraken_ledgers", []), market, hp, project / "snapshots" / "kraken_trade_refs.json",
                              offline=offline or not live)
    except Exception as e:  # noqa: BLE001 - informational
        costs = {"error": f"{type(e).__name__}: {e}"[:200], "items": []}
    hp.save()
    first = min((r.get("_ms") or 0 for r in led), default=0)
    return {
        "present": True, "configured": configured,
        "snapshot_time": snap.get("captured_oslo"), "snapshot_file": str(snap_path.relative_to(project)) if snap_path else None,
        "prices": "live tickers" if live else "snapshot prices",
        "total": total, "spot": spot_v, "earn": earn_v, "futures_at_cost": fut_cost,
        "holdings": sorted(hold, key=lambda h: -(h["value"] or 0)),
        "unpriced": [h["asset"] for h in hold if h["price"] is None],
        "deposits": dep, "withdrawals": wd, "net_inflow": net, "fiat_deposits": fiat_dep, "fiat_withdrawals": fiat_wd,
        "pnl": (total - net) if flows else None, "pnl_pct": ((total - net) / net * 100) if flows and net > 0 else None,
        "history_complete": full, "ledger_from": ms_to_oslo(first)[:10] if first else None,
        "flows": [{k: v for k, v in f.items() if k != "ms"} for f in flows],
        "rewards_usdt": rewards, "rewards_by_coin": dict(sorted(reward_by_coin.items(), key=lambda kv: -kv[1])),
        "fees_usdt": fees, "ledger_tags": dict(by_tag), "ledger_rows": len(led),
        "trades": {"count": len(trades), "closed_orders": len(arch.get("kraken_closed_orders", []))},
        "earn_allocations": {"total_allocated_usd": alloc_usd, "total_rewarded_usd": _f(alloc.get("total_rewarded")) if alloc.get("ok") else None,
                             "items": alloc.get("items") or [], "ok": alloc.get("ok")},
        "earn_check": (None if alloc_usd is None else {"balances_usdt": earn_v, "allocations_usd": alloc_usd,
                       "diff": alloc_usd - earn_v}),
        "open_orders": snap.get("open_orders"),
        "trading_costs": costs,
        "trade_balance_usd": snap.get("trade_balance_usd"),
        "errors": {**(snap.get("errors") or {}), **{k: v.get("last_errors") for k, v in (state.get("sources") or {}).items() if v.get("last_errors")}},
        "price_misses": sorted(set(hp.misses))[:50],
        "notes": [
            "Kraken 保留完整帳本：首次封存會讀取全部歷史（ofs 分頁、遵守速率限制），之後只抓新紀錄。"
            + ("已完整讀取。" if full else "⚠ 尚未完成一次完整讀取（下次 archive 會從頭補齊）。"),
            "淨入金＝帳本中的外部充值（含 EUR 銀行入金）− 提領，以當日價格換成 USDT；盈虧＝總資產 − 淨入金。",
            "質押／Earn 餘額（.S／.M／.B／.F）已包含在 Balance 裡並計入總資產；現貨↔質押的移動視為內部，不算入金或出金。",
            *([f"轉到 Kraken Futures 的淨額 {fut_cost:,.2f} USDT 以成本計入（現貨 API Key 看不到合約帳戶）。"] if fut_cost > 0.005 else []),
            *([f"⚠ {len(set(hp.misses))} 筆歷史價格查不到，以目前價格估算。"] if hp.misses else []),
        ],
    }


# ---------------------------------------------------------------- trading cost (instant Buy/Sell spread)
QUOTE_SIDE = ("USD", "USDT", "USDC", "EUR")
REF_WINDOW_S = 300      # reference = VWAP of public trades within ±5 min
REF_WIDE_S = 900        # fallback: nearest public trades within ±15 min


def instant_trades(rows: list[dict]) -> list[dict]:
    """Pair ledger spend/receive rows by refid into conversions (Kraken app Buy/Sell/Convert); dust sweeps excluded."""
    g: dict[str, list] = defaultdict(list)
    for r in rows:
        if r.get("type") in ("spend", "receive") and str(r.get("subtype") or "") != "dustsweeping":
            g[r.get("refid")].append(r)
    out = []
    for ref, rs in g.items():
        if len(rs) != 2:
            continue
        a, b = sorted(rs, key=lambda r: _f(r.get("amount")))      # a = spent (negative), b = received
        ca, cb = coin(a.get("asset")), coin(b.get("asset"))
        if cb in QUOTE_SIDE and ca not in QUOTE_SIDE:             # sell: spend asset, receive quote
            side, asset, qty, quote, q_amt = "sell", ca, -_f(a.get("amount")), cb, _f(b.get("amount"))
        elif ca in QUOTE_SIDE and cb not in QUOTE_SIDE:           # buy: spend quote, receive asset
            side, asset, qty, quote, q_amt = "buy", cb, _f(b.get("amount")), ca, -_f(a.get("amount"))
        else:
            continue
        fee_q = _f(a.get("fee")) if coin(a.get("asset")) == quote else _f(b.get("fee")) if coin(b.get("asset")) == quote else 0.0
        fee_asset = _f(b.get("fee")) if side == "buy" else _f(a.get("fee"))
        if qty <= 0 or q_amt <= 0:
            continue
        out.append({"refid": ref, "ms": int(min(a.get("_ms") or 0, b.get("_ms") or 0)), "side": side, "asset": asset,
                    "raw_asset": b.get("asset") if side == "buy" else a.get("asset"), "qty": qty, "quote": quote,
                    "quote_amount": q_amt, "fee_quote": fee_q, "fee_asset": fee_asset, "implied_price": q_amt / qty})
    return sorted(out, key=lambda x: x["ms"])


def _public_trades_around(alt: str, aclass: str | None, t_s: float) -> list[tuple] | None:
    """Public trades (time s, price, volume) in [t−15 min, t+15 min]; None if the API failed."""
    out, since = [], int((t_s - REF_WIDE_S) * 1e9)
    for _ in range(6):
        prm = {"pair": alt, "since": since, "count": 1000}
        if aclass:
            prm["asset_class"] = aclass
        r = public("Trades", prm)
        time.sleep(1.1)                                    # public limit ≈ 1 call/s
        if is_error(r):
            return None
        res = r["result"]
        rows = next((v for k, v in res.items() if k != "last"), [])
        out += [(float(x[2]), float(x[0]), float(x[1])) for x in rows]
        if len(rows) < 1000 or float(rows[-1][2]) > t_s + REF_WIDE_S:
            break
        since = int(res["last"])
    return [x for x in out if abs(x[0] - t_s) <= REF_WIDE_S]


def reference_price(market: "Market", asset: str, quote: str, t_s: float, cache: dict, offline: bool = False) -> dict:
    """Market reference for one instant trade from Kraken's public order-book trades of the same pair."""
    alt = market.altname.get((asset, quote))
    if not alt:
        return {"ok": False, "why": f"Kraken 沒有 {asset}/{quote} 交易對"}
    key = f"{alt}@{int(t_s)}"
    if key in cache:
        return cache[key]
    if offline:
        return {"ok": False, "why": "離線"}
    tr = _public_trades_around(alt, market.aclass.get((asset, quote)), t_s)
    if tr is None:
        return {"ok": False, "why": "公開 Trades 讀取失敗"}     # not cached: retry next time
    near = [x for x in tr if abs(x[0] - t_s) <= REF_WINDOW_S]
    if near:
        v = sum(x[2] for x in near)
        ref = {"ok": True, "pair": alt, "price": sum(x[1] * x[2] for x in near) / v, "method": "VWAP ±5 分",
               "n": len(near), "lo": min(x[1] for x in near), "hi": max(x[1] for x in near),
               "gap_s": min(abs(x[0] - t_s) for x in near)}
    elif tr:
        before = [x for x in tr if x[0] <= t_s]
        after = [x for x in tr if x[0] > t_s]
        pts = ([before[-1]] if before else []) + ([after[0]] if after else [])
        ref = {"ok": True, "pair": alt, "price": sum(x[1] for x in pts) / len(pts), "method": "前後最近成交 ±15 分",
               "n": len(pts), "lo": min(x[1] for x in pts), "hi": max(x[1] for x in pts),
               "gap_s": min(abs(x[0] - t_s) for x in pts)}
    else:
        ref = {"ok": False, "pair": alt, "why": "前後 15 分鐘內沒有公開成交"}
    cache[key] = ref
    return ref


YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/"


def underlying_bars(sym: str, t0: float, t1: float, cache: dict, offline: bool = False) -> list | None:
    """Hourly bars incl. US pre/after-market for the underlying stock (Yahoo Finance public chart API, ≤ 730 days).
    Returns [[start_s, open, close, high, low], ...] or None."""
    key = f"yahoo:{sym}:{int(t0 // 86400)}:{int(t1 // 86400)}"
    if key in cache:
        return cache[key]
    if offline:
        return None
    url = f"{YAHOO}{urllib.parse.quote(sym)}?interval=60m&period1={int(t0)}&period2={int(t1)}&includePrePost=true"
    r = _http(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (assetsboard)"}))
    try:
        res = r["chart"]["result"][0]
        q = res["indicators"]["quote"][0]
        bars = [[t, q["open"][i], q["close"][i], q["high"][i], q["low"][i]] for i, t in enumerate(res.get("timestamp") or [])
                if q["open"][i] is not None and q["close"][i] is not None]
    except (KeyError, IndexError, TypeError):
        return None                                        # not cached: retry next time
    cache[key] = bars
    time.sleep(0.5)
    return bars


def underlying_ref(bars: list | None, t_s: float) -> dict:
    if not bars:
        return {"ok": False, "why": "無美股資料"}
    i = max((k for k, b in enumerate(bars) if b[0] <= t_s), default=None)
    if i is not None and t_s - bars[i][0] < 3600 and (i + 1 >= len(bars) or t_s < bars[i + 1][0]):
        b = bars[i]
        frac = (t_s - b[0]) / 3600
        return {"ok": True, "price": b[1] + (b[2] - b[1]) * frac, "method": "美股 1 小時 K 線（開→收按時間內插，含盤前盤後）",
                "lo": b[4], "hi": b[3]}
    last = bars[i][2] if i is not None else None
    nxt = bars[i + 1][1] if i is not None and i + 1 < len(bars) else None
    return {"ok": False, "why": "美股休市（含盤前盤後皆無交易）", "last_close": last, "next_open": nxt}


def trading_costs(rows: list[dict], market: "Market", hp: "HistPrices", cache_path: Path, offline: bool = False) -> dict:
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    trades = instant_trades(rows)
    xs = [t for t in trades if str(t["raw_asset"]).split(".")[0].endswith("x")]
    t0 = min((t["ms"] for t in xs), default=0) / 1000 - 4 * 86400
    t1 = max((t["ms"] for t in xs), default=0) / 1000 + 4 * 86400
    bars = {sym: underlying_bars(sym, t0, t1, cache, offline) for sym in sorted({t["asset"][:-1] for t in xs})}
    items = []
    for t in trades:
        kr = reference_price(market, t["asset"], t["quote"], t["ms"] / 1000, cache, offline)
        is_x = str(t["raw_asset"]).split(".")[0].endswith("x")
        ul = underlying_ref(bars.get(t["asset"][:-1]), t["ms"] / 1000) if is_x else None
        # preference: Kraken VWAP with ≥3 trades in ±5 min → underlying stock bar (market open) → any Kraken trade ±15 min
        strong = kr.get("ok") and kr.get("method", "").startswith("VWAP") and kr.get("n", 0) >= 3
        if strong or (kr.get("ok") and not (ul and ul.get("ok"))):
            ref = {**kr, "source": "Kraken 訂單簿成交"}
        elif ul and ul.get("ok"):
            ref = {**ul, "source": "美股本體（Yahoo）", "pair": kr.get("pair")}
        else:
            ref = {"ok": False, "pair": kr.get("pair"), "why": "；".join(x for x in (kr.get("why"), (ul or {}).get("why")) if x)}
        qp = hp.get(t["quote"], t["ms"]) or 1.0                 # quote → USDT at that time (USD ≈ 1)
        x = {**t, "time": ms_to_oslo(t["ms"]), "pair": ref.get("pair") or f"{t['asset']}/{t['quote']}",
             "xstock": is_x, "ref": ref, "kraken_ref": kr, "underlying_ref": ul,
             "underlying_markup_pct": ((t["implied_price"] / ul["price"] - 1) * 100 * (1 if t["side"] == "buy" else -1)) if ul and ul.get("ok") else None,
             "fee_usdt": (t["fee_quote"] + t["fee_asset"] * t["implied_price"]) * qp}
        if ref.get("ok"):
            fair = t["qty"] * ref["price"]
            x["markup_pct"] = (t["implied_price"] / ref["price"] - 1) * 100 * (1 if t["side"] == "buy" else -1)
            x["markup_usdt"] = ((t["quote_amount"] - fair) if t["side"] == "buy" else (fair - t["quote_amount"])) * qp
            x["ref_range_pct"] = (ref["hi"] - ref["lo"]) / ref["price"] * 100
        items.append(x)
    cache_path.parent.mkdir(exist_ok=True)
    cache_path.write_text(json.dumps(cache, separators=(",", ":"), sort_keys=True), encoding="utf-8")
    priced = [x for x in items if "markup_pct" in x]
    vol = sum(x["quote_amount"] for x in priced)
    return {
        "items": items, "count": len(items), "xstock_count": sum(1 for x in items if x["xstock"]),
        "with_reference": len(priced),
        "xstock_with_reference": sum(1 for x in priced if x["xstock"]),
        "xstock_volume_quote": sum(x["quote_amount"] for x in items if x["xstock"]),
        "xstock_priced_volume_quote": sum(x["quote_amount"] for x in priced if x["xstock"]),
        "xstock_fees_usdt": sum(x["fee_usdt"] for x in items if x["xstock"]),
        "xstock_markup_usdt": sum(x["markup_usdt"] for x in priced if x["xstock"]),
        "xstock_avg_markup_pct": (lambda L: sum(L) / len(L) if L else None)([x["markup_pct"] for x in priced if x["xstock"]]),
        "xstock_median_markup_pct": (lambda L: sorted(L)[len(L) // 2] if L else None)([x["markup_pct"] for x in priced if x["xstock"]]),
        "unpriced_volume_quote": sum(x["quote_amount"] for x in items if "markup_pct" not in x),
        "fees_usdt": sum(x["fee_usdt"] for x in items),
        "markup_usdt": sum(x["markup_usdt"] for x in priced),
        "volume_quote": sum(x["quote_amount"] for x in items),
        "avg_markup_pct": (sum(x["markup_pct"] for x in priced) / len(priced)) if priced else None,
        "vw_markup_pct": (sum(x["markup_usdt"] for x in priced) / vol * 100) if vol else None,
        "method": ("成交價＝帳本中同一 refid 的 spend／receive 配對（花費 USD ÷ 收到數量）。參考價＝Kraken 公開成交紀錄（同一交易對、"
                   "訂單簿成交）在交易時間 ±5 分鐘內至少 3 筆成交的成交量加權均價；否則（xStock）用美股本體（Yahoo Finance 1 小時 K 線、含盤前盤後，開→收按時間內插；"
                   "xStock 1:1 追蹤股價，股息再投入可能造成小幅偏差）；兩者都沒有時用 Kraken ±15 分鐘內前後最近的成交。都沒有就不估算。"
                   "加價＝成交價相對參考價的差（買入：成交價高於參考價為正）。這是「相對市場成交價」的估計，"
                   "包含訂單簿本身的半個買賣價差與這幾分鐘內的價格波動；數量只有 6 位小數，小額交易的成交價有約 ±0.05% 的捨入誤差。"),
    }


# ---------------------------------------------------------------- perms
def perms_report(kc: KrakenClient) -> dict:
    """Try each read method once; nothing that writes is ever attempted (check_method would refuse it)."""
    probes = [("Balance", {}, False), ("BalanceEx", {}, False), ("TradeBalance", {"asset": "ZUSD"}, False),
              ("Ledgers", {"ofs": 0}, False), ("QueryLedgers", {"id": "LXXXXX-XXXXX-XXXXXX"}, False),
              ("TradesHistory", {"ofs": 0}, False), ("ClosedOrders", {"ofs": 0}, False), ("OpenOrders", {}, False),
              ("Earn/Allocations", {"hide_zero_allocations": True}, True)]
    out = {}
    for m, p, js in probes:
        r = kc.private(m, p, name=m, as_json=js)
        e = err_text(r) if is_error(r) else "OK"
        if m == "QueryLedgers" and e.startswith("EGeneral:Invalid arguments"):
            e = "OK（權限可用；測試 ID 不存在）"
        out[m] = e
    refused = []
    for m in ("AddOrder", "Withdraw", "WalletTransfer", "Earn/Allocate"):
        try:
            check_method(m)
        except WriteRefused:
            refused.append(m)
    local = next((v[len("ELOCAL:"):] for v in out.values() if v.startswith("ELOCAL:")), None)
    return {"read_probes": out, "write_refused_locally": refused, "local_problem": local,
            "ok": any(v.startswith("OK") for v in out.values()), "error": None if any(v.startswith("OK") for v in out.values()) else next(iter(out.values()), "")}
