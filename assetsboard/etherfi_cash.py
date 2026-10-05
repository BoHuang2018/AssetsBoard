"""Read-only ether.fi Cash (spending / 開銷帳戶).

No public REST API for card merchant history — ether.fi's public surface is on-chain.
We read Optimism EtherFiSafe balances (Blockscout), Arbitrum USDC top-ups into the
vault/TopUp address, and optional OP USDC outflows excluding Liquid vault deposits.

Card-level lunch spend needs the Cash app login (not implemented). Milestone coverage
uses top-up sum as spend_proxy when Spend events are unavailable.

Config: archive/etherfi_cash/config.json (under archive/, gitignored).
See etherfi_cash.example.json at repo root.
"""
from __future__ import annotations

import datetime as dt
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .client import cli_cmd, now_oslo, stamp

UA = "assetsboard/1.0 (read-only ether.fi Cash)"
BS_OP = "https://optimism.blockscout.com/api/v2"
BS_ARB = "https://arbitrum.blockscout.com/api/v2"
USDC_ARB = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
LIQUID_USD_VAULT = "0x08c6F91e2B681FaF5e17227F2a44C307b3C1364C"
LIQUID_USD_TELLER = "0x4DE413a26fC24c3FC27Cc983be70aA9c5C299387"
CASH_FACTORY = "0xF4e147Db314947fC1275a8CbB6Cde48c510cd8CF"

STABLEISH = {"USDC", "USDT", "USDC.E", "DAI", "USDE", "LIQUIDUSD", "LIQUIDRWA"}
ETHISH = {"WETH", "ETH", "LIQUIDETH", "WEETH", "EETH"}

DEFAULT_CONFIG = {
    "_doc": "唯讀 ether.fi Cash 開銷帳戶。填 vault（Optimism EtherFiSafe＝各鏈 TopUp 同址）。不要放 seed／密鑰。",
    "enabled": False,
    "label": "ether.fi Cash（開銷帳戶）",
    "vault": "0x00000000000000000000000000000000DEADBEEF",
    "note": "Copy from ether.fi Cash → Add Funds address (same 0x on Arb/Eth/Base/OP).",
}


class CashError(Exception):
    pass


def available() -> bool:
    return True


def config_path(root: Path) -> Path:
    return root / "config.json"


def load_config(root: Path) -> dict:
    p = config_path(root)
    if not p.exists():
        root.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return dict(DEFAULT_CONFIG)
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise CashError(f"讀取 {p} 失敗：{e}") from None
    out = dict(DEFAULT_CONFIG)
    out.update(doc)
    return out


def _http_json(url: str, timeout: int = 30) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raw = (e.read() or b"")[:160].decode("utf-8", "replace")
        raise CashError(f"HTTP {e.code}：{raw}") from None
    except Exception as e:  # noqa: BLE001
        raise CashError(f"連線失敗（{type(e).__name__}）") from None


def _prices() -> dict[str, float]:
    px = {"USDC": 1.0, "USDT": 1.0, "USDC.E": 1.0, "DAI": 1.0, "USDE": 1.0,
          "LIQUIDUSD": 1.0, "LIQUIDRWA": 1.0}
    try:
        d = _http_json("https://api.crypto.com/exchange/v1/public/get-tickers")
        for row in (d.get("result") or {}).get("data") or []:
            inst = (row.get("i") or "").upper()
            if inst in ("ETH_USDT", "ETH_USD"):
                try:
                    v = float(row.get("a") or row.get("k") or row.get("b") or 0)
                    if v:
                        px["ETH"] = v
                        px["WETH"] = v
                        px["LIQUIDETH"] = v
                        px["WEETH"] = v * 1.05
                except (TypeError, ValueError):
                    pass
            if inst == "ETHFI_USDT":
                try:
                    v = float(row.get("a") or 0)
                    if v:
                        px["ETHFI"] = v
                        px["SETHFI"] = v
                except (TypeError, ValueError):
                    pass
    except CashError:
        pass
    return px


