"""P1A ownership identity, receipt and environment regressions."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from council import session_runtime as R


class TestProcessIdentity(unittest.TestCase):
    def identity(self, *, boot_id: str = "boot-a", started: str = "start-a") -> dict:
        return {"pid": 4242, "boot_id": boot_id, "started": started}

    def test_boot_mismatch_never_signals_numeric_pid(self):
        owned = self.identity()
        inventory = {4242: self.identity(boot_id="boot-b")}
        with patch.object(R.os, "kill") as kill:
            with self.assertRaises(R.SessionError):
                R._signal_owned_process(owned, signal.SIGKILL, inventory=inventory)
        kill.assert_not_called()

    def test_pid_reuse_start_mismatch_never_signals_numeric_pid(self):
        owned = self.identity()
        inventory = {4242: self.identity(started="start-b")}
        with patch.object(R.os, "kill") as kill:
            with self.assertRaises(R.SessionError):
                R._signal_owned_process(owned, signal.SIGTERM, inventory=inventory)
        kill.assert_not_called()

    def test_inventory_failure_never_falls_back_to_blind_signal(self):
        with patch.object(R, "_process_inventory", side_effect=OSError("inventory unavailable")), \
             patch.object(R.os, "kill") as kill:
            with self.assertRaises(R.SessionError):
                R._signal_owned_process(self.identity(), signal.SIGTERM)
        kill.assert_not_called()

    def test_exact_process_instance_can_be_signalled(self):
        owned = self.identity()
        with patch.object(R.os, "kill") as kill:
            self.assertTrue(R._signal_owned_process(owned, signal.SIGTERM, inventory={4242: owned}))
        kill.assert_called_once_with(4242, signal.SIGTERM)

    def test_missing_pid_in_incomplete_inventory_is_unknown_not_absent(self):
        inventory = R.ProcessInventory(
            processes={}, method="fixture-partial", boot_id="boot-a",
            observed_at_monotonic_ns=1, complete_for_descendants=False,
        )
        with patch.object(R.os, "kill") as kill:
            with self.assertRaisesRegex(R.SessionError, "incomplete observation"):
                R._signal_owned_process(self.identity(), signal.SIGTERM, inventory=inventory)
        kill.assert_not_called()

    def test_eperm_from_exact_signal_is_not_reinterpreted_as_absence(self):
        owned = self.identity()
        with patch.object(R.os, "kill", side_effect=PermissionError(1, "denied")) as kill:
            with self.assertRaises(PermissionError):
                R._signal_owned_process(owned, signal.SIGTERM, inventory={4242: owned})
        kill.assert_called_once_with(4242, signal.SIGTERM)

    def test_exit_between_identity_check_and_signal_is_safe_but_not_containment_proof(self):
        owned = self.identity()
        with patch.object(R.os, "kill", side_effect=ProcessLookupError()) as kill:
            self.assertFalse(R._signal_owned_process(
                owned, signal.SIGTERM, inventory={4242: owned},
            ))
        kill.assert_called_once_with(4242, signal.SIGTERM)
        self.assertFalse(R.CLOSED_NATIVE_ADMISSION.permits_process_launch)

    def test_stop_without_bound_guardian_receipt_never_uses_numeric_group(self):
        proc = Mock(pid=4242)
        with patch.object(R, "_signal_group") as signal_group:
            with self.assertRaisesRegex(R.SessionError, "ownership receipt is incomplete"):
                R._stop_group(proc)
        signal_group.assert_not_called()

    def test_cleanup_receipt_nonce_mismatch_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as root:
            receipt = Path(root) / "cleanup.json"
            owned = self.identity()
            receipt.write_text(json.dumps({
                "status": "stopped",
                "containment": "best_effort_lineage_observation",
                "live_admission_qualified": False,
                "ownership_nonce": "wrong",
                "process_group": 4242,
                "guardian_identity": owned,
                "boot_id": owned["boot_id"],
            }))
            proc = Mock(pid=4242)
            proc.poll.return_value = 0
            proc._council_cleanup_receipt = str(receipt)
            proc._council_guardian_identity = owned
            proc._council_ownership_nonce = "expected"
            with self.assertRaisesRegex(R.SessionError, "did not verify"):
                R._stop_group(proc)


class TestRuntimeReceiptAndEnvironment(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_turn_receipt_binds_boot_process_and_qualified_environment(self):
        turn = self.root / "turn"
        turn.mkdir()
        sid = "11111111-1111-4111-8111-111111111111"
        event = {"event": "result", "conversation_id": sid,
                 "result": {"status": "SUCCESS", "response": "ok", "conversation_id": sid}}
        code = f"import json;print(json.dumps({event!r}),flush=True)"
        result = R.run_native_turn(
            "antigravity", [sys.executable, "-c", code], self.root, turn, timeout=3,
            admission=R.LOCAL_FIXTURE_ADMISSION,
        )
        self.assertEqual(result["status"], "completed")
        receipt = json.loads((turn / "process.json").read_text())
        self.assertTrue(receipt["boot_id"])
        self.assertEqual(receipt["guardian_identity"]["boot_id"], receipt["boot_id"])
        self.assertEqual(receipt["guardian_cleanup_receipt"], "guardian-cleanup.json")
        self.assertRegex(receipt["ownership_nonce"], r"^[0-9a-f]{32}$")
        self.assertFalse(receipt["inventory_provenance"]["complete_for_descendants"])
        self.assertFalse(receipt["guardian_cleanup"]["containment_qualified"])
        guardian = json.loads((turn / receipt["guardian_cleanup_receipt"]).read_text())
        self.assertEqual(guardian["ownership_nonce"], receipt["ownership_nonce"])
        self.assertTrue(R._same_process_instance(
            guardian["guardian_identity"], receipt["guardian_identity"],
        ))
        self.assertEqual(guardian["process_group"], receipt["process_group"])
        execution = receipt["execution_environment"]
        self.assertEqual(execution["policy"], "p1a-isolated-cache-v1")
        self.assertEqual(len(execution["env_sha256"]), 64)
        self.assertEqual(execution["cwd"], str(self.root.resolve()))
        self.assertTrue(execution["executable"])
        self.assertTrue(execution["guardian_interpreter"])

    def test_runtime_env_strips_module_overrides_and_isolates_caches(self):
        turn = self.root / "turn"
        with patch.dict(os.environ, {
            "PYTHONPATH": "/foreign/modules", "PYTHONHOME": "/foreign/python",
            "PYTHONSTARTUP": "/foreign/startup.py", "PYTHONPYCACHEPREFIX": "/foreign/cache",
            "XDG_CACHE_HOME": "/foreign/xdg", "TMPDIR": "/foreign/tmp",
        }, clear=False):
            env = R.runtime_env(self.root, turn)
        self.assertNotIn("PYTHONPATH", env)
        self.assertNotIn("PYTHONHOME", env)
        self.assertNotIn("PYTHONSTARTUP", env)
        self.assertEqual(env["PYTHONPYCACHEPREFIX"], str(turn / "runtime-cache/pycache"))
        self.assertEqual(env["XDG_CACHE_HOME"], str(turn / "runtime-cache/xdg"))
        self.assertEqual(env["TMPDIR"], str(turn / "runtime-cache/tmp"))
        for path in (env["PYTHONPYCACHEPREFIX"], env["XDG_CACHE_HOME"], env["TMPDIR"]):
            self.assertTrue(Path(path).is_dir())

    def test_live_native_admission_is_programmatically_closed_by_default(self):
        turn = self.root / "blocked-turn"
        turn.mkdir()
        marker = self.root / "must-not-run"
        code = f"from pathlib import Path;Path({str(marker)!r}).write_text('ran')"
        with patch.object(R.sys, "platform", "darwin"):
            result = R.run_native_turn(
                "grok", [sys.executable, "-c", code], self.root, turn, timeout=3,
            )
        self.assertEqual(result["status"], "containment_unqualified")
        self.assertFalse(result["report_received"])
        self.assertFalse(marker.exists())
        self.assertIn("containment", result["error"])


if __name__ == "__main__":
    unittest.main()
