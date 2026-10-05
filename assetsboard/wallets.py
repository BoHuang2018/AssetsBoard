"""Read-only on-chain wallets (no private keys).

Chains:
  arbitrum  — public JSON-RPC (eth_getBalance + ERC-20 balanceOf for a fixed token list)
  bittensor — public explorer API https://taoscan.online/api/account/<ss58> (free, no key)

Addresses live in archive/wallets/wallets.json (not secrets). Defaults are written on first use.
"""
from __future__ import annotations

import datetime as dt
import json
import urllib.error
import urllib.request
from pathlib import Path

from .client import OSLO, cli_cmd, now_oslo, stamp

UA = "assetsboard/1.0 (read-only portfolio tracker)"
ARB_RPCS = (
    "https://arb1.arbitrum.io/rpc",
    "https://arbitrum.llamarpc.com",
    "https://rpc.ankr.com/arbitrum",
)
# Native USDC on Arbitrum One; USDC.e / USDT / WETH also probed (zero balances are dropped).
ARB_TOKENS = (
    ("USDC", "0xaf88d065e77c8cC2239327C5EDb3A432268e5831", 6),
    ("USDC.e", "0xFF970A61A04b1cA14834A43f5dE4533eBDDB5CC8", 6),
    ("USDT", "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9", 6),
    ("WETH", "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1", 18),
)
TAOSCAN = "https://taoscan.online/api/account/"
TICKERS_URL = "https://api.crypto.com/exchange/v1/public/get-tickers"
COINGECKO_TAO = "https://api.coingecko.com/api/v3/simple/price?ids=bittensor&vs_currencies=usd"

DEFAULT_WALLETS = [
    # Placeholders only — replace with your own addresses in archive/wallets/wallets.json (gitignored).
    {
        "id": "example-arbitrum",
        "label": "Example · Arbitrum wallet",
        "chain": "arbitrum",
        "address": "0x00000000000000000000000000000000DEADBEEF",
        "note": "Optional note (e.g. funding source). Public RPC read-only; never store a seed phrase here.",
    },
    {
        "id": "example-bittensor",
        "label": "Example · Bittensor coldkey",
        "chain": "bittensor",
        "address": "5FakeBittensorAddressDoNotUseReplaceMeXXXXXXX",
        "note": None,
    },
]


class WalletError(Exception):
    pass


def available() -> bool:
    """Always True: public reads need no credentials; presence depends on config/wallets.json."""
    return True


def config_path(root: Path) -> Path:
    return root / "wallets.json"


def load_config(root: Path) -> list[dict]:
    p = config_path(root)
    if not p.exists():
        root.mkdir(parents=True, exist_ok=True)
        doc = {"_doc": "唯讀鏈上錢包地址（不是密鑰）。可新增／修改後執行 archive --exchange wallets。",
               "wallets": DEFAULT_WALLETS}
        p.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return [dict(w) for w in DEFAULT_WALLETS]
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise WalletError(f"讀取 {p} 失敗：{e}") from None
    return list(doc.get("wallets") or [])


def _http_json(url: str, body: bytes | None = None, timeout: int = 20) -> dict:
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST" if body else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read() or b""
        raise WalletError(f"HTTP {e.code} {url.split('?', 1)[0][:60]}：{(raw[:120] or b'').decode('utf-8', 'replace')}") from None
    except Exception as e:  # noqa: BLE001
        raise WalletError(f"連線失敗（{type(e).__name__}）{url.split('?', 1)[0][:60]}") from None


def _arb_rpc(method: str, params: list):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    last = None
    for url in ARB_RPCS:
        try:
            d = _http_json(url, body)
            if "error" in d and d["error"]:
                last = WalletError(str(d["error"])[:160])
                continue
            return d["result"]
        except WalletError as e:
            last = e
    raise last or WalletError("Arbitrum RPC 全部失敗")


def fetch_arbitrum(address: str) -> dict:
    addr = address if address.startswith("0x") else "0x" + address
    if len(addr) != 42:
        raise WalletError(f"Arbitrum 地址長度不對：{addr[:12]}…")
    eth = int(_arb_rpc("eth_getBalance", [addr, "latest"]), 16) / 1e18
    tokens = []
    if eth > 0:
        tokens.append({"symbol": "ETH", "amount": eth, "contract": None, "decimals": 18})
    data = "0x70a08231" + addr[2:].lower().zfill(64)
    for sym, contract, dec in ARB_TOKENS:
        raw = _arb_rpc("eth_call", [{"to": contract, "data": data}, "latest"])
        amt = int(raw or "0x0", 16) / (10 ** dec)
        if amt > 0:
            tokens.append({"symbol": sym, "amount": amt, "contract": contract, "decimals": dec})
    return {"chain": "arbitrum", "address": addr, "tokens": tokens, "source": "arbitrum-rpc"}


