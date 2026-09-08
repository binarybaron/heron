import unittest

import support  # noqa: F401  (sets HERON_STATE before heron.common is imported)
from heron.common import redact


class RedactTest(unittest.TestCase):
    def test_known_shapes(self) -> None:
        text = (
            "gh token ghp_abcdefghijklmnopqrstuvwxyz0123456789 and sk-ant-api03-abcdefghijklmnopqrstuvwxyz "
            "Authorization: Bearer abcdefghijklmnop.qrstuvwxyz0123 password=Sup3rSecretValue "
            "https://x-access-token:gho_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789@github.com/o/r.git"
        )
        out = redact(text)
        for secret in ("ghp_abcdefghijklmnopqrstuvwxyz0123456789", "sk-ant-api03", "qrstuvwxyz0123", "Sup3rSecretValue", "gho_ABCDEFGHIJ"):
            self.assertNotIn(secret, out)
        self.assertIn("gh token [redacted]", out)
        self.assertIn("password=[redacted]", out)
        self.assertIn("https://x-access-token:[redacted]@github.com", out)

    def test_plain_text_untouched(self) -> None:
        text = "cargo check finished in 3.2s; the token bucket refills hourly"
        self.assertEqual(redact(text), text)


if __name__ == "__main__":
    unittest.main()
