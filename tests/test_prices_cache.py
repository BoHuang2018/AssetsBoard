"""Price cache: a coin asked within 6 h is not asked again (delisted coins / coins listed after the window used to be
re-fetched on every analyze, the slowest part of a refresh); fetches run concurrently and all land in the cache."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from assetsboard import analysis as A

DAY = A.DAY


class PricesCache(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.path = Path(self.td.name) / "price_cache.json"

    def tearDown(self):
        self.td.cleanup()

    def test_recently_checked_not_refetched_and_parallel_results_kept(self):
        today = int(time.time() * 1000) // DAY
        calls = []

        def fake(path, params):
            calls.append(params["symbol"])
            if params["symbol"] == "GONEUSDT":
                return {"code": "40034", "data": []}
            return {"code": "00000", "data": [[str((today - 300 + i) * DAY), "0", "0", "0", "2.0"] for i in range(200)]}
        since = (today - 100) * DAY
        with mock.patch.object(A, "public_get", side_effect=fake):
            p = A.Prices(self.path)
            p.ensure(["ABC", "XYZ", "GONE", "USDT"], since)
            n1 = len(calls)
            self.assertEqual(sorted(set(calls)), ["ABCUSDT", "GONEUSDT", "XYZUSDT"])
            self.assertEqual(p.at("ABC", (today - 150) * DAY), 2.0)
            self.assertEqual([m["coin"] for m in p.misses], ["GONE"])
            p2 = A.Prices(self.path)             # new run within 6 h: nothing re-asked, miss still reported
            p2.ensure(["ABC", "XYZ", "GONE"], since)
            self.assertEqual(len(calls), n1)
            self.assertEqual([m["coin"] for m in p2.misses], ["GONE"])
        doc = json.loads(self.path.read_text())
        self.assertEqual(sorted(doc["checked"]), ["ABC", "GONE", "XYZ"])


if __name__ == "__main__":
    unittest.main()
