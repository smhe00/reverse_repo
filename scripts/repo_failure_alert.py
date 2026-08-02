from __future__ import annotations

import argparse
import hashlib
import json
import multiprocessing
import os
import smtplib
import ssl
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Protocol

ALERT_PASSWORD_ENV = "MINIQMT_ALERT_SMTP_PASSWORD"
ALERT_CONFIG_SCHEMA_VERSION = 1
MAXIMUM_ALERT_ATTEMPTS = 5
MAXIMUM_ALERT_TIMEOUT_SECONDS = 30.0


class AlertConfigurationError(RuntimeError):
    """The local failure-alert configuration is missing or unsafe."""


class AlertDeliveryError(RuntimeError):
    """All bounded attempts to deliver a failure alert failed."""


class FailureNotifier(Protocol):
    def send(self, alert: FailureAlert) -> None: ...


class JournalLike(Protocol):
    path: Path
    strategy: str
    trade_date: str
    payload: dict[str, Any]

    def update_data(self, **values: object) -> None: ...


@dataclass(frozen=True)
class FailureAlert:
    strategy: str
    trade_date: str
    environment: str
    state: str
    event: str
    reason: str
    unresolved_order: bool
    journal_path: str
    occurred_at: str
    error_type: str | None = None
    error_message: str | None = None

    @property
    def key(self) -> str:
        material = {
            "strategy": self.strategy,
            "trade_date": self.trade_date,
            "state": self.state,
            "event": self.event,
            "reason": self.reason,
            "unresolved_order": self.unresolved_order,
        }
        payload = json.dumps(
            material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class SmtpAlertConfig:
    to_addresses: tuple[str, ...]
    from_address: str
    smtp_host: str
    smtp_port: int
    smtp_security: str
    smtp_username: str
    timeout_seconds: float
    attempts: int


@dataclass(frozen=True)
class AlertDelivery:
    key: str
    status: str
    attempted_at: str
    attempts: int
    error: str | None = None


class SmtpFailureNotifier:
    def __init__(
        self,
        config: SmtpAlertConfig,
        *,
        password: str,
    ) -> None:
        self.config = config
        self._password = str(password)
        self.last_attempts = 0

    def send(self, alert: FailureAlert) -> None:
        errors: list[str] = []
        for attempt in range(1, self.config.attempts + 1):
            self.last_attempts = attempt
            error = _send_attempt_with_hard_timeout(
                self.config,
                self._password,
                alert,
            )
            if error is None:
                return
            errors.append(f"attempt {attempt}: {error}")
            if attempt < self.config.attempts:
                time.sleep(min(0.25 * (2 ** (attempt - 1)), 2.0))
        raise AlertDeliveryError(
            "SMTP alert delivery failed after bounded retries: "
            + " | ".join(errors)
        )

def load_smtp_failure_notifier(
    config_path: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> SmtpFailureNotifier:
    path = Path(config_path).resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AlertConfigurationError(
            f"failure-alert configuration is unreadable: {path}"
        ) from exc
    if not isinstance(payload, dict):
        raise AlertConfigurationError(
            "failure-alert configuration root must be an object"
        )
    if payload.get("schema_version") != ALERT_CONFIG_SCHEMA_VERSION:
        raise AlertConfigurationError(
            "unexpected failure-alert configuration schema"
        )
    if payload.get("enabled") is not True:
        raise AlertConfigurationError("failure-alert email is not enabled")
    if payload.get("transport") != "smtp":
        raise AlertConfigurationError(
            "only the authenticated SMTP alert transport is supported"
        )
    raw_to = payload.get("to")
    if isinstance(raw_to, str):
        raw_to = [raw_to]
    if (
        not isinstance(raw_to, list)
        or not raw_to
        or not all(isinstance(value, str) for value in raw_to)
    ):
        raise AlertConfigurationError(
            "failure-alert recipient list is missing"
        )
    to_addresses = tuple(_validated_email(value) for value in raw_to)
    from_address = _validated_email(payload.get("from"))
    smtp_host = _validated_host(payload.get("smtp_host"))
    smtp_port = _bounded_int(
        payload.get("smtp_port"),
        field="smtp_port",
        minimum=1,
        maximum=65535,
    )
    smtp_security = str(payload.get("smtp_security", "")).strip().lower()
    if smtp_security not in {"starttls", "ssl"}:
        raise AlertConfigurationError(
            "smtp_security must be 'starttls' or 'ssl'; plaintext is forbidden"
        )
    smtp_username = str(payload.get("smtp_username", "")).strip()
    if "\r" in smtp_username or "\n" in smtp_username:
        raise AlertConfigurationError("smtp_username contains a newline")
    timeout_seconds = _bounded_float(
        payload.get("timeout_seconds", 10.0),
        field="timeout_seconds",
        minimum=1.0,
        maximum=MAXIMUM_ALERT_TIMEOUT_SECONDS,
    )
    attempts = _bounded_int(
        payload.get("attempts", 3),
        field="attempts",
        minimum=1,
        maximum=MAXIMUM_ALERT_ATTEMPTS,
    )
    environment = os.environ if environ is None else environ
    password = str(environment.get(ALERT_PASSWORD_ENV, ""))
    if smtp_username and not password:
        raise AlertConfigurationError(
            f"SMTP password is missing from {ALERT_PASSWORD_ENV}"
        )
    return SmtpFailureNotifier(
        SmtpAlertConfig(
            to_addresses=to_addresses,
            from_address=from_address,
            smtp_host=smtp_host,
            smtp_port=smtp_port,
            smtp_security=smtp_security,
            smtp_username=smtp_username,
            timeout_seconds=timeout_seconds,
            attempts=attempts,
        ),
        password=password,
    )


def load_optional_smtp_failure_notifier(
    config_path: Path | None,
    *,
    environ: Mapping[str, str] | None = None,
) -> tuple[SmtpFailureNotifier | None, str | None]:
    """Load best-effort email alerting without blocking a strategy."""
    if config_path is None:
        return None, "failure-alert email is not configured"
    path = Path(config_path)
    if not path.is_file():
        return None, f"failure-alert configuration does not exist: {path}"
    try:
        notifier = load_smtp_failure_notifier(path, environ=environ)
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"
    return notifier, None


def notify_journal_failure(
    notifier: FailureNotifier | None,
    journal: JournalLike,
    *,
    environment: str,
    state: str,
    event: str,
    reason: str,
    unresolved_order: bool,
    error: BaseException | Mapping[str, object] | None = None,
) -> AlertDelivery:
    alert = FailureAlert(
        strategy=str(journal.strategy),
        trade_date=str(journal.trade_date),
        environment=str(environment),
        state=str(state),
        event=str(event),
        reason=_bounded_text(reason, 2_000),
        unresolved_order=bool(unresolved_order),
        journal_path=str(Path(journal.path).resolve()),
        occurred_at=datetime.now().astimezone().isoformat(),
        error_type=_error_type(error),
        error_message=_error_message(error),
    )
    previous = _previous_delivery(journal.payload)
    if (
        previous is not None
        and previous.get("key") == alert.key
        and previous.get("status") == "sent"
    ):
        return AlertDelivery(
            key=alert.key,
            status="already_sent",
            attempted_at=str(previous.get("attempted_at", "")),
            attempts=int(previous.get("attempts", 0)),
        )

    attempted_at = datetime.now().astimezone().isoformat()
    if notifier is None:
        delivery = AlertDelivery(
            key=alert.key,
            status="disabled",
            attempted_at=attempted_at,
            attempts=0,
            error="no notifier configured",
        )
    else:
        try:
            notifier.send(alert)
        except Exception as exc:  # noqa: BLE001
            delivery = AlertDelivery(
                key=alert.key,
                status="failed",
                attempted_at=attempted_at,
                attempts=_notifier_attempts(notifier),
                error=_bounded_text(
                    f"{type(exc).__name__}: {exc}",
                    2_000,
                ),
            )
        else:
            delivery = AlertDelivery(
                key=alert.key,
                status="sent",
                attempted_at=attempted_at,
                attempts=_notifier_attempts(notifier),
            )
    try:
        journal.update_data(failure_alert=asdict(delivery))
    except Exception:  # noqa: BLE001
        # Alerting is a side effect after fail-closed state persistence.
        # It must never reopen trading or replace the original exit result.
        return delivery
    return delivery


def send_standalone_failure(
    notifier: FailureNotifier,
    *,
    strategy: str,
    trade_date: str,
    environment: str,
    reason: str,
    journal_path: Path,
    error: BaseException | None = None,
) -> None:
    alert = FailureAlert(
        strategy=strategy,
        trade_date=trade_date,
        environment=environment,
        state="startup_failure",
        event="startup_failure",
        reason=_bounded_text(reason, 2_000),
        unresolved_order=False,
        journal_path=str(Path(journal_path).resolve()),
        occurred_at=datetime.now().astimezone().isoformat(),
        error_type=_error_type(error),
        error_message=_error_message(error),
    )
    notifier.send(alert)


def _build_message(
    config: SmtpAlertConfig,
    alert: FailureAlert,
) -> EmailMessage:
    subject = (
        "[miniQMT][需人工检查]"
        f"[{alert.environment.upper()}] {alert.strategy} "
        f"{alert.trade_date}"
    )
    message = EmailMessage()
    message["From"] = config.from_address
    message["To"] = ", ".join(config.to_addresses)
    message["Subject"] = _clean_header(subject)
    lines = [
        "miniQMT 逆回购执行器已安全停止，需要人工检查。",
        "",
        f"策略：{alert.strategy}",
        f"交易日：{alert.trade_date}",
        f"环境：{alert.environment}",
        f"状态：{alert.state}",
        f"事件：{alert.event}",
        f"原因：{alert.reason}",
        (
            "是否存在未决委托："
            + ("是，请先到券商端查单" if alert.unresolved_order else "否/未检测到")
        ),
        f"发生时间：{alert.occurred_at}",
        f"本机日志：{alert.journal_path}",
    ]
    if alert.error_type:
        lines.extend(
            [
                f"异常类型：{alert.error_type}",
                f"异常信息：{alert.error_message or ''}",
            ]
        )
    lines.extend(
        [
            "",
            "安全约束：程序没有自动补单、追价、改价或扩大金额。",
        ]
    )
    message.set_content("\n".join(lines), charset="utf-8")
    return message


def _send_attempt_with_hard_timeout(
    config: SmtpAlertConfig,
    password: str,
    alert: FailureAlert,
) -> str | None:
    context = multiprocessing.get_context("spawn")
    parent_connection, child_connection = context.Pipe(duplex=False)
    process = context.Process(
        target=_smtp_attempt_worker,
        args=(child_connection, config, password, alert),
        daemon=True,
    )
    try:
        process.start()
        child_connection.close()
        process.join(config.timeout_seconds + 1.0)
        if process.is_alive():
            process.terminate()
            process.join(2.0)
            if process.is_alive():
                process.kill()
                process.join(2.0)
            return (
                f"HardTimeout: SMTP attempt exceeded "
                f"{config.timeout_seconds:.1f} seconds"
            )
        if parent_connection.poll():
            ok, detail = parent_connection.recv()
            return None if ok else str(detail)
        if process.exitcode == 0:
            return "SMTP worker exited without a delivery result"
        return f"SMTP worker exited with code {process.exitcode}"
    except (OSError, RuntimeError) as exc:
        return f"{type(exc).__name__}: {exc}"
    finally:
        child_connection.close()
        parent_connection.close()
        if process.is_alive():
            process.terminate()
            process.join(2.0)


def _smtp_attempt_worker(
    connection: Any,
    config: SmtpAlertConfig,
    password: str,
    alert: FailureAlert,
) -> None:
    try:
        _smtp_send_once(config, password, _build_message(config, alert))
    except Exception as exc:  # noqa: BLE001
        try:
            connection.send(
                (
                    False,
                    f"{type(exc).__name__}: {_bounded_text(exc, 1_000)}",
                )
            )
        finally:
            connection.close()
        return
    connection.send((True, "sent"))
    connection.close()


def _smtp_send_once(
    config: SmtpAlertConfig,
    password: str,
    message: EmailMessage,
) -> None:
    context = ssl.create_default_context()
    if config.smtp_security == "ssl":
        with smtplib.SMTP_SSL(
            config.smtp_host,
            config.smtp_port,
            timeout=config.timeout_seconds,
            context=context,
        ) as client:
            _smtp_authenticate_and_send(
                client,
                config,
                password,
                message,
            )
        return
    with smtplib.SMTP(
        config.smtp_host,
        config.smtp_port,
        timeout=config.timeout_seconds,
    ) as client:
        client.ehlo()
        client.starttls(context=context)
        client.ehlo()
        _smtp_authenticate_and_send(
            client,
            config,
            password,
            message,
        )


def _smtp_authenticate_and_send(
    client: smtplib.SMTP,
    config: SmtpAlertConfig,
    password: str,
    message: EmailMessage,
) -> None:
    if config.smtp_username:
        client.login(config.smtp_username, password)
    client.send_message(
        message,
        from_addr=config.from_address,
        to_addrs=list(config.to_addresses),
    )


def _previous_delivery(
    payload: Mapping[str, object],
) -> Mapping[str, object] | None:
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return None
    previous = data.get("failure_alert")
    return previous if isinstance(previous, Mapping) else None


def _notifier_attempts(notifier: FailureNotifier) -> int:
    last_attempts = getattr(notifier, "last_attempts", None)
    if last_attempts is not None:
        return int(last_attempts)
    config = getattr(notifier, "config", None)
    return int(getattr(config, "attempts", 1))


def _validated_email(value: object) -> str:
    text = str(value or "").strip()
    if "\r" in text or "\n" in text:
        raise AlertConfigurationError("email address contains a newline")
    _, parsed = parseaddr(text)
    if parsed != text or "@" not in parsed or parsed.startswith("@"):
        raise AlertConfigurationError(f"invalid email address: {text!r}")
    return parsed


def _validated_host(value: object) -> str:
    text = str(value or "").strip()
    if (
        not text
        or len(text) > 253
        or "\r" in text
        or "\n" in text
        or "://" in text
        or any(char.isspace() for char in text)
    ):
        raise AlertConfigurationError("invalid SMTP host")
    return text


def _bounded_int(
    value: object,
    *,
    field: str,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise AlertConfigurationError(f"{field} must be an integer") from exc
    if not minimum <= parsed <= maximum:
        raise AlertConfigurationError(
            f"{field} must be between {minimum} and {maximum}"
        )
    return parsed


def _bounded_float(
    value: object,
    *,
    field: str,
    minimum: float,
    maximum: float,
) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise AlertConfigurationError(f"{field} must be numeric") from exc
    if not minimum <= parsed <= maximum:
        raise AlertConfigurationError(
            f"{field} must be between {minimum} and {maximum}"
        )
    return parsed


def _bounded_text(value: object, maximum: int) -> str:
    text = str(value).replace("\x00", "").strip()
    return text[:maximum]


def _clean_header(value: object) -> str:
    return _bounded_text(str(value).replace("\r", " ").replace("\n", " "), 240)


def _error_type(
    error: BaseException | Mapping[str, object] | None,
) -> str | None:
    if error is None:
        return None
    if isinstance(error, BaseException):
        return type(error).__name__
    return _bounded_text(error.get("type", "error"), 200)


def _error_message(
    error: BaseException | Mapping[str, object] | None,
) -> str | None:
    if error is None:
        return None
    if isinstance(error, BaseException):
        return _bounded_text(str(error), 2_000)
    return _bounded_text(error.get("message", ""), 2_000)


def _test_alert(notifier: FailureNotifier) -> None:
    now = datetime.now().astimezone()
    notifier.send(
        FailureAlert(
            strategy="email_alert_configuration_test",
            trade_date=now.date().isoformat(),
            environment="configuration_test",
            state="test",
            event="test",
            reason="This is a configuration test; no trading error occurred.",
            unresolved_order=False,
            journal_path="not-applicable",
            occurred_at=now.isoformat(),
        )
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate or test the miniQMT SMTP failure alert."
    )
    parser.add_argument("--config", required=True)
    parser.add_argument(
        "--test-send",
        action="store_true",
        help="Send one clearly marked configuration-test email.",
    )
    args = parser.parse_args(argv)
    try:
        notifier = load_smtp_failure_notifier(Path(args.config))
        if args.test_send:
            _test_alert(notifier)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {type(exc).__name__}: {exc}")
        return 1
    print("SMTP failure-alert configuration is valid.")
    if args.test_send:
        print("Configuration-test email was sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
