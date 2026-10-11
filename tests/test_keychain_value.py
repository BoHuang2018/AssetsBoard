"""Keychain values: whitespace around the value and `security -w`'s hex output for non-text bytes are undone;
real hex secrets are left alone."""
import unittest

from assetsboard.client import clean_keychain_value as clean


class KeychainValue(unittest.TestCase):
    def test_plain_and_whitespace(self):
        self.assertEqual(clean("mx0vglFAKEFAKE1234\n"), "mx0vglFAKEFAKE1234")
        self.assertEqual(clean("  abc_DEF-123 \n"), "abc_DEF-123")

    def test_hex_output_of_value_with_newline_is_decoded(self):
        stored = "mx0vglFAKEFAKE1234\n"                       # pasted with a trailing newline
        self.assertEqual(clean(stored.encode().hex() + "\n"), "mx0vglFAKEFAKE1234")
        self.assertEqual(clean(("\tsecretFAKE99 ").encode().hex()), "secretFAKE99")

    def test_real_hex_secret_untouched(self):
        for h in ("0123456789abcdef0123456789abcdef", "a3f9" * 16, "deadbeefcafebabe00112233445566778899aabbccddeeff0011223344556677"):
            self.assertEqual(clean(h + "\n"), h)
        # hex that happens to decode to printable text but has no whitespace/control bytes: kept as is
        self.assertEqual(clean("41424344454647484950"), "41424344454647484950")


if __name__ == "__main__":
    unittest.main()
