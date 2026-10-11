"""Local dashboard: python3 -m assetsboard web [--port 47651].

- Binds 127.0.0.1 only; rejects requests whose Host header is not localhost (DNS-rebinding guard).
- Serves assetsboard/static/index.html and JSON read from local files only (snapshots/analysis_latest.json,
  archive/state.json, snapshots/analysis_overrides.json). Two actions, both need the per-launch token
  embedded in the page: POST /api/refresh (「抓取最新資料」: every configured source read-only, then `analyze`;
  per-source status in /api/status → refresh.sources) and
  POST /api/app-total (stores the total the user typed from the Bitget app, with a timestamp).
"""
from __future__ import annotations

import datetime as dt
import json
import secrets
import socket
import socketserver
import threading
import time
import urllib.request
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__
from .client import cli_cmd, credential_sources, keychain_supported, now_oslo

STATIC = Path(__file__).with_name("static")
DEFAULT_PORT = 47651
APP_ID = "assetsboard"
APP_TOTAL_STALE_H = 24


def no_creds_msg() -> str:
    if keychain_supported():
        return ("找不到 Bitget API 憑證，無法重新抓取；畫面仍顯示本機已存的資料。\n"
                "請在終端機執行一次：cd ~/assetsboard && " + cli_cmd() + " setup-keychain\n"
                "（把三個值存進 macOS 鑰匙圈；之後從 Dock 開啟的 App 也能抓取最新資料。第一次讀取時 macOS 可能會詢問是否允許存取，請按「允許」或「永遠允許」。）")
    return "找不到 Bitget API 憑證（環境變數 BITGET_API_KEY／SECRET／PASSPHRASE），無法重新抓取；畫面仍顯示本機已存的資料。"


def creds_ok() -> bool:
    return all(credential_sources().values())


class LocalServer(ThreadingHTTPServer):
    """HTTPServer without the reverse-DNS lookup in server_bind (getfqdn can hang for a long time on macOS)."""
    daemon_threads = True

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name = "127.0.0.1"
        self.server_port = self.server_address[1]


# what 「抓取最新資料」 updates, in run order (key, label). Robinhood has no API: screenshot-fed, shown as manual.
SOURCES = (("bitget", "Bitget"), ("mexc", "MEXC"), ("kraken", "Kraken"), ("cryptocom", "Crypto.com"),
           ("wallets", "鏈上錢包"), ("etherfi_cash", "ether.fi Cash"), ("ibkr", "IBKR（IB Gateway）"),
           ("ibkr_flex", "IBKR Flex 出入金"), ("robinhood", "Robinhood"), ("analysis", "重新計算盈虧"))
LAST_RESULT = "refresh_last.json"


# per-source buttons (「⟳ 更新」): key → the status rows it updates. IBKR's button = Gateway snapshot + Flex catch-up.
FETCHERS = ("bitget", "mexc", "kraken", "cryptocom", "wallets", "etherfi_cash", "ibkr")
ROWS_OF = {"ibkr": ("ibkr", "ibkr_flex")}
VENUE_OF = {"ibkr_flex": "ibkr"}          # status row → analysis venue


def _blank_sources(prev: dict | None = None) -> dict:
    """Fresh status rows; keeps each row's last successful update time (shown next to its button)."""
    prev = prev or {}
    return {k: {"label": n, "status": "pending", "msg": "", "at": None, "last_ok": (prev.get(k) or {}).get("last_ok"),
                "duration_s": None, "running": False} for k, n in SOURCES}


def summarize(doc: dict | None) -> dict | None:
    """Small numbers to compare before/after a refresh (combined total, PnL, per-venue totals)."""
    if not doc:
        return None
    c = doc.get("combined") or {}
    return {"total": c.get("total"), "pnl": c.get("pnl"), "pnl_pct": c.get("pnl_pct"),
            "venues": {k: v.get("total") for k, v in (doc.get("exchanges") or {}).items() if isinstance(v, dict) and v.get("present")}}


