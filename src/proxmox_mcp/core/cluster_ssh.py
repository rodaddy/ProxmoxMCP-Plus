# Ported from rodaddy/proxmox-skill (TypeScript)
# Original: https://github.com/rodaddy/proxmox-skill
# Capabilities: cluster-aware SSH routing, streaming exec, parallel node discovery
"""
Cluster-aware SSH client for Proxmox.

Unlike the existing container_manager.py which requires knowing the exact
node a container lives on, this module connects to a primary/first-enabled
Proxmox node and lets the Proxmox cluster route ``pct exec`` commands to
the correct node automatically.

Features:
- Cluster-aware routing via a primary node
- Streaming exec with real-time output callbacks
- Non-streaming (buffered) exec
- Raw SSH exec on the node itself (not in a container)

Requires paramiko (already a project dependency).
"""

from __future__ import annotations

import logging
import os
import shlex
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol

import paramiko  # type: ignore[import-untyped]

from proxmox_mcp.config.models import SSHConfig

logger = logging.getLogger("proxmox-mcp.cluster-ssh")


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class ExecResult:
    """Result of a remote command execution."""

    success: bool
    stdout: str
    stderr: str
    exit_code: int


@dataclass
class NodeEndpoint:
    """Resolved SSH endpoint for a Proxmox node."""

    name: str
    hostname: str
    port: int
    username: str
    key_file: Optional[str] = None
    password: Optional[str] = None
    use_sudo: bool = False


# ---------------------------------------------------------------------------
# Protocol for things that look like a proxmoxer API
# ---------------------------------------------------------------------------

class ProxmoxAPILike(Protocol):
    """Minimal interface we need from the proxmoxer API."""

    @property
    def nodes(self) -> Any: ...


# ---------------------------------------------------------------------------
# Cluster SSH Client
# ---------------------------------------------------------------------------

