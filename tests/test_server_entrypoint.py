"""Tests for server entrypoint CLI behavior."""

import sys
from unittest.mock import MagicMock, patch

import pytest

from src.zulipchat_mcp import server


@pytest.fixture
def mock_config_manager():
    """Config manager fixture that exits early from main."""
    cfg = MagicMock()
    cfg.validate_config.return_value = False
    return cfg


def test_debug_flag_sets_debug_logging_level(mock_config_manager):
    """--debug should configure structured logging at DEBUG level."""
    with (
        patch("src.zulipchat_mcp.server.setup_structured_logging") as mock_setup,
        patch("src.zulipchat_mcp.server.get_logger") as mock_get_logger,
        patch("src.zulipchat_mcp.server.init_config_manager") as mock_init_cfg,
        patch.object(sys, "argv", ["zulipchat-mcp", "--debug"]),
    ):
        mock_get_logger.return_value = MagicMock()
        mock_init_cfg.return_value = mock_config_manager

        server.main()

        mock_setup.assert_called_once_with("DEBUG")


def test_default_logging_level_is_info(mock_config_manager):
    """Without --debug, structured logging should use INFO."""
    with (
        patch("src.zulipchat_mcp.server.setup_structured_logging") as mock_setup,
        patch("src.zulipchat_mcp.server.get_logger") as mock_get_logger,
        patch("src.zulipchat_mcp.server.init_config_manager") as mock_init_cfg,
        patch.object(sys, "argv", ["zulipchat-mcp"]),
    ):
        mock_get_logger.return_value = MagicMock()
        mock_init_cfg.return_value = mock_config_manager

        server.main()

        mock_setup.assert_called_once_with("INFO")


def test_server_disables_global_fastmcp_tasks():
    """Task support must be opt-in per tool, not a server-wide default."""
    cfg = MagicMock()
    cfg.validate_config.return_value = True
    logger = MagicMock()
    mcp = MagicMock()

    with (
        patch("src.zulipchat_mcp.server.setup_structured_logging"),
        patch("src.zulipchat_mcp.server.get_logger", return_value=logger),
        patch("src.zulipchat_mcp.server.init_config_manager", return_value=cfg),
        patch("src.zulipchat_mcp.server.init_database"),
        patch("src.zulipchat_mcp.server.FastMCP", return_value=mcp) as mock_fastmcp,
        patch("src.zulipchat_mcp.server.register_core_tools") as mock_register_core,
        patch.object(sys, "argv", ["zulipchat-mcp"]),
    ):
        server.main()

    kwargs = mock_fastmcp.call_args.kwargs
    assert kwargs["tasks"] is False
    assert kwargs["lifespan"] is not None
    mock_register_core.assert_called_once_with(mcp)
    mcp.run.assert_called_once_with()


@pytest.mark.asyncio
async def test_server_lifespan_keeps_listener_lazy_when_disabled():
    """Default startup should not initialize the listener until a tool needs it."""
    cfg = MagicMock()
    svc = MagicMock()

    with (
        patch(
            "src.zulipchat_mcp.server.init_service_manager", return_value=svc
        ) as mock_init,
        patch("src.zulipchat_mcp.server.shutdown_service_manager") as mock_shutdown,
    ):
        server_lifespan = server._build_server_lifespan(cfg, enable_listener=False)
        async with server_lifespan(MagicMock()) as context:
            assert context["service_manager"] is svc

    mock_init.assert_called_once_with(cfg, enable_listener=False)
    svc.start.assert_not_called()
    mock_shutdown.assert_called_once_with()


@pytest.mark.asyncio
async def test_server_lifespan_starts_listener_when_enabled():
    """The explicit listener flag should still start services at server boot."""
    cfg = MagicMock()
    svc = MagicMock()

    with (
        patch("src.zulipchat_mcp.server.init_service_manager", return_value=svc),
        patch("src.zulipchat_mcp.server.shutdown_service_manager") as mock_shutdown,
    ):
        server_lifespan = server._build_server_lifespan(cfg, enable_listener=True)
        async with server_lifespan(MagicMock()) as context:
            assert context["service_manager"] is svc

    svc.start.assert_called_once_with()
    mock_shutdown.assert_called_once_with()