def _norm_addr(a: str) -> str:
    a = (a or "").strip()
    if not a.startswith("0x"):
        a = "0x" + a
    if len(a) != 42:
        raise CashError(f"vault 地址長度不對：{a[:14]}…")
    return a


def fetch_op_balances(vault: str) -> list[dict]:
    vault = _norm_addr(vault)
    d = _http_json(f"{BS_OP}/addresses/{vault}/tokens")
    items, px = [], _prices()
    for it in d.get("items") or []:
        tok = it.get("token") or {}
        sym = (tok.get("symbol") or "?").upper()
        name = tok.get("name") or ""
        low = name.lower()
        if any(x in low for x in ("airdrop", "claim", "http", "optibase", "poks", "mopka")):
            continue
        dec = int(tok.get("decimals") or 18)
        try:
            amt = int(it.get("value") or "0") / (10 ** dec)
        except (TypeError, ValueError):
            continue
        if amt <= 0:
            continue
        price = None
        if sym in STABLEISH:
            price = 1.0
        elif sym in ETHISH or sym in px:
            price = px.get(sym) or px.get("ETH")
        elif sym in ("SETHFI", "ETHFI"):
            price = px.get("SETHFI") or px.get("ETHFI")
        val = (amt * price) if price is not None else None
        items.append({
            "symbol": sym, "name": name, "amount": amt,
            "contract": tok.get("address_hash") or tok.get("address") or tok.get("hash"),
            "decimals": dec, "price_usdt": price, "value_usdt": val,
        })
    items.sort(key=lambda x: -(x["value_usdt"] or 0))
    return items


def _page_token_transfers(base: str, path: str, max_pages: int = 8) -> list[dict]:
    out, url, pages = [], f"{base}{path}", 0
    while url and pages < max_pages:
        d = _http_json(url)
        out.extend(d.get("items") or [])
        nxt = d.get("next_page_params")
        if not nxt:
            break
        qs = "&".join(f"{k}={urllib.parse.quote(str(v), safe='')}" for k, v in nxt.items())
        path_only = path.split("?", 1)[0]
        url = f"{base}{path_only}?{qs}"
        pages += 1
    return out


def fetch_arb_topups(vault: str, max_pages: int = 6) -> list[dict]:
    vault = _norm_addr(vault)
    path = f"/addresses/{vault}/token-transfers?type=ERC-20&filter=to"
    rows = []
    try:
        items = _page_token_transfers(BS_ARB, path, max_pages=max_pages)
    except CashError:
        return []
    for it in items:
        tok = it.get("token") or {}
        sym = (tok.get("symbol") or "").upper()
        caddr = (tok.get("address_hash") or tok.get("address") or tok.get("hash") or "").lower()
        if sym != "USDC" and caddr != USDC_ARB.lower():
            continue
        fr = (it.get("from") or {}).get("hash") or ""
        if fr.lower() == CASH_FACTORY.lower():
            continue
        dec = int((it.get("total") or {}).get("decimals") or tok.get("decimals") or 6)
        try:
            amt = int((it.get("total") or {}).get("value") or 0) / (10 ** dec)
        except (TypeError, ValueError):
            continue
        if amt <= 0:
            continue
        rows.append({
            "at": it.get("timestamp"), "chain": "arbitrum", "symbol": "USDC",
            "amount": amt, "usdt": amt, "from": fr,
            "tx": (it.get("transaction_hash") or it.get("tx_hash")), "kind": "topup",
        })
    rows.sort(key=lambda r: r.get("at") or "")
    return rows


