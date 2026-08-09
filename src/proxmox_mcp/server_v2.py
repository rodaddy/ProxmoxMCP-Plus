"""Additive MCP 2026-07-28 (SDK v2) server for Proxmox.

Truth state: WRITTEN, 2026-08-09. Not deployed. The live stdio/STREAMABLE v1
server (``proxmox_mcp.server``) is untouched and keeps serving every existing
caller; this module is a NEW, parallel entry point.

Why a separate module instead of migrating ``server.py`` in place
-----------------------------------------------------------------
``server.py`` imports ``mcp.server.fastmcp``, which does not exist in SDK 2.x
(no alias, no deprecation shim). Migrating in place would mean the repo can
serve exactly one wire revision at a time, and any rollback would be a code
revert. Keeping the two side by side means the v1 path and the v2 path are
independently runnable, and rolling back is "stop the v2 process" rather than
"revert and redeploy".

The cost is one deliberate duplication: the read-only tool surface is
registered twice, once per SDK. The underlying tool *implementations*
(``NodeTools``, ``VMTools``, ...) are shared, so behaviour cannot drift between
the two paths -- only the registration boilerplate is repeated.

Scope: READ-ONLY tools only
---------------------------
This module deliberately exposes only the six read-only verbs. The v1 server's
write verbs (``create_vm``, ``delete_container``, ``execute_vm_command``, ...)
are NOT registered here. A new wire revision is not the place to also re-expose
cluster mutation; the write surface can be added in a later, separately
reviewed change once the read path is proven in production.

Wire shape is preserved: arguments stay flat (``{"node": "proxmox01"}``), and
tool names match the v1 server exactly, so a client switching transports sees
the same tools with the same parameters.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Annotated, Any, Optional

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from .config.loader import load_config
from .core.logging import setup_logging
from .core.proxmox import ProxmoxManager
from .strictness import enforce_strict_schemas
from .tools.cluster import ClusterTools
from .tools.containers import ContainerTools
from .tools.node import NodeTools
from .tools.storage import StorageTools
from .tools.vm import VMTools

__all__ = ["build_server", "ProxmoxMCPServerV2", "READ_ONLY_TOOLS"]

#: The exact tool surface this module serves. Asserted by the done-means
#: script so an accidental addition (especially a write verb) fails the gate
#: rather than shipping quietly.
READ_ONLY_TOOLS = (
    "get_cluster_status",
    "get_containers",
    "get_node_status",
    "get_nodes",
    "get_storage",
    "get_vms",
)


def build_server(
    config_path: Optional[str] = None,
    *,
    proxmox_api: Any = None,
    strict: bool = True,
) -> MCPServer:
    """Build the v2 ``MCPServer`` with the read-only Proxmox tool surface.

    Args:
        config_path: Path to the JSON config. Defaults to ``PROXMOX_MCP_CONFIG``.
            Ignored when ``proxmox_api`` is supplied.
        proxmox_api: Pre-built Proxmox API object. Injecting a stub here is how
            the test suite exercises the wire without touching a real cluster;
            when supplied, no config is loaded and no connection is opened.
        strict: Enforce strict schemas (reject undeclared parameters). Only a
            test proving the permissive default should pass ``False``.

    Returns:
        A configured ``MCPServer``, ready for ``run()`` or an ASGI mount.
    """
    if proxmox_api is None:
        resolved = config_path or os.getenv("PROXMOX_MCP_CONFIG")
        if not resolved:
            raise ValueError(
                "PROXMOX_MCP_CONFIG environment variable must be set "
                "(or config_path passed)"
            )
        config = load_config(resolved)
        setup_logging(config.logging)
        proxmox_api = ProxmoxManager(config.proxmox, config.auth).api

    node_tools = NodeTools(proxmox_api)
    vm_tools = VMTools(proxmox_api)
    storage_tools = StorageTools(proxmox_api)
    cluster_tools = ClusterTools(proxmox_api)
    container_tools = ContainerTools(proxmox_api)

    server = MCPServer("ProxmoxMCP")

    @server.tool(description="List all nodes in the Proxmox cluster.")
    def get_nodes() -> Any:
        return node_tools.get_nodes()

    @server.tool(description="Get detailed status of a specific Proxmox node.")
    def get_node_status(
        node: Annotated[
            str,
            Field(description="Name/ID of node to query (e.g. 'proxmox01')"),
        ],
    ) -> Any:
        return node_tools.get_node_status(node)

    @server.tool(description="List all virtual machines across the cluster.")
    def get_vms() -> Any:
        return vm_tools.get_vms()

    @server.tool(description="List all LXC containers across the cluster.")
    def get_containers() -> Any:
        return container_tools.get_containers()

    @server.tool(description="List storage pools across the cluster.")
    def get_storage() -> Any:
        return storage_tools.get_storage()

    @server.tool(description="Get overall Proxmox cluster health and status.")
    def get_cluster_status() -> Any:
        return cluster_tools.get_cluster_status()

    if strict:
        enforce_strict_schemas(server)

    return server


class ProxmoxMCPServerV2:
    """Thin runner around :func:`build_server`.

    Mirrors the v1 class's shape so operators recognise it, but owns only
    transport startup -- construction lives in ``build_server`` so tests can
    build a server without running one.
    """

    def __init__(self, config_path: Optional[str] = None):
        self.config_path = config_path or os.getenv("PROXMOX_MCP_CONFIG")
        if not self.config_path:
            raise ValueError("PROXMOX_MCP_CONFIG environment variable must be set")
        self.config = load_config(self.config_path)
        self.logger = logging.getLogger("proxmox-mcp.v2")
        self.mcp = build_server(self.config_path)

    def start(self) -> None:
        """Serve the 2026-07-28 wire over stateless streamable HTTP.

        Binds the literal address from config. Do NOT substitute the name
        "localhost": on macOS that resolves ``::1`` first, so the server binds
        IPv6-only and every 127.0.0.1 client gets ECONNREFUSED that reads as
        "server down".
        """
        host = self.config.mcp.host
        port = self.config.mcp.port
        self.logger.info("Starting MCP v2 server on %s:%s", host, port)
        self.mcp.run(transport="streamable-http", host=host, port=port)


def main() -> None:
    try:
        ProxmoxMCPServerV2().start()
    except KeyboardInterrupt:
        print("\nShutting down gracefully...", file=sys.stderr)
        sys.exit(0)
    except Exception as exc:  # noqa: BLE001 - top-level entry point
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