def fetch_bittensor(address: str) -> dict:
    d = _http_json(TAOSCAN + address)
    bal = d.get("balance") or {}
    free = float(bal.get("freeTao") or 0)
    reserved = float(bal.get("reservedTao") or 0)
    frozen = float(bal.get("frozenTao") or 0)
    stakes = d.get("stakes") or []
    stake_tao = 0.0
    for s in stakes:
        for k in ("stakeTao", "tao", "amountTao", "amount"):
            if s.get(k) is not None:
                try:
                    stake_tao += float(s[k])
                    break
                except (TypeError, ValueError):
                    pass
    totals = d.get("totals") or {}
    if totals.get("stakeValueTao") is not None:
        try:
            stake_tao = float(totals["stakeValueTao"])
        except (TypeError, ValueError):
            pass
    tokens = []
    liquid = free  # reserved/frozen still "owned"; show separately
    total_tao = free + reserved + stake_tao
    if total_tao > 0 or True:
        tokens.append({"symbol": "TAO", "amount": total_tao, "free": free, "reserved": reserved,
                       "frozen": frozen, "staked": stake_tao, "contract": None, "decimals": 9})
    return {"chain": "bittensor", "address": address, "tokens": tokens, "nonce": bal.get("nonce"),
            "block": d.get("blockNumber"), "source": "taoscan.online", "raw_stakes": len(stakes)}


def prices() -> dict[str, float]:
    """USDT per unit. USDC/USDT = 1; ETH/TAO from Crypto.com public tickers, CoinGecko fallback for TAO."""
    out = {"USDC": 1.0, "USDC.e": 1.0, "USDT": 1.0, "USD": 1.0}
    try:
        data = _http_json(TICKERS_URL)["result"]["data"]
        px = {d["i"]: float(d["a"]) for d in data if d.get("a") not in (None, "", "0")}
        if px.get("ETH_USDT"):
            out["ETH"] = px["ETH_USDT"]
            out["WETH"] = px["ETH_USDT"]
        if px.get("TAO_USDT"):
            out["TAO"] = px["TAO_USDT"]
    except Exception:  # noqa: BLE001
        pass
    if "TAO" not in out:
        try:
            out["TAO"] = float(_http_json(COINGECKO_TAO)["bittensor"]["usd"])
        except Exception:  # noqa: BLE001
            pass
    if "ETH" not in out:
        try:
            cg = _http_json("https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd")
            out["ETH"] = out["WETH"] = float(cg["ethereum"]["usd"])
        except Exception:  # noqa: BLE001
            pass
    return out


def value_tokens(tokens: list[dict], px: dict) -> tuple[list[dict], float, list[str]]:
    rows, total, unpriced = [], 0.0, []
    for t in tokens:
        sym = t["symbol"]
        p = px.get(sym)
        # USDC.e priced as USDC
        if p is None and sym == "USDC.e":
            p = px.get("USDC", 1.0)
        row = {**t, "price_usdt": p, "value_usdt": (t["amount"] * p) if p is not None else None}
        rows.append(row)
        if row["value_usdt"] is not None:
            total += row["value_usdt"]
        else:
            unpriced.append(sym)
    return rows, total, unpriced


def build_snapshot(root: Path, t: dt.datetime | None = None, px: dict | None = None) -> dict:
    t = t or now_oslo()
    wallets = load_config(root)
    px = px or prices()
    items, errors = [], {}
    for w in wallets:
        wid, chain, addr = w.get("id") or w.get("address"), (w.get("chain") or "").lower(), w.get("address") or ""
        try:
            if chain == "arbitrum":
                raw = fetch_arbitrum(addr)
            elif chain == "bittensor":
                raw = fetch_bittensor(addr)
            else:
                raise WalletError(f"不支援的鏈：{chain}")
            toks, val, unp = value_tokens(raw["tokens"], px)
            items.append({
                "id": wid, "label": w.get("label") or wid, "chain": chain, "address": raw["address"],
                "note": w.get("note"), "tokens": toks, "value_usdt": val, "unpriced": unp,
                "source": raw.get("source"), "extra": {k: raw[k] for k in ("nonce", "block", "raw_stakes") if k in raw},
            })
        except WalletError as e:
            errors[str(wid)] = str(e)[:240]
            items.append({"id": wid, "label": w.get("label") or wid, "chain": chain, "address": addr,
                          "note": w.get("note"), "tokens": [], "value_usdt": None, "unpriced": [], "error": str(e)[:240]})
    total = sum(i["value_usdt"] or 0 for i in items)
    ok = any(i.get("value_usdt") is not None and not i.get("error") for i in items)
    return {"exchange": "wallets", "captured_oslo": t.isoformat(timespec="seconds"), "ok": ok,
            "wallets": items, "total_usdt": total if ok else None, "prices": {k: px[k] for k in sorted(px)},
            "errors": errors, "n_configured": len(wallets)}


def write_snapshot(directory: Path, doc: dict, t: dt.datetime) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / f"wallets_snapshot_{stamp(t)}.json"
    p.write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
    return p


