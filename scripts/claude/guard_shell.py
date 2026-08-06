from __future__ import annotations

import json
import re
import sys
from typing import Any

BLOCKED: tuple[tuple[str, str], ...] = (
    (r"\brm\s+-rf\b", "Recursive force deletion is prohibited."),
    (r"\bremove-item\b[^\n]*\b-recurse\b", "Recursive PowerShell deletion is prohibited."),
    (r"\bgit\s+reset\s+--hard\b", "Hard reset is prohibited."),
    (r"\bgit\s+clean\s+-[^\n]*f", "Destructive git clean is prohibited."),
    (r"\bgit\s+push\b[^\n]*(--force|-f\b)", "Force push is prohibited."),
    (r"\bcurl\b[^\n]*\|\s*(sh|bash|zsh)\b", "Piping network content to a shell is prohibited."),
    (r"\bwget\b[^\n]*\|\s*(sh|bash|zsh)\b", "Piping network content to a shell is prohibited."),
    (
        r"clob\.polymarket\.com[^\n]*/(order|orders|cancel)",
        "Direct trading endpoint access is outside M0-M4.",
    ),
    (
        r"\b(private[_-]?key|mnemonic|seed[_ -]?phrase)\s*=",
        "Private credential material is prohibited.",
    ),
    (
        r"\b(cat|type|get-content)\b[^\n]*\.env\b",
        "Reading local environment secrets through shell is prohibited.",
    ),
)


def _deny(reason: str) -> None:
    payload: dict[str, Any] = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
    print(json.dumps(payload))


def main() -> None:
    try:
        event = json.load(sys.stdin)
    except Exception:
        return
    tool_input = event.get("tool_input") or {}
    command = str(tool_input.get("command") or "")
    lowered = command.lower()
    for pattern, reason in BLOCKED:
        if re.search(pattern, lowered, flags=re.IGNORECASE):
            _deny(reason)
            return


if __name__ == "__main__":
    main()
