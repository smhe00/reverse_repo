from __future__ import annotations

import argparse
import secrets
from pathlib import Path

from repo_execution_core import atomic_write_json


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Create the local HMAC key used to authenticate simulation "
            "validation certificates."
        )
    )
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    if output.exists():
        print(f"Release-gate signing key already exists: {output}")
        return 0
    atomic_write_json(
        output,
        {
            "version": 1,
            "hmac_sha256_key_hex": secrets.token_hex(32),
        },
    )
    print(f"Created local release-gate signing key: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
