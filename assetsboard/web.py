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


def _blank_sources() -> dict:
    return {k: {"label": n, "status": "pending", "msg": "", "at": None} for k, n in SOURCES}


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
        self.lock = threading.Lock()
        self.refresh = {"running": False, "last": None, "error": None, "log": [], "stage": "", "pct": 0,
                        "sources": _blank_sources(), "summary": None, "hint": None}
        lp = project / "snapshots" / LAST_RESULT          # last result survives an app restart
        try:
            if lp.exists():
                last = json.loads(lp.read_text(encoding="utf-8"))
                self.refresh.update({k: last.get(k) for k in ("last", "error", "log", "sources", "summary", "hint")})
                self.refresh["stage"] = ""
        except (ValueError, OSError):
            pass


def _read_analysis(project: Path) -> dict | None:
    p = project / "snapshots" / "analysis_latest.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    except (ValueError, OSError):
        return None


def _refresh(st: State) -> None:
    """Fetch every configured source read-only (each one independently: one failing never blocks the others), then analyze.
    Per-source status goes to st.refresh['sources'] for the UI; the result is also saved to snapshots/refresh_last.json."""
    from . import analysis as analysis_mod
    R = st.refresh
    S = R["sources"] = _blank_sources()
    order = [k for k, _ in SOURCES]
    log = R["log"]

    def mark(key: str, status: str, msg: str = "") -> None:
        S[key].update(status=status, msg=msg[:220], at=now_oslo().isoformat(timespec="seconds"))
        if status != "running":
            log.append(f"{S[key]['label']}：{msg}" if msg else S[key]["label"])
        if status == "running":
            R.update(stage=f"{S[key]['label']}…" + (f"（{msg}）" if msg else ""),
                     pct=int(100 * order.index(key) / len(order)))

    def sub(key: str):   # sub-step progress callback for long archives
        def cb(label: str, done: int, total: int) -> None:
            base = order.index(key) / len(order)
            R.update(stage=f"{S[key]['label']}：{str(label).split(':')[-1]}（{done}/{total}）",
                     pct=int(100 * (base + (done / max(total, 1)) / len(order))))
        return cb

    before = summarize(_read_analysis(st.project))
    try:
        # ---- Bitget
        mark("bitget", "running")
        if not creds_ok():
            mark("bitget", "skipped", "沒有 API 憑證，沿用上次資料"); R["hint"] = no_creds_msg()
        else:
            try:
                from . import archive as archive_mod
                from .client import Client
                rep = archive_mod.run(Client(), st.project / "archive", progress=False, on_step=sub("bitget"))
                added = sum(v["added"] for v in rep["sources"].values())
                if "snapshot" in rep["errors"]:
                    mark("bitget", "failed", "持倉快照讀取失敗：" + json.dumps(rep["errors"]["snapshot"], ensure_ascii=False)[:150])
                elif rep["errors"]:
                    mark("bitget", "warn", f"已更新；新增 {added} 筆紀錄；{len(rep['errors'])} 個紀錄來源讀取失敗")
                else:
                    mark("bitget", "ok", f"已更新；新增 {added} 筆紀錄")
            except SystemExit as e:
                mark("bitget", "failed", str(e)[:200]); R["hint"] = no_creds_msg()
            except Exception as e:  # noqa: BLE001
                mark("bitget", "failed", f"{type(e).__name__}: {e}")

        # ---- MEXC / Kraken (same shape)
        for key, modname, cls in (("mexc", "mexc", "MexcClient"), ("kraken", "kraken", "KrakenClient")):
            mod = __import__(f"assetsboard.{modname}", fromlist=["available"])
            mark(key, "running")
            if not mod.available():
                mark(key, "skipped", "未設定 API 憑證"); continue
            try:
                r = mod.archive_run(getattr(mod, cls)(), st.project / "archive" / modname, progress=False, on_step=sub(key))
                add = sum(v["added"] for v in r["sources"].values())
                if "snapshot" in (r.get("errors") or {}):
                    why = "; ".join(str(x.get("msg", "")) for x in r["errors"]["snapshot"] if isinstance(x, dict))[:120]
                    mark(key, "failed", "持倉讀取失敗，沿用上次資料" + (f"：{why}" if why else ""))
                elif r.get("errors"):
                    mark(key, "warn", f"已更新；新增 {add} 筆；{len(r['errors'])} 個紀錄來源讀取失敗（{', '.join(list(r['errors'])[:3])}）")
                else:
                    mark(key, "ok", f"已更新；新增 {add} 筆")
            except SystemExit as e:
                mark(key, "failed", str(e)[:200])
            except Exception as e:  # noqa: BLE001
                mark(key, "failed", f"{type(e).__name__}: {e}")

        # ---- Crypto.com
        from . import cryptocom as cdc_mod
        mark("cryptocom", "running")
        if not cdc_mod.available():
            mark("cryptocom", "skipped", "未設定 API 憑證")
        else:
            try:
                r = cdc_mod.archive_run(cdc_mod.CdcClient(), st.project / "archive" / "cryptocom", progress=False)
                add = sum(v["added"] for v in r["sources"].values())
                msg = f"已更新；新增 {add} 筆" + (f"；讀取失敗：{', '.join(r['errors'])}" if r["errors"] else "") + \
                    (f"；⚠ {r['warning']}" if r.get("warning") else "")
                mark("cryptocom", "warn" if r["errors"] or r.get("warning") else "ok", msg)
            except SystemExit as e:
                mark("cryptocom", "failed", str(e)[:200])
            except Exception as e:  # noqa: BLE001
                mark("cryptocom", "failed", f"{type(e).__name__}: {e}")

        # ---- on-chain wallets
        mark("wallets", "running")
        try:
            from . import wallets as wallets_mod
            r = wallets_mod.archive_run(st.project / "archive" / "wallets", progress=False)
            tot = f"合計 {r.get('total_usdt'):,.2f} USDT" if r.get("total_usdt") is not None else "未設定錢包"
            if r.get("errors"):
                mark("wallets", "warn", f"{tot}；讀取失敗：{', '.join(map(str, r['errors']))}")
            else:
                mark("wallets", "ok" if r.get("total_usdt") is not None else "skipped", tot)
        except Exception as e:  # noqa: BLE001
            mark("wallets", "failed", f"{type(e).__name__}: {e}")

        # ---- ether.fi Cash
        mark("etherfi_cash", "running")
        try:
            from . import etherfi_cash as efc_mod
            r = efc_mod.archive_run(st.project / "archive" / "etherfi_cash", progress=False)
            if r.get("total_usdt") is not None:
                mark("etherfi_cash", "warn" if r.get("errors") else "ok", f"已更新；餘額 {r['total_usdt']:,.2f}")
            elif r.get("errors"):
                mark("etherfi_cash", "failed", str(r["errors"])[:160])
            else:
                mark("etherfi_cash", "skipped", "未設定")
        except Exception as e:  # noqa: BLE001
            mark("etherfi_cash", "failed", f"{type(e).__name__}: {e}")

        # ---- IBKR live (only when the user has IB Gateway open)
        from . import ibkr as ibkr_mod
        mark("ibkr", "running", "讀取本機 IB Gateway（唯讀）")
        if ibkr_mod.available():
            try:
                irep = ibkr_mod.archive_run(st.project / "archive" / "ibkr", progress=False)
                mark("ibkr", "ok" if irep.get("snapshot") else "failed", ibkr_mod.refresh_message(irep).removeprefix("IBKR "))
            except ibkr_mod.IbkrUnavailable as e:
                mark("ibkr", "failed", "未更新：" + str(e)[:160])
            except Exception as e:  # noqa: BLE001
                mark("ibkr", "failed", f"{type(e).__name__}: {e}")
        else:
            mark("ibkr", "kept", "IB Gateway 未開啟 → 沿用上次快照（要更新：開啟並登入 IB Gateway，等 1 分鐘再按）")

        # ---- IBKR Flex (HTTP, no Gateway): deposits/trades up to the last business day; at most every 6 h
        mark("ibkr_flex", "running")
        try:
            from . import ibkr_flex
            from .client import credential_sources as cs, load_secret
            iroot = st.project / "archive" / "ibkr"
            if not (ibkr_flex.present(iroot) and all(cs(ibkr_flex.FLEX_NAMES).values())):
                mark("ibkr_flex", "skipped", "未設定 Flex Web Service")
            else:
                rng = ibkr_flex.catch_up_range(iroot)
                if not rng:
                    mark("ibkr_flex", "ok", f"已是最新（涵蓋至 {ibkr_flex.coverage_to(iroot)}）")
                elif ibkr_flex.catch_up_due(iroot, rng):
                    mark("ibkr_flex", "running", f"補抓 {rng[0]}–{rng[1]}")
                    frep = ibkr_flex.catch_up(iroot, load_secret("IBKR_FLEX_TOKEN"), load_secret("IBKR_FLEX_QUERY_ID"),
                                              st.project / "imports" / "ibkr")
                    mark("ibkr_flex", "ok", f"已補到 {frep.get('coverage_to')}")
                else:
                    mark("ibkr_flex", "kept", f"涵蓋至 {ibkr_flex.coverage_to(iroot)}；6 小時內已試過，稍後再補")
        except Exception as e:  # noqa: BLE001
            mark("ibkr_flex", "failed", f"{type(e).__name__}: {e}")

        # ---- Robinhood: no API
        try:
            from . import robinhood as rh_mod
            rows = rh_mod.load_ledger(st.project / "archive" / "robinhood")
            pf = rh_mod.load_portfolio(st.project / "archive" / "robinhood")
            last = max((r["date"] for r in rows), default=None)
            mark("robinhood", "manual", ("無 API，靠截圖手動更新" + (f"；最新紀錄 {last}" if last else "；尚無紀錄")
                                         + (f"；Portfolio 截圖 {str(pf.get('at'))[:16].replace('T', ' ')}" if pf else "")))
        except Exception as e:  # noqa: BLE001
            mark("robinhood", "manual", f"無 API，靠截圖手動更新（{type(e).__name__}）")

        # ---- analysis
        mark("analysis", "running", "成本、盈虧、資金流")
        analysis_mod.run(st.project)
        mark("analysis", "ok", "已重新計算")
        R["summary"] = diff_summary(before, summarize(_read_analysis(st.project)))
        R.update(stage="完成", pct=100, error=None)
    except SystemExit as e:
        R["error"] = str(e)[:300]
    except BaseException as e:  # noqa: BLE001 - report to UI, keep server alive
        R["error"] = f"{type(e).__name__}: {e}"[:300]
        traceback.print_exc()
    finally:
        for v in S.values():
            if v["status"] in ("pending", "running"):
                v.update(status="failed" if R["error"] else "skipped", msg=v["msg"] or ("未執行" if R["error"] else ""))
        R["running"] = False
        R["last"] = now_oslo().isoformat(timespec="seconds")
        if R["error"]:
            R["stage"] = "失敗"
        try:
            (st.project / "snapshots").mkdir(exist_ok=True)
            (st.project / "snapshots" / LAST_RESULT).write_text(json.dumps(
                {k: R.get(k) for k in ("last", "error", "log", "sources", "summary", "hint")}, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass


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
                                   "last_archive_run": runs[-1]["at"] if runs else None, "now": now_oslo().isoformat(timespec="seconds")})
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
                    if st.refresh["running"]:
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
            with st.lock:   # without Bitget credentials the other sources still refresh (Bitget is marked skipped)
                if st.refresh["running"]:
                    return self._json({"started": False, "running": True})
                st.refresh.update(running=True, error=None, log=[], stage="開始…", pct=0, sources=_blank_sources(),
                                  summary=None, hint=None)
            threading.Thread(target=_refresh, args=(st,), daemon=True).start()
            self._json({"started": True})

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
