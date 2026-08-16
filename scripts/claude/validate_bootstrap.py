from __future__ import annotations

import json
from pathlib import Path

REQUIRED = (
    "CLAUDE.md",
    ".claude/settings.json",
    ".claude/agents/argos-lead.md",
    ".claude/skills/bootstrap/SKILL.md",
    "docs/CORE_INVARIANTS.md",
    "docs/07_MILESTONES.md",
    "docs/STATUS.md",
    "docs/OWNER_REVIEW_GATE.md",
    "pyproject.toml",
)


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    missing = [path for path in REQUIRED if not (root / path).exists()]
    if missing:
        print("Missing required files:")
        for path in missing:
            print(f"- {path}")
        return 1

    json.loads((root / ".claude" / "settings.json").read_text(encoding="utf-8"))

    names: dict[str, Path] = {}
    for agent in (root / ".claude" / "agents").rglob("*.md"):
        text = agent.read_text(encoding="utf-8")
        marker = "\nname: "
        if marker not in text:
            print(f"Agent missing name frontmatter: {agent}")
            return 1
        name = text.split(marker, 1)[1].splitlines()[0].strip()
        if name in names:
            print(f"Duplicate agent name {name}: {names[name]} and {agent}")
            return 1
        names[name] = agent

    skills = list((root / ".claude" / "skills").glob("*/SKILL.md"))
    print(f"Bootstrap valid: {len(names)} agents, {len(skills)} skills")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
