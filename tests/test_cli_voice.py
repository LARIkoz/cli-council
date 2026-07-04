"""Defining a new subscription-CLI voice via a `type = "cli"` council.toml block
(and the shared argv-must-be-an-array guard). No network, no CLIs.

This branch had ZERO coverage when it shipped — which is exactly why a green suite
missed the argv-as-string char-explosion bug (found by /consreview dogfood 2026-07-04)."""
import tempfile
import unittest
from pathlib import Path

from council import config as C


class TestCliVoiceDefinition(unittest.TestCase):
    def _write(self, body: str) -> str:
        fd = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
        fd.write(body)
        fd.close()
        self.addCleanup(lambda: Path(fd.name).unlink(missing_ok=True))
        return fd.name

    def test_defines_new_cli_voice_with_full_passthrough(self):
        cfg = C.load(self._write(
            '[providers.opus]\n'
            'type = "cli"\nbin = "claude"\n'
            'argv = ["claude", "-p", "--model", "opus"]\n'
            'native = true\nexperimental = true\ntimeout = 450\n'
            '[council]\nvoices = ["opus"]\nchairman = "opus"\n'))
        p = cfg.providers["opus"]
        self.assertEqual(p.transport, "cli")
        self.assertEqual(p.bin, "claude")
        self.assertEqual(p.argv, ["claude", "-p", "--model", "opus"])
        self.assertTrue(p.native)
        self.assertTrue(p.experimental)   # was silently dropped before the fix
        self.assertEqual(p.timeout, 450.0)

    def test_argv_as_string_fails_loudly_at_load(self):
        # The bug: list("claude -p") -> ['c','l','a',...]. Must raise at load, not
        # fail cryptically at subprocess time.
        with self.assertRaises(ValueError) as ctx:
            C.load(self._write(
                '[providers.bad]\ntype = "cli"\nbin = "claude"\nargv = "claude -p"\n'
                '[council]\nvoices = ["bad"]\n'))
        self.assertIn("argv must be an array", str(ctx.exception))

    def test_override_argv_as_string_also_fails_loudly(self):
        # The same latent coercion lived in the override arm (existing voice).
        with self.assertRaises(ValueError) as ctx:
            C.load(self._write(
                '[providers.codex]\nargv = "codex exec -"\n'
                '[council]\nvoices = ["codex"]\n'))
        self.assertIn("argv must be an array", str(ctx.exception))

    def test_missing_bin_or_argv_fails(self):
        with self.assertRaises(ValueError):
            C.load(self._write(
                '[providers.x]\ntype = "cli"\nbin = "claude"\n'   # no argv
                '[council]\nvoices = ["x"]\n'))

    def test_cli_type_on_existing_name_overrides_not_redefines(self):
        # type="cli" on a name already in the base table falls to the OVERRIDE arm
        # (argv replaced) — the base transport/extract survive; not a fresh voice.
        cfg = C.load(self._write(
            '[providers.codex]\ntype = "cli"\n'
            'argv = ["codex", "exec", "-c", "model_reasoning_effort=high", "-"]\n'
            '[council]\nvoices = ["codex"]\n'))
        p = cfg.providers["codex"]
        self.assertIn("model_reasoning_effort=high", p.argv)
        self.assertEqual(p.argv[-1], "-")

    def test_argv_elements_are_stringified(self):
        # Defensive: a stray non-string arg (e.g. a number) is coerced, not passed raw.
        cfg = C.load(self._write(
            '[providers.v]\ntype = "cli"\nbin = "x"\nargv = ["x", 7]\n'
            '[council]\nvoices = ["v"]\n'))
        self.assertEqual(cfg.providers["v"].argv, ["x", "7"])


if __name__ == "__main__":
    unittest.main()
