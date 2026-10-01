"""Behavioral session tests using real subprocesses but no models or network."""
import json
import os
from pathlib import Path
import subprocess
import signal
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, Mock
from urllib.parse import quote

from council import review_sessions as S
from council.session_runtime import (
    LOCAL_FIXTURE_ADMISSION,
    NativeStream,
    SessionError,
    native_argv,
    run_native_turn,
    _signal_group,
    _stop_group,
)


SID = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


def agy_events(sid=SID, reason="SUCCESS", response="Finding A: exact reproduction."):
    return [{"event": "init", "conversation_id": sid, "init": {"model": "Gemini 3.8 Flash (Medium)"}},
            {"event": "step_update", "step_update": {"conversation_id": sid,
             "step_type": "tool", "tool_name": "run_command", "state": "DONE",
             "tool_info": {"parameters": {"CommandLine": "python3 -m unittest"}, "output": "OK"}}},
            {"event": "result", "result": {"conversation_id": sid, "status": reason,
                                             "response": response}}]


def grok_events(sid=SID, reason="EndTurn"):
    return [{"type": "text", "data": "Finding A: "}, {"type": "text", "data": "exact reproduction."},
            {"type": "end", "sessionId": sid, "stopReason": reason, "requestId": "req-12345"}]


def fixture_review(*args, **kwargs):
    """Make every injected reviewer fixture an explicit non-native launch."""
    kwargs["admission"] = LOCAL_FIXTURE_ADMISSION
    return S.run_reviewer(*args, **kwargs)


def fixture_recheck(*args, **kwargs):
    """Exercise recheck transitions without enabling the native runtime."""
    kwargs["admission"] = LOCAL_FIXTURE_ADMISSION
    return S.recheck(*args, **kwargs)


