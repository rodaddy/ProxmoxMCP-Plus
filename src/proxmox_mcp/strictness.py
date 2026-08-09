"""Strict-schema enforcement for MCP tools.

Why this module exists
----------------------
The MCP Python SDK derives a tool's ``inputSchema`` from the handler's type
hints and, by default, produces a *permissive* schema: no
``additionalProperties: false``, and a pydantic argument model whose ``extra``
policy is unset. An undeclared parameter sent by a client is therefore
**silently dropped** and the call succeeds as if it had never been sent.

Reproduced live against ``mcp==2.0.0`` on 2026-08-09: calling a two-argument
tool with ``{"a": 1, "b": 2, "BOGUS": "x"}`` returned the correct result with
``is_error=False``. The v1 -> v2 SDK bump does NOT fix this; strictness must be
enforced deliberately.

The two halves, and why BOTH are required
-----------------------------------------
1. **Advertisement** -- ``additionalProperties: false`` on the published
   ``inputSchema``, so clients can see the contract.
2. **Enforcement** -- ``extra="forbid"`` on the pydantic argument model, which
   is what actually rejects the call.

Setting only (1) was verified insufficient: the schema advertised
``additionalProperties: false`` while the server still silently dropped the
undeclared parameter. An advertised-but-unenforced schema is worse than no
claim at all, because it invites clients to trust it. Setting both makes the
advertised contract and the runtime behaviour agree.

The SDK exposes no public author-facing hook for either half, so this reaches
through ``MCPServer._tool_manager``. That is a private surface: the exact
version pin in ``pyproject.toml`` is load-bearing, and
``test_strictness.py`` fails loudly if the surface moves.

Wire shape is deliberately preserved: arguments stay flat
(``{"node": "proxmox01"}``), never nested under an ``args`` key. Nesting would
be a breaking change for every existing caller.
"""

from __future__ import annotations

from typing import Any

__all__ = ["enforce_strict_schemas", "StrictnessSurfaceError"]


class StrictnessSurfaceError(RuntimeError):
    """Raised when the SDK's private tool surface is not shaped as expected.

    Fail loudly rather than silently serving permissive schemas: a server that
    quietly stopped enforcing strictness would accept undeclared parameters
    again with no signal, which is the exact defect this module exists to fix.
    """


def _tool_registry(server: Any) -> dict[str, Any]:
    """Return the SDK's internal ``name -> Tool`` mapping.

    Isolated so that an SDK layout change produces one clear error naming the
    attribute that moved, instead of an ``AttributeError`` from deep in a loop.
    """
    manager = getattr(server, "_tool_manager", None)
    if manager is None:
        raise StrictnessSurfaceError(
            "MCPServer has no '_tool_manager'; the SDK's private tool surface "
            "moved. Re-probe the installed mcp package and update "
            "proxmox_mcp.strictness."
        )

    tools = getattr(manager, "_tools", None)
    if not isinstance(tools, dict):
        raise StrictnessSurfaceError(
            "MCPServer._tool_manager has no '_tools' dict; the SDK's private "
            "tool surface moved. Re-probe the installed mcp package and "
            "update proxmox_mcp.strictness."
        )
    return tools


def enforce_strict_schemas(server: Any) -> list[str]:
    """Make every registered tool reject undeclared parameters by name.

    Applies both halves described in the module docstring to each tool already
    registered on ``server``. Call this AFTER all tools are registered; tools
    added later are unaffected.

    Returns the sorted names of the tools that were tightened, so a caller can
    assert the expected surface was covered rather than trusting a silent pass.

    Raises:
        StrictnessSurfaceError: if the SDK's private surface is not as expected.
    """
    tightened: list[str] = []

    for name, tool in _tool_registry(server).items():
        # Half 1: advertise the contract on the published schema.
        parameters = getattr(tool, "parameters", None)
        if not isinstance(parameters, dict):
            raise StrictnessSurfaceError(
                f"Tool {name!r} has no 'parameters' dict; the SDK's private "
                "tool surface moved. Re-probe the installed mcp package."
            )
        # Rebind rather than mutate in place: the dict may be shared with a
        # cached schema, and a silent in-place edit is harder to trace.
        tool.parameters = {**parameters, "additionalProperties": False}

        # Half 2: enforce it. This is the half that actually rejects.
        fn_metadata = getattr(tool, "fn_metadata", None)
        arg_model = getattr(fn_metadata, "arg_model", None)
        if arg_model is None:
            raise StrictnessSurfaceError(
                f"Tool {name!r} has no 'fn_metadata.arg_model'; the SDK's "
                "private tool surface moved. Re-probe the installed mcp "
                "package."
            )
        arg_model.model_config["extra"] = "forbid"
        arg_model.model_rebuild(force=True)

        tightened.append(name)

    return sorted(tightened)
