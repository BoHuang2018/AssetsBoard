"""「抓取最新資料」 (parallel) and per-source 「⟳ 更新」: per-source status, one failure never blocks the others, before/after summary, last result persisted."""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from assetsboard import web


def doc(total, pnl, venues):
    return {"combined": {"total": total, "pnl": pnl, "pnl_pct": pnl / (total - pnl) * 100},
            "exchanges": {k: {"present": True, "total": v} for k, v in venues.items()}}


class Summary(unittest.TestCase):
    def test_diff(self):
        d = web.diff_summary(web.summarize(doc(100, 10, {"bitget": 60, "mexc": 40})),
                             web.summarize(doc(110, 12, {"bitget": 70, "mexc": 40})))
        self.assertAlmostEqual(d["total_change"], 10)
        self.assertAlmostEqual(d["pnl_change"], 2)
        self.assertEqual([v["key"] for v in d["venues"]], ["bitget"])        # unchanged venues are not listed
        self.assertIsNone(web.diff_summary(None, None))
        self.assertIsNone(web.diff_summary(None, web.summarize(doc(1, 0, {})))["total_change"])


class Refresh(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.p = Path(self.td.name)
        (self.p / "snapshots").mkdir()
        (self.p / "snapshots" / "analysis_latest.json").write_text(json.dumps(doc(100, 10, {"kraken": 50, "wallets": 50})))

    def tearDown(self):
        self.td.cleanup()

    def _run(self):
        from assetsboard import analysis, cryptocom, etherfi_cash, ibkr, ibkr_flex, kraken, mexc, wallets

        def fake_analysis(project, only=None):
            (project / "snapshots" / "analysis_latest.json").write_text(json.dumps(doc(105, 15, {"kraken": 50, "wallets": 55})))
        st = web.State(self.p)
        with mock.patch.object(web, "creds_ok", return_value=False), \
                mock.patch.object(mexc, "available", return_value=False), \
                mock.patch.object(kraken, "available", return_value=True), \
                mock.patch.object(kraken, "KrakenClient", return_value=object()), \
                mock.patch.object(kraken, "archive_run", side_effect=RuntimeError("EAPI:Invalid key")), \
                mock.patch.object(cryptocom, "available", return_value=False), \
                mock.patch.object(wallets, "archive_run", return_value={"total_usdt": 55.0, "errors": []}), \
                mock.patch.object(etherfi_cash, "archive_run", return_value={}), \
                mock.patch.object(ibkr, "available", return_value=False), \
                mock.patch.object(ibkr_flex, "present", return_value=False), \
                mock.patch.object(analysis, "run", side_effect=fake_analysis):
            st.refresh["running"] = True
            web._refresh(st)
        return st

    def test_statuses_and_summary(self):
        st = self._run()
        R = st.refresh
        S = {k: v["status"] for k, v in R["sources"].items()}
        self.assertEqual(S["bitget"], "skipped")           # no credentials: skipped, others still run
        self.assertEqual(S["kraken"], "failed")
        self.assertIn("Invalid key", R["sources"]["kraken"]["msg"])
        self.assertEqual(S["wallets"], "ok")
        self.assertEqual(S["etherfi_cash"], "skipped")
        self.assertEqual(S["ibkr"], "kept")                # Gateway closed → previous snapshot kept
        self.assertEqual(S["robinhood"], "manual")
        self.assertEqual(S["analysis"], "ok")
        self.assertFalse(R["running"]) ; self.assertIsNone(R["error"])
        self.assertAlmostEqual(R["summary"]["total_change"], 5)
        self.assertEqual([v["key"] for v in R["summary"]["venues"]], ["wallets"])
        self.assertTrue(R["hint"])                          # tells how to add Bitget credentials
        # persisted: a new app instance shows the last result
        st2 = web.State(self.p)
        self.assertEqual(st2.refresh["sources"]["kraken"]["status"], "failed")
        self.assertEqual(st2.refresh["last"], R["last"])
        self.assertFalse(st2.refresh["running"])


if __name__ == "__main__":
    unittest.main()


class PerSource(unittest.TestCase):
    """One section's 「⟳ 更新」 fetches only that source and recomputes only that venue; the same source never runs twice
    at once; different sources run concurrently."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.p = Path(self.td.name)
        (self.p / "snapshots").mkdir()
        (self.p / "snapshots" / "analysis_latest.json").write_text(json.dumps(doc(100, 10, {"kraken": 50, "wallets": 50})))
        self.st = web.State(self.p)
        self.only = []
        self.gate = threading.Event()
        self.inside = []

        def slow(name):
            def f(st, mark, explicit):
                self.inside.append((name, explicit))
                self.gate.wait(5)
                mark(name, "ok", "done")
            return f
        self.fetch = mock.patch.dict(web.FETCH, {k: slow(k) for k in web.FETCH})
        self.fetch.start()

        def fake_analysis(project, only=None):
            self.only.append(only)
            (project / "snapshots" / "analysis_latest.json").write_text(json.dumps(doc(101, 11, {"kraken": 51, "wallets": 50})))
        from assetsboard import analysis
        self.an = mock.patch.object(analysis, "run", side_effect=fake_analysis)
        self.an.start()

    def tearDown(self):
        self.gate.set()
        self.fetch.stop(); self.an.stop()
        self.td.cleanup()

    def wait_idle(self):
        for _ in range(200):
            with self.st.lock:
                if not self.st.busy and not self.st.analysis_active and not self.st.refresh["running"]:
                    return
            time.sleep(0.02)
        self.fail("did not finish")

    def test_single_source_no_overlap_partial_recompute(self):
        self.assertTrue(web.start_source(self.st, "kraken"))
        self.assertFalse(web.start_source(self.st, "kraken"))          # same source: refused while running
        self.assertTrue(web.start_source(self.st, "wallets"))          # another source: runs concurrently
        time.sleep(0.1)
        self.assertEqual(sorted(n for n, _ in self.inside), ["kraken", "wallets"])
        self.assertTrue(all(e for _, e in self.inside))                # explicit click
        self.assertEqual(self.st.refresh["sources"]["kraken"]["status"], "running")
        self.gate.set()
        self.wait_idle()
        R = self.st.refresh
        self.assertEqual(R["sources"]["kraken"]["status"], "ok")
        self.assertTrue(R["sources"]["kraken"]["last_ok"])
        self.assertIsNotNone(R["sources"]["kraken"]["duration_s"])
        self.assertTrue(all(o is not None and o <= {"kraken", "wallets"} for o in self.only))   # never a full recompute
        self.assertEqual(set().union(*self.only), {"kraken", "wallets"})
        self.assertEqual(R["sources"]["analysis"]["status"], "ok")
        self.assertAlmostEqual(R["summary"]["total_after"], 101)        # summary of the last recompute
        self.assertIn("kraken", R["timings"])
        self.assertEqual(R["sources"]["bitget"]["status"], "pending")  # untouched

    def test_ibkr_button_updates_both_rows_and_recomputes_ibkr(self):
        def ib(st, mark, explicit):
            mark("ibkr", "kept", "IB Gateway 未開啟"); mark("ibkr_flex", "ok", "x" if explicit else "auto")
        with mock.patch.dict(web.FETCH, {"ibkr": ib}):
            web.start_source(self.st, "ibkr")
            self.wait_idle()
        S = self.st.refresh["sources"]
        self.assertEqual((S["ibkr"]["status"], S["ibkr_flex"]["status"], S["ibkr_flex"]["msg"]), ("kept", "ok", "x"))
        self.assertEqual(self.only, [{"ibkr"}])

    def test_global_runs_in_parallel_then_one_full_recompute(self):
        self.assertTrue(web.start_all(self.st))
        self.assertFalse(web.start_all(self.st))
        self.assertFalse(web.start_source(self.st, "mexc"))            # already part of the global run
        for _ in range(100):
            if len(self.inside) == len(web.FETCH):
                break
            time.sleep(0.02)
        self.assertEqual(len(self.inside), len(web.FETCH))              # all started before any finished → parallel
        self.assertTrue(not any(e for _, e in self.inside))
        self.gate.set()
        self.wait_idle()
        self.assertEqual(self.only, [None])
        R = self.st.refresh
        self.assertEqual(R["sources"]["robinhood"]["status"], "manual")
        self.assertIn("full_refresh", R["timings"])
        self.assertEqual(json.loads((self.p / "snapshots" / web.LAST_RESULT).read_text())["sources"]["kraken"]["status"], "ok")


class WalletMessage(unittest.TestCase):
    def test_names_wallet_and_reason(self):
        st, msg = web.wallets_message({"total_usdt": 300.0, "errors": {"wallet": [{"code": "tao", "msg": "HTTP 502"}]},
                                       "wallets": [{"id": "arb", "error": None},
                                                   {"id": "tao", "error": "taoscan: HTTP 502", "carried_from": "2026-10-11T12:00:00+02:00"}]})
        self.assertEqual(st, "warn")
        self.assertIn("tao 讀取失敗（taoscan: HTTP 502）", msg)
        self.assertIn("沿用 10-11 12:00", msg)
        self.assertEqual(web.wallets_message({"total_usdt": 5.0, "wallets": [{"id": "a"}]})[0], "ok")



class MexcRefused(unittest.TestCase):
    def test_700001_explains_instead_of_blaming_the_key(self):
        from assetsboard import mexc
        td = tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup)
        st = web.State(Path(td.name)); marks = []
        rep = {"sources": {}, "errors": {"snapshot": [{"code": 700001, "msg": "700001 API-key format invalid"}]}}
        with mock.patch.object(mexc, "available", return_value=True), mock.patch.object(mexc, "MexcClient", return_value=object()), \
                mock.patch.object(mexc, "archive_run", return_value=rep):
            web.FETCH["mexc"](st, lambda k, s, m="": marks.append((k, s, m)), True)
        k, s, m = marks[-1]
        self.assertEqual((k, s), ("mexc", "failed"))
        self.assertIn("700001", m); self.assertIn("鑰匙圈內容正確", m); self.assertNotIn("換一組", m)
