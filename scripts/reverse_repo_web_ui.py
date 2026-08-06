from __future__ import annotations

import argparse
import hmac
import json
import locale
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import webbrowser
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Mapping

from repo_execution_core import reverse_repo_strategy_config


MAX_REQUEST_BYTES = 64 * 1024
MAX_OUTPUT_CHARACTERS = 200_000
LOOPBACK_HOST = "127.0.0.1"


@dataclass(frozen=True)
class ActionSpec:
    manager_action: str | None = None
    verify: bool = False
    confirmation: str | None = None
    timeout_seconds: int = 120


ACTION_SPECS: dict[str, ActionSpec] = {
    "status": ActionSpec(manager_action="Status", timeout_seconds=30),
    "off": ActionSpec(
        manager_action="Disable",
        confirmation="DISABLE LIVE",
    ),
    "on": ActionSpec(
        manager_action="Enable",
        confirmation="ENABLE LIVE",
        timeout_seconds=300,
    ),
    "live_cert": ActionSpec(
        manager_action="LiveCert",
        confirmation="LIVE 1000",
        timeout_seconds=420,
    ),
    "live_cert_preflight": ActionSpec(
        manager_action="LiveCertPreflight",
        timeout_seconds=360,
    ),
    "live_cert_status": ActionSpec(
        manager_action="LiveCertStatus",
        timeout_seconds=30,
    ),
    "live_cert_reset": ActionSpec(
        manager_action="LiveCertReset",
        confirmation="REVOKE LIVE CERT",
        timeout_seconds=60,
    ),
    "mail_test": ActionSpec(manager_action="TestMail", timeout_seconds=60),
}

STATUS_FIELD_NAMES = {
    "TaskName": "task_name",
    "Installed": "installed",
    "State": "state",
    "StrategyParameters": "strategy_parameters",
    "EnabledByConfig": "enabled_by_config",
    "Schedule": "schedule",
    "ScheduleMatchesConfig": "schedule_matches_config",
    "LiveEnableSnapshot": "live_enable_snapshot",
    "NextRunTime": "next_run_time",
    "LastRunTime": "last_run_time",
    "LastResult": "last_result",
}


def _decode_process_output(value: bytes) -> str:
    if not value:
        return ""
    encodings = ["utf-8", locale.getpreferredencoding(False), "gb18030"]
    for encoding in dict.fromkeys(encodings):
        try:
            return value.decode(encoding, errors="strict")
        except (LookupError, UnicodeDecodeError):
            continue
    return value.decode("utf-8", errors="replace")


def _windows_powershell() -> Path:
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    path = system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not path.is_file():
        raise RuntimeError(f"Windows PowerShell 5.1 is missing: {path}")
    return path


def _parse_live_task_status(output: str) -> list[dict[str, str]]:
    tasks: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for raw_line in output.splitlines():
        if ":" not in raw_line:
            continue
        raw_key, raw_value = raw_line.split(":", 1)
        field = STATUS_FIELD_NAMES.get(raw_key.strip())
        if field is None:
            continue
        if field == "task_name":
            if current is not None:
                tasks.append(current)
            current = {field: raw_value.strip()}
        elif current is not None:
            current[field] = raw_value.strip()
    if current is not None:
        tasks.append(current)
    return tasks


def _parse_certification_status(output: str) -> dict[str, str]:
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if line.startswith("Certification basis: live-channel"):
            return {
                "kind": "live_channel",
                "valid": "true",
                "summary": "实盘通道认证：有效",
                "scope": "固定1000元真实通道；不含故障注入恢复证明",
            }
        if line.startswith("实盘通道认证："):
            if "强制启用" in line:
                return {
                    "kind": "live_channel",
                    "valid": "forced",
                    "summary": line,
                    "scope": "强制启用：跳过实盘认证证书检查，直接交易",
                }
            valid = "有效" in line and "无效" not in line
            return {
                "kind": "live_channel",
                "valid": str(valid).lower(),
                "summary": line,
                "scope": "固定1000元真实通道；不含故障注入恢复证明",
            }
    return {
        "kind": "live_channel",
        "valid": "false",
        "summary": "实盘通道认证：不存在。",
        "scope": "固定1000元真实通道；不含故障注入恢复证明",
    }


