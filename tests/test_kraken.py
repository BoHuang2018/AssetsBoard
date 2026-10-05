"""Offline tests for assetsboard.kraken (no network, no real keys). Run: python3 -m unittest discover -s tests"""
import base64
import hashlib
import hmac
import io
import json
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from assetsboard import analysis, kraken

# Kraken REST docs, "Authentication" example
DOC_SECRET = "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg=="
DOC_NONCE = "1616492376594"
DOC_POST = "nonce=1616492376594&ordertype=limit&pair=XBTUSD&price=37500&type=buy&volume=1.25"
DOC_SIG = "4/dpxb3iT4tp/ZCVEwSnEsLxx0bqyhLpdfOpc6fn7OR8+UClSV5n9E6aSS8MPtnRfp32bAb0nmbRn6H8ndwLUQ=="


class Signing(unittest.TestCase):
    def test_doc_example(self):
        # pure signing function only; the client itself refuses AddOrder (see Allowlist)
        self.assertEqual(kraken.sign("/0/private/AddOrder", DOC_POST, DOC_NONCE, DOC_SECRET), DOC_SIG)

    def test_doc_example_int_nonce_and_form_encoder(self):
        post = kraken.encode_form({"nonce": 1616492376594, "ordertype": "limit", "pair": "XBTUSD", "price": 37500, "type": "buy", "volume": 1.25})
        self.assertEqual(post, DOC_POST)
        self.assertEqual(kraken.sign("/0/private/AddOrder", post, 1616492376594, DOC_SECRET), DOC_SIG)


class _Resp(io.BytesIO):
    headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _client():
    kc = kraken.KrakenClient()
    kc._creds = ("TESTKEY", DOC_SECRET)
    kc._wait = lambda cost: None
    return kc


class Allowlist(unittest.TestCase):
    def test_write_methods_refused_before_sending(self):
        kc = _client()
        with mock.patch("urllib.request.urlopen") as uo:
            for m in ("AddOrder", "Withdraw", "WalletTransfer", "Earn/Allocate", "Earn/Deallocate", "CancelOrder",
                      "EditOrder", "AddOrderBatch", "Stake", "DepositAddresses", "SomethingNew", "/0/private/Withdraw", "Balance/../AddOrder"):
                with self.assertRaises(kraken.WriteRefused, msg=m):
                    kc.private(m, {"x": 1})
            uo.assert_not_called()

    def test_read_methods_allowed(self):
        for m in ("Balance", "BalanceEx", "TradeBalance", "Ledgers", "QueryLedgers", "TradesHistory", "ClosedOrders", "OpenOrders", "Earn/Allocations"):
            self.assertEqual(kraken.check_method(m), f"/0/private/{m}")

    def test_request_is_signed_post(self):
        kc = _client()
        seen = {}

        def fake(req, timeout=30):
            seen["req"] = req
            return _Resp(json.dumps({"error": [], "result": {"XXBT": "1.0"}}).encode())
        with mock.patch("urllib.request.urlopen", side_effect=fake):
            r = kc.private("Balance", name="b")
        self.assertEqual(r["result"], {"XXBT": "1.0"})
        req = seen["req"]
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.full_url, "https://api.kraken.com/0/private/Balance")
        body = req.data.decode()
        nonce = urllib.parse.parse_qs(body)["nonce"][0]
        msg = b"/0/private/Balance" + hashlib.sha256((nonce + body).encode()).digest()
        exp = base64.b64encode(hmac.new(base64.b64decode(DOC_SECRET), msg, hashlib.sha512).digest()).decode()
        self.assertEqual(req.get_header("Api-sign"), exp)
        self.assertEqual(req.get_header("Api-key"), "TESTKEY")
        self.assertNotIn(DOC_SECRET, json.dumps(kc.calls))   # secrets never recorded

    def test_earn_allocations_json_body(self):
        kc = _client()
        seen = {}

        def fake(req, timeout=30):
            seen["req"] = req
            return _Resp(b'{"error":[],"result":{"items":[]}}')
        with mock.patch("urllib.request.urlopen", side_effect=fake):
            kc.private("Earn/Allocations", {"hide_zero_allocations": True}, as_json=True)
        req = seen["req"]
        self.assertEqual(req.get_header("Content-type"), "application/json")
        body = json.loads(req.data)
        self.assertTrue(body["hide_zero_allocations"])
        msg = b"/0/private/Earn/Allocations" + hashlib.sha256((str(body["nonce"]) + req.data.decode()).encode()).digest()
        self.assertEqual(req.get_header("Api-sign"), base64.b64encode(hmac.new(base64.b64decode(DOC_SECRET), msg, hashlib.sha512).digest()).decode())

    def test_nonce_monotonic(self):
        kc = _client()
        ns = [kc._nonce() for _ in range(50)]
        self.assertEqual(ns, sorted(set(ns)))

    def test_perms_never_writes(self):
        kc = _client()
        sent = []

        def fake(req, timeout=30):
            sent.append(req.full_url)
            return _Resp(b'{"error":[],"result":{}}')
        with mock.patch("urllib.request.urlopen", side_effect=fake):
            p = kraken.perms_report(kc)
        self.assertTrue(all(u.split("/0/private/")[1] in kraken.READ_METHODS for u in sent))
        self.assertEqual(set(p["write_refused_locally"]), {"AddOrder", "Withdraw", "WalletTransfer", "Earn/Allocate"})


