"""Per-voice `extract` override in council.toml. A wrapper that prints plain text in
place of a built-in's JSON transport (grok-p in place of `grok --output-format json`)
must be read with the plain extractor, or an answer that is itself a JSON object is
taken for the transport and blanked or truncated. Exercised through the real invoke()
with only subprocess.run faked — offline, no CLIs."""
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from council import config  # noqa: E402
from council import providers as PV  # noqa: E402

JSON_ANSWER = '{"verdict": "SHIP", "findings": []}'
TEXT_KEY_ANSWER = '{"text": "SHIP", "findings": ["f.py:1 bug"]}'


class TestExtractOverride(unittest.TestCase):
    def _load(self, body):
        d = tempfile.mkdtemp()
        p = Path(d) / "council.toml"
        p.write_text(textwrap.dedent(body))
        return config.load(str(p))

    def _invoke(self, provider, stdout):
        done = subprocess.CompletedProcess(args=["x"], returncode=0, stdout=stdout, stderr="")
        with mock.patch.object(PV, "is_installed", return_value=True), \
             mock.patch.object(PV.subprocess, "run", return_value=done):
            return PV.invoke(provider, "PROMPT", timeout=5)

    def test_builtin_grok_keeps_json_extractor_by_default(self):
        cfg = self._load("""
            [council]
            voices = ["grok"]
            [providers.grok]
            argv = ["grok-p", "--file", "{prompt_file}"]
        """)
        self.assertIs(cfg.providers["grok"].extract, PV._grok_json)

    def test_plain_override_keeps_a_json_object_answer_intact(self):
        cfg = self._load("""
            [council]
            voices = ["grok"]
            [providers.grok]
            bin = "grok-p"
            argv = ["grok-p", "--file", "{prompt_file}"]
            auth_check = []
            extract = "plain"
        """)
        grok = cfg.providers["grok"]
        self.assertIs(grok.extract, PV._plain)
        self.assertEqual(self._invoke(grok, JSON_ANSWER + "\n"), (True, JSON_ANSWER))
        self.assertEqual(self._invoke(grok, TEXT_KEY_ANSWER), (True, TEXT_KEY_ANSWER))
        # the other override fields still land
        self.assertEqual(grok.bin, "grok-p")
        self.assertTrue(grok.uses_prompt_file)

    def test_without_override_the_json_answer_is_mangled(self):
        # The failure the override exists for, pinned so a future default change is seen.
        cfg = self._load("""
            [council]
            voices = ["grok"]
            [providers.grok]
            argv = ["grok-p", "--file", "{prompt_file}"]
            auth_check = []
        """)
        grok = cfg.providers["grok"]
        ok, out = self._invoke(grok, JSON_ANSWER)
        self.assertFalse(ok)
        self.assertIn("empty output", out)
        self.assertEqual(self._invoke(grok, TEXT_KEY_ANSWER), (True, "SHIP"))

    def test_new_cli_voice_can_select_grok_json(self):
        cfg = self._load("""
            [council]
            voices = ["g2"]
            [providers.g2]
            type = "cli"
            bin = "grok"
            argv = ["grok", "--prompt-file", "{prompt_file}", "--output-format", "json"]
            extract = "grok_json"
        """)
        g2 = cfg.providers["g2"]
        self.assertIs(g2.extract, PV._grok_json)
        self.assertEqual(self._invoke(g2, '{"text": "hello"}'), (True, "hello"))

    def test_new_cli_voice_defaults_to_plain(self):
        cfg = self._load("""
            [council]
            voices = ["v"]
            [providers.v]
            type = "cli"
            bin = "v"
            argv = ["v"]
        """)
        self.assertIs(cfg.providers["v"].extract, PV._plain)

    def test_unknown_or_non_string_extract_fails_loudly(self):
        for bad in ('"xml"', '["plain"]', "1"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError) as e:
                    self._load(f"""
                        [council]
                        voices = ["grok"]
                        [providers.grok]
                        extract = {bad}
                    """)
                self.assertIn("extract must be one of", str(e.exception))


if __name__ == "__main__":
    unittest.main()
