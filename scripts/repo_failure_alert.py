from __future__ import annotations

import argparse
import hashlib
import json
import multiprocessing
import os
import smtplib
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Protocol

ALERT_PASSWORD_ENV = "MINIQMT_ALERT_SMTP_PASSWORD"
WXPUSHER_TOKEN_ENV = "MINIQMT_ALERT_WXPUSHER_TOKEN"
WXPUSHER_SIMPLE_PUSH_URL = (
    "https://wxpusher.zjiecode.com/api/send/message/simple-push"
)
WXPUSHER_SUMMARY_MAX_CHARACTERS = 100
WXPUSHER_CONTENT_MAX_CHARACTERS = 40_000
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
    kind: str = "failure"
    certification: bool = False
    details: Mapping[str, object] = field(default_factory=dict)

    @property
    def key(self) -> str:
        material = {
            "strategy": self.strategy,
            "trade_date": self.trade_date,
            "state": self.state,
            "event": self.event,
            "reason": self.reason,
            "unresolved_order": self.unresolved_order,
            "kind": self.kind,
            "certification": self.certification,
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
class WxPusherAlertConfig:
    spt: str
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


class WxPusherFailureNotifier:
    """Deliver alerts through WxPusher simple-push (SPT) to the owner's WeChat."""

    def __init__(self, config: WxPusherAlertConfig) -> None:
        self.config = config
        self.last_attempts = 0

    def send(self, alert: FailureAlert) -> None:
        errors: list[str] = []
        for attempt in range(1, self.config.attempts + 1):
            self.last_attempts = attempt
            error = _wxpusher_attempt_with_hard_timeout(self.config, alert)
            if error is None:
                return
            errors.append(f"attempt {attempt}: {error}")
            if attempt < self.config.attempts:
                time.sleep(min(0.25 * (2 ** (attempt - 1)), 2.0))
        raise AlertDeliveryError(
            "WxPusher alert delivery failed after bounded retries: "
            + " | ".join(errors)
        )


class CompositeNotifier:
    """Deliver every alert through each configured transport."""

    def __init__(self, notifiers: Sequence[FailureNotifier]) -> None:
        if not notifiers:
            raise ValueError("CompositeNotifier requires at least one notifier")
        self.notifiers = list(notifiers)
        self.last_attempts = 0

    @property
    def transport_names(self) -> tuple[str, ...]:
        return tuple(type(notifier).__name__ for notifier in self.notifiers)

    def send(self, alert: FailureAlert) -> None:
        errors: list[str] = []
        delivered = 0
        self.last_attempts = 0
        for notifier in self.notifiers:
            try:
                notifier.send(alert)
                delivered += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    f"{type(notifier).__name__}: {type(exc).__name__}: {exc}"
                )
            self.last_attempts = max(
                self.last_attempts,
                int(getattr(notifier, "last_attempts", 1)),
            )
        if delivered == 0:
            raise AlertDeliveryError(
                "all notification transports failed: " + " | ".join(errors)
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


def load_wxpusher_failure_notifier(
    config_path: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> WxPusherFailureNotifier:
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
        raise AlertConfigurationError("failure-alert WxPusher is not enabled")
    if payload.get("transport") != "wxpusher":
        raise AlertConfigurationError(
            "WxPusher configuration must declare transport 'wxpusher'"
        )
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
    spt = str(environment.get(WXPUSHER_TOKEN_ENV, "")).strip()
    if not spt:
        raise AlertConfigurationError(
            f"WxPusher SPT token is missing from {WXPUSHER_TOKEN_ENV}"
        )
    if (
        "\r" in spt
        or "\n" in spt
        or not spt.startswith("SPT_")
        or len(spt) > 256
    ):
        raise AlertConfigurationError("invalid WxPusher SPT token")
    return WxPusherFailureNotifier(
        WxPusherAlertConfig(
            spt=spt,
            timeout_seconds=timeout_seconds,
            attempts=attempts,
        )
    )


def load_failure_notifier(
    config_path: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> FailureNotifier:
    """Load whichever transport a single configuration file declares."""
    path = Path(config_path).resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AlertConfigurationError(
            f"failure-alert configuration is unreadable: {path}"
        ) from exc
    transport = str(payload.get("transport", "")).strip().lower()
    if transport == "smtp":
        return load_smtp_failure_notifier(path, environ=environ)
    if transport == "wxpusher":
        return load_wxpusher_failure_notifier(path, environ=environ)
    raise AlertConfigurationError(
        f"unsupported failure-alert transport: {transport!r}"
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


def load_optional_alert_notifiers(
    config_path: Path | None,
    *,
    environ: Mapping[str, str] | None = None,
) -> tuple[FailureNotifier | None, list[str]]:
    """Load every configured notification transport best-effort.

    SMTP comes from ``config_path``; WxPusher is discovered as the sibling
    file ``repo_failure_wxpusher.local.json`` next to it. When both exist,
    every alert is delivered through both channels; when only one exists,
    alerts go through that single channel.
    """
    warnings: list[str] = []
    notifiers: list[FailureNotifier] = []
    if config_path is not None and Path(config_path).is_file():
        try:
            notifiers.append(
                load_smtp_failure_notifier(config_path, environ=environ)
            )
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"smtp: {type(exc).__name__}: {exc}")
    else:
        warnings.append("smtp: failure-alert email is not configured")
    wxpusher_path = _sibling_wxpusher_config_path(config_path)
    if wxpusher_path is not None and wxpusher_path.is_file():
        try:
            notifiers.append(
                load_wxpusher_failure_notifier(
                    wxpusher_path,
                    environ=environ,
                )
            )
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"wxpusher: {type(exc).__name__}: {exc}")
    else:
        warnings.append("wxpusher: WxPusher push is not configured")
    if not notifiers:
        return None, warnings
    if len(notifiers) == 1:
        return notifiers[0], warnings
    return CompositeNotifier(notifiers), warnings


def _sibling_wxpusher_config_path(
    config_path: Path | None,
) -> Path | None:
    if config_path is None:
        return None
    return (
        Path(config_path)
        .resolve()
        .with_name("repo_failure_wxpusher.local.json")
    )


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
    return _deliver_journal_alert(
        notifier,
        journal,
        alert,
        data_field="failure_alert",
    )


def notify_journal_success(
    notifier: FailureNotifier | None,
    journal: JournalLike,
    *,
    environment: str,
    state: str,
) -> AlertDelivery:
    data = journal.payload.get("data") or {}
    order = data.get("current_order") or data.get("last_terminal_order") or {}
    if not isinstance(order, Mapping):
        order = {}
    filled = int(
        data.get(
            "accounted_filled_principal_yuan",
            data.get("filled_principal_yuan", 0),
        )
        or 0
    )
    detail_fields = {
        "成交本金（元）": filled,
        "证券": order.get("symbol", ""),
        "委托号": order.get("order_id", ""),
        "委托利率（%）": order.get("limit_price", ""),
        "成交均价（%）": order.get("traded_price", ""),
        "成交数量": order.get("traded_volume", ""),
    }
    reason = (
        f"成交本金={filled}元；证券={order.get('symbol', '')}；"
        f"委托号={order.get('order_id', '')}；"
        f"委托利率={order.get('limit_price', '')}%；"
        f"成交均价={order.get('traded_price', '')}%；"
        f"成交数量={order.get('traded_volume', '')}"
    )
    alert = FailureAlert(
        strategy=str(journal.strategy),
        trade_date=str(journal.trade_date),
        environment=str(environment),
        state=str(state),
        event="execution_completed",
        reason=reason,
        unresolved_order=False,
        journal_path=str(Path(journal.path).resolve()),
        occurred_at=datetime.now().astimezone().isoformat(),
        kind="success",
        details=detail_fields,
    )
    return _deliver_journal_alert(
        notifier,
        journal,
        alert,
        data_field="success_alert",
    )


def notify_journal_certification(
    notifier: FailureNotifier | None,
    journal: JournalLike,
    *,
    environment: str,
    state: str,
    passed: bool,
    reason: str = "",
) -> AlertDelivery:
    data = journal.payload.get("data") or {}
    order = data.get("current_order") or data.get("last_terminal_order") or {}
    if not isinstance(order, Mapping):
        order = {}
    filled = int(data.get("filled_principal_yuan", 0) or 0)
    detail_fields = {
        "成交本金（元）": filled,
        "证券": order.get("symbol", ""),
        "委托号": order.get("order_id", ""),
        "委托利率（%）": order.get("limit_price", ""),
        "成交均价（%）": order.get("traded_price", ""),
        "成交数量": order.get("traded_volume", ""),
    }
    alert = FailureAlert(
        strategy=str(journal.strategy),
        trade_date=str(journal.trade_date),
        environment=str(environment),
        state=str(state),
        event=("certification_passed" if passed else "certification_failed"),
        reason=_bounded_text(
            reason or "实盘通道认证未通过。",
            2_000,
        ),
        unresolved_order=False,
        journal_path=str(Path(journal.path).resolve()),
        occurred_at=datetime.now().astimezone().isoformat(),
        kind=("success" if passed else "failure"),
        certification=True,
        details=detail_fields if passed else {},
    )
    return _deliver_journal_alert(
        notifier,
        journal,
        alert,
        data_field="certification_alert",
    )


def _deliver_journal_alert(
    notifier: FailureNotifier | None,
    journal: JournalLike,
    alert: FailureAlert,
    *,
    data_field: str,
) -> AlertDelivery:
    previous = _previous_delivery(journal.payload, data_field=data_field)
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
        journal.update_data(**{data_field: asdict(delivery)})
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


def _build_subject(alert: FailureAlert) -> str:
    successful = alert.kind == "success"
    is_test = alert.strategy == "notification_configuration_test"
    if is_test:
        return f"[miniQMT][测试通知] {alert.trade_date} 通知配置测试"
    elif alert.certification and successful:
        return (
            f"[miniQMT][实盘认证成功][{alert.environment.upper()}] "
            f"{alert.trade_date} GC001 1000元"
        )
    elif alert.certification:
        return (
            f"[miniQMT][实盘认证失败][{alert.environment.upper()}] "
            f"{alert.trade_date}"
        )
    elif successful:
        return (
            f"[miniQMT][执行成功][{alert.environment.upper()}] "
            f"{alert.trade_date} 国债逆回购"
        )
    return (
        f"[miniQMT][需人工检查][{alert.environment.upper()}] "
        f"{alert.trade_date} 国债逆回购"
    )


def _build_body_lines(alert: FailureAlert) -> list[str]:
    successful = alert.kind == "success"
    environment_label = (
        "实盘" if alert.environment == "live" else str(alert.environment)
    )
    is_test = alert.strategy == "notification_configuration_test"
    details = dict(alert.details or {})
    lines: list[str] = []
    if is_test:
        lines.append("这是一条通知配置测试，无需任何处理。")
        lines.append("")
        lines.append(f"测试时间：{alert.occurred_at}")
        lines.append(f"策略：{alert.strategy}")
    elif alert.certification:
        if successful:
            lines.append("miniQMT 实盘通道认证已通过。")
            lines.append("")
            lines.append(f"策略：{alert.strategy}")
            lines.append(f"交易日：{alert.trade_date}")
            lines.append(f"环境：{environment_label}")
            lines.append("证书状态：已签发（本机HMAC签名）")
        else:
            lines.append("miniQMT 实盘通道认证未通过，需要人工检查。")
            lines.append("")
            lines.append(f"策略：{alert.strategy}")
            lines.append(f"交易日：{alert.trade_date}")
            lines.append(f"环境：{environment_label}")
            lines.append(f"原因：{alert.reason}")
            lines.append("")
            lines.append("安全约束：未自动补单、追价或扩大金额。")
    elif successful:
        lines.append("miniQMT 国债逆回购执行已成功完成。")
        lines.append("")
        lines.append(f"策略：{alert.strategy}")
        lines.append(f"交易日：{alert.trade_date}")
        lines.append(f"环境：{environment_label}")
        lines.append(f"状态：{alert.state}")
    else:
        lines.append("miniQMT 逆回购执行器已安全停止，需要人工检查。")
        lines.append("")
        lines.append(f"策略：{alert.strategy}")
        lines.append(f"交易日：{alert.trade_date}")
        lines.append(f"环境：{environment_label}")
        lines.append(f"状态：{alert.state}")
        lines.append(f"事件：{alert.event}")
        lines.append(f"原因：{alert.reason}")
        lines.append(
            "是否存在未决委托："
            + (
                "是，请先到券商端查单"
                if alert.unresolved_order
                else "否"
            )
        )
        if alert.error_type:
            lines.append(f"异常类型：{alert.error_type}")
            lines.append(f"异常信息：{alert.error_message or ''}")
        lines.append("")
        lines.append("安全约束：程序未自动补单、追价、改价或扩大金额。")

    if details:
        lines.append("")
        lines.append("成交明细：")
        for name, value in details.items():
            lines.append(f"  {name}：{value}")
    elif successful and not is_test:
        lines.append("")
        lines.append(f"结果：{alert.reason}")

    lines.append("")
    lines.append(f"发生时间：{alert.occurred_at}")
    lines.append(f"本机日志：{alert.journal_path}")
    if alert.certification and successful:
        lines.append("")
        lines.append("后续：实盘任务仍为 Disabled；人工复核后执行 rr on。")
    elif alert.certification and not successful:
        lines.append("")
        lines.append("后续：修复问题后重新执行 rr cert。")
    return lines


def _build_message(
    config: SmtpAlertConfig,
    alert: FailureAlert,
) -> EmailMessage:
    message = EmailMessage()
    message["From"] = config.from_address
    message["To"] = ", ".join(config.to_addresses)
    message["Subject"] = _clean_header(_build_subject(alert))
    message.set_content(
        "\n".join(_build_body_lines(alert)),
        charset="utf-8",
    )
    return message


def _build_wxpusher_payload(
    config: WxPusherAlertConfig,
    alert: FailureAlert,
) -> dict[str, object]:
    content = "\n".join(_build_body_lines(alert))
    content = content[:WXPUSHER_CONTENT_MAX_CHARACTERS]
    summary = _bounded_text(
        _build_subject(alert),
        WXPUSHER_SUMMARY_MAX_CHARACTERS,
    )
    return {
        "spt": config.spt,
        "content": content,
        "summary": summary,
        "contentType": 1,
    }


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


def _wxpusher_attempt_with_hard_timeout(
    config: WxPusherAlertConfig,
    alert: FailureAlert,
) -> str | None:
    context = multiprocessing.get_context("spawn")
    parent_connection, child_connection = context.Pipe(duplex=False)
    process = context.Process(
        target=_wxpusher_attempt_worker,
        args=(child_connection, config, alert),
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
                f"HardTimeout: WxPusher attempt exceeded "
                f"{config.timeout_seconds:.1f} seconds"
            )
        if parent_connection.poll():
            ok, detail = parent_connection.recv()
            return None if ok else str(detail)
        if process.exitcode == 0:
            return "WxPusher worker exited without a delivery result"
        return f"WxPusher worker exited with code {process.exitcode}"
    except (OSError, RuntimeError) as exc:
        return f"{type(exc).__name__}: {exc}"
    finally:
        child_connection.close()
        parent_connection.close()
        if process.is_alive():
            process.terminate()
            process.join(2.0)


def _wxpusher_attempt_worker(
    connection: Any,
    config: WxPusherAlertConfig,
    alert: FailureAlert,
) -> None:
    try:
        _wxpusher_send_once(config, alert)
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


def _wxpusher_send_once(
    config: WxPusherAlertConfig,
    alert: FailureAlert,
) -> None:
    payload = _build_wxpusher_payload(config, alert)
    request = urllib.request.Request(
        WXPUSHER_SIMPLE_PUSH_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=config.timeout_seconds,
        ) as response:
            raw = response.read(64 * 1024)
    except urllib.error.HTTPError as exc:
        detail = _bounded_text(exc.read(2_048), 1_000)
        raise AlertDeliveryError(
            f"WxPusher HTTP {exc.code}: {detail or exc.reason}"
        ) from exc
    except (OSError, urllib.error.URLError) as exc:
        raise AlertDeliveryError(
            f"WxPusher request failed: {type(exc).__name__}: {exc}"
        ) from exc
    try:
        response_payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AlertDeliveryError(
            f"WxPusher returned a non-JSON response: {exc}"
        ) from exc
    if (
        not isinstance(response_payload, dict)
        or response_payload.get("code") != 1000
    ):
        message = "unexpected response"
        if isinstance(response_payload, dict):
            message = _bounded_text(
                str(response_payload.get("msg", message)),
                500,
            )
        raise AlertDeliveryError(
            f"WxPusher rejected the message: {message}"
        )


def _previous_delivery(
    payload: Mapping[str, object],
    *,
    data_field: str = "failure_alert",
) -> Mapping[str, object] | None:
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return None
    previous = data.get(data_field)
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
            strategy="notification_configuration_test",
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
        description="Validate or test the miniQMT failure alert."
    )
    parser.add_argument("--config", required=True)
    parser.add_argument(
        "--test-send",
        action="store_true",
        help="Send one clearly marked configuration-test notification.",
    )
    args = parser.parse_args(argv)
    try:
        notifier = load_failure_notifier(Path(args.config))
        if args.test_send:
            _test_alert(notifier)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {type(exc).__name__}: {exc}")
        return 1
    print("Failure-alert configuration is valid.")
    if args.test_send:
        print("Configuration-test notification was sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
