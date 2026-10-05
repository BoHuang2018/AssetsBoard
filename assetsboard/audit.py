"""Audit: compare spot quantities vs a baseline, reconcile with bills, flag risky flows. GET only."""
from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict
from pathlib import Path

from . import flows as flows_mod
from . import holdings as hold
from .client import OSLO, SNAPSHOT_DIR, Client, cli_cmd, data, fnum, ms_to_oslo, now_oslo, ok

BASELINE_GLOB = "*_spot_qty.txt"
BASELINE_RES: dict[str, float] = {}  # coin -> rounding resolution of the loaded baseline value
_STAMP_RE = re.compile(r"(\d{4}-\d{2}-\d{2})T(\d{2})(\d{2})")
NOT_VISIBLE = (
    "雙幣投資（Dual Investment）明細（無讀取端點）",
    "Cash+ / 活期理財加強版明細",
    "PoolX / Launchpool / Launchpad 參與明細",
    "鎖倉（lock-up）與質押（staking / POS pledge）明細",
    "交易機器人／策略的個別明細（只看得到 bot-assets 總額）",
    "劃轉到 'uta' 之後的去向",
    "P2P、Bitget Card、法幣出入金",
)


# ---------- baseline ----------
def write_spot_qty(path: Path, qty: dict[str, float], captured: dt.datetime, note: str = "") -> None:
    lines = [
        f"# Bitget Classic spot quantities via /api/v2/spot/account/assets (assetType=hold_only)",
        f"# captured_oslo: {captured.isoformat(timespec='seconds')}",
    ]
    if note:
        lines.append(f"# {note}")
    for coin, q in sorted(qty.items(), key=lambda kv: kv[0].lower()):
        lines.append(f"{coin} {q!r}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def baseline_time(path: Path, text: str) -> dt.datetime | None:
    m = re.search(r"captured_oslo:\s*(\S+)", text)
    if m:
        try:
            t = dt.datetime.fromisoformat(m.group(1))
            return t if t.tzinfo else t.replace(tzinfo=OSLO)
        except ValueError:
            pass
    m = _STAMP_RE.search(path.name)
    if m:
        return dt.datetime.fromisoformat(f"{m.group(1)}T{m.group(2)}:{m.group(3)}").replace(tzinfo=OSLO)
    return None


def _resolution(num: str) -> float:
    """Half a unit in the last written decimal place (baseline files may be rounded)."""
    try:
        from decimal import Decimal
        exp = Decimal(num).as_tuple().exponent
        return 0.5 * 10 ** exp if isinstance(exp, int) and exp < 0 else 0.0
    except Exception:
        return 0.0


def load_baseline(path: Path) -> tuple[dict[str, float], dt.datetime]:
    text = path.read_text(encoding="utf-8")
    qty: dict[str, float] = {}
    BASELINE_RES.clear()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            qty[parts[0]] = fnum(parts[1])
            BASELINE_RES[parts[0]] = _resolution(parts[1])
    t = baseline_time(path, text)
    if t is None:
        raise SystemExit(f"無法判斷基準檔時間（檔名需含 YYYY-MM-DDTHHMM 或 '# captured_oslo:'）：{path}")
    return qty, t


def latest_baseline(directory: Path = SNAPSHOT_DIR) -> Path | None:
    cands = []
    for p in directory.glob(BASELINE_GLOB):
        m = _STAMP_RE.search(p.name)
        if m:
            cands.append((m.group(0), p))
    return max(cands)[1] if cands else None


# ---------- analysis ----------
def reconcile(base: dict[str, float], now: dict[str, float], bills: list[dict], since_ms: int) -> list[dict]:
    bill_sum: dict[str, float] = defaultdict(float)
    bill_n: dict[str, int] = defaultdict(int)
    bill_types: dict[str, set] = defaultdict(set)
    for b in bills:
        if int(b.get("cTime") or 0) >= since_ms:
            coin = str(b.get("coin"))
            # 'fees' is signed (negative) and not included in 'size' (e.g. BGB fee deduction rows)
            bill_sum[coin] += fnum(b.get("size")) + fnum(b.get("fees"))
            bill_n[coin] += 1
            bill_types[coin].add(str(b.get("businessType")))
    rows = []
    for coin in sorted(set(base) | set(now) | set(bill_sum), key=str.lower):
        before, after = base.get(coin, 0.0), now.get(coin, 0.0)
        delta = after - before
        bsum = bill_sum.get(coin, 0.0)
        tol = max(1e-8, 1e-9 * max(abs(before), abs(after))) + BASELINE_RES.get(coin, 0.0) * 1.01
        changed = abs(delta) > tol
        explained = abs(delta - bsum) <= max(tol, 1e-6 * abs(bsum))
        if not changed and not bill_n.get(coin):
            continue
        rows.append({
            "coin": coin, "before": before, "after": after, "delta": delta,
            "bills_sum": bsum, "bills_n": bill_n.get(coin, 0), "bill_types": sorted(bill_types.get(coin, ())),
            "changed": changed, "explained": explained,
            "status": ("APPEARED" if before == 0 and after else "GONE" if after == 0 and before else "CHANGED") if changed else "UNCHANGED",
        })
    return rows


def permissions(info_result: dict) -> dict:
    d = data(info_result, {}) or {}
    auths = [str(a) for a in (d.get("authorities") or [])]
    write = [a for a in auths if not a.endswith("r")]
    return {
        "ok": ok(info_result),
        "error": None if ok(info_result) else f"{info_result.get('code')} {info_result.get('msg')}",
        "authorities": auths,
        "write_authorities": write,
        "read_only": ok(info_result) and not write,
        "ip_whitelist_set": bool(str(d.get("ips") or "").strip()),
    }


def _near(ts: int, times: list[int], window_ms: int = 180_000) -> bool:
    return any(abs(ts - t) <= window_ms for t in times)


def flag_flows(f: dict, h: dict) -> list[tuple[str, str]]:
    """Return list of (level, message). level: '!!' 需處理, '?' 需人工確認, 'i' 資訊."""
    out: list[tuple[str, str]] = []
    own = hold.own_user_id(h)
    known = {own} | {s["userId"] for s in hold.subaccounts(h)}
    known.discard("")

    for w in f["withdrawals"]["rows"]:
        out.append(("!!", f"提幣 {ms_to_oslo(w.get('cTime'))} {w.get('coin')} {w.get('size')} 鏈={w.get('chain')} 狀態={w.get('status')} 目的={w.get('dest')}"))
    for b in f["spot_bills"]["rows"]:
        bt = str(b.get("businessType") or "").upper()
        if str(b.get("groupType")) == "withdraw" or "WITHDRAW" in bt:
            out.append(("!!", f"現貨帳單出現提幣類紀錄 {ms_to_oslo(b.get('cTime'))} {b.get('coin')} {b.get('size')} {bt}"))
    for x in f["convert"]["rows"]:
        out.append(("?", f"閃兌 {ms_to_oslo(x.get('ts') or x.get('cTime'))} {x.get('fromCoin')}->{x.get('toCoin')} {x.get('fromCoinSize')}->{x.get('toCoinSize')}"))
    for x in f["bgb_convert"]["rows"]:
        out.append(("?", f"小額兌換 BGB {ms_to_oslo(x.get('cTime') or x.get('ts'))} {x.get('fromCoin')} {x.get('fromAmount')}"))
    for d in f["deposits"]["rows"]:
        out.append(("i", f"充值 {ms_to_oslo(d.get('cTime'))} {d.get('coin')} {d.get('size')} 鏈={d.get('chain')} 類型={d.get('dest')} 狀態={d.get('status')}"))

    strat_times = [int(b.get("cTime") or 0) for k, v in f.items() if k.startswith("futures_bills_")
                   for b in v["rows"] if "strategy" in str(b.get("businessType") or "")]
    strat_times += [int(b.get("cTime") or 0) for b in f["spot_bills"]["rows"] if "STRATEGY" in str(b.get("businessType") or "")]
    for t in f["sub_main_transfers"]["rows"]:
        fu, tu = str(t.get("fromUserId") or ""), str(t.get("toUserId") or "")
        ts = int(t.get("ts") or 0)
        desc = f"{ms_to_oslo(ts)} {t.get('coin')} {t.get('size')} {t.get('fromType')}->{t.get('toType')} UID {fu}->{tu}"
        if (tu and tu not in known) or (fu and fu not in known):
            hint = "；同時間有策略/機器人劃轉帳單，疑似為機器人子帳戶" if _near(ts, strat_times) else "；找不到對應機器人帳單"
            out.append(("?", f"劃轉到非本人/非已知子帳戶 UID：{desc}{hint}"))
        elif str(t.get("toType")) == "uta":
            out.append(("?", f"劃轉到 'uta'（API 看不到後續去向）：{desc}"))
    seen_uta = {(t.get("ts"), t.get("size")) for t in f["sub_main_transfers"]["rows"] if str(t.get("toType")) == "uta"}
    for t in f["transfer_records"]["rows"]:
        if str(t.get("toType")) == "uta" and (t.get("ts"), t.get("size")) not in seen_uta:
            out.append(("?", f"劃轉到 'uta'（API 看不到後續去向）：{ms_to_oslo(t.get('ts'))} {t.get('coin')} {t.get('size')} {t.get('fromType')}->uta"))
    for o in f["spot_orders"]["rows"]:
        src = str(o.get("enterPointSource") or "").upper()
        if src in ("API", "SYS") or not src:
            out.append(("?", f"非網頁/App 下單來源={src or '未知'}：{ms_to_oslo(o.get('cTime'))} {o.get('symbol')} {o.get('side')} {o.get('status')}"))
    for k, v in f.items():
        for e in v.get("errors") or []:
            out.append(("?", f"讀取失敗 {k}: {e.get('code')} {e.get('msg')}"))
    return out


# ---------- runner ----------
def run(c: Client, since: dt.date, baseline_path: Path | None = None) -> dict:
    now = now_oslo()
    baseline_path = baseline_path or latest_baseline()
    base_qty, base_t = (load_baseline(baseline_path) if baseline_path else ({}, None))
    since_dt = dt.datetime.combine(since, dt.time(0, 0), OSLO)
    start_dt = min(since_dt, base_t) if base_t else since_dt
    start_ms, end_ms = int(start_dt.timestamp() * 1000), int(now.timestamp() * 1000)

    h = hold.collect(c, full=True)
    now_qty = hold.spot_quantities(h["spot_assets"])
    coins = set(now_qty) | set(base_qty) | set(hold.earn_assets(h))
    f = flows_mod.collect(c, start_ms, end_ms, coins)

    recon = reconcile(base_qty, now_qty, f["spot_bills"]["rows"], int(base_t.timestamp() * 1000)) if base_t else []
    perm = permissions(h["account_info"])
    flags = flag_flows(f, h)
    if not perm["ok"]:
        flags.insert(0, ("?", f"權限檢查失敗：{perm['error']}"))
    elif not perm["read_only"]:
        flags.insert(0, ("!!", f"API Key 具寫入權限：{', '.join(perm['write_authorities'])}"))
    if perm["ok"] and not perm["ip_whitelist_set"]:
        flags.insert(0, ("?", "API Key 未綁定 IP 白名單"))
    for r in recon:
        if r["changed"] and not r["explained"]:
            flags.insert(0, ("!!", f"數量變化無法由帳單解釋：{r['coin']} 變化 {r['delta']:+.10g}，帳單合計 {r['bills_sum']:+.10g}"))
    if not ok(h["spot_assets"]):
        flags.insert(0, ("!!", f"現貨資產讀取失敗：{h['spot_assets'].get('code')} {h['spot_assets'].get('msg')}"))

    return {
        "captured_oslo": now.isoformat(timespec="seconds"),
        "since": since.isoformat(),
        "window_oslo": [start_dt.isoformat(timespec="seconds"), now.isoformat(timespec="seconds")],
        "baseline": {"path": baseline_path.name if baseline_path else None,
                     "time_oslo": base_t.isoformat(timespec="minutes") if base_t else None},
        "mode": hold.mode(h),
        "spot_qty": now_qty,
        "reconciliation": recon,
        "permissions": perm,
        "flags": [{"level": lv, "msg": m} for lv, m in flags],
        "flows": f,
        "calls": c.calls,
    }


LEVEL_TAG = {"!!": "[需處理]", "?": "[需人工確認]", "i": "[資訊]"}


def render_summary(a: dict) -> str:
    L: list[str] = []
    L.append(f"Bitget 資產守衛（assetsboard）唯讀稽核  擷取時間 {a['captured_oslo']}（Oslo）")
    L.append(f"模式：{a['mode']}；查詢區間：{a['window_oslo'][0]} → {a['window_oslo'][1]}")
    b = a["baseline"]
    L.append(f"基準快照：{b['path'] or '（無）'}  時間 {b['time_oslo'] or '-'}")
    L.append("")
    p = a["permissions"]
    L.append("== API Key 權限 ==")
    if p["ok"]:
        L.append(f"  權限：{'唯讀 ✔' if p['read_only'] else '含寫入權限 ✘ ' + ', '.join(p['write_authorities'])}")
        L.append(f"  authorities：{', '.join(p['authorities']) or '(無)'}")
        L.append(f"  IP 白名單：{'已設定（內容不顯示）' if p['ip_whitelist_set'] else '未設定'}")
    else:
        L.append(f"  讀取失敗：{p['error']}")
    L.append("")

    L.append("== 現貨數量 vs 基準 ==")
    recon = a["reconciliation"]
    if not b["path"]:
        L.append(f"  （沒有基準檔，略過比對；先執行 `{cli_cmd()} snapshot`）")
    else:
        changed = [r for r in recon if r["changed"]]
        unchanged_n = len(set(a["spot_qty"]) - {r["coin"] for r in changed})
        L.append(f"  未變動幣種：{unchanged_n}；有變動：{len(changed)}")
        for r in changed:
            tag = "已由帳單解釋" if r["explained"] else "無法解釋 ✘"
            L.append(f"  {r['status']:9} {r['coin']}: {r['before']:.10g} → {r['after']:.10g}（{r['delta']:+.10g}）"
                     f" 帳單 {r['bills_n']} 筆合計 {r['bills_sum']:+.10g} → {tag}")
            if r["bill_types"]:
                L.append(f"            帳單類型：{', '.join(r['bill_types'])}")
        unexplained = [r for r in changed if not r["explained"]]
        L.append("  對帳結果：" + ("全部變動皆可由現貨帳單解釋。" if not unexplained else f"{len(unexplained)} 個幣種無法解釋！"))
    L.append("")

    f = a["flows"]
    L.append("== 資金流（查詢區間內）==")
    L.append(f"  提幣 {len(f['withdrawals']['rows'])}；充值 {len(f['deposits']['rows'])}；閃兌 {len(f['convert']['rows'])}；"
             f"BGB 小額兌換 {len(f['bgb_convert']['rows'])}；現貨訂單 {len(f['spot_orders']['rows'])}；成交 {len(f['spot_fills']['rows'])}")
    L.append(f"  劃轉紀錄 {len(f['sub_main_transfers']['rows'])}（主子帳戶）/ {len(f['transfer_records']['rows'])}（逐幣 transferRecords）；"
             f"現貨帳單 {len(f['spot_bills']['rows'])} 筆")
    types = defaultdict(int)
    for bill in f["spot_bills"]["rows"]:
        types[f"{bill.get('groupType')}/{bill.get('businessType')}"] += 1
    for k, n in sorted(types.items(), key=lambda kv: -kv[1]):
        L.append(f"    {k}: {n}")
    L.append("")

    L.append("== 警示 ==")
    flags = a["flags"]
    if not flags:
        L.append("  （無）")
    for fl in sorted(flags, key=lambda x: {"!!": 0, "?": 1, "i": 2}[x["level"]]):
        L.append(f"  {LEVEL_TAG[fl['level']]} {fl['msg']}")
    L.append("")
    errs = {n: c for n, c in a["calls"].items() if not ok(c.get("response")) and n != "uta_assets"}
    if errs:
        L.append("== 端點錯誤 ==")
        for n, c in errs.items():
            L.append(f"  {n} {c.get('path')}: {c['response'].get('code')} {c['response'].get('msg')}")
        L.append("")
    L.append("== API 看不到、請手動到 App/網頁確認 ==")
    for x in NOT_VISIBLE:
        L.append(f"  - {x}")
    L.append("")
    n_crit = sum(1 for fl in flags if fl["level"] == "!!")
    n_check = sum(1 for fl in flags if fl["level"] == "?")
    L.append(f"結論：{'未發現無法解釋的變動或提幣。' if n_crit == 0 else f'發現 {n_crit} 項需處理的警示！'}"
             f"{f' 另有 {n_check} 項需人工確認。' if n_check else ''}")
    return "\n".join(L) + "\n"
