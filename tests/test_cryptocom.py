"""Crypto.com App Agent API (read-only): signing, allowlist, client, snapshot, pagination, baseline/flows, key expiry."""
import datetime as dt
import io
import json
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock

from assetsboard import cryptocom as C


class Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_api(routes, log):
    """routes: path -> list of (status, json) consumed in order (last one repeats)."""
    def opener(req, timeout=0):
        u = urllib.parse.urlparse(req.full_url)
        log.append((req.get_method(), u.path, dict(urllib.parse.parse_qsl(u.query)), dict(req.header_items())))
        seq = routes[u.path]
        status, body = seq.pop(0) if len(seq) > 1 else seq[0]
        raw = json.dumps(body).encode()
        if status != 200:
            raise urllib.error.HTTPError(req.full_url, status, "err", {}, io.BytesIO(raw))
        return Resp(raw)
    return opener


def client(routes, log):
    return C.CdcClient(creds=("key-abc", "secret-xyz"), opener=fake_api(routes, log), sleep=lambda s: None)


class Signing(unittest.TestCase):
    def test_vectors_match_official_node_implementation(self):
        # generated with Node crypto exactly as in crypto-com-agent-trading/crypto-com-app/scripts/lib/api.ts
        self.assertEqual(C.sign("my-secret-123", "1771761038000", "GET", "/v1/crypto-account"),
                         "0mRj2umeZ/KgWJ3AhOOziF6qGVoInapb2XjvOAV4GAE=")
        self.assertEqual(C.sign("my-secret-123", "1771761038000", "post", "/v1/fiat/withdrawals", '{"order_id":"42"}'),
                         "P1TY54mc9Vt1Ym/l1Jg1U8B4tI1EjYsDWrKvP7vbeLU=")

    def test_query_string_is_not_signed(self):
        self.assertEqual(C.sign("s", "1", "GET", "/v1/currency_allocation?currency=BTC"), C.sign("s", "1", "GET", "/v1/currency_allocation"))


class Allowlist(unittest.TestCase):
    def test_allowed_reads(self):
        for p in C.ALLOWED_GET:
            C.check_request("GET", p)
        C.check_request("GET", "/v1/currency_allocation", {"currency": "BTC"})
        C.check_request("GET", "/v1/transactions", {"last_id": "crypto_earn/1", "count": "50"})

    def test_refuses_writes_and_unknown(self):
        for w in C.KNOWN_WRITES:
            for m in ("GET", "POST"):
                with self.assertRaises(C.WriteRefused):
                    C.check_request(m, w)
        for m in ("POST", "PUT", "DELETE"):
            with self.assertRaises(C.WriteRefused):
                C.check_request(m, "/v1/crypto-account")
        with self.assertRaises(C.WriteRefused):
            C.check_request("GET", "/v1/api-keys/current")
        with self.assertRaises(C.WriteRefused):
            C.check_request("GET", "/v1/crypto-account?x=1")
        with self.assertRaises(C.WriteRefused):
            C.check_request("GET", "/v1/currency_allocation", {"currency": "BTC", "amount": "1"})
        with self.assertRaises(C.WriteRefused):
            C.check_request("GET", "/v1/transactions", {"page_token": "x"})

    def test_refused_request_never_reaches_network(self):
        log = []
        c = client({}, log)
        with self.assertRaises(C.WriteRefused):
            c.get("/v1/fiat/withdrawals")
        self.assertEqual(log, [])


