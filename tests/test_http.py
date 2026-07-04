"""Optional token (http) voices: env-gated reachability, OpenAI-compatible call,
config wiring, and the enrol gate preserving hand-written provider blocks. All
offline — urlopen is faked, no key or network is ever touched."""
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "installer"))

from council import config as C  # noqa: E402
from council import providers as P  # noqa: E402

HTTP = P.Provider(
    name="deepseek", transport="http",
    endpoint="https://api.deepseek.com/v1/chat/completions",
    model="deepseek-chat", key_env="DEEPSEEK_API_KEY", timeout=120.0,
)


class _FakeResp:
    def __init__(self, payload: bytes):
        self._p = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._p


def _openai_payload(text: str) -> bytes:
    return json.dumps({"choices": [{"message": {"content": text}}]}).encode()


class TestHttpReachability(unittest.TestCase):
    def test_installed_means_key_present(self):
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-x"}, clear=False):
            self.assertTrue(P.is_installed(HTTP))

    def test_missing_key_is_not_installed(self):
        env = {k: v for k, v in os.environ.items() if k != "DEEPSEEK_API_KEY"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertFalse(P.is_installed(HTTP))

    def test_http_voice_uses_its_timeout_ceiling(self):
        self.assertEqual(P.resolve_timeout(HTTP, None), 120.0)
        self.assertEqual(P.resolve_timeout(HTTP, 30.0), 30.0)  # explicit override still wins


class TestHttpInvoke(unittest.TestCase):
    def test_success_parses_openai_shape(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["auth"] = req.headers.get("Authorization")
            captured["body"] = json.loads(req.data.decode())
            captured["timeout"] = timeout
            return _FakeResp(_openai_payload("  the answer  "))

        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-secret"}, clear=False), \
                mock.patch("urllib.request.urlopen", fake_urlopen):
            ok, out = P.invoke(HTTP, "ping", timeout=42.0)
        self.assertTrue(ok)
        self.assertEqual(out, "the answer")               # stripped
        self.assertEqual(captured["url"], HTTP.endpoint)
        self.assertEqual(captured["auth"], "Bearer sk-secret")  # key from env, bearer header
        self.assertEqual(captured["body"]["model"], "deepseek-chat")
        self.assertEqual(captured["body"]["messages"][0]["content"], "ping")
        self.assertEqual(captured["timeout"], 42.0)

    def test_body_sends_max_tokens(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode())
            return _FakeResp(_openai_payload("ok"))

        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-x"}, clear=False), \
                mock.patch("urllib.request.urlopen", fake_urlopen):
            P.invoke(HTTP, "ping")
        self.assertEqual(captured["body"]["max_tokens"], 8192)   # F1: output ceiling set

    def test_truncated_output_is_marked_not_silent(self):
        def fake_urlopen(req, timeout=None):
            payload = json.dumps({"choices": [{"message": {"content": "half a review"},
                                               "finish_reason": "length"}]}).encode()
            return _FakeResp(payload)

        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-x"}, clear=False), \
                mock.patch("urllib.request.urlopen", fake_urlopen):
            ok, out = P.invoke(HTTP, "ping")
        self.assertTrue(ok)                       # F2: partial text still returned
        self.assertIn("half a review", out)
        self.assertIn("truncated", out.lower())   # ...but loudly marked, never silent

    def test_429_is_retried_then_succeeds_honouring_retry_after(self):
        calls, waits = [], []

        def fake_urlopen(req, timeout=None):
            calls.append(1)
            if len(calls) == 1:
                raise urllib.error.HTTPError(req.full_url, 429, "rate limited",
                                             {"Retry-After": "1"}, None)
            return _FakeResp(_openai_payload("recovered"))

        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-x"}, clear=False), \
                mock.patch("urllib.request.urlopen", fake_urlopen), \
                mock.patch("council.providers.time.sleep", lambda s: waits.append(s)):
            ok, out = P.invoke(HTTP, "ping")
        self.assertTrue(ok)                # F3: recovered on retry
        self.assertEqual(out, "recovered")
        self.assertEqual(len(calls), 2)    # retried exactly once
        self.assertEqual(waits, [1.0])     # respected Retry-After, not blind backoff

    def test_client_error_is_not_retried(self):
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(1)
            raise urllib.error.HTTPError(req.full_url, 401, "unauthorized", {}, None)

        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-x"}, clear=False), \
                mock.patch("urllib.request.urlopen", fake_urlopen), \
                mock.patch("council.providers.time.sleep", lambda s: None):
            ok, out = P.invoke(HTTP, "ping")
        self.assertFalse(ok)
        self.assertEqual(len(calls), 1)    # 401 is terminal — no retry
        self.assertIn("HTTP 401", out)

    def test_missing_key_fails_loudly(self):
        env = {k: v for k, v in os.environ.items() if k != "DEEPSEEK_API_KEY"}
        with mock.patch.dict(os.environ, env, clear=True):
            ok, out = P.invoke(HTTP, "ping")
        self.assertFalse(ok)
        self.assertIn("DEEPSEEK_API_KEY", out)

    def test_http_error_is_reported(self):
        def boom(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-x"}, clear=False), \
                mock.patch("urllib.request.urlopen", boom):
            ok, out = P.invoke(HTTP, "ping")
        self.assertFalse(ok)
        self.assertIn("HTTP 401", out)

    def test_unexpected_shape_is_reported_not_crash(self):
        def weird(req, timeout=None):
            return _FakeResp(b'{"not":"openai"}')

        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-x"}, clear=False), \
                mock.patch("urllib.request.urlopen", weird):
            ok, out = P.invoke(HTTP, "ping")
        self.assertFalse(ok)
        self.assertIn("unexpected response shape", out)

    def test_empty_content_fails(self):
        def empty(req, timeout=None):
            return _FakeResp(_openai_payload(""))

        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-x"}, clear=False), \
                mock.patch("urllib.request.urlopen", empty):
            ok, out = P.invoke(HTTP, "ping")
        self.assertFalse(ok)
        self.assertIn("empty content", out)


class TestPromptFileFallback(unittest.TestCase):
    """A config override can keep uses_prompt_file=True but drop {prompt_file}
    from argv; the prompt must fall back to stdin, never be silently lost."""

    def test_missing_placeholder_delivers_via_stdin(self):
        p = P.Provider(name="x", bin="cat", uses_prompt_file=True, argv=["cat"])  # no {prompt_file}
        captured = {}

        def fake_run(argv, input=None, **kw):
            captured["argv"] = argv
            captured["input"] = input
            r = mock.Mock()
            r.returncode = 0
            r.stdout = input or ""
            r.stderr = ""
            return r

        with mock.patch.object(P, "is_installed", lambda _p: True), \
                mock.patch.object(P.subprocess, "run", fake_run):
            ok, out = P.invoke(p, "HELLO-PROMPT", timeout=5)
        self.assertTrue(ok)
        self.assertEqual(captured["input"], "HELLO-PROMPT")   # delivered on stdin
        self.assertEqual(captured["argv"], ["cat"])           # no temp path injected

    def test_placeholder_present_still_uses_file(self):
        p = P.Provider(name="x", bin="tool", uses_prompt_file=True,
                       argv=["tool", "--file", "{prompt_file}"])
        captured = {}

        def fake_run(argv, input=None, **kw):
            captured["argv"] = argv
            captured["input"] = input
            r = mock.Mock()
            r.returncode = 0
            r.stdout = "ok"
            r.stderr = ""
            return r

        with mock.patch.object(P, "is_installed", lambda _p: True), \
                mock.patch.object(P.subprocess, "run", fake_run):
            P.invoke(p, "PROMPT", timeout=5)
        self.assertIsNone(captured["input"])                  # file mode → no stdin
        self.assertTrue(any(a.endswith(".txt") for a in captured["argv"]))  # temp path injected


class TestHttpConfig(unittest.TestCase):
    def _write(self, body: str) -> str:
        fd = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
        fd.write(body)
        fd.close()
        self.addCleanup(lambda: Path(fd.name).unlink(missing_ok=True))
        return fd.name

    def test_http_block_defines_enrollable_voice(self):
        cfg = C.load(self._write(
            '[providers.deepseek]\n'
            'type = "http"\n'
            'endpoint = "https://api.deepseek.com/v1/chat/completions"\n'
            'model = "deepseek-chat"\n'
            'key_env = "DEEPSEEK_API_KEY"\n'
            'timeout = 120\n\n'
            '[council]\n'
            'voices = ["claude", "deepseek"]\n'
            'chairman = "claude"\n'))
        p = cfg.providers["deepseek"]
        self.assertEqual(p.transport, "http")
        self.assertEqual(p.model, "deepseek-chat")
        self.assertEqual(p.key_env, "DEEPSEEK_API_KEY")
        self.assertEqual(p.timeout, 120.0)
        self.assertEqual(cfg.voices, ["claude", "deepseek"])  # no "unknown voice" raise

    def test_http_voice_can_chair(self):
        cfg = C.load(self._write(
            '[providers.ds]\ntype="http"\nendpoint="u"\nmodel="m"\nkey_env="K"\n\n'
            '[council]\nvoices=["ds"]\nchairman="ds"\n'))
        self.assertEqual(cfg.chairman, "ds")

    def test_missing_required_field_fails_loudly(self):
        with self.assertRaises(ValueError) as e:
            C.load(self._write('[providers.x]\ntype="http"\nendpoint="u"\n'))  # no model/key_env
        self.assertIn("model", str(e.exception))
        self.assertIn("key_env", str(e.exception))

    def test_review_panels_parsed_and_validated(self):
        cfg = C.load(self._write(
            '[providers.ds]\ntype="http"\nendpoint="u"\nmodel="m"\nkey_env="K"\n\n'
            '[council]\nvoices=["claude","ds"]\n\n'
            '[review]\naudit=["claude","ds"]\nredteam=["ds"]\n'))
        self.assertEqual(cfg.review_audit, ["claude", "ds"])
        self.assertEqual(cfg.review_redteam, ["ds"])

    def test_review_panel_unknown_voice_raises(self):
        with self.assertRaises(ValueError) as e:
            C.load(self._write('[council]\nvoices=["claude"]\n\n[review]\naudit=["ghost"]\n'))
        self.assertIn("ghost", str(e.exception))

    def test_registry_includes_http_without_validation(self):
        reg = C.provider_registry(self._write(
            '[providers.ds]\ntype="http"\nendpoint="u"\nmodel="m"\nkey_env="K"\n'))
        self.assertIn("ds", reg)
        self.assertEqual(reg["ds"].transport, "http")
        self.assertIn("claude", reg)  # built-in CLI voices still present


class TestEnrollPreservesHttpBlocks(unittest.TestCase):
    """The clobber guard: `enroll` rewrites [council] but must keep the user's
    hand-written [providers.*] token definitions."""

    def setUp(self):
        import doctor
        self.doctor = doctor
        self._orig_invoke = doctor.invoke
        self._orig_installed = doctor.is_installed
        self._orig_cfg = doctor.CONFIG
        doctor.is_installed = lambda p: True
        doctor.invoke = lambda p, prompt, timeout=90.0: (True, "ok")
        fd = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
        fd.write(
            '[providers.deepseek]\n'
            'type = "http"\n'
            'endpoint = "https://api.deepseek.com/v1/chat/completions"\n'
            'model = "deepseek-chat"\n'
            'key_env = "DEEPSEEK_API_KEY"\n'
            'timeout = 120\n\n'
            '[council]\nvoices = ["claude"]\nchairman = "claude"\n')
        fd.close()
        self._tmp = Path(fd.name)
        doctor.CONFIG = self._tmp

    def tearDown(self):
        self.doctor.invoke = self._orig_invoke
        self.doctor.is_installed = self._orig_installed
        self.doctor.CONFIG = self._orig_cfg
        self._tmp.unlink(missing_ok=True)

    def test_enroll_keeps_provider_block(self):
        rc = self.doctor.enroll(["claude", "deepseek"])
        self.assertEqual(rc, 0)
        import tomllib
        data = tomllib.loads(self._tmp.read_text())
        self.assertEqual(data["council"]["voices"], ["claude", "deepseek"])
        # the http voice definition survived the rewrite
        self.assertEqual(data["providers"]["deepseek"]["type"], "http")
        self.assertEqual(data["providers"]["deepseek"]["endpoint"],
                         "https://api.deepseek.com/v1/chat/completions")
        self.assertEqual(data["providers"]["deepseek"]["key_env"], "DEEPSEEK_API_KEY")


if __name__ == "__main__":
    unittest.main()
