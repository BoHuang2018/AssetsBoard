"""Robinhood (EU, EUR account) — manual, screenshot-fed module. Robinhood has no API for this account.

Ledger: archive/robinhood/ledger.csv (gitignored), one row per History line, transcribed from screenshots:
    date,type,asset,kind,qty,price_eur,amount_eur,qty_estimated,note
  type  : deposit | withdrawal | reward | buy | sell | dividend | fee | other
  kind  : stock (“… token buy/sell”) | crypto (“… market buy/sell”) | cash
  amount_eur : signed, exactly as shown in History (−15.01 for a buy, +14.94 for a sell)
Adding rows is idempotent (multiset de-dup on date/type/asset/qty/amount), so a batch of screenshots that overlaps
earlier ones can be re-imported safely:  `robinhood-add "2026-01-05 buy ACME 0.5 40.00 -20.02"` or
`robinhood-import rows.csv`.

Optional calibration from a Portfolio screenshot: archive/robinhood/portfolio.json (`robinhood-set-portfolio`).
Optional config: archive/robinhood/config.json  {"rewards_paid_as": "crypto" | "cash"}.

Valuation (estimate unless a fresher portfolio screenshot exists):
- stock tokens: qty × public US price (Yahoo, USD) ÷ EUR/USD (ECB via frankfurter, else Yahoo) → EUR
- crypto with known qty: Bitget public ticker (USDT) → EUR
- sign-up rewards whose coin is unknown: carried at the EUR value shown when received (placeholder, flagged)
- cash: deposits − withdrawals + sells + dividends − buys − fees (+ rewards if paid as cash)
PnL = value − net deposits (EUR). Converted to USDT at the current EUR rate for both value and net deposits,
so the % is the EUR % (FX moves do not create PnL).
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from .client import OSLO, cli_cmd, now_oslo

FIELDS = ("date", "type", "asset", "kind", "qty", "price_eur", "amount_eur", "qty_estimated", "note")
TYPES = ("deposit", "withdrawal", "reward", "buy", "sell", "dividend", "fee", "other")
KINDS = ("stock", "crypto", "cash")
KNOWN_CRYPTO = {"BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "AVAX", "LINK", "DOT", "LTC", "BCH", "UNI", "AAVE", "SHIB", "PEPE",
                "ARB", "OP", "SUI", "TON", "XLM", "USDC", "USDT", "POL", "XTZ", "COMP", "CRV", "WIF", "BONK"}
PORTFOLIO_FRESH_DAYS = 3


class LedgerError(ValueError):
    pass


def paths(root: Path) -> dict[str, Path]:
    return {"ledger": root / "ledger.csv", "portfolio": root / "portfolio.json", "config": root / "config.json",
            "prices": root / "prices.json"}


# ------------------------------------------------------------------ ledger I/O
def _num(x) -> float | None:
    if x is None:
        return None
    s = str(x).strip().replace("€", "").replace(",", "").replace("+", "")
    if s == "":
        return None
    return float(s)


def normalize(row: dict) -> dict:
    """Validate / canonicalize one ledger row (raises LedgerError)."""
    r = {k: (str(row.get(k)).strip() if row.get(k) is not None else "") for k in FIELDS}
    try:
        dt.date.fromisoformat(r["date"])
    except ValueError as e:
        raise LedgerError(f"日期格式錯誤（需 YYYY-MM-DD）：{r['date']!r}") from e
    t = r["type"].lower().replace("_", "-")
    if t in ("token-buy", "market-buy"):
        r["kind"] = r["kind"] or ("stock" if t.startswith("token") else "crypto"); t = "buy"
    elif t in ("token-sell", "market-sell"):
        r["kind"] = r["kind"] or ("stock" if t.startswith("token") else "crypto"); t = "sell"
    if t not in TYPES:
        raise LedgerError(f"未知類型：{r['type']!r}（可用：{', '.join(TYPES)}）")
    r["type"] = t
    r["asset"] = r["asset"].upper()
    amt = _num(r["amount_eur"])
    if amt is None:
        raise LedgerError("缺少金額 amount_eur")
    # sign convention from History: money in +, money out −
    if t in ("deposit", "reward", "sell", "dividend"):
        amt = abs(amt)
    elif t in ("withdrawal", "buy", "fee"):
        amt = -abs(amt)
    r["amount_eur"] = f"{amt:.2f}"
    for k in ("qty", "price_eur"):
        v = _num(r[k])
        r[k] = "" if v is None else f"{abs(v):.10g}"
    if t in ("buy", "sell"):
        if not r["asset"]:
            raise LedgerError("買賣需要 asset")
        if not r["kind"]:
            r["kind"] = "crypto" if r["asset"] in KNOWN_CRYPTO else "stock"
    elif t in ("deposit", "withdrawal", "fee"):
        r["kind"] = r["kind"] or "cash"
    elif t == "dividend":
        r["kind"] = r["kind"] or "stock"
    elif t == "reward":
        r["kind"] = r["kind"] or ("crypto" if r["asset"] and r["asset"] in KNOWN_CRYPTO or r["qty"] else "")
    if r["kind"] and r["kind"] not in KINDS:
        raise LedgerError(f"未知 kind：{r['kind']!r}（stock／crypto／cash）")
    r["qty_estimated"] = "1" if str(r["qty_estimated"]).lower() in ("1", "true", "yes", "y") else ""
    return r


def row_key(r: dict) -> tuple:
    q = _num(r.get("qty"))
    return (r["date"], r["type"], r.get("asset", ""), round(q, 8) if q is not None else None, r["amount_eur"])


def load_ledger(root: Path) -> list[dict]:
    p = paths(root)["ledger"]
    if not p.exists():
        return []
    with p.open(encoding="utf-8", newline="") as f:
        rows = [normalize(r) for r in csv.DictReader(f) if any((v or "").strip() for v in r.values())]
    return rows


def save_ledger(root: Path, rows: list[dict]) -> Path:
    p = paths(root)["ledger"]
    root.mkdir(parents=True, exist_ok=True)
    rows = sorted(rows, key=lambda r: r["date"])   # stable: keeps History order within a day
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=FIELDS, lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({k: r.get(k, "") for k in FIELDS})
    p.write_text(buf.getvalue(), encoding="utf-8")
    return p


def merge(existing: list[dict], new: list[dict]) -> tuple[list[dict], list[dict], int]:
    """Multiset merge: a key already present n times absorbs up to n identical new rows (e.g. 5 × €10 rewards)."""
    have = Counter(row_key(r) for r in existing)
    seen: Counter = Counter()
    added, dup = [], 0
    for r in new:
        k = row_key(r)
        seen[k] += 1
        if seen[k] <= have[k]:
            dup += 1
            continue
        added.append(r)
    return existing + added, added, dup


def parse_line(line: str) -> dict:
    """'2026-01-05 buy ACME 0.5 40.00 -20.02'  |  '2026-01-05 reward 10'  |  '2026-02-15 dividend ACME 0.03'
    '2026-02-02 market-sell XYZ 100 0.11 10.94'  |  '2026-01-05 deposit 100 note=bank'
    A trailing '~' on qty marks it as estimated (e.g. 0.12345~)."""
    note = ""
    if " note=" in line:
        line, note = line.split(" note=", 1)
    tok = line.split()
    if len(tok) < 3:
        raise LedgerError(f"格式：DATE TYPE [ASSET] [QTY PRICE] AMOUNT —— {line!r}")
    date, typ, rest = tok[0], tok[1].lower(), tok[2:]
    row = {"date": date, "type": typ, "note": note.strip()}
    base = typ.split("-")[-1]
    if base in ("buy", "sell"):
        if len(rest) != 4:
            raise LedgerError(f"買賣需要 ASSET QTY PRICE AMOUNT：{line!r}")
        row.update(asset=rest[0], qty=rest[1].rstrip("~"), price_eur=rest[2], amount_eur=rest[3],
                   qty_estimated="1" if rest[1].endswith("~") else "")
    elif base in ("dividend",):
        row.update(asset=rest[0], amount_eur=rest[-1])
    elif base == "reward" and len(rest) >= 3:          # reward ASSET QTY AMOUNT (coin known)
        row.update(asset=rest[0], qty=rest[1], amount_eur=rest[-1], kind="crypto")
    else:
        if len(rest) == 2:
            row.update(asset=rest[0], amount_eur=rest[1])
        else:
            row.update(amount_eur=rest[-1])
    return normalize(row)


def import_csv_text(text: str) -> list[dict]:
    rdr = csv.DictReader(io.StringIO(text))
    missing = {"date", "type", "amount_eur"} - set(rdr.fieldnames or [])
    if missing:
        raise LedgerError(f"CSV 缺少欄位：{', '.join(sorted(missing))}（欄位：{', '.join(FIELDS)}）")
    return [normalize(r) for r in rdr if any((v or "").strip() for v in r.values())]


def add_rows(root: Path, new: list[dict]) -> tuple[list[dict], int]:
    rows, added, dup = merge(load_ledger(root), new)
    if added:
        save_ledger(root, rows)
    return added, dup


# ------------------------------------------------------------------ config / portfolio
def load_config(root: Path) -> dict:
    p = paths(root)["config"]
    cfg = {"rewards_paid_as": "crypto"}
    if p.exists():
        try:
            cfg.update(json.loads(p.read_text(encoding="utf-8")))
        except ValueError:
            pass
    return cfg


def load_portfolio(root: Path) -> dict | None:
    p = paths(root)["portfolio"]
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return None


def save_portfolio(root: Path, total_eur: float | None, cash_eur: float | None = None, crypto_eur: float | None = None,
                   positions: dict | None = None, at: str | None = None) -> Path:
    p = paths(root)["portfolio"]
    if total_eur is None:
        p.unlink(missing_ok=True)
        return p
    root.mkdir(parents=True, exist_ok=True)
    d = {"at": at or now_oslo().isoformat(timespec="minutes"), "total_eur": total_eur, "cash_eur": cash_eur,
         "crypto_eur": crypto_eur, "positions": positions or {}, "source": "Robinhood Portfolio screenshot (manual)"}
    p.write_text(json.dumps(d, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return p


# ------------------------------------------------------------------ prices
def _get_json(url: str, timeout: int = 15):
    import urllib.request
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (assetsboard)"}), timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:  # noqa: BLE001
        return None


def yahoo_usd(sym: str) -> float | None:
    j = _get_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=5d")
    try:
        m = j["chart"]["result"][0]["meta"]
        return float(m["regularMarketPrice"]) if str(m.get("currency", "USD")).upper() == "USD" else None
    except (TypeError, KeyError, IndexError, ValueError):
        return None


def fetch_prices(stocks: list[str], cryptos: list[str], cache_path: Path | None = None, offline: bool = False) -> dict:
    """{'eur_usd': x, 'fx_source': str, 'usdt_per_usd': y, 'usd': {SYM: usd}, 'src': {SYM: str}, 'at': iso}."""
    cache = {}
    if cache_path and cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except ValueError:
            cache = {}
    if offline:
        return cache or {"eur_usd": None, "usd": {}, "src": {}, "fx_source": "離線（無快取）", "usdt_per_usd": 1.0}
    from . import ibkr
    eur_usd, fx_src = ibkr.usd_per("EUR")
    out = {"eur_usd": eur_usd or cache.get("eur_usd"), "fx_source": fx_src if eur_usd else (cache.get("fx_source", "") + "（快取）"),
           "usdt_per_usd": ibkr.usdt_per_usd(), "usd": dict(cache.get("usd") or {}), "src": dict(cache.get("src") or {}),
           "at": now_oslo().isoformat(timespec="minutes")}
    for s in stocks:
        p = yahoo_usd(s)
        if p:
            out["usd"][s] = p; out["src"][s] = "Yahoo（美股 USD）"
        elif s in out["usd"]:
            out["src"][s] = "快取"
    if cryptos:
        from . import valuation as val
        try:
            tk = val.fetch_tickers()
        except Exception:  # noqa: BLE001
            tk = {}
        for c in cryptos:
            if tk.get(c):
                out["usd"][c] = tk[c] / (out["usdt_per_usd"] or 1.0); out["src"][c] = "Bitget 公開行情"
    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
    return out


# ------------------------------------------------------------------ analysis
def derive(rows: list[dict], rewards_paid_as: str = "crypto") -> dict:
    """Pure: holdings (average cost, EUR), cash estimate, flows, implied fees, unknowns. No prices."""
    pos: dict = defaultdict(lambda: {"qty": 0.0, "cost": 0.0, "kind": "", "realized": 0.0, "qty_estimated": False,
                                     "buys": 0, "sells": 0, "unknown_cost_qty": 0.0})
    cash = 0.0; dep = wd = rew = div = fee_rows = implied_fees = 0.0
    rewards_unknown: list[dict] = []
    unknown_sells: list[dict] = []
    for r in rows:
        t, a, amt = r["type"], r["asset"], float(r["amount_eur"])
        q = _num(r["qty"]) or 0.0; px = _num(r["price_eur"])
        if t == "deposit":
            dep += amt; cash += amt
        elif t == "withdrawal":
            wd += -amt; cash += amt
        elif t == "dividend":
            div += amt; cash += amt
        elif t == "fee":
            fee_rows += -amt; cash += amt
        elif t == "reward":
            rew += amt
            if a and q:
                p = pos[a]; p["kind"] = r["kind"] or "crypto"; p["qty"] += q          # cost basis 0 (free)
            elif rewards_paid_as == "cash" or r["kind"] == "cash":
                cash += amt
            else:
                rewards_unknown.append({"date": r["date"], "eur": amt})
        elif t in ("buy", "sell"):
            p = pos[a]; p["kind"] = p["kind"] or r["kind"]; p["qty_estimated"] |= bool(r["qty_estimated"])
            if px and q:
                implied_fees += (-amt - q * px) if t == "buy" else (q * px - amt)
            cash += amt
            if t == "buy":
                p["qty"] += q; p["cost"] += -amt; p["buys"] += 1
            else:
                p["sells"] += 1
                held = max(p["qty"], 0.0)
                take = min(q, held)
                avg = p["cost"] / held if held > 0 else 0.0
                missing = q - take
                if missing > 1e-12:            # sold more than the ledger ever delivered → cost unknown
                    p["unknown_cost_qty"] += missing
                    unknown_sells.append({"date": r["date"], "asset": a, "qty": missing, "eur": amt * missing / q})
                p["realized"] += amt - avg * take      # the unknown-cost part has cost 0 (reward)
                p["cost"] -= avg * take; p["qty"] -= take
        elif t == "other":
            cash += amt
    # crypto sold without a visible acquisition: most likely the sign-up rewards (coin unknown in History)
    sold_unknown_assets = sorted({u["asset"] for u in unknown_sells})
    n_left = max(0, len(rewards_unknown) - len(sold_unknown_assets)) if rewards_paid_as != "cash" else 0
    left_eur = sum(x["eur"] for x in rewards_unknown[-n_left:]) if n_left else 0.0
    holdings = []
    for a, p in sorted(pos.items()):
        if p["qty"] > 1e-9:
            holdings.append({"asset": a, "kind": p["kind"], "qty": p["qty"], "cost_eur": p["cost"],
                             "avg_cost_eur": p["cost"] / p["qty"], "realized_eur": p["realized"], "qty_estimated": p["qty_estimated"]})
    realized = {a: p["realized"] for a, p in pos.items() if abs(p["realized"]) > 0.004}
    kinds = {a: p["kind"] for a, p in pos.items()}
    return {"holdings": holdings, "cash_eur": cash, "deposits_eur": dep, "withdrawals_eur": wd, "net_deposits_eur": dep - wd,
            "rewards_eur": rew, "dividends_eur": div, "fees_eur": fee_rows, "implied_fees_eur": implied_fees,
            "rewards_unknown": rewards_unknown, "unknown_cost_sells": unknown_sells, "sold_unknown_assets": sold_unknown_assets,
            "unknown_reward_coins_left": n_left, "kinds": kinds, "unknown_reward_coins_left_eur": left_eur, "realized_eur": realized,
            "cash_if_rewards_cash_eur": cash + (sum(x["eur"] for x in rewards_unknown) if rewards_paid_as != "cash" else 0.0)}


def analyze(project: Path, root: Path | None = None, offline: bool = False) -> dict:
    root = root or project / "archive" / "robinhood"
    try:
        rows = load_ledger(root)
    except (LedgerError, ValueError) as e:
        return {"present": False, "configured": True, "reason": f"Robinhood ledger 格式錯誤：{e}"[:300]}
    if not rows:
        return {"present": False, "configured": False,
                "reason": f"尚無 Robinhood 紀錄：把 History 截圖轉成 {cli_cmd()} robinhood-add \"YYYY-MM-DD buy SYM QTY PRICE AMOUNT\""}
    cfg = load_config(root)
    d = derive(rows, cfg.get("rewards_paid_as", "crypto"))
    stocks = [h["asset"] for h in d["holdings"] if h["kind"] == "stock"]
    cryptos = [h["asset"] for h in d["holdings"] if h["kind"] == "crypto"]
    P = fetch_prices(stocks, cryptos, paths(root)["prices"], offline=offline)
    eur_usd = P.get("eur_usd")
    stock_eur = crypto_eur = 0.0; unpriced = []
    for h in d["holdings"]:
        usd = (P.get("usd") or {}).get(h["asset"])
        if usd and eur_usd:
            h["price_eur"] = usd / eur_usd; h["price_src"] = (P.get("src") or {}).get(h["asset"])
            h["value_eur"] = h["qty"] * h["price_eur"]; h["unrealized_eur"] = h["value_eur"] - h["cost_eur"]
            h["unrealized_pct"] = h["unrealized_eur"] / h["cost_eur"] * 100 if h["cost_eur"] > 0 else None
        else:
            h["price_eur"] = None; h["value_eur"] = h["cost_eur"]; h["unrealized_eur"] = None; h["price_src"] = "無報價（以成本計）"
            unpriced.append(h["asset"])
        if h["kind"] == "crypto":
            crypto_eur += h["value_eur"]
        else:
            stock_eur += h["value_eur"]
    crypto_eur += d["unknown_reward_coins_left_eur"]
    est_total = stock_eur + crypto_eur + d["cash_eur"]
    last_date = max(r["date"] for r in rows)
    pf = load_portfolio(root)
    use_pf = False
    if pf and pf.get("total_eur") is not None:
        pf_date = str(pf.get("at", ""))[:10]
        age = (now_oslo().date() - dt.date.fromisoformat(pf_date)).days if pf_date else 999
        pf["age_days"] = age
        pf["stale"] = pf_date < last_date or age > PORTFOLIO_FRESH_DAYS
        use_pf = not pf["stale"]
    total_eur = float(pf["total_eur"]) if use_pf else est_total
    net = d["net_deposits_eur"]
    pnl_eur = total_eur - net
    rate = (eur_usd or 0.0) * (P.get("usdt_per_usd") or 1.0)        # USDT per EUR
    crypto_part = (float(pf["crypto_eur"]) if use_pf and pf.get("crypto_eur") is not None else crypto_eur)
    stock_cost = sum(h["cost_eur"] for h in d["holdings"] if h["kind"] == "stock")
    stock_real = sum(v for a, v in d["realized_eur"].items() if d["kinds"].get(a) == "stock")
    unk_proceeds = sum(-u["eur"] if u["eur"] < 0 else u["eur"] for u in d["unknown_cost_sells"])
    n_sold_rewards = len(d["sold_unknown_assets"]) if cfg.get("rewards_paid_as", "crypto") != "cash" else 0
    sold_receipt = sum(x["eur"] for x in d["rewards_unknown"][:n_sold_rewards])
    breakdown = {"rewards_received_eur": d["rewards_eur"], "reward_crypto_sold_vs_receipt_eur": unk_proceeds - sold_receipt if n_sold_rewards else unk_proceeds,
                 "stock_realized_eur": stock_real, "stock_unrealized_eur": stock_eur - stock_cost, "dividends_eur": d["dividends_eur"]}
    breakdown["other_eur"] = (total_eur - net) - sum(breakdown.values())   # screenshot vs estimate, crypto price moves, rounding
    unknowns = []
    if d["sold_unknown_assets"]:
        unknowns.append(f"加密貨幣賣出但 History 沒有買入：{', '.join(d['sold_unknown_assets'])} —— 推測來自 sign-up reward（History 只顯示 €），成本視為 0（獎勵），已實現收益＝賣出所得。")
    if d["unknown_reward_coins_left"]:
        unknowns.append(f"還有 {d['unknown_reward_coins_left']} 筆 sign-up reward 的幣種未知（可能仍持有），暫以領取時 €{d['unknown_reward_coins_left_eur']:.2f} 計入，非市價。")
    est = [f"{r['date']} {r['type']} {r['asset']} 數量 {r['qty']}（推算：截圖被遮住）" for r in rows if r["qty_estimated"]]
    if est:
        unknowns.append("推算的數量：" + "；".join(est))
    if unpriced:
        unknowns.append(f"查不到報價、以成本計：{', '.join(unpriced)}")
    if not use_pf:
        unknowns.append("目前價值是估算（公開美股價 × ECB 匯率；Robinhood 的代幣報價可能略有價差）。傳一張 Portfolio 截圖可校準。")
    return {
        "present": bool(rate), "configured": True,
        "reason": None if rate else "查不到 EUR→USD 匯率，無法併入合計",
        "currency": "EUR", "ledger_rows": len(rows), "first_date": min(r["date"] for r in rows), "last_date": last_date,
        "ledger": [{**r, "amount_eur": float(r["amount_eur"])} for r in reversed(rows)],
        **{k: d[k] for k in ("cash_eur", "deposits_eur", "withdrawals_eur", "net_deposits_eur", "rewards_eur", "dividends_eur",
                             "fees_eur", "implied_fees_eur", "realized_eur", "unknown_cost_sells", "rewards_unknown",
                             "unknown_reward_coins_left", "unknown_reward_coins_left_eur", "cash_if_rewards_cash_eur")},
        "holdings": d["holdings"], "stock_eur": stock_eur, "crypto_eur": crypto_eur,
        "est_total_eur": est_total, "total_eur": total_eur, "value_method": "portfolio_screenshot" if use_pf else "estimate",
        "portfolio": pf, "pnl_eur": pnl_eur, "pnl_breakdown": breakdown,
        "pnl_ex_rewards_eur": pnl_eur - d["rewards_eur"], "pnl_pct": pnl_eur / net * 100 if net > 0 else None,
        "eur_usd": eur_usd, "fx_source": P.get("fx_source"), "usdt_per_eur": rate, "prices_at": P.get("at"),
        "total": total_eur * rate, "net_inflow": net * rate, "pnl": pnl_eur * rate,
        "split": {"securities": (total_eur - crypto_part) * rate, "crypto": crypto_part * rate},
        "rewards_paid_as": cfg.get("rewards_paid_as", "crypto"),
        "snapshot_time": (pf.get("at") if use_pf else P.get("at")) or last_date,
        "unknowns": unknowns,
    }
