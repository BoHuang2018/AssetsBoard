"""「抓取最新資料」: per-source status, one failure never blocks the others, before/after summary, last result persisted."""
import json
import tempfile
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

        def fake_analysis(project):
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
