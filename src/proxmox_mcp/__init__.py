"""
Proxmox MCP Server - A Model Context Protocol server for interacting with Proxmox hypervisors.
"""

from typing import TYPE_CHECKING, Any

__version__ = "0.1.0"
__all__ = ["ProxmoxMCPServer"]

if TYPE_CHECKING:  # pragma: no cover - type-checker only
    from .server import ProxmoxMCPServer


def __getattr__(name: str) -> Any:
    """Resolve ``ProxmoxMCPServer`` on first access instead of at import time.

    ``from .server import ProxmoxMCPServer`` at module scope made importing
    ANY submodule -- including ``proxmox_mcp.server_v2`` -- also import the v1
    server, and with it ``mcp.server.fastmcp``. That module does not exist in
    SDK 2.x, so the v2 environment could not import its own server at all
    (``ModuleNotFoundError: No module named 'mcp.server.fastmcp'``).

    Deferring the import keeps ``from proxmox_mcp import ProxmoxMCPServer``
    working exactly as before for every existing v1 caller, while letting the
    v2 module load in an environment where the v1 SDK is deliberately absent.
    PEP 562 module ``__getattr__`` is the smallest change that achieves that:
    no v1 call site has to change.
    """
    if name == "ProxmoxMCPServer":
        from .server import ProxmoxMCPServer

        return ProxmoxMCPServer
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
