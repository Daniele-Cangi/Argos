# Claude Code operations

Last verified against official Claude Code documentation: **2026-08-06**.

## Project memory

`CLAUDE.md` is loaded as shared project memory. It imports the core invariants and current status. Detailed procedures live in skills so they do not consume context until needed.

Official memory reference:
`https://docs.anthropic.com/en/docs/claude-code/memory`

## Default lead agent

`.claude/settings.json` sets `argos-lead` as the main project agent. Project subagents live under `.claude/agents/` and are version controlled.

Use:

```text
/agents
```

to inspect definitions and running agents. Restart the session after manually adding or changing agent files if the installed Claude Code version does not hot-reload them.

Official subagent reference:
`https://code.claude.com/docs/en/sub-agents`

## Skills

Project workflows live under `.claude/skills/<name>/SKILL.md` and appear as slash commands:

```text
/bootstrap
/continue-milestone
/quality-gate
/architecture-decision
/market-audit
/close-milestone
/handoff
/research-source
```

Official skills reference:
`https://code.claude.com/docs/en/slash-commands`

## Hooks

The project configures:

- `SessionStart`: inject current STATUS and non-negotiable boundaries;
- `PreToolUse`: deny destructive shell commands, direct trading endpoint use, and obvious credential handling.

Inspect loaded hooks with:

```text
/hooks
```

Official hook reference:
`https://code.claude.com/docs/en/hooks`

## Settings verification

After cloning or changing configuration:

```text
/status
/agents
/hooks
```

Run from the shell:

```bash
claude doctor
python scripts/claude/validate_bootstrap.py
```

Project settings are shared through `.claude/settings.json`. Machine-specific approvals belong in `.claude/settings.local.json`, which is gitignored.

Official settings reference:
`https://code.claude.com/docs/en/configuration`

## Recommended human permission posture

- Trust the repository only after reviewing `.claude/settings.json` and hook scripts.
- Keep normal permission prompts enabled for commands not allowlisted.
- Do not use bypass-permissions mode.
- Review dependency installation and any command that writes outside the repository.
- Normal `git push` remains a human-visible permission boundary; force push is denied.
