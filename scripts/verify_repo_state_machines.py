from __future__ import annotations

import argparse
import json
import platform
from datetime import datetime
from pathlib import Path

from repo_execution_core import atomic_write_json
from repo_execution_state_machine import verify_state_machines


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Exhaustively verify both reverse-repo finite-state machines."
        )
    )
    parser.add_argument("--output")
    args = parser.parse_args()

    result = {
        "schema_version": 1,
        "verified_at": datetime.now().astimezone().isoformat(),
        "python": platform.python_version(),
        "passed": True,
        "verification": verify_state_machines(),
    }
    if args.output:
        atomic_write_json(Path(args.output), result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