class SecretCheck(unittest.TestCase):
    def test_problems(self):
        self.assertIsNone(kraken.secret_problem("K" * 56, DOC_SECRET))
        self.assertIn("同一個值", kraken.secret_problem("A" * 56, "A" * 56))
        self.assertIsNotNone(kraken.secret_problem("K" * 56, "A" * 56))
        self.assertIsNotNone(kraken.secret_problem("K", ""))

    def test_bad_secret_sends_nothing(self):
        kc = kraken.KrakenClient()
        kc._creds = ("A" * 56, "A" * 56)
        with mock.patch("urllib.request.urlopen") as uo:
            p = kraken.perms_report(kc)
            uo.assert_not_called()
        self.assertIn("同一個值", p["local_problem"])
        self.assertFalse(p["ok"])


class Assets(unittest.TestCase):
    def test_norm(self):
        cases = {"XXBT": ("BTC", "spot"), "XBT.M": ("BTC", "opt-in rewards"), "ZEUR": ("EUR", "spot"), "ZUSD": ("USD", "spot"),
                 "XETH": ("ETH", "spot"), "ETH2.S": ("ETH", "staked"), "ETH2": ("ETH", "spot"), "DOT.S": ("DOT", "staked"),
                 "USDC.F": ("USDC", "auto earn (Kraken Rewards)"), "SOL.B": ("SOL", "earn (yield-bearing)"), "XXDG": ("DOGE", "spot"),
                 "EUR.HOLD": ("EUR", "hold"), "USDT": ("USDT", "spot"), "KSM.P": ("KSM", "parachain"),
                 "NVDAx.T": ("NVDAx", "xStock"), "GOOGLx": ("GOOGLx", "spot"), "SOL03.S": ("SOL", "staked"),
                 "1INCH": ("1INCH", "spot"), "API3": ("API3", "spot")}
        for a, exp in cases.items():
            self.assertEqual(kraken.norm(a), exp, a)

    def test_market_prices(self):
        pairs = {"XXBTZUSD": {"base": "XXBT", "quote": "ZUSD", "altname": "XBTUSD"},
                 "USDTZUSD": {"base": "USDT", "quote": "ZUSD", "altname": "USDTUSD"},
                 "USDTEUR": {"base": "USDT", "quote": "ZEUR", "altname": "USDTEUR"},
                 "DOTEUR": {"base": "DOT", "quote": "ZEUR", "altname": "DOTEUR"},
                 "SOLUSDT": {"base": "SOL", "quote": "USDT", "altname": "SOLUSDT"}}
        tk = {"XXBTZUSD": {"c": ["100000", "1"]}, "USDTZUSD": {"c": ["1.0", "1"]}, "USDTEUR": {"c": ["0.9", "1"]},
              "DOTEUR": {"c": ["4.5", "1"]}, "SOLUSDT": {"c": ["150", "1"]}}
        m = kraken.Market(pairs, tk)
        self.assertAlmostEqual(m.price("SOL"), 150)
        self.assertAlmostEqual(m.price("XXBT"), 100000)
        self.assertAlmostEqual(m.price("ZEUR"), 1 / 0.9)
        self.assertAlmostEqual(m.price("DOT.S"), 4.5 / 0.9)
        self.assertEqual(m.price("USDT"), 1.0)
        self.assertIsNone(m.price("NOPE"))