def test_version_flag_exits_zero():
    """--version should exit with status code 0."""
    with patch.object(sys, "argv", ["zulipchat-mcp", "--version"]):
        with pytest.raises(SystemExit) as exc:
            server.main()
    assert exc.value.code == 0


def test_http_transport_passes_host_port_and_registers_tasks_extension():
    """--transport http should serve streamable-HTTP on the requested bind."""
    cfg = MagicMock()
    cfg.validate_config.return_value = True
    mcp = MagicMock()

    with (
        patch("src.zulipchat_mcp.server.setup_structured_logging"),
        patch("src.zulipchat_mcp.server.get_logger", return_value=MagicMock()),
        patch("src.zulipchat_mcp.server.init_config_manager", return_value=cfg),
        patch("src.zulipchat_mcp.server.init_database"),
        patch("src.zulipchat_mcp.server.FastMCP", return_value=mcp),
        patch("src.zulipchat_mcp.server.register_core_tools"),
        patch.object(
            sys,
            "argv",
            [
                "zulipchat-mcp",
                "--transport",
                "http",
                "--host",
                "0.0.0.0",
                "--port",
                "9000",
            ],
        ),
    ):
        server.main()

    mcp.add_extension.assert_called_once()
    mcp.run.assert_called_once_with(transport="http", host="0.0.0.0", port=9000)


def test_http_transport_non_localhost_without_auth_warns():
    """Binding HTTP beyond localhost with no auth configured at all must warn loudly."""
    cfg = MagicMock()
    cfg.validate_config.return_value = True
    logger = MagicMock()
    mcp = MagicMock()

    with (
        patch("src.zulipchat_mcp.server.setup_structured_logging"),
        patch("src.zulipchat_mcp.server.get_logger", return_value=logger),
        patch("src.zulipchat_mcp.server.init_config_manager", return_value=cfg),
        patch("src.zulipchat_mcp.server.init_database"),
        patch("src.zulipchat_mcp.server.FastMCP", return_value=mcp),
        patch("src.zulipchat_mcp.server.register_core_tools"),
        patch.dict("os.environ", {}, clear=False),
        patch.object(
            sys, "argv", ["zulipchat-mcp", "--transport", "http", "--host", "0.0.0.0"]
        ),
    ):
        import os

        os.environ.pop("ZULIPCHAT_MCP_SERVICE_TOKEN", None)
        os.environ.pop("ZULIPCHAT_MCP_OIDC_CLIENT_ID", None)
        server.main()

    assert any(
        "WITHOUT auth configured" in str(call) for call in logger.warning.call_args_list
    )


def test_http_transport_with_service_token_configures_multiauth():
    """A service token alone should produce a MultiAuth with one verifier and no OAuth server."""
    cfg = MagicMock()
    cfg.validate_config.return_value = True
    mcp = MagicMock()

    with (
        patch("src.zulipchat_mcp.server.setup_structured_logging"),
        patch("src.zulipchat_mcp.server.get_logger", return_value=MagicMock()),
        patch("src.zulipchat_mcp.server.init_config_manager", return_value=cfg),
        patch("src.zulipchat_mcp.server.init_database"),
        patch("src.zulipchat_mcp.server.FastMCP", return_value=mcp) as mock_fastmcp,
        patch("src.zulipchat_mcp.server.register_core_tools"),
        patch.dict("os.environ", {}, clear=False),
        patch.object(
            sys,
            "argv",
            ["zulipchat-mcp", "--transport", "http", "--service-token", "secret-token"],
        ),
    ):
        import os

        os.environ.pop("ZULIPCHAT_MCP_OIDC_CLIENT_ID", None)
        server.main()

    auth = mock_fastmcp.call_args.kwargs["auth"]
    assert auth is not None
    assert auth.server is None
    assert len(auth.verifiers) == 1
    assert "secret-token" in auth.verifiers[0].tokens
    assert auth.required_scopes == []


