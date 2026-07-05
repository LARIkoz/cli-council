"""Enrolment gate: `enroll` must re-smoke and write ONLY the PASSes. Monkeypatched
so it runs offline — the invariant is 'no voice reaches council.toml without a PASS'."""
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "installer"))
import doctor  # noqa: E402


class TestEnrollSmokeGate(unittest.TestCase):
    def setUp(self):
        # A fake smoke: only these voices "answer". is_installed is always True so
        # the gate is exercised by the invoke result, not the which() check.
        self._GOOD = {"claude", "codex"}
        self._orig_invoke = doctor.invoke
        self._orig_installed = doctor.is_installed
        self._orig_cfg = doctor.CONFIG
        doctor.is_installed = lambda p: True
        doctor.invoke = lambda p, prompt, timeout=90.0: (
            (True, "ok") if p.name in self._GOOD else (False, f"{p.name}: exit 1: dead"))
        fd = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
        fd.close()
        self._tmp = Path(fd.name)
        doctor.CONFIG = self._tmp

    def tearDown(self):
        doctor.invoke = self._orig_invoke
        doctor.is_installed = self._orig_installed
        doctor.CONFIG = self._orig_cfg
        self._tmp.unlink(missing_ok=True)

    def _written_voices(self):
        import tomllib
        return tomllib.loads(self._tmp.read_text()).get("council", {}).get("voices", [])

    def test_failing_voice_is_refused_not_written(self):
        rc = doctor.enroll(["claude", "gemini"])  # gemini "fails" smoke
        self.assertEqual(rc, 0)
        self.assertEqual(self._written_voices(), ["claude"])  # gemini dropped

    def test_all_failing_writes_nothing(self):
        self._tmp.write_text("SENTINEL")  # must NOT be overwritten
        rc = doctor.enroll(["gemini", "agy"])  # both fail
        self.assertEqual(rc, 1)
        self.assertEqual(self._tmp.read_text(), "SENTINEL")

    def test_no_verify_trusts_caller(self):
        rc = doctor.enroll(["claude", "gemini"], verify=False)  # skip re-smoke
        self.assertEqual(rc, 0)
        self.assertEqual(self._written_voices(), ["claude", "gemini"])

    def test_enroll_writes_gated_panels(self):
        # enroll must produce a GATED config (not a bare council): chairman off its own
        # audit, decide redteam OFF, review keeps a lean redteam.
        import tomllib
        doctor.enroll(["claude", "codex"], verify=False)  # chairman defaults to claude
        cfg = tomllib.loads(self._tmp.read_text())
        self.assertEqual(cfg["decide"]["audit"], ["codex"])     # non-chairman only
        self.assertEqual(cfg["decide"]["redteam"], [])          # off for a decision
        self.assertEqual(cfg["review"]["audit"], ["codex"])
        self.assertEqual(cfg["review"]["redteam"], ["codex"])

    def test_reenroll_preserves_timeout_and_providers(self):
        # A user's documented [council].timeout escape hatch and hand-written
        # [providers.*] blocks must survive a re-enroll — enroll owns voices/chairman/
        # panels, not those. (Panels ARE regenerated from the new voice set.)
        import tomllib
        self._tmp.write_text(
            '[council]\nvoices = ["claude"]\nchairman = "claude"\ntimeout = 720\n'
            '[review]\naudit = ["stale"]\n'  # a stale hand-tuned panel — should be regenerated
            '\n[providers.deepseek]\ntype = "http"\nkey_env = "DEEPSEEK_API_KEY"\n')
        doctor.enroll(["claude", "codex"], verify=False)
        cfg = tomllib.loads(self._tmp.read_text())
        self.assertEqual(cfg["council"]["timeout"], 720)              # preserved
        self.assertEqual(cfg["providers"]["deepseek"]["type"], "http")  # preserved
        self.assertEqual(cfg["review"]["audit"], ["codex"])          # regenerated, not "stale"


if __name__ == "__main__":
    unittest.main()
