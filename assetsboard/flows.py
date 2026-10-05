"""Historical flows (bills, deposits, withdrawals, converts, trades, transfers). GET only."""
from __future__ import annotations

from .client import Client, data, ok, result_list

MAX_WINDOW_MS = 89 * 24 * 3600 * 1000  # most Bitget history endpoints allow <= 90 days per query
COMMON_COINS = ("USDT", "USDC", "BTC", "ETH", "BGB")


def windows(start_ms: int, end_ms: int):
    s = start_ms
    while s < end_ms:
        e = min(end_ms, s + MAX_WINDOW_MS)
        yield s, e
        s = e


def paged(c: Client, name: str, path: str, base_params: dict, start_ms: int, end_ms: int,
          id_field: str, list_keys: tuple = (), limit: int = 100, cursor_param: str = "idLessThan",
          max_pages: int = 60) -> dict:
    """Page through a time-ranged list endpoint. Returns {'rows': [...], 'errors': [...], 'pages': n}."""
    rows, errors, pages = [], [], 0
    for ws, we in windows(start_ms, end_ms):
        cursor = None
        for _ in range(max_pages):
            p = dict(base_params, startTime=ws, endTime=we, limit=limit)
            if cursor:
                p[cursor_param] = cursor
            r = c.get(f"{name}_p{pages}", path, p, record=False)
            pages += 1
            if not ok(r):
                errors.append({"code": r.get("code"), "msg": r.get("msg"), "params": {k: v for k, v in p.items()}})
                break
            batch = result_list(r, *list_keys) if list_keys else (data(r, []) if isinstance(data(r), list) else result_list(r))
            rows.extend(batch)
            if len(batch) < limit:
                break
            nxt = batch[-1].get(id_field)
            d = data(r)
            if isinstance(d, dict) and d.get("endId"):
                nxt = d.get("endId")
            if not nxt or nxt == cursor:
                break
            cursor = nxt
    c.calls[name] = {"path": path, "params": dict(base_params, startTime=start_ms, endTime=end_ms),
                     "paged": True, "pages": pages, "rows": len(rows), "errors": errors,
                     "response": {"code": "00000" if not errors else str(errors[0]["code"]), "msg": "paged" if not errors else errors[0]["msg"]}}
    return {"rows": rows, "errors": errors, "pages": pages}


def collect(c: Client, start_ms: int, end_ms: int, coins: set[str] | None = None) -> dict:
    f: dict = {}
    f["spot_bills"] = paged(c, "spot_bills", "/api/v2/spot/account/bills", {}, start_ms, end_ms, "billId", limit=500)
    f["deposits"] = paged(c, "deposits", "/api/v2/spot/wallet/deposit-records", {}, start_ms, end_ms, "orderId")
    f["withdrawals"] = paged(c, "withdrawals", "/api/v2/spot/wallet/withdrawal-records", {}, start_ms, end_ms, "orderId")
    f["convert"] = paged(c, "convert_records", "/api/v2/convert/convert-record", {}, start_ms, end_ms, "id", ("dataList",))
    f["bgb_convert"] = paged(c, "bgb_convert_records", "/api/v2/convert/bgb-convert-records", {}, start_ms, end_ms, "orderId")
    f["spot_fills"] = paged(c, "spot_fills", "/api/v2/spot/trade/fills", {}, start_ms, end_ms, "tradeId")
    f["spot_orders"] = paged(c, "spot_history_orders", "/api/v2/spot/trade/history-orders", {}, start_ms, end_ms, "orderId")
    f["sub_main_transfers"] = paged(c, "sub_main_trans_record", "/api/v2/spot/account/sub-main-trans-record", {}, start_ms, end_ms, "transferId")
    for p in ("flexible", "fixed"):
        f[f"savings_records_{p}"] = paged(c, f"savings_records_{p}", "/api/v2/earn/savings/records", {"periodType": p}, start_ms, end_ms, "orderId", ("resultList",))
    f["sharkfin_records"] = paged(c, "sharkfin_records", "/api/v2/earn/sharkfin/records", {}, start_ms, end_ms, "orderId", ("resultList",))
    f["margin_crossed_financial"] = paged(c, "margin_crossed_financial", "/api/v2/margin/crossed/financial-records", {}, start_ms, end_ms, "marginId", ("resultList",))
    for pt in ("USDT-FUTURES", "COIN-FUTURES", "USDC-FUTURES"):
        f[f"futures_bills_{pt}"] = paged(c, f"futures_bills_{pt}", "/api/v2/mix/account/bill", {"productType": pt}, start_ms, end_ms, "billId", ("bills",))
    # transferRecords requires a coin param -> iterate
    tr_rows, tr_err = [], {}
    for coin in sorted((coins or set()) | set(COMMON_COINS)):
        res = paged(c, f"transferRecords_{coin}", "/api/v2/spot/account/transferRecords", {"coin": coin}, start_ms, end_ms, "transferId")
        c.calls.pop(f"transferRecords_{coin}", None)
        tr_rows.extend(res["rows"])
        if res["errors"]:
            tr_err[coin] = res["errors"][0]
    c.calls["transferRecords_per_coin"] = {"path": "/api/v2/spot/account/transferRecords", "coins": len((coins or set()) | set(COMMON_COINS)),
                                           "rows": len(tr_rows), "errors": tr_err,
                                           "response": {"code": "00000" if not tr_err else "PARTIAL", "msg": f"{len(tr_err)} coin errors"}}
    f["transfer_records"] = {"rows": tr_rows, "errors": list(tr_err.values()), "pages": None}
    return f
