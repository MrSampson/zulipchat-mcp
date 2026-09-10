"""Claude Code package helpers for richer ZulipChat integration assets."""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

from .. import __version__

# uvx needs an explicit --from with the extra to get a working backend, since
# duckdb/duckdb-engine are no longer bundled by default (see pyproject.toml's
# [project.optional-dependencies]) - a bare `uvx zulipchat-mcp` installs a
# server with no working database backend. Shared with registry.py.
UVX_PACKAGE_SPEC = "zulipchat-mcp[duckdb]"


def _build_mcp_args(
    zulip_config_file: str,
    zulip_bot_config_file: str | None,
    *,
    extended_tools: bool,
) -> list[str]:
    args = [
        "--from",
        UVX_PACKAGE_SPEC,
        "zulipchat-mcp",
        "--zulip-config-file",
        zulip_config_file,
    ]
    if zulip_bot_config_file:
        args.extend(["--zulip-bot-config-file", zulip_bot_config_file])
    if extended_tools:
        args.append("--extended-tools")
    return args


def _build_hook_command(
    zulip_config_file: str,
    zulip_bot_config_file: str | None,
) -> str:
    command = [
        "uvx",
        "--from",
        UVX_PACKAGE_SPEC,
        "zulipchat-mcp-hook",
        "--zulip-config-file",
        zulip_config_file,
    ]
    if zulip_bot_config_file:
        command.extend(["--zulip-bot-config-file", zulip_bot_config_file])
    return shlex.join(command)


def build_claude_hook_settings(
    zulip_config_file: str,
    zulip_bot_config_file: str | None,
) -> dict[str, Any]:
    """Build the Claude Code hook settings payload."""
    hook_command = _build_hook_command(zulip_config_file, zulip_bot_config_file)
    command_hook = {"type": "command", "command": hook_command}
    timeout_hook = {**command_hook, "timeout": 900}

    return {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "startup|resume",
                    "hooks": [command_hook],
                }
            ],
            "PermissionRequest": [
                {
                    "matcher": ".*",
                    "hooks": [timeout_hook],
                }
            ],
            "PostToolUseFailure": [
                {
                    "matcher": ".*",
                    "hooks": [command_hook],
                }
            ],
            "Notification": [
                {
                    "matcher": "idle_prompt",
                    "hooks": [command_hook],
                }
            ],
            "StopFailure": [
                {
                    "matcher": ".*",
                    "hooks": [command_hook],
                }
            ],
            "TaskCompleted": [
                {
                    "hooks": [command_hook],
                }
            ],
            "SessionEnd": [
                {
                    "matcher": "other|prompt_input_exit|logout",
                    "hooks": [command_hook],
                }
            ],
        }
    }


def _skill_session_operator() -> str:
    return """---
name: zulipchat-session-operator
description: Run the current Claude Code session as a Zulip-controlled work session with topic-bound steering, approvals, and lifecycle updates.
disable-model-invocation: true
---

# Zulip session operator

Use this skill when the current Claude Code session is already bound to a Zulip topic
through `zulipchat-mcp-hook` and you want the rest of the work to respect that
control plane.

## Required setup

1. Read `ZULIPCHAT_SESSION_ID`, `ZULIPCHAT_SESSION_STREAM`, and
   `ZULIPCHAT_SESSION_TOPIC` from the shell before using any ZulipChat MCP tools.
2. If `ZULIPCHAT_SESSION_ID` is empty, stop and explain that the hook bridge has not
   initialized the session binding yet.

## Operating rules

1. Treat Zulip as the owner control plane for this session.
2. Poll `poll_agent_events(session_id=..., limit=20)` at the start of each work
   cycle, after each meaningful unit of work, and before going idle.
3. Interpret inbound events this way:
   - `steer`: treat the message as a high-priority owner instruction.
   - `command`: support `/status`, `/pause`, `/resume`, `/cancel`, and `/handoff`.
   - `approval_response`: only consume it when tied to a pending
     `request_user_input` approval flow.
4. Default notification policy is lifecycle-only. Use `agent_message` only for:
   `started`, `blocked`, `waiting`, `completed`, `failed`, or a targeted owner
   message that materially changes execution.
5. If you need a decision from the owner, call `request_user_input` instead of
   posting a freeform message.
6. Never spam the topic with token-by-token progress. One message per state
   transition is the expected behavior.
7. If you are running inside Claude's native loop mode, keep the same poll-work-post
   discipline for every loop turn.

## Command handling

- `/status`: send one concise status update covering current goal, next step, and any
  blockers.
- `/pause`: acknowledge, stop proactive work, and wait for a new owner message.
- `/resume`: acknowledge and continue from the current plan.
- `/cancel`: confirm the cancellation in the topic, stop work cleanly, and close the
  session if appropriate.
- `/handoff`: summarize state, remaining work, and risks for whoever takes over next.
- Any other slash command: reply once that it is not authorized or not supported by
  this session policy.
"""