class RealData(unittest.TestCase):
    """Shapes seen on a real account (2026-10): xStocks, SPV twins, Earn not in Balance, hybrid earn moves."""

    def test_spv_twin_and_tokenized(self):
        pairs = {"NVDAxUSD": {"base": "NVDAx", "quote": "ZUSD", "altname": "NVDAxUSD", "aclass_base": "tokenized_asset"},
                 "NVDASPVUSD": {"base": "NVDAx", "quote": "ZUSD", "altname": "NVDAxUSD", "aclass_base": "tokenized_asset"},
                 "EWYxUSD": {"base": "EWYx", "quote": "ZUSD", "altname": "EWYxUSD", "aclass_base": "tokenized_asset"}}
        tk = {"NVDAxUSD": {"c": ["234.8", "1"]}, "NVDASPVUSD": {"c": ["235.2", "1"]}, "EWYxUSD": {"c": ["0.00000", "0"]}}
        m = kraken.Market(pairs, tk)
        self.assertAlmostEqual(m.price("NVDAx.T"), 234.8)
        self.assertIsNone(m.price("EWYx"))           # inactive: zero last price is not a price
        self.assertEqual(m.aclass[("NVDAx", "USD")], "tokenized_asset")

    def test_earn_allocation_not_in_balance(self):
        m = kraken.Market({"NVDAxUSD": {"base": "NVDAx", "quote": "ZUSD", "altname": "NVDAxUSD"},
                           "XXBTZUSD": {"base": "XXBT", "quote": "ZUSD", "altname": "XBTUSD"}},
                          {"NVDAxUSD": {"c": ["200", "1"]}, "XXBTZUSD": {"c": ["100000", "1"]}})

        class KC:
            def private(self, method, params=None, name=None, as_json=False):
                return {"error": [], "result": {
                    "BalanceEx": {"XXBT": {"balance": "0.01"}, "NVDAx.T": {"balance": "0"}},
                    "Earn/Allocations": {"converted_asset": "USD", "total_allocated": "1008", "items": [
                        {"native_asset": "BTC", "amount_allocated": {"total": {"native": "0.01", "converted": "1000"}}},
                        {"native_asset": "NVDAx", "amount_allocated": {"total": {"native": "0.04", "converted": "8"}}}]},
                    "OpenOrders": {"open": {}}, "TradeBalance": {"eb": "1000.0"}}[method]}

            def errors(self):
                return {}
        d = kraken.build_snapshot(KC(), kraken.now_oslo(), m)
        extra = [h for h in d["holdings"] if h["kind"].startswith("earn allocation")]
        self.assertEqual([(h["coin"], round(h["qty"], 8)) for h in extra], [("NVDAx", 0.04)])   # BTC already in Balance
        self.assertAlmostEqual(d["value"]["total"], 1000 + 8)
        self.assertEqual(d["trade_balance_usd"]["eb"], 1000.0)

    def test_hybrid_earn_internal(self):
        rows = [L(1, 1.7e9, "hybridearnwithdrawal", "USDC", -116.6), L(2, 1.7e9 + 9, "hybridearndeposit", "USDC", 116.7),
                L(3, 1.7e9 + 99, "hybridearnwithdrawal", "NVDAx", -0.04), L(4, 1.7e9 + 999, "reward", "XXDG", 5, sub="welcomebonus")]
        self.assertEqual([r["_tag"] for r in kraken.classify_ledger(rows)], ["staking_move", "staking_move", "staking_move", "reward"])


