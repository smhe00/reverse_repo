from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import reverse_repo_web_ui as web_ui  # noqa: E402


VALID_CONFIG = {
    "first_execution_time": "09:30:42",
    "first_cash_usage_ratio": 0.9,
    "second_execution_time": "15:10:00",
    "second_cash_usage_ratio": 1.0,
}


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, command: list[str], **kwargs: object):
        self.calls.append((list(command), dict(kwargs)))
        return subprocess.CompletedProcess(command, 0, stdout=b"operation ok\n")


class ReverseRepoWebUiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        for name in ("scripts", "config", "web"):
            (self.root / name).mkdir()
        for relative in (
            "scripts/manage_reverse_repo_tasks.ps1",
            "scripts/configure_reverse_repo_strategy.ps1",
            "verify.ps1",
        ):
            (self.root / relative).write_text("exit 0\n", encoding="utf-8")
        for name in ("index.html", "app.js", "style.css"):
            (self.root / "web" / name).write_text(name, encoding="utf-8")
        for name in ("runtime.local.json", "runtime.example.json"):
            (self.root / "config" / name).write_text(
                json.dumps(VALID_CONFIG),
                encoding="utf-8",
            )
        self.runner = RecordingRunner()
        with mock.patch.object(web_ui, "_windows_powershell", return_value=Path("pwsh5.exe")):
            self.application = web_ui.LocalUiApplication(
                self.root,
                process_runner=self.runner,
            )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_configuration_response_exposes_only_four_strategy_values(self):
        model = self.application.configuration_model()
        self.assertEqual(set(model["current"]), set(VALID_CONFIG))
        serialized = json.dumps(model)
        self.assertNotIn("qmt_path", serialized)
        self.assertNotIn("account", serialized)
        self.assertNotIn("python_path", serialized)

    def test_actions_are_whitelisted_confirmed_and_never_use_a_shell(self):
        with self.assertRaises(ValueError):
            self.application.run_action("arbitrary-command")
        with self.assertRaises(PermissionError):
            self.application.run_action("on", "wrong")
        result = self.application.run_action("on", "ENABLE LIVE")
        self.assertTrue(result["ok"])
        command, kwargs = self.runner.calls[-1]
        self.assertEqual(command[-2:], ["-Action", "Enable"])
        self.assertIs(kwargs["shell"], False)
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)

    def test_configuration_requires_exact_fields_and_explicit_confirmation(self):
        values = {key: str(value) for key, value in VALID_CONFIG.items()}
        values["first_cash_usage_ratio"] = "0.8"
        with self.assertRaises(PermissionError):
            self.application.apply_configuration(values, "wrong")
        with self.assertRaises(ValueError):
            self.application.apply_configuration(
                {**values, "unexpected": "value"},
                "SAVE PARAMETERS",
            )
        invalid = dict(values)
        invalid["first_cash_usage_ratio"] = "1.1"
        with self.assertRaises(Exception):
            self.application.apply_configuration(
                invalid,
                "SAVE PARAMETERS",
            )
        result = self.application.apply_configuration(
            values,
            "SAVE PARAMETERS",
        )
        self.assertTrue(result["ok"])
        command, kwargs = self.runner.calls[-1]
        self.assertIn("-NonInteractiveConfirmed", command)
        self.assertIn("-FirstExecutionTime", command)
        self.assertIn("09:30:42", command)
        self.assertIs(kwargs["shell"], False)

    def test_http_server_requires_token_and_post_origin(self):
        token = "unit-test-token"
        server, origin = web_ui.create_server(
            self.application,
            port=0,
            token=token,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(origin + "/", timeout=3) as response:
                self.assertEqual(response.status, 200)
                self.assertIn("default-src 'none'", response.headers["Content-Security-Policy"])

            with self.assertRaises(urllib.error.HTTPError) as unauthorized:
                urllib.request.urlopen(origin + "/api/bootstrap", timeout=3)
            self.assertEqual(unauthorized.exception.code, 401)

            request = urllib.request.Request(
                origin + "/api/bootstrap",
                headers={"X-RR-Token": token},
            )
            with urllib.request.urlopen(request, timeout=3) as response:
                payload = json.load(response)
            self.assertTrue(payload["ok"])

            body = json.dumps({"action": "status"}).encode()
            request = urllib.request.Request(
                origin + "/api/action",
                data=body,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "X-RR-Token": token,
                    "Origin": "http://evil.invalid",
                },
            )
            with self.assertRaises(urllib.error.HTTPError) as forbidden:
                urllib.request.urlopen(request, timeout=3)
            self.assertEqual(forbidden.exception.code, 403)

            with self.assertRaises(urllib.error.HTTPError) as traversal:
                urllib.request.urlopen(origin + "/../config/runtime.local.json", timeout=3)
            self.assertEqual(traversal.exception.code, 404)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