def _skill_notifyme() -> str:
    return """---
name: zulipchat-notifyme
description: Send a deterministic message into the current Zulip-bound session topic.
disable-model-invocation: true
---

# Zulip notify me

Use this skill when you explicitly want to post to the bound Zulip topic outside the
default lifecycle hooks.

## Required setup

1. Read `ZULIPCHAT_SESSION_ID` from the shell.
2. If it is empty, stop and explain that there is no active Zulip-bound Claude
   session.

## Invocation contract

Interpret `$ARGUMENTS` as either:

- `<category> :: <message>`
- `<message>` for a plain `message` category

Supported categories:

- `message`
- `started`
- `blocked`
- `waiting`
- `completed`
- `failed`

## Execution

1. Parse the category and message.
2. Call `agent_message(session_id=..., category=..., content=...)`.
3. Keep the Zulip post concise and operational.
4. If the owner explicitly needs to reply with a choice, use
   `request_user_input(...)` instead.
"""


def _skill_loop() -> str:
    return """---
name: zulipchat-loop
description: Run the current Claude Code session as a continuous Zulip-aware work loop.
disable-model-invocation: true
---

# Zulip loop

Use this skill when you want the session to behave like a continuous autonomous worker
that still remains steerable from Zulip.

Treat `$ARGUMENTS` as the current mission or loop objective.

## Required setup

1. Read `ZULIPCHAT_SESSION_ID`, `ZULIPCHAT_SESSION_STREAM`, and
   `ZULIPCHAT_SESSION_TOPIC` from the shell.
2. If the session is not bound yet, stop and explain the missing hook setup.

## Loop contract

Each cycle should do exactly this:

1. Poll `poll_agent_events(session_id=..., limit=20)`.
2. Incorporate any owner steering or commands before doing more work.
3. Decide whether the lifecycle state changed. If it did, emit one lifecycle message.
4. Do one concrete unit of work toward the mission.
5. If blocked on owner input, call `request_user_input(...)` and wait.
6. Exit the loop when the mission is complete, the owner pauses or cancels the
   session, or the session binding is closed.

## `/loop` integration

If the current Claude Code build exposes the native `/loop` command, this skill's
instructions are the policy for each loop turn. If `/loop` is not available, follow
the same cycle in the current conversation instead of failing.
"""


def _agent_session_operator() -> str:
    return """---
name: zulip-session-operator
description: Owns the Zulip communication and owner-control plane for the current Claude Code session. Use proactively when work should stay synchronized with a Zulip topic.
skills:
  - zulipchat-session-operator
  - zulipchat-notifyme
  - zulipchat-loop
---

You are responsible for keeping the current Claude Code session aligned with its bound
Zulip topic.

Start by reading `ZULIPCHAT_SESSION_ID`, `ZULIPCHAT_SESSION_STREAM`, and
`ZULIPCHAT_SESSION_TOPIC` from the shell.

Then:

1. Use the preloaded skills as the operating procedure.
2. Keep outbound Zulip traffic minimal, deliberate, and lifecycle-oriented.
3. Escalate with `request_user_input` whenever the owner must choose between options.
4. Treat inbound topic steering as higher priority than speculative autonomous work.
"""


def _plugin_manifest() -> dict[str, Any]:
    return {
        "name": "zulipchat",
        "description": (
            "Connect Claude Code sessions to Zulip topics with lifecycle hooks, "
            "owner approvals, and bidirectional control."
        ),
        "version": __version__,
        "author": {"name": "Anthony Kougkas"},
        "homepage": "https://github.com/akougkas/zulipchat-mcp",
        "repository": "https://github.com/akougkas/zulipchat-mcp",
        "license": "MIT",
    }


