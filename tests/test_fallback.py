"""Fallback chain: invoke_chain traversal, dead-provider cache, timeout
per-hop resolution, and config validation. Patches invoke() (not
invoke_chain) to test the real chain logic."""
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from council import config  # noqa: E402
from council.providers import (  # noqa: E402
    DEFAULT_TIMEOUT,
    Provider,
    invoke_chain,
    reset_dead_cache,
    _dead_providers,
)


def _v(name, timeout=0.0, fallbacks=None):
    return Provider(name=name, transport="cli", bin="x", argv=["x"],
                    timeout=timeout, fallbacks=list(fallbacks or []))


class TestInvokeChainTraversal(unittest.TestCase):
    """Patch providers.invoke to test invoke_chain's own logic."""

    def setUp(self):
        reset_dead_cache()

    def tearDown(self):
        reset_dead_cache()

    def _patch(self, side_effect):
        import council.providers as PV
        return mock.patch.object(PV, "invoke", side_effect=side_effect)

    def test_primary_ok_no_fallback(self):
        provs = {"a": _v("a")}

        def fake(p, prompt, timeout):
            return True, f"answer-{p.name}"

        with self._patch(fake):
            ok, out = invoke_chain("a", provs, "q")
        self.assertTrue(ok)
        self.assertEqual(out, "answer-a")

    def test_primary_fail_fallback_ok(self):
        provs = {"a": _v("a", fallbacks=["b"]), "b": _v("b")}

        def fake(p, prompt, timeout):
            if p.name == "a":
                return False, "a: dead"
            return True, f"answer-{p.name}"

        with self._patch(fake):
            ok, out = invoke_chain("a", provs, "q")
        self.assertTrue(ok)
        self.assertEqual(out, "answer-b")

    def test_all_fail_joined_errors(self):
        provs = {"a": _v("a", fallbacks=["b", "c"]),
                 "b": _v("b"), "c": _v("c")}

        def fake(p, prompt, timeout):
            return False, f"{p.name}: err"

        with self._patch(fake):
            ok, out = invoke_chain("a", provs, "q")
        self.assertFalse(ok)
        self.assertIn("a: err", out)
        self.assertIn("b: err", out)
        self.assertIn("c: err", out)

    def test_unknown_fallback_skipped(self):
        provs = {"a": _v("a", fallbacks=["ghost", "b"]), "b": _v("b")}

        def fake(p, prompt, timeout):
            if p.name == "a":
                return False, "a: dead"
            return True, f"answer-{p.name}"

        with self._patch(fake):
            ok, out = invoke_chain("a", provs, "q")
        self.assertTrue(ok)
        self.assertEqual(out, "answer-b")

    def test_unknown_primary_returns_error(self):
        ok, out = invoke_chain("nonexistent", {}, "q")
        self.assertFalse(ok)
        self.assertIn("unknown provider", out)

    def test_empty_fallbacks_single_voice(self):
        provs = {"a": _v("a", fallbacks=[])}

        def fake(p, prompt, timeout):
            return False, "a: dead"

        with self._patch(fake):
            ok, out = invoke_chain("a", provs, "q")
        self.assertFalse(ok)
        self.assertIn("a: dead", out)


class TestDeadProviderCache(unittest.TestCase):

    def setUp(self):
        reset_dead_cache()

    def tearDown(self):
        reset_dead_cache()

    def _patch(self, side_effect):
        import council.providers as PV
        return mock.patch.object(PV, "invoke", side_effect=side_effect)

    def test_dead_cached_on_failure(self):
        provs = {"a": _v("a", fallbacks=["b"]), "b": _v("b")}
        calls = []

        def fake(p, prompt, timeout):
            calls.append(p.name)
            if p.name == "a":
                return False, "a: dead"
            return True, "ok"

        with self._patch(fake):
            invoke_chain("a", provs, "q1")
            self.assertIn("a", _dead_providers)

            calls.clear()
            invoke_chain("a", provs, "q2")
            self.assertNotIn("a", calls)
            self.assertIn("b", calls)

    def test_reset_clears_cache(self):
        _dead_providers.add("test")
        reset_dead_cache()
        self.assertEqual(len(_dead_providers), 0)


class TestTimeoutPerHop(unittest.TestCase):
    """Each fallback must use its OWN timeout, not the primary's."""

    def setUp(self):
        reset_dead_cache()

    def tearDown(self):
        reset_dead_cache()

    def test_fallback_uses_own_timeout(self):
        provs = {"a": _v("a", timeout=2700, fallbacks=["b"]),
                 "b": _v("b", timeout=600)}
        seen_timeouts = []

        def fake(p, prompt, timeout):
            seen_timeouts.append((p.name, timeout))
            if p.name == "a":
                return False, "a: dead"
            return True, "ok"

        import council.providers as PV
        with mock.patch.object(PV, "invoke", side_effect=fake):
            invoke_chain("a", provs, "q", timeout_override=None)
        self.assertEqual(seen_timeouts[0], ("a", 2700))
        self.assertEqual(seen_timeouts[1], ("b", 600))

    def test_explicit_override_forces_all(self):
        provs = {"a": _v("a", timeout=2700, fallbacks=["b"]),
                 "b": _v("b", timeout=600)}
        seen_timeouts = []

        def fake(p, prompt, timeout):
            seen_timeouts.append((p.name, timeout))
            if p.name == "a":
                return False, "a: dead"
            return True, "ok"

        import council.providers as PV
        with mock.patch.object(PV, "invoke", side_effect=fake):
            invoke_chain("a", provs, "q", timeout_override=999)
        self.assertEqual(seen_timeouts[0], ("a", 999))
        self.assertEqual(seen_timeouts[1], ("b", 999))

    def test_no_timeout_uses_default(self):
        provs = {"a": _v("a", timeout=0)}
        seen = []

        def fake(p, prompt, timeout):
            seen.append(timeout)
            return True, "ok"

        import council.providers as PV
        with mock.patch.object(PV, "invoke", side_effect=fake):
            invoke_chain("a", provs, "q", timeout_override=None)
        self.assertEqual(seen[0], DEFAULT_TIMEOUT)


class TestFallbacksConfigValidation(unittest.TestCase):

    def _toml(self, body: str) -> str:
        fd = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
        fd.write(textwrap.dedent(body))
        fd.close()
        self.addCleanup(lambda: Path(fd.name).unlink(missing_ok=True))
        return fd.name

    def test_string_fallbacks_rejected(self):
        with self.assertRaises(ValueError) as e:
            config.load(self._toml("""
                [providers.opus]
                type = "cli"
                bin  = "claude"
                argv = ["claude", "-p"]
                fallbacks = "codex"

                [council]
                voices = ["opus"]
            """))
        self.assertIn("must be an array", str(e.exception))

    def test_unknown_fallback_name_rejected(self):
        with self.assertRaises(ValueError) as e:
            config.load(self._toml("""
                [providers.opus]
                type = "cli"
                bin  = "claude"
                argv = ["claude", "-p"]
                fallbacks = ["ghostvoice"]

                [council]
                voices = ["opus"]
            """))
        self.assertIn("unknown providers", str(e.exception))

    def test_valid_fallbacks_accepted(self):
        cfg = config.load(self._toml("""
            [providers.opus]
            type = "cli"
            bin  = "claude"
            argv = ["claude", "-p"]
            fallbacks = ["codex"]

            [council]
            voices = ["opus"]
        """))
        self.assertEqual(cfg.providers["opus"].fallbacks, ["codex"])


if __name__ == "__main__":
    unittest.main()
