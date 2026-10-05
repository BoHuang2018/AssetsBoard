"""Offline tests for assetsboard.mexc (no network, no real keys). Run: python3 -m unittest discover -s tests"""
import hashlib
import hmac
import json
import os
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from assetsboard import mexc

DOC_SECRET = "45d0b3c26f2644f19bfb98b07741b2f5"   # MEXC Spot v3 docs, "SIGNED Endpoint Examples"
DOC_QUERY = "symbol=BTCUSDT&side=BUY&type=LIMIT&quantity=1&price=11&recvWindow=5000&timestamp=1644489390087"


class Signing(unittest.TestCase):
    def test_doc_example_query_string(self):
        # docs Example 2 (query string) – also what `openssl dgst -sha256 -hmac` prints for this string
        self.assertEqual(mexc.spot_signature(DOC_SECRET, DOC_QUERY),
                         "fd3e4e8543c5188531eb7279d68ae7d26a573d0fc5ab0d18eb692451654d837a")

    def test_doc_example_mixed(self):
        # docs Example 3: query + body concatenated without '&'
        self.assertEqual(mexc.spot_signature(DOC_SECRET, "symbol=BTCUSDT&side=BUY&type=LIMIT" + "quantity=1&price=11&recvWindow=5000&timestamp=1644489390087"),
                         "d1a676610ceb39174c8039b3f548357994b2a34139a8addd33baadba65684592")

    def test_query_builder_matches_doc(self):
        p = {"symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT", "quantity": 1, "price": 11, "recvWindow": 5000, "timestamp": 1644489390087}
        self.assertEqual(mexc.spot_query(p), DOC_QUERY)

    def test_contract_param_string_sorted_and_encoded(self):
        self.assertEqual(mexc.contract_param_string({"page_size": 100, "page_num": 1, "symbol": "BTC_USDT", "x": None}),
                         "page_num=1&page_size=100&symbol=BTC_USDT")
        self.assertEqual(mexc.contract_param_string({"ids": "1,2"}), "ids=1%2C2")

    def test_contract_signature_rule(self):
        want = hmac.new(b"sec", b"mx0key1611038237237page_num=1", hashlib.sha256).hexdigest()
        self.assertEqual(mexc.contract_signature("sec", "mx0key", "1611038237237", "page_num=1"), want)


class Records(unittest.TestCase):
    def test_coin_network_suffix(self):
        self.assertEqual(mexc.coin_of({"coin": "USDT-PLASMA"}), "USDT")
        self.assertEqual(mexc.coin_of({"coin": "BTC-LIGHTNING"}), "BTC")
        self.assertEqual(mexc.coin_of({"coin": "eth"}), "ETH")


class Safety(unittest.TestCase):
    def test_write_endpoints_blocked(self):
        mc = mexc.MexcClient(sleep=0)
        mc._creds = ("k", "s")
        for p in ("/api/v3/order", "/api/v3/capital/withdraw", "/api/v3/capital/withdraw/apply", "/api/v3/batchOrders"):
            with self.assertRaises(ValueError):
                mc.spot(p)
        for p in ("/api/v1/private/order/submit", "/api/v1/private/position/change_margin"):
            with self.assertRaises(ValueError):
                mc.contract(p)
        with self.assertRaises(ValueError):
            mexc.public("/api/v3/order")

    def test_requests_are_signed_gets(self):
        seen = []

        class Resp:
            headers = {}
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"serverTime": 1700000000000}' if "time" in seen[-1].full_url else b'{"balances": []}'

        def fake_urlopen(req, timeout=0):
            seen.append(req)
            return Resp()

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            mc = mexc.MexcClient(sleep=0)
            mc._creds = ("mykey", "mysecret")
            mc.spot("/api/v3/account")
        req = seen[-1]
        self.assertEqual(req.get_method(), "GET")
        self.assertEqual(req.get_header("X-mexc-apikey"), "mykey")
        q = urllib.parse.urlsplit(req.full_url).query
        body, sig = q.rsplit("&signature=", 1)
        self.assertEqual(sig, mexc.spot_signature("mysecret", body))
        self.assertIn("timestamp=", body)
        self.assertNotIn("mysecret", req.full_url)


