"""Current holdings across Bitget Classic accounts (GET only)."""
from __future__ import annotations

from .client import Client, data, fnum, ok, result_list

FUTURES_TYPES = ("USDT-FUTURES", "COIN-FUTURES", "USDC-FUTURES")


def fetch_spot(c: Client) -> dict:
    return c.get("spot_assets", "/api/v2/spot/account/assets", {"assetType": "hold_only"})


def spot_quantities(spot_result: dict) -> dict[str, float]:
    """coin -> available+frozen+locked (nonzero only)."""
    out: dict[str, float] = {}
    for r in data(spot_result, []) or []:
        total = fnum(r.get("available")) + fnum(r.get("frozen")) + fnum(r.get("locked"))
        if total > 0:
            out[str(r.get("coin"))] = total
    return out


def collect(c: Client, full: bool = True) -> dict[str, dict]:
    """Fetch all holdings endpoints. Returns name -> raw response."""
    r: dict[str, dict] = {}
    r["uta_assets"] = c.get("uta_assets", "/api/v3/account/assets")  # Classic accounts return 40084
    r["spot_assets"] = fetch_spot(c)
    r["all_account_balance"] = c.get("all_account_balance", "/api/v2/account/all-account-balance")
    r["funding_assets"] = c.get("funding_assets", "/api/v2/account/funding-assets")
    for at in ("spot", "futures"):
        r[f"bot_assets_{at}"] = c.get(f"bot_assets_{at}", "/api/v2/account/bot-assets", {"accountType": at})
    for pt in FUTURES_TYPES:
        r[f"mix_accounts_{pt}"] = c.get(f"mix_accounts_{pt}", "/api/v2/mix/account/accounts", {"productType": pt})
        r[f"mix_positions_{pt}"] = c.get(f"mix_positions_{pt}", "/api/v2/mix/position/all-position", {"productType": pt})
        r[f"mix_orders_pending_{pt}"] = c.get(f"mix_orders_pending_{pt}", "/api/v2/mix/order/orders-pending", {"productType": pt})
    r["spot_open_orders"] = c.get("spot_open_orders", "/api/v2/spot/trade/unfilled-orders")
    if not full:
        return r
    r["margin_crossed_assets"] = c.get("margin_crossed_assets", "/api/v2/margin/crossed/account/assets")
    r["margin_isolated_assets"] = c.get("margin_isolated_assets", "/api/v2/margin/isolated/account/assets")
    r["earn_account_assets"] = c.get("earn_account_assets", "/api/v2/earn/account/assets")
    r["savings_account"] = c.get("savings_account", "/api/v2/earn/savings/account")
    for p in ("flexible", "fixed"):
        r[f"savings_assets_{p}"] = c.get(f"savings_assets_{p}", "/api/v2/earn/savings/assets", {"periodType": p})
    r["sharkfin_account"] = c.get("sharkfin_account", "/api/v2/earn/sharkfin/account")
    for s in ("subscribed", "settled"):
        r[f"sharkfin_assets_{s}"] = c.get(f"sharkfin_assets_{s}", "/api/v2/earn/sharkfin/assets", {"status": s})
    r["loan_ongoing"] = c.get("loan_ongoing", "/api/v2/earn/loan/ongoing-orders")
    r["subaccount_assets"] = c.get("subaccount_assets", "/api/v2/spot/account/subaccount-assets")
    r["account_info"] = c.get("account_info", "/api/v2/spot/account/info")
    return r


def mode(h: dict) -> str:
    return "UTA" if ok(h.get("uta_assets")) else "Classic"


def account_totals(h: dict) -> dict[str, float]:
    return {str(a.get("accountType")): fnum(a.get("usdtBalance")) for a in data(h.get("all_account_balance"), []) or []}


def positions(h: dict) -> list[dict]:
    out = []
    for pt in FUTURES_TYPES:
        for p in data(h.get(f"mix_positions_{pt}"), []) or []:
            out.append({"productType": pt, **p})
    return out


def open_orders(h: dict) -> dict[str, int]:
    out = {"spot": len(data(h.get("spot_open_orders"), []) or [])}
    for pt in FUTURES_TYPES:
        out[pt] = len(result_list(h.get(f"mix_orders_pending_{pt}"), "entrustedList"))
    return out


def futures_equity(h: dict) -> dict[str, float]:
    return {
        pt: sum(fnum(a.get("usdtEquity")) for a in data(h.get(f"mix_accounts_{pt}"), []) or [])
        for pt in FUTURES_TYPES
    }


def earn_assets(h: dict) -> dict[str, float]:
    return {str(a.get("coin")): fnum(a.get("amount")) for a in data(h.get("earn_account_assets"), []) or [] if fnum(a.get("amount"))}


def bot_assets(h: dict) -> list[dict]:
    out = []
    for at in ("spot", "futures"):
        for a in data(h.get(f"bot_assets_{at}"), []) or []:
            out.append({"accountType": at, "coin": a.get("coin"), "equity": fnum(a.get("equity")), "usdtValue": fnum(a.get("usdtValue"))})
    return out


def subaccounts(h: dict) -> list[dict]:
    out = []
    for s in data(h.get("subaccount_assets"), []) or []:
        coins = {str(a.get("coin")): fnum(a.get("available")) + fnum(a.get("frozen")) + fnum(a.get("locked")) for a in s.get("assetsList") or []}
        out.append({"userId": str(s.get("userId")), "coins": {k: v for k, v in coins.items() if v}})
    return out


def own_user_id(h: dict) -> str:
    return str((data(h.get("account_info"), {}) or {}).get("userId") or "")