class ClusterSSHClient:
    """SSH client with cluster-aware routing for Proxmox containers.

    The key insight ported from the TypeScript skill: Proxmox clusters route
    ``pct exec`` calls to the correct node automatically.  We only need to
    SSH into *one* node (the "primary") and the cluster handles the rest.
    """

    def __init__(
        self,
        ssh_config: SSHConfig,
        proxmox_api: Any,
    ) -> None:
        self._ssh_config = ssh_config
        self._proxmox = proxmox_api
        self._primary_node: Optional[NodeEndpoint] = None

    # ------------------------------------------------------------------
    # Primary node resolution
    # ------------------------------------------------------------------

    def _resolve_ssh_host(self, node_name: str) -> str:
        """Resolve the SSH hostname for a node, checking host_overrides."""
        return self._ssh_config.host_overrides.get(node_name, node_name)

    def _build_endpoint(self, node_name: str) -> NodeEndpoint:
        """Build a NodeEndpoint from the SSH config and a node name."""
        return NodeEndpoint(
            name=node_name,
            hostname=self._resolve_ssh_host(node_name),
            port=self._ssh_config.port,
            username=self._ssh_config.user,
            key_file=self._ssh_config.key_file,
            password=self._ssh_config.password,
            use_sudo=self._ssh_config.use_sudo,
        )

    def get_primary_node(self) -> NodeEndpoint:
        """Auto-select the first enabled/online node from the cluster.

        The result is cached for the lifetime of this client instance.
        Call :meth:`reset_primary` to force re-discovery.
        """
        if self._primary_node is not None:
            return self._primary_node

        try:
            nodes_raw = self._proxmox.nodes.get()
            if not isinstance(nodes_raw, list):
                nodes_raw = []
        except Exception as exc:
            logger.warning("Failed to query cluster nodes: %s", exc)
            nodes_raw = []

        # Prefer an online node; fall back to first available
        online_nodes: List[Dict[str, Any]] = []
        all_nodes: List[Dict[str, Any]] = []

        for node in nodes_raw:
            if not isinstance(node, dict):
                continue
            name = node.get("node")
            if not name:
                continue
            all_nodes.append(node)
            status = str(node.get("status", "")).lower()
            if status == "online":
                online_nodes.append(node)

        candidates = online_nodes or all_nodes
        if not candidates:
            raise RuntimeError(
                "No Proxmox nodes found via API. Cannot determine primary node "
                "for cluster-aware SSH routing."
            )

        chosen = candidates[0]
        node_name = chosen["node"]
        self._primary_node = self._build_endpoint(node_name)
        logger.info("Primary node selected for cluster SSH: %s (%s)", node_name, self._primary_node.hostname)
        return self._primary_node

    def reset_primary(self) -> None:
        """Clear the cached primary node so the next call re-discovers."""
        self._primary_node = None

    # ------------------------------------------------------------------
    # Internal: paramiko connection helper
    # ------------------------------------------------------------------

    def _connect(self, endpoint: NodeEndpoint) -> paramiko.SSHClient:
        """Open an SSH connection to the given endpoint."""
        client = paramiko.SSHClient()
        client.load_system_host_keys()

        known_hosts = self._ssh_config.known_hosts_file
        if known_hosts:
            client.load_host_keys(os.path.expanduser(known_hosts))

        if self._ssh_config.strict_host_key_checking:
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        connect_kwargs: Dict[str, Any] = {
            "hostname": endpoint.hostname,
            "port": endpoint.port,
            "username": endpoint.username,
            "timeout": 15,
        }
        if endpoint.key_file:
            connect_kwargs["key_filename"] = os.path.expanduser(endpoint.key_file)
        elif endpoint.password:
            connect_kwargs["password"] = endpoint.password

        client.connect(**connect_kwargs)
        return client

    def _build_pct_command(self, vmid: int, command: str, use_sudo: bool) -> str:
        """Build the ``pct exec`` SSH command string."""
        escaped_cmd = command.replace("'", "'\\''")
        prefix = "sudo " if use_sudo else ""
        return f"{prefix}pct exec {vmid} -- bash -c '{escaped_cmd}'"

    # ------------------------------------------------------------------
    # Container exec (non-streaming / buffered)
    # ------------------------------------------------------------------

    def container_exec(self, vmid: int, command: str) -> ExecResult:
        """Execute a command inside a container via the primary node.

        Cluster-aware: connects to the primary node and lets Proxmox
        route the ``pct exec`` to the correct node automatically.

        Args:
            vmid:    Container VMID.
            command: Shell command to run inside the container.

        Returns:
            ExecResult with stdout, stderr, and exit code.
        """
        endpoint = self.get_primary_node()
        ssh_cmd = self._build_pct_command(vmid, command, endpoint.use_sudo)

        logger.info("container_exec CT %d via %s: %s", vmid, endpoint.name, command)

        client = self._connect(endpoint)
        try:
            _, stdout_ch, stderr_ch = client.exec_command(ssh_cmd, timeout=120)
            stdout_data = stdout_ch.read().decode("utf-8", errors="replace")
            stderr_data = stderr_ch.read().decode("utf-8", errors="replace")
            exit_code = stdout_ch.channel.recv_exit_status()

            return ExecResult(
                success=exit_code == 0,
                stdout=stdout_data,
                stderr=stderr_data,
                exit_code=exit_code,
            )
        finally:
            client.close()

    # ------------------------------------------------------------------
    # Container exec (streaming)
    # ------------------------------------------------------------------

    def container_exec_stream(
        self,
        vmid: int,
        command: str,
        on_output: Callable[[str, bool], None],
    ) -> int:
        """Execute a command with real-time streaming output.

        Cluster-aware: connects to the primary node and lets Proxmox
        route the ``pct exec`` to the correct node automatically.

        Args:
            vmid:      Container VMID.
            command:   Shell command to run inside the container.
            on_output: Callback ``(data: str, is_error: bool) -> None``
                       invoked for each chunk of stdout/stderr.

        Returns:
            The command's exit code.
        """
        endpoint = self.get_primary_node()
        ssh_cmd = self._build_pct_command(vmid, command, endpoint.use_sudo)

        logger.info("container_exec_stream CT %d via %s: %s", vmid, endpoint.name, command)

        client = self._connect(endpoint)
        try:
            transport = client.get_transport()
            if transport is None:
                raise RuntimeError("SSH transport unavailable")

            channel = transport.open_session()
            channel.exec_command(ssh_cmd)

            # Read in chunks until the channel closes
            buf_size = 4096
            while True:
                # Check stdout
                if channel.recv_ready():
                    data = channel.recv(buf_size).decode("utf-8", errors="replace")
                    if data:
                        on_output(data, False)

                # Check stderr
                if channel.recv_stderr_ready():
                    data = channel.recv_stderr(buf_size).decode("utf-8", errors="replace")
                    if data:
                        on_output(data, True)

                # Check if channel is done
                if channel.exit_status_ready():
                    # Drain any remaining data
                    while channel.recv_ready():
                        data = channel.recv(buf_size).decode("utf-8", errors="replace")
                        if data:
                            on_output(data, False)
                    while channel.recv_stderr_ready():
                        data = channel.recv_stderr(buf_size).decode("utf-8", errors="replace")
                        if data:
                            on_output(data, True)
                    break

            exit_code = channel.recv_exit_status()
            channel.close()
            return exit_code
        finally:
            client.close()

    # ------------------------------------------------------------------
    # Raw SSH exec (on the node itself, not in a container)
    # ------------------------------------------------------------------

    def node_exec(self, node_name: str, command: str) -> ExecResult:
        """Execute a command directly on a Proxmox node via SSH.

        Unlike :meth:`container_exec`, this runs the command on the node
        itself, not inside a container.

        Args:
            node_name: Name of the Proxmox node (used to look up hostname
                       from host_overrides or used directly).
            command:   Shell command to execute on the node.

        Returns:
            ExecResult with stdout, stderr, and exit code.
        """
        endpoint = self._build_endpoint(node_name)

        logger.info("node_exec on %s: %s", node_name, command)

        client = self._connect(endpoint)
        try:
            _, stdout_ch, stderr_ch = client.exec_command(command, timeout=120)
            stdout_data = stdout_ch.read().decode("utf-8", errors="replace")
            stderr_data = stderr_ch.read().decode("utf-8", errors="replace")
            exit_code = stdout_ch.channel.recv_exit_status()

            return ExecResult(
                success=exit_code == 0,
                stdout=stdout_data,
                stderr=stderr_data,
                exit_code=exit_code,
            )
        finally:
            client.close()
