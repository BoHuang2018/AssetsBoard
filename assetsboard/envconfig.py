"""Private, non-secret settings from a gitignored `.env` in the project root.

Order: real environment variable > `.env` > caller's fallback (the legacy JSON configs).
Holds things like wallet / vault addresses and account ids. API keys and tokens stay in the macOS Keychain.
`ASSETSBOARD_ENV_FILE` points at another file (tests use it). Values are never logged.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_cache: dict[tuple[str, float], dict[str, str]] = {}

# Names that may live in .env. Anything matching *_KEY/_SECRET/_TOKEN/_PASSPHRASE is refused (Keychain only).
_SECRETISH = re.compile(r"(_KEY|_SECRET|_TOKEN|_PASSPHRASE|_PASSWORD)$")


def env_path() -> Path:
    p = os.environ.get("ASSETSBOARD_ENV_FILE", "").strip()
    return Path(p).expanduser() if p else PROJECT_ROOT / ".env"


def parse(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        k, sep, v = line.partition("=")
        k = k.strip()
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k):
            continue
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        else:
            v = re.split(r"\s+#", v, maxsplit=1)[0].strip()
        out[k] = v
    return out


def load(path: Path | None = None) -> dict[str, str]:
    p = path or env_path()
    try:
        st = p.stat()
    except OSError:
        return {}
    key = (str(p), st.st_mtime)
    if key not in _cache:
        _cache.clear()
        try:
            vals = parse(p.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            vals = {}
        _cache[key] = {k: v for k, v in vals.items() if not _SECRETISH.search(k)}
    return dict(_cache[key])


def get(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name, "").strip()
    if v:
        return v
    v = load().get(name, "").strip()
    return v or default


def wallet_var(wallet_id: str) -> str:
    """wallets.json id 'trust-arbitrum' -> WALLET_TRUST_ARBITRUM_ADDRESS."""
    return "WALLET_" + re.sub(r"[^A-Za-z0-9]+", "_", wallet_id).strip("_").upper() + "_ADDRESS"
