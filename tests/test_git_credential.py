"""Synthetic credential protocol checks; no live account or credential reads."""

from contextlib import contextmanager, redirect_stderr, redirect_stdout
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
# The protocol tests do not load MSAL or contact an identity provider.
sys.modules["msal"] = types.ModuleType("msal")
SPEC = importlib.util.spec_from_file_location("azure_test_subject", ROOT / "bin" / "az-devops.py")
AZURE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = AZURE
SPEC.loader.exec_module(AZURE)

URL = "https://dev.azure.com/example/Example%20Project/_git/sample-repo"
REQUEST = "protocol=https\nhost=dev.azure.com\npath=example/Example%20Project/_git/sample-repo\ncapability[]=authtype\n\n"
FAKE_TOKEN = "synthetic-token-for-tests-only"


class GitCredentialTests(unittest.TestCase):
    def setUp(self):
        self.document = {"devops": {"git_urls": [URL]}}
        self.profile = types.SimpleNamespace(username="you@example.com")
        self.provider_calls = 0
        self.result = {"access_token": FAKE_TOKEN}

    @contextmanager
    def application(self, profile):
        self.provider_calls += 1
        # Both provider streams must stay out of the credential protocol.
        print("synthetic-provider-stdout")
        print("synthetic-provider-stderr", file=sys.stderr)
        yield None, object()

    def run_helper(self, operation="get", request=REQUEST):
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(sys, "stdin", io.StringIO(request)), \
                patch.object(AZURE, "_read_document", return_value=self.document), \
                patch.object(AZURE, "_profile", return_value=self.profile) as profile_lookup, \
                patch.object(AZURE, "_locked_application", self.application), \
                patch.object(AZURE, "_account", return_value={"id": "synthetic-account"}), \
                patch.object(AZURE, "_silent_token", return_value=self.result), \
                redirect_stdout(output), redirect_stderr(errors):
            status = AZURE._git_credential([operation])
        self.profile_lookup = profile_lookup
        return status, output.getvalue(), errors.getvalue()

    def test_exact_repository_uses_negotiated_ephemeral_bearer(self):
        status, output, errors = self.run_helper()
        self.assertEqual(status, 0)
        self.assertEqual(output, "capability[]=authtype\nauthtype=bearer\ncredential=" + FAKE_TOKEN + "\nephemeral=true\n\n")
        self.assertEqual(errors, "")
        self.assertEqual(self.provider_calls, 1)
        self.assertNotIn("password=", output)

    def test_percent_encoded_and_decoded_project_paths_match(self):
        status, _, _ = self.run_helper(request=REQUEST.replace("Example%20Project", "Example Project"))
        self.assertEqual(status, 0)
        self.assertEqual(self.provider_calls, 1)

    def test_unsupported_scope_never_loads_a_profile_or_token(self):
        for request in (
            REQUEST.replace("protocol=https", "protocol=http"),
            REQUEST.replace("host=dev.azure.com", "host=dev.azure.com.attacker.example"),
            REQUEST.replace("host=dev.azure.com", "host=dev.azure.com:443"),
            REQUEST.replace("sample-repo", "another-repo"),
            REQUEST.replace("example/Example", "other/Example"),
            REQUEST.replace("sample-repo", "%2e%2e"),
            REQUEST.replace("sample-repo", "sample%2frepo"),
            REQUEST.replace("sample-repo", "sample%5crepo"),
            REQUEST.replace("sample-repo", "sample%0arepo"),
            "protocol=https\nhost=dev.azure.com\n\n",
        ):
            with self.subTest(request=request):
                status, output, errors = self.run_helper(request=request)
                self.assertEqual((status, output, errors), (0, "", ""))
                self.assertEqual(self.provider_calls, 0)
                self.profile_lookup.assert_not_called()

    def test_missing_or_invalid_allowlist_never_acquires_a_token(self):
        for document in ({}, {"devops": []}, {"devops": {"git_urls": URL}},
                         {"devops": {"git_urls": [URL + "?key=synthetic"]}},
                         {"devops": {"git_urls": [URL.replace("https://", "https://user@")]}},
                         {"devops": {"git_urls": [URL.replace("sample-repo", "different-repo")]}}):
            with self.subTest(document=document):
                self.document = document
                self.assertEqual(self.run_helper(), (0, "", ""))
                self.assertEqual(self.provider_calls, 0)

    def test_no_password_fallback_without_authtype(self):
        status, output, errors = self.run_helper(request=REQUEST.replace("capability[]=authtype\n", ""))
        self.assertEqual(status, 1)
        self.assertEqual(output, "quit=true\n\n")
        self.assertIn("authtype capability", errors)
        self.assertEqual(self.provider_calls, 0)

    def test_store_and_erase_do_not_read_or_persist_offered_credentials(self):
        for operation in ("store", "erase"):
            with patch.object(AZURE, "_read_git_request") as read_request, \
                    patch.object(AZURE, "_write_document") as write_document:
                self.assertEqual(self.run_helper(operation, "password=" + FAKE_TOKEN), (0, "", ""))
                read_request.assert_not_called()
                write_document.assert_not_called()
                self.assertEqual(self.provider_calls, 0)

    def test_capability_is_safe_without_profile_access(self):
        self.assertEqual(self.run_helper("capability"), (0, "version 0\ncapability authtype\n", ""))
        self.assertEqual(self.provider_calls, 0)

    def test_silent_session_failures_return_static_errors_only(self):
        for result in (None, {"error_description": "synthetic-sensitive-provider-data"},
                       {"access_token": "invalid\ntoken"}):
            with self.subTest(result=result):
                self.result = result
                status, output, errors = self.run_helper()
                self.assertEqual(status, 1)
                self.assertEqual(output, "quit=true\n\n")
                self.assertEqual(errors, "Azure Git authentication could not reuse the configured silent session.\n")

    def test_provider_exceptions_do_not_escape(self):
        @contextmanager
        def fail(profile):
            print(FAKE_TOKEN)
            raise RuntimeError(FAKE_TOKEN)
            yield

        self.application = fail
        status, output, errors = self.run_helper()
        self.assertEqual(status, 1)
        self.assertNotIn(FAKE_TOKEN, output + errors)
        self.assertEqual(output, "quit=true\n\n")

    def test_malformed_or_oversized_input_does_not_acquire_a_token(self):
        for request in ("x" * (AZURE.MAX_GIT_CREDENTIAL_INPUT + 1),
                        "protocol=https\nprotocol=https\n\n", "host=dev.azure.com\0x\n\n",
                        "invalid-line\n\n"):
            with self.subTest(size=len(request)):
                status, output, errors = self.run_helper(request=request)
                self.assertEqual(status, 1)
                self.assertEqual(output, "quit=true\n\n")
                self.assertEqual(self.provider_calls, 0)
                self.assertNotIn(FAKE_TOKEN, output + errors)

    def test_configure_preserves_allowlist_unless_replacement_is_explicit(self):
        with tempfile.TemporaryDirectory() as temporary:
            for arguments, expected_urls in (([], [URL]), (["--git-url", URL.replace("sample-repo", "second-repo")], [URL.replace("sample-repo", "second-repo")])):
                with patch.object(AZURE, "_read_document", return_value=self.document), \
                        patch.object(AZURE, "_write_document") as write, \
                        redirect_stdout(io.StringIO()):
                    status = AZURE._configure(["--username", "you@example.com", "--config-dir", temporary, *arguments])
                self.assertEqual(status, 0)
                self.assertEqual(write.call_args.args[0]["devops"]["git_urls"], expected_urls)

    def test_git_accepts_the_fake_bearer_protocol(self):
        capability = subprocess.run(["git", "credential", "capability"], capture_output=True, text=True)
        if capability.returncode or "capability authtype" not in capability.stdout.splitlines():
            self.skipTest("Installed Git does not advertise authtype")
        with tempfile.TemporaryDirectory() as temporary:
            helper = Path(temporary) / "git-credential-synthetic"
            helper.write_text("#!/bin/sh\ncase \"$1\" in\ncapability) printf 'version 0\\ncapability authtype\\n' ;;\nget) cat >/dev/null; printf 'capability[]=authtype\\nauthtype=bearer\\ncredential=" + FAKE_TOKEN + "\\nephemeral=true\\n\\n' ;;\nesac\n")
            helper.chmod(0o700)
            # All credential output is captured and contains synthetic data only.
            result = subprocess.run(["git", "-c", "credential.helper=", "-c", "credential.helper=" + str(helper),
                                     "-c", "credential.useHttpPath=true", "credential", "fill"],
                                    input=REQUEST, capture_output=True, text=True,
                                    env={"PATH": os.environ["PATH"], "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0"},
                                    cwd=temporary, timeout=30)
            self.assertEqual(result.returncode, 0)
            self.assertIn("authtype=bearer\n", result.stdout)
            self.assertIn("credential=" + FAKE_TOKEN + "\n", result.stdout)
            self.assertIn("ephemeral=1\n", result.stdout)
            self.assertNotIn("password=", result.stdout)


if __name__ == "__main__":
    unittest.main()
