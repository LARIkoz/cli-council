"""P1A local reliability regressions; no native CLI or network is used."""
from __future__ import annotations

import importlib.util
import base64
import hashlib
import json
import os
from pathlib import Path
import py_compile
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from council import review_sessions as S
from council.session_runtime import LOCAL_FIXTURE_ADMISSION, NativeStream, SessionError


def fixture_recheck(*args, **kwargs):
    """Exercise recheck transitions without enabling the native runtime."""
    kwargs["admission"] = LOCAL_FIXTURE_ADMISSION
    return S.recheck(*args, **kwargs)


class P1AReviewSessions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        (self.repo / "subject.py").write_text("value = 1\n")
        self.git("add", "subject.py")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "commit", "-qm", "base")
        self.out = self.root / "session"

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True)

    def prepare(self):
        S.prepare(self.repo, "R1", self.out, {"gemini": "g", "grok": "x"},
                  {"gemini": sys.executable, "grok": sys.executable})
        for name, model in (("gemini", "g"), ("grok", "x")):
            S._save(self.out / "reviewers" / name / "preflight.json", {
                "ready": True, "requested_model": model,
                "capabilities": {"new_session_uuid": name == "grok"},
                "binary": S._binary_signature(sys.executable), "checks": {"version": {"version": "fixture"}},
            })

    @staticmethod
    def change_rows(path):
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        assert rows[0]["format"] == "council-captured-change-v1"
        return rows[1:]

    @staticmethod
    def runner(runtime, argv, workspace, td, *, expected_id, on_event, **kwargs):
        sid = expected_id or kwargs.get("planned_id") or "11111111-1111-4111-8111-111111111111"
        stream = NativeStream(runtime, expected_id=expected_id or kwargs.get("planned_id"))
        event = {"event": "init", "conversation_id": sid, "init": {"model": "g"}}
        stream.feed(event); on_event(event, stream)
        (td / "response.md").write_text("report")
        return {"status": "completed", "report_received": True, "conversation_id": sid,
                "claims_verified": False, "task_accepted": False}

    def test_freeze_diff_is_derived_from_captured_bytes_not_live_git_diff(self):
        original = S._git
        def changing(repo, *args):
            if args and args[0] == "diff":
                (repo / "subject.py").write_text("value = 2\n")
                try:
                    return original(repo, *args)
                finally:
                    (repo / "subject.py").write_text("value = 1\n")
            return original(repo, *args)
        with patch.object(S, "_git", side_effect=changing):
            manifest = S.freeze(self.repo, self.root / "snapshot")
        self.assertEqual((self.root / "snapshot/source/subject.py").read_text(), "value = 1\n")
        self.assertEqual(self.change_rows(self.root / "snapshot/change.capture.jsonl"), [])
        self.assertIn("baseline", manifest)

    def test_capture_covers_staged_unstaged_untracked_and_deleted_from_one_tree(self):
        (self.repo / "subject.py").write_text("value = 2\n")
        self.git("add", "subject.py")
        (self.repo / "subject.py").write_text("value = 3\n")
        (self.repo / "new.py").write_text("new = 4\n")
        (self.repo / "gone.py").write_text("gone = 1\n")
        self.git("add", "gone.py"); self.git("commit", "-qm", "add gone")
        (self.repo / "gone.py").unlink()
        S.freeze(self.repo, self.root / "mixed")
        source = self.root / "mixed/source"
        self.assertEqual((source / "subject.py").read_text(), "value = 3\n")
        self.assertEqual((source / "new.py").read_text(), "new = 4\n")
        self.assertFalse((source / "gone.py").exists())
        changes = {row["path"]: row for row in self.change_rows(
            self.root / "mixed/change.capture.jsonl")}
        self.assertEqual(base64.b64decode(changes["subject.py"]["new"]["data_base64"]), b"value = 3\n")
        self.assertEqual(base64.b64decode(changes["new.py"]["new"]["data_base64"]), b"new = 4\n")
        self.assertEqual(base64.b64decode(changes["gone.py"]["old"]["data_base64"]), b"gone = 1\n")
        self.assertEqual(changes["new.py"]["operation"], "add")
        self.assertEqual(changes["gone.py"]["operation"], "delete")

    def test_captured_change_round_trips_empty_binary_mode_and_no_final_newline(self):
        def entry(data, executable=False):
            return {"data": data, "sha256": hashlib.sha256(data).hexdigest(),
                    "bytes": len(data), "executable": executable}

        baseline = {
            "deleted-empty": entry(b""),
            "text": entry(b"value = 1"),
            "binary": entry(b"\x00old\xff"),
            "mode-only": entry(b"same\n", False),
        }
        current = {
            "added-empty": entry(b""),
            "text": entry(b"value = 2"),
            "binary": entry(b"\x00new\xfe"),
            "mode-only": entry(b"same\n", True),
        }
        packet = S._captured_diff(baseline, current)
        lines = [json.loads(line) for line in packet.decode().splitlines()]
        self.assertEqual(lines[0]["semantics"], "ordered complete old/new records; not a unified patch")
        rows = {row["path"]: row for row in lines[1:]}
        self.assertEqual(rows["added-empty"]["operation"], "add")
        self.assertEqual(rows["deleted-empty"]["operation"], "delete")
        self.assertEqual(base64.b64decode(rows["text"]["old"]["data_base64"]), b"value = 1")
        self.assertEqual(base64.b64decode(rows["text"]["new"]["data_base64"]), b"value = 2")
        self.assertEqual(base64.b64decode(rows["binary"]["old"]["data_base64"]), b"\x00old\xff")
        self.assertEqual(base64.b64decode(rows["binary"]["new"]["data_base64"]), b"\x00new\xfe")
        self.assertFalse(rows["mode-only"]["old"]["executable"])
        self.assertTrue(rows["mode-only"]["new"]["executable"])

        reconstructed = dict(baseline)
        for row in lines[1:]:
            if row["new"] is None:
                reconstructed.pop(row["path"])
            else:
                side = row["new"]
                data = base64.b64decode(side["data_base64"])
                reconstructed[row["path"]] = entry(data, side["executable"])
        self.assertEqual(reconstructed, current)

        preview = S._captured_preview(baseline, current).decode("utf-8")
        self.assertIn("Readable aid only", preview)
        self.assertIn("=== add added-empty ===", preview)
        self.assertIn("empty-file add/delete", preview)
        self.assertIn("=== delete deleted-empty ===", preview)
        self.assertIn("--- a/text", preview)
        self.assertIn("-value = 1\n\\ No newline at end of file", preview)
        self.assertIn("+value = 2\n\\ No newline at end of file", preview)
        self.assertIn("binary content", preview)
        self.assertIn("old: mode=100644", preview)
        self.assertIn("new: mode=100755", preview)
        self.assertIn("file content unchanged; operation/mode metadata", preview)

    def test_prepared_reviewer_packet_carries_readable_and_lossless_change_views(self):
        (self.repo / "subject.py").write_text("value = 2")
        self.prepare()
        snapshot = self.out / "snapshots/0001"
        manifest = S._load(snapshot / "manifest.json")
        capture = snapshot / "change.capture.jsonl"
        preview = snapshot / "change.preview.txt"
        self.assertEqual(hashlib.sha256(capture.read_bytes()).hexdigest(),
                         manifest["capture"]["change_capture_sha256"])
        self.assertEqual(hashlib.sha256(preview.read_bytes()).hexdigest(),
                         manifest["capture"]["change_preview_sha256"])
        for name in ("gemini", "grok"):
            packet = self.out / f"reviewers/{name}/workspace/.review-input"
            self.assertEqual((packet / capture.name).read_bytes(), capture.read_bytes())
            self.assertEqual((packet / preview.name).read_bytes(), preview.read_bytes())
        prompt = S._prompt(S._load(self.out / "session.json"), "initial", "review",
                           self.out / "reviewers/gemini/workspace", 3)
        self.assertIn("change.preview.txt first", prompt)
        self.assertIn("change.capture.jsonl", prompt)

    def test_head_or_index_motion_during_capture_fails_instead_of_minting_packet(self):
        original = S._copy_captured
        def moves_index(repo, files, destination):
            original(repo, files, destination)
            (repo / "subject.py").write_text("value = staged\n")
            self.git("add", "subject.py")
        with patch.object(S, "_copy_captured", side_effect=moves_index):
            with self.assertRaisesRegex(SessionError, "checkout, index or source changed"):
                S.freeze(self.repo, self.root / "moved")
        self.assertFalse((self.root / "moved").exists())

    def test_baseline_reads_are_pinned_to_one_commit_not_symbolic_head(self):
        head = self.git("rev-parse", "HEAD").stdout.decode().strip()
        calls = []
        original = S._git

        def observe(repo, *args):
            if args and args[0] in ("ls-tree", "show"):
                calls.append(args)
            return original(repo, *args)

        with patch.object(S, "_git", side_effect=observe):
            manifest = S.freeze(self.repo, self.root / "pinned")
        self.assertEqual(manifest["baseline"]["head"], head)
        self.assertTrue(calls)
        self.assertTrue(all("HEAD" not in args for args in calls))
        self.assertTrue(all(any(head == part or str(part).startswith(head + ":") for part in args)
                            for args in calls))

    def test_snapshot_identity_binds_same_current_bytes_to_changed_baseline_and_diff(self):
        first = S.freeze(self.repo, self.root / "first")
        (self.repo / "subject.py").write_text("value = 2\n")
        self.git("add", "subject.py")
        self.git("commit", "-qm", "new baseline")
        (self.repo / "subject.py").write_text("value = 1\n")
        second = S.freeze(self.repo, self.root / "second")
        self.assertEqual(first["capture"]["current_id"], second["capture"]["current_id"])
        self.assertNotEqual(first["baseline"]["id"], second["baseline"]["id"])
        self.assertNotEqual(first["capture"]["change_capture_sha256"],
                            second["capture"]["change_capture_sha256"])
        self.assertNotEqual(first["id"], second["id"])

    def test_recheck_copy_failure_is_pending_and_blocks_all_dispatch(self):
        self.prepare()
        (self.repo / "subject.py").write_text("value = 2\n")
        original = S._copy_workspace
        calls = []
        def failing(snapshot, rd, task, **kwargs):
            calls.append(rd.name)
            if len(calls) == 2:
                raise OSError("copy fails")
            return original(snapshot, rd, task, **kwargs)
        with patch.object(S, "_copy_workspace", side_effect=failing):
            with self.assertRaises(OSError):
                fixture_recheck(self.out, self.repo, "again", 3, panel=lambda *a, **kw: {})
        state = S.status(self.out)
        self.assertEqual(state["transition_state"], "pending")
        for name in ("gemini", "grok"):
            self.assertFalse(state["reviewers"][name]["integrity_verified"])
            with self.assertRaisesRegex(SessionError, "generation transition"):
                S.run_reviewer(self.out, name, "initial", "no", 3, runner=self.runner,
                               admission=LOCAL_FIXTURE_ADMISSION)

    def test_activation_failure_recovers_old_stable_generation_without_erasing_stage(self):
        self.prepare()
        old = (self.out / "reviewers/gemini/workspace/subject.py").read_text()
        (self.repo / "subject.py").write_text("value = 2\n")
        original = S._activate_workspace
        def fail_second(rd, transition, name):
            if name == "grok":
                raise OSError("rename failed")
            return original(rd, transition, name)
        with patch.object(S, "_activate_workspace", side_effect=fail_second):
            with self.assertRaises(OSError):
                fixture_recheck(self.out, self.repo, "again", 3, panel=lambda *a, **kw: {})
        self.assertEqual(S.status(self.out)["transition_state"], "pending")
        restored = S.recover_generation(self.out)
        self.assertEqual(restored["transition_state"], "recovered")
        for name in ("gemini", "grok"):
            self.assertEqual((self.out / f"reviewers/{name}/workspace/subject.py").read_text(), old)
            self.assertTrue(restored["reviewers"][name]["integrity_verified"])
        self.assertTrue(list((self.out / "reviewers/gemini/recovery").rglob("subject.py")))

    def test_recovery_inferrs_deterministic_archive_after_crash_before_journal_row_save(self):
        self.prepare()
        session = S._load(self.out / "session.json")
        transition_id = "crash-between-rename-and-save"
        rd = self.out / "reviewers/gemini"
        archive = rd / "archives" / transition_id
        archive.parent.mkdir()
        (rd / "workspace").rename(archive)  # simulated crash: row.old_archive was never saved
        session["generation_transition"] = {
            "id": transition_id, "state": "activating", "previous_snapshot": session["snapshot_id"],
            "target_snapshot": "other", "reviewers": {
                "gemini": {"staged_workspace": str(rd / "staged" / transition_id / "workspace"), "activated": False},
                "grok": {"staged_workspace": str(self.out / "reviewers/grok/staged" / transition_id / "workspace"), "activated": False},
            },
        }
        S._save(self.out / "session.json", session)
        restored = S.recover_generation(self.out)
        self.assertEqual(restored["transition_state"], "recovered")
        self.assertTrue((rd / "workspace/subject.py").exists())
        self.assertTrue(restored["reviewers"]["gemini"]["integrity_verified"])

    def test_missing_workspace_and_deterministic_archive_is_ambiguous_and_stays_blocked(self):
        self.prepare()
        session = S._load(self.out / "session.json")
        transition_id = "ambiguous"
        (self.out / "reviewers/gemini/workspace").rename(self.out / "lost-workspace")
        session["generation_transition"] = {
            "id": transition_id, "state": "activating", "previous_snapshot": session["snapshot_id"],
            "target_snapshot": "other", "reviewers": {
                name: {"staged_workspace": str(self.out / f"reviewers/{name}/staged/{transition_id}/workspace"), "activated": False}
                for name in ("gemini", "grok")
            },
        }
        S._save(self.out / "session.json", session)
        with self.assertRaisesRegex(SessionError, "ambiguous"):
            S.recover_generation(self.out)
        self.assertEqual(S.status(self.out)["transition_state"], "activating")

    def test_fsync_failure_before_snapshot_publish_leaves_no_destination(self):
        with patch.object(S, "_fsync_file", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                S.freeze(self.repo, self.root / "unsynced")
        self.assertFalse((self.root / "unsynced").exists())

    def test_staged_parent_fsync_failure_never_advances_or_dispatches(self):
        self.prepare()
        (self.repo / "subject.py").write_text("value = 2\n")
        actual_fsync = S._fsync_dir
        dispatched = []

        def fail_staged_parent(path):
            if Path(path).name == "staged":
                raise OSError("staged directory fsync failed")
            return actual_fsync(path)

        with patch.object(S, "_fsync_dir", side_effect=fail_staged_parent):
            with self.assertRaisesRegex(OSError, "staged directory fsync failed"):
                fixture_recheck(
                    self.out, self.repo, "again", 3,
                    panel=lambda *args, **kwargs: dispatched.append((args, kwargs)))
        self.assertEqual(dispatched, [])
        self.assertEqual(S.status(self.out)["transition_state"], "pending")

    def test_commit_save_failure_stays_pending_and_status_separates_report_from_install(self):
        self.prepare()
        old_session = S._load(self.out / "session.json")
        (self.repo / "subject.py").write_text("value = 2\n")
        actual_save = S._save
        def fail_commit(path, value):
            if path == self.out / "session.json" and value.get("generation_transition", {}).get("state") == "committed":
                raise OSError("session save failed")
            return actual_save(path, value)
        with patch.object(S, "_save", side_effect=fail_commit):
            with self.assertRaises(OSError):
                fixture_recheck(self.out, self.repo, "again", 3, panel=lambda *a, **kw: {})
        state = S.status(self.out)
        self.assertEqual(state["transition_state"], "pending")
        self.assertEqual(S._load(self.out / "session.json")["snapshot"], old_session["snapshot"])
        self.assertNotEqual(state["reviewers"]["gemini"]["report_snapshot"],
                            state["reviewers"]["gemini"]["installed_generation"])
        self.assertFalse(state["reviewers"]["gemini"]["integrity_verified"])
        recovered = S.recover_generation(self.out)
        self.assertEqual(recovered["transition_state"], "recovered")
        self.assertEqual(recovered["snapshot_id"], old_session["snapshot_id"])
        self.assertEqual((self.out / "reviewers/gemini/workspace/subject.py").read_text(), "value = 1\n")

    def test_failure_after_final_session_replace_rolls_back_selected_generation(self):
        self.prepare()
        old_session = S._load(self.out / "session.json")
        (self.repo / "subject.py").write_text("value = 2\n")
        actual_save = S._save
        fired = []

        def publish_then_fail(path, value):
            if (path == self.out / "session.json"
                    and value.get("generation_transition", {}).get("state") == "committed"
                    and not fired):
                fired.append(True)
                actual_save(path, value)
                raise OSError("synthetic post-replace acknowledgement failure")
            return actual_save(path, value)

        with patch.object(S, "_save", side_effect=publish_then_fail):
            with self.assertRaisesRegex(OSError, "post-replace"):
                fixture_recheck(self.out, self.repo, "again", 3, panel=lambda *a, **kw: {})
        failed = S._load(self.out / "session.json")
        self.assertEqual(failed["generation_transition"]["state"], "pending")
        self.assertEqual(failed["snapshot"], old_session["snapshot"])
        recovered = S.recover_generation(self.out)
        self.assertEqual(recovered["snapshot_id"], old_session["snapshot_id"])
        self.assertEqual(recovered["transition_state"], "recovered")

    def test_post_replace_and_rollback_save_failure_is_blocked_by_durable_marker(self):
        self.prepare()
        old_session = S._load(self.out / "session.json")
        (self.repo / "subject.py").write_text("value = 2\n")
        actual_save = S._save
        failures = []

        def fail_commit_and_rollback(path, value):
            if path == self.out / "session.json" and len(failures) < 2:
                state = value.get("generation_transition", {}).get("state")
                if state == "committed" and not failures:
                    actual_save(path, value)
                    failures.append("committed_after_replace")
                    raise OSError("commit acknowledgement failed")
                if state == "pending" and failures == ["committed_after_replace"]:
                    failures.append("rollback_save_failed")
                    raise OSError("rollback save failed")
            return actual_save(path, value)

        with patch.object(S, "_save", side_effect=fail_commit_and_rollback):
            with self.assertRaisesRegex(OSError, "commit acknowledgement"):
                fixture_recheck(self.out, self.repo, "again", 3, panel=lambda *a, **kw: {})
        self.assertEqual(failures, ["committed_after_replace", "rollback_save_failed"])
        self.assertEqual(S._load(self.out / "session.json")["generation_transition"]["state"], "committed")
        self.assertTrue((self.out / "generation-transition.pending.json").exists())
        self.assertEqual(S.status(self.out)["transition_state"], "pending")
        with self.assertRaisesRegex(SessionError, "generation transition"):
            S.run_reviewer(self.out, "gemini", "initial", "no", 3, runner=self.runner,
                           admission=LOCAL_FIXTURE_ADMISSION)
        marker_before = (self.out / "generation-transition.pending.json").read_bytes()
        with self.assertRaisesRegex(SessionError, "durable pending marker"):
            fixture_recheck(self.out, self.repo, "new attempt", 3, panel=lambda *a, **kw: {})
        self.assertEqual((self.out / "generation-transition.pending.json").read_bytes(), marker_before)
        recovered = S.recover_generation(self.out)
        self.assertEqual(recovered["snapshot_id"], old_session["snapshot_id"])
        self.assertEqual(recovered["transition_state"], "recovered")
        self.assertFalse((self.out / "generation-transition.pending.json").exists())

    def test_recovery_is_repeatable_after_its_own_partial_save_failure(self):
        self.prepare()
        old_session = S._load(self.out / "session.json")
        (self.repo / "subject.py").write_text("value = 2\n")
        original_copy = S._copy_workspace
        copied = []

        def fail_second_copy(snapshot, rd, task, **kwargs):
            copied.append(rd.name)
            if len(copied) == 2:
                raise OSError("copy fails")
            return original_copy(snapshot, rd, task, **kwargs)

        with patch.object(S, "_copy_workspace", side_effect=fail_second_copy):
            with self.assertRaises(OSError):
                fixture_recheck(self.out, self.repo, "again", 3, panel=lambda *a, **kw: {})
        actual_save = S._save
        fired = []

        def fail_first_reviewer_receipt(path, value):
            if (path.name == "reviewer.json" and value.get("installed_generation") == old_session["snapshot_id"]
                    and not fired):
                fired.append(True)
                raise OSError("recovery reviewer receipt failure")
            return actual_save(path, value)

        with patch.object(S, "_save", side_effect=fail_first_reviewer_receipt):
            with self.assertRaisesRegex(OSError, "recovery reviewer"):
                S.recover_generation(self.out)
        recovered = S.recover_generation(self.out)
        self.assertEqual(recovered["snapshot_id"], old_session["snapshot_id"])
        self.assertEqual(recovered["transition_state"], "recovered")
        self.assertEqual(S.recover_generation(self.out)["transition_state"], "recovered")

    def test_executable_cache_or_shadow_output_invalidates_receipt(self):
        self.prepare()
        def cached(runtime, argv, workspace, td, **kwargs):
            source = self.root / "other.py"; source.write_text("value = 9\n")
            target = Path(importlib.util.cache_from_source(str(workspace / "subject.py")))
            target.parent.mkdir(exist_ok=True)
            py_compile.compile(str(source), cfile=str(target), doraise=True,
                               invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
            return self.runner(runtime, argv, workspace, td, **kwargs)
        result = S.run_reviewer(self.out, "gemini", "initial", "review", 3, runner=cached,
                                admission=LOCAL_FIXTURE_ADMISSION)
        self.assertEqual(result["status"], "workspace_changed")
        self.assertFalse(result["report_received"])
        self.assertIn("__pycache__", "\n".join(result["workspace_source_changes"]))

    def test_shadow_and_recheck_never_reuse_old_executable_output(self):
        self.prepare()
        def shadow(runtime, argv, workspace, td, **kwargs):
            (workspace / "subject.pyi").write_text("value: int\n")
            return self.runner(runtime, argv, workspace, td, **kwargs)
        result = S.run_reviewer(self.out, "gemini", "initial", "review", 3, runner=shadow,
                                admission=LOCAL_FIXTURE_ADMISSION)
        self.assertEqual(result["status"], "workspace_changed")
        (self.repo / "subject.py").write_text("value = 2\n")
        fixture_recheck(self.out, self.repo, "again", 3, panel=lambda *a, **kw: {})
        for name in ("gemini", "grok"):
            workspace = self.out / f"reviewers/{name}/workspace"
            self.assertFalse((workspace / "subject.pyi").exists())
            self.assertFalse((workspace / "__pycache__").exists())
            self.assertEqual((workspace / "subject.py").read_text(), "value = 2\n")

    def test_clean_transport_receipt_records_environment_but_not_executed_code_claim(self):
        self.prepare()
        result = S.run_reviewer(self.out, "gemini", "initial", "review", 3, runner=self.runner,
                                admission=LOCAL_FIXTURE_ADMISSION)
        proof = result["executed_code_provenance"]
        self.assertEqual(proof["status"], "unqualified_runtime_telemetry")
        self.assertEqual(proof["environment"]["source_manifest"], result["snapshot_id"])
        self.assertFalse(result["claims_verified"])
        self.assertFalse(result["task_accepted"])

    def test_default_native_runner_is_closed_without_launching_a_process(self):
        self.prepare()
        with patch("council.session_runtime.subprocess.Popen") as launch:
            with self.assertRaisesRegex(SessionError, "no native turn was reserved"):
                S.run_reviewer(self.out, "gemini", "initial", "review", 3)
        launch.assert_not_called()

    def test_closed_grok_admission_does_not_reserve_or_poison_conversation_state(self):
        self.prepare()
        rd = self.out / "reviewers/grok"
        before = S._load(rd / "reviewer.json")
        with self.assertRaisesRegex(SessionError, "no native turn was reserved"):
            S.run_reviewer(self.out, "grok", "initial", "review", 3)
        after = S._load(rd / "reviewer.json")
        self.assertEqual(after["turn_count"], before["turn_count"])
        self.assertIsNone(after["conversation_id"])
        self.assertNotIn("planned_conversation_id", after)
        self.assertFalse((rd / "turns").exists())

    def test_default_recheck_gate_is_closed_before_any_durable_mutation(self):
        self.prepare()
        (self.repo / "subject.py").write_text("value = 2\n")
        session_path = self.out / "session.json"
        before_session = session_path.read_bytes()
        before_workspaces = {
            name: (self.out / f"reviewers/{name}/workspace/subject.py").read_bytes()
            for name in ("gemini", "grok")
        }
        with self.assertRaisesRegex(SessionError, "recheck left the session unchanged"):
            S.recheck(self.out, self.repo, "again", 3)
        self.assertEqual(session_path.read_bytes(), before_session)
        self.assertEqual([p.name for p in (self.out / "snapshots").iterdir()], ["0001"])
        for name, content in before_workspaces.items():
            rd = self.out / f"reviewers/{name}"
            self.assertEqual((rd / "workspace/subject.py").read_bytes(), content)
            self.assertFalse((rd / "staged").exists())
            self.assertFalse((rd / "archives").exists())

    def _seed_reaped_cleanup(self, identity):
        rd = self.out / "reviewers/gemini"
        td = rd / "turns/0001"; td.mkdir(parents=True)
        S._save(td / "result.json", {"process_cleanup": {
            "status": "stopped", "process_group": identity["pgid"], "guardian_identity": identity,
        }})
        reviewer = S._load(rd / "reviewer.json")
        reviewer.update(status="cleanup_failed", last_result="turns/0001/result.json")
        S._save(rd / "reviewer.json", reviewer)
        return rd, reviewer

    def test_previous_cleanup_rejects_boot_or_pid_reuse_and_never_uses_numeric_pgid(self):
        self.prepare()
        identity = {"pid": 4242, "ppid": 1, "pgid": 4242, "started": "start-a", "boot_id": "boot-a"}
        rd, reviewer = self._seed_reaped_cleanup(identity)
        with patch.object(S.session_runtime, "_boot_id", return_value="boot-b"), \
             patch.object(S.session_runtime, "_process_inventory") as inventory:
            with self.assertRaisesRegex(SessionError, "another boot"):
                S._check_previous_cleanup(rd, reviewer)
            inventory.assert_not_called()
        with patch.object(S.session_runtime, "_boot_id", return_value="boot-a"), \
             patch.object(S.session_runtime, "_process_inventory", return_value={4242: {**identity, "started": "start-b"}}):
            with self.assertRaisesRegex(SessionError, "stale or reused"):
                S._check_previous_cleanup(rd, reviewer)

    def test_previous_cleanup_requires_reaped_receipt_then_exact_absence(self):
        self.prepare()
        identity = {"pid": 4242, "ppid": 1, "pgid": 4242, "started": "start-a", "boot_id": "boot-a"}
        rd, reviewer = self._seed_reaped_cleanup(identity)
        with patch.object(S.session_runtime, "_boot_id", return_value="boot-a"), \
             patch.object(S.session_runtime, "_process_inventory", return_value={}):
            S._check_previous_cleanup(rd, reviewer)
        receipt = S._load(rd / "turns/0001/cleanup-resolution.json")
        self.assertEqual(receipt["status"], "exact_guardian_absent_after_reaped_receipt")

    def test_coordinator_death_uses_process_referenced_guardian_receipt_only_when_bound(self):
        self.prepare()
        rd = self.out / "reviewers/gemini"; td = rd / "turns/0001"; td.mkdir(parents=True)
        guardian = {"pid": 4242, "ppid": 1, "pgid": 4242, "started": "guard", "boot_id": "boot-a"}
        native = {"pid": 4243, "ppid": 4242, "pgid": 4242, "started": "native", "boot_id": "boot-a"}
        nonce = "0123456789abcdef0123456789abcdef"
        S._save(td / "process.json", {"schema": 2, "guardian_pid": 4242, "process_group": 4242,
                                        "guardian_identity": guardian, "ownership_nonce": nonce,
                                        "guardian_cleanup_receipt": "guardian-cleanup.json"})
        S._save(td / "guardian-cleanup.json", {"schema": 1, "status": "stopped", "boot_id": "boot-a",
                                                 "native_leader_identity": native, "observed_processes": [native],
                                                 "guardian_identity": guardian, "process_group": 4242,
                                                 "ownership_nonce": nonce,
                                                 "containment": "best_effort_lineage_observation",
                                                 "live_admission_qualified": False})
        reviewer = S._load(rd / "reviewer.json")
        reviewer.update(status="running", active_turn="turns/0001")
        S._save(rd / "reviewer.json", reviewer)
        with patch.object(S.session_runtime, "_boot_id", return_value="boot-a"), \
             patch.object(S.session_runtime, "_process_inventory", return_value={}):
            S._check_previous_cleanup(rd, reviewer)
        self.assertTrue((td / "cleanup-resolution.json").exists())
        (td / "guardian-cleanup.json").write_text("not json")
        (td / "cleanup-resolution.json").unlink()
        with self.assertRaisesRegex(SessionError, "malformed"):
            S._check_previous_cleanup(rd, reviewer)

    def test_referenced_guardian_receipt_nonce_mismatch_fails_closed(self):
        self.prepare()
        rd = self.out / "reviewers/gemini"; td = rd / "turns/0001"; td.mkdir(parents=True)
        guardian = {"pid": 4242, "ppid": 1, "pgid": 4242, "started": "guard", "boot_id": "boot-a"}
        native = {"pid": 4243, "ppid": 4242, "pgid": 4242, "started": "native", "boot_id": "boot-a"}
        S._save(td / "process.json", {"schema": 2, "process_group": 4242, "guardian_identity": guardian,
                                        "ownership_nonce": "0123456789abcdef0123456789abcdef",
                                        "guardian_cleanup_receipt": "guardian-cleanup.json"})
        S._save(td / "guardian-cleanup.json", {"schema": 1, "status": "stopped", "boot_id": "boot-a",
            "guardian_identity": guardian, "process_group": 4242,
            "ownership_nonce": "fedcba9876543210fedcba9876543210", "native_leader_identity": native,
            "observed_processes": [native], "containment": "best_effort_lineage_observation",
            "live_admission_qualified": False})
        reviewer = S._load(rd / "reviewer.json"); reviewer.update(status="running", active_turn="turns/0001")
        S._save(rd / "reviewer.json", reviewer)
        with self.assertRaisesRegex(SessionError, "does not bind"):
            S._check_previous_cleanup(rd, reviewer)
