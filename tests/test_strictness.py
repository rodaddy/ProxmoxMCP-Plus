"""Tests for the v2 strict-schema enforcement.

These run ONLY in the v2 environment (`.venv-v2`), which has `mcp==2.0.0`.
They are skipped in the v1 environment, where `mcp.server.mcpserver` does not
exist -- so a v1 test run does not turn red just for lacking the v2 SDK.

Run:
    ./.venv-v2/bin/python -m pytest tests/test_strictness.py -v

No `tmp_path` / `tmp_path_factory` fixtures are used anywhere here: pytest
resolves those under `$TMPDIR`, which is sandbox-local and invisible across
runtimes. These tests need no filesystem at all.
"""

from __future__ import annotations

import pytest

pytest.importorskip(
    "mcp.server.mcpserver",
    reason="v2 SDK (mcp==2.0.0) not installed; run in .venv-v2",
)

from mcp.server.mcpserver import MCPServer  # noqa: E402

from proxmox_mcp.strictness import (  # noqa: E402
    StrictnessSurfaceError,
    enforce_strict_schemas,
)
from tests.stub_proxmox import ForbiddenWriteError, StubProxmoxAPI  # noqa: E402


@pytest.fixture
def anyio_backend() -> str:
    """Run the async tests on asyncio only (no trio dependency)."""
    return "asyncio"


def _server_with_tool() -> MCPServer:
    server = MCPServer("test")

    @server.tool(description="add two ints")
    def add(a: int, b: int) -> str:
        return str(a + b)

    return server


@pytest.mark.anyio
async def test_permissive_default_silently_drops_undeclared_param():
    """Documents the defect being fixed, so a future SDK bump that fixes it
    upstream shows up as a failure here rather than passing unnoticed."""
    server = _server_with_tool()
    result = await server.call_tool("add", {"a": 1, "b": 2, "BOGUS": "x"})
    assert result.is_error is False
    assert "3" in str(result.content)


@pytest.mark.anyio
async def test_enforced_schema_rejects_undeclared_param_by_name():
    server = _server_with_tool()
    enforce_strict_schemas(server)
    with pytest.raises(Exception) as excinfo:
        await server.call_tool("add", {"a": 1, "b": 2, "BOGUS": "x"})
    # The name must appear: a generic "invalid arguments" gives a client
    # nothing to correct.
    assert "BOGUS" in str(excinfo.value)


@pytest.mark.anyio
async def test_enforced_schema_still_accepts_valid_flat_arguments():
    """Strictness must not change the wire shape -- arguments stay flat."""
    server = _server_with_tool()
    enforce_strict_schemas(server)
    result = await server.call_tool("add", {"a": 1, "b": 2})
    assert result.is_error is False
    assert "3" in str(result.content)


@pytest.mark.anyio
async def test_enforcement_advertises_additional_properties_false():
    server = _server_with_tool()
    enforce_strict_schemas(server)
    tools = await server.list_tools()
    assert tools[0].input_schema["additionalProperties"] is False


def test_enforce_returns_the_tools_it_tightened():
    server = _server_with_tool()
    assert enforce_strict_schemas(server) == ["add"]


def test_missing_tool_manager_raises_surface_error():
    """The private surface must fail loudly, never degrade to permissive."""

    class NotAServer:
        pass

    with pytest.raises(StrictnessSurfaceError, match="_tool_manager"):
        enforce_strict_schemas(NotAServer())


def test_stub_forbids_write_verbs():
    api = StubProxmoxAPI()
    with pytest.raises(ForbiddenWriteError, match="write"):
        api.nodes("proxmox01").qemu("100").status.start.post()


def test_v2_server_exposes_only_read_only_tools():
    from proxmox_mcp.server_v2 import READ_ONLY_TOOLS, build_server

    server = build_server(proxmox_api=StubProxmoxAPI())
    assert sorted(server._tool_manager._tools) == sorted(READ_ONLY_TOOLS)
    # No write verb may sneak in.
    for forbidden in ("create_vm", "delete_vm", "execute_vm_command",
                      "delete_container", "stop_vm"):
        assert forbidden not in server._tool_manager._tools