def _plugin_mcp_config(
    zulip_config_file: str,
    zulip_bot_config_file: str | None,
    *,
    extended_tools: bool,
) -> dict[str, Any]:
    return {
        "mcpServers": {
            "zulipchat": {
                "type": "stdio",
                "command": "uvx",
                "args": _build_mcp_args(
                    zulip_config_file,
                    zulip_bot_config_file,
                    extended_tools=extended_tools,
                ),
            }
        }
    }


def standalone_package_files(
    zulip_config_file: str,
    zulip_bot_config_file: str | None,
) -> dict[str, str]:
    """Render standalone `.claude/` assets."""
    return {
        ".claude/settings.json": (
            json.dumps(
                build_claude_hook_settings(
                    zulip_config_file,
                    zulip_bot_config_file,
                ),
                indent=2,
            )
            + "\n"
        ),
        ".claude/skills/zulipchat-session-operator/SKILL.md": _skill_session_operator(),
        ".claude/skills/zulipchat-notifyme/SKILL.md": _skill_notifyme(),
        ".claude/skills/zulipchat-loop/SKILL.md": _skill_loop(),
        ".claude/agents/zulip-session-operator.md": _agent_session_operator(),
    }


def plugin_package_files(
    zulip_config_file: str,
    zulip_bot_config_file: str | None,
    *,
    extended_tools: bool,
) -> dict[str, str]:
    """Render a Claude Code plugin package."""
    return {
        ".claude-plugin/plugin.json": json.dumps(_plugin_manifest(), indent=2) + "\n",
        ".mcp.json": (
            json.dumps(
                _plugin_mcp_config(
                    zulip_config_file,
                    zulip_bot_config_file,
                    extended_tools=extended_tools,
                ),
                indent=2,
            )
            + "\n"
        ),
        "hooks/hooks.json": (
            json.dumps(
                build_claude_hook_settings(
                    zulip_config_file,
                    zulip_bot_config_file,
                ),
                indent=2,
            )
            + "\n"
        ),
        "skills/zulipchat-session-operator/SKILL.md": _skill_session_operator(),
        "skills/zulipchat-notifyme/SKILL.md": _skill_notifyme(),
        "skills/zulipchat-loop/SKILL.md": _skill_loop(),
        "agents/zulip-session-operator.md": _agent_session_operator(),
    }


def _merge_settings_file(path: Path, incoming: dict[str, Any]) -> str:
    payload: dict[str, Any] = {}
    action = "written"
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"{path} must contain a JSON object")
        action = "merged"

    hooks = payload.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(f"{path} has a non-object `hooks` field")

    for event_name, groups in incoming.get("hooks", {}).items():
        existing = hooks.setdefault(event_name, [])
        if not isinstance(existing, list):
            raise ValueError(f"{path} has a non-array hook group for {event_name}")
        for group in groups:
            if group not in existing:
                existing.append(group)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return action


def _write_text_file(path: Path, content: str, *, force: bool) -> str:
    if path.exists():
        current = path.read_text(encoding="utf-8")
        if current == content:
            return "unchanged"
        if not force:
            raise FileExistsError(
                f"{path} already exists; re-run with --force to overwrite it"
            )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return "written"


def export_claude_code_package(
    output_dir: str | Path,
    *,
    zulip_config_file: str,
    zulip_bot_config_file: str | None = None,
    mode: str = "standalone",
    extended_tools: bool = False,
    force: bool = False,
) -> list[dict[str, str]]:
    """Export a richer Claude Code package into a destination directory."""
    base_dir = Path(output_dir)
    base_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, str]] = []

    if mode == "standalone":
        settings = build_claude_hook_settings(
            zulip_config_file,
            zulip_bot_config_file,
        )
        settings_path = base_dir / ".claude" / "settings.json"
        settings_action = _merge_settings_file(settings_path, settings)
        results.append({"path": str(settings_path), "action": settings_action})

        files = standalone_package_files(
            zulip_config_file,
            zulip_bot_config_file,
        )
        for relative_path, content in files.items():
            if relative_path == ".claude/settings.json":
                continue
            destination = base_dir / relative_path
            action = _write_text_file(destination, content, force=force)
            results.append({"path": str(destination), "action": action})
        return results

    if mode == "plugin":
        files = plugin_package_files(
            zulip_config_file,
            zulip_bot_config_file,
            extended_tools=extended_tools,
        )
        for relative_path, content in files.items():
            destination = base_dir / relative_path
            action = _write_text_file(destination, content, force=force)
            results.append({"path": str(destination), "action": action})
        return results

    raise ValueError(f"Unsupported Claude Code package mode: {mode}")