def latest_snapshot(roots: list[Path]):
    best = None
    for r in roots:
        if r.exists():
            for p in r.rglob("wallets_snapshot_*.json"):
                if not p.name.startswith("._") and (best is None or p.name > best.name):
                    best = p
    return (json.loads(best.read_text(encoding="utf-8")), best) if best else (None, None)


def _rj(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _wj(p: Path, d) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, indent=1, ensure_ascii=False), encoding="utf-8")


def load_baseline(root: Path) -> dict | None:
    return _rj(root / "baseline.json", None)


def save_baseline(root: Path, snap: dict | None, clear: bool = False) -> dict | None:
    p = root / "baseline.json"
    if clear:
        p.unlink(missing_ok=True)
        return None
    old = load_baseline(root) or {}
    b = {"at": snap["captured_oslo"], "value_usdt": snap["total_usdt"],
         "flows": old.get("flows", []), "auto_flows": False,
         "note": "盈虧＝目前總額 − 基準。鏈上出入金未自動辨識；基準後若有轉入／轉出請在 flows 手動補登 [{at, usdt}]。"}
    _wj(p, b)
    return b


def archive_run(root: Path, progress: bool = True) -> dict:
    t = now_oslo()
    rep = {"captured_oslo": t.isoformat(timespec="seconds"), "root": str(root), "sources": {}, "errors": {},
           "snapshot": None, "baseline_created": None}
    load_config(root)  # ensure wallets.json exists
    try:
        snap = build_snapshot(root, t)
    except WalletError as e:
        rep["errors"]["snapshot"] = [{"code": "fetch", "msg": str(e)}]
        return rep
    if snap["ok"]:
        sp = write_snapshot(root / t.strftime("%Y-%m") / "snapshots", snap, t)
        rep["snapshot"] = str(sp.relative_to(root))
        if load_baseline(root) is None and snap.get("total_usdt") is not None:
            save_baseline(root, snap)
            rep["baseline_created"] = snap["captured_oslo"]
    for k, v in (snap.get("errors") or {}).items():
        rep["errors"].setdefault("wallet", []).append({"code": k, "msg": v})
    rep["sources"]["wallets"] = {"from": "公開 RPC／瀏覽器 API", "added": 0, "duplicates": 0,
                                  "total": snap.get("n_configured", 0), "earliest": None, "calls": snap.get("n_configured", 0)}
    rep["total_usdt"] = snap.get("total_usdt")
    rep["wallets"] = [{"id": w["id"], "value_usdt": w.get("value_usdt"), "error": w.get("error"),
                       "tokens": [(t["symbol"], t["amount"]) for t in w.get("tokens") or []]} for w in snap.get("wallets") or []]
    st = _rj(root / "state.json", {})
    st["last_run"] = rep["captured_oslo"]
    st["last_total_usdt"] = snap.get("total_usdt")
    _wj(root / "state.json", st)
    return rep


def analyze(project: Path, root: Path | None = None, offline: bool = False) -> dict:
    root = root or project / "archive" / "wallets"
    snap, sp = latest_snapshot([root, project / "snapshots"])
    cfg = []
    try:
        cfg = load_config(root) if root.exists() or True else []
    except WalletError:
        cfg = DEFAULT_WALLETS
    if snap is None:
        return {"present": False, "configured": bool(cfg),
                "reason": f"尚無鏈上錢包快照：執行 {cli_cmd()} archive --exchange wallets"}
    bl = load_baseline(root)
    total = snap.get("total_usdt")
    flows = []
    if bl:
        for f in bl.get("flows") or []:
            flows.append({"at": f.get("at"), "kind": "in" if (f.get("usdt") or 0) >= 0 else "out",
                          "usdt": f.get("usdt"), "auto": False})
    net = (bl["value_usdt"] + sum(f["usdt"] or 0 for f in flows)) if bl and bl.get("value_usdt") is not None else None
    pnl = (total - net) if total is not None and net is not None else None
    return {
        "present": True, "configured": True, "snapshot_time": snap["captured_oslo"],
        "snapshot_file": str(sp.relative_to(project)) if sp else None,
        "total": total, "wallets": snap.get("wallets"), "prices": snap.get("prices"),
        "errors": snap.get("errors"), "baseline": bl, "flows": flows,
        "net_inflow": net, "pnl": pnl, "pnl_pct": (pnl / net * 100) if pnl is not None and net else None,
        "notes": [
            "唯讀：只呼叫公開 RPC／瀏覽器 API，不存也不需要私鑰。",
            "Arbitrum：官方／公共 RPC 查 ETH 與固定穩定幣清單（USDC／USDC.e／USDT／WETH）。",
            "Bittensor：taoscan.online 公開帳戶 API（free＋reserved＋質押）。",
            "盈虧以第一次成功快照為基準（鏈上出入金未自動辨識；可在 baseline.json 的 flows 手動補登）。",
            "Trust Wallet 的 USDC 來自 MEXC 合約獲利，已不在 MEXC 餘額內，合併總覽不會重複計算。",
        ],
    }