def diff_summary(before: dict | None, after: dict | None, names: dict | None = None) -> dict | None:
    if not after:
        return None
    names = names or dict(SOURCES)
    out = {"total_after": after.get("total"), "pnl_after": after.get("pnl"), "pnl_pct_after": after.get("pnl_pct"),
           "total_change": None, "pnl_change": None, "pnl_pct_change": None, "venues": []}
    if before:
        for k in ("total", "pnl", "pnl_pct"):
            if before.get(k) is not None and after.get(k) is not None:
                out[f"{k}_change"] = after[k] - before[k]
        for k, v in (after.get("venues") or {}).items():
            b = (before.get("venues") or {}).get(k)
            if v is not None and b is not None and abs(v - b) >= 0.01:
                out["venues"].append({"key": k, "label": names.get(k, k), "before": b, "after": v, "change": v - b})
        out["venues"].sort(key=lambda x: -abs(x["change"]))
    return out


class State:
    def __init__(self, project: Path):
        self.project = project
        self.token = secrets.token_urlsafe(24)
        self.lock = threading.Lock()          # guards refresh dict, busy set and the analysis queue
        self.busy: set[str] = set()           # fetchers running now (the same source never runs twice at once)
        self.pending: set[str] | None = None  # analysis queued: venues to recompute ({"*"} = everything)
        self.analysis_active = False
        self.refresh = {"running": False, "last": None, "error": None, "log": [], "stage": "", "pct": 0,
                        "sources": _blank_sources(), "summary": None, "hint": None, "busy": [], "scope": None,
                        "timings": {}}
        lp = project / "snapshots" / LAST_RESULT          # last result survives an app restart
        try:
            if lp.exists():
                last = json.loads(lp.read_text(encoding="utf-8"))
                self.refresh.update({k: last.get(k) for k in ("last", "error", "log", "summary", "hint", "scope")})
                self.refresh["timings"] = last.get("timings") or {}
                old = last.get("sources") or {}
                srcs = _blank_sources(old)
                for k, v in srcs.items():
                    o = old.get(k) or {}
                    v.update({x: o.get(x) for x in ("status", "msg", "at", "duration_s") if o.get(x) is not None})
                    if v["status"] in ("pending", "running"):
                        v["status"] = "skipped"
                    if not v.get("last_ok") and o.get("status") in ("ok", "warn", "kept", "manual"):
                        v["last_ok"] = o.get("at")      # files written before per-source refresh existed
                self.refresh["sources"] = srcs
        except (ValueError, OSError):
            pass

    def save(self) -> None:
        R = self.refresh
        try:
            (self.project / "snapshots").mkdir(exist_ok=True)
            (self.project / "snapshots" / LAST_RESULT).write_text(json.dumps(
                {k: R.get(k) for k in ("last", "error", "log", "sources", "summary", "hint", "scope", "timings")},
                ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass


def _read_analysis(project: Path) -> dict | None:
    p = project / "snapshots" / "analysis_latest.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    except (ValueError, OSError):
        return None


def _marker(st: State):
    """mark(key, status, msg): updates one status row (thread-safe); 'ok'/'warn'/'kept'/'manual' stamp last_ok."""
    R = st.refresh

    def mark(key: str, status: str, msg: str = "") -> None:
        with st.lock:
            S = R["sources"][key]
            now = now_oslo().isoformat(timespec="seconds")
            if status == "running" and "_t0" not in S:
                S["_t0"] = time.time()
            S.update(status=status, msg=msg[:220], at=now, running=status == "running")
            if status != "running":
                if "_t0" in S:
                    S["duration_s"] = round(time.time() - S.pop("_t0"), 1)
                if status in ("ok", "warn"):
                    S["last_ok"] = now
                R["log"] = (R.get("log") or [])[-40:] + [f"{S['label']}：{msg}" if msg else S["label"]]
    return mark


def _sub(st: State, key: str):   # sub-step progress for long archives → shown in that source's row
    def cb(label: str, done: int, total: int) -> None:
        with st.lock:
            st.refresh["sources"][key]["msg"] = f"{str(label).split(':')[-1]}（{done}/{total}）"
    return cb


# ---------------------------------------------------------------- one function per source (read-only fetch)
def _fetch_bitget(st: State, mark, explicit: bool) -> None:
    if not creds_ok():
        mark("bitget", "skipped", "沒有 API 憑證，沿用上次資料"); st.refresh["hint"] = no_creds_msg(); return
    try:
        from . import archive as archive_mod
        from .client import Client
        rep = archive_mod.run(Client(), st.project / "archive", progress=False, on_step=_sub(st, "bitget"))
        added = sum(v["added"] for v in rep["sources"].values())
        if "snapshot" in rep["errors"]:
            mark("bitget", "failed", "持倉快照讀取失敗：" + json.dumps(rep["errors"]["snapshot"], ensure_ascii=False)[:150])
        elif rep["errors"]:
            mark("bitget", "warn", f"已更新；新增 {added} 筆紀錄；{len(rep['errors'])} 個紀錄來源讀取失敗")
        else:
            mark("bitget", "ok", f"已更新；新增 {added} 筆紀錄")
    except SystemExit as e:
        mark("bitget", "failed", str(e)[:200]); st.refresh["hint"] = no_creds_msg()


def _fetch_simple(key: str, modname: str, cls: str):
    def run(st: State, mark, explicit: bool) -> None:
        mod = __import__(f"assetsboard.{modname}", fromlist=["available"])
        if not mod.available():
            mark(key, "skipped", "未設定 API 憑證"); return
        try:
            kw = {"on_step": _sub(st, key)} if key != "cryptocom" else {}
            r = mod.archive_run(getattr(mod, cls)(), st.project / "archive" / modname, progress=False, **kw)
        except SystemExit as e:
            mark(key, "failed", str(e)[:200]); return
        add = sum(v["added"] for v in r["sources"].values())
        errs = r.get("errors") or {}
        if "snapshot" in errs:
            why = "; ".join(str(x.get("msg", "")) for x in errs["snapshot"] if isinstance(x, dict))[:140]
            mark(key, "failed", "持倉讀取失敗，畫面沿用上次資料" + (f"：{why}" if why else "")
                 + ("（API 金鑰無效／過期 → 換一組唯讀金鑰）" if any(w in why.lower() for w in ("api-key", "apikey", "api key", "signature", "invalid")) else ""))
        elif errs or r.get("warning"):
            mark(key, "warn", f"已更新；新增 {add} 筆" + (f"；{len(errs)} 個紀錄來源讀取失敗（{', '.join(list(errs)[:3])}）" if errs else "")
                 + (f"；⚠ {r['warning']}" if r.get("warning") else ""))
        else:
            mark(key, "ok", f"已更新；新增 {add} 筆")
    return run


def wallets_message(r: dict) -> tuple[str, str]:
    """(status, message) for the 鏈上錢包 row; names the wallet and the reason instead of a bare 'wallet'."""
    tot = f"合計 {r['total_usdt']:,.2f} USDT" if r.get("total_usdt") is not None else None
    bad = [w for w in r.get("wallets") or [] if w.get("error")]
    if not bad:
        return ("ok", tot) if tot else ("skipped", "未設定錢包")
    parts = []
    for w in bad:
        p = f"{w['id']} 讀取失敗（{str(w['error'])[:80]}）"
        p += f"，沿用 {str(w['carried_from'])[5:16].replace('T', ' ')} 的數量" if w.get("carried_from") else "，未計入"
        parts.append(p)
    errs = r.get("errors") if isinstance(r.get("errors"), dict) else {}
    if errs.get("snapshot"):
        return "failed", "讀取失敗：" + str(errs["snapshot"][0].get("msg"))[:160]
    return ("warn" if tot else "failed"), "；".join(([tot] if tot else []) + parts)


def _fetch_wallets(st: State, mark, explicit: bool) -> None:
    from . import wallets as wallets_mod
    r = wallets_mod.archive_run(st.project / "archive" / "wallets", progress=False)
    mark("wallets", *wallets_message(r))


def _fetch_etherfi(st: State, mark, explicit: bool) -> None:
    from . import etherfi_cash as efc_mod
    r = efc_mod.archive_run(st.project / "archive" / "etherfi_cash", progress=False)
    if r.get("total_usdt") is not None:
        mark("etherfi_cash", "warn" if r.get("errors") else "ok", f"已更新；餘額 {r['total_usdt']:,.2f}")
    elif r.get("errors"):
        mark("etherfi_cash", "failed", str(r["errors"])[:160])
    else:
        mark("etherfi_cash", "skipped", "未設定")


def _fetch_ibkr(st: State, mark, explicit: bool) -> None:
    """Gateway snapshot when port 4001 answers (never opens Gateway), then the Flex catch-up (HTTP, no Gateway).
    Flex: at most every 6 h from the global refresh; an explicit IBKR click may retry after 15 min."""
    from . import ibkr as ibkr_mod
    mark("ibkr", "running", "讀取本機 IB Gateway（唯讀）")
    try:
        if ibkr_mod.available():
            try:
                irep = ibkr_mod.archive_run(st.project / "archive" / "ibkr", progress=False)
                mark("ibkr", "ok" if irep.get("snapshot") else "failed", ibkr_mod.refresh_message(irep).removeprefix("IBKR "))
            except ibkr_mod.IbkrUnavailable as e:
                mark("ibkr", "failed", "未更新：" + str(e)[:160])
        else:
            mark("ibkr", "kept", "IB Gateway 未開啟（127.0.0.1:4001 沒有回應）→ 持倉沿用上次快照；"
                                 "要更新：開啟並登入 IB Gateway，等 1 分鐘再按 IBKR 的「⟳ 更新」")
    except Exception as e:  # noqa: BLE001
        mark("ibkr", "failed", f"{type(e).__name__}: {e}")
    mark("ibkr_flex", "running")
    try:
        from . import ibkr_flex
        from .client import credential_sources as cs, load_secret
        iroot = st.project / "archive" / "ibkr"
        hrs = ibkr_flex.CLICK_MIN_HOURS if explicit else ibkr_flex.AUTO_MIN_HOURS
        if not (ibkr_flex.present(iroot) and all(cs(ibkr_flex.FLEX_NAMES).values())):
            mark("ibkr_flex", "skipped", "未設定 Flex Web Service")
            return
        rng = ibkr_flex.catch_up_range(iroot)
        if not rng:
            mark("ibkr_flex", "ok", f"已是最新（涵蓋至 {ibkr_flex.coverage_to(iroot)}）")
        elif ibkr_flex.catch_up_due(iroot, rng, min_hours=hrs):
            mark("ibkr_flex", "running", f"補抓 {rng[0]}–{rng[1]}")
            frep = ibkr_flex.catch_up(iroot, load_secret("IBKR_FLEX_TOKEN"), load_secret("IBKR_FLEX_QUERY_ID"),
                                      st.project / "imports" / "ibkr", min_hours=hrs)
            if frep.get("status") == "ok":
                mark("ibkr_flex", "ok", f"已補到 {frep.get('coverage_to')}")
            else:
                mark("ibkr_flex", "kept", f"涵蓋至 {ibkr_flex.coverage_to(iroot)}（{frep.get('status')}）")
        else:
            wait = "15 分鐘" if explicit else "6 小時（按 IBKR 的「⟳ 更新」可提早到 15 分鐘）"
            mark("ibkr_flex", "kept", f"涵蓋至 {ibkr_flex.coverage_to(iroot)}；{wait}內已試過，稍後再補")
    except Exception as e:  # noqa: BLE001
        mark("ibkr_flex", "failed", f"{type(e).__name__}: {e}")


def _mark_robinhood(st: State, mark) -> None:
    pf = None
    try:
        from . import robinhood as rh_mod
        rows = rh_mod.load_ledger(st.project / "archive" / "robinhood")
        pf = rh_mod.load_portfolio(st.project / "archive" / "robinhood")
        last = max((r["date"] for r in rows), default=None)
        msg = ("手動：沒有 API，傳截圖給投資顧問更新" + (f"；最新紀錄 {last}" if last else "；尚無紀錄")
               + (f"；Portfolio 截圖 {str(pf.get('at'))[:16].replace('T', ' ')}" if pf else ""))
    except Exception as e:  # noqa: BLE001
        msg = f"手動：沒有 API，傳截圖給投資顧問更新（{type(e).__name__}）"
    mark("robinhood", "manual", msg)
    with st.lock:
        st.refresh["sources"]["robinhood"]["last_ok"] = str(pf.get("at"))[:19] if pf and pf.get("at") else None


FETCH = {"bitget": _fetch_bitget, "mexc": _fetch_simple("mexc", "mexc", "MexcClient"),
         "kraken": _fetch_simple("kraken", "kraken", "KrakenClient"),
         "cryptocom": _fetch_simple("cryptocom", "cryptocom", "CdcClient"),
         "wallets": _fetch_wallets, "etherfi_cash": _fetch_etherfi, "ibkr": _fetch_ibkr}


def _run_fetch(st: State, key: str, explicit: bool) -> None:
    """One source, read-only; never raises (a failure only marks that source). Releases the source's busy flag."""
    mark = _marker(st)
    t0 = time.time()
    try:
        mark(key, "running", "讀取中…")
        FETCH[key](st, mark, explicit)
    except BaseException as e:  # noqa: BLE001 - report to UI, keep server alive
        mark(key, "failed", f"{type(e).__name__}: {e}")
        traceback.print_exc()
    finally:
        with st.lock:
            for row in ROWS_OF.get(key, (key,)):
                S = st.refresh["sources"][row]
                if S["status"] in ("pending", "running"):
                    S.update(status="failed", msg=S["msg"] or "未完成", running=False)
            st.busy.discard(key)
            st.refresh["busy"] = sorted(st.busy)
            st.refresh.setdefault("timings", {})[key] = round(time.time() - t0, 1)


# ---------------------------------------------------------------- analysis queue (serialized, coalesced)
def request_analysis(st: State, venues) -> None:
    """Queue a recompute of `venues` ({'*'} = all). One analysis at a time; requests that arrive meanwhile are merged."""
    with st.lock:
        st.pending = set(venues) | (st.pending or set())
        st.refresh["sources"]["analysis"].update(status="pending" if st.analysis_active else "running", running=True)
        if st.analysis_active:
            return
        st.analysis_active = True
    threading.Thread(target=_analysis_worker, args=(st,), daemon=True).start()


def _analysis_worker(st: State) -> None:
    from . import analysis as analysis_mod
    mark = _marker(st)
    while True:
        with st.lock:
            todo, st.pending = st.pending, None
            if not todo:
                st.analysis_active = False
                st.refresh["sources"]["analysis"]["running"] = False
                break
        only = None if "*" in todo or set(analysis_mod.VENUES) <= todo else set(todo)
        names = dict(SOURCES)
        what = "全部" if only is None else "、".join(names.get(v, v) for v in sorted(only))
        mark("analysis", "running", f"重新計算：{what}（其他沿用）" if only else "重新計算全部（成本、盈虧、資金流）")
        before = summarize(_read_analysis(st.project))
        try:
            analysis_mod.run(st.project, only=only)
            mark("analysis", "ok", f"已重新計算：{what}" + ("" if only is None else "（其他交易所沿用上次結果）"))
            with st.lock:
                st.refresh["summary"] = diff_summary(before, summarize(_read_analysis(st.project)))
                if st.refresh["summary"]:
                    st.refresh["summary"]["scope"] = sorted(only) if only else "all"
                st.refresh.setdefault("timings", {})["analysis_" + ("full" if only is None else "partial")] = st.refresh["sources"]["analysis"]["duration_s"]
        except BaseException as e:  # noqa: BLE001
            mark("analysis", "failed", f"{type(e).__name__}: {e}"[:220])
            with st.lock:
                st.refresh["error"] = f"重新計算失敗：{type(e).__name__}: {e}"[:300]
            traceback.print_exc()
        with st.lock:
            st.refresh["last"] = now_oslo().isoformat(timespec="seconds")
            st.save()


# ---------------------------------------------------------------- entry points
def start_source(st: State, key: str) -> bool:
    """Per-source 「⟳ 更新」: fetch only `key`, then recompute only that venue. False when it is already running."""
    if key not in FETCH:
        raise KeyError(key)
    with st.lock:
        if key in st.busy:
            return False
        st.busy.add(key)
        st.refresh["busy"] = sorted(st.busy)
        st.refresh.update(error=None, scope=key)
        for row in ROWS_OF.get(key, (key,)):
            st.refresh["sources"][row].update(status="pending", msg="", running=True)
            st.refresh["sources"][row].pop("_t0", None)

    def job():
        _run_fetch(st, key, explicit=True)
        request_analysis(st, {VENUE_OF.get(r, r) for r in ROWS_OF.get(key, (key,))})
    threading.Thread(target=job, daemon=True).start()
    return True


def start_all(st: State) -> bool:
    """Global 「抓取最新資料」: every source in parallel (sources already running on their own are left alone), then one
    full recompute. False when a global refresh is already running."""
    with st.lock:
        if st.refresh["running"]:
            return False
        keys = [k for k in FETCH if k not in st.busy]
        st.busy.update(keys)
        old = st.refresh["sources"]
        new = _blank_sources(old)
        for k in new:                     # rows of sources that are running on their own keep their live state
            if any(k in ROWS_OF.get(b, (b,)) for b in st.busy - set(keys)):
                new[k] = old[k]
        st.refresh.update(running=True, error=None, log=[], stage="開始…", pct=0, sources=new, summary=None, hint=None,
                          busy=sorted(st.busy), scope="all")
    threading.Thread(target=_refresh_all, args=(st, keys), daemon=True).start()
    return True


def _refresh_all(st: State, keys: list[str]) -> None:
    import concurrent.futures as cf
    R = st.refresh
    t0 = time.time()
    mark = _marker(st)
    try:
        _mark_robinhood(st, mark)
        with cf.ThreadPoolExecutor(max_workers=max(1, len(keys)), thread_name_prefix="ab-src") as ex:
            futs = {ex.submit(_run_fetch, st, k, False): k for k in keys}
            for i, _ in enumerate(cf.as_completed(futs), 1):
                with st.lock:
                    left = [st.refresh["sources"][k]["label"] for k in keys if k in st.busy]
                    R.update(pct=int(80 * i / len(keys)), stage=("等待：" + "、".join(left)) if left else "重新計算…")
        R.setdefault("timings", {})["fetch_parallel"] = round(time.time() - t0, 1)
        request_analysis(st, {"*"})
        while True:                         # keep 'running' until the recompute is done (the button shows progress)
            time.sleep(0.3)
            with st.lock:
                if not st.analysis_active:
                    break
        R.update(stage="完成", pct=100)
        R["timings"]["full_refresh"] = round(time.time() - t0, 1)
    except BaseException as e:  # noqa: BLE001
        R["error"] = f"{type(e).__name__}: {e}"[:300]
        R["stage"] = "失敗"
        traceback.print_exc()
    finally:
        with st.lock:
            R["running"] = False
            R["last"] = now_oslo().isoformat(timespec="seconds")
            st.save()


def _refresh(st: State) -> None:
    """Synchronous global refresh (CLI/tests): same as the button, waits until done."""
    with st.lock:
        keys = [k for k in FETCH if k not in st.busy]
        st.busy.update(keys)
        st.refresh.update(running=True, sources=_blank_sources(st.refresh.get("sources")), log=[], summary=None, error=None)
    _refresh_all(st, keys)


def _overrides_path(st: State) -> Path:
    return st.project / "snapshots" / "analysis_overrides.json"


def app_total_info(st: State) -> dict:
    p = _overrides_path(st)
    ov = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    v, at = ov.get("app_total_assets_usdt"), ov.get("app_total_set_at")
    age = None
    if at:
        try:
            age = (now_oslo() - dt.datetime.fromisoformat(at)).total_seconds() / 3600
        except ValueError:
            age = None
    return {"value": v, "set_at": at, "age_hours": age, "stale": v is not None and (age is None or age > APP_TOTAL_STALE_H)}


def set_app_total(st: State, value) -> dict:
    p = _overrides_path(st)
    ov = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    ov["app_total_assets_usdt"] = value
    ov["app_total_set_at"] = now_oslo().isoformat(timespec="seconds") if value is not None else None
    p.parent.mkdir(exist_ok=True)
    p.write_text(json.dumps(ov, indent=1, ensure_ascii=False), encoding="utf-8")
    return app_total_info(st)


def mexc_earn_info(st: State) -> dict:
    from . import mexc as mexc_mod
    p = _overrides_path(st)
    ov = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    e = mexc_mod.earn_manual(ov)
    return e or {"value": None, "set_at": None, "age_days": None, "stale": False, "label": "手動填入", "source": "manual"}


def set_mexc_earn(st: State, value) -> dict:
    """Store the MEXC Earn balance; the page recomputes totals right away, the next analyze bakes it in."""
    set_override_earn(_overrides_path(st), value)
    return mexc_earn_info(st)


def set_override_earn(path: Path, value) -> None:
    ov = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    ov["mexc_earn_usdt"] = value
    ov["mexc_earn_set_at"] = now_oslo().isoformat(timespec="seconds") if value is not None else None
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(ov, indent=1, ensure_ascii=False), encoding="utf-8")


def make_handler(st: State, port: int):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class H(BaseHTTPRequestHandler):
        server_version = "assetsboard"

        def log_message(self, fmt, *args):  # quiet, no query strings
            pass

        def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False, default=float).encode("utf-8"))

        def _host_ok(self) -> bool:
            if self.headers.get("Host") not in allowed_hosts:
                self._send(403, b'{"error":"host not allowed"}')
                return False
            return True

        def do_GET(self):
            if not self._host_ok():
                return
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                html = (STATIC / "index.html").read_text(encoding="utf-8").replace("__BITGUARD_TOKEN__", st.token)
                return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            if path == "/api/analysis":
                p = st.project / "snapshots" / "analysis_latest.json"
                if not p.exists():
                    return self._json({"error": "尚未產生分析，請執行 " + cli_cmd() + " analyze"}, 404)
                doc = json.loads(p.read_text(encoding="utf-8"))
                doc["app_total"] = app_total_info(st)
                doc["mexc_earn"] = mexc_earn_info(st)
                return self._json(doc)
            if path == "/api/ping":
                return self._json({"app": APP_ID, "version": __version__, "project": st.project.name})
            if path == "/api/status":
                a = st.project / "archive" / "state.json"
                runs = (json.loads(a.read_text(encoding="utf-8")).get("runs") or []) if a.exists() else []
                src = credential_sources()  # presence only, never values
                from . import mexc as mexc_mod
                msrc = mexc_mod.sources()
                from . import kraken as kraken_mod
                ksrc = kraken_mod.sources()
                return self._json({"refresh": st.refresh, "credentials": all(src.values()), "credential_sources": src,
                                   "mexc_credentials": all(msrc.values()), "mexc_credential_sources": msrc,
                                   "mexc_hint": None if all(msrc.values()) else mexc_mod.no_creds_msg(),
                                   "kraken_credentials": all(ksrc.values()), "kraken_credential_sources": ksrc,
                                   "kraken_hint": None if all(ksrc.values()) else kraken_mod.no_creds_msg(),
                                   "cryptocom_credentials": __import__("assetsboard.cryptocom", fromlist=["available"]).available(),
                                   "ibkr_gateway": __import__("assetsboard.ibkr", fromlist=["available"]).available(0.3),
                                   "no_credentials_hint": None if all(src.values()) else no_creds_msg(),
                                   "last_archive_run": runs[-1]["at"] if runs else None, "now": now_oslo().isoformat(timespec="seconds"),
                                   "features": ["per_source"]})
            self._send(404, b'{"error":"not found"}')

        def do_POST(self):
            if not self._host_ok():
                return
            if self.path not in ("/api/refresh", "/api/app-total", "/api/mexc-earn"):
                return self._send(404, b'{"error":"not found"}')
            if not secrets.compare_digest(self.headers.get("X-Bitguard-Token", ""), st.token):
                return self._send(403, b'{"error":"bad token"}')
            if self.path == "/api/mexc-earn":
                try:
                    n = min(int(self.headers.get("Content-Length") or 0), 1000)
                    v = json.loads(self.rfile.read(n) or b"{}").get("value")
                    v = None if v in (None, "") else float(v)
                    if v is not None and not (0 <= v < 1e9):
                        raise ValueError
                except (ValueError, TypeError, AttributeError):
                    return self._json({"error": "請輸入 0 或正數（USDT）"}, 400)
                with st.lock:
                    if st.refresh["running"] or "mexc" in st.busy:
                        return self._json({"error": "正在抓取最新資料，請稍後再存"}, 409)
                return self._json(set_mexc_earn(st, v))
            if self.path == "/api/app-total":
                try:
                    n = min(int(self.headers.get("Content-Length") or 0), 1000)
                    v = json.loads(self.rfile.read(n) or b"{}").get("value")
                    v = None if v in (None, "") else float(v)
                    if v is not None and not (0 < v < 1e9):
                        raise ValueError
                except (ValueError, TypeError, AttributeError):
                    return self._json({"error": "請輸入正數（USDT）"}, 400)
                return self._json(set_app_total(st, v))
            # 「抓取最新資料」 (body {} → every source in parallel) or one section's 「⟳ 更新」 (body {"source": key})
            try:
                n = min(int(self.headers.get("Content-Length") or 0), 1000)
                key = (json.loads(self.rfile.read(n) or b"{}") or {}).get("source")
            except (ValueError, TypeError, AttributeError):
                key = None
            if key:
                if key == "robinhood":
                    return self._json({"started": False, "manual": True, "error": "Robinhood 沒有 API：傳截圖給投資顧問更新"}, 400)
                if key not in FETCH:
                    return self._json({"error": "unknown source"}, 400)
                ok_ = start_source(st, key)
                return self._json({"started": ok_, "running": not ok_, "source": key})
            ok_ = start_all(st)
            self._json({"started": ok_, "running": not ok_})

    return H