class Client(unittest.TestCase):
    def test_headers_and_signature(self):
        log = []
        c = client({"/v1/currency_allocation": [(200, {"ok": True, "crypto_earn": {"amount": "1"}})]}, log)
        with mock.patch("time.time", return_value=1771761038.0):
            c.get("/v1/currency_allocation", {"currency": "BTC"})
        m, path, q, h = log[0]
        self.assertEqual((m, path, q), ("GET", "/v1/currency_allocation", {"currency": "BTC"}))
        self.assertEqual(h["Cdc-api-key"], "key-abc")
        self.assertEqual(h["Cdc-api-timestamp"], "1771761038000")
        self.assertEqual(h["Cdc-api-signature"], C.sign("secret-xyz", "1771761038000", "GET", "/v1/currency_allocation"))

    def test_auth_rejection(self):
        c = client({"/v1/portfolio": [(401, {"ok": False, "error": "unauthorized", "error_message": "Unauthorized."})]}, [])
        with self.assertRaises(C.CdcError) as cm:
            c.get("/v1/portfolio")
        self.assertTrue(cm.exception.auth)
        self.assertNotIn("secret-xyz", str(cm.exception))
        c = client({"/v1/portfolio": [(200, {"ok": False, "error": "key_not_active"})]}, [])
        with self.assertRaises(C.CdcError) as cm:
            c.get("/v1/portfolio")
        self.assertTrue(cm.exception.auth)

    def test_429_retries_once(self):
        log = []
        c = client({"/v1/portfolio": [(429, {"ok": False, "error": "RATE_LIMITED"}), (200, {"ok": True, "products": []})]}, log)
        self.assertTrue(c.get("/v1/portfolio")["ok"])
        self.assertEqual(len(log), 2)

    def test_throttle_caps_calls_per_minute(self):
        t = [0.0]
        slept = []
        c = C.CdcClient(creds=("k", "s"), opener=fake_api({"/v1/portfolio": [(200, {"ok": True})]}, []),
                        sleep=lambda s: (slept.append(s), t.__setitem__(0, t[0] + s)), clock=lambda: t[0])
        for _ in range(C.MAX_PER_MIN + 1):
            c.get("/v1/portfolio")
        self.assertGreaterEqual(t[0], 60)          # the 91st call waits until the 60 s window has room

    def test_bad_local_secret_sends_nothing(self):
        log = []
        c = C.CdcClient(creds=("same", "same"), opener=fake_api({}, log), sleep=lambda s: None)
        with self.assertRaises(C.CdcError):
            c.get("/v1/portfolio")
        self.assertEqual(log, [])


def amt_(a, c):
    return {"amount": str(a), "currency": c}


def tx(i, nature, kind, when, a, c, usd, status="done", desc=""):
    return {"id": str(i), "nature": nature, "kind": kind, "created_at": when, "amount": amt_(a, c), "native_amount": amt_(usd, "USD"),
            "status": status, "description": desc}


# shapes as returned by the real API on 2026-10-04 (values invented)
ROUTES = lambda: {
    "/v1/crypto-account": [(200, {"ok": True, "account": {"native_currency": "USD", "wallets": [
        {"currency": "BTC", "balance": amt_("0.001", "BTC"), "available": amt_("0.001", "BTC"), "native_balance": amt_("60", "USD")},
        {"currency": "CRO", "balance": amt_("0", "CRO"), "available": amt_("0", "CRO"), "native_balance": amt_("0", "USD")},
        {"currency": "DUST", "balance": amt_("0.000000001", "DUST"), "available": amt_("0.000000001", "DUST"), "native_balance": amt_("0", "USD")},
        {"currency": "ETH", "balance": amt_("0", "ETH"), "available": amt_("0", "ETH"), "native_balance": amt_("0", "USD")}]}})],
    "/v1/fiat-account": [(200, {"ok": True, "account": {"balances": [{"currency": "EUR", "amount": amt_("10", "EUR")}]}})],
    "/v1/portfolio": [(200, {"ok": True, "price_native": amt_("1300", "USD"),
                             "coins": [{"id": "BTC", "amount": amt_("0.011", "BTC"), "price_native": amt_("660", "USD")},
                                       {"id": "CRO", "amount": amt_("6400", "CRO"), "price_native": amt_("640", "USD")}],
                             "products": [{"name": "Earn", "price_native": amt_("660", "USD")},
                                          {"name": "Crypto Staking", "price_native": amt_("600", "USD")},
                                          {"name": "Defi Staking", "price_native": amt_("40", "USD")},
                                          {"name": "Crypto Wallet", "price_native": amt_("0", "USD")}]})],
    "/v1/currency_allocation": [(200, {"ok": True, "crypto_earn": amt_("0.01", "BTC"), "staking": amt_("0", "BTC")}),
                                (200, {"ok": True, "staking": amt_("6000", "CRO"), "defi_staking": amt_("400", "CRO")})],
    "/v1/transactions": [
        (200, {"ok": True, "transactions": [
            tx(9, "crypto_earn", "crypto_earn_interest_paid", "2026-10-03T00:00:00Z", "0.0001", "BTC", "6"),
            tx(8, "withdraw", "crypto_withdrawal", "2026-09-20T00:00:00Z", "-0.002", "BTC", "-120")],
            "meta": {"pagination": {"total": 1, "next": {"last_id": "withdraw/8", "count": 2, "pagination_key": "id"}}}}),
        (200, {"ok": True, "transactions": [
            tx(5, "purchase", "trading.crypto_purchase.apple_pay", "2025-08-08T10:00:00Z", "1000", "USDC", "1000"),
            tx(4, "purchase", "crypto_purchase", "2025-08-08T09:00:00Z", "500", "USDC", "500", status="failed")],
            "meta": {"pagination": {"total": 1}}})],
}