def fetch_op_possible_spends(vault: str, max_pages: int = 4) -> list[dict]:
    vault = _norm_addr(vault)
    skip = {LIQUID_USD_VAULT.lower(), LIQUID_USD_TELLER.lower(), vault.lower()}
    path = f"/addresses/{vault}/token-transfers?type=ERC-20&filter=from"
    out = []
    try:
        items = _page_token_transfers(BS_OP, path, max_pages=max_pages)
    except CashError:
        return []
    for it in items:
        tok = it.get("token") or {}
        sym = (tok.get("symbol") or "").upper()
        if sym not in ("USDC", "USDT"):
            continue
        to = ((it.get("to") or {}).get("hash") or "").lower()
        if to in skip:
            continue
        dec = int((it.get("total") or {}).get("decimals") or 6)
        try:
            amt = int((it.get("total") or {}).get("value") or 0) / (10 ** dec)
        except (TypeError, ValueError):
            continue
        if amt <= 0:
            continue
        out.append({
            "at": it.get("timestamp"), "chain": "optimism", "symbol": sym,
            "amount": amt, "usdt": amt, "to": to,
            "to_name": (it.get("to") or {}).get("name"),
            "tx": it.get("transaction_hash") or it.get("tx_hash"),
            "kind": "possible_spend",
        })
    out.sort(key=lambda r: r.get("at") or "")
    return out


def build_snapshot(root: Path, t: dt.datetime | None = None) -> dict:
    t = t or now_oslo()
    cfg = load_config(root)
    vault = cfg.get("vault") or ""
    enabled = bool(cfg.get("enabled")) and not vault.lower().endswith("deadbeef")
    base = {
        "exchange": "etherfi_cash", "captured_oslo": t.isoformat(timespec="seconds"),
        "ok": False, "configured": enabled, "label": cfg.get("label") or "ether.fi Cash",
        "vault": vault, "note": cfg.get("note"), "group": "spending",
        "tokens": [], "total_usdt": None, "topups": [], "possible_spends": [],
        "topups_usdt": 0.0, "possible_spends_usdt": 0.0,
        "spend_proxy_usdt": 0.0, "spend_proxy_method": None, "errors": {}, "notes": [],
    }
    if not enabled:
        base["reason"] = f"未啟用：編輯 {config_path(root)}（enabled=true + vault），見 etherfi_cash.example.json"
        return base
    try:
        toks = fetch_op_balances(vault)
        topups = fetch_arb_topups(vault)
        spends = fetch_op_possible_spends(vault)
    except CashError as e:
        base["errors"]["fetch"] = str(e)[:240]
        base["reason"] = str(e)[:240]
        return base
    total = sum(x["value_usdt"] or 0 for x in toks)
    unpriced = [x["symbol"] for x in toks if x.get("value_usdt") is None]
    top_sum = sum(x["usdt"] for x in topups)
    spend_sum = sum(x["usdt"] for x in spends)
    proxy, method = top_sum, "arb_usdc_topups"
    if top_sum <= 0 and spend_sum > 0:
        proxy, method = spend_sum, "op_usdc_outflows_ex_liquid"
    base.update({
        "ok": True, "tokens": toks, "total_usdt": total, "unpriced": unpriced,
        "topups": topups[-40:], "possible_spends": spends[-40:],
        "topups_usdt": top_sum, "possible_spends_usdt": spend_sum,
        "spend_proxy_usdt": proxy, "spend_proxy_method": method,
        "n_topups": len(topups), "n_possible_spends": len(spends),
        "notes": [
            "開銷帳戶：可花用的 float，不是長期投資倉；總覽「投資盈虧」不含此帳戶。",
            "餘額：Optimism EtherFiSafe 公開代幣列表（Blockscout）。",
            "儲值：Arbitrum 原生 USDC 轉入同一 vault／TopUp 地址（常見來自 MEXC）。",
            "卡片商戶明細沒有公開 API；里程碑以儲值合計為開銷代理（spend_proxy）。",
            "若日後取得 Cash App 匯出，可改為實際刷卡金額。",
        ],
    })
    return base


def write_snapshot(directory: Path, doc: dict, t: dt.datetime) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / f"etherfi_cash_snapshot_{stamp(t)}.json"
    p.write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
    return p


