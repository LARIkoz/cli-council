"""Per-voice timeout resolution + config precedence. No network, no CLIs."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from council import config as C
from council.providers import DEFAULT_TIMEOUT, PROVIDERS, resolve_timeout


class TestResolveTimeout(unittest.TestCase):
    def test_native_uses_default(self):
        # claude declares no ceiling → falls back to DEFAULT_TIMEOUT.
        self.assertEqual(resolve_timeout(PROVIDERS["claude"], None), DEFAULT_TIMEOUT)

    def test_slow_voices_get_headroom(self):
        # codex/grok time out on the ranking bundle at 300s; they declare 600.
        self.assertEqual(resolve_timeout(PROVIDERS["codex"], None), 600.0)
        self.assertEqual(resolve_timeout(PROVIDERS["grok"], None), 600.0)

    def test_explicit_override_wins_for_every_voice(self):
        # A global --timeout / toml value beats the per-voice ceiling both ways.
        self.assertEqual(resolve_timeout(PROVIDERS["codex"], 120.0), 120.0)
        self.assertEqual(resolve_timeout(PROVIDERS["claude"], 900.0), 900.0)


class TestConfigTimeout(unittest.TestCase):
    def test_no_file_is_per_voice(self):
        # Isolate load()'s no-file behaviour from filesystem discovery: a real
        # user (or this box) may have a council.toml at the repo root, which _find
        # would legitimately pick up. We assert the branch where nothing is found.
        with patch.object(C, "_find", return_value=None):
            cfg = C.load(None)
        self.assertIsNone(cfg.timeout)  # None => each voice uses its own ceiling
        self.assertEqual(cfg.voices, ["claude"])

    def _write(self, body: str) -> str:
        fd = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
        fd.write(body)
        fd.close()
        self.addCleanup(lambda: Path(fd.name).unlink(missing_ok=True))
        return fd.name

    def test_absent_timeout_stays_none(self):
        cfg = C.load(self._write('[council]\nvoices = ["claude", "codex"]\n'))
        self.assertIsNone(cfg.timeout)
        self.assertEqual(resolve_timeout(cfg.providers["codex"], cfg.timeout), 600.0)

    def test_global_timeout_overrides_all(self):
        cfg = C.load(self._write('[council]\nvoices = ["claude"]\ntimeout = 250\n'))
        self.assertEqual(cfg.timeout, 250.0)
        self.assertEqual(resolve_timeout(cfg.providers["codex"], cfg.timeout), 250.0)

    def test_per_provider_timeout_override(self):
        cfg = C.load(self._write(
            '[council]\nvoices = ["claude", "codex"]\n\n[providers.codex]\ntimeout = 900\n'))
        self.assertIsNone(cfg.timeout)
        self.assertEqual(resolve_timeout(cfg.providers["codex"], cfg.timeout), 900.0)


if __name__ == "__main__":
    unittest.main()