def is_assetsboard(port: int, timeout: float = 1.0) -> bool:
    """Does the server on 127.0.0.1:port answer /api/ping as assetsboard?"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8")).get("app") == APP_ID
    except Exception:  # noqa: BLE001 - anything else (IOTA dashboard, closed port, non-JSON) is "no"
        return False


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def start(project: Path, port: int = DEFAULT_PORT, tries: int = 50) -> tuple[LocalServer, str]:
    """Bind the first free port from `port` upward and serve in a background thread."""
    st = State(project)
    last: OSError | None = None
    for p in range(port, port + tries):
        if not _port_free(p):
            continue
        try:
            httpd = LocalServer(("127.0.0.1", p), make_handler(st, p))
        except OSError as e:
            last = e
            continue
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{p}/"
        if not is_assetsboard(p, timeout=3):
            httpd.shutdown(); httpd.server_close()
            raise RuntimeError(f"啟動後自我檢查失敗：{url}")
        return httpd, url
    raise RuntimeError(f"{port}–{port + tries - 1} 沒有可用的連接埠（{last}）")


def serve(project: Path, port: int = DEFAULT_PORT, open_browser: bool = True) -> int:
    if not _port_free(port):
        who = "另一個 assetsboard" if is_assetsboard(port) else "其他程式"
        print(f"連接埠 {port} 已被{who}使用，改用下一個可用的連接埠。", flush=True)
    httpd, url = start(project, port)
    print(f"assetsboard 儀表板：{url}（只綁定本機；Ctrl+C 結束）", flush=True)
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        httpd.server_close()
    return 0
