"""Tests for claude_code_package's uvx invocation builders."""

import shlex

from src.zulipchat_mcp.integrations.claude_code_package import (
    UVX_PACKAGE_SPEC,
    _build_hook_command,
    _build_mcp_args,
    _plugin_mcp_config,
)


def test_build_hook_command_requests_duckdb_extra():
    """duckdb/duckdb-engine are an opt-in extra - the hook command (which
    calls init_database() in claude_hooks.py) must request it, or it
    installs a server with no working database backend.
    """
    command = _build_hook_command("/home/test/.zuliprc", None)

    # shlex.join quotes the [duckdb] extra since brackets are shell
    # metacharacters - this is the safe, expected quoting, not a bug.
    assert f"--from {shlex.quote(UVX_PACKAGE_SPEC)}" in command
    assert "zulipchat-mcp-hook" in command


def test_build_mcp_args_requests_duckdb_extra():
    args = _build_mcp_args("/home/test/.zuliprc", None, extended_tools=False)

    assert args[0] == "--from"
    assert args[1] == UVX_PACKAGE_SPEC
    assert "zulipchat-mcp" in args


def test_plugin_mcp_config_requests_duckdb_extra():
    config = _plugin_mcp_config("/home/test/.zuliprc", None, extended_tools=False)

    args = config["mcpServers"]["zulipchat"]["args"]
    assert args[0] == "--from"
    assert args[1] == UVX_PACKAGE_SPEC
