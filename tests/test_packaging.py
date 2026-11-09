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

    def test_build_script_excludes_operator_config(self):
        text = (ROOT / "scripts" / "build_portable.ps1").read_text(encoding="utf-8-sig")
        self.assertIn('"config.json"', text, "operator config (paths/settings) must not ship in the bundle")

    def test_release_checklist_and_verifier_exist(self):
        checklist = ROOT / "docs" / "release-checklist-llm.md"
        verifier = ROOT / "scripts" / "verify_release.ps1"
        self.assertTrue(checklist.is_file())
        self.assertTrue(verifier.is_file())
        checklist_text = checklist.read_text(encoding="utf-8")
        verifier_text = verifier.read_text(encoding="utf-8-sig")
        self.assertIn("signtool", checklist_text)
        self.assertIn("Get-AuthenticodeSignature", verifier_text)
        self.assertIn("llm-stage5-smoke", verifier_text)

    def test_smoke_script_checks_for_secrets(self):
        text = (ROOT / "scripts" / "smoke_portable_llm.py").read_text(encoding="utf-8")
        self.assertIn("no_dotenv_in_bundle", text)
        self.assertIn("no_api_key_in_bundle", text)


if __name__ == "__main__":
    unittest.main()