class TradingCost(unittest.TestCase):
    def test_instant_trades_pairing(self):
        rows = [dict(L(1, 1.78e9, "receive", "NVDAx", "0.004508"), refid="T1"), dict(L(2, 1.78e9, "spend", "ZUSD", "-1.0000"), refid="T1"),
                dict(L(3, 1.78e9 + 9, "spend", "SOXLx", "-0.017694"), refid="T2"), dict(L(4, 1.78e9 + 9, "receive", "ZUSD", "4.77", fee="0.01"), refid="T2"),
                dict(L(5, 1.78e9 + 99, "spend", "PEPE", "-5", sub="dustsweeping"), refid="T3"),
                dict(L(6, 1.78e9 + 99, "receive", "ZUSD", "0.001", sub="dustsweeping"), refid="T3")]
        t = kraken.instant_trades(rows)
        self.assertEqual([(x["side"], x["asset"], x["quote"]) for x in t], [("buy", "NVDAx", "USD"), ("sell", "SOXLx", "USD")])
        self.assertAlmostEqual(t[0]["implied_price"], 1 / 0.004508)
        self.assertAlmostEqual(t[1]["implied_price"], 4.77 / 0.017694)
        self.assertAlmostEqual(t[1]["fee_quote"], 0.01)

    def test_underlying_ref(self):
        bars = [[1000, 100.0, 102.0, 103.0, 99.0], [4600, 102.0, 101.0, 102.5, 100.5]]
        r = kraken.underlying_ref(bars, 1000 + 1800)
        self.assertTrue(r["ok"]); self.assertAlmostEqual(r["price"], 101.0)       # halfway open→close
        self.assertFalse(kraken.underlying_ref(bars, 4600 + 7200)["ok"])         # market closed after last bar
        self.assertFalse(kraken.underlying_ref(bars, 500)["ok"])


class Paging(unittest.TestCase):
    def test_ofs_paging(self):
        rows = {f"L{i:03d}": {"refid": f"R{i}", "time": 1700000000 + i, "type": "deposit", "asset": "ZEUR", "amount": "1", "fee": "0"}
                for i in range(120)}
        ids = sorted(rows, reverse=True)
        calls = []

        class KC:
            def private(self, method, params):
                calls.append(params)
                o = params["ofs"]
                return {"error": [], "result": {"ledger": {k: rows[k] for k in ids[o:o + 50]}, "count": len(rows)}}
        out, errs, n = kraken.fetch_paged(KC(), kraken.ARCH[0], None, 1800000000.0)
        self.assertEqual((len(out), errs, n), (120, [], 3))
        self.assertEqual([c["ofs"] for c in calls], [0, 50, 100])
        self.assertNotIn("start", calls[0])
        self.assertEqual(out[0]["_ms"], (1700000000 + 119) * 1000)

    def test_error_stops(self):
        class KC:
            def private(self, method, params):
                return {"error": ["EGeneral:Permission denied"]}
        out, errs, n = kraken.fetch_paged(KC(), kraken.ARCH[0], 1.0, 2.0)
        self.assertEqual((out, n), ([], 1))
        self.assertIn("Permission denied", errs[0]["msg"])


def L(i, t, typ, asset, amount, sub="", fee="0"):
    return {"_id": f"L{i}", "_ms": int(t * 1000), "time": t, "refid": f"R{i}", "type": typ, "subtype": sub, "asset": asset,
            "amount": str(amount), "fee": fee, "aclass": "currency", "balance": "0"}


