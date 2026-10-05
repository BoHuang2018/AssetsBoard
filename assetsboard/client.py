"""Read-only Bitget REST client (stdlib only).

Safety rules enforced here:
- Only HTTP GET is ever sent. There is no code path for POST/PUT/DELETE.
- Secret values are never logged, stored in call records, or printed.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = "https://api.bitget.com"
OSLO = ZoneInfo("Europe/Oslo")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_DIR = PROJECT_ROOT / "snapshots"

SECRET_NAMES = ("BITGET_API_KEY", "BITGET_API_SECRET", "BITGET_API_PASSPHRASE")
MEXC_SECRET_NAMES = ("MEXC_API_KEY", "MEXC_API_SECRET")   # optional second exchange
KRAKEN_SECRET_NAMES = ("KRAKEN_API_KEY", "KRAKEN_API_SECRET")   # optional third exchange
KEYCHAIN_SERVICE = "assetsboard"
LEGACY_KEYCHAIN_SERVICE = "bitguard"  # read fallback: existing Mac Keychain items keep working
SECURITY_BIN = "/usr/bin/security"
# Box-only fallback file (JSON: {"card": {"BITGET_API_KEY": ...}}). Order: env → macOS Keychain → file.
DEFAULT_SECRET_FILES = (
    Path("/home/box/agent-data/box-secrets.json"),
)


def secret_files() -> list[Path]:
    custom = (os.environ.get("ASSETSBOARD_SECRETS_FILE") or os.environ.get("BITGUARD_SECRETS_FILE") or "").strip()
    return ([Path(custom).expanduser()] if custom else []) + list(DEFAULT_SECRET_FILES)


def keychain_supported() -> bool:
    return sys.platform == "darwin" and os.path.exists(SECURITY_BIN)


def _keychain_queries(name: str) -> list[list[str]]:
    # preferred: service=assetsboard; then legacy service=bitguard (existing Mac keys);
    # older README: service=<NAME>, account=$USER
    q = [["-s", KEYCHAIN_SERVICE, "-a", name], ["-s", LEGACY_KEYCHAIN_SERVICE, "-a", name]]
    user = os.environ.get("USER", "").strip()
    if user:
        q.append(["-s", name, "-a", user])
    return q


def keychain_read(name: str, timeout: float = 120) -> str:
    """Password of the generic item, or "" if absent/denied. Never logged."""
    if not keychain_supported():
        return ""
    for q in _keychain_queries(name):
        try:
            r = subprocess.run([SECURITY_BIN, "find-generic-password", *q, "-w"],
                               capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    return ""


def keychain_has(name: str, legacy: bool = True) -> bool:
    """Whether the item exists (reads attributes only, so no access prompt and no secret is returned)."""
    if not keychain_supported():
        return False
    for q in _keychain_queries(name)[: None if legacy else 1]:
        try:
            r = subprocess.run([SECURITY_BIN, "find-generic-password", *q],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if r.returncode == 0:
            return True
    return False


def _file_secret(name: str) -> str:
    for f in secret_files():
        try:
            data = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        val = str(((data.get("card") or {}).get(name)) or "").strip()
        if val:
            return val
    return ""


def cli_cmd() -> str:
    """How the user should invoke assetsboard: the project venv's python if we run from it, else python3."""
    venv = PROJECT_ROOT / ".venv"
    try:
        in_venv = Path(sys.prefix).resolve() == venv.resolve()
    except OSError:
        in_venv = False
    return ".venv/bin/python -m assetsboard" if in_venv or (venv / "bin" / "python").exists() else "python3 -m assetsboard"


def load_secret(name: str) -> str:
    val = os.environ.get(name, "").strip() or keychain_read(name) or _file_secret(name)
    if val:
        return val
    flag = next((f" --exchange {e}" for pre, e in (("MEXC_", "mexc"), ("KRAKEN_", "kraken"), ("CRYPTOCOM_", "cryptocom"), ("IBKR_FLEX_", "ibkr-flex")) if name.startswith(pre)), "")
    hint = f"或在 ~/AssetsBoard 執行 {cli_cmd()} setup-keychain{flag} 存入鑰匙圈" if keychain_supported() else ""
    raise SystemExit(f"缺少憑證：請設定環境變數 {name}{hint}（見 README）")


def credential_sources(names=SECRET_NAMES) -> dict[str, str | None]:
    """name -> 'env' | 'keychain' | 'file' | None. Checks presence only; values are not read from the Keychain."""
    out: dict[str, str | None] = {}
    for n in names:
        if os.environ.get(n, "").strip():
            out[n] = "env"
        elif keychain_has(n):
            out[n] = "keychain"
        elif _file_secret(n):
            out[n] = "file"
        else:
            out[n] = None
    return out