class LocalUiApplication:
    def __init__(
        self,
        repo_root: Path,
        *,
        process_runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.scripts_root = self.repo_root / "scripts"
        self.web_root = self.repo_root / "web"
        self.runtime_config = self.repo_root / "config" / "runtime.local.json"
        self.default_config = self.repo_root / "config" / "runtime.example.json"
        self.manager = self.scripts_root / "manage_reverse_repo_tasks.ps1"
        self.configurator = self.scripts_root / "configure_reverse_repo_strategy.ps1"
        self.verifier = self.repo_root / "verify.ps1"
        self.powershell = _windows_powershell()
        self._process_runner = process_runner
        self._operation_lock = threading.Lock()
        self._closing = False
        for required in (
            self.runtime_config,
            self.default_config,
            self.manager,
            self.configurator,
            self.verifier,
            self.web_root / "index.html",
            self.web_root / "app.js",
            self.web_root / "style.css",
        ):
            if not required.is_file():
                raise RuntimeError(f"Local UI dependency is missing: {required}")

    def configuration_model(self) -> dict[str, object]:
        return {
            "current": reverse_repo_strategy_config(self.runtime_config),
            "defaults": reverse_repo_strategy_config(self.default_config),
            "limits": {
                "first_execution_time": (
                    "09:30:00-11:28:00 or 13:00:00-15:28:00"
                ),
                "second_execution_time": (
                    "09:30:00-11:29:59 or 13:00:00-15:29:59; "
                    "at least five minutes after the first time"
                ),
                "cash_usage_ratio": "0 through 1 inclusive",
            },
        }

    def run_action(self, action: str, confirmation: object = None) -> dict[str, object]:
        spec = ACTION_SPECS.get(str(action))
        if spec is None:
            raise ValueError("Unsupported UI action")
        if spec.confirmation is not None and not hmac.compare_digest(
            str(confirmation or ""), spec.confirmation
        ):
            raise PermissionError("Action confirmation did not match")
        if not self._operation_lock.acquire(blocking=False):
            raise RuntimeError("Another local UI operation is still running")
        try:
            if self._closing:
                raise RuntimeError("The local UI is shutting down")
            if spec.verify:
                command = self._powershell_file(self.verifier)
            else:
                arguments = ["-Action", str(spec.manager_action)]
                if action == "live_cert":
                    arguments.extend(
                        ["-LiveCertConfirmation", "LIVE 1000"]
                    )
                command = self._powershell_file(self.manager, *arguments)
            result = self._run(command, timeout_seconds=spec.timeout_seconds)
            payload = self._result_payload(result)
            if action == "status":
                payload["tasks"] = _parse_live_task_status(
                    str(payload["output"])
                )
                payload["certification"] = _parse_certification_status(
                    str(payload["output"])
                )
            return payload
        finally:
            self._operation_lock.release()

    def apply_configuration(
        self,
        values: Mapping[str, object],
        confirmation: object,
    ) -> dict[str, object]:
        if not hmac.compare_digest(str(confirmation or ""), "SAVE PARAMETERS"):
            raise PermissionError("Parameter confirmation did not match")
        allowed = {
            "first_execution_time",
            "first_cash_usage_ratio",
            "second_execution_time",
            "second_cash_usage_ratio",
        }
        if set(values) != allowed:
            raise ValueError("Exactly four strategy parameters are required")
        normalized = {
            key: str(values[key]).strip()
            for key in sorted(allowed)
        }
        if any(not value or len(value) > 32 for value in normalized.values()):
            raise ValueError("A strategy parameter is empty or too long")
        self._validate_candidate(normalized)
        if not self._operation_lock.acquire(blocking=False):
            raise RuntimeError("Another local UI operation is still running")
        try:
            if self._closing:
                raise RuntimeError("The local UI is shutting down")
            command = self._powershell_file(
                self.configurator,
                "-FirstExecutionTime",
                normalized["first_execution_time"],
                "-FirstCashUsageRatio",
                normalized["first_cash_usage_ratio"],
                "-SecondExecutionTime",
                normalized["second_execution_time"],
                "-SecondCashUsageRatio",
                normalized["second_cash_usage_ratio"],
                "-NonInteractiveConfirmed",
            )
            result = self._run(command, timeout_seconds=420)
            payload = self._result_payload(result)
            payload["configuration"] = self.configuration_model()
            return payload
        finally:
            self._operation_lock.release()

    def prepare_shutdown(self, confirmation: object) -> None:
        if not hmac.compare_digest(str(confirmation or ""), "CLOSE UI"):
            raise PermissionError("Shutdown confirmation did not match")
        if not self._operation_lock.acquire(blocking=False):
            raise RuntimeError(
                "A background operation is still running; wait for Idle before closing"
            )
        try:
            if self._closing:
                raise RuntimeError("The local UI is already shutting down")
            self._closing = True
        finally:
            self._operation_lock.release()

    def read_static(self, route: str) -> tuple[bytes, str]:
        files = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/index.html": ("index.html", "text/html; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
            "/style.css": ("style.css", "text/css; charset=utf-8"),
        }
        selected = files.get(route)
        if selected is None:
            raise FileNotFoundError(route)
        return (self.web_root / selected[0]).read_bytes(), selected[1]

    def _validate_candidate(self, values: Mapping[str, str]) -> None:
        payload = json.dumps(dict(values), ensure_ascii=False).encode("utf-8")
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".json") as stream:
                stream.write(payload)
                temporary_name = stream.name
            reverse_repo_strategy_config(Path(temporary_name))
        finally:
            if temporary_name is not None:
                try:
                    Path(temporary_name).unlink()
                except FileNotFoundError:
                    pass

    def _powershell_file(self, path: Path, *arguments: str) -> list[str]:
        return [
            str(self.powershell),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(path),
            *arguments,
        ]

    def _run(
        self,
        command: list[str],
        *,
        timeout_seconds: int,
    ) -> subprocess.CompletedProcess[bytes]:
        creation_flags = 0x08000000 if os.name == "nt" else 0
        try:
            return self._process_runner(
                command,
                cwd=str(self.repo_root),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout_seconds,
                shell=False,
                creationflags=creation_flags,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("The local operation exceeded its safety timeout") from exc

    @staticmethod
    def _result_payload(result: subprocess.CompletedProcess[bytes]) -> dict[str, object]:
        output = _decode_process_output(bytes(result.stdout or b""))
        if len(output) > MAX_OUTPUT_CHARACTERS:
            output = output[-MAX_OUTPUT_CHARACTERS:]
            output = "[earlier output omitted]\n" + output
        return {
            "ok": int(result.returncode) == 0,
            "exit_code": int(result.returncode),
            "output": output,
        }


def make_handler(
    application: LocalUiApplication,
    *,
    token: str,
    expected_origin: str,
) -> type[BaseHTTPRequestHandler]:
    class LocalUiHandler(BaseHTTPRequestHandler):
        server_version = "ReverseRepoLocalUI/1"

        def do_GET(self) -> None:  # noqa: N802
            route = self.path.split("?", 1)[0]
            if route == "/favicon.ico":
                self.send_response(HTTPStatus.NO_CONTENT)
                self._security_headers("text/plain")
                self.end_headers()
                return
            if route == "/api/bootstrap":
                if not self._authorized(require_origin=False):
                    return
                try:
                    status = application.run_action("status")
                    self._json_response(
                        HTTPStatus.OK,
                        {
                            "ok": bool(status["ok"]),
                            "status": status,
                            "configuration": application.configuration_model(),
                            "actions": sorted(ACTION_SPECS),
                        },
                    )
                except Exception as exc:  # noqa: BLE001
                    self._json_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
                return
            try:
                data, content_type = application.read_static(route)
            except FileNotFoundError:
                self._json_error(HTTPStatus.NOT_FOUND, "Not found")
                return
            self.send_response(HTTPStatus.OK)
            self._security_headers(content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:  # noqa: N802
            route = self.path.split("?", 1)[0]
            if not self._authorized(require_origin=True):
                return
            try:
                payload = self._read_json()
                if route == "/api/action":
                    result = application.run_action(
                        str(payload.get("action", "")),
                        payload.get("confirmation"),
                    )
                elif route == "/api/configuration":
                    values = payload.get("values")
                    if not isinstance(values, dict):
                        raise ValueError("Configuration values must be an object")
                    result = application.apply_configuration(
                        values,
                        payload.get("confirmation"),
                    )
                elif route == "/api/shutdown":
                    application.prepare_shutdown(payload.get("confirmation"))
                    result = {
                        "ok": True,
                        "output": "后台操作均为Idle；本机控制台正在关闭。",
                    }
                else:
                    self._json_error(HTTPStatus.NOT_FOUND, "Not found")
                    return
                self._json_response(HTTPStatus.OK, result)
                if route == "/api/shutdown":
                    threading.Thread(
                        target=self.server.shutdown,
                        name="reverse-repo-ui-shutdown",
                        daemon=True,
                    ).start()
            except PermissionError as exc:
                self._json_error(HTTPStatus.FORBIDDEN, str(exc))
            except ValueError as exc:
                self._json_error(HTTPStatus.BAD_REQUEST, str(exc))
            except RuntimeError as exc:
                self._json_error(HTTPStatus.CONFLICT, str(exc))
            except Exception as exc:  # noqa: BLE001
                self._json_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

        def log_message(self, _format: str, *_arguments: object) -> None:
            return

        def _authorized(self, *, require_origin: bool) -> bool:
            if self.client_address[0] != LOOPBACK_HOST:
                self._json_error(HTTPStatus.FORBIDDEN, "Loopback access only")
                return False
            if self.headers.get("Host", "") != expected_origin.removeprefix("http://"):
                self._json_error(HTTPStatus.FORBIDDEN, "Invalid host")
                return False
            supplied = self.headers.get("X-RR-Token", "")
            if not hmac.compare_digest(supplied, token):
                self._json_error(HTTPStatus.UNAUTHORIZED, "Invalid session token")
                return False
            if require_origin and self.headers.get("Origin", "") != expected_origin:
                self._json_error(HTTPStatus.FORBIDDEN, "Invalid request origin")
                return False
            return True

        def _read_json(self) -> dict[str, object]:
            content_type = self.headers.get("Content-Type", "")
            if not content_type.lower().startswith("application/json"):
                raise ValueError("Content-Type must be application/json")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValueError("Invalid Content-Length") from exc
            if length <= 0 or length > MAX_REQUEST_BYTES:
                raise ValueError("Request body size is invalid")
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("Request body is not valid UTF-8 JSON") from exc
            if not isinstance(payload, dict):
                raise ValueError("Request body must be a JSON object")
            return payload

        def _json_error(self, status: HTTPStatus, message: str) -> None:
            self._json_response(status, {"ok": False, "error": message})

        def _json_response(self, status: HTTPStatus, payload: object) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self._security_headers("application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _security_headers(self, content_type: str) -> None:
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; img-src 'self'; base-uri 'none'; "
                "form-action 'none'; frame-ancestors 'none'",
            )

    return LocalUiHandler


def create_server(
    application: LocalUiApplication,
    *,
    port: int,
    token: str,
) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer((LOOPBACK_HOST, int(port)), BaseHTTPRequestHandler)
    server.daemon_threads = True
    actual_port = int(server.server_address[1])
    origin = f"http://{LOOPBACK_HOST}:{actual_port}"
    server.RequestHandlerClass = make_handler(
        application,
        token=token,
        expected_origin=origin,
    )
    return server, origin


def main() -> int:
    parser = argparse.ArgumentParser(description="Local-only reverse-repo web UI")
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("--port must be from 0 through 65535")

    application = LocalUiApplication(Path(args.repo_root))
    token = secrets.token_urlsafe(32)
    server, origin = create_server(application, port=args.port, token=token)
    url = f"{origin}/#token={token}"
    print("reverse_repo local UI is ready.")
    print(f"Open: {url}")
    print("It listens only on 127.0.0.1. Press Ctrl+C to stop it.")
    if not args.no_browser:
        webbrowser.open(url, new=1, autoraise=True)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nLocal UI stopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
