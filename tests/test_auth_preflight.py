"""Auth/liveness preflight for lapse-prone subscription CLIs.

The bug this guards: grok's OAuth access token is a short (~6h) OIDC token. When
it lapses, an unauthenticated `grok -p` HANGS the real call to the full 600s
ceiling — so every council run eats 600s on a dead grok voice. But `grok models`
returns FAST and prints "You are not authenticated." (even with exit 0), so the
fix gates on that OUTPUT and fails the voice fast + loud. No CLIs, no network."""
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import council.providers as PV  # noqa: E402


class _Done:
    """Minimal stand-in for subprocess.CompletedProcess."""
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


def _grok():
    return PV.PROVIDERS["grok"]


class TestAuthPreflight(unittest.TestCase):
    def test_grok_ships_with_the_preflight_configured(self):
        p = _grok()
        self.assertEqual(p.auth_check, ["grok", "models"])
        self.assertIn("not authenticated", p.auth_fail_marker.lower())

    def test_unauthenticated_fails_fast_and_never_fires_the_real_call(self):
        def fake_run(argv, **kw):
            if argv[:2] == ["grok", "models"]:              # the preflight probe
                return _Done(stdout="You are not authenticated.\n", returncode=0)
            raise AssertionError("real grok call must NOT run when unauthenticated")
        with mock.patch.object(PV, "is_installed", lambda _p: True), \
                mock.patch.object(PV.subprocess, "run", side_effect=fake_run):
            ok, out = PV.invoke(_grok(), "review this diff", timeout=600)
        self.assertFalse(ok)
        self.assertIn("not authenticated", out.lower())
        self.assertIn("grok login", out)                    # the loud, actionable hint

    def test_authenticated_proceeds_to_the_real_call(self):
        def fake_run(argv, **kw):
            if argv[:2] == ["grok", "models"]:
                return _Done(stdout="You are logged in with grok.com.\n")
            return _Done(stdout='{"text": "REVIEW_OK"}')     # grok --output-format json
        with mock.patch.object(PV, "is_installed", lambda _p: True), \
                mock.patch.object(PV.subprocess, "run", side_effect=fake_run):
            ok, out = PV.invoke(_grok(), "review this diff", timeout=600)
        self.assertTrue(ok)
        self.assertEqual(out, "REVIEW_OK")

    def test_probe_that_hangs_fails_fast_not_at_the_full_ceiling(self):
        def fake_run(argv, **kw):
            if argv[:2] == ["grok", "models"]:
                raise PV.subprocess.TimeoutExpired(cmd=argv, timeout=kw.get("timeout"))
            raise AssertionError("real grok call must NOT run when the preflight hangs")
        with mock.patch.object(PV, "is_installed", lambda _p: True), \
                mock.patch.object(PV.subprocess, "run", side_effect=fake_run):
            ok, out = PV.invoke(_grok(), "review this diff", timeout=600)
        self.assertFalse(ok)
        self.assertTrue("hung" in out.lower() or "stuck" in out.lower())

    def test_probe_timeout_is_capped_well_under_the_voice_ceiling(self):
        seen = {}
        def fake_run(argv, **kw):
            if argv[:2] == ["grok", "models"]:
                seen["probe_timeout"] = kw.get("timeout")
                return _Done(stdout="You are logged in with grok.com.\n")
            return _Done(stdout='{"text": "OK"}')
        with mock.patch.object(PV, "is_installed", lambda _p: True), \
                mock.patch.object(PV.subprocess, "run", side_effect=fake_run):
            PV.invoke(_grok(), "x", timeout=600)
        self.assertLessEqual(seen["probe_timeout"], PV.AUTH_CHECK_TIMEOUT)

    def test_voice_without_auth_check_skips_the_preflight(self):
        # codex has no auth_check → the preflight is a no-op; exactly one call runs.
        calls = []
        def fake_run(argv, **kw):
            calls.append(argv)
            return _Done(stdout="CODEX_OK")
        with mock.patch.object(PV, "is_installed", lambda _p: True), \
                mock.patch.object(PV.subprocess, "run", side_effect=fake_run):
            ok, out = PV.invoke(PV.PROVIDERS["codex"], "hi", timeout=30)
        self.assertTrue(ok)
        self.assertEqual(out, "CODEX_OK")
        self.assertEqual(len(calls), 1)   # no extra preflight call was added


if __name__ == "__main__":
    unittest.main()
