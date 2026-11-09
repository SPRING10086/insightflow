from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PackagingGuardTests(unittest.TestCase):
    def test_build_script_excludes_dotenv(self):
        text = (ROOT / "scripts" / "build_portable.ps1").read_text(encoding="utf-8-sig")
        self.assertIn('".env"', text, "build script must exclude .env from the portable bundle")
        self.assertIn(".env.*", text, "build script must exclude .env.* variants")

    def test_smoke_script_checks_for_secrets(self):
        text = (ROOT / "scripts" / "smoke_portable_llm.py").read_text(encoding="utf-8")
        self.assertIn("no_dotenv_in_bundle", text)
        self.assertIn("no_api_key_in_bundle", text)


if __name__ == "__main__":
    unittest.main()
