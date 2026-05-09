# Ported from rodaddy/proxmox-skill (TypeScript)
# Original: https://github.com/rodaddy/proxmox-skill
# Capabilities: cluster-aware SSH routing, streaming exec, parallel node discovery
"""
Real-time cluster monitoring MCP tool.

Provides a snapshot of the entire Proxmox cluster: node status, resource
usage, and per-node container/VM counts.  Uses parallel discovery via
:class:`~proxmox_mcp.core.node_discovery.NodeDiscovery`.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from mcp.types import TextContent as Content

from proxmox_mcp.core.node_discovery import NodeDiscovery
from proxmox_mcp.tools.base import ProxmoxTool

logger = logging.getLogger("proxmox-mcp.monitor")


def _bytes_to_human(n: int | float) -> str:
    """Convert bytes to human-readable string (binary units)."""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "0.00 B"
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    idx = 0
    while n >= 1024.0 and idx < len(units) - 1:
        n /= 1024.0
        idx += 1
    return f"{n:.2f} {units[idx]}"


class MonitorTools(ProxmoxTool):
    """MCP tool for real-time cluster monitoring.

    Combines node status data with parallel container/VM discovery to
    give a complete overview of cluster health and resource utilisation.
    """

    def __init__(
        self,
        proxmox_api: Any,
        node_discovery: NodeDiscovery,
        metrics: Any = None,
        job_store: Any = None,
    ) -> None:
        super().__init__(proxmox_api, metrics=metrics, job_store=job_store)
        self._discovery = node_discovery

    def get_cluster_overview(
        self,
        format_style: str = "pretty",
    ) -> List[Content]:
        """Get a comprehensive snapshot of the entire Proxmox cluster.

        Returns per-node:
        - Online/offline status
        - CPU usage (percentage and core count)
        - Memory usage (used / total)
        - Number of containers (running / total)
        - Number of VMs (running / total)

        Plus cluster-level aggregates.

        Args:
            format_style: "pretty" for human-readable, "json" for raw data.

        Returns:
            List[Content] with the cluster overview.
        """
        try:
            # Fetch node status info
            nodes_raw = self._safe_get_nodes()
            if not nodes_raw:
                return [Content(type="text", text="No nodes found in the cluster.")]

            # Fetch all containers and VMs in parallel
            all_containers = self._discovery.list_all_containers_sync()
            all_vms = self._discovery.list_all_vms_sync()

            # Build per-node data
            node_data: List[Dict[str, Any]] = []
            total_cpu_used = 0.0
            total_cpu_max = 0
            total_mem_used = 0
            total_mem_max = 0
            total_ct = 0
            total_ct_running = 0
            total_vm = 0
            total_vm_running = 0

            for node in nodes_raw:
                if not isinstance(node, dict):
                    continue
                name = node.get("node", "unknown")
                status = str(node.get("status", "unknown")).lower()

                cpu_frac = float(node.get("cpu", 0) or 0)
                cpu_pct = round(cpu_frac * 100.0, 1)
                maxcpu = int(node.get("maxcpu", 0) or 0)
                mem_used = int(node.get("mem", 0) or 0)
                mem_max = int(node.get("maxmem", 0) or 0)
                uptime = int(node.get("uptime", 0) or 0)

                # Count containers on this node
                node_cts = [c for c in all_containers if c.node == name]
                node_cts_running = [c for c in node_cts if c.status == "running"]

                # Count VMs on this node
                node_vms = [v for v in all_vms if v.node == name]
                node_vms_running = [v for v in node_vms if v.status == "running"]

                entry: Dict[str, Any] = {
                    "node": name,
                    "status": status,
                    "cpu_pct": cpu_pct,
                    "cpu_cores": maxcpu,
                    "mem_used": mem_used,
                    "mem_total": mem_max,
                    "mem_pct": round((mem_used / mem_max * 100.0), 1) if mem_max > 0 else 0.0,
                    "uptime_hours": round(uptime / 3600.0, 1),
                    "containers_total": len(node_cts),
                    "containers_running": len(node_cts_running),
                    "vms_total": len(node_vms),
                    "vms_running": len(node_vms_running),
                }
                node_data.append(entry)

                # Accumulate totals
                if status == "online":
                    total_cpu_used += cpu_frac * maxcpu
                    total_cpu_max += maxcpu
                    total_mem_used += mem_used
                    total_mem_max += mem_max
                total_ct += len(node_cts)
                total_ct_running += len(node_cts_running)
                total_vm += len(node_vms)
                total_vm_running += len(node_vms_running)

            cluster_summary: Dict[str, Any] = {
                "nodes_online": sum(1 for n in node_data if n["status"] == "online"),
                "nodes_total": len(node_data),
                "cpu_cores_total": total_cpu_max,
                "cpu_pct_avg": round((total_cpu_used / total_cpu_max * 100.0), 1) if total_cpu_max > 0 else 0.0,
                "mem_used_total": total_mem_used,
                "mem_total": total_mem_max,
                "mem_pct_avg": round((total_mem_used / total_mem_max * 100.0), 1) if total_mem_max > 0 else 0.0,
                "containers_total": total_ct,
                "containers_running": total_ct_running,
                "vms_total": total_vm,
                "vms_running": total_vm_running,
            }

            full_result = {
                "cluster": cluster_summary,
                "nodes": node_data,
            }

            if format_style == "json":
                return [Content(type="text", text=json.dumps(full_result, indent=2))]

            return self._render_pretty(cluster_summary, node_data)

        except Exception as exc:
            self._handle_error("get_cluster_overview", exc)

    def _safe_get_nodes(self) -> List[Dict[str, Any]]:
        """Fetch node list from the Proxmox API, handling errors."""
        try:
            raw = self.proxmox.nodes.get()
            if isinstance(raw, list):
                return raw
            return []
        except Exception as exc:
            logger.error("Failed to get nodes: %s", exc)
            return []

    @staticmethod
    def _render_pretty(
        cluster: Dict[str, Any],
        nodes: List[Dict[str, Any]],
    ) -> List[Content]:
        """Render a human-readable cluster overview."""
        lines: List[str] = []

        # Cluster summary
        lines.append("=== Proxmox Cluster Overview ===")
        lines.append("")
        lines.append(
            f"Nodes: {cluster['nodes_online']}/{cluster['nodes_total']} online"
        )
        lines.append(
            f"CPU: {cluster['cpu_pct_avg']:.1f}% avg across {cluster['cpu_cores_total']} cores"
        )
        lines.append(
            f"Memory: {_bytes_to_human(cluster['mem_used_total'])} / "
            f"{_bytes_to_human(cluster['mem_total'])} "
            f"({cluster['mem_pct_avg']:.1f}%)"
        )
        lines.append(
            f"Containers: {cluster['containers_running']}/{cluster['containers_total']} running"
        )
        lines.append(
            f"VMs: {cluster['vms_running']}/{cluster['vms_total']} running"
        )
        lines.append("")
        lines.append("--- Per-Node ---")

        for nd in nodes:
            status_icon = "OK" if nd["status"] == "online" else "DOWN"
            lines.append("")
            lines.append(f"{nd['node']} [{status_icon}]")
            lines.append(f"  CPU: {nd['cpu_pct']:.1f}% ({nd['cpu_cores']} cores)")
            lines.append(
                f"  Memory: {_bytes_to_human(nd['mem_used'])} / "
                f"{_bytes_to_human(nd['mem_total'])} ({nd['mem_pct']:.1f}%)"
            )
            lines.append(f"  Uptime: {nd['uptime_hours']:.1f}h")
            lines.append(
                f"  Containers: {nd['containers_running']}/{nd['containers_total']} running"
            )
            lines.append(
                f"  VMs: {nd['vms_running']}/{nd['vms_total']} running"
            )

        return [Content(type="text", text="\n".join(lines))]
