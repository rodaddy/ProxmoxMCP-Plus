# Ported from rodaddy/proxmox-skill (TypeScript)
# Original: https://github.com/rodaddy/proxmox-skill
# Capabilities: cluster-aware SSH routing, streaming exec, parallel node discovery
"""
Streaming container command execution MCP tool.

Uses :class:`~proxmox_mcp.core.cluster_ssh.ClusterSSHClient` for
cluster-aware routing -- no need to know which node hosts the container.
Output is streamed in real-time chunks.

Falls back to buffered exec if streaming encounters issues.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from mcp.types import TextContent as Content

from proxmox_mcp.core.cluster_ssh import ClusterSSHClient, ExecResult
from proxmox_mcp.tools.base import ProxmoxTool

logger = logging.getLogger("proxmox-mcp.streaming-exec")


class StreamingExecTools(ProxmoxTool):
    """MCP tool for streaming command execution inside LXC containers.

    This tool connects to the cluster primary node and lets Proxmox
    route ``pct exec`` to the correct node automatically.  Output is
    collected in real-time chunks and returned as a structured result.
    """

    def __init__(
        self,
        proxmox_api: Any,
        cluster_ssh: ClusterSSHClient,
        command_policy: Any = None,
        metrics: Any = None,
        job_store: Any = None,
    ) -> None:
        super().__init__(proxmox_api, metrics=metrics, job_store=job_store)
        self._cluster_ssh = cluster_ssh
        self._command_policy = command_policy

    def execute_container_command_streaming(
        self,
        vmid: int,
        command: str,
        approval_token: Optional[str] = None,
    ) -> List[Content]:
        """Execute a command inside an LXC container with streaming output.

        Cluster-aware: does NOT require specifying the node.  The cluster
        primary is contacted and Proxmox routes the command automatically.

        Output is collected in real-time chunks.  Each chunk records whether
        it came from stdout or stderr, preserving ordering.

        Args:
            vmid:           Container VMID (e.g. 101).
            command:        Shell command to run inside the container.
            approval_token: Optional policy approval token.

        Returns:
            List[Content] containing a JSON result with:
            - success (bool)
            - exit_code (int)
            - output (str) -- combined stdout
            - error (str) -- combined stderr
            - chunks (list) -- ordered list of {text, stream} dicts
            - mode ("streaming" | "buffered")
        """
        # Enforce command policy if configured
        if self._command_policy is not None:
            decision = self._command_policy.evaluate(command, approval_token=approval_token)
            if not decision.allowed:
                result = {
                    "success": False,
                    "exit_code": -1,
                    "output": "",
                    "error": f"Command blocked by policy: {decision.message}",
                    "chunks": [],
                    "mode": "blocked",
                }
                return [Content(type="text", text=json.dumps(result, indent=2))]

        # Try streaming first
        try:
            return self._exec_streaming(vmid, command)
        except Exception as stream_err:
            logger.warning(
                "Streaming exec failed for CT %d, falling back to buffered: %s",
                vmid,
                stream_err,
            )
            return self._exec_buffered(vmid, command)

    def _exec_streaming(self, vmid: int, command: str) -> List[Content]:
        """Execute with real-time streaming output."""
        chunks: List[Dict[str, str]] = []
        stdout_parts: List[str] = []
        stderr_parts: List[str] = []

        def on_output(data: str, is_error: bool) -> None:
            stream_name = "stderr" if is_error else "stdout"
            chunks.append({"text": data, "stream": stream_name})
            if is_error:
                stderr_parts.append(data)
            else:
                stdout_parts.append(data)

        exit_code = self._cluster_ssh.container_exec_stream(vmid, command, on_output)

        result = {
            "success": exit_code == 0,
            "exit_code": exit_code,
            "output": "".join(stdout_parts),
            "error": "".join(stderr_parts),
            "chunks": chunks,
            "mode": "streaming",
        }
        return [Content(type="text", text=json.dumps(result, indent=2))]

    def _exec_buffered(self, vmid: int, command: str) -> List[Content]:
        """Execute with buffered (non-streaming) output as fallback."""
        exec_result: ExecResult = self._cluster_ssh.container_exec(vmid, command)

        result = {
            "success": exec_result.success,
            "exit_code": exec_result.exit_code,
            "output": exec_result.stdout,
            "error": exec_result.stderr,
            "chunks": [],
            "mode": "buffered",
        }
        return [Content(type="text", text=json.dumps(result, indent=2))]

    def execute_node_command(
        self,
        node: str,
        command: str,
        approval_token: Optional[str] = None,
    ) -> List[Content]:
        """Execute a command directly on a Proxmox node (not in a container).

        Args:
            node:           Proxmox node name (e.g. 'pve1').
            command:        Shell command to run on the node.
            approval_token: Optional policy approval token.

        Returns:
            List[Content] containing a JSON result.
        """
        if self._command_policy is not None:
            decision = self._command_policy.evaluate(command, approval_token=approval_token)
            if not decision.allowed:
                result = {
                    "success": False,
                    "exit_code": -1,
                    "output": "",
                    "error": f"Command blocked by policy: {decision.message}",
                }
                return [Content(type="text", text=json.dumps(result, indent=2))]

        try:
            exec_result = self._cluster_ssh.node_exec(node, command)
            result = {
                "success": exec_result.success,
                "exit_code": exec_result.exit_code,
                "output": exec_result.stdout,
                "error": exec_result.stderr,
            }
            return [Content(type="text", text=json.dumps(result, indent=2))]
        except Exception as exc:
            self._handle_error("execute_node_command", exc)
