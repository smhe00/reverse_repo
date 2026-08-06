from __future__ import annotations

import argparse
import hashlib
import hmac
import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from repo_execution_core import (
    atomic_write_json,
    reverse_repo_strategy_config,
    reverse_repo_strategy_config_sha256,
    xtquant_runtime_sha256,
)
from repo_execution_state_machine import verify_state_machines


SCHEMA_VERSION = 2
MANIFEST_KIND = "reverse_repo_live_enable_manifest"


def create_live_enable_manifest(
    *,
    strategy_config: Path,
    live_channel_certificate: Path,
    signing_key: Path,
    now: datetime | None = None,
    verification: Mapping[str, Any] | None = None,
    runtime_sha256: str | None = None,
    armed_without_certificate: bool = False,
) -> dict[str, Any]:
    configuration = reverse_repo_strategy_config(strategy_config)
    formal = (
        dict(verify_state_machines())
        if verification is None
        else dict(verification)
    )
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": MANIFEST_KIND,
        "armed_at": (
            datetime.now().astimezone() if now is None else now
        ).isoformat(),
        "strategy_config": configuration,
        "strategy_config_sha256": reverse_repo_strategy_config_sha256(
            strategy_config
        ),
        "live_channel_certificate_sha256": (
            ""
            if armed_without_certificate
            else _file_sha256(live_channel_certificate)
        ),
        "armed_without_certificate": bool(armed_without_certificate),
        "transition_spec_sha256": str(
            formal["transition_spec_sha256"]
        ),
        "execution_source_sha256": str(
            formal["execution_source_sha256"]
        ),
        "execution_source_commit": formal.get(
            "execution_source_commit"
        ),
        "xtquant_runtime_sha256": (
            xtquant_runtime_sha256()
            if runtime_sha256 is None
            else runtime_sha256
        ),
    }
    payload["signature_hmac_sha256"] = _sign_payload(
        payload,
        _load_signing_key(signing_key),
    )
    return payload


def verify_live_enable_manifest(
    *,
    manifest_path: Path,
    strategy_config: Path,
    live_channel_certificate: Path,
    signing_key: Path,
    verification: Mapping[str, Any] | None = None,
    runtime_sha256: str | None = None,
) -> dict[str, Any]:
    manifest = _load_manifest(manifest_path)
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError("unsupported live-enable manifest schema")
    if manifest.get("kind") != MANIFEST_KIND:
        raise RuntimeError("live-enable manifest kind is invalid")
    _verify_signature(manifest, signing_key)
    configuration = reverse_repo_strategy_config(strategy_config)
    current_config_hash = reverse_repo_strategy_config_sha256(
        strategy_config
    )
    if manifest.get("strategy_config") != configuration:
        raise RuntimeError(
            "live configuration changed after rr on; run rr off and rr on"
        )
    if manifest.get("strategy_config_sha256") != current_config_hash:
        raise RuntimeError(
            "live configuration hash changed after rr on"
        )
    if manifest.get("armed_without_certificate") is not True:
        if manifest.get("live_channel_certificate_sha256") != _file_sha256(
            live_channel_certificate
        ):
            raise RuntimeError(
                "simulation certificate changed after rr on"
            )
    formal = (
        dict(verify_state_machines())
        if verification is None
        else dict(verification)
    )
    if manifest.get("transition_spec_sha256") != str(
        formal["transition_spec_sha256"]
    ):
        raise RuntimeError("state machines changed after rr on")
    if manifest.get("execution_source_sha256") != str(
        formal["execution_source_sha256"]
    ):
        raise RuntimeError("execution sources changed after rr on")
    certified_commit = str(
        manifest.get("execution_source_commit") or ""
    ).strip()
    current_commit = str(
        formal.get("execution_source_commit") or ""
    ).strip()
    if certified_commit and current_commit:
        if certified_commit != current_commit:
            raise RuntimeError(
                "execution source commit changed after rr on "
                f"(armed {certified_commit[:12]}, now {current_commit[:12]})"
            )
    current_runtime_hash = (
        xtquant_runtime_sha256()
        if runtime_sha256 is None
        else runtime_sha256
    )
    if manifest.get("xtquant_runtime_sha256") != current_runtime_hash:
        raise RuntimeError("XtQuant runtime changed after rr on")
    return manifest


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("live-enable manifest is missing or unreadable") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("live-enable manifest must be an object")
    return payload


def _load_signing_key(path: Path) -> bytes:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        key = bytes.fromhex(str(payload["hmac_sha256_key_hex"]))
    except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("release-gate signing key is unreadable") from exc
    if payload.get("version") != 1 or len(key) != 32:
        raise RuntimeError("release-gate signing key is invalid")
    return key


def _unsigned_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        name: value
        for name, value in payload.items()
        if name != "signature_hmac_sha256"
    }


def _sign_payload(payload: Mapping[str, Any], key: bytes) -> str:
    message = json.dumps(
        _unsigned_payload(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def _verify_signature(payload: Mapping[str, Any], signing_key: Path) -> None:
    provided = str(payload.get("signature_hmac_sha256", ""))
    expected = _sign_payload(payload, _load_signing_key(signing_key))
    if not hmac.compare_digest(provided, expected):
        raise RuntimeError("live-enable manifest signature is invalid")


def _file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError as exc:
        raise RuntimeError(f"required file is unreadable: {path}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create or verify the fail-closed live-enable snapshot."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    create_parser = subparsers.add_parser("create")
    verify_parser = subparsers.add_parser("verify")
    for child in (create_parser, verify_parser):
        child.add_argument("--strategy-config", required=True)
        child.add_argument("--live-channel-certificate", required=True)
        child.add_argument("--signing-key", required=True)
        child.add_argument("--manifest", required=True)
    create_parser.add_argument(
        "--force",
        action="store_true",
        help="arm without binding a live-channel certificate",
    )
    args = parser.parse_args()
    arguments = {
        "strategy_config": Path(args.strategy_config),
        "live_channel_certificate": Path(args.live_channel_certificate),
        "signing_key": Path(args.signing_key),
    }
    if args.command == "create":
        manifest = create_live_enable_manifest(
            **arguments,
            armed_without_certificate=bool(args.force),
        )
        atomic_write_json(Path(args.manifest), manifest)
        print("Live-enable manifest created and signed.")
    else:
        verify_live_enable_manifest(
            manifest_path=Path(args.manifest),
            **arguments,
        )
        print("Live-enable manifest matches the armed configuration.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