class Snapshot(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.p = Path(self.td.name)
        self.root = self.p / "archive" / "cryptocom"
        self.px = {"BTC": 60000.0, "CRO": 0.1, "USDT": 1.0, "USD": 1.0, "EUR": 1.1}
        self.patches = [mock.patch.object(C, "tickers", return_value=self.px)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.td.cleanup()

    def test_snapshot_values_and_allocations(self):
        log = []
        s = C.build_snapshot(client(ROUTES(), log), dt.datetime(2026, 10, 4, 12, tzinfo=C.OSLO))
        self.assertEqual([h["coin"] for h in s["holdings"]], ["BTC", "CRO"])          # dust / zero wallets skipped
        self.assertEqual(s["dust_wallets"], 1)
        btc, cro = s["holdings"]
        self.assertAlmostEqual(btc["qty_total"], 0.011)                              # portfolio qty = wallet 0.001 + Earn 0.01
        self.assertAlmostEqual(btc["qty_check_diff"], 0.0)
        self.assertAlmostEqual(cro["qty_check_diff"], 0.0)
        self.assertAlmostEqual(btc["value_usdt"], 660.0)                             # App value (USD) x USDT/USD
        self.assertEqual(s["native_currency"], "USD")
        self.assertAlmostEqual(s["total_usdt"], 1300 + 10 * 1.1)                     # portfolio + fiat cash
        self.assertEqual(s["total_source"], "portfolio")
        self.assertEqual({p["kind"] for p in s["products"]}, {"Earn", "Staking", "DeFi 質押"})
        self.assertTrue(all(m == "GET" for m, *_ in log))
        self.assertEqual({p for _, p, *_ in log} - set(C.ALLOWED_GET), set())

    def test_archive_paginates_with_last_id_and_dedupes(self):
        log = []
        r = C.archive_run(client(ROUTES(), log), self.root)
        tq = [q for _, p, q, _ in log if p == "/v1/transactions"]
        self.assertEqual(tq, [{"count": "50"}, {"count": "50", "last_id": "withdraw/8"}])
        self.assertEqual(r["sources"]["cryptocom_transactions"]["added"], 4)
        self.assertTrue(r["tx_info"]["backfill_complete"])
        self.assertTrue(r["baseline_created"])
        r2 = C.archive_run(client(ROUTES(), []), self.root)
        self.assertEqual(r2["sources"]["cryptocom_transactions"]["added"], 0)
        self.assertEqual(r2["tx_info"]["stopped"], "整頁都已封存")
        self.assertEqual(r2["tx_info"]["pages"], 1)
        self.assertEqual(len(C.load_transactions(self.root)), 4)

    def test_interrupted_history_resumes_from_cursor(self):
        with mock.patch.object(C, "MAX_TX_PAGES", 1):
            r = C.archive_run(client(ROUTES(), []), self.root)
        self.assertFalse(r["tx_info"]["backfill_complete"])
        self.assertEqual(r["tx_info"]["backfill_cursor"], "withdraw/8")
        routes = ROUTES()
        # (the fake API ignores parameters: 1st call = newest page, 2nd = the page after the saved cursor)
        log = []
        r = C.archive_run(client(routes, log), self.root)
        self.assertTrue(r["tx_info"]["backfill_complete"])
        self.assertEqual(len(C.load_transactions(self.root)), 4)
        self.assertEqual([q.get("last_id") for _, p, q, _ in log if p == "/v1/transactions"], [None, "withdraw/8"])

    def test_classification_uses_explicit_kinds(self):
        cases = {
            ("crypto_purchase", "Buy USDC"): "in", ("trading.crypto_purchase.apple_pay", "Buy USDC"): "in",
            ("viban_purchase", "Bought USDC"): "in", ("crypto_deposit", "USDC (ERC20) Deposit"): "in",
            ("crypto_withdrawal", "Withdraw BTC (BTC)"): "out", ("crypto_viban_exchange", "Sold USDC"): "out",
            ("rewards_platform_deposit_credited", "Mystery Box Rewards"): "income",
            ("supercharger_deposit", "Supercharger Deposit (via app)"): "internal",
            ("finance.airdrop_arena.deposit.crypto_wallet", "Airdrop Arena deposit payment"): "internal",
            ("crypto_earn_program_created", "Crypto Earn Allocation"): "internal",
            ("crypto_earn_interest_paid", "Crypto Earn"): "income", ("recurring_buy_order", "USDC > BTC"): "internal",
            ("crypto_exchange", "USDC > SOL"): "internal", ("something_new", ""): "unknown",
        }
        for (k, d), want in cases.items():
            self.assertEqual(C.tx_class({"kind": k, "description": d, "status": "done"})[0], want, k)
        self.assertEqual(C.tx_class({"kind": "crypto_purchase", "status": "failed"})[0], "ignored")

    def test_full_history_pnl(self):
        C.archive_run(client(ROUTES(), []), self.root)
        a = C.analyze(self.p, self.root)
        self.assertEqual(a["method"], "full")
        self.assertAlmostEqual(a["deposits"], 1000.0)                 # failed purchase ignored
        self.assertAlmostEqual(a["withdrawals"], 120.0)
        self.assertAlmostEqual(a["net_inflow"], 880.0)
        self.assertAlmostEqual(a["pnl"], 1311.0 - 880.0)
        self.assertAlmostEqual(a["income_total"], 6.0)
        self.assertAlmostEqual(a["earn"], 1300.0)
        self.assertEqual(a["unknown_kinds"], {})

    def test_baseline_mode_when_history_incomplete(self):
        with mock.patch.object(C, "MAX_TX_PAGES", 1):
            C.archive_run(client(ROUTES(), []), self.root)
        b = C.load_baseline(self.root)
        b["at"] = "2026-09-01T00:00:00+02:00"
        (self.root / "baseline.json").write_text(json.dumps(b))
        a = C.analyze(self.p, self.root)
        self.assertEqual(a["method"], "baseline")
        self.assertEqual([f["raw_type"] for f in a["flows"]], ["crypto_withdrawal"])   # interest is not a flow
        self.assertAlmostEqual(a["net_inflow"], b["value_usdt"] - 120.0)

    def test_auth_failure_recorded_and_warned(self):
        routes = {"/v1/crypto-account": [(401, {"ok": False, "error": "unauthorized"})]}
        r = C.archive_run(client(routes, []), self.root)
        self.assertIn("auth", r["errors"])
        self.assertIn("被拒絕", r["warning"])
        self.assertNotIn("secret-xyz", json.dumps(C.load_state(self.root)))

    def test_expiry_warning(self):
        C.archive_run(client(ROUTES(), []), self.root)
        st = C.load_state(self.root)
        st["key"]["first_ok"] = (C.now_oslo() - dt.timedelta(days=25)).isoformat(timespec="seconds")
        C.save_state(self.root, st)
        ks = C.key_status(self.root)
        self.assertTrue(ks["expires_assumed"])
        self.assertIn("到期", ks["warning"])
        C.set_key_expiry(self.root, (C.now_oslo() + dt.timedelta(days=60)).date().isoformat())
        self.assertIsNone(C.key_status(self.root)["warning"])


if __name__ == "__main__":
    unittest.main()