def test_http_transport_with_oidc_configures_multiauth_server():
    """OIDC client id + public URL should produce a MultiAuth with an OIDCProxy server."""
    cfg = MagicMock()
    cfg.validate_config.return_value = True
    mcp = MagicMock()
    fake_oidc_server = MagicMock()

    with (
        patch("src.zulipchat_mcp.server.setup_structured_logging"),
        patch("src.zulipchat_mcp.server.get_logger", return_value=MagicMock()),
        patch("src.zulipchat_mcp.server.init_config_manager", return_value=cfg),
        patch("src.zulipchat_mcp.server.init_database"),
        patch("src.zulipchat_mcp.server.FastMCP", return_value=mcp) as mock_fastmcp,
        patch("src.zulipchat_mcp.server.register_core_tools"),
        # OIDCProxy performs OIDC discovery (a real HTTP GET to config_url) at
        # construction time. Mock it so this stays a network-free unit test
        # regardless of whether --oidc-issuer's host is reachable/resolvable.
        patch(
            "fastmcp.server.auth.OIDCProxy", return_value=fake_oidc_server
        ) as mock_oidc_proxy,
        patch.object(
            sys,
            "argv",
            [
                "zulipchat-mcp",
                "--transport",
                "http",
                "--oidc-client-id",
                "test-client-id",
                "--oidc-client-secret",
                "test-secret",
                "--oidc-issuer",
                "https://gitlab.example.com",
                "--public-url",
                "https://your-mcp-server.example.com",
            ],
        ),
    ):
        server.main()

    auth = mock_fastmcp.call_args.kwargs["auth"]
    assert auth is not None
    assert auth.server is fake_oidc_server

    # issuer_url identifies this proxy's own issuer identity (used in the
    # OAuth metadata it publishes and as the audience/iss claim it checks on
    # its own issued tokens) — it must be the server's own public URL, never
    # the upstream IdP's URL (that belongs only in config_url, which locates
    # the upstream's discovery document).
    oidc_kwargs = mock_oidc_proxy.call_args.kwargs
    assert oidc_kwargs["issuer_url"] == "https://your-mcp-server.example.com"
    assert oidc_kwargs["issuer_url"] != "https://gitlab.example.com"
    assert auth.verifiers == []

    # Without an explicit scope request, OAuthProxy omits the `scope`
    # parameter from the upstream authorize redirect entirely, and providers
    # with no default_scopes fallback reject the request as invalid.
    assert oidc_kwargs["required_scopes"] == ["openid", "profile", "email"]

    # MultiAuth defaults its OWN required_scopes to server.required_scopes
    # when not given explicitly, which would turn the scope OIDCProxy needs
    # for the upstream IdP into a floor enforced on every request. Regression
    # test: this silently broke the separate service-token verifier (whose
    # tokens intentionally carry no scopes) the one time this wasn't pinned.
    assert auth.required_scopes == []


