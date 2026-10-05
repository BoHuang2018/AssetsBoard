"""Crypto.com App (retail) via the "Agent API Key (Beta)" — read-only.

Base URL https://wapi.crypto.com (from Crypto.com's official agent skill, github.com/crypto-com/crypto-agent-trading).
Signing: headers Cdc-Api-Key, Cdc-Api-Timestamp (ms), Cdc-Api-Signature = base64(HMAC-SHA256(secret,
timestamp + METHOD + path-without-query + body)).

Safety: only GET on a strict allowlist of read endpoints (with allowlisted query parameters); every other path or
method — trades (quotations/orders), fiat withdrawals, key self-revoke, bank accounts — is refused locally before
anything is sent. There is no public API reference: response shapes follow the official skill and are parsed
defensively; the transaction archive records what it sees so pagination can be confirmed on a real account.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from pathlib import Path

from .client import OSLO, cli_cmd, credential_sources, credentials_available, load_secret, now_oslo, stamp

BASE_URL = "https://wapi.crypto.com"
SECRET_NAMES = ("CRYPTOCOM_API_KEY", "CRYPTOCOM_API_SECRET")
UA = "assetsboard/1.0 (read-only portfolio tracker)"
TICKERS_URL = "https://api.crypto.com/exchange/v1/public/get-tickers"     # public, no key

# path -> allowed query parameters. GET only. Nothing else is ever sent.
ALLOWED_GET = {
    "/v1/crypto-account": frozenset(),
    "/v1/fiat-account": frozenset(),
    "/v1/portfolio": frozenset(),
    "/v1/currency_allocation": frozenset({"currency"}),
    # real API (2026-10-04): cursor = meta.pagination.next.last_id ("<nature>/<id>"), page size = count (server caps at 50)
    "/v1/transactions": frozenset({"last_id", "count"}),
}
KNOWN_WRITES = ("/v1/crypto-purchase/orders", "/v1/crypto-sales/orders", "/v1/crypto-exchange/orders",
                "/v1/crypto-purchase/quotations", "/v1/crypto-sales/quotations", "/v1/crypto-exchange/quotations",
                "/v1/fiat/withdrawal-orders", "/v1/fiat/withdrawals", "/v1/fiat/deposit-info/email", "/v1/api-keys/self-revoke")
AUTH_ERRORS = {"unauthorized", "key_not_active", "api_key_not_found", "invalid_scope", "forbidden", "key_expired", "invalid_signature"}
MAX_PER_MIN = 90          # documented limit is 100 calls/min
MIN_INTERVAL = 0.4
MAX_TX_PAGES = 400        # per run (50 rows each); an unfinished backfill resumes next run
TX_PAGE_SIZE = 50
EXPIRY_DAYS_DEFAULT = 30  # App default key expiry; also auto-expires after 30 days without calls
WARN_DAYS = 7


class WriteRefused(Exception):
    pass


class CdcError(Exception):
    def __init__(self, msg: str, code: str | None = None, status: int | None = None, auth: bool = False):
        super().__init__(msg)
        self.code, self.status, self.auth = code, status, auth


def available() -> bool:
    return credentials_available(SECRET_NAMES)


def sources() -> dict:
    return credential_sources(SECRET_NAMES)


def no_creds_msg() -> str:
    return (f"Crypto.com 未設定憑證（CRYPTOCOM_API_KEY／CRYPTOCOM_API_SECRET），略過。在 Mac 的 ~/assetsboard 執行："
            f"{cli_cmd()} setup-keychain --exchange cryptocom")


def auth_hint(code: str | None = None) -> str:
    return (f"Crypto.com 拒絕了 API Key（{code or 'unauthorized'}）：可能已到期（預設 30 天，或 30 天沒有呼叫會自動失效）、"
            f"被撤銷，或 Key／Secret 存錯。請在 Crypto.com App → 個人檔案 → More → Agent API Key 重新產生，"
            f"再執行 {cli_cmd()} setup-keychain --exchange cryptocom")


def secret_problem(key: str, secret: str) -> str | None:
    if not key or not secret:
        return "API Key 或 Secret 是空的"
    if key == secret:
        return "API Key 與 Secret 相同（可能把同一個值存了兩次）"
    if any(c.isspace() for c in key + secret):
        return "API Key 或 Secret 含有空白字元"
    return None


def sign(secret: str, timestamp: str, method: str, path: str, body: str = "") -> str:
    """Official scheme: base64(HMAC-SHA256(secret, timestamp + METHOD + path(no query) + body))."""
    msg = f"{timestamp}{method.upper()}{path.split('?', 1)[0]}{body}"
    return base64.b64encode(hmac.new(secret.encode(), msg.encode(), hashlib.sha256).digest()).decode()


def check_request(method: str, path: str, params: dict | None = None) -> None:
    """Raise WriteRefused unless this is an allowlisted read. Called before every request."""
    if method.upper() != "GET":
        raise WriteRefused(f"拒絕：只允許 GET（{method} {path}）")
    if "?" in path or path not in ALLOWED_GET:
        raise WriteRefused(f"拒絕：{path} 不在唯讀白名單")
    bad = set(params or {}) - ALLOWED_GET[path]
    if bad:
        raise WriteRefused(f"拒絕：{path} 不接受參數 {sorted(bad)}")


def key_fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:12]


class CdcClient:
    def __init__(self, verbose: bool = False, creds: tuple[str, str] | None = None, opener=None, sleep=time.sleep, clock=time.monotonic):
        self.verbose, self._creds, self._opener, self._sleep, self._clock = verbose, creds, opener, sleep, clock
        self._calls: deque = deque()
        self.calls = 0
        self.sent: list[str] = []

    def creds(self) -> tuple[str, str]:
        if self._creds is None:
            self._creds = (load_secret("CRYPTOCOM_API_KEY"), load_secret("CRYPTOCOM_API_SECRET"))
        return self._creds

    def fingerprint(self) -> str:
        return key_fingerprint(self.creds()[0])

    def _throttle(self):
        now = self._clock()
        while self._calls and now - self._calls[0] > 60:
            self._calls.popleft()
        if len(self._calls) >= MAX_PER_MIN:
            self._sleep(60 - (now - self._calls[0]) + 0.1)
        if self._calls and self._clock() - self._calls[-1] < MIN_INTERVAL:
            self._sleep(MIN_INTERVAL - (self._clock() - self._calls[-1]))
        self._calls.append(self._clock())

    def get(self, path: str, params: dict | None = None) -> dict:
        check_request("GET", path, params)
        key, secret = self.creds()
        bad = secret_problem(key, secret)
        if bad:
            raise CdcError(f"Crypto.com 憑證有問題：{bad}（請重新執行 setup-keychain --exchange cryptocom）", "local", auth=True)
        q = ("?" + urllib.parse.urlencode(params)) if params else ""
        for attempt in (1, 2):
            self._throttle()
            ts = str(int(time.time() * 1000))
            req = urllib.request.Request(BASE_URL + path + q, method="GET", headers={
                "User-Agent": UA, "Accept": "application/json", "Cdc-Api-Key": key, "Cdc-Api-Timestamp": ts,
                "Cdc-Api-Signature": sign(secret, ts, "GET", path)})
            self.calls += 1
            self.sent.append(path)
            status, raw = 0, b""
            try:
                with (self._opener or urllib.request.urlopen)(req, timeout=30) as r:
                    status, raw = getattr(r, "status", 200), r.read()
            except urllib.error.HTTPError as e:
                status, raw = e.code, e.read() or b""
            except Exception as e:  # noqa: BLE001
                raise CdcError(f"連線 Crypto.com 失敗（{type(e).__name__}）", "network") from None
            try:
                data = json.loads(raw.decode() or "{}")
            except ValueError:
                data = {}
            if status == 429 and attempt == 1:
                self._sleep(60)
                continue
            code = str(data.get("error") or data.get("code") or "") if isinstance(data, dict) else ""
            if status in (401, 403) or code.lower() in AUTH_ERRORS:
                raise CdcError(auth_hint(code or str(status)), code or str(status), status, auth=True)
            if status != 200 or not (isinstance(data, dict) and data.get("ok") is True):
                msg = (data.get("error_message") or data.get("message") or "") if isinstance(data, dict) else ""
                raise CdcError(f"{path}：HTTP {status} {code} {msg}".strip(), code or str(status), status)
            if self.verbose:
                print(f"  GET {path}{q} -> {status}")
            return data
        raise CdcError(f"{path}：請求過於頻繁（HTTP 429）", "RATE_LIMITED", 429)


# ---------------------------------------------------------------- parsing helpers
def amt(x) -> float | None:
    """'1.23' | 1.23 | {"amount": "1.23", "currency": ".."} -> float."""
    if isinstance(x, dict):
        x = x.get("amount", x.get("value"))
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def cur_of(x) -> str | None:
    return x.get("currency") if isinstance(x, dict) else None


def tickers() -> dict[str, float]:
    """Crypto.com Exchange public tickers -> {coin: price in USDT} (coin_USDT, else coin_USD ÷ USDT_USD)."""
    try:
        with urllib.request.urlopen(urllib.request.Request(TICKERS_URL, headers={"User-Agent": UA}), timeout=20) as r:
            data = json.loads(r.read().decode())["result"]["data"]
    except Exception:  # noqa: BLE001
        return {}
    px = {d["i"]: float(d["a"]) for d in data if d.get("a") not in (None, "", "0")}
    usdt_usd = px.get("USDT_USD") or 1.0
    out = {"USDT": 1.0, "USD": 1 / usdt_usd}
    for inst, p in px.items():
        b, _, q = inst.partition("_")
        if q == "USDT":
            out[b] = p
    for inst, p in px.items():
        b, _, q = inst.partition("_")
        if q == "USD" and b not in out:
            out[b] = p / usdt_usd
    return out


def fiat_usdt(cur: str | None, px: dict) -> float | None:
    """USDT per 1 unit of a fiat currency (USD/EUR from Crypto.com tickers, others via ECB/Yahoo + USDT)."""
    if not cur:
        return None
    cur = cur.upper()
    if cur in ("USD", "USDT"):
        return px.get(cur) or 1.0
    if cur == "EUR" and px.get("EUR"):
        return px["EUR"]
    try:
        from . import ibkr
        u, _ = ibkr.usd_per(cur)
        return u * px.get("USD", 1.0) if u else None
    except Exception:  # noqa: BLE001
        return None


PRODUCT_KIND = (("earn", "Earn"), ("defi", "DeFi 質押"), ("stak", "Staking"), ("supercharg", "Supercharger"), ("basket", "Crypto Basket"),
                ("exchange", "Exchange"), ("fiat", "法幣錢包"), ("cash", "法幣錢包"), ("wallet", "加密錢包"), ("card", "卡片"),
                ("airdrop", "Airdrop Arena"), ("loan", "Crypto Credit"), ("credit", "Crypto Credit"), ("dual", "雙幣"))
EARN_KINDS = ("Earn", "Staking", "DeFi 質押", "Supercharger", "雙幣")


def product_kind(name: str) -> str:
    n = (name or "").lower()
    for k, lab in PRODUCT_KIND:
        if k in n:
            return lab
    return name or "其他"


# ---------------------------------------------------------------- snapshot
def build_snapshot(c: CdcClient, t: dt.datetime, max_alloc: int = 80) -> dict:
    """Read balances; raises CdcError(auth=True) when the key is rejected.

    Real response shapes (2026-10-04):
      /v1/portfolio        {price_native: total, coins: [{id, amount{qty}, price_native{VALUE}}], products: [{name, price_native{VALUE}}]}
                           coins = every coin held anywhere in the App (wallet + Earn + staking …), qty = total.
      /v1/crypto-account   account.wallets[] (~2000 entries, mostly zero): balance / available / native_balance = wallet part only.
      /v1/currency_allocation?currency=X  {crypto_earn|staking|defi_staking|…: {amount}} = amounts OUTSIDE the wallet balance.
      /v1/fiat-account     account.balances[] {currency, amount{}}.
    """
    errors: dict[str, str] = {}

    def safe(path, params=None):
        try:
            return c.get(path, params)
        except CdcError as e:
            if e.auth:
                raise
            errors[path + (f"?{params}" if params else "")] = str(e)[:200]
            return None

    ca = safe("/v1/crypto-account") or {}
    fa = safe("/v1/fiat-account") or {}
    pf = safe("/v1/portfolio") or {}
    wallets = ((ca.get("account") or {}).get("wallets")) or []
    balances = ((fa.get("account") or {}).get("balances")) or []
    px = tickers()
    native = cur_of(pf.get("price_native")) or ((ca.get("account") or {}).get("native_currency"))
    k_native = fiat_usdt(native, px) if native else None
    wal = {}
    for w in wallets:
        coin = (w.get("currency") or "").upper()
        if coin:
            wal[coin] = {"balance": amt(w.get("balance")) or 0.0, "available": amt(w.get("available")),
                         "native": amt(w.get("native_balance")) or 0.0}
    holdings, seen = [], set()
    for k in pf.get("coins") or []:
        coin = (k.get("id") or cur_of(k.get("amount")) or "").upper()
        q = amt(k.get("amount"))
        if not coin or not q:
            continue
        seen.add(coin)
        w = wal.get(coin) or {}
        holdings.append({"coin": coin, "name": k.get("name"), "qty_total": q, "balance": w.get("balance", 0.0),
                         "available": w.get("available") if w.get("available") is not None else w.get("balance", 0.0),
                         "value_native": amt(k.get("price_native")), "allocation": {}, "alloc_in_balance": False, "source": "portfolio"})
    dust = 0
    for coin, w in wal.items():          # wallets the portfolio did not list (normally none); dust (native value 0) is skipped
        if coin in seen or w["balance"] <= 0:
            continue
        if w["native"] <= 0 and not ((px.get(coin) or 0) * w["balance"] >= 0.01):
            dust += 1
            continue
        holdings.append({"coin": coin, "name": None, "qty_total": None, "balance": w["balance"],
                         "available": w["available"] if w["available"] is not None else w["balance"],
                         "value_native": w["native"] or None, "allocation": {}, "alloc_in_balance": False, "source": "wallet"})
    for n, h in enumerate(holdings):
        if n >= max_alloc:
            errors["currency_allocation"] = f"超過 {max_alloc} 個幣種，其餘未查詢"
            break
        r = safe("/v1/currency_allocation", {"currency": h["coin"]})
        if r:
            h["allocation"] = {k: amt(v) for k, v in r.items() if k != "ok" and amt(v)}
    for h in holdings:
        alloc = sum(v for v in h["allocation"].values() if v)
        if h["qty_total"] is None:
            h["qty_total"] = h["balance"] + alloc
        # sanity: wallet + allocations should equal the portfolio quantity
        h["qty_check_diff"] = h["qty_total"] - (h["balance"] + alloc)
        p = px.get(h["coin"])
        if p is None and h["value_native"] and k_native and h["qty_total"]:
            p = h["value_native"] * k_native / h["qty_total"]
        h["price_usdt"] = p
        if h["value_native"] is not None and k_native:
            h["value_usdt"], h["value_source"] = h["value_native"] * k_native, "app"
        else:
            h["value_usdt"], h["value_source"] = (h["qty_total"] * p if p is not None else None), "ticker"
        h["ticker_usdt"] = px.get(h["coin"])
        pa = (h["value_usdt"] / h["qty_total"]) if h["value_usdt"] is not None and h["qty_total"] else p   # App's own price
        h["alloc_usdt"] = {k: v * pa for k, v in h["allocation"].items()} if pa is not None else {}
        h["wallet_usdt"] = h["balance"] * pa if pa is not None else None
    fiat = []
    for b in balances:
        a, cu = amt(b.get("amount")), (b.get("currency") or cur_of(b.get("amount")) or "").upper()
        if a:
            k = fiat_usdt(cu, px)
            fiat.append({"currency": cu, "amount": a, "usdt": a * k if k else None})
    products = []
    for p in (pf.get("products") or []):
        v = amt(p.get("price_native"))
        if v:
            products.append({"name": p.get("name"), "kind": product_kind(p.get("name")), "value_native": v,
                             "currency": cur_of(p.get("price_native")), "value_usdt": v * k_native if k_native else None})
    holdings.sort(key=lambda h: -(h["value_usdt"] or 0))
    fiat_usdt_sum = sum(f["usdt"] or 0 for f in fiat)
    hold_total = sum(h["value_usdt"] or 0 for h in holdings) + fiat_usdt_sum
    pf_native = amt(pf.get("price_native"))
    if pf_native is None and products:
        pf_native = sum(p["value_native"] for p in products)
    pf_usdt = pf_native * k_native if pf_native is not None and k_native else None
    if pf_usdt is not None and not any(p["kind"] == "法幣錢包" for p in products):
        pf_usdt += fiat_usdt_sum            # the portfolio lists crypto products only; add fiat cash
    total, source = (pf_usdt, "portfolio") if pf_usdt is not None else (hold_total, "holdings")
    unpriced = [h["coin"] for h in holdings if h["value_usdt"] is None]
    return {"exchange": "cryptocom", "captured_oslo": t.isoformat(timespec="seconds"), "ok": bool(wallets or products or balances),
            "holdings": holdings, "fiat": fiat, "products": products, "native_currency": native, "usdt_per_native": k_native,
            "portfolio_native": pf_native, "portfolio_usdt": pf_usdt, "holdings_usdt": hold_total,
            "total_usdt": total, "total_source": source, "unpriced": unpriced, "errors": errors, "dust_wallets": dust,
            "wallets_listed": len(wallets), "key_fingerprint": c.fingerprint(), "calls": c.calls}


def write_snapshot(directory: Path, doc: dict, t: dt.datetime) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / f"cryptocom_snapshot_{stamp(t)}.json"
    p.write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
    return p


def latest_snapshot(roots: list[Path]):
    best = None
    for r in roots:
        if r.exists():
            for p in r.rglob("cryptocom_snapshot_*.json"):
                if not p.name.startswith("._") and (best is None or p.name > best.name):
                    best = p
    return (json.loads(best.read_text(encoding="utf-8")), best) if best else (None, None)


# ---------------------------------------------------------------- state / baseline / key expiry
def _rj(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _wj(p: Path, d) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, indent=1, ensure_ascii=False), encoding="utf-8")


def load_state(root: Path) -> dict:
    return _rj(root / "state.json", {})


def save_state(root: Path, st: dict) -> None:
    _wj(root / "state.json", st)


def load_baseline(root: Path) -> dict | None:
    return _rj(root / "baseline.json", None)


def save_baseline(root: Path, snap: dict | None, clear: bool = False) -> dict | None:
    p = root / "baseline.json"
    if clear:
        p.unlink(missing_ok=True)
        return None
    old = load_baseline(root) or {}
    b = {"at": snap["captured_oslo"], "value_usdt": snap["total_usdt"], "value_native": snap.get("portfolio_native"),
         "native_currency": snap.get("native_currency"), "source": snap.get("total_source"),
         "flows": old.get("flows", []), "auto_flows": old.get("auto_flows", True),
         "note": "盈虧＝目前總額 − 基準 − 基準後淨入金。auto_flows=true 時會從交易紀錄自動辨識入金／出金；"
                 "也可手動加到 flows：[{\"at\": ISO, \"usdt\": 正=入金}]。"}
    _wj(p, b)
    return b


def note_key(root: Path, fp: str, ok: bool, err: CdcError | None = None) -> dict:
    """Remember when the current key (by fingerprint, never the key itself) first worked; record rejections."""
    st = load_state(root)
    k = st.setdefault("key", {})
    now = now_oslo().isoformat(timespec="seconds")
    if k.get("fingerprint") != fp:
        k.clear()
        k.update(fingerprint=fp, first_ok=None, expires=None)
    if ok:
        k["first_ok"] = k.get("first_ok") or now
        k["last_ok"] = now
        k.pop("rejected", None)
    elif err is not None and err.auth:
        k["rejected"] = {"at": now, "code": err.code, "message": str(err)[:300]}
    save_state(root, st)
    return k


def key_status(root: Path) -> dict:
    k = (load_state(root).get("key")) or {}
    out = {"fingerprint": k.get("fingerprint"), "first_ok": k.get("first_ok"), "last_ok": k.get("last_ok"),
           "rejected": k.get("rejected"), "expires": k.get("expires"), "expires_assumed": False, "days_left": None, "warning": None}
    exp = k.get("expires")
    if not exp and k.get("first_ok"):
        exp = (dt.datetime.fromisoformat(k["first_ok"]) + dt.timedelta(days=EXPIRY_DAYS_DEFAULT)).isoformat(timespec="seconds")
        out["expires_assumed"] = True
    if exp:
        e = dt.datetime.fromisoformat(exp if "T" in exp else exp + "T23:59:59+02:00")
        if e.tzinfo is None:
            e = e.replace(tzinfo=OSLO)
        out["expires"] = e.isoformat(timespec="seconds")
        out["days_left"] = (e - now_oslo()).total_seconds() / 86400
    if out["rejected"]:
        out["warning"] = "Crypto.com API Key 被拒絕（" + str(out["rejected"].get("code")) + "）：請在 App 重新產生並執行 setup-keychain --exchange cryptocom"
    elif out["days_left"] is not None and out["days_left"] <= WARN_DAYS:
        out["warning"] = (f"Crypto.com API Key {'可能' if out['expires_assumed'] else ''}將在 {max(out['days_left'], 0):.0f} 天內到期"
                          f"（{out['expires'][:10]}{'，以第一次成功使用＋30 天估計' if out['expires_assumed'] else ''}）："
                          f"請在 App 延長或重新產生，然後 {cli_cmd()} set-cryptocom-key-expiry YYYY-MM-DD")
    return out


def set_key_expiry(root: Path, date: str | None) -> dict:
    st = load_state(root)
    k = st.setdefault("key", {})
    k["expires"] = (dt.date.fromisoformat(date).isoformat() + "T23:59:59+02:00") if date else None
    save_state(root, st)
    return k


# ---------------------------------------------------------------- transactions
def tx_key(t: dict) -> str:
    if t.get("id") and t.get("nature"):          # ids are per "nature" (the cursor is "<nature>/<id>")
        return f"id:{t['nature']}/{t['id']}"
    for k in ("id", "transaction_id", "txn_id", "uuid", "order_id"):
        if t.get(k):
            return f"{k}:{t[k]}"
    return "h:" + hashlib.sha1(json.dumps(t, sort_keys=True).encode()).hexdigest()[:20]


def tx_time(t: dict) -> str | None:
    for k in ("created_at", "updated_at", "time", "timestamp", "date", "transaction_time", "completed_at"):
        v = t.get(k)
        if v in (None, ""):
            continue
        try:
            if isinstance(v, (int, float)) or str(v).isdigit():
                v = float(v)
                return dt.datetime.fromtimestamp(v / 1000 if v > 1e11 else v, OSLO).isoformat(timespec="seconds")
            return dt.datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(OSLO).isoformat(timespec="seconds")
        except (ValueError, OSError):
            continue
    return None


def load_transactions(root: Path) -> list[dict]:
    p = root / "transactions.jsonl"
    rows = {}
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                rows[r["_key"]] = r
    return sorted(rows.values(), key=lambda r: r.get("_time") or "")


def _next_cursor(d: dict) -> str | None:
    pg = (d.get("meta") or {}).get("pagination") or d.get("pagination") or {}
    nx = pg.get("next") if isinstance(pg, dict) else None
    if isinstance(nx, dict):
        return nx.get("last_id") or None
    return (pg.get("next_page_token") if isinstance(pg, dict) else None) or None


def fetch_transactions(c: CdcClient, known: set, start: str | None = None, stop_on_known: bool = True,
                       max_pages: int | None = None) -> tuple[list[dict], dict]:
    """Page through /v1/transactions (newest first). Cursor: meta.pagination.next.last_id → ?last_id=…&count=50.
    stop_on_known: stop at the first page that is entirely archived (incremental top-up).
    Returns (new rows, info). info["cursor"] = where to resume; info["end"] = True when the oldest page was reached."""
    rows, info = [], {"pages": 0, "top_level_keys": None, "item_keys": None, "pagination": None, "stopped": None,
                      "cursor": start, "end": False, "page_size": TX_PAGE_SIZE}
    cursor, seen_cursors = start, set()
    max_pages = max_pages or MAX_TX_PAGES
    for _ in range(max_pages):
        params = {"count": str(TX_PAGE_SIZE)}
        if cursor:
            params["last_id"] = cursor
        d = c.get("/v1/transactions", params)
        info["pages"] += 1
        items = d.get("transactions")
        if items is None:
            items = (d.get("data") or {}).get("transactions") if isinstance(d.get("data"), dict) else d.get("data")
        items = [t for t in (items or []) if isinstance(t, dict)]
        info["top_level_keys"] = info["top_level_keys"] or sorted(d.keys())
        if items and not info["item_keys"]:
            info["item_keys"] = sorted(items[0].keys())
        pg = (d.get("meta") or {}).get("pagination") or d.get("pagination")
        info["pagination"] = pg if isinstance(pg, dict) else (str(pg)[:80] if pg else None)
        new = [t for t in items if tx_key(t) not in known]
        rows += new
        known |= {tx_key(t) for t in new}
        nxt = _next_cursor(d)
        if not items:
            info["stopped"], info["end"] = "空頁（已到最早的紀錄）", True
            break
        if nxt:
            info["cursor"] = nxt
        if stop_on_known and not new:
            info["stopped"] = "整頁都已封存"
            break
        if not nxt or nxt in seen_cursors or nxt == cursor:
            info["stopped"], info["end"] = "API 沒有下一頁（已到最早的紀錄）", True
            break
        if len(items) < TX_PAGE_SIZE and nxt is None:
            info["stopped"], info["end"] = "最後一頁", True
            break
        seen_cursors.add(nxt)
        cursor = nxt
    else:
        info["stopped"] = f"達到本次 {max_pages} 頁上限（下次從游標繼續）"
    return rows, info


def archive_run(c: CdcClient, root: Path, progress: bool = True) -> dict:
    t = now_oslo()
    rep = {"captured_oslo": t.isoformat(timespec="seconds"), "root": str(root), "sources": {}, "errors": {}, "snapshot": None,
           "baseline_created": None, "warning": None}
    try:
        fp = c.fingerprint()
    except SystemExit as e:
        rep["errors"]["credentials"] = [{"code": "missing", "msg": str(e)}]
        return rep
    try:
        snap = build_snapshot(c, t)
    except CdcError as e:
        note_key(root, fp, False, e)
        rep["errors"]["auth" if e.auth else "snapshot"] = [{"code": e.code, "msg": str(e)}]
        rep["warning"] = key_status(root).get("warning")
        return rep
    note_key(root, fp, snap["ok"])
    if snap["ok"]:
        sp = write_snapshot(root / t.strftime("%Y-%m") / "snapshots", snap, t)
        rep["snapshot"] = str(sp.relative_to(root))
        if load_baseline(root) is None and snap.get("total_usdt") is not None:
            save_baseline(root, snap)
            rep["baseline_created"] = snap["captured_oslo"]
    for k, v in snap["errors"].items():
        rep["errors"].setdefault("snapshot", []).append({"code": k, "msg": v})
    old = {r["_key"]: r for r in load_transactions(root)}
    st0 = load_state(root).get("transactions") or {}
    new, info = [], {"pages": 0}
    try:
        # 1) newest pages until we meet already-archived rows
        new, info = fetch_transactions(c, set(old), stop_on_known=bool(old))
        info["backfill_complete"] = bool(st0.get("backfill_complete")) or (info.get("end") and not old)
        info["backfill_cursor"] = None if info["backfill_complete"] else (st0.get("backfill_cursor") or info.get("cursor"))
        if not old and info.get("end"):
            info["backfill_complete"], info["backfill_cursor"] = True, None
        # 2) history not complete yet (first run cut short, or earlier error): continue from the saved cursor
        if not info["backfill_complete"] and info["backfill_cursor"] and old:
            more, bi = fetch_transactions(c, set(old) | {tx_key(r) for r in new}, start=info["backfill_cursor"], stop_on_known=False,
                                          max_pages=max(MAX_TX_PAGES - info["pages"], 1))
            new += more
            info["pages"] += bi["pages"]
            info["backfill"] = {k: bi[k] for k in ("pages", "stopped", "end")}
            info["backfill_complete"] = bool(bi["end"])
            info["backfill_cursor"] = None if bi["end"] else bi["cursor"]
        elif not old and not info.get("end"):
            info["backfill_cursor"] = info.get("cursor")
    except CdcError as e:
        rep["errors"]["transactions"] = [{"code": e.code, "msg": str(e)}]
        info = {**info, "backfill_complete": bool(st0.get("backfill_complete")), "backfill_cursor": st0.get("backfill_cursor")}
    for r in new:
        old[tx_key(r)] = {"_key": tx_key(r), "_time": tx_time(r), **r}
    if not new and not old and info.get("end"):
        info["backfill_complete"] = True
    if new:
        p = root / "transactions.jsonl"
        root.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n" for r in sorted(old.values(), key=lambda r: r.get("_time") or "")), encoding="utf-8")
        tmp.replace(p)
    st = load_state(root)
    st["transactions"] = {**info, "total": len(old), "last_run": rep["captured_oslo"]}
    save_state(root, st)
    times = sorted(r.get("_time") for r in old.values() if r.get("_time"))
    rep["sources"]["cryptocom_transactions"] = {"from": "全部頁", "added": len(new), "duplicates": 0, "total": len(old),
                                               "earliest": times[0][:10] if times else None, "calls": info.get("pages", 0)}
    rep["calls"] = c.calls
    rep["tx_info"] = info
    rep["warning"] = key_status(root).get("warning")
    return rep


# ---------------------------------------------------------------- transaction classification
# Kinds seen in the real API (2026-10-04, 1,534 rows since account opening 2025-08-08). Explicit lists, never substrings:
# e.g. "rewards_platform_deposit_credited" (Mystery Box), "supercharger_deposit", "finance.airdrop_arena.deposit.…" are NOT deposits.
FLOW_IN_KINDS = {"crypto_purchase": "刷卡／付款買幣", "viban_purchase": "法幣錢包買幣（銀行入金）", "crypto_deposit": "鏈上充值"}
FLOW_OUT_KINDS = {"crypto_withdrawal": "鏈上提幣", "crypto_viban_exchange": "賣幣到法幣錢包（出金）", "card_top_up": "轉到卡片"}
INTERNAL_KINDS = {
    "crypto_exchange": "幣幣兌換", "recurring_buy_order": "定期定額（幣幣）", "dust_conversion_credited": "小額轉換",
    "crypto_earn_program_created": "Earn 存入", "crypto_earn_program_withdrawn": "Earn 取回",
    "finance.dpos.staking.crypto_wallet": "質押存入", "finance.dpos.unstaking.crypto_wallet": "解除質押",
    "finance.dpos.instant_unstaking.crypto_wallet": "立即解除質押",
    "finance.defi_staking.staking.crypto_wallet": "DeFi 質押存入", "finance.defi_staking.unstaking.crypto_wallet": "DeFi 解除質押",
    "finance.defi_lending.staking.crypto_wallet": "DeFi 借貸存入",
    "supercharger_deposit": "Supercharger 存入", "supercharger_withdrawal": "Supercharger 取回",
    "finance.airdrop_arena.deposit.crypto_wallet": "Airdrop Arena 存入", "finance.airdrop_arena.withdrawal.crypto_wallet": "Airdrop Arena 取回",
}
INCOME_KINDS = {
    "crypto_earn_interest_paid": "Earn 利息", "finance.crypto_earn.loyalty_program_extra_interest_paid.crypto_wallet": "Earn 利息",
    "finance.dpos.non_compound_interest.crypto_wallet": "質押收益", "finance.dpos.non_compound_restaking_interest.crypto_wallet": "質押收益",
    "finance.defi_staking.non_compound_interest.crypto_wallet": "DeFi 質押收益",
    "supercharger_reward_to_app_credited": "Supercharger 獎勵", "finance.airdrop_arena.reward.crypto_wallet": "Airdrop Arena 獎勵",
    "rewards_platform_deposit_credited": "Mystery Box 等獎勵", "reward.loyalty_program.trading_rebate.crypto_wallet": "交易回饋",
}


def tx_type(t: dict) -> str:
    """Real API: `kind` (e.g. crypto_earn_interest_paid, finance.defi_staking.staking.crypto_wallet) is the most specific field."""
    for k in ("kind", "transaction_type", "type", "source_type", "nature"):
        if t.get(k):
            return str(t[k])
    return ""


def _norm_kind(k: str) -> str:
    return "crypto_purchase" if k.startswith("trading.crypto_purchase") else k     # e.g. trading.crypto_purchase.apple_pay


def tx_class(t: dict) -> tuple[str, str]:
    """-> (class, label); class in in|out|internal|income|ignored|unknown. Only status 'done' rows count."""
    if (t.get("status") or "done") != "done":
        return "ignored", f"狀態 {t.get('status')}"
    k = _norm_kind(tx_type(t))
    if k == "recurring_buy_order" and " > " not in (t.get("description") or ""):
        return "in", "定期定額（付款）"          # a card-funded recurring buy says "Buy X"; "USDC > BTC" is a swap
    for cls, table in (("in", FLOW_IN_KINDS), ("out", FLOW_OUT_KINDS), ("internal", INTERNAL_KINDS), ("income", INCOME_KINDS)):
        if k in table:
            return cls, table[k]
    if any(w in k for w in ("interest", "reward", "cashback", "rebate")):
        return "income", "其他獎勵"
    return "unknown", k or "（無類型）"


def classify(t: dict) -> str | None:
    c, _ = tx_class(t)
    return c if c in ("in", "out") else None


def tx_native_usd(t: dict) -> tuple[float | None, str | None]:
    v = amt(t.get("native_amount"))
    return (v, cur_of(t.get("native_amount")) or t.get("native_currency")) if v is not None else (None, None)


def flow_usdt(t: dict, px: dict, k_native: float | None) -> float | None:
    """Absolute USDT value at the time of the transaction (App's native value), falling back to today's price."""
    v, c = tx_native_usd(t)
    if v is not None:
        kk = fiat_usdt(c, px) if (c and px) else (k_native or (1.0 if (c or "").upper() in ("USD", "USDT") else None))
        return abs(v) * kk if kk else None
    v = amt(t.get("amount"))
    c = cur_of(t.get("amount")) or t.get("currency")
    if v is not None and c:
        p = px.get(str(c).upper()) or fiat_usdt(c, px)
        return abs(v) * p if p else None
    return None


# ---------------------------------------------------------------- analysis
def analyze(project: Path, root: Path | None = None, offline: bool = False) -> dict:
    root = root or project / "archive" / "cryptocom"
    ks = key_status(root)
    snap, sp = latest_snapshot([root, project / "snapshots"])
    if snap is None:
        return {"present": False, "configured": available(), "key": ks,
                "reason": ("尚無 Crypto.com 快照：" + (ks.get("warning") or f"在 Mac 執行 {cli_cmd()} archive --exchange cryptocom"))
                if available() else no_creds_msg()}
    bl = load_baseline(root)
    txs = load_transactions(root)
    st = load_state(root)
    sti = st.get("transactions") or {}
    complete = bool(sti.get("backfill_complete"))
    px = {} if offline else tickers()
    k_native = snap.get("usdt_per_native")
    all_flows, by_class, unknown = [], {}, {}
    income: dict[str, float] = {}
    for r in txs:
        cls, lab = tx_class(r)
        v = flow_usdt(r, px or {}, k_native)
        b = by_class.setdefault(cls, {"count": 0, "usdt": 0.0})
        b["count"] += 1
        b["usdt"] += (amt(r.get("native_amount")) or 0.0) * (k_native or 1.0)
        if cls == "unknown":
            unknown[lab] = unknown.get(lab, 0) + 1
        elif cls == "income":
            income[lab] = income.get(lab, 0.0) + (v or 0.0) * (1 if (amt(r.get("native_amount")) or 0) >= 0 else -1)
        elif cls in ("in", "out"):
            all_flows.append({"at": r.get("_time"), "kind": cls, "usdt": (v if cls == "in" else -v) if v is not None else None,
                              "auto": True, "raw_type": tx_type(r), "label": lab, "desc": r.get("description"),
                              "amount": amt(r.get("amount")), "currency": cur_of(r.get("amount"))})
    deposits = sum(f["usdt"] for f in all_flows if f["kind"] == "in" and f["usdt"] is not None)
    withdrawals = -sum(f["usdt"] for f in all_flows if f["kind"] == "out" and f["usdt"] is not None)
    total = snap.get("total_usdt")
    full = None
    if complete:
        net_full = deposits - withdrawals
        full = {"from": txs[0]["_time"] if txs else None, "deposits": deposits, "withdrawals": withdrawals, "net_inflow": net_full,
                "pnl": (total - net_full) if total is not None else None}
        full["pnl_pct"] = (full["pnl"] / deposits * 100) if full["pnl"] is not None and deposits else None   # vs money put in
    flows = []
    if bl:
        if bl.get("auto_flows", True):
            flows = [f for f in all_flows if (f["at"] or "") > bl["at"]]
        for f in bl.get("flows") or []:
            flows.append({"at": f.get("at"), "kind": "in" if (f.get("usdt") or 0) >= 0 else "out", "usdt": f.get("usdt"), "auto": False})
    unknown_flow = any(f["usdt"] is None for f in flows)
    bnet = (bl["value_usdt"] + sum(f["usdt"] or 0 for f in flows)) if bl and bl.get("value_usdt") is not None else None
    bpnl = (total - bnet) if total is not None and bnet is not None else None
    since_bl = {"net_inflow": bnet, "pnl": bpnl, "pnl_pct": (bpnl / bnet * 100) if bpnl is not None and bnet else None}
    if full and full["pnl"] is not None:
        method, net, pnl, pnl_pct = "full", full["net_inflow"], full["pnl"], full["pnl_pct"]
    else:
        method, net, pnl, pnl_pct = "baseline", bnet, bpnl, since_bl["pnl_pct"]
    split: dict[str, float] = {}
    for p in snap.get("products") or []:
        if p.get("value_usdt") is not None:
            split[p["kind"]] = split.get(p["kind"], 0.0) + p["value_usdt"]
    alloc_split: dict[str, float] = {}
    for h in snap.get("holdings") or []:
        pa = (h["value_usdt"] / h["qty_total"]) if h.get("value_usdt") is not None and h.get("qty_total") else None
        al = {k: v * pa for k, v in (h.get("allocation") or {}).items()} if pa is not None else (h.get("alloc_usdt") or {})
        for k, v in al.items():
            alloc_split[k] = alloc_split.get(k, 0.0) + v
    earn = sum(v for k, v in split.items() if k in EARN_KINDS) if split else sum(alloc_split.values())
    times = [r["_time"] for r in txs if r.get("_time")]
    return {
        "present": True, "configured": available(), "snapshot_time": snap["captured_oslo"],
        "snapshot_file": str(sp.relative_to(project)) if sp else None,
        "total": total, "total_source": snap.get("total_source"), "portfolio_usdt": snap.get("portfolio_usdt"),
        "holdings_usdt": snap.get("holdings_usdt"), "native_currency": snap.get("native_currency"),
        "portfolio_native": snap.get("portfolio_native"), "usdt_per_native": k_native,
        "holdings": snap.get("holdings"), "fiat": snap.get("fiat"), "products": snap.get("products"),
        "product_split": split, "allocation_split": alloc_split, "earn": earn, "unpriced": snap.get("unpriced"),
        "errors": snap.get("errors"), "baseline": bl, "flows": flows, "flows_unpriced": unknown_flow,
        "method": method, "history_complete": complete, "full": full, "since_baseline": since_bl,
        "all_flows": all_flows, "deposits": deposits, "withdrawals": withdrawals, "income": income,
        "income_total": sum(income.values()), "by_class": by_class, "unknown_kinds": unknown,
        "net_inflow": net, "pnl": pnl, "pnl_pct": pnl_pct,
        "transactions": {"count": len(txs), "recent": txs[-30:], "info": sti, "earliest": times[0] if times else None,
                         "latest": times[-1] if times else None,
                         "types": dict(sorted(__import__("collections").Counter(tx_type(r) or "（無類型欄位）" for r in txs).items(), key=lambda x: -x[1]))},
        "key": ks, "warning": ks.get("warning"),
        "notes": [
            "資料來自 Crypto.com App 的 Agent API Key（Beta，唯讀：只呼叫白名單內的 GET 端點；交易、法幣出金、撤銷金鑰等端點在本機就被拒絕）。",
            "總額＝/v1/portfolio 的 App 市值（美元換成 USDT）；各幣＝錢包＋Earn／質押等配置（currency_allocation），與 portfolio 的數量互相核對。",
            ("盈虧＝目前總額 − 開戶以來淨入金（交易紀錄完整，自 " + (times[0][:10] if times else "—") + "）。入金＝刷卡／Apple Pay 買幣、法幣錢包買幣、鏈上充值；"
             "出金＝鏈上提幣、賣幣到法幣錢包；金額用交易當時的美元價值。Earn／質押收益、獎勵、兌換、存入／取回都不算入金。")
            if method == "full" else
            ("盈虧以基準起算（" + ((bl or {}).get("at") or "—")[:16].replace("T", " ") + "）：交易紀錄尚未讀完整；基準後的入金／出金從交易紀錄辨識。"),
            "轉到其他交易所的提幣在這裡算出金，在對方算入金；合併檢視中兩者互相抵銷。",
        ] + ([f"未分類的交易類型（不計入出入金，請確認）：{', '.join(f'{k}×{v}' for k, v in unknown.items())}"] if unknown else []),
    }


def perms_report(c: CdcClient) -> dict:
    out = {"probes": {}, "refused_locally": [], "local_problem": None}
    try:
        key, secret = c.creds()
    except SystemExit as e:
        out["local_problem"] = str(e)
        return out
    bad = secret_problem(key, secret)
    if bad:
        out["local_problem"] = bad
        return out
    for path in ("/v1/crypto-account", "/v1/fiat-account", "/v1/portfolio", "/v1/transactions"):
        try:
            d = c.get(path)
            out["probes"][path] = "OK（" + ", ".join(sorted(k for k in d if k != "ok"))[:80] + "）"
        except CdcError as e:
            out["probes"][path] = f"{e.code}: {e}"[:160]
            if e.auth:
                break
    for w in KNOWN_WRITES:
        try:
            check_request("POST" if "orders" in w or "withdraw" in w or "revoke" in w or "email" in w or "quotations" in w else "GET", w)
        except WriteRefused:
            out["refused_locally"].append(w)
    return out
