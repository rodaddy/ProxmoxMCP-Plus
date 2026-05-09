"""
Module for managing LXC container console operations via SSH + pct exec.

pct exec is not exposed through the Proxmox REST API; it must be invoked
as a subprocess on the Proxmox node where the container lives. This module
SSHes to the appropriate node and runs:
    pct exec <vmid> -- sh -c '<cmd>'

Enhanced with optional cluster-aware routing: when the specific node is not
known (or when node routing fails), falls back to the ClusterSSHClient
which connects to the primary node and lets Proxmox route automatically.
"""

import os
import shlex
import logging
import subprocess
from typing import Any, Dict, Optional

import paramiko  # type: ignore[import-untyped]


class ContainerConsoleManager:
    """Execute shell commands inside LXC containers via SSH + pct exec.

    Supports an optional :class:`~proxmox_mcp.core.cluster_ssh.ClusterSSHClient`
    for cluster-aware fallback when the target node is unknown or unreachable.
    """

    def __init__(
        self,
        proxmox_api: Any,
        ssh_config: Any,
        cluster_ssh: Optional[Any] = None,
    ) -> None:
        self.proxmox = proxmox_api
        self.ssh_cfg = ssh_config
        self.logger = logging.getLogger("proxmox-mcp.ct-console")
        self._cluster_ssh = cluster_ssh

    def _ssh_host(self, node: str) -> str:
        return self.ssh_cfg.host_overrides.get(node, node)

    def _use_system_ssh(self) -> bool:
        return bool(getattr(self.ssh_cfg, "prefer_ssh_client", False))

    def _execute_via_system_ssh(self, target: str, cmd: str) -> Dict[str, Any]:
        ssh_cmd = ["ssh"]
        key_file = getattr(self.ssh_cfg, "key_file", None)
        if key_file:
            ssh_cmd.extend(["-i", os.path.expanduser(key_file)])
        if getattr(self.ssh_cfg, "port", None):
            ssh_cmd.extend(["-p", str(self.ssh_cfg.port)])
        ssh_cmd.extend([target, cmd])

        self.logger.debug("Executing via OpenSSH client: %s", " ".join(shlex.quote(p) for p in ssh_cmd))
        completed = subprocess.run(  # noqa: S603
            ssh_cmd,
            capture_output=True,
            text=True,
            timeout=70,
            check=False,
        )
        return {
            "success": completed.returncode == 0,
            "output": completed.stdout,
            "error": completed.stderr,
            "exit_code": completed.returncode,
        }

    def execute_command(self, node: str, vmid: str, command: str) -> Dict[str, Any]:
        """Execute *command* inside the LXC container identified by *vmid* on *node*.

        If ``node`` is empty/None and a :class:`ClusterSSHClient` is available,
        cluster-aware routing is used automatically.  If the direct node
        connection fails and cluster SSH is available, it falls back to the
        cluster-aware path.

        Args:
            node:    Proxmox node name (e.g. 'pve1'). Can be empty/None
                     to trigger cluster-aware routing.
            vmid:    Container ID as a string (e.g. '101').
            command: Shell command to run inside the container.

        Returns:
            {"success": bool, "output": str, "error": str, "exit_code": int}

        Raises:
            ValueError:  Container is not running (when node is specified).
            RuntimeError: SSH / pct exec failure.
        """
        # If no node specified, try cluster-aware routing
        if not node and self._cluster_ssh is not None:
            return self._execute_via_cluster(vmid, command)

        # Standard path: direct SSH to the specified node
        try:
            return self._execute_direct(node, vmid, command)
        except Exception as direct_err:
            # If we have cluster SSH available, try that as fallback
            if self._cluster_ssh is not None:
                self.logger.warning(
                    "Direct exec on %s failed (%s), falling back to cluster-aware routing",
                    node,
                    direct_err,
                )
                try:
                    return self._execute_via_cluster(vmid, command)
                except Exception as cluster_err:
                    self.logger.error("Cluster-aware fallback also failed: %s", cluster_err)
                    raise direct_err from cluster_err
            raise

    def _execute_via_cluster(self, vmid: str, command: str) -> Dict[str, Any]:
        """Execute via the ClusterSSHClient (cluster-aware routing)."""
        self.logger.info("Using cluster-aware routing for CT %s: %s", vmid, command)
        result = self._cluster_ssh.container_exec(int(vmid), command)
        return {
            "success": result.success,
            "output": result.stdout,
            "error": result.stderr,
            "exit_code": result.exit_code,
        }

    def _execute_direct(self, node: str, vmid: str, command: str) -> Dict[str, Any]:
        """Execute via direct SSH to the specified node (original behaviour)."""
        # 1. Verify container is running via Proxmox API
        status = self.proxmox.nodes(node).lxc(vmid).status.current.get()
        if status.get("status") != "running":
            raise ValueError(f"Container {vmid} on node {node} is not running")

        # 2. Build pct exec command
        prefix = "sudo " if self.ssh_cfg.use_sudo else ""
        cmd = f"{prefix}/usr/sbin/pct exec {shlex.quote(str(vmid))} -- sh -c {shlex.quote(command)}"
        self.logger.info("Executing on CT %s@%s: %s", vmid, node, command)
        target = self._ssh_host(node)

        if self._use_system_ssh():
            return self._execute_via_system_ssh(target, cmd)

        # 3. SSH to node and run command
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        if self.ssh_cfg.known_hosts_file:
            client.load_host_keys(os.path.expanduser(self.ssh_cfg.known_hosts_file))
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        if not self.ssh_cfg.strict_host_key_checking:
            self.logger.warning(
                "Ignoring strict_host_key_checking=false for Paramiko execution; "
                "unknown SSH host keys are always rejected. "
                "Use prefer_ssh_client=true if you need OpenSSH-specific host key behavior."
            )

        connect_kwargs: Dict[str, Any] = dict(
            hostname=target,
            port=self.ssh_cfg.port,
            username=self.ssh_cfg.user,
            timeout=10,
        )
        if self.ssh_cfg.key_file:
            connect_kwargs["key_filename"] = os.path.expanduser(self.ssh_cfg.key_file)
        elif self.ssh_cfg.password:
            connect_kwargs["password"] = self.ssh_cfg.password

        try:
            client.connect(**connect_kwargs)
            _, stdout, stderr = client.exec_command(cmd, timeout=60)
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            exit_code = stdout.channel.recv_exit_status()
            return {
                "success": exit_code == 0,
                "output": out,
                "error": err,
                "exit_code": exit_code,
            }
        except paramiko.SSHException as e:
            self.logger.error("SSH error connecting to %s: %s", node, e)
            raise RuntimeError(f"SSH error connecting to node {node}: {e}") from e
        finally:
            client.close()
