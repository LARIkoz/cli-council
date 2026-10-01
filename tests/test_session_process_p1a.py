"""P1A process lifetime regressions. Local subprocess fixtures only."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from council.review_sessions import SessionError, _lock
from council.session_runtime import LOCAL_FIXTURE_ADMISSION, run_native_turn


SID = "11111111-1111-4111-8111-111111111111"


def _events() -> list[dict]:
    return [
        {"event": "init", "conversation_id": SID, "init": {"model": "fixture"}},
        {"event": "result", "result": {
            "conversation_id": SID, "status": "SUCCESS", "response": "fixture report",
        }},
    ]


def _helper_code(marker: Path, lifetime: float = 3.0) -> str:
    return (
        "import signal,time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"f=open({str(marker)!r},'a'); deadline=time.monotonic()+{lifetime!r}\n"
        "while time.monotonic()<deadline:\n"
        " f.write('x');f.flush();time.sleep(.025)\n"
    )


def _native_code(marker: Path, *, terminal: bool) -> str:
    emitted = "\n".join(f"print(json.dumps({event!r}),flush=True)" for event in _events())
    return (
        "import json,os,subprocess,sys,time\n"
        f"p=subprocess.Popen([{sys.executable!r},'-c',{_helper_code(marker)!r}],"
        "start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
        "stderr=subprocess.DEVNULL)\n"
        f"open({str(marker) + '.pid'!r},'w').write(str(p.pid))\n"
        f"while not os.path.exists({str(marker)!r}): time.sleep(.005)\n"
        + (emitted + "\n" if terminal else "time.sleep(10)\n")
    )


class TestOwnedProcessLifetime(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _turn(self, code: str, *, timeout: float = 3, **kwargs):
        turn = self.root / f"turn-{len(list(self.root.glob('turn-*')))}"
        turn.mkdir()
        return run_native_turn(
            "antigravity", [sys.executable, "-c", code], self.root, turn,
            timeout=timeout, admission=LOCAL_FIXTURE_ADMISSION, **kwargs,
        ), turn

    def _assert_stopped(self, marker: Path) -> None:
        self.assertTrue(marker.exists(), "detached helper never started")
        size = marker.stat().st_size
        time.sleep(.18)
        self.assertEqual(marker.stat().st_size, size, "owned detached helper survived cleanup")

    def test_success_stops_detached_new_session_child_before_report(self):
        marker = self.root / "success-activity"
        result, _ = self._turn(_native_code(marker, terminal=True))
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["report_received"])
        self.assertEqual(result["process_cleanup"]["status"], "stopped")
        self._assert_stopped(marker)

    def test_cancellation_stops_detached_new_session_child(self):
        marker = self.root / "cancel-activity"
        cancelled = False

        def cancel() -> bool:
            return cancelled

        def on_event(*_args) -> None:
            nonlocal cancelled
            cancelled = True

        # Emit init only, then wait. Cancellation begins after identity capture.
        code = _native_code(marker, terminal=False).replace("time.sleep(10)\n", (
            f"print(json.dumps({_events()[0]!r}),flush=True)\n"
            "time.sleep(10)\n"
        ))
        result, _ = self._turn(code, on_event=on_event, cancel=cancel)
        self.assertEqual(result["status"], "cancelled")
        self._assert_stopped(marker)

    def test_timeout_hard_stop_covers_detached_new_session_child(self):
        marker = self.root / "timeout-activity"
        result, _ = self._turn(_native_code(marker, terminal=False), timeout=.35)
        self.assertEqual(result["status"], "timed_out")
        self._assert_stopped(marker)

    def test_guardian_holds_lease_after_leader_exit_until_owned_work_stops(self):
        turn = self.root / "coordinator-turn"
        turn.mkdir()
        lock = self.root / "reviewer.lock"
        marker = self.root / "leader-exit-activity"
        leader_done = self.root / "leader-exited"
        native = _native_code(marker, terminal=True) + f"open({str(leader_done)!r},'w').write('done')\n"
        coordinator_code = (
            "from pathlib import Path\n"
            "from council.review_sessions import _lock\n"
            "from council.session_runtime import run_native_turn\n"
            f"with _lock(Path({str(lock)!r})) as lease:\n"
            f" run_native_turn('antigravity',[{sys.executable!r},'-c',{native!r}],"
            f"Path({str(self.root)!r}),Path({str(turn)!r}),timeout=5,lease_fd=lease,"
            "admission=__import__('council.session_runtime',fromlist=['LOCAL_FIXTURE_ADMISSION']).LOCAL_FIXTURE_ADMISSION)\n"
        )
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
        coordinator = subprocess.Popen(
            [sys.executable, "-c", coordinator_code], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 4
            while not leader_done.exists() and coordinator.poll() is None and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(leader_done.exists(), "native leader did not reach its exit boundary")
            coordinator.kill()
            coordinator.wait(timeout=3)

            acquired = False
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                try:
                    with _lock(lock):
                        acquired = True
                        self._assert_stopped(marker)
                    break
                except SessionError:
                    time.sleep(.02)
            self.assertTrue(acquired, "guardian failed to release the reviewer lease after cleanup")
            process_receipt = json.loads((turn / "process.json").read_text())
            cleanup_path = turn / process_receipt["guardian_cleanup_receipt"]
            deadline = time.monotonic() + 2
            while not cleanup_path.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            cleanup = json.loads(cleanup_path.read_text())
            self.assertEqual(cleanup["status"], "stopped")
            self.assertFalse(cleanup["live_admission_qualified"])
            self.assertEqual(cleanup["ownership_nonce"], process_receipt["ownership_nonce"])
            self.assertEqual(cleanup["process_group"], process_receipt["process_group"])
        finally:
            if coordinator.poll() is None:
                coordinator.kill()
                coordinator.wait(timeout=3)


if __name__ == "__main__":
    unittest.main()
