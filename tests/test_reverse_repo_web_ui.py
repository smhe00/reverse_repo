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

    def test_live_certification_requires_exact_phrase_and_fixed_backend_action(self):
        preflight = self.application.run_action("live_cert_preflight")
        self.assertTrue(preflight["ok"])
        command, _ = self.runner.calls[-1]
        self.assertEqual(command[-2:], ["-Action", "LiveCertPreflight"])
        with self.assertRaises(PermissionError):
            self.application.run_action("live_cert", "live 1000")
        result = self.application.run_action("live_cert", "LIVE 1000")
        self.assertTrue(result["ok"])
        command, kwargs = self.runner.calls[-1]
        self.assertEqual(
            command[-4:],
            ["-Action", "LiveCert", "-LiveCertConfirmation", "LIVE 1000"],
        )
        self.assertNotIn("--maximum-principal-yuan", command)
        self.assertIs(kwargs["shell"], False)

    def test_live_cert_reset_requires_exact_phrase_and_dispatches_reset(self):
        with self.assertRaises(PermissionError):
            self.application.run_action("live_cert_reset", "revoke live cert")
        result = self.application.run_action(
            "live_cert_reset",
            "REVOKE LIVE CERT",
        )
        self.assertTrue(result["ok"])
        command, kwargs = self.runner.calls[-1]
        self.assertEqual(command[-2:], ["-Action", "LiveCertReset"])
        self.assertIs(kwargs["shell"], False)

    def test_status_output_is_reduced_to_structured_task_fields(self):
        output = """
TaskName              : miniQMT Reverse Repo First
Installed             : True
State                 : Ready
StrategyParameters    : first_order=09:30:42; cash_usage=90%
Schedule              : 周一至周五 09:28:00
NextRunTime           : 2026/8/5 9:28:00

TaskName              : miniQMT Reverse Repo Second
Installed             : True
State                 : Disabled
StrategyParameters    : second_start=15:10:00; cash_usage=100%
ScheduleMatchesConfig : True
LastResult            : 尚未运行 (0x41303)
"""
        tasks = web_ui._parse_live_task_status(output)
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0]["state"], "Ready")
        self.assertEqual(tasks[0]["next_run_time"], "2026/8/5 9:28:00")
        self.assertEqual(tasks[1]["state"], "Disabled")
        self.assertEqual(tasks[1]["schedule_matches_config"], "True")
        self.assertNotIn("output", json.dumps(tasks))
        certification = web_ui._parse_certification_status(
            "Certification basis: live-channel certification; "
            "does not include fault-injection recovery proof."
        )
        self.assertEqual(certification["valid"], "true")
        self.assertEqual(certification["kind"], "live_channel")

    def test_shutdown_requires_idle_and_prevents_new_operations(self):
        with self.assertRaises(PermissionError):
            self.application.prepare_shutdown("wrong")
        self.application._operation_lock.acquire()
        try:
            with self.assertRaises(RuntimeError):
                self.application.prepare_shutdown("CLOSE UI")
        finally:
            self.application._operation_lock.release()
        self.application.prepare_shutdown("CLOSE UI")
        with self.assertRaises(RuntimeError):
            self.application.run_action("status")

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

            shutdown_request = urllib.request.Request(
                origin + "/api/shutdown",
                data=json.dumps({"confirmation": "CLOSE UI"}).encode(),
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "X-RR-Token": token,
                    "Origin": origin,
                },
            )
            with urllib.request.urlopen(shutdown_request, timeout=3) as response:
                shutdown_payload = json.load(response)
            self.assertTrue(shutdown_payload["ok"])
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
        finally:
            if thread.is_alive():
                server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
