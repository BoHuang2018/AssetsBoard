"""Local dashboard: python3 -m assetsboard web [--port 47651].

- Binds 127.0.0.1 only; rejects requests whose Host header is not localhost (DNS-rebinding guard).
- Serves assetsboard/static/index.html and JSON read from local files only (snapshots/analysis_latest.json,
  archive/state.json, snapshots/analysis_overrides.json). Two actions, both need the per-launch token
  embedded in the page: POST /api/refresh (GET-only `archive` incl. snapshot, then `analyze`) and
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
                "（把三個值存進 macOS 鑰匙圈；之後從 Dock 開啟的 App 也能重新整理。第一次讀取時 macOS 可能會詢問是否允許存取，請按「允許」或「永遠允許」。）")
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


class State:
    def __init__(self, project: Path):
        self.project = project
        self.token = secrets.token_urlsafe(24)
        self.lock = threading.Lock()
        self.refresh = {"running": False, "last": None, "error": None, "log": [], "stage": "", "pct": 0}


def _refresh(st: State) -> None:
    """archive (incremental, read-only; also writes holdings snapshots; MEXC/Kraken when configured) → analyze."""
    from . import analysis as analysis_mod
    from . import archive as archive_mod
    from .client import Client
    R = st.refresh
    try:
        if not creds_ok():
            raise RuntimeError(no_creds_msg())
        c = Client()
        R.update(stage="讀取憑證並連線 Bitget（唯讀）…", pct=1)

        def on_step(label: str, done: int, total: int) -> None:
            name = "持倉快照" if label == "snapshot" else label.replace("transfer_records:", "劃轉紀錄 ")
            R.update(stage=f"封存原始紀錄：{name}（{done}/{total}）", pct=int(5 + 75 * done / max(total, 1)))

        rep = archive_mod.run(c, st.project / "archive", progress=False, on_step=on_step)
        added = sum(v["added"] for v in rep["sources"].values())
        R["log"].append(f"封存完成：新增 {added} 筆" + (f"；{len(rep['errors'])} 個來源讀取失敗" if rep["errors"] else ""))
        if "snapshot" in rep["errors"]:
            raise RuntimeError("持倉快照讀取失敗：" + json.dumps(rep["errors"]["snapshot"], ensure_ascii=False)[:200])
        from . import mexc as mexc_mod
        if mexc_mod.available():
            def on_mexc(label: str, done: int, total: int) -> None:
                R.update(stage=f"MEXC 封存：{label.replace('mexc_', '')}（{done}/{total}）", pct=int(80 + 5 * done / max(total, 1)))
            try:
                mrep = mexc_mod.archive_run(mexc_mod.MexcClient(), st.project / "archive" / "mexc", progress=False, on_step=on_mexc)
                madd = sum(v["added"] for v in mrep["sources"].values())
                R["log"].append(f"MEXC 封存：新增 {madd} 筆" + (f"；{len(mrep['errors'])} 個來源讀取失敗" if mrep["errors"] else ""))
            except SystemExit as e:
                R["log"].append("MEXC 略過：" + str(e)[:200])
            except Exception as e:  # noqa: BLE001 - optional exchange must not break the Bitget refresh
                R["log"].append(f"MEXC 失敗：{type(e).__name__}: {e}"[:200])
        else:
            R["log"].append("MEXC 未設定憑證，略過")
        from . import kraken as kraken_mod
        if kraken_mod.available():
            def on_kraken(label: str, done: int, total: int) -> None:
                R.update(stage=f"Kraken 封存：{label.replace('kraken_', '')}（{done}/{total}）", pct=int(85 + 1 * done / max(total, 1)))
            try:
                krep = kraken_mod.archive_run(kraken_mod.KrakenClient(), st.project / "archive" / "kraken", progress=False, on_step=on_kraken)
                kadd = sum(v["added"] for v in krep["sources"].values())
                R["log"].append(f"Kraken 封存：新增 {kadd} 筆" + (f"；{len(krep['errors'])} 個來源讀取失敗" if krep["errors"] else ""))
            except SystemExit as e:
                R["log"].append("Kraken 略過：" + str(e)[:200])
            except Exception as e:  # noqa: BLE001 - optional exchange must not break the refresh
                R["log"].append(f"Kraken 失敗：{type(e).__name__}: {e}"[:200])
        else:
            R["log"].append("Kraken 未設定憑證，略過")
        from . import cryptocom as cdc_mod
        if cdc_mod.available():
            R.update(stage="Crypto.com 封存", pct=86)
            try:
                crep = cdc_mod.archive_run(cdc_mod.CdcClient(), st.project / "archive" / "cryptocom", progress=False)
                cadd = sum(v["added"] for v in crep["sources"].values())
                R["log"].append(f"Crypto.com 封存：新增 {cadd} 筆交易紀錄" + (f"；讀取失敗：{', '.join(crep['errors'])}" if crep["errors"] else "")
                                + (f"；⚠ {crep['warning']}" if crep.get("warning") else ""))
            except SystemExit as e:
                R["log"].append("Crypto.com 略過：" + str(e)[:200])
            except Exception as e:  # noqa: BLE001 - optional exchange must not break the refresh
                R["log"].append(f"Crypto.com 失敗：{type(e).__name__}: {e}"[:200])
        else:
            R["log"].append("Crypto.com 未設定憑證，略過")
        R.update(stage="鏈上錢包", pct=88)
        try:
            from . import wallets as wallets_mod
            wrep = wallets_mod.archive_run(st.project / "archive" / "wallets", progress=False)
            R["log"].append("鏈上錢包：合計 " + (f"{wrep.get('total_usdt'):,.2f} USDT" if wrep.get("total_usdt") is not None else "—")
                            + (f"；讀取失敗：{', '.join(wrep['errors'])}" if wrep.get("errors") else ""))
        except Exception as e:  # noqa: BLE001
            R["log"].append(f"鏈上錢包失敗：{type(e).__name__}: {e}"[:200])
        R.update(stage="ether.fi Cash", pct=89)
        try:
            from . import etherfi_cash as efc_mod
            erep = efc_mod.archive_run(st.project / "archive" / "etherfi_cash", progress=False)
            if erep.get("total_usdt") is not None:
                R["log"].append(f"ether.fi Cash：float {erep['total_usdt']:,.2f}；開銷代理 {erep.get('spend_proxy_usdt') or 0:,.2f}")
            elif erep.get("errors"):
                R["log"].append("ether.fi Cash：" + str(erep["errors"])[:160])
            else:
                R["log"].append("ether.fi Cash：未設定")
        except Exception as e:  # noqa: BLE001
            R["log"].append(f"ether.fi Cash 失敗：{type(e).__name__}: {e}"[:200])

        from . import ibkr as ibkr_mod
        if ibkr_mod.available():
            R.update(stage="IBKR：讀取本機 IB Gateway（唯讀）…", pct=86)
            try:
                irep = ibkr_mod.archive_run(st.project / "archive" / "ibkr", progress=False)
                R["log"].append("IBKR 已更新" + (f"；{len(irep['errors'])} 項錯誤" if irep["errors"] else ""))
            except ibkr_mod.IbkrUnavailable as e:
                R["log"].append("IBKR 略過：" + str(e)[:160])
            except Exception as e:  # noqa: BLE001 - optional source must not break the refresh
                R["log"].append(f"IBKR 失敗：{type(e).__name__}: {e}"[:200])
        else:
            R["log"].append("IBKR：IB Gateway 未開啟，略過")
        R.update(stage="重新計算分析（成本、盈虧、資金流）…", pct=86)
        analysis_mod.run(st.project)
        R["log"].append("分析已更新")
        R.update(stage="完成", pct=100, error=None)
    except SystemExit as e:  # missing credential raised by load_secret
        R["error"] = str(e)[:300] + "\n" + no_creds_msg()
    except RuntimeError as e:
        R["error"] = str(e)[:600]
    except BaseException as e:  # noqa: BLE001 - report to UI, keep server alive
        R["error"] = f"{type(e).__name__}: {e}"[:300]
        traceback.print_exc()
    finally:
        R["running"] = False
        R["last"] = now_oslo().isoformat(timespec="seconds")
        if R["error"]:
            R["stage"] = "失敗"


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
                        return self._json({"error": "正在重新整理，請稍後再存"}, 409)
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
            if not creds_ok():
                return self._json({"started": False, "error": no_creds_msg()}, 409)
            with st.lock:
                if st.refresh["running"]:
                    return self._json({"started": False, "running": True})
                st.refresh.update(running=True, error=None, log=[], stage="開始…", pct=0)
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
