from __future__ import annotations

import os
from pathlib import Path


def _read(path: Path, limit: int = 8000) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return f"[missing: {path}]"
    return text[:limit]


def main() -> None:
    root = Path(os.environ.get("CLAUDE_PROJECT_DIR", Path.cwd())).resolve()
    status = _read(root / "docs" / "STATUS.md", 5000)
    print(
        "\n=== ARGOS SESSION CONTEXT ===\n"
        "Autonomous boundary: complete M0-M4, then stop for owner review.\n"
        "Hard boundary: public read-only Polymarket data; no wallet/auth/orders/execution.\n"
        "Scientific boundary: uncalibrated output is a raw score, never a probability.\n"
        "Workflow: criterion -> specialist -> implementation -> tests -> review -> docs\n"
        "          -> commit.\n\n"
        "CURRENT STATUS:\n"
        f"{status}\n"
        "=== END ARGOS SESSION CONTEXT ===\n"
    )


if __name__ == "__main__":
    main()
