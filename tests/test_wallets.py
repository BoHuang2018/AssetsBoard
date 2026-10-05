"""On-chain wallets: config, Arbitrum ERC-20 decode, Bittensor parse, snapshot/baseline, allowlist of public reads only."""
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from assetsboard import wallets as W


class Config(unittest.TestCase):
    def test_writes_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = W.load_config(root)
            self.assertEqual(len(ws), 2)
            self.assertTrue((root / "wallets.json").exists())
            self.assertEqual(ws[0]["chain"], "arbitrum")
            self.assertEqual(ws[1]["chain"], "bittensor")
            self.assertTrue(ws[0]["address"].startswith("0x"))
            self.assertEqual(len(ws[0]["address"]), 42)
            self.assertIn("DEADBEEF", ws[0]["address"].upper())
            # no private-key-like fields
            blob = (root / "wallets.json").read_text()
            # config must not look like it embeds key material fields
            self.assertNotRegex(blob.lower(), r'"private(_key)?"\s*:')
            self.assertNotRegex(blob.lower(), r'"mnemonic"\s*:')
            self.assertNotRegex(blob.lower(), r'"secret"\s*:')


class Arbitrum(unittest.TestCase):
    def test_fetches_usdc_via_rpc(self):
        calls = []
        def fake_rpc(method, params):
            calls.append((method, params[0] if method == "eth_call" else None))
            if method == "eth_getBalance":
                return "0x0"
            # USDC native contract → 120.1367 * 1e6
            to = params[0]["to"].lower()
            if to == "0xaf88d065e77c8cc2239327c5edb3a432268e5831":
                return hex(120136700)
            return "0x0"
        with mock.patch.object(W, "_arb_rpc", side_effect=fake_rpc):
            d = W.fetch_arbitrum("0x00000000000000000000000000000000DEADBEEF")
        self.assertEqual(d["tokens"][0]["symbol"], "USDC")
        self.assertAlmostEqual(d["tokens"][0]["amount"], 120.1367)
        self.assertTrue(all(m in ("eth_getBalance", "eth_call") for m, _ in calls))


class Bittensor(unittest.TestCase):
    def test_parses_taoscan(self):
        payload = {"address": "5D…", "blockNumber": 1,
                   "balance": {"freeTao": "0.118168306", "reservedTao": "0", "frozenTao": "0", "nonce": 7},
                   "stakes": [], "totals": {}}
        with mock.patch.object(W, "_http_json", return_value=payload):
            d = W.fetch_bittensor("5FakeBittensorAddressDoNotUseReplaceMeXXXXXXX")
        self.assertAlmostEqual(d["tokens"][0]["amount"], 0.118168306)
        self.assertEqual(d["tokens"][0]["symbol"], "TAO")


class Snapshot(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name) / "archive" / "wallets"
        W.load_config(self.root)

    def tearDown(self):
        self.td.cleanup()

    def test_archive_sets_baseline_and_values(self):
        arb = {"chain": "arbitrum", "address": "0x3f…", "tokens": [{"symbol": "USDC", "amount": 100.0}], "source": "t"}
        tao = {"chain": "bittensor", "address": "5D…", "tokens": [{"symbol": "TAO", "amount": 1.0, "free": 1.0, "reserved": 0, "frozen": 0, "staked": 0}], "source": "t"}
        with mock.patch.object(W, "fetch_arbitrum", return_value=arb), \
             mock.patch.object(W, "fetch_bittensor", return_value=tao), \
             mock.patch.object(W, "prices", return_value={"USDC": 1.0, "TAO": 400.0}):
            r = W.archive_run(self.root)
        self.assertTrue(r["baseline_created"])
        self.assertAlmostEqual(r["total_usdt"], 500.0)
        a = W.analyze(self.root.parent.parent, self.root)
        self.assertTrue(a["present"])
        self.assertAlmostEqual(a["total"], 500.0)
        self.assertAlmostEqual(a["pnl"], 0.0)
        self.assertEqual(a["net_inflow"], 500.0)


if __name__ == "__main__":
    unittest.main()
