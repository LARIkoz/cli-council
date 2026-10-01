"""session_rules: a voice can be told, in the prompt itself, that it is one independent
voice (no subagents or couriers, no commands, read only). No network: a shell CLI echoes
what it received."""
import tempfile
import unittest
from pathlib import Path

from council import config as C
from council import providers as P


class TestSessionRules(unittest.TestCase):
    def _cfg(self, body):
        fd = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
        fd.write(body)
        fd.close()
        self.addCleanup(lambda: Path(fd.name).unlink(missing_ok=True))
        return C.load(fd.name)

    def test_new_cli_voice_gets_the_block_prepended_once(self):
        cfg = self._cfg('[providers.echo]\ntype = "cli"\nbin = "cat"\nargv = ["cat"]\n'
                        'session_rules = true\n[council]\nvoices = ["echo"]\nchairman = "echo"\n')
        ok, out = P.invoke(cfg.providers["echo"], "review this", 30)
        self.assertTrue(ok)
        self.assertTrue(out.startswith("SESSION RULES:"))
        self.assertTrue(out.rstrip().endswith("review this"))
        ok, out2 = P.invoke(cfg.providers["echo"], out, 30)   # already carries a block
        self.assertEqual(out2.count("SESSION RULES:"), 1)

    def test_override_of_a_builtin_voice_can_turn_it_on(self):
        cfg = self._cfg('[providers.agy]\nbin = "cat"\nargv = ["cat"]\nsession_rules = true\n'
                        '[council]\nvoices = ["agy"]\nchairman = "agy"\n')
        self.assertTrue(cfg.providers["agy"].session_rules)

    def test_off_by_default(self):
        cfg = self._cfg('[providers.echo]\ntype = "cli"\nbin = "cat"\nargv = ["cat"]\n'
                        '[council]\nvoices = ["echo"]\nchairman = "echo"\n')
        ok, out = P.invoke(cfg.providers["echo"], "plain", 30)
        self.assertEqual(out.strip(), "plain")


if __name__ == "__main__":
    unittest.main()
