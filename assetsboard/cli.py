"""Command line interface: python3 -m assetsboard <command>."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from xml.etree.ElementTree import ParseError as ET_ParseError

from . import __version__
from . import audit as audit_mod
from . import holdings as hold
from . import analysis as analysis_mod
from . import archive as archive_mod
from . import pnl as pnl_mod
from . import snapshot as snapshot_mod
from . import valuation as val
from . import mexc as mexc_mod
from . import kraken as kraken_mod
from . import ibkr as ibkr_mod
from . import cryptocom as cdc_mod
from . import wallets as wallets_mod
from . import etherfi_cash as efc_mod
from .client import SNAPSHOT_DIR, Client, cli_cmd, credentials_available, fnum, now_oslo, ok, stamp


def _client(args) -> Client:
    return Client(verbose=getattr(args, "verbose", False))


def _which(args) -> tuple[bool, bool, bool]:
    """(run bitget, run mexc, run kraken) for --exchange all|bitget|mexc|kraken|ibkr. 'all' skips an exchange without credentials.
    IBKR is handled separately by _run_ibkr (it needs the local IB Gateway, not API keys)."""
    ex = getattr(args, "exchange", "bitget") or "bitget"
    if ex in ("bitget", "mexc", "kraken", "ibkr", "cryptocom", "wallets", "etherfi-cash"):
        return ex == "bitget", ex == "mexc", ex == "kraken"
    b, m, k = credentials_available(), mexc_mod.available(), kraken_mod.available()
    if not b:
        print(f"Bitget 未設定憑證，略過（{cli_cmd()} setup-keychain）", file=sys.stderr)
    if not m:
        print(mexc_mod.no_creds_msg(), file=sys.stderr)
    if not k:
        print(kraken_mod.no_creds_msg(), file=sys.stderr)
    return b, m, k


def _run_ibkr(args) -> bool:
    ex = getattr(args, "exchange", "bitget") or "bitget"
    if ex == "ibkr":
        return True
    if ex == "all" and ibkr_mod.available():
        return True
    if ex == "all":
        print("IBKR：IB Gateway 未開啟（127.0.0.1:%d），略過" % ibkr_mod.port(), file=sys.stderr)
    return False



def _run_wallets(args) -> bool:
    ex = getattr(args, "exchange", "bitget") or "bitget"
    return ex in ("wallets", "all")


def _run_etherfi(args) -> bool:
    ex = getattr(args, "exchange", "bitget") or "bitget"
    return ex in ("etherfi-cash", "all")


def _wallets_print(d: dict) -> None:
    print(f"\n=== 鏈上錢包（唯讀，公開 RPC）  {d['captured_oslo'][:16].replace('T', ' ')} Oslo ===")
    for w in d.get("wallets") or []:
        if w.get("error"):
            print(f"  {w.get('label')}: 失敗 — {w['error']}")
            continue
        toks = "、".join(f"{t['symbol']} {t['amount']:.8g}" + (f" ≈ {_fmt(t['value_usdt'])} USDT" if t.get("value_usdt") is not None else "")
                         for t in w.get("tokens") or []) or "（空）"
        print(f"  {w.get('label')}（{w.get('chain')}）  {toks}  合計 ≈ {_fmt(w.get('value_usdt') or 0)} USDT")
        print(f"    {w.get('address')}")
    print(f"合計 ≈ {_fmt(d.get('total_usdt') or 0)} USDT")
    if d.get("errors"):
        print("錯誤：" + "; ".join(f"{k}: {v}" for k, v in d["errors"].items()))


def _run_cdc(args) -> bool:
    ex = getattr(args, "exchange", "bitget") or "bitget"
    if ex == "cryptocom":
        if not cdc_mod.available():
            print(cdc_mod.no_creds_msg(), file=sys.stderr)
            return False
        return True
    if ex == "all":
        if cdc_mod.available():
            return True
        print(cdc_mod.no_creds_msg(), file=sys.stderr)
    return False


def _cdc_print(d: dict) -> None:
    print(f"\n=== Crypto.com App（唯讀，Agent API Key）  {d['captured_oslo'][:16].replace('T', ' ')} Oslo ===")
    for h in d["holdings"]:
        al = "、".join(f"{k} {v:.8g}" for k, v in h["allocation"].items())
        print(f"  {h['coin']:8} 總量 {h['qty_total']:.10g}（錢包 {h['balance']:.10g}" + (f"，{al}" if al else "") + "）"
              + (f"  ≈ {_fmt(h['value_usdt'])} USDT" if h["value_usdt"] is not None else "  （無報價）"))
    for f in d["fiat"]:
        print(f"  法幣 {f['currency']} {f['amount']:.2f}" + (f"  ≈ {_fmt(f['usdt'])} USDT" if f["usdt"] is not None else ""))
    if d["products"]:
        print("產品：" + "、".join(f"{p['name']} {p['value_native']:.2f} {p.get('currency') or ''}" for p in d["products"]))
    print(f"合計 ≈ {_fmt(d['total_usdt'] or 0)} USDT（{'portfolio 產品市值' if d['total_source'] == 'portfolio' else '錢包 × 公開行情'}；"
          f"錢包估算 {_fmt(d['holdings_usdt'] or 0)}）；API 呼叫 {d['calls']} 次")
    if d["unpriced"]:
        print("無報價：" + ", ".join(d["unpriced"]))
    if d["errors"]:
        print("讀取失敗：" + "; ".join(f"{k}: {v}" for k, v in d["errors"].items()))


def _cdc_snapshot(args, write: bool) -> int:
    root = SNAPSHOT_DIR.parent / "archive" / "cryptocom"
    c = cdc_mod.CdcClient(verbose=args.verbose)
    t = now_oslo()
    try:
        d = cdc_mod.build_snapshot(c, t)
    except cdc_mod.CdcError as e:
        cdc_mod.note_key(root, c.fingerprint(), False, e)
        print(f"Crypto.com：{e}", file=sys.stderr)
        return 2 if e.auth else 1
    cdc_mod.note_key(root, c.fingerprint(), d["ok"])
    _cdc_print(d)
    w = cdc_mod.key_status(root).get("warning")
    if w:
        print("⚠ " + w)
    if write and d["ok"]:
        p = cdc_mod.write_snapshot(SNAPSHOT_DIR, d, t)
        print(f"已寫入：{p.relative_to(SNAPSHOT_DIR.parent)}（Crypto.com ≈ {_fmt(d['total_usdt'] or 0)} USDT）")
        if cdc_mod.load_baseline(root) is None and d.get("total_usdt") is not None:
            cdc_mod.save_baseline(root, d)
            print(f"已建立 Crypto.com 盈虧基準：{d['captured_oslo'][:16]}（{_fmt(d['total_usdt'])} USDT）")
    return 0 if d["ok"] else 1



def cmd_set_wallets_baseline(args) -> int:
    root = SNAPSHOT_DIR.parent / "archive" / "wallets"
    if args.action == "clear":
        wallets_mod.save_baseline(root, None, clear=True)
        print("已清除鏈上錢包盈虧基準")
        return 0
    if args.action == "latest":
        snap, _ = wallets_mod.latest_snapshot([root, SNAPSHOT_DIR])
        if not snap:
            print("沒有鏈上錢包快照", file=sys.stderr); return 1
    else:
        snap = wallets_mod.build_snapshot(root)
        if not snap.get("ok"):
            print("讀取失敗：" + "; ".join(f"{k}:{v}" for k, v in (snap.get("errors") or {}).items()), file=sys.stderr)
            return 1
        t = now_oslo()
        wallets_mod.write_snapshot(root / t.strftime("%Y-%m") / "snapshots", snap, t)
    b = wallets_mod.save_baseline(root, snap)
    print(f"鏈上錢包盈虧基準：{b['at'][:16]} → {_fmt(b['value_usdt'])} USDT")
    return 0


def cmd_set_cryptocom_baseline(args) -> int:
    root = SNAPSHOT_DIR.parent / "archive" / "cryptocom"
    if args.action == "clear":
        cdc_mod.save_baseline(root, None, clear=True)
        print("已清除 Crypto.com 基準（下一次成功的快照會自動重建）。")
        return 0
    if args.action == "latest":
        snap, _ = cdc_mod.latest_snapshot([root, SNAPSHOT_DIR])
        if snap is None:
            print("沒有 Crypto.com 快照。", file=sys.stderr)
            return 1
    else:
        if not cdc_mod.available():
            print(cdc_mod.no_creds_msg(), file=sys.stderr)
            return 2
        t = now_oslo()
        c = cdc_mod.CdcClient(verbose=args.verbose)
        try:
            snap = cdc_mod.build_snapshot(c, t)
        except cdc_mod.CdcError as e:
            print(f"Crypto.com：{e}", file=sys.stderr)
            return 2 if e.auth else 1
        cdc_mod.write_snapshot(root / t.strftime("%Y-%m") / "snapshots", snap, t)
    if snap.get("total_usdt") is None:
        print("快照沒有總額，無法設定基準。", file=sys.stderr)
        return 1
    b = cdc_mod.save_baseline(root, snap)
    print(f"Crypto.com 基準：{b['at'][:16].replace('T', ' ')}　{_fmt(b['value_usdt'])} USDT → {root / 'baseline.json'}")
    return 0


def cmd_set_cryptocom_key_expiry(args) -> int:
    root = SNAPSHOT_DIR.parent / "archive" / "cryptocom"
    k = cdc_mod.set_key_expiry(root, None if args.date == "clear" else args.date)
    print(f"Crypto.com API Key 到期日：{(k.get('expires') or '（未設定：以第一次成功使用＋30 天估計）')[:10]}")
    return 0


def _ibkr_print(snap: dict) -> None:
    b = snap["base_currency"]
    nav = snap["net_liquidation"] or 0
    print(f"\n=== IBKR {snap['account']}（唯讀，IB Gateway）  {snap['captured_oslo'][:16].replace('T', ' ')} Oslo ===")
    print(f"淨資產 NetLiquidation ≈ {_fmt(nav)} {b}；現金 {_fmt(snap['total_cash'] or 0)} {b}（{(snap['total_cash'] or 0) / nav * 100 if nav else 0:.1f}%）")
    for p in sorted(snap["positions"], key=lambda p: -(p.get("market_value_base") or 0)):
        mv = p.get("market_value_base")
        print(f"  {p['symbol']:8} {p['secType']:4} {p['position']:>12.6g} @ 成本 {p['avg_cost'] or 0:.4g} {p['currency']}"
              + (f"  市值 ≈ {_fmt(mv)} {b}（{mv / nav * 100:.1f}%）" if mv is not None and nav else ""))
    print("現金：" + "、".join(f"{c} {_fmt(v)}" for c, v in (snap["cash_by_currency"] or {}).items()))
    if snap.get("pnl"):
        print(f"今日 PnL：{snap['pnl']}")


def _need_kraken() -> bool:
    if not kraken_mod.available():
        print(kraken_mod.no_creds_msg(), file=sys.stderr)
        return False
    return True


def _kraken_balance(args) -> int:
    if not _need_kraken():
        return 2
    d = kraken_mod.build_snapshot(kraken_mod.KrakenClient(verbose=args.verbose), now_oslo())
    print(f"\n=== Kraken 持倉（唯讀）  {now_oslo():%Y-%m-%d %H:%M} Oslo ===")
    if not d["balance_ok"]:
        print(f"餘額讀取失敗：{d['errors']}")
        return 1
    for h in d["holdings"]:
        print(f"  {h['asset']:10} {h['coin']:8} {h['kind']:22} {h['qty']:.10g}" + (f"  ≈ {_fmt(h['value'])}" if h["value"] is not None else "  （無報價）"))
    v = d["value"]
    print(f"合計 ≈ {_fmt(v['total'])} USDT（現貨 {_fmt(v['spot'])}＋質押／Earn {_fmt(v['earn'])}）；掛單 {d['open_orders']}")
    ea = d["earn_allocations"]
    if ea["ok"]:
        print(f"Earn/Allocations：已配置 {ea['total_allocated']} {ea['converted_asset']}，累計獎勵 {ea['total_rewarded']}（{len(ea['items'])} 項）")
    if d["unpriced"]:
        print(f"無報價：{', '.join(d['unpriced'])}")
    errs = {k: v for k, v in d["errors"].items()}
    if errs:
        print("讀取失敗：" + "; ".join(f"{k}: {v}" for k, v in errs.items()))
    return 0


def _need_mexc() -> bool:
    if not mexc_mod.available():
        print(mexc_mod.no_creds_msg(), file=sys.stderr)
        return False
    return True


def cmd_set_mexc_baseline(args) -> int:
    p = SNAPSHOT_DIR / "analysis_overrides.json"
    ov = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    keys = ("mexc_baseline_at", "mexc_baseline_usdt", "mexc_baseline_earn_usdt", "mexc_baseline_snapshot")
    if args.action == "clear":
        for k in keys:
            ov[k] = None
        p.write_text(json.dumps(ov, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"已清除 MEXC 基準。執行 `{cli_cmd()} analyze` 更新。")
        return 0
    earn = mexc_mod.earn_manual(ov)
    if args.value is not None:  # sync an existing baseline (e.g. from the Mac to the box)
        if not args.at:
            print("--value 需要搭配 --at", file=sys.stderr)
            return 2
        t = dt.datetime.fromisoformat(args.at)
        vals = (t.isoformat(timespec="seconds"), args.value, earn["value"] if earn else None, args.snapshot)
    else:
        root = SNAPSHOT_DIR.parent
        if mexc_mod.available():
            t = now_oslo()
            d = mexc_mod.build_snapshot(mexc_mod.MexcClient(verbose=args.verbose), t)
            if not d["spot_ok"]:
                print(f"MEXC 現貨讀取失敗，未設定基準：{d['errors'].get('account')}", file=sys.stderr)
                return 1
            snap_p = mexc_mod.write_snapshot(root / "archive" / "mexc" / t.strftime("%Y-%m") / "snapshots", d, t)
            mexc_mod.write_snapshot(SNAPSHOT_DIR, d, t)
        else:
            d, snap_p = mexc_mod.latest_snapshot([root / "archive" / "mexc", SNAPSHOT_DIR])
            if d is None:
                print(mexc_mod.no_creds_msg(), file=sys.stderr)
                return 1
            t = dt.datetime.fromisoformat(d["captured_oslo"])
            print(f"沒有 MEXC 憑證：使用最新快照 {snap_p.name}（{d['captured_oslo']}）", file=sys.stderr)
        tk = mexc_mod.fetch_tickers()
        api = 0.0
        for h in d.get("holdings") or []:
            px = mexc_mod.price_usdt(h["coin"], tk) if tk else None
            px = px if px is not None else h.get("price")
            api += h["qty"] * px if px is not None else 0
        for c, f in (d.get("futures") or {}).items():
            px = (mexc_mod.price_usdt(c, tk) if tk else None) or (1.0 if c in mexc_mod.STABLE else None)
            api += f["equity"] * px if px is not None else 0
        value = api + (earn["value"] if earn else 0.0)
        vals = (t.isoformat(timespec="seconds"), round(value, 2), earn["value"] if earn else None, snap_p.name)
        print(f"MEXC 現貨＋合約 {api:,.2f}" + (f"＋理財（手動填入）{earn['value']:,.2f}" if earn else "（未填理財）") + f" = {value:,.2f} USDT")
    for k, v in zip(keys, vals):
        ov[k] = v
    p.write_text(json.dumps(ov, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"已設定 MEXC 基準：{vals[1]:,.2f} USDT @ {vals[0]}（快照 {vals[3]}）。執行 `{cli_cmd()} analyze` 更新。")
    return 0


def _flex_summary(a: dict) -> None:
    B = a.get("base_currency") or ""
    c, r = a["costs"], a["ratios"]
    pc = lambda x: "—" if x is None else f"{x:.2f}%"
    print(f"帳戶 {a.get('account')}：開戶（第一筆淨值）{a.get('account_start')}；資料至 {a.get('coverage_to')}；"
          + ("涵蓋完整（期初淨值 0）" if a.get("complete") else f"⚠ 不完整：期初淨值 {a.get('opening_value'):.2f} {B}（帳戶早於 Flex 期間）"))
    print(f"淨入金 {a['net_inflow']:,.2f} {B}；期末淨值 {a['nav_end']:,.2f}；期間盈虧 {a['pnl']:+,.2f}（{a['pnl_pct']:+.2f}%；IBKR TWR {a['twr_pct']:+.2f}%）")
    print(f"已實現 {a['realized']['total']:+,.2f}（賣出 {a['realized']['sells']} 筆）；股息 毛 {a['dividends']['gross']:.2f}／稅 {a['dividends']['tax']:.2f}／淨 {a['dividends']['net']:.2f}；利息 {a['interest_total']:+.2f}")
    print(f"成本：股票佣金 {c['stock_commission']:.2f}；換匯佣金 {c['fx_commission']:.2f}＋估計價差 {c['fx_spread_est']:.2f}；其他費用 {c['other_fees']:.2f}；合計 {c['total']:.2f} {B}")
    print(f"佔股票成交額 {pc(r['stock_commission_pct_of_stock_volume'])}（佣金）；總成本佔總成交額 {pc(r['total_cost_pct_of_traded_volume'])}；佔盈虧 {pc(r['total_cost_pct_of_pnl'])}")


def cmd_ibkr_import(args) -> int:
    from . import ibkr_flex
    root = SNAPSHOT_DIR.parent / "archive" / "ibkr"
    rc = 0
    for x in args.xml:
        try:
            rep = ibkr_flex.import_xml(Path(x).expanduser(), root)
        except (OSError, ibkr_flex.FlexError, ET_ParseError) as e:
            print(f"✘ {x}：{e}", file=sys.stderr)
            rc = 1
            continue
        print(f"匯入 {rep['file']}（帳戶 {rep['account']}，{rep['from']}–{rep['to']}，{rep['statements']} 份報表）→ {ibkr_flex.flex_dir(root)}")
        for k, v in rep["sections"].items():
            if v["in_file"] or v["total"]:
                print(f"  {k:<20} 檔案 {v['in_file']:>5}  新增 {v['new']:>5}  更新 {v['updated']:>4}  累計 {v['total']:>5}")
    if rc == 0 and not args.quiet:
        _flex_summary(ibkr_flex.analyze(SNAPSHOT_DIR.parent, root, offline=args.offline))
        print(f"執行 `{cli_cmd()} analyze` 更新儀表板；Mac 上的 archive/ibkr 會隨每週的 exports/ibkr_archive.tgz 給 box。")
    return rc


def cmd_ibkr_flex_fetch(args) -> int:
    from . import ibkr_flex
    from .client import credential_sources, load_secret
    src = credential_sources(ibkr_flex.FLEX_NAMES)
    if not all(src.values()):
        msg = (f"IBKR Flex Web Service 尚未設定（{', '.join(n for n, v in src.items() if not v)}）："
               f"在 IBKR Client Portal 建立 Flex Web Service Token 與 Activity Flex Query 後執行 {cli_cmd()} setup-keychain --exchange ibkr-flex")
        print(msg, file=sys.stderr)
        return 0 if args.if_configured else 2
    try:
        p = ibkr_flex.flex_fetch(load_secret("IBKR_FLEX_TOKEN"), load_secret("IBKR_FLEX_QUERY_ID"), SNAPSHOT_DIR.parent / "imports" / "ibkr")
    except ibkr_flex.FlexError as e:
        print(f"IBKR Flex：{e}", file=sys.stderr)
        return 1
    print(f"已下載 Flex 報表 → {p}")
    if args.no_import:
        return 0
    args.xml, args.quiet = [str(p)], False
    return cmd_ibkr_import(args)


def cmd_set_ibkr_baseline(args) -> int:
    root = SNAPSHOT_DIR.parent / "archive" / "ibkr"
    if args.action == "clear":
        ibkr_mod.save_baseline(root, None, clear=True)
        print("已清除 IBKR 基準（下次 archive 會以當時 NAV 重新建立）。")
        return 0
    snap, p = ibkr_mod.latest_snapshot([root, SNAPSHOT_DIR])
    if args.action == "now":
        try:
            t = now_oslo()
            snap = ibkr_mod.build_snapshot(ibkr_mod.fetch(verbose=args.verbose, executions=False), t)
            ibkr_mod.write_snapshot(root / t.strftime("%Y-%m") / "snapshots", snap, t)
        except ibkr_mod.IbkrUnavailable as e:
            print(str(e), file=sys.stderr)
            return 2
    elif snap is None:
        print("沒有 IBKR 快照。", file=sys.stderr)
        return 2
    u, src = ibkr_mod.usd_per(snap["base_currency"], snap)
    b = ibkr_mod.save_baseline(root, snap, u * ibkr_mod.usdt_per_usd() if u else None)
    print(f"IBKR 基準：{b['at'][:16]}  {_fmt(b['value_base'])} {b['base_currency']}（1 {b['base_currency']} ≈ {b['usdt_per_base']} USDT，{src}）")
    print(f"寫入 {ibkr_mod.baseline_path(root)}（會隨 exports/ibkr_archive.tgz 給 box）。執行 `{cli_cmd()} analyze` 更新。")
    return 0


def cmd_set_mexc_earn(args) -> int:
    from .web import set_override_earn
    from .client import SNAPSHOT_DIR
    raw = str(args.value).strip().lower()
    try:
        v = None if raw in ("clear", "none", "") else float(raw.replace(",", ""))
        if v is not None and not (0 <= v < 1e9):
            raise ValueError
    except ValueError:
        print("請輸入 0 或正數（USDT），或 clear", file=sys.stderr)
        return 2
    p = SNAPSHOT_DIR / "analysis_overrides.json"
    set_override_earn(p, v)
    print(f"已寫入 {p}：mexc_earn_usdt = {v}（手動填入）。執行 `{cli_cmd()} analyze` 更新合計。")
    return 0


def _fmt(x: float) -> str:
    return f"{x:,.2f}"


def _mexc_balance(args) -> int:
    if not _need_mexc():
        return 2
    mc = mexc_mod.MexcClient(verbose=args.verbose)
    d = mexc_mod.build_snapshot(mc, now_oslo())
    print(f"\n=== MEXC 持倉（唯讀）  {now_oslo():%Y-%m-%d %H:%M} Oslo ===")
    if not d["spot_ok"]:
        print(f"現貨讀取失敗：{d['errors'].get('account')}")
        return 1
    print(f"-- 現貨非零幣種：{len(d['spot_qty'])}（USDT 估值以 MEXC 最新成交價）--")
    for h in d["holdings"]:
        print(f"  {h['coin']:10} {h['qty']:.10g}" + (f"  ≈ {_fmt(h['value'])}" if h["value"] is not None else "  （無報價）"))
    print("-- 合約 --")
    if d["futures_ok"]:
        print("  " + ("  ".join(f"{c}: equity={f['equity']:.6g}" for c, f in d["futures"].items()) or "（無資產）"))
        for p in d["positions"]:
            print(f"    {p.get('symbol')} type={p.get('positionType')} vol={p.get('holdVol')} realised={p.get('realised')}")
    else:
        print(f"  讀取失敗：{d['errors'].get('futures_assets')}")
    print(f"合計 ≈ {_fmt(d['value']['total'])} USDT（現貨 {_fmt(d['value']['spot'])}＋合約 {_fmt(d['value']['futures'])}）")
    if d["unpriced"]:
        print(f"無報價：{', '.join(d['unpriced'])}")
    return 0


def cmd_balance(args) -> int:
    run_b, run_m, run_k = _which(args)
    rc = _bitget_balance(args) if run_b else 0
    if run_m:
        rc = max(rc, _mexc_balance(args))
    if run_k:
        rc = max(rc, _kraken_balance(args))
    if _run_cdc(args):
        rc = max(rc, _cdc_snapshot(args, write=False))
    if _run_wallets(args):
        d = wallets_mod.build_snapshot(SNAPSHOT_DIR.parent / "archive" / "wallets")
        _wallets_print(d)
        if not d.get("ok"):
            rc = max(rc, 1)
    if _run_etherfi(args):
        root = SNAPSHOT_DIR.parent / "archive" / "etherfi_cash"
        d = efc_mod.build_snapshot(root)
        if not d.get("ok"):
            print(f"\n=== ether.fi Cash ===\n{d.get("reason") or d.get("errors")}")
        else:
            print(f"\n=== ether.fi Cash（開銷帳戶）  {d["captured_oslo"][:16]} ===")
            print(f"vault {d.get("vault")}")
            for tok in d.get("tokens") or []:
                v = tok.get("value_usdt")
                print(f"  {tok["symbol"]:12} {tok["amount"]:.8g}" + (f"  ≈ {v:,.2f}" if v is not None else "  （無報價）"))
            print(f"合計 ≈ {(d.get("total_usdt") or 0):,.2f} USDT｜儲值代理 {d.get("spend_proxy_usdt") or 0:,.2f}（{d.get("spend_proxy_method")}）")

    if _run_ibkr(args):
        try:
            _ibkr_print(ibkr_mod.build_snapshot(ibkr_mod.fetch(verbose=args.verbose, executions=False), now_oslo()))
        except ibkr_mod.IbkrUnavailable as e:
            print(str(e), file=sys.stderr); rc = max(rc, 2 if args.exchange == "ibkr" else rc)
    return rc


def _bitget_balance(args) -> int:
    c = _client(args)
    h = hold.collect(c, full=False)
    h["earn_account_assets"] = c.get("earn_account_assets", "/api/v2/earn/account/assets")
    print(f"=== Bitget 持倉（唯讀）  {now_oslo():%Y-%m-%d %H:%M} Oslo ===")
    print(f"模式：{hold.mode(h)}")
    if not ok(h["spot_assets"]):
        print(f"現貨讀取失敗：{h['spot_assets'].get('code')} {h['spot_assets'].get('msg')}")
        return 1
    totals = hold.account_totals(h)
    if totals:
        print("\n-- 各帳戶（USDT 估值，Bitget 提供）--")
        for k, v in totals.items():
            print(f"  {k:8} {_fmt(v)}")
        print(f"  {'合計':8} {_fmt(sum(totals.values()))}")
    qty = hold.spot_quantities(h["spot_assets"])
    print(f"\n-- 現貨非零幣種：{len(qty)} --")
    for coin, q in sorted(qty.items(), key=lambda kv: kv[0].lower()):
        print(f"  {coin:10} {q:.10g}")
    earn = hold.earn_assets(h)
    if earn:
        print(f"\n-- 理財（Earn）：{len(earn)} 幣種 --")
        for coin, q in sorted(earn.items()):
            print(f"  {coin:10} {q:.10g}")
    bots = [b for b in hold.bot_assets(h) if b["equity"]]
    if bots:
        print("\n-- 機器人帳戶 --")
        for b in bots:
            print(f"  {b['accountType']:8} {b['coin']} equity={b['equity']:.6g}")
    fe = hold.futures_equity(h)
    print("\n-- 合約 --")
    print("  " + "  ".join(f"{k}={v:.4g}" for k, v in fe.items()))
    pos = hold.positions(h)
    oo = hold.open_orders(h)
    print(f"  持倉 {len(pos)}；掛單 " + ", ".join(f"{k}={v}" for k, v in oo.items()))
    for p in pos:
        print(f"    {p.get('productType')} {p.get('symbol')} {p.get('holdSide')} total={p.get('total')} uPL={p.get('unrealizedPL')}")
    _print_errors(c)
    return 0


def _print_errors(c: Client) -> None:
    errs = {k: v for k, v in c.errors().items() if k != "uta_assets"}
    if errs:
        print("\n-- 讀取失敗的端點 --")
        for k, v in errs.items():
            print(f"  {k}: {v}")


def _valued(c: Client):
    h = hold.collect(c, full=True)
    qty = hold.spot_quantities(h["spot_assets"])
    tickers = val.fetch_tickers()
    return h, qty, val.value_spot(qty, tickers)


def cmd_summary(args) -> int:
    c = _client(args)
    h, qty, v = _valued(c)
    if not ok(h["spot_assets"]):
        print(f"現貨讀取失敗：{h['spot_assets'].get('code')} {h['spot_assets'].get('msg')}")
        return 1
    print(f"=== 估值摘要（現貨 × 最新價，約數） {now_oslo():%Y-%m-%d %H:%M} Oslo ===")
    print(f"現貨約合計：{_fmt(v['total_usdt'])} USDT")
    for s in val.SLEEVES:
        d = v["sleeves"][s]
        print(f"  {val.SLEEVE_NAMES[s]:14} {_fmt(d['usdt']):>12} U  {d['pct']:5.1f}%")
    for s, n in (("rtoken", 15), ("crypto", 10), ("stock_plus", 10)):
        rows = [r for r in v["rows"] if r["sleeve"] == s][:n]
        if rows:
            print(f"\n-- {val.SLEEVE_NAMES[s]} 前 {len(rows)} --")
            for r in rows:
                px = f"{r['price']:.6g}" if r["price"] is not None else "無價"
                print(f"  {r['coin']:10} qty={r['qty']:.8g}  ≈{_fmt(r['usdt'])} U @ {px}")
    if v["missing_price"]:
        print(f"\n無 ticker 的幣種 {len(v['missing_price'])} 個（估值計 0）：{', '.join(v['missing_price'][:15])}")
        print("  （Stock+ …ON 代幣在公開現貨行情沒有報價，估值以 Bitget App 為準）")
    totals = hold.account_totals(h)
    if totals:
        grand = sum(totals.values())
        print("\n-- 全帳戶（Bitget all-account-balance）--")
        for k, x in totals.items():
            pct = x / grand * 100 if grand else 0
            print(f"  {k:8} {_fmt(x):>12} U  {pct:5.1f}%")
        print(f"  {'合計':8} {_fmt(grand):>12} U")
    subs = hold.subaccounts(h)
    if subs:
        print(f"\n子帳戶 {len(subs)} 個（幣種：{', '.join(sorted({k for s in subs for k in s['coins']})) or '無'}）")
    _print_errors(c)
    return 0


def cmd_snapshot(args) -> int:
    run_b, run_m, run_k = _which(args)
    rc = _bitget_snapshot(args) if run_b else 0
    if _run_ibkr(args):
        try:
            t = now_oslo()
            d = ibkr_mod.build_snapshot(ibkr_mod.fetch(verbose=args.verbose, executions=False), t)
            p = ibkr_mod.write_snapshot(SNAPSHOT_DIR, d, t)
            print(f"已寫入：{p.relative_to(SNAPSHOT_DIR.parent)}（IBKR {len(d['positions'])} 個部位，NAV ≈ {_fmt(d['net_liquidation'] or 0)} {d['base_currency']}）")
        except ibkr_mod.IbkrUnavailable as e:
            print(str(e), file=sys.stderr); rc = max(rc, 2 if args.exchange == "ibkr" else rc)
    if _run_cdc(args):
        rc = max(rc, _cdc_snapshot(args, write=True))
    if _run_wallets(args):
        t = now_oslo()
        root = SNAPSHOT_DIR.parent / "archive" / "wallets"
        d = wallets_mod.build_snapshot(root, t)
        p = wallets_mod.write_snapshot(root / t.strftime("%Y-%m") / "snapshots", d, t)
        if wallets_mod.load_baseline(root) is None and d.get("total_usdt") is not None:
            wallets_mod.save_baseline(root, d)
            print(f"已建立鏈上錢包盈虧基準：{d['captured_oslo'][:16]}")
        print(f"已寫入：{p.relative_to(SNAPSHOT_DIR.parent)}（鏈上錢包 {d.get('n_configured', 0)} 個，合計 ≈ {_fmt(d.get('total_usdt') or 0)} USDT）")
        if not d.get("ok"):
            rc = max(rc, 1)
    if run_k and _need_kraken():
        t = now_oslo()
        d = kraken_mod.build_snapshot(kraken_mod.KrakenClient(verbose=args.verbose), t)
        if not d["balance_ok"]:
            print(f"Kraken 餘額讀取失敗，未寫入快照：{d['errors']}")
            rc = max(rc, 1)
        else:
            p = kraken_mod.write_snapshot(SNAPSHOT_DIR, d, t)
            print(f"已寫入：{p.relative_to(SNAPSHOT_DIR.parent)}（Kraken {len(d['holdings'])} 項，≈ {_fmt(d['value']['total'])} USDT）")
    if run_m and _need_mexc():
        t = now_oslo()
        d = mexc_mod.build_snapshot(mexc_mod.MexcClient(verbose=args.verbose), t)
        if not d["spot_ok"]:
            print(f"MEXC 現貨讀取失敗，未寫入快照：{d['errors'].get('account')}")
            rc = max(rc, 1)
        else:
            p = mexc_mod.write_snapshot(SNAPSHOT_DIR, d, t)
            print(f"已寫入：{p.relative_to(SNAPSHOT_DIR.parent)}（MEXC {len(d['spot_qty'])} 幣種）")
    return rc


def _bitget_snapshot(args) -> int:
    c = _client(args)
    t = now_oslo()
    doc, qty, h = snapshot_mod.build(c, t)
    if doc is None:
        print(f"現貨讀取失敗，未寫入快照：{h['spot_assets'].get('code')} {h['spot_assets'].get('msg')}")
        return 1
    jpath, qpath = snapshot_mod.write(SNAPSHOT_DIR, doc, qty, t)
    print(f"已寫入：{jpath.relative_to(SNAPSHOT_DIR.parent)}")
    print(f"已寫入基準檔：{qpath.relative_to(SNAPSHOT_DIR.parent)}（{len(qty)} 幣種）")
    _print_errors(c)
    return 0


def cmd_audit(args) -> int:
    try:
        since = dt.date.fromisoformat(args.since)
    except ValueError:
        print("--since 格式須為 YYYY-MM-DD", file=sys.stderr)
        return 2
    baseline = Path(args.baseline).expanduser() if args.baseline else None
    if baseline and not baseline.exists():
        print(f"找不到基準檔：{baseline}", file=sys.stderr)
        return 2
    c = _client(args)
    print("稽核中（只讀 GET，需要約 1 分鐘）…", file=sys.stderr)
    a = audit_mod.run(c, since, baseline)
    SNAPSHOT_DIR.mkdir(exist_ok=True)
    st = stamp(dt.datetime.fromisoformat(a["captured_oslo"]))
    raw_path = SNAPSHOT_DIR / f"audit_{st}_raw.json"
    sum_path = SNAPSHOT_DIR / f"audit_{st}_summary.txt"
    raw_path.write_text(json.dumps(a, indent=1, ensure_ascii=False), encoding="utf-8")
    text = audit_mod.render_summary(a)
    sum_path.write_text(text, encoding="utf-8")
    print(text)
    print(f"已寫入：{raw_path.relative_to(SNAPSHOT_DIR.parent)}、{sum_path.relative_to(SNAPSHOT_DIR.parent)}")
    return 1 if any(f["level"] == "!!" for f in a["flags"]) else 0


def cmd_perms(args) -> int:
    run_b, run_m, run_k = _which(args)
    rc = _bitget_perms(args) if run_b else 0
    if _run_ibkr(args):
        p = ibkr_mod.perms_report(verbose=args.verbose)
        print("\n=== IBKR（IB Gateway）唯讀檢查 ===")
        if not p["ok"]:
            print(f"  ✘ {p['error']}")
            rc = max(rc, 1)
        else:
            print(f"  帳戶 {', '.join(p['accounts'])}；server version {p['server_version']}")
            for k, ok in p["complete"].items():
                print(f"  {k:12} {'✔ 已收到' if ok else '✘ 逾時／未收到'}")
            print(f"  筆數：{p['counts']}")
            if p["errors"]:
                print("  Gateway 訊息：" + "; ".join(f"{e['code']} {e['msg']}"[:80] for e in p["errors"][:5]))
            print(f"  已送出的訊息：{', '.join(dict.fromkeys(p['sent']))}")
            print(f"  本機拒絕（未送出）：{', '.join(p['write_refused_locally'])} 及其他所有非白名單訊息")
            print("  另請保持 IB Gateway「Read-Only API」勾選（第二道防線）。")
    if _run_cdc(args):
        p = cdc_mod.perms_report(cdc_mod.CdcClient(verbose=args.verbose))
        print("\n=== Crypto.com App Agent API Key 檢查（只送出白名單內的 GET）===")
        if p["local_problem"]:
            print(f"  ✘ 未送出任何請求：{p['local_problem']}")
            rc = max(rc, 1)
        for k, v in p["probes"].items():
            print(f"  {k:24} {'✔ ' if v.startswith('OK') else '✘ '}{v}")
        print(f"  本機拒絕（未送出）：{', '.join(p['refused_locally'])} 及其他所有非白名單端點")
        print("  提醒：在 App 的 Agent API Key 設定中關閉「Execute trades」（第二道防線）。")
    if run_k and _need_kraken():
        p = kraken_mod.perms_report(kraken_mod.KrakenClient(verbose=args.verbose))
        print("\n=== Kraken API Key 權限檢查（只送出唯讀查詢）===")
        if p.get("local_problem"):
            print(f"  ✘ 未送出任何請求：{p['local_problem']}")
            return max(rc, 1)
        for k, v in p["read_probes"].items():
            print(f"  {k:18} {'✔ 可讀' if v.startswith('OK') else '✘ ' + v}")
        print(f"  本機拒絕（未送出）：{', '.join(p['write_refused_locally'])} 及其他所有非白名單端點")
        print("  Kraken 沒有「查詢 Key 權限」的端點；寫入權限無法在不寫入的情況下驗證。")
        print("  請在 Kraken「Settings → API」確認此 Key 只勾選 Query Funds、Query Open/Closed Orders & Trades、Query Ledger Entries，")
        print("  未勾選 Deposit/Withdraw、Create & Modify Orders、Cancel/Close Orders、Earn（Allocate funds）等。")
        rc = max(rc, 0 if p["ok"] else 1)
    if run_m and _need_mexc():
        p = mexc_mod.perms_report(mexc_mod.MexcClient(verbose=args.verbose))
        print("\n=== MEXC API Key 權限檢查 ===")
        if not p["ok"]:
            print(f"讀取失敗：{p['error']}")
            return max(rc, 1)
        for k, v in p["read_probes"].items():
            print(f"  {k:34} {'✔ 可讀' if v == 'OK' else '✘ ' + v}")
        print(f"  /api/v3/account：canTrade={p['canTrade']} canWithdraw={p['canWithdraw']} canDeposit={p['canDeposit']} permissions={p['permissions']}")
        print("  注意：MEXC 沒有「查詢 API Key 權限」的唯讀端點；canTrade/canWithdraw 是否代表 Key 權限（或只是帳戶狀態）文件未說明。")
        print("  寫入權限無法用 GET 驗證（assetsboard 不會送出任何下單／提幣請求）。請在 MEXC 網頁「API 管理」確認此 Key 只勾選讀取類權限。")
        risky = bool(p["canTrade"]) or bool(p["canWithdraw"])
        print("  結論：" + ("⚠ 回應顯示 canTrade/canWithdraw 為 true，請到 API 管理確認未勾選交易、提幣、劃轉（WRITE）權限" if risky
                         else "回應顯示不可交易、不可提幣 ✔"))
        rc = max(rc, 1 if risky else 0)
    return rc


def _bitget_perms(args) -> int:
    c = _client(args)
    p = audit_mod.permissions(c.get("account_info", "/api/v2/spot/account/info"))
    print("=== API Key 權限檢查 ===")
    if not p["ok"]:
        print(f"讀取失敗：{p['error']}")
        return 1
    print(f"authorities：{', '.join(p['authorities']) or '(無)'}")
    if p["read_only"]:
        print("結論：唯讀 ✔（所有權限皆以 r 結尾）")
    else:
        print(f"結論：含寫入權限 ✘ → {', '.join(p['write_authorities'])}；建議立即改成唯讀 Key")
    print(f"IP 白名單：{'已設定 ✔（內容不顯示）' if p['ip_whitelist_set'] else '未設定 ✘（建議綁定）'}")
    return 0 if p["read_only"] else 1


def cmd_pnl(args) -> int:
    try:
        since = dt.date.fromisoformat(args.since)
    except ValueError:
        print("--since 格式須為 YYYY-MM-DD", file=sys.stderr)
        return 2
    c = _client(args)
    print("計算中（只讀 GET，約 3–5 分鐘）…", file=sys.stderr)
    p = pnl_mod.run(c, since)
    SNAPSHOT_DIR.mkdir(exist_ok=True)
    st = stamp(dt.datetime.fromisoformat(p["captured_oslo"]))
    raw_path = SNAPSHOT_DIR / f"pnl_{st}_raw.json"
    sum_path = SNAPSHOT_DIR / f"pnl_{st}_summary.txt"
    raw_path.write_text(json.dumps(p, indent=1, ensure_ascii=False), encoding="utf-8")
    text = pnl_mod.render(p)
    sum_path.write_text(text, encoding="utf-8")
    print(text)
    print(f"已寫入：{raw_path.relative_to(SNAPSHOT_DIR.parent)}、{sum_path.relative_to(SNAPSHOT_DIR.parent)}")
    return 0


def cmd_archive(args) -> int:
    if args.since:
        try:
            dt.date.fromisoformat(args.since)
        except ValueError:
            print("--since 格式須為 YYYY-MM-DD", file=sys.stderr)
            return 2
    root = Path(args.dir).expanduser()
    if not root.is_absolute():
        root = SNAPSHOT_DIR.parent / root
    since = dt.date.fromisoformat(args.since) if args.since else None
    run_b, run_m, run_k = _which(args)
    rc = 0
    if run_b:
        c = _client(args)
        print(f"封存中（Bitget，只讀 GET）→ {root} …", file=sys.stderr)
        rep = archive_mod.run(c, root, since)
        print(archive_mod.render(rep))
        rc = 0 if not rep["errors"] else 1
    if run_m and _need_mexc():
        print(f"封存中（MEXC，只讀 GET）→ {root / 'mexc'} …", file=sys.stderr)
        rep = mexc_mod.archive_run(mexc_mod.MexcClient(verbose=args.verbose), root / "mexc", since)
        print(archive_mod.render(rep).replace("=== 封存完成", "=== MEXC 封存完成"))
        rc = max(rc, 0 if not rep["errors"] else 1)
    if _run_ibkr(args):
        print(f"封存中（IBKR，本機 IB Gateway 唯讀）→ {root / 'ibkr'} …", file=sys.stderr)
        try:
            rep = ibkr_mod.archive_run(root / "ibkr", verbose=args.verbose)
            print(archive_mod.render(rep).replace("=== 封存完成", "=== IBKR 封存完成"))
            if rep.get("baseline_created"):
                print(f"已建立 IBKR 盈虧基準：{rep['baseline_created'][:16]}（之後的盈虧從這裡起算）")
            rc = max(rc, 0 if not rep["errors"] else 1)
        except ibkr_mod.IbkrUnavailable as e:
            print(str(e), file=sys.stderr)
            rc = max(rc, 2 if args.exchange == "ibkr" else rc)
    if _run_cdc(args):
        print(f"封存中（Crypto.com App，只送白名單內的 GET）→ {root / 'cryptocom'} …", file=sys.stderr)
        rep = cdc_mod.archive_run(cdc_mod.CdcClient(verbose=args.verbose), root / "cryptocom")
        if rep["sources"]:
            print(archive_mod.render(rep).replace("=== 封存完成", "=== Crypto.com 封存完成"))
            ti = rep.get("tx_info") or {}
            print(f"交易紀錄：讀了 {ti.get('pages', 0)} 頁；停止原因：{ti.get('stopped')}；"
                  f"{'歷史已完整' if ti.get('backfill_complete') else '歷史未完整（下次從游標繼續）'}")
        else:
            print("Crypto.com：" + "; ".join(f"{e['msg']}" for errs in rep["errors"].values() for e in errs), file=sys.stderr)
        if rep.get("baseline_created"):
            print(f"已建立 Crypto.com 盈虧基準：{rep['baseline_created'][:16]}（之後的盈虧從這裡起算）")
        if rep.get("warning"):
            print("⚠ " + rep["warning"])
        rc = max(rc, 2 if "auth" in rep["errors"] else (1 if rep["errors"] else 0))
    if _run_wallets(args):
        print(f"封存中（鏈上錢包，公開 RPC／瀏覽器 API，無私鑰）→ {root / 'wallets'} …", file=sys.stderr)
        rep = wallets_mod.archive_run(root / "wallets")
        if rep.get("snapshot"):
            print(archive_mod.render(rep).replace("=== 封存完成", "=== 鏈上錢包 封存完成"))
            for w in rep.get("wallets") or []:
                toks = ",".join(f"{a}={b}" for a, b in (w.get("tokens") or []))
                print(f"  {w.get('id')}: {toks or '—'} → {_fmt(w.get('value_usdt') or 0)} USDT" + (f"  ERR {w['error']}" if w.get("error") else ""))
        else:
            print("鏈上錢包：" + "; ".join(e["msg"] for errs in rep["errors"].values() for e in errs), file=sys.stderr)
        if rep.get("baseline_created"):
            print(f"已建立鏈上錢包盈虧基準：{rep['baseline_created'][:16]}（之後的盈虧從這裡起算）")
        rc = max(rc, 1 if rep["errors"] else 0)
    if run_k and _need_kraken():
        print(f"封存中（Kraken，只送唯讀查詢；首次會讀完整帳本，可能需數分鐘）→ {root / 'kraken'} …", file=sys.stderr)
        rep = kraken_mod.archive_run(kraken_mod.KrakenClient(verbose=args.verbose), root / "kraken", since)
        print(archive_mod.render(rep).replace("=== 封存完成", "=== Kraken 封存完成"))
        rc = max(rc, 0 if not rep["errors"] else 1)

    if _run_etherfi(args):
        eroot = root / "etherfi_cash"
        print(f"封存中（ether.fi Cash 開銷帳戶，公開瀏覽器 API）→ {eroot} …", file=sys.stderr)
        rep = efc_mod.archive_run(eroot)
        if rep.get("snapshot"):
            print(f"=== ether.fi Cash 封存完成：餘額 ≈ {(rep.get('total_usdt') or 0):,.2f} USDT；"
                  f"儲值代理 {(rep.get('spend_proxy_usdt') or 0):,.2f} ===")
        else:
            print("ether.fi Cash：" + "; ".join(str(v) for v in (rep.get("errors") or {}).values()), file=sys.stderr)
        rc = max(rc, 1 if rep.get("errors") and rep.get("configured") else 0)

    return rc


def cmd_analyze(args) -> int:
    print("分析中（讀取 archive/ 與最新快照；只用公開行情）…", file=sys.stderr)
    res = analysis_mod.run(SNAPSHOT_DIR.parent, offline=args.offline)
    print(analysis_mod.render(res))
    return 0


def cmd_web(args) -> int:
    from . import web as web_mod
    return web_mod.serve(SNAPSHOT_DIR.parent, args.port, open_browser=not args.no_browser)


def cmd_app(args) -> int:
    from . import app as app_mod
    return app_mod.main(SNAPSHOT_DIR.parent, args.port)


def cmd_setup_keychain(args) -> int:
    from . import keychain as keychain_mod
    return keychain_mod.run(only=args.only, use_security_prompt=args.security_prompt, exchange=args.exchange)


# ---------------------------------------------------------------- Robinhood (manual, screenshot-fed)
def _rh_root():
    from .client import SNAPSHOT_DIR
    return SNAPSHOT_DIR.parent / "archive" / "robinhood"


def _rh_summary(root) -> None:
    from . import robinhood as rh
    rows = rh.load_ledger(root)
    d = rh.derive(rows, rh.load_config(root).get("rewards_paid_as", "crypto"))
    print(f"Robinhood ledger：{len(rows)} 筆（{rows[0]['date'] if rows else '—'} → {rows[-1]['date'] if rows else '—'}）  {rh.paths(root)['ledger']}")
    print(f"  淨入金 €{d['net_deposits_eur']:,.2f}　現金估算 €{d['cash_eur']:,.2f}　獎勵 €{d['rewards_eur']:,.2f}　股息 €{d['dividends_eur']:,.2f}")
    for h in d["holdings"]:
        print(f"  {h['asset']:6} {h['kind']:6} {h['qty']:.6g}  成本 €{h['cost_eur']:,.2f}" + ("  （數量含推算）" if h["qty_estimated"] else ""))


def cmd_robinhood_add(args) -> int:
    from . import robinhood as rh
    root = _rh_root()
    lines = [x for x in args.lines if x.strip()]
    if args.stdin:
        lines += [x for x in sys.stdin.read().splitlines() if x.strip() and not x.lstrip().startswith("#")]
    try:
        new = [rh.parse_line(x) for x in lines]
    except rh.LedgerError as e:
        print(f"錯誤：{e}", file=sys.stderr)
        return 2
    added, dup = rh.add_rows(root, new)
    print(f"新增 {len(added)} 筆，略過重複 {dup} 筆。")
    _rh_summary(root)
    print(f"執行 `{cli_cmd()} analyze` 更新儀表板。")
    return 0


def cmd_robinhood_import(args) -> int:
    from . import robinhood as rh
    root = _rh_root()
    total_add = total_dup = 0
    for f in args.csv:
        try:
            new = rh.import_csv_text(Path(f).read_text(encoding="utf-8"))
        except (OSError, rh.LedgerError) as e:
            print(f"{f}：{e}", file=sys.stderr)
            return 2
        added, dup = rh.add_rows(root, new)
        total_add += len(added); total_dup += dup
    print(f"新增 {total_add} 筆，略過重複 {total_dup} 筆。")
    _rh_summary(root)
    return 0


def cmd_robinhood_list(args) -> int:
    from . import robinhood as rh
    root = _rh_root()
    for r in rh.load_ledger(root):
        print(f"{r['date']}  {r['type']:9} {r['asset']:6} {r['kind']:6} {r['qty']:>10} @ {r['price_eur']:>9}  {float(r['amount_eur']):+9.2f}"
              + ("  ~推算" if r["qty_estimated"] else "") + (f"  {r['note']}" if r["note"] else ""))
    _rh_summary(root)
    return 0


def cmd_robinhood_set_portfolio(args) -> int:
    from . import robinhood as rh
    root = _rh_root()
    if str(args.total).lower() == "clear":
        rh.save_portfolio(root, None)
        print("已清除 Portfolio 校準值。")
        return 0
    pos = {}
    for p in args.pos or []:
        try:
            sym, rest = p.split("=", 1)
            q, v = (rest.split(":", 1) + [""])[:2]
            pos[sym.upper()] = {"qty": float(q) if q else None, "value_eur": float(v) if v else None}
        except ValueError:
            print(f"--pos 格式：SYM=QTY:VALUE_EUR（{p!r}）", file=sys.stderr)
            return 2
    path = rh.save_portfolio(root, float(args.total), args.cash, args.crypto, pos, args.at)
    print(f"已寫入 {path}（總值 €{float(args.total):,.2f}）。執行 `{cli_cmd()} analyze` 更新。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python3 -m assetsboard",
        description="assetsboard — Bitget＋MEXC＋Kraken＋IBKR 資產守衛（唯讀：只發送查詢請求，不下單、不劃轉、不提幣）",
    )
    ap.add_argument("--version", action="version", version=f"assetsboard {__version__}")
    ap.add_argument("-v", "--verbose", action="store_true", help="在 stderr 顯示每個端點的狀態")
    sub = ap.add_subparsers(dest="cmd", metavar="<command>")
    def ex(p, default="all"):
        p.add_argument("--exchange", choices=["all", "bitget", "mexc", "kraken", "ibkr", "cryptocom", "wallets", "etherfi-cash"], default=default,
                       help="交易所（預設 all：有憑證的都跑，沒有憑證的略過）")
        return p
    ex(sub.add_parser("balance", help="快速查看各帳戶持倉（Bitget＋MEXC＋Kraken＋Crypto.com＋鏈上錢包）")).set_defaults(func=cmd_balance)
    sub.add_parser("summary", help="現貨估值與分類（rToken / crypto / Stock+ / 穩定幣）").set_defaults(func=cmd_summary)
    pb = sub.add_parser("set-mexc-baseline", help="設定 MEXC 盈虧基準（從現在起計算；之前的歷史不計入盈虧）")
    pb.add_argument("action", nargs="?", default="now", choices=["now", "clear"],
                    help="now＝立即拍快照並以目前 MEXC 總資產（含手動理財）為基準；clear＝清除基準")
    pb.add_argument("--value", type=float, help="直接指定基準金額（USDT；用於在另一台機器同步同一個基準）")
    pb.add_argument("--at", help="搭配 --value：基準時間 ISO（例如 2026-10-04T15:24:00+02:00）")
    pb.add_argument("--snapshot", help="搭配 --value：基準快照檔名（mexc_snapshot_*.json）")
    pb.set_defaults(func=cmd_set_mexc_baseline)
    pf = sub.add_parser("ibkr-import", help="匯入 IBKR Activity Flex Query XML（交易、現金交易、佣金明細、NAV 變動）→ archive/ibkr/flex")
    pf.add_argument("xml", nargs="+", help="Flex Query XML 檔")
    pf.add_argument("--quiet", action="store_true", help="只匯入，不印摘要")
    pf.add_argument("--offline", action="store_true", help="摘要不抓匯率參考價（只用快取）")
    pf.set_defaults(func=cmd_ibkr_import)
    pg = sub.add_parser("ibkr-flex-fetch", help="用 Flex Web Service 下載報表（IBKR_FLEX_TOKEN／IBKR_FLEX_QUERY_ID，鑰匙圈）並匯入")
    pg.add_argument("--no-import", action="store_true", help="只下載到 imports/ibkr/，不匯入")
    pg.add_argument("--if-configured", action="store_true", help="沒設定 token 時安靜略過（exit 0；給每週排程用）")
    pg.add_argument("--offline", action="store_true", help=argparse.SUPPRESS)
    pg.set_defaults(func=cmd_ibkr_flex_fetch)
    pcb = sub.add_parser("set-cryptocom-baseline", help="設定 Crypto.com 盈虧基準（now＝重新讀取；latest＝用最新快照；clear）")
    pcb.add_argument("action", nargs="?", default="now", choices=["now", "latest", "clear"])
    pcb.set_defaults(func=cmd_set_cryptocom_baseline)
    pwb = sub.add_parser("set-wallets-baseline", help="設定鏈上錢包盈虧基準（now／latest／clear）")
    pwb.add_argument("action", nargs="?", default="now", choices=["now", "latest", "clear"])
    pwb.set_defaults(func=cmd_set_wallets_baseline)
    pce = sub.add_parser("set-cryptocom-key-expiry", help="記錄 Crypto.com Agent API Key 的到期日（YYYY-MM-DD 或 clear），到期前 7 天提醒")
    pce.add_argument("date")
    pce.set_defaults(func=cmd_set_cryptocom_key_expiry)
    pi = sub.add_parser("set-ibkr-baseline", help="設定 IBKR 盈虧基準（now＝重新讀取 Gateway；latest＝用最新快照；clear）")
    pi.add_argument("action", nargs="?", default="now", choices=["now", "latest", "clear"])
    pi.set_defaults(func=cmd_set_ibkr_baseline)
    pe = sub.add_parser("set-mexc-earn", help="手動填入 MEXC 理財（Earn）金額（USDT，固定值；API 看不到理財）")
    pe.add_argument("value", help="USDT 金額，或 clear 清除")
    pe.set_defaults(func=cmd_set_mexc_earn)
    prh = sub.add_parser("robinhood-add", help="Robinhood（無 API）：加入 History 截圖的紀錄，例如 \"2026-01-05 buy ACME 0.5 40.00 -20.02\"（重複的會自動略過）")
    prh.add_argument("lines", nargs="*", help="DATE TYPE [ASSET] [QTY PRICE] AMOUNT；TYPE＝deposit/withdrawal/reward/buy/sell/token-buy/market-sell/dividend/fee；QTY 結尾加 ~＝推算")
    prh.add_argument("--stdin", action="store_true", help="從 stdin 讀多行（# 開頭為註解）")
    prh.set_defaults(func=cmd_robinhood_add)
    pri = sub.add_parser("robinhood-import", help="Robinhood：匯入 CSV（欄位 date,type,asset,kind,qty,price_eur,amount_eur,qty_estimated,note），重複自動略過")
    pri.add_argument("csv", nargs="+")
    pri.set_defaults(func=cmd_robinhood_import)
    sub.add_parser("robinhood-list", help="Robinhood：列出 ledger 與推算持倉／現金").set_defaults(func=cmd_robinhood_list)
    prp = sub.add_parser("robinhood-set-portfolio", help="Robinhood：用 Portfolio 截圖校準總值（EUR），或 clear")
    prp.add_argument("total", help="Portfolio 總值 EUR，或 clear")
    prp.add_argument("--cash", type=float, help="現金 EUR")
    prp.add_argument("--crypto", type=float, help="加密貨幣部分 EUR")
    prp.add_argument("--pos", nargs="*", help="持倉 SYM=QTY:VALUE_EUR（例如 ACME=0.25:12.00）")
    prp.add_argument("--at", help="截圖時間 ISO（預設現在）")
    prp.set_defaults(func=cmd_robinhood_set_portfolio)
    ex(sub.add_parser("snapshot", help="儲存快照 JSON 與可作為基準的現貨數量檔到 snapshots/")).set_defaults(func=cmd_snapshot)
    pa = sub.add_parser("audit", help="與基準比對並檢查資金流，輸出中文摘要")
    pa.add_argument("--since", required=True, help="查詢起始日 YYYY-MM-DD（Oslo 時間 00:00）")
    pa.add_argument("--baseline", help="基準檔路徑（預設 snapshots/ 內最新的 *_spot_qty.txt）")
    pa.set_defaults(func=cmd_audit)
    pp = sub.add_parser("pnl", help="估算開戶以來盈虧：目前價值 − 淨入金（充值/法幣入金 − 提幣）")
    pp.add_argument("--since", default="2026-03-01", help="查詢起始日 YYYY-MM-DD（預設 2026-03-01）")
    pp.set_defaults(func=cmd_pnl)
    pr = sub.add_parser("archive", help="增量封存原始紀錄（帳單/成交/充提/劃轉/稅務/合約/理財/P2P）與持倉快照")
    pr.add_argument("--since", help="強制起始日 YYYY-MM-DD（預設：各來源上次封存點；首次為可查詢的最早時間）")
    pr.add_argument("--dir", default="archive", help="封存目錄（預設 archive/，相對於專案根目錄）")
    ex(pr)
    pr.set_defaults(func=cmd_archive)
    pz = sub.add_parser("analyze", help="離線分析：成本（溯源）、已實現／未實現盈虧、每月資金流 → snapshots/analysis_latest.json")
    pz.add_argument("--offline", action="store_true", help="不抓公開行情，只用快取價格")
    pz.set_defaults(func=cmd_analyze)
    pw = sub.add_parser("web", help="本機儀表板（只綁定 127.0.0.1，只讀本機檔案）")
    pw.add_argument("--port", type=int, default=47651, help="起始連接埠（預設 47651；被佔用時自動改用下一個）")
    pw.add_argument("--no-browser", action="store_true", help="不自動開啟瀏覽器")
    pw.set_defaults(func=cmd_web)
    pa2 = sub.add_parser("app", help="原生視窗開啟儀表板（macOS，需要 pywebview；見 README）")
    pa2.add_argument("--port", type=int, default=47651, help="起始連接埠（預設 47651）")
    pa2.set_defaults(func=cmd_app)
    pk = sub.add_parser("setup-keychain", help="把 API 憑證存入 macOS 鑰匙圈（隱藏輸入；供 AssetsBoard.app 使用）")
    pk.add_argument("--getpass", action="store_true", help="（預設行為，保留相容）由 assetsboard 以 getpass 讀取，經 stdin 交給 security")
    pk.add_argument("--security-prompt", action="store_true", help="改由 security 自己提示輸入（部分 macOS 版本會存成空值，不建議）")
    pk.add_argument("--exchange", choices=["bitget", "mexc", "kraken", "ibkr-flex", "cryptocom"], default="bitget", help="要設定的交易所（預設 bitget；ibkr-flex＝IBKR Flex Web Service token）")
    pk.add_argument("--only", nargs="+", choices=["BITGET_API_KEY", "BITGET_API_SECRET", "BITGET_API_PASSPHRASE", "MEXC_API_KEY", "MEXC_API_SECRET", "KRAKEN_API_KEY", "KRAKEN_API_SECRET", "IBKR_FLEX_TOKEN", "IBKR_FLEX_QUERY_ID", "CRYPTOCOM_API_KEY", "CRYPTOCOM_API_SECRET"], help="只設定指定項目")
    pk.set_defaults(func=cmd_setup_keychain)
    ex(sub.add_parser("perms", help="檢查 API Key 是否唯讀、是否綁定 IP 白名單（Bitget＋MEXC＋Kraken）")).set_defaults(func=cmd_perms)
    return ap


def main(argv: list[str] | None = None) -> None:
    ap = build_parser()
    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        ap.print_help()
        sys.exit(0)
    sys.exit(args.func(args))