class Ledger(unittest.TestCase):
    def rows(self):
        d = 86400
        return [L(1, 1.70e9, "deposit", "ZEUR", 1000), L(2, 1.70e9 + d, "trade", "ZEUR", -900),
                L(3, 1.70e9 + d, "trade", "DOT", 200, fee="0.1"),
                L(4, 1.70e9 + 2 * d, "withdrawal", "DOT", -100),      # legacy staking: base withdrawal …
                L(5, 1.70e9 + 2 * d + 60, "deposit", "DOT.S", 100),   # … mirrored by the .S deposit
                L(6, 1.70e9 + 3 * d, "staking", "DOT.S", 2),
                L(7, 1.70e9 + 4 * d, "transfer", "DOT", -50, sub="spottostaking"),
                L(8, 1.70e9 + 4 * d, "transfer", "DOT.S", 50, sub="stakingfromspot"),
                L(9, 1.70e9 + 5 * d, "transfer", "USDT", -30, sub="spottofutures"),
                L(10, 1.70e9 + 6 * d, "withdrawal", "USDT", -10, fee="1"),
                L(11, 1.70e9 + 7 * d, "earn", "DOT.S", 1, sub="reward")]

    def test_classify(self):
        tags = {r["_id"]: r["_tag"] for r in kraken.classify_ledger(self.rows())}
        self.assertEqual(tags, {"L1": "external_deposit", "L2": "trade", "L3": "trade", "L4": "staking_move", "L5": "staking_move",
                                "L6": "reward", "L7": "staking_move", "L8": "staking_move", "L9": "futures_out",
                                "L10": "external_withdrawal", "L11": "reward"})

    def test_analyze_offline(self):
        with tempfile.TemporaryDirectory() as td:
            proj = Path(td)
            root = proj / "archive" / "kraken"
            (root / "2023-11").mkdir(parents=True)
            with (root / "2023-11" / "kraken_ledgers.jsonl").open("w") as fh:
                for r in self.rows():
                    fh.write(json.dumps(r) + "\n")
            (root / "state.json").write_text(json.dumps({"sources": {"kraken_ledgers": {"full_history": True}}}))
            snap = {"captured_oslo": "2026-10-04T16:00:00+02:00", "balance_ok": True,
                    "holdings": [{"asset": "DOT", "coin": "DOT", "kind": "spot", "qty": 50, "price": 5.0},
                                 {"asset": "DOT.S", "coin": "DOT", "kind": "staked", "qty": 153, "price": 5.0},
                                 {"asset": "ZEUR", "coin": "EUR", "kind": "spot", "qty": 100, "price": 1.1}],
                    "earn_allocations": {"ok": False}, "errors": {}}
            kraken.write_snapshot(root / "2026-10" / "snapshots", snap, kraken.now_oslo())
            with mock.patch.object(kraken, "available", return_value=False):
                a = kraken.analyze(proj, offline=True)
        self.assertTrue(a["present"])
        self.assertAlmostEqual(a["spot"], 250 + 110)
        self.assertAlmostEqual(a["earn"], 765)
        self.assertAlmostEqual(a["futures_at_cost"], 30)
        self.assertAlmostEqual(a["deposits"], 1100)      # EUR @ 1.1 (offline fallback price)
        self.assertAlmostEqual(a["withdrawals"], 10)
        self.assertAlmostEqual(a["net_inflow"], 1090)
        self.assertAlmostEqual(a["pnl"], 360 + 765 + 30 - 1090)
        self.assertAlmostEqual(a["rewards_usdt"], 15)
        self.assertTrue(a["history_complete"])
        self.assertAlmostEqual(a["fiat_deposits"], 1100)


class Cross(unittest.TestCase):
    def test_match(self):
        h = 3_600_000
        legs = [{"exchange": "kraken", "kind": "withdrawal", "coin": "USDT", "qty": 500, "ms": 0, "usd": 500, "counted": True},
                {"exchange": "bitget", "kind": "deposit", "coin": "USDT", "qty": 498.5, "ms": 1 * h, "usd": 498.5, "counted": True},
                {"exchange": "mexc", "kind": "deposit", "coin": "USDT", "qty": 499, "ms": 13 * h, "usd": 499, "counted": True},   # too late
                {"exchange": "bitget", "kind": "withdrawal", "coin": "SOL", "qty": 2, "ms": 20 * h, "usd": 300, "counted": True},
                {"exchange": "kraken", "kind": "deposit", "coin": "SOL", "qty": 1.99, "ms": 20.5 * h, "usd": 298.5, "counted": True},
                {"exchange": "kraken", "kind": "deposit", "coin": "SOL", "qty": 2.5, "ms": 20.6 * h, "usd": 375, "counted": True}]  # more than sent
        p = analysis.match_cross(legs)
        self.assertEqual([(q["direction"], q["received"]) for q in p], [("kraken→bitget", 498.5), ("bitget→kraken", 1.99)])
        self.assertTrue(all(q["both_counted"] for q in p))


if __name__ == "__main__":
    unittest.main()