def credentials_available(names=SECRET_NAMES) -> bool:
    """True if all credentials in `names` appear to be present (no secret values are returned)."""
    return all(credential_sources(names).values())


def sign(secret: str, timestamp: str, method: str, request_path: str, body: str = "") -> str:
    msg = f"{timestamp}{method.upper()}{request_path}{body}"
    digest = hmac.new(secret.encode(), msg.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def ok(result: dict | None) -> bool:
    return isinstance(result, dict) and str(result.get("code", "")) == "00000"


def fnum(x) -> float:
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def now_oslo() -> dt.datetime:
    return dt.datetime.now(OSLO)


def stamp(t: dt.datetime | None = None) -> str:
    return (t or now_oslo()).strftime("%Y-%m-%dT%H%M")


def ms_to_oslo(ms) -> str:
    try:
        return dt.datetime.fromtimestamp(int(ms) / 1000, OSLO).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return str(ms)


def _encode_query(params: dict | None) -> str:
    if not params:
        return ""
    items = sorted((k, str(v)) for k, v in params.items() if v is not None)
    return "?" + urllib.parse.urlencode(items) if items else ""


def _http_get(url: str, headers: dict, timeout: int = 30) -> dict:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            return json.loads(body)
        except ValueError:
            return {"code": f"HTTP{e.code}", "msg": body[:300]}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        return {"code": "NETWORK", "msg": f"{type(e).__name__}: {e}"[:300]}


def _is_rate_limited(result: dict) -> bool:
    code = str(result.get("code", ""))
    return code in ("429", "HTTP429", "40429") or "too many" in str(result.get("msg", "")).lower()


def public_get(path: str, params: dict | None = None) -> dict:
    """Unsigned public market-data GET."""
    return _http_get(BASE + path + _encode_query(params), {"locale": "zh-CN"}, timeout=60)


def _redact(result: dict) -> dict:
    """Copy of a response safe to store: IP whitelist contents are replaced by a flag."""
    d = result.get("data") if isinstance(result, dict) else None
    if isinstance(d, dict) and "ips" in d:
        d = dict(d, ips="<redacted:set>" if str(d.get("ips") or "").strip() else "")
        return dict(result, data=d)
    return result


class Client:
    """Signed GET-only client. Records every call (without secrets) in self.calls."""

    def __init__(self, sleep: float = 0.25, verbose: bool = False):
        self.sleep = sleep
        self.verbose = verbose
        self.calls: dict[str, dict] = {}
        self._creds: tuple[str, str, str] | None = None

    def _credentials(self) -> tuple[str, str, str]:
        if self._creds is None:
            self._creds = tuple(load_secret(n) for n in SECRET_NAMES)  # type: ignore[assignment]
        return self._creds  # type: ignore[return-value]

    def raw_get(self, path: str, params: dict | None = None) -> dict:
        if not path.startswith("/api/"):
            raise ValueError("path must start with /api/")
        key, secret, passphrase = self._credentials()
        request_path = path + _encode_query(params)
        result: dict = {}
        for attempt in range(4):
            time.sleep(self.sleep if attempt == 0 else 1.5 * attempt)
            ts = str(int(time.time() * 1000))
            headers = {
                "ACCESS-KEY": key,
                "ACCESS-SIGN": sign(secret, ts, "GET", request_path),
                "ACCESS-TIMESTAMP": ts,
                "ACCESS-PASSPHRASE": passphrase,
                "Content-Type": "application/json",
                "locale": "zh-CN",
            }
            result = _http_get(BASE + request_path, headers)
            if not isinstance(result, dict):
                result = {"code": "BADJSON", "msg": str(result)[:200]}
            if not _is_rate_limited(result):
                break
        return result

    def get(self, name: str, path: str, params: dict | None = None, record: bool = True) -> dict:
        result = self.raw_get(path, params)
        if record:
            self.calls[name] = {"path": path, "params": params, "response": _redact(result)}
        if self.verbose:
            status = "OK " if ok(result) else f"ERR {result.get('code')} {result.get('msg')}"
            print(f"  [{status[:80]}] {name}", file=sys.stderr)
        return result

    def errors(self) -> dict[str, str]:
        return {
            n: f"{c['response'].get('code')} {c['response'].get('msg')}"
            for n, c in self.calls.items() if not ok(c["response"])
        }


def data(result: dict | None, default=None):
    if not ok(result):
        return default
    d = result.get("data")
    return default if d is None else d


def result_list(result: dict | None, *keys: str) -> list:
    """Extract a list from data, or data[key] for the first key that holds a list."""
    d = data(result)
    if isinstance(d, list):
        return d
    if isinstance(d, dict):
        for k in keys or ("resultList", "dataList", "bills", "entrustedList", "trackingList"):
            v = d.get(k)
            if isinstance(v, list):
                return v
    return []
