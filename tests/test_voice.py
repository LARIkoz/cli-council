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

    def test_unknown_voice_is_rejected_at_launch_without_a_job(self):
        out = self.tmp / "job"
        for extra in ([], ["--detach"]):
            with self.assertRaises(SystemExit):
                self.run_cli("voice", "nope", "--prompt-file", str(self.prompt),
                             "--out-dir", str(out), "--config", str(self.cfg), *extra)
        self.assertFalse(out.exists())

    def test_engine_exception_is_a_loud_failure_with_traceback(self):
        out = self.tmp / "job"
        orig = V.providers.invoke_chain
        V.providers.invoke_chain = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("engine blew up"))
        try:
            rc, text = self.run_cli("voice", "echo", "--prompt-file", str(self.prompt),
                                    "--out-dir", str(out), "--config", str(self.cfg))
        finally:
            V.providers.invoke_chain = orig
        self.assertEqual(rc, V.EXIT_FAILED)
        self.assertIn("failed:engine_error", text)
        self.assertIn("engine blew up", text)

    def test_voice_runs_in_the_job_directory_and_cwd_is_restored(self):
        cfg = self.tmp / "pwd.toml"
        cfg.write_text('[providers.pwd]\ntype = "cli"\nbin = "sh"\nargv = ["sh", "-c", "cat >/dev/null; pwd"]\n'
                       '[council]\nvoices = ["pwd"]\nchairman = "pwd"\n')
        out = self.tmp / "job"
        here = Path.cwd()
        rc, text = self.run_cli("voice", "pwd", "--prompt-file", str(self.prompt),
                                "--out-dir", str(out), "--config", str(cfg))
        self.assertEqual(rc, V.EXIT_ALIVE)
        self.assertEqual(Path(text.strip().splitlines()[-1]).resolve(), out.resolve())
        self.assertEqual(Path.cwd(), here)

    def test_job_files_are_private(self):
        out = self.tmp / "job"
        self.run_cli("voice", "echo", "--prompt-file", str(self.prompt),
                     "--out-dir", str(out), "--config", str(self.cfg))
        self.assertEqual(out.stat().st_mode & 0o777, 0o700)
        self.assertEqual((out / "prompt.md").stat().st_mode & 0o777, 0o600)

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

    def test_wait_never_overwrites_a_final_status(self):
        out = self.tmp / "job"
        out.mkdir()
        V._update_status(out, voice="echo", status="alive", started=time.time(), pid=2 ** 22 + 12345)
        self.assertFalse(V._update_status(out, only_if_status="running", status="failed", reason="job_died"))
        self.assertEqual(V.read_status(out)["status"], "alive")

    def test_wait_honours_max_without_oversleeping(self):
        out = self.tmp / "job"
        out.mkdir()
        V._update_status(out, voice="slow", status="running", started=time.time(), pid=__import__("os").getpid())
        t0 = time.time()
        rc, _ = V.wait(out, 0.3, poll=2.0)
        self.assertEqual(rc, V.EXIT_RUNNING)
        self.assertLess(time.time() - t0, 1.5)

    def test_dead_worker_reports_its_run_log(self):
        out = self.tmp / "job"
        out.mkdir()
        (out / "run.log").write_text("Traceback: ImportError: no module named council\n")
        V._update_status(out, voice="echo", status="running", started=time.time(), pid=2 ** 22 + 12345)
        rc, text = V.wait(out, 5)
        self.assertEqual(rc, V.EXIT_FAILED)
        self.assertIn("failed:job_died", text)
        self.assertIn("ImportError", text)

    def test_wait_reports_a_dead_worker_instead_of_hanging(self):
        out = self.tmp / "job"
        out.mkdir()
        (out / "prompt.md").write_text("x")
        V._update_status(out, voice="echo", status="running", started=time.time(), pid=2 ** 22 + 12345)
        rc, text = self.run_cli("wait", str(out), "--max", "5")
        self.assertEqual(rc, V.EXIT_FAILED)
        self.assertIn("failed:job_died", text)


class TestRound2(_Base):
    def test_detached_worker_gets_an_absolute_config_path(self):
        import os
        out = self.tmp / "job"
        here = os.getcwd()
        os.chdir(self.tmp)
        try:
            rc, _ = self.run_cli("voice", "echo", "--prompt-file", str(self.prompt),
                                 "--out-dir", str(out), "--config", "council.toml", "--detach")
        finally:
            os.chdir(here)
        self.assertEqual(rc, 0)
        rc, text = self.run_cli("wait", str(out), "--max", "30")
        self.assertEqual(rc, V.EXIT_ALIVE, text)
        self.assertTrue(Path(V.read_status(out)["config"]).is_absolute())

    def test_existing_prompt_wins_over_an_empty_non_terminal_stdin(self):
        out = self.tmp / "job"
        out.mkdir()
        (out / "prompt.md").write_text("from the job dir")
        rc, text = self.run_cli("voice", "echo", "--out-dir", str(out), "--config", str(self.cfg))
        self.assertEqual(rc, V.EXIT_ALIVE, text)
        self.assertIn("from the job dir [echoed]", text)

    def test_alive_without_answer_is_a_failure(self):
        out = self.tmp / "job"
        out.mkdir()
        V._update_status(out, voice="echo", status="alive", started=time.time(), pid=1)
        rc, text = V.wait(out, 1)
        self.assertEqual(rc, V.EXIT_FAILED)
        self.assertIn("answer.md is missing", text)


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