DAYMS = 86_400_000


def fake_router(now_ms):
    trades = [{"symbol": "ETHUSDT", "id": f"t{i}", "orderId": "o", "price": "2000", "qty": "0.01", "quoteQty": "20",
               "commission": "0.02", "commissionAsset": "USDT", "time": now_ms - 3 * DAYMS + i * 1000, "isBuyer": True}
              for i in range(3)]

    def route(url, headers, timeout=30):
        u = urllib.parse.urlsplit(url)
        q = dict(urllib.parse.parse_qsl(u.query))
        path = u.path
        if path == "/api/v3/time":
            return {"serverTime": now_ms}
        if path == "/api/v3/ticker/price":
            return [{"symbol": "ETHUSDT", "price": "2500"}, {"symbol": "BTCUSDT", "price": "80000"}, {"symbol": "MXUSDT", "price": "2"}]
        if path == "/api/v3/exchangeInfo":
            return {"symbols": [{"symbol": "ETHUSDT", "baseAsset": "ETH", "quoteAsset": "USDT"},
                                {"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT"}]}
        if path == "/api/v3/klines":
            return [[int(q["endTime"]) - DAYMS, "1", "1", "1", "2400", "1", 0, "1"]]
        if path == "/api/v3/account":
            return {"canTrade": False, "canWithdraw": False, "canDeposit": False, "accountType": "SPOT",
                    "balances": [{"asset": "ETH", "free": "0.4", "locked": "0"}, {"asset": "USDT", "free": "100", "locked": "0"},
                                 {"asset": "NOPRICE", "free": "5", "locked": "0"}], "permissions": ["SPOT"]}
        if path == "/api/v3/capital/deposit/hisrec":
            s, e = int(q["startTime"]), int(q["endTime"])
            rows = [{"coin": "USDT", "amount": "1000", "status": 5, "txId": "0xabc", "insertTime": now_ms - 10 * DAYMS},
                    {"coin": "ETH-ERC20", "amount": "0.1", "status": 5, "txId": "0xdef", "insertTime": now_ms - 5 * DAYMS}]
            return [r for r in rows if s <= r["insertTime"] <= e]
        if path == "/api/v3/capital/withdraw/history":
            s, e = int(q["startTime"]), int(q["endTime"])
            rows = [{"id": "w1", "coin": "USDT", "amount": "300", "status": 7, "transactionFee": "1", "applyTime": now_ms - 2 * DAYMS}]
            return [r for r in rows if s <= r["applyTime"] <= e]
        if path in ("/api/v3/capital/transfer/internal", "/api/v3/capital/convert"):
            return {"data": [], "totalPageNum": 0}
        if path == "/api/v3/capital/transfer":
            return [{"rows": [], "total": 0}]
        if path == "/api/v3/myTrades":
            s, e = int(q["startTime"]), int(q["endTime"])
            return [t for t in trades if q["symbol"] == t["symbol"] and s <= t["time"] <= e]
        if path == "/api/v1/private/account/assets":
            return {"success": True, "code": 0, "data": [{"currency": "USDT", "equity": 50, "unrealized": 0, "positionMargin": 0}]}
        if path == "/api/v1/private/position/open_positions":
            return {"success": True, "code": 0, "data": []}
        if path == "/api/v1/private/position/list/history_positions":
            return {"success": True, "code": 0, "data": [{"positionId": 1, "symbol": "BTC_USDT", "realised": -4.5, "updateTime": now_ms - DAYMS}]}
        if path.startswith("/api/v1/private/"):
            return {"success": True, "code": 0, "data": {"resultList": [], "totalPage": 0}}
        return {"code": 404, "msg": "unexpected " + path}
    return route


class FakeRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name)
        (self.project / "snapshots").mkdir()
        self.env = mock.patch.dict(os.environ, {"MEXC_API_KEY": "k", "MEXC_API_SECRET": "s"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_archive_incremental_and_analyze(self):
        import time
        now = int(time.time() * 1000)
        with mock.patch.object(mexc, "_http_get", fake_router(now)), mock.patch("time.sleep"):
            root = self.project / "archive" / "mexc"
            r1 = mexc.archive_run(mexc.MexcClient(sleep=0), root, progress=False)
            self.assertEqual(r1["errors"], {})
            self.assertEqual(r1["sources"]["mexc_deposits"]["added"], 2)
            self.assertEqual(r1["sources"]["mexc_trades"]["added"], 3)
            r2 = mexc.archive_run(mexc.MexcClient(sleep=0), root, progress=False)
            self.assertEqual(sum(v["added"] for v in r2["sources"].values()), 0)
            res = mexc.analyze(self.project, root)
        self.assertTrue(res["present"])
        # value: 0.4 ETH*2500 + 100 USDT + futures 50 = 1150; NOPRICE unvalued
        self.assertAlmostEqual(res["total"], 1150.0)
        self.assertEqual(res["unpriced"], ["NOPRICE"])
        # inflow: 1000 + 0.1 ETH @ 2400 (kline close) − 300 = 940
        self.assertAlmostEqual(res["net_inflow"], 940.0)
        self.assertAlmostEqual(res["pnl"], 210.0)
        self.assertEqual(res["trades"]["count"], 3)
        self.assertAlmostEqual(res["futures_history"]["realised"], -4.5)

    def test_absent_without_keys_or_data(self):
        with mock.patch.dict(os.environ, {"MEXC_API_KEY": "", "MEXC_API_SECRET": ""}), \
             mock.patch("assetsboard.client.keychain_has", return_value=False), \
             mock.patch("assetsboard.client._file_secret", return_value=""):
            self.assertFalse(mexc.available())
            res = mexc.analyze(self.project, self.project / "archive" / "mexc", offline=True)
        self.assertFalse(res["present"])
        self.assertIn("setup-keychain --exchange mexc", res["reason"])


if __name__ == "__main__":
    unittest.main()


class EarnAndCrossTest(unittest.TestCase):
    def test_earn_manual(self):
        import datetime as dt
        from assetsboard import mexc
        from assetsboard.client import OSLO
        now = dt.datetime(2026, 10, 4, 15, 0, tzinfo=OSLO)
        self.assertIsNone(mexc.earn_manual({}, now))
        self.assertIsNone(mexc.earn_manual({"mexc_earn_usdt": None}, now))
        e = mexc.earn_manual({"mexc_earn_usdt": 210.04, "mexc_earn_set_at": "2026-10-04T14:42:00+02:00"}, now)
        self.assertEqual(e["value"], 210.04); self.assertFalse(e["stale"]); self.assertEqual(e["label"], "手動填入")
        e = mexc.earn_manual({"mexc_earn_usdt": "5", "mexc_earn_set_at": "2026-08-01T10:00:00+02:00"}, now)
        self.assertTrue(e["stale"])
        self.assertTrue(mexc.earn_manual({"mexc_earn_usdt": 5}, now)["stale"])  # no date -> stale

    def test_cross_transfers(self):
        import json, tempfile
        from pathlib import Path
        from assetsboard import analysis
        from assetsboard.client import ms_to_oslo
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); (root / "2026-07").mkdir(); (root / "2026-05").mkdir()
            t = 1783588000000  # 2026-07-09
            w = [{"orderId": "1", "coin": "USDT", "size": "54", "cTime": str(t), "status": "success", "toAddress": "0xMEXC"},
                 {"orderId": "2", "coin": "USDT", "size": "100", "cTime": str(t - 60 * 86400000), "status": "success", "toAddress": "0xMEXC"},
                 {"orderId": "3", "coin": "USDT", "size": "77", "cTime": str(t - 60 * 86400000), "status": "success", "toAddress": "0xOTHER"}]
            dep = [{"orderId": "4", "coin": "USDT", "size": "30", "cTime": str(t + 9 * 86400000 + 60000), "status": "success", "fromAddress": "0xHOT"},
                   {"orderId": "5", "coin": "USDT", "size": "40", "cTime": str(t - 50 * 86400000), "status": "success", "fromAddress": "0xHOT"}]
            (root / "2026-07" / "withdrawals.jsonl").write_text("\n".join(json.dumps(x) for x in w))
            (root / "2026-05" / "deposits.jsonl").write_text("\n".join(json.dumps(x) for x in dep))
            mx = [{"kind": "deposit", "coin": "USDT", "qty": 53.999, "usd": 53.999, "time": ms_to_oslo(t + 120000)},
                  {"kind": "withdrawal", "coin": "USDT", "qty": -30, "usd": -30, "time": ms_to_oslo(t + 9 * 86400000)}]
            r = analysis.cross_transfers(root, mx)
            self.assertEqual(r["count"], 2)
            self.assertAlmostEqual(r["bitget_to_mexc_usdt"], 53.999); self.assertAlmostEqual(r["mexc_to_bitget_usdt"], 30)
            pre = r["before_mexc_window"]
            self.assertAlmostEqual(pre["to_mexc"], 100); self.assertAlmostEqual(pre["from_mexc"], 40)
            self.assertAlmostEqual(pre["net_to_mexc"], 60)


class BaselineTest(unittest.TestCase):
    def test_split_and_baseline_info(self):
        from assetsboard import mexc
        self.assertEqual(mexc.split_symbol("ETHUSDT"), ("ETH", "USDT"))
        self.assertEqual(mexc.split_symbol("MXBTC"), ("MX", "BTC"))
        self.assertIsNone(mexc.baseline_info({}))
        b = mexc.baseline_info({"mexc_baseline_at": "2026-10-04T15:24:00+02:00", "mexc_baseline_usdt": 231.29})
        self.assertEqual(b["value"], 231.29); self.assertEqual(b["ms"], 1791120240000)

    def test_since_baseline_fifo(self):
        from assetsboard import mexc

        class HP:
            def get(self, c, ms, tk):
                return {"ETH": 3000.0, "MX": 2.0}.get(c, 1.0)
        b = {"ms": 1000}
        snap0 = {"holdings": [{"coin": "ETH", "qty": 1.0, "price": 2000.0}, {"coin": "USDT", "qty": 50, "price": 1}]}
        arch = {
            "mexc_trades": [
                {"symbol": "ETHUSDT", "isBuyer": True, "qty": "1", "quoteQty": "2500", "time": 2000, "commission": "1", "commissionAsset": "USDT"},
                {"symbol": "ETHUSDT", "isBuyer": False, "qty": "1.5", "quoteQty": "4500", "time": 3000, "commission": "0", "commissionAsset": "USDT"},
                {"symbol": "ETHUSDT", "isBuyer": False, "qty": "9", "quoteQty": "1", "time": 500},  # before baseline: ignored
            ],
            "mexc_futures_positions": [{"realised": "5", "updateTime": 1500}, {"realised": "100", "updateTime": 10}],
            "mexc_futures_funding": [{"funding": "-0.5", "settleTime": 1200}, {"funding": "9", "settleTime": 1}],
        }
        r = mexc.since_baseline(arch, b, snap0, [], HP(), {})
        # FIFO: 1 ETH @2000 + 0.5 ETH @2500 = 3250 cost for 4500 → +1250, minus 1 USDT fee
        self.assertAlmostEqual(r["spot_realized"], 1249.0)
        self.assertEqual(r["spot_trades"], 2)
        self.assertAlmostEqual(r["futures_realized"], 5.0); self.assertAlmostEqual(r["funding"], -0.5)
        self.assertEqual(r["inventory_deficits"], {})
