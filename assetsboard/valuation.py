"""Price spot holdings with public tickers and group them into sleeves."""
from __future__ import annotations

from collections import defaultdict

from .client import fnum, public_get

SLEEVES = ("rtoken", "crypto", "stock_plus", "stable")
SLEEVE_NAMES = {"rtoken": "rToken 美股代幣", "crypto": "加密貨幣", "stock_plus": "Stock+（…ON）", "stable": "穩定幣/法幣"}
STABLES = {"USDT", "USDC", "USD", "EUR", "FDUSD", "DAI"}
NOT_STOCK_PLUS = {"PYTHON", "BITTON", "PHOTON", "HORIZON", "AXION", "DRAGON", "CANTON", "BURNON"}


def fetch_tickers() -> dict[str, float]:
    """coin -> last price in USDT (or USD). Public endpoint, no auth."""
    res = public_get("/api/v2/spot/market/tickers")
    out: dict[str, float] = {}
    for t in res.get("data") or []:
        sym = str(t.get("symbol") or "").upper()
        px = fnum(t.get("lastPr") or t.get("close"))
        if px <= 0:
            continue
        if sym.endswith("USDT"):
            out.setdefault(sym[:-4], px)
        elif sym.endswith("USDC"):
            out.setdefault(sym[:-4], px)
        elif sym == "USDTEUR":
            out["EUR"] = 1.0 / px
    out["USDT"] = 1.0
    out.setdefault("USDC", 1.0)
    return out


def classify(coin: str) -> str:
    c = coin.upper()
    if c in STABLES:
        return "stable"
    if coin.startswith("r") and len(coin) > 1 and coin[1:].isalpha():
        return "rtoken"
    if c.endswith("ON") and len(c) >= 5 and c not in NOT_STOCK_PLUS:
        return "stock_plus"
    return "crypto"


def price_of(coin: str, tickers: dict[str, float]) -> float | None:
    return tickers.get(coin.upper(), tickers.get(coin))


def value_spot(qty: dict[str, float], tickers: dict[str, float]) -> dict:
    sleeves: dict[str, float] = defaultdict(float)
    rows = []
    missing = []
    for coin, q in qty.items():
        px = price_of(coin, tickers)
        usd = q * px if px is not None else 0.0
        if px is None:
            missing.append(coin)
        s = classify(coin)
        sleeves[s] += usd
        rows.append({"coin": coin, "qty": q, "price": px, "usdt": usd, "sleeve": s})
    total = sum(sleeves.values())
    return {
        "total_usdt": total,
        "sleeves": {s: {"usdt": sleeves.get(s, 0.0), "pct": (sleeves.get(s, 0.0) / total * 100) if total else 0.0} for s in SLEEVES},
        "rows": sorted(rows, key=lambda r: r["usdt"], reverse=True),
        "missing_price": sorted(missing),
    }