def latest_snapshot(roots: list[Path]):
    best = None
    for r in roots:
        if r.exists():
            for p in r.rglob("etherfi_cash_snapshot_*.json"):
                if not p.name.startswith("._") and (best is None or p.name > best.name):
                    best = p
    return (json.loads(best.read_text(encoding="utf-8")), best) if best else (None, None)


def archive_run(root: Path, progress: bool = True) -> dict:
    t = now_oslo()
    rep = {"captured_oslo": t.isoformat(timespec="seconds"), "root": str(root),
           "sources": {}, "errors": {}, "snapshot": None}
    load_config(root)
    snap = build_snapshot(root, t)
    if snap.get("ok"):
        sp = write_snapshot(root / t.strftime("%Y-%m") / "snapshots", snap, t)
        rep["snapshot"] = str(sp.relative_to(root))
    if snap.get("errors"):
        rep["errors"] = snap["errors"]
    if snap.get("reason") and not snap.get("ok"):
        rep["errors"]["config"] = snap["reason"]
    rep["total_usdt"] = snap.get("total_usdt")
    rep["topups_usdt"] = snap.get("topups_usdt")
    rep["spend_proxy_usdt"] = snap.get("spend_proxy_usdt")
    rep["configured"] = snap.get("configured")
    st: dict = {}
    try:
        st = json.loads((root / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    st["last_run"] = rep["captured_oslo"]
    st["last_total_usdt"] = snap.get("total_usdt")
    root.mkdir(parents=True, exist_ok=True)
    (root / "state.json").write_text(json.dumps(st, indent=1, ensure_ascii=False), encoding="utf-8")
    return rep


def analyze(project: Path, root: Path | None = None, offline: bool = False,
            investment_pnl: float | None = None) -> dict:
    root = root or project / "archive" / "etherfi_cash"
    try:
        cfg = load_config(root)
    except CashError:
        cfg = dict(DEFAULT_CONFIG)
    enabled = bool(cfg.get("enabled")) and not str(cfg.get("vault") or "").lower().endswith("deadbeef")
    snap, sp = latest_snapshot([root, project / "snapshots"])
    if not enabled and snap is None:
        return {"present": False, "configured": False, "group": "spending",
                "reason": "未設定 ether.fi Cash：複製 etherfi_cash.example.json → archive/etherfi_cash/config.json"}
    if snap is None:
        return {"present": False, "configured": enabled, "group": "spending",
                "reason": f"尚無快照：執行 {cli_cmd()} archive --exchange etherfi-cash"}
    if not offline and enabled:
        try:
            live = build_snapshot(root)
            if live.get("ok"):
                snap = live
        except CashError:
            pass
    total = snap.get("total_usdt")
    proxy = float(snap.get("spend_proxy_usdt") or 0.0)
    coverage = (investment_pnl / proxy) if (investment_pnl is not None and proxy > 0) else None
    return {
        "present": True, "configured": True, "group": "spending",
        "label": snap.get("label") or cfg.get("label"),
        "vault": snap.get("vault") or cfg.get("vault"),
        "snapshot_time": snap.get("captured_oslo"),
        "snapshot_file": str(sp.relative_to(project)) if sp else None,
        "total": total, "tokens": snap.get("tokens") or [],
        "topups_usdt": snap.get("topups_usdt") or 0.0,
        "possible_spends_usdt": snap.get("possible_spends_usdt") or 0.0,
        "spend_proxy_usdt": proxy,
        "spend_proxy_method": snap.get("spend_proxy_method"),
        "n_topups": snap.get("n_topups") or len(snap.get("topups") or []),
        "topups": snap.get("topups") or [],
        "possible_spends": snap.get("possible_spends") or [],
        "investment_pnl": investment_pnl,
        "coverage_ratio": coverage,
        "coverage_pct": (coverage * 100) if coverage is not None else None,
        "notes": snap.get("notes") or [],
        "errors": snap.get("errors") or {},
        "unpriced": snap.get("unpriced") or [],
    }