class TestNativeTurns(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def run_child(self, events, *, runtime="antigravity", suffix="", timeout=3, **kwargs):
        td = self.root / f"turn-{len(list(self.root.iterdir()))}"
        td.mkdir()
        script = "import json,sys,time,os\n"
        for event in events:
            script += f"print(json.dumps({event!r}), flush=True)\n"
        script += suffix
        result = run_native_turn(runtime, [sys.executable, "-c", script], self.root, td,
                                 timeout=timeout, admission=LOCAL_FIXTURE_ADMISSION, **kwargs)
        return result, td

    def test_both_runtimes_complete_without_accepting_task(self):
        for runtime, events in (("antigravity", agy_events()), ("grok", grok_events())):
            result, td = self.run_child(events, runtime=runtime)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["conversation_id"], SID)
            self.assertFalse(result["claims_verified"])
            self.assertFalse(result["task_accepted"])
            self.assertIn("reproduction", (td / "response.md").read_text())

    def test_resume_rejects_another_conversation_even_if_success(self):
        result, td = self.run_child(agy_events(OTHER), expected_id=SID)
        self.assertEqual(result["status"], "failed")
        self.assertIn("different conversation", result["error"])
        self.assertTrue((td / "stdout.ndjson").stat().st_size)

    def test_timeout_preserves_identity_and_partial_events(self):
        result, td = self.run_child(agy_events()[:2], suffix="time.sleep(10)\n", timeout=2)
        self.assertEqual(result["status"], "timed_out")
        self.assertEqual(result["conversation_id"], SID)
        self.assertFalse(result["report_received"])
        self.assertEqual(len((td / "events.ndjson").read_text().splitlines()), 2)

    def test_planned_grok_uuid_is_not_a_persisted_conversation(self):
        result, _ = self.run_child(grok_events()[:1], runtime="grok", planned_id=SID,
                                   suffix="time.sleep(10)\n", timeout=2)
        self.assertEqual(result["status"], "timed_out")
        self.assertIsNone(result["conversation_id"])
        self.assertIsNone(result["requested_conversation_id"])
        self.assertEqual(result["planned_conversation_id"], SID)

    def test_preallocated_grok_uuid_must_match_terminal_identity(self):
        result, _ = self.run_child(grok_events(OTHER), runtime="grok", planned_id=SID)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["report_received"])
        self.assertIsNone(result["conversation_id"])

    def test_git_cannot_discover_parent_repo_or_inherit_worktree_override(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True, capture_output=True)
        workspace = self.root / "projection"
        workspace.mkdir()
        td = self.root / "native-turn"
        td.mkdir()
        script = ("import subprocess,json\n"
                  "p=subprocess.run(['git','rev-parse','--show-toplevel'],capture_output=True)\n"
                  "assert p.returncode != 0, p.stdout\n"
                  + "\n".join(f"print(json.dumps({e!r}),flush=True)" for e in grok_events()))
        with patch.dict(os.environ, {"GIT_DIR": str(self.root / ".git"), "GIT_WORK_TREE": str(self.root)}):
            result = run_native_turn("grok", [sys.executable, "-c", script], workspace, td, timeout=3,
                                     admission=LOCAL_FIXTURE_ADMISSION)
        self.assertEqual(result["status"], "completed")

    def test_empty_group_eperm_after_term_still_reaps_process(self):
        proc = Mock(pid=123456)
        with patch("council.session_runtime.os.killpg") as signal_group:
            with self.assertRaisesRegex(SessionError, "ownership receipt is incomplete"):
                _stop_group(proc)
        signal_group.assert_not_called()

    def test_cleanup_failure_retains_partial_evidence_and_original_timeout(self):
        def stop_then_report_failure(proc):
            _stop_group(proc)  # Do not leak a real child from the failure fixture.
            raise PermissionError(1, "simulated cleanup failure")

        with patch("council.session_runtime._stop_group", side_effect=stop_then_report_failure):
            result, td = self.run_child(grok_events()[:2], runtime="grok", expected_id=SID,
                                        suffix="time.sleep(10)\n", timeout=2)
        self.assertEqual(result["status"], "cleanup_failed")
        self.assertEqual(result["failure_status"], "timed_out")
        self.assertEqual(result["conversation_id"], SID)
        self.assertFalse(result["report_received"])
        self.assertIn("time budget", result["error"])
        self.assertEqual(result["process_cleanup"]["error_type"], "PermissionError")
        self.assertEqual(len((td / "events.ndjson").read_text().splitlines()), 2)
        self.assertIn("exact reproduction", (td / "response.md").read_text())

    def test_success_stops_background_helper_before_returning_report(self):
        marker = self.root / "background-activity"
        helper = ("import signal,time\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
                  f"f=open({str(marker)!r},'a')\ndeadline=time.monotonic()+5\n"
                  "while time.monotonic()<deadline:\n f.write('x');f.flush();time.sleep(.03)\n")
        suffix = ("import subprocess\n"
                  f"subprocess.Popen([{sys.executable!r},'-c',{helper!r}], "
                  "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
                  "deadline=time.monotonic()+2\n"
                  f"while not os.path.exists({str(marker)!r}) and time.monotonic()<deadline: time.sleep(.01)\n")
        result, _ = self.run_child(agy_events(), suffix=suffix)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["process_cleanup"]["status"], "stopped")
        self.assertTrue(marker.exists())
        size = marker.stat().st_size
        time.sleep(.15)
        self.assertEqual(marker.stat().st_size, size, "background helper survived the completed turn")

    def test_successful_native_result_is_not_accepted_when_cleanup_fails(self):
        def stop_then_report_failure(proc):
            _stop_group(proc)
            raise PermissionError(1, "simulated cleanup failure")
        with patch("council.session_runtime._stop_group", side_effect=stop_then_report_failure):
            result, _ = self.run_child(agy_events())
        self.assertEqual(result["status"], "cleanup_failed")
        self.assertEqual(result["failure_status"], "completed")
        self.assertEqual(result["native_terminal_reason"], "SUCCESS")
        self.assertFalse(result["report_received"])

    def test_group_eperm_is_not_ignored_when_group_still_exists(self):
        with patch("council.session_runtime.os.killpg", side_effect=PermissionError(1, "denied")), \
             patch("council.session_runtime.subprocess.run", return_value=Mock(returncode=0, stdout="123456\n")):
            with self.assertRaises(PermissionError):
                _signal_group(123456, signal.SIGTERM)

    def test_group_eperm_is_not_ignored_when_inventory_fails(self):
        with patch("council.session_runtime.os.killpg", side_effect=PermissionError(1, "denied")), \
             patch("council.session_runtime.subprocess.run", return_value=Mock(returncode=1, stdout="")):
            with self.assertRaises(PermissionError):
                _signal_group(123456, signal.SIGTERM)

    def test_native_success_then_nonzero_exit_is_failure(self):
        result, _ = self.run_child(agy_events(), suffix="sys.exit(9)\n")
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["report_received"])

    def test_truncated_and_empty_results_never_complete(self):
        for events in (agy_events()[:2], agy_events(reason="TIMEOUT"), agy_events(response=""),
                       agy_events() + [agy_events()[-1]]):
            result, _ = self.run_child(events)
            self.assertEqual(result["status"], "failed")

    def test_error_event_cannot_be_overridden_by_success(self):
        result, _ = self.run_child([{"type": "error", "message": "quota"}] + grok_events(), runtime="grok")
        self.assertEqual(result["status"], "failed")

    def test_current_grok_end_turn_and_token_limit_are_distinct(self):
        result, _ = self.run_child(grok_events(reason="end_turn"), runtime="grok")
        self.assertEqual(result["status"], "completed")
        result, _ = self.run_child(grok_events(reason="max_tokens"), runtime="grok")
        self.assertEqual(result["status"], "failed")

    def test_grok_native_model_usage_preserves_effective_model_identity(self):
        events = grok_events(reason="end_turn")
        events[-1]["modelUsage"] = {"grok-4.6-build": {"inputTokens": 20, "outputTokens": 5}}
        result, _ = self.run_child(events, runtime="grok")
        self.assertEqual(result["reported_model"], "grok-4.6-build")
        self.assertEqual(result["reported_models"], ["grok-4.6-build"])

    def test_malformed_terminal_reason_fails_without_losing_evidence(self):
        events = agy_events()
        events[-1]["result"]["status"] = {"unexpected": "shape"}
        result, td = self.run_child(events)
        self.assertEqual(result["status"], "failed")
        self.assertTrue((td / "stdout.ndjson").exists())

    def test_stderr_only_after_success_cannot_bypass_output_budget(self):
        result, td = self.run_child(agy_events(), suffix="for _ in range(34): os.write(2, b'x' * 1048576)\n")
        self.assertEqual(result["status"], "failed")
        self.assertIn("32 MiB", result["error"])
        self.assertFalse(result["report_received"])

    def test_killed_coordinator_stops_native_before_releasing_reviewer_lock(self):
        td = self.root / "guarded"
        td.mkdir()
        lock = self.root / "reviewer.lock"
        marker = self.root / "activity"
        native = f"import time\nf=open({str(marker)!r},'a')\nwhile True:\n f.write('x');f.flush();time.sleep(.04)\n"
        code = ("from pathlib import Path\nfrom council.review_sessions import _lock\n"
                "from council.session_runtime import LOCAL_FIXTURE_ADMISSION,run_native_turn\n"
                f"with _lock(Path({str(lock)!r})) as lease:\n"
                f" run_native_turn('antigravity', [{sys.executable!r}, '-c', {native!r}], "
                f"Path({str(self.root)!r}), Path({str(td)!r}), timeout=10, lease_fd=lease, "
                "admission=LOCAL_FIXTURE_ADMISSION)\n")
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
        parent = subprocess.Popen([sys.executable, "-c", code], env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 4
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(.03)
            self.assertTrue(marker.exists(), "native child never started")
            with self.assertRaises(SessionError):
                with S._lock(lock):
                    pass
            parent.kill()
            parent.wait(timeout=3)
            deadline = time.monotonic() + 4
            acquired = False
            while time.monotonic() < deadline:
                try:
                    with S._lock(lock):
                        acquired = True
                        size = marker.stat().st_size
                        time.sleep(.15)
                        self.assertEqual(marker.stat().st_size, size, "orphan kept executing after lock release")
                    break
                except SessionError:
                    time.sleep(.03)
            self.assertTrue(acquired, "guardian did not release lock after stopping the native process")
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait()
            process_file = td / "process.json"
            if process_file.exists():
                try:
                    os.killpg(json.loads(process_file.read_text())["process_group"], signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_invalid_json_is_preserved_but_not_accepted(self):
        result, td = self.run_child([], suffix="print('not-json', flush=True)\n")
        self.assertEqual(result["status"], "failed")
        self.assertIn("not-json", (td / "stdout.ndjson").read_text())

    def test_cancellation_after_init_preserves_resumable_id(self):
        cancelled = threading.Event()
        def on_event(*_):
            cancelled.set()
        result, _ = self.run_child(agy_events()[:1], suffix="time.sleep(10)\n",
                                   on_event=on_event, cancel=cancelled.is_set)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["conversation_id"], SID)

    def test_closed_stdout_does_not_escape_timeout(self):
        result, _ = self.run_child(agy_events(), suffix="os.close(1)\ntime.sleep(10)\n", timeout=0.3)
        self.assertEqual(result["status"], "timed_out")

    def test_native_resume_arguments_never_use_most_recent_or_restore_code(self):
        prompt = self.root / "prompt.txt"
        prompt.write_text("Review literal `commands` and $(expressions)")
        for runtime in ("antigravity", "grok"):
            args = native_argv(runtime, "native-cli", "exact-model", prompt, SID, 10)
            self.assertIn(SID, args)
            self.assertNotIn("--continue", args)
            self.assertNotIn("--restore-code", args)

    def test_new_uuid_is_never_combined_with_resume_or_fork(self):
        prompt = self.root / "prompt.txt"
        prompt.write_text("Review the current task")
        args = native_argv("grok", "native-cli", "exact-model", prompt, None, 10, planned_id=SID)
        self.assertEqual(args[-2:], ["--session-id", SID])
        self.assertNotIn("--resume", args)
        self.assertNotIn("--fork-session", args)
        for runtime, saved, planned in (("grok", SID, OTHER), ("antigravity", None, SID),
                                         ("grok", None, "not-a-uuid")):
            with self.assertRaises(SessionError):
                native_argv(runtime, "native-cli", "model", prompt, saved, 10, planned_id=planned)


class TestFrozenReviewSession(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = patch.dict(os.environ, {"GROK_HOME": str(self.root / "native-grok")})
        env.start()
        self.addCleanup(env.stop)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        (self.repo / "price.py").write_text("def total(x):\n    return x - 1\n")
        (self.repo / ".gitignore").write_text("ignored\n")
        self.git("add", ".")
        self.git("-c", "user.name=Review Fixture", "-c", "user.email=fixture@example.invalid",
                 "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "commit", "-qm", "fixture")
        self.out = self.root / "session"
        self.task = "R1: total returns the input unchanged. Verify with a focused check."

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, capture_output=True, check=True)

    def prepare(self):
        result = S.prepare(self.repo, self.task, self.out, {"gemini": "candidate-gemini", "grok": "candidate-grok"},
                           {"gemini": sys.executable, "grok": sys.executable})
        for name in ("gemini", "grok"):
            S._save(self.out / "reviewers" / name / "preflight.json",
                    {"ready": True, "requested_model": f"candidate-{name}",
                     "capabilities": {"new_session_uuid": name == "grok"},
                     "binary": S._binary_signature(sys.executable),
                     "checks": {"version": {"version": "fixture"}}})
        return result

    @staticmethod
    def fake_runner(runtime, argv, workspace, td, *, expected_id, on_event, **kwargs):
        sid = expected_id or kwargs.get("planned_id") or (SID if runtime == "antigravity" else OTHER)
        stream = NativeStream(runtime, expected_id=expected_id or kwargs.get("planned_id"))
        events = agy_events(sid) if runtime == "antigravity" else grok_events(sid)
        for event in events:
            stream.feed(event)
            on_event(event, stream)
        (td / "response.md").write_text("A: evidence tied to the current snapshot")
        return {"status": "completed", "report_received": True, "conversation_id": sid,
                "reported_model": None, "claims_verified": False, "task_accepted": False}

    def test_snapshot_includes_dirty_untracked_and_deletion_not_ignored(self):
        (self.repo / "new.py").write_text("value = 2\n")
        (self.repo / "price.py").unlink()
        (self.repo / "ignored").write_text("not input")
        self.prepare()
        source = self.out / "snapshots/0001/source"
        self.assertTrue((source / "new.py").exists())
        self.assertFalse((source / "price.py").exists())
        self.assertFalse((source / "ignored").exists())

    def test_workspaces_are_independent_and_share_snapshot_identity(self):
        self.prepare()
        a = self.out / "reviewers/gemini/workspace/price.py"
        b = self.out / "reviewers/grok/workspace/price.py"
        a.write_text("changed by reviewer")
        self.assertNotEqual(a.read_text(), b.read_text())
        self.assertEqual(b.read_text(), (self.repo / "price.py").read_text())

    def test_session_survives_reload_and_followup_uses_exact_id(self):
        self.prepare()
        first = fixture_review(self.out, "gemini", "initial", "review", 3, runner=self.fake_runner)
        seen = []
        def resumed(*args, **kwargs):
            seen.append(kwargs["expected_id"])
            return self.fake_runner(*args, **kwargs)
        second = fixture_review(self.out, "gemini", "follow_up", "test the counterexample", 3, runner=resumed)
        self.assertEqual(seen, [SID])
        self.assertEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertEqual(second["turn"], 2)
        self.assertTrue((self.out / "reviewers/gemini/turns/0001/result.json").exists())

    def test_followup_without_handle_fails_before_execution(self):
        self.prepare()
        with self.assertRaisesRegex(SessionError, "no saved conversation"):
            fixture_review(self.out, "gemini", "follow_up", "check", 3, runner=self.fake_runner)

    def test_recheck_retains_history_but_advances_snapshot_and_keeps_cwd(self):
        old = self.prepare()
        for name in ("gemini", "grok"):
            fixture_review(self.out, name, "initial", "review", 3, runner=self.fake_runner)
        grok_id = S._load(self.out / "reviewers/grok/reviewer.json")["conversation_id"]
        (self.repo / "price.py").write_text("def total(x):\n    return x\n")
        seen = []
        def panel(out, kind, message, timeout, **kwargs):
            rows = {}
            for name in ("gemini", "grok"):
                def runner(runtime, argv, workspace, td, **kw):
                    seen.append((workspace, kw["expected_id"], (workspace / "price.py").read_text()))
                    return self.fake_runner(runtime, argv, workspace, td, **kw)
                rows[name] = fixture_review(out, name, kind, message, timeout, runner=runner)
            return rows
        result = fixture_recheck(self.out, self.repo, "verify the previous finding", 3, panel=panel)
        self.assertNotEqual(result["gemini"]["snapshot_id"], old["snapshot_id"])
        self.assertEqual([x[1] for x in seen], [SID, grok_id])
        for name, (workspace, _, code) in zip(("gemini", "grok"), seen):
            self.assertEqual(workspace, self.out / "reviewers" / name / "workspace")
            self.assertIn("return x\n", code)
            self.assertEqual(len(list((self.out / "reviewers" / name / "archives").iterdir())), 1)
        self.assertIn("x - 1", (self.out / "snapshots/0001/source/price.py").read_text())

    def test_recheck_does_not_touch_workspaces_while_a_guardian_holds_any_lease(self):
        self.prepare()
        (self.repo / "price.py").write_text("new revision")
        with S._lock(self.out / "reviewers/grok/reviewer.lock"):
            with self.assertRaisesRegex(SessionError, "busy"):
                fixture_recheck(self.out, self.repo, "check", 3, panel=lambda *_, **__: {})
        self.assertEqual(len(list((self.out / "snapshots").iterdir())), 1)
        self.assertFalse((self.out / "reviewers/gemini/archives").exists())
        self.assertIn("x - 1", (self.out / "reviewers/grok/workspace/price.py").read_text())

    def test_source_modification_by_reviewer_invalidates_scope(self):
        self.prepare()
        def modifying(runtime, argv, workspace, td, **kwargs):
            (workspace / "price.py").write_text("silently repaired")
            return self.fake_runner(runtime, argv, workspace, td, **kwargs)
        result = fixture_review(self.out, "gemini", "initial", "review", 3, runner=modifying)
        self.assertEqual(result["status"], "workspace_changed")
        self.assertFalse(result["report_received"])
        with self.assertRaisesRegex(SessionError, "workspace changed"):
            fixture_review(self.out, "gemini", "follow_up", "check", 3, runner=self.fake_runner)

    def test_changed_task_or_packet_cannot_reuse_old_acceptance_scope(self):
        self.prepare()
        (self.out / "reviewers/gemini/workspace/.review-input/task.md").write_text("accept everything")
        with self.assertRaisesRegex(SessionError, "input packet changed"):
            fixture_review(self.out, "gemini", "initial", "review", 3, runner=self.fake_runner)
        (self.out / "task.md").write_text("different requirements")
        with self.assertRaisesRegex(SessionError, "task criteria changed"):
            fixture_recheck(self.out, self.repo, "check", 3, panel=lambda *_, **__: {})

    def test_added_pytest_hook_invalidates_snapshot(self):
        self.prepare()
        def modifying(runtime, argv, workspace, td, **kwargs):
            (workspace / "conftest.py").write_text("# changes test collection\n")
            return self.fake_runner(runtime, argv, workspace, td, **kwargs)
        result = fixture_review(self.out, "gemini", "initial", "review", 3, runner=modifying)
        self.assertEqual(result["status"], "workspace_changed")
        self.assertIn("conftest.py", result["workspace_source_changes"])

    def test_oversized_prompt_does_not_poison_later_attempts(self):
        self.prepare()
        with self.assertRaisesRegex(SessionError, "argv budget"):
            fixture_review(self.out, "gemini", "initial", "x" * 81000, 3, runner=self.fake_runner)
        result = fixture_review(self.out, "gemini", "initial", "short request", 3, runner=self.fake_runner)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["turn"], 2)

    def test_model_change_requires_new_preflight(self):
        self.prepare()
        p = self.out / "reviewers/gemini/reviewer.json"
        reviewer = S._load(p)
        reviewer["requested_model"] = "different-model"
        S._save(p, reviewer)
        with self.assertRaisesRegex(SessionError, "rerun preflight"):
            fixture_review(self.out, "gemini", "initial", "review", 3, runner=self.fake_runner)

    def test_successful_help_on_stderr_is_accepted(self):
        self.prepare()
        def probe(argv, **kwargs):
            if "--help" in argv:
                return subprocess.CompletedProcess(argv, 0, b"", b"--output-format --conversation")
            return subprocess.CompletedProcess(argv, 0, b"candidate-gemini", b"")
        with patch.object(S.subprocess, "run", side_effect=probe):
            result = S.preflight(self.out, "gemini")
        self.assertTrue(result["ready"])

    @staticmethod
    def no_id_runner(runtime, argv, workspace, td, **kwargs):
        planned = kwargs["planned_id"]
        persisted = S._load(td / "turn.json")
        assert persisted["planned_conversation_id"] == planned
        assert argv[-2:] == ["--session-id", planned]
        (td / "response.md").write_text("partial text is not a report")
        return {"status": "timed_out", "report_received": False, "conversation_id": None,
                "process_cleanup": {"status": "stopped"}, "claims_verified": False,
                "task_accepted": False}

    def persisted_no_id_runner(self, runtime, argv, workspace, td, *, suffix="", **kwargs):
        sid = kwargs["planned_id"]
        native = Path(os.environ["GROK_HOME"]) / "sessions" / quote(str(workspace.resolve()), safe="") / sid
        native.mkdir(parents=True)
        (native / "summary.json").write_text(json.dumps({"chat_format_version": 1,
            "info": {"id": sid, "cwd": str(workspace.resolve())}}))
        query = (td / "prompt.txt").read_text().strip() + suffix
        (native / "chat_history.jsonl").write_text(json.dumps({"type": "user", "prompt_index": 0,
            "content": [{"type": "text", "text": "<user_query>\n" + query + "\n</user_query>"}]}) + "\n")
        return self.no_id_runner(runtime, argv, workspace, td, **kwargs)

    def test_timeout_auto_recovers_exact_handle_but_keeps_turn_incomplete(self):
        self.prepare()
        with patch.object(S.subprocess, "run") as no_export:
            first = fixture_review(self.out, "grok", "initial", "review", 3, runner=self.persisted_no_id_runner)
            no_export.assert_not_called()
        self.assertEqual(first["status"], "timed_out")
        self.assertFalse(first["report_received"])
        self.assertEqual(first["conversation_id"], first["planned_conversation_id"])
        self.assertEqual(first["identity_recovery"]["status"], "confirmed")
        old_result = (self.out / "reviewers/grok/turns/0001/result.json").read_bytes()
        second = fixture_review(self.out, "grok", "follow_up", "continue", 3, runner=self.fake_runner)
        self.assertEqual(second["requested_conversation_id"], first["conversation_id"])
        self.assertIsNone(second["planned_conversation_id"])
        self.assertEqual(old_result, (self.out / "reviewers/grok/turns/0001/result.json").read_bytes())

    def test_missing_export_cannot_become_a_fresh_or_resumed_conversation(self):
        self.prepare()
        with patch.object(S.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)):
            first = fixture_review(self.out, "grok", "initial", "review", 3, runner=self.no_id_runner)
            self.assertIsNone(first["conversation_id"])
            for kind in ("initial", "follow_up"):
                runner = Mock()
                with self.assertRaisesRegex(SessionError, "planned conversation is not verified"):
                    fixture_review(self.out, "grok", kind, "retry", 3, runner=runner)
                runner.assert_not_called()
        self.assertEqual(len(list((self.out / "reviewers/grok/turns").iterdir())), 1)

    def test_another_prompt_cannot_recover_identity_even_with_original_prefix(self):
        self.prepare()
        def wrong(*args, **kwargs):
            return self.persisted_no_id_runner(*args, suffix="\n\n## Assistant\n\nAdditional instruction", **kwargs)
        first = fixture_review(self.out, "grok", "initial", "review", 3, runner=wrong)
        self.assertIsNone(first["conversation_id"])
        self.assertFalse(first["identity_recovery"]["exact_initial_prompt_match"])

    def test_recovery_failure_cannot_erase_the_native_attempt_result(self):
        self.prepare()
        def real_timeout(runtime, argv, workspace, td, **kwargs):
            self.persisted_no_id_runner(runtime, argv, workspace, td, **kwargs)
            return run_native_turn(runtime, [sys.executable, "-c", "import time; time.sleep(30)"],
                                   workspace, td, timeout=0.3, planned_id=kwargs["planned_id"],
                                   admission=LOCAL_FIXTURE_ADMISSION)
        with patch.object(S, "_recover_grok_identity", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                fixture_review(self.out, "grok", "initial", "review", 3, runner=real_timeout)
        td = self.out / "reviewers/grok/turns/0001"
        original = (td / "native-result.json").read_bytes()
        first = S._load(td / "result.json")
        self.assertEqual(first["status"], "timed_out")
        self.assertEqual(first["process_cleanup"]["status"], "stopped")
        self.assertIsNone(first["conversation_id"])
        second = fixture_review(self.out, "grok", "follow_up", "continue", 3, runner=self.fake_runner)
        self.assertEqual(second["requested_conversation_id"], first["planned_conversation_id"])
        self.assertEqual(original, (td / "native-result.json").read_bytes())
        self.assertEqual(first, S._load(td / "result.json"))

    def test_native_summary_scope_must_match_uuid_cwd_and_format(self):
        self.prepare()
        def wrong_scope(*args, **kwargs):
            result = self.persisted_no_id_runner(*args, **kwargs)
            workspace = args[2]
            native = Path(os.environ["GROK_HOME"]) / "sessions" / quote(str(workspace.resolve()), safe="") / kwargs["planned_id"]
            (native / "summary.json").write_text(json.dumps({"chat_format_version": 1,
                "info": {"id": OTHER, "cwd": str(workspace.resolve())}}))
            return result
        first = fixture_review(self.out, "grok", "initial", "review", 3, runner=wrong_scope)
        self.assertIsNone(first["conversation_id"])
        self.assertFalse(first["identity_recovery"]["native_scope_matches"])
        rd = self.out / "reviewers/grok"
        reviewer = S._load(rd / "reviewer.json")
        workspace = str((rd / "workspace").resolve())
        native = Path(os.environ["GROK_HOME"]) / "sessions" / quote(workspace, safe="") / reviewer["planned_conversation_id"]
        with S._lock(rd / "reviewer.lock") as fd:
            for version, cwd in ((2, workspace), (True, workspace), ("1", workspace), (1, workspace + "-other")):
                with self.subTest(version=version, cwd=cwd):
                    (native / "summary.json").write_text(json.dumps({"chat_format_version": version,
                        "info": {"id": reviewer["planned_conversation_id"], "cwd": cwd}}))
                    self.assertEqual(S._recover_grok_identity(rd, reviewer, fd)["status"], "unverified")

    def test_boolean_prompt_index_is_not_the_first_user_request(self):
        self.prepare()
        def wrong_index(*args, **kwargs):
            result = self.persisted_no_id_runner(*args, **kwargs)
            native = Path(os.environ["GROK_HOME"]) / "sessions" / quote(str(args[2].resolve()), safe="") / kwargs["planned_id"]
            history = native / "chat_history.jsonl"
            row = json.loads(history.read_text())
            row["prompt_index"] = False
            history.write_text(json.dumps(row) + "\n")
            return result
        first = fixture_review(self.out, "grok", "initial", "review", 3, runner=wrong_index)
        self.assertIsNone(first["conversation_id"])
        self.assertFalse(first["identity_recovery"]["exact_initial_prompt_match"])

    def test_native_state_reader_rejects_fifo_symlink_and_oversized_file(self):
        fifo = self.root / "fifo"
        os.mkfifo(fifo)
        ordinary = self.root / "state.json"
        ordinary.write_bytes(b"12345")
        symlink = self.root / "link"
        symlink.symlink_to(ordinary)
        for path, limit in ((fifo, 10), (symlink, 10), (ordinary, 4)):
            with self.assertRaises((OSError, SessionError)):
                S._read_native_state(path, limit)

    def test_changed_recovery_prompt_is_rejected_before_export(self):
        self.prepare()
        with patch.object(S.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)):
            fixture_review(self.out, "grok", "initial", "review", 3, runner=self.no_id_runner)
        (self.out / "reviewers/grok/turns/0001/prompt.txt").write_text("replacement prompt")
        with patch.object(S.subprocess, "run") as export:
            with self.assertRaisesRegex(SessionError, "recovery provenance changed"):
                fixture_review(self.out, "grok", "follow_up", "continue", 3, runner=self.fake_runner)
            export.assert_not_called()

    def test_unresolved_cleanup_skips_identity_export(self):
        self.prepare()
        def failed_cleanup(*args, **kwargs):
            result = self.no_id_runner(*args, **kwargs)
            result.update(status="cleanup_failed", process_cleanup={"status": "failed", "process_group": 123456})
            return result
        with patch.object(S.subprocess, "run") as export:
            first = fixture_review(self.out, "grok", "initial", "review", 3, runner=failed_cleanup)
            export.assert_not_called()
        self.assertIsNone(first["conversation_id"])
        self.assertNotIn("identity_recovery", first)

    def test_interrupted_controller_does_not_display_running_forever(self):
        self.prepare()
        p = self.out / "reviewers/gemini/reviewer.json"
        reviewer = S._load(p)
        reviewer.update(status="running", active_turn="turns/0001")
        S._save(p, reviewer)
        self.assertEqual(S.status(self.out)["reviewers"]["gemini"]["status"], "interrupted")

    def seed_cleanup_failure(self, name="gemini"):
        def failing(*args, **kwargs):
            result = self.fake_runner(*args, **kwargs)
            result.update(status="cleanup_failed", report_received=False, failure_status="timed_out",
                          process_cleanup={"status": "failed", "process_group": 123456,
                                           "error_type": "PermissionError"})
            return result
        return fixture_review(self.out, name, "initial", "review", 3, runner=failing)

    def test_cleanup_failure_is_durable_and_blocks_followup_while_group_exists(self):
        self.prepare()
        self.seed_cleanup_failure()
        rd = self.out / "reviewers/gemini"
        result = S._load(rd / "turns/0001/result.json")
        self.assertEqual(result["status"], "cleanup_failed")
        self.assertIsNone(result["workspace_source_changes"])
        self.assertEqual(S.status(self.out)["reviewers"]["gemini"]["status"], "cleanup_failed")
        with patch.object(S.subprocess, "run", return_value=Mock(returncode=0, stdout="123456\n")):
            with self.assertRaisesRegex(SessionError, "missing or ambiguous"):
                fixture_review(self.out, "gemini", "follow_up", "continue", 3, runner=self.fake_runner)
        self.assertEqual(S._load(rd / "reviewer.json")["turn_count"], 1)
        self.assertFalse((rd / "turns/0002").exists())

    def test_failed_inventory_blocks_recheck_before_any_workspace_changes(self):
        self.prepare()
        self.seed_cleanup_failure("grok")
        (self.repo / "price.py").write_text("new revision")
        failures = [Mock(returncode=1, stdout=""), OSError("inventory unavailable"),
                    subprocess.TimeoutExpired(["ps"], 5)]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                options = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
                with patch.object(S.subprocess, "run", **options):
                    with self.assertRaises(SessionError):
                        fixture_recheck(self.out, self.repo, "check", 3, panel=lambda *_, **__: {})
                self.assertEqual(len(list((self.out / "snapshots").iterdir())), 1)
                for name in ("gemini", "grok"):
                    rd = self.out / "reviewers" / name
                    self.assertIn("x - 1", (rd / "workspace/price.py").read_text())
                    self.assertFalse((rd / "archives").exists())

    def test_numeric_group_absence_cannot_resolve_unverified_cleanup(self):
        self.prepare()
        self.seed_cleanup_failure()
        rd = self.out / "reviewers/gemini"
        before = (rd / "turns/0001/result.json").read_bytes()
        with patch.object(S.subprocess, "run", return_value=Mock(returncode=0, stdout="12\n34\n")):
            with self.assertRaisesRegex(SessionError, "missing or ambiguous"):
                fixture_review(self.out, "gemini", "follow_up", "continue", 3, runner=self.fake_runner)
        self.assertEqual((rd / "turns/0001/result.json").read_bytes(), before)
        self.assertFalse((rd / "turns/0001/cleanup-resolution.json").exists())

    def test_interrupted_final_state_write_cannot_bypass_cleanup_guard(self):
        self.prepare()
        rd = self.out / "reviewers/gemini"
        save = S._save
        def crash_on_final_reviewer_state(path, value):
            if path == rd / "reviewer.json" and value.get("status") == "cleanup_failed":
                raise OSError("simulated final state write interruption")
            save(path, value)
        with patch.object(S, "_save", side_effect=crash_on_final_reviewer_state):
            with self.assertRaises(OSError):
                self.seed_cleanup_failure()
        self.assertEqual(S._load(rd / "reviewer.json")["status"], "running")
        self.assertEqual(S._load(rd / "turns/0001/result.json")["status"], "cleanup_failed")
        with patch.object(S.subprocess, "run", return_value=Mock(returncode=0, stdout="123456\n")):
            with self.assertRaisesRegex(SessionError, "reference is missing or unsafe"):
                fixture_review(self.out, "gemini", "follow_up", "continue", 3, runner=self.fake_runner)
            with self.assertRaisesRegex(SessionError, "reference is missing or unsafe"):
                fixture_recheck(self.out, self.repo, "check", 3, panel=lambda *_, **__: {})
        self.assertFalse((rd / "turns/0002").exists())
        self.assertEqual(len(list((self.out / "snapshots").iterdir())), 1)

    def test_stale_active_process_receipt_blocks_without_a_result_file(self):
        self.prepare()
        rd = self.out / "reviewers/gemini"
        td = rd / "turns/0001"
        td.mkdir(parents=True)
        S._save(td / "process.json", {"process_group": 123456})
        reviewer = S._load(rd / "reviewer.json")
        reviewer.update(status="running", active_turn="turns/0001", turn_count=1,
                        conversation_id=SID)
        S._save(rd / "reviewer.json", reviewer)
        with patch.object(S.subprocess, "run", return_value=Mock(returncode=0, stdout="123456\n")):
            with self.assertRaisesRegex(SessionError, "reference is missing or unsafe"):
                fixture_review(self.out, "gemini", "follow_up", "continue", 3, runner=self.fake_runner)
        with patch.object(S.subprocess, "run", return_value=Mock(returncode=0, stdout="12\n34\n")):
            with self.assertRaisesRegex(SessionError, "reference is missing or unsafe"):
                fixture_review(self.out, "gemini", "follow_up", "continue", 3, runner=self.fake_runner)
        self.assertFalse((td / "cleanup-resolution.json").exists())
        self.assertFalse((td / "result.json").exists())

    def test_recheck_does_not_replace_workspace_from_numeric_absence_only(self):
        self.prepare()
        self.seed_cleanup_failure("grok")
        rd = self.out / "reviewers/grok"
        (rd / "workspace/price.py").write_text("changed before cleanup completed")
        (self.repo / "price.py").write_text("new revision")
        native_run = subprocess.run
        def inventory_or_run(argv, **kwargs):
            if argv == ["ps", "-axo", "pgid="]:
                return Mock(returncode=0, stdout="12\n34\n")
            return native_run(argv, **kwargs)
        with patch.object(S.subprocess, "run", side_effect=inventory_or_run):
            with self.assertRaisesRegex(SessionError, "missing or ambiguous"):
                fixture_recheck(self.out, self.repo, "check", 3, panel=lambda *_, **__: {})
        self.assertEqual(len(list((self.out / "snapshots").iterdir())), 1)
        self.assertEqual((rd / "workspace/price.py").read_text(), "changed before cleanup completed")
        self.assertEqual(S._load(rd / "turns/0001/result.json")["status"], "cleanup_failed")

    def test_cancellation_targets_only_the_active_turn(self):
        self.prepare()
        def cancelling(runtime, argv, workspace, td, **kwargs):
            S.cancel_turn(self.out, "gemini")
            self.assertTrue(kwargs["cancel"]())
            return {"status": "cancelled", "report_received": False, "conversation_id": None,
                    "claims_verified": False, "task_accepted": False}
        result = fixture_review(self.out, "gemini", "initial", "review", 3, runner=cancelling)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(S.status(self.out)["reviewers"]["grok"]["status"], "prepared")

    def test_output_inside_repository_is_rejected_without_creating_it(self):
        with self.assertRaisesRegex(SessionError, "outside"):
            S.prepare(self.repo, self.task, self.repo / "output", {"gemini": "g", "grok": "x"})
        self.assertFalse((self.repo / "output").exists())

    def test_symlink_cannot_pull_external_files_into_snapshot(self):
        (self.root / "outside.txt").write_text("external")
        (self.repo / "linked").symlink_to(self.root / "outside.txt")
        with self.assertRaisesRegex(SessionError, "regular in-repository"):
            self.prepare()


if __name__ == "__main__":
    unittest.main()
