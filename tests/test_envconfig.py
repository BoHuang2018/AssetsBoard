import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from assetsboard import client, envconfig, etherfi_cash, wallets

ROOT = Path(__file__).resolve().parent.parent
FAKE_EVM = "0x000000000000000000000000000000000000F00D"
FAKE_TAO = "5FakeEnvTestAddressXXXXXXXXXXXXXXXXXXXXXXXXXXX"


class EnvFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = Path(self.tmp.name) / ".env"
        self.p = mock.patch.dict(os.environ, {"ASSETSBOARD_ENV_FILE": str(self.env)})
        self.p.start()
        envconfig._cache.clear()

    def tearDown(self):
        self.p.stop()
        envconfig._cache.clear()
        self.tmp.cleanup()

    def write(self, text):
        self.env.write_text(text)

    def test_parse(self):
        d = envconfig.parse('# c\nA=1\nexport B = "two words"\nC=x # comment\nD=\'q#\'\nbad line\n1X=no\n')
        self.assertEqual(d, {"A": "1", "B": "two words", "C": "x", "D": "q#"})

    def test_precedence_and_secrets_refused(self):
        self.write("ETHERFI_CASH_VAULT=fromfile\nMEXC_API_KEY=nope\nIBKR_FLEX_TOKEN=nope\n")
        self.assertEqual(envconfig.get("ETHERFI_CASH_VAULT"), "fromfile")
        self.assertIsNone(envconfig.get("MEXC_API_KEY"))
        self.assertIsNone(envconfig.get("IBKR_FLEX_TOKEN"))
        with mock.patch.dict(os.environ, {"ETHERFI_CASH_VAULT": "fromenv"}):
            self.assertEqual(envconfig.get("ETHERFI_CASH_VAULT"), "fromenv")

    def test_missing_file(self):
        self.assertIsNone(envconfig.get("ETHERFI_CASH_VAULT"))
        self.assertEqual(envconfig.get("NOPE", "d"), "d")

    def test_wallet_override_keeps_json_fallback(self):
        d = Path(self.tmp.name) / "wallets"
        d.mkdir()
        (d / "wallets.json").write_text(json.dumps({"wallets": [
            {"id": "my-arb", "chain": "arbitrum", "address": "0x00000000000000000000000000000000DEADBEEF"},
            {"id": "my tao", "chain": "bittensor", "address": "5JsonOnlyXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"}]}))
        self.assertEqual(envconfig.wallet_var("my tao"), "WALLET_MY_TAO_ADDRESS")
        self.write(f"WALLET_MY_ARB_ADDRESS={FAKE_EVM}\n")
        ws = wallets.load_config(d)
        self.assertEqual(ws[0]["address"], FAKE_EVM)
        self.assertTrue(ws[1]["address"].startswith("5JsonOnly"))   # no env var -> json value

    def test_etherfi_vault_override(self):
        d = Path(self.tmp.name) / "efc"
        self.assertFalse(etherfi_cash.load_config(d)["enabled"])        # default file, no env
        self.write(f"ETHERFI_CASH_VAULT={FAKE_EVM}\n")
        c = etherfi_cash.load_config(d)
        self.assertEqual((c["vault"], c["enabled"]), (FAKE_EVM, True))

    def test_flex_query_id_from_env_file_only_for_ids(self):
        self.write("IBKR_FLEX_QUERY_ID=1234567\nIBKR_FLEX_TOKEN=fromfile\n")
        with mock.patch.object(client, "keychain_read", return_value=None), \
             mock.patch.object(client, "_file_secret", return_value=None), \
             mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("IBKR_FLEX_QUERY_ID", None); os.environ.pop("IBKR_FLEX_TOKEN", None)
            self.assertEqual(client.load_secret("IBKR_FLEX_QUERY_ID"), "1234567")
            with self.assertRaises(SystemExit):
                client.load_secret("IBKR_FLEX_TOKEN")   # tokens never come from .env


class PrecommitScan(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(ROOT / "tools"))
        import precommit_scan
        self.s = precommit_scan

    def test_rules(self):
        s = self.s
        hashes = {s.sha("0x00000000000000000000000000000000C0FFEE00")}
        files = {
            "a.py": [(1, 'X = "privvalue123"'), (2, "ok = 1")],
            "b.py": [(3, "addr = '0x00000000000000000000000000000000c0ffee00'")],
            "c.py": [(4, 'api_key = "abcdefghijklmnopqrstuvwx"'), (5, 'K = "abcdefghijklmnopqrstuvwx"  # allowlist-secret')],
            "d.txt": [(1, "-----BEGIN RSA PRIVATE KEY-----")],  # allowlist-secret (fixture),
            ".env": [(1, "A=b")], "archive/wallets/wallets.json": [(1, "{}")],
            ".env.example": [(1, "ETHERFI_CASH_VAULT=0x00000000000000000000000000000000DEADBEEF")],
            "archive/.gitkeep": [],
        }
        out = s.scan(files, {"PrivValue123"}, hashes)
        self.assertEqual(len(out), 6, out)
        self.assertTrue(any(o.startswith("a.py:1:") for o in out))
        self.assertTrue(any(o.startswith("b.py:3:") and "known" in o for o in out))
        self.assertTrue(any(o.startswith("c.py:4:") for o in out))
        self.assertFalse(any(o.startswith("c.py:5:") or ".env.example" in o or ".gitkeep" in o for o in out))
        self.assertFalse(any("privvalue123" in o.lower() for o in out))   # values are never echoed

    def test_committed_hash_list_has_no_plaintext(self):
        for line in (ROOT / "tools" / "private_hashes.txt").read_text().splitlines():
            if line and not line.startswith("#"):
                self.assertRegex(line.split()[0], r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
