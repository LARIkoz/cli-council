"""`council voice` / `council wait` / `council roster`: one voice through the same
provider registry as the council. Fake voices are tiny shell CLIs, so no network."""
import io
import json
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from council import voice as V
from council.__main__ import main


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = self.tmp / "council.toml"
        self.cfg.write_text(
            '[providers.echo]\ntype = "cli"\nbin = "sh"\n'
            'argv = ["sh", "-c", "cat; printf \' [echoed]\'"]\ntimeout = 30\n'
            '[providers.broken]\ntype = "cli"\nbin = "sh"\n'
            'argv = ["sh", "-c", "echo boom >&2; exit 7"]\ntimeout = 30\n'
            '[providers.slow]\ntype = "cli"\nbin = "sh"\n'
            'argv = ["sh", "-c", "sleep 3; echo late"]\ntimeout = 30\n'
            '[council]\nvoices = ["echo", "slow"]\nchairman = "echo"\n')
        self.prompt = self.tmp / "p.md"
        self.prompt.write_text("hello voice")

    def run_cli(self, *args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(list(args))
        return rc, buf.getvalue()


class TestVoiceForeground(_Base):
    def test_alive_writes_answer_and_status_line(self):
        out = self.tmp / "job"
        rc, text = self.run_cli("voice", "echo", "--prompt-file", str(self.prompt),
                                "--out-dir", str(out), "--config", str(self.cfg))
        self.assertEqual(rc, V.EXIT_ALIVE)
        self.assertTrue(text.startswith("council-voice status: alive · voice: echo"))
        self.assertIn("hello voice [echoed]", text)
        self.assertEqual((out / "prompt.md").read_text(), "hello voice")
        self.assertEqual(V.read_status(out)["status"], "alive")

    def test_failed_voice_is_loud_and_never_writes_an_answer(self):
        out = self.tmp / "job"
        rc, text = self.run_cli("voice", "broken", "--prompt-file", str(self.prompt),
                                "--out-dir", str(out), "--config", str(self.cfg))
        self.assertEqual(rc, V.EXIT_FAILED)
        self.assertIn("status: failed:voice_error", text)
        self.assertIn("error_head:", text)
        self.assertFalse((out / "answer.md").exists())

    def test_unknown_voice_fails_without_running_anything(self):
        out = self.tmp / "job"
        rc, text = self.run_cli("voice", "nope", "--prompt-file", str(self.prompt),
                                "--out-dir", str(out), "--config", str(self.cfg))
        self.assertEqual(rc, V.EXIT_FAILED)
        self.assertIn("failed:unknown_voice", text)

    def test_refuses_to_reuse_a_job_directory(self):
        out = self.tmp / "job"
        self.run_cli("voice", "echo", "--prompt-file", str(self.prompt),
                     "--out-dir", str(out), "--config", str(self.cfg))
        with self.assertRaises(SystemExit):
            self.run_cli("voice", "echo", "--prompt-file", str(self.prompt),
                         "--out-dir", str(out), "--config", str(self.cfg))


class TestDetachAndWait(_Base):
    def test_detached_job_is_collected_by_wait(self):
        out = self.tmp / "job"
        rc, text = self.run_cli("voice", "slow", "--prompt-file", str(self.prompt),
                                "--out-dir", str(out), "--config", str(self.cfg), "--detach")
        self.assertEqual(rc, 0)
        self.assertIn("started · voice: slow", text)
        rc, text = self.run_cli("wait", str(out), "--max", "0.5")
        self.assertEqual(rc, V.EXIT_RUNNING)
        self.assertTrue(text.startswith("still_running"))
        rc, text = self.run_cli("wait", str(out), "--max", "30")
        self.assertEqual(rc, V.EXIT_ALIVE, text)
        self.assertIn("late", text)

    def test_wait_reports_a_dead_worker_instead_of_hanging(self):
        out = self.tmp / "job"
        out.mkdir()
        (out / "prompt.md").write_text("x")
        V._write_status(out, voice="echo", status="running", started=time.time(), pid=2 ** 22 + 12345)
        rc, text = self.run_cli("wait", str(out), "--max", "5")
        self.assertEqual(rc, V.EXIT_FAILED)
        self.assertIn("failed:job_died", text)


class TestRoster(_Base):
    def test_roster_prints_enrolled_voices(self):
        rc, text = self.run_cli("roster", "--config", str(self.cfg))
        self.assertEqual(rc, 0)
        self.assertEqual(text.split(), ["echo", "slow"])

    def test_roster_json_carries_chairman(self):
        rc, text = self.run_cli("roster", "--config", str(self.cfg), "--json")
        self.assertEqual(json.loads(text)["chairman"], "echo")


if __name__ == "__main__":
    unittest.main()