def test_http_transport_with_oidc_and_service_token_does_not_scope_gate_service_token():
    """The service token's scopeless tokens must not be floor-checked against OIDC's scopes."""
    cfg = MagicMock()
    cfg.validate_config.return_value = True
    mcp = MagicMock()
    fake_oidc_server = MagicMock()
    fake_oidc_server.required_scopes = ["openid", "profile", "email"]

    with (
        patch("src.zulipchat_mcp.server.setup_structured_logging"),
        patch("src.zulipchat_mcp.server.get_logger", return_value=MagicMock()),
        patch("src.zulipchat_mcp.server.init_config_manager", return_value=cfg),
        patch("src.zulipchat_mcp.server.init_database"),
        patch("src.zulipchat_mcp.server.FastMCP", return_value=mcp) as mock_fastmcp,
        patch("src.zulipchat_mcp.server.register_core_tools"),
        patch(
            "fastmcp.server.auth.OIDCProxy", return_value=fake_oidc_server
        ),
        patch.object(
            sys,
            "argv",
            [
                "zulipchat-mcp",
                "--transport",
                "http",
                "--service-token",
                "secret-token",
                "--oidc-client-id",
                "test-client-id",
                "--oidc-client-secret",
                "test-secret",
                "--oidc-issuer",
                "https://gitlab.example.com",
                "--public-url",
                "https://your-mcp-server.example.com",
            ],
        ),
    ):
        server.main()

    auth = mock_fastmcp.call_args.kwargs["auth"]
    assert auth.server is fake_oidc_server
    assert len(auth.verifiers) == 1
    assert auth.required_scopes == []


def test_http_transport_oidc_without_public_url_errors_and_skips_oidc():
    """--oidc-client-id without --public-url must not silently construct a broken OIDCProxy."""
    cfg = MagicMock()
    cfg.validate_config.return_value = True
    logger = MagicMock()
    mcp = MagicMock()

    with (
        patch("src.zulipchat_mcp.server.setup_structured_logging"),
        patch("src.zulipchat_mcp.server.get_logger", return_value=logger),
        patch("src.zulipchat_mcp.server.init_config_manager", return_value=cfg),
        patch("src.zulipchat_mcp.server.init_database"),
        patch("src.zulipchat_mcp.server.FastMCP", return_value=mcp) as mock_fastmcp,
        patch("src.zulipchat_mcp.server.register_core_tools"),
        patch.dict("os.environ", {}, clear=False),
        patch.object(
            sys,
            "argv",
            [
                "zulipchat-mcp",
                "--transport",
                "http",
                "--host",
                "0.0.0.0",
                "--oidc-client-id",
                "test-client-id",
                "--oidc-issuer",
                "https://gitlab.example.com",
            ],
        ),
    ):
        import os

        os.environ.pop("ZULIPCHAT_MCP_PUBLIC_URL", None)
        os.environ.pop("ZULIPCHAT_MCP_SERVICE_TOKEN", None)
        server.main()

    assert any("--public-url" in str(call) for call in logger.error.call_args_list)
    auth = mock_fastmcp.call_args.kwargs["auth"]
    assert auth is None


def test_http_transport_oidc_without_issuer_errors_and_skips_oidc():
    """--oidc-client-id without --oidc-issuer must not silently construct a broken OIDCProxy."""
    cfg = MagicMock()
    cfg.validate_config.return_value = True
    logger = MagicMock()
    mcp = MagicMock()

    with (
        patch("src.zulipchat_mcp.server.setup_structured_logging"),
        patch("src.zulipchat_mcp.server.get_logger", return_value=logger),
        patch("src.zulipchat_mcp.server.init_config_manager", return_value=cfg),
        patch("src.zulipchat_mcp.server.init_database"),
        patch("src.zulipchat_mcp.server.FastMCP", return_value=mcp) as mock_fastmcp,
        patch("src.zulipchat_mcp.server.register_core_tools"),
        patch.dict("os.environ", {}, clear=False),
        patch.object(
            sys,
            "argv",
            [
                "zulipchat-mcp",
                "--transport",
                "http",
                "--host",
                "0.0.0.0",
                "--oidc-client-id",
                "test-client-id",
                "--public-url",
                "https://your-mcp-server.example.com",
            ],
        ),
    ):
        import os

        os.environ.pop("ZULIPCHAT_MCP_OIDC_ISSUER", None)
        os.environ.pop("ZULIPCHAT_MCP_SERVICE_TOKEN", None)
        server.main()

    assert any("--oidc-issuer" in str(call) for call in logger.error.call_args_list)
    auth = mock_fastmcp.call_args.kwargs["auth"]
    assert auth is None
