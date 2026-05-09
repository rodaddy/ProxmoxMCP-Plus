# Ported from rodaddy/proxmox-skill (TypeScript)
# Original: https://github.com/rodaddy/proxmox-skill
# Capabilities: cluster-aware SSH routing, streaming exec, parallel node discovery
"""
Parallel multi-node discovery for Proxmox clusters.

Queries all cluster nodes in parallel (via asyncio) to build a complete
inventory of containers and VMs.  Also provides lookup helpers to find
which node hosts a specific VMID.

Uses the existing proxmoxer-based API client from
:mod:`proxmox_mcp.core.proxmox`.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("proxmox-mcp.node-discovery")


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class ContainerInfo:
    """Lightweight container descriptor."""

    vmid: int
    name: str
    status: str
    node: str
    cpus: Optional[int] = None
    maxmem: Optional[int] = None
    mem: Optional[int] = None
    uptime: Optional[int] = None


@dataclass
class VMInfo:
    """Lightweight VM descriptor."""

    vmid: int
    name: str
    status: str
    node: str
    cpus: Optional[int] = None
    maxmem: Optional[int] = None
    mem: Optional[int] = None
    uptime: Optional[int] = None


@dataclass
class ContainerLocation:
    """Result of finding a container in the cluster."""

    node: str
    hostname: str
    container: ContainerInfo


@dataclass
class VMLocation:
    """Result of finding a VM in the cluster."""

    node: str
    hostname: str
    vm: VMInfo


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

class NodeDiscovery:
    """Parallel multi-node resource discovery.

    All discovery methods use a thread pool executor to query multiple
    Proxmox nodes concurrently (the proxmoxer library is synchronous, so
    we offload each per-node call into a thread and gather with asyncio).
    """

    def __init__(self, proxmox_api: Any, max_workers: int = 8) -> None:
        """
        Args:
            proxmox_api: An initialised ``proxmoxer.ProxmoxAPI`` instance.
            max_workers: Maximum threads for parallel queries.
        """
        self._proxmox = proxmox_api
        self._max_workers = max_workers

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_node_names(self) -> List[str]:
        """Return names of all online nodes in the cluster."""
        try:
            nodes_raw = self._proxmox.nodes.get()
            if not isinstance(nodes_raw, list):
                return []
        except Exception as exc:
            logger.error("Failed to enumerate cluster nodes: %s", exc)
            return []

        names: List[str] = []
        for node in nodes_raw:
            if not isinstance(node, dict):
                continue
            name = node.get("node")
            status = str(node.get("status", "")).lower()
            if name and status == "online":
                names.append(name)
        return names

    @staticmethod
    def _parse_container(raw: Dict[str, Any], node: str) -> ContainerInfo:
        return ContainerInfo(
            vmid=int(raw.get("vmid", 0)),
            name=raw.get("name", "") or raw.get("hostname", "") or f"ct-{raw.get('vmid', '?')}",
            status=str(raw.get("status", "unknown")),
            node=node,
            cpus=raw.get("cpus") or raw.get("maxcpu"),
            maxmem=raw.get("maxmem"),
            mem=raw.get("mem"),
            uptime=raw.get("uptime"),
        )

    @staticmethod
    def _parse_vm(raw: Dict[str, Any], node: str) -> VMInfo:
        return VMInfo(
            vmid=int(raw.get("vmid", 0)),
            name=raw.get("name", "") or f"vm-{raw.get('vmid', '?')}",
            status=str(raw.get("status", "unknown")),
            node=node,
            cpus=raw.get("cpus") or raw.get("maxcpu"),
            maxmem=raw.get("maxmem"),
            mem=raw.get("mem"),
            uptime=raw.get("uptime"),
        )

    # ------------------------------------------------------------------
    # Synchronous per-node fetchers (run inside threads)
    # ------------------------------------------------------------------

    def _fetch_containers_for_node(self, node: str) -> List[ContainerInfo]:
        """Fetch containers from a single node (synchronous)."""
        try:
            raw = self._proxmox.nodes(node).lxc.get()
            if not isinstance(raw, list):
                return []
            return [self._parse_container(item, node) for item in raw if isinstance(item, dict)]
        except Exception as exc:
            logger.warning("Failed to list containers on %s: %s", node, exc)
            return []

    def _fetch_vms_for_node(self, node: str) -> List[VMInfo]:
        """Fetch VMs from a single node (synchronous)."""
        try:
            raw = self._proxmox.nodes(node).qemu.get()
            if not isinstance(raw, list):
                return []
            return [self._parse_vm(item, node) for item in raw if isinstance(item, dict)]
        except Exception as exc:
            logger.warning("Failed to list VMs on %s: %s", node, exc)
            return []

    # ------------------------------------------------------------------
    # Async parallel methods
    # ------------------------------------------------------------------

    async def list_all_containers(self) -> List[ContainerInfo]:
        """List every container across all cluster nodes (parallel)."""
        nodes = self._get_node_names()
        if not nodes:
            return []

        loop = asyncio.get_running_loop()
        all_containers: List[ContainerInfo] = []

        with ThreadPoolExecutor(max_workers=min(self._max_workers, len(nodes))) as pool:
            futures = [
                loop.run_in_executor(pool, self._fetch_containers_for_node, node)
                for node in nodes
            ]
            results = await asyncio.gather(*futures, return_exceptions=True)

        for result in results:
            if isinstance(result, BaseException):
                logger.warning("Node query failed: %s", result)
                continue
            all_containers.extend(result)

        logger.info("Discovered %d containers across %d nodes", len(all_containers), len(nodes))
        return all_containers

    async def list_all_vms(self) -> List[VMInfo]:
        """List every VM across all cluster nodes (parallel)."""
        nodes = self._get_node_names()
        if not nodes:
            return []

        loop = asyncio.get_running_loop()
        all_vms: List[VMInfo] = []

        with ThreadPoolExecutor(max_workers=min(self._max_workers, len(nodes))) as pool:
            futures = [
                loop.run_in_executor(pool, self._fetch_vms_for_node, node)
                for node in nodes
            ]
            results = await asyncio.gather(*futures, return_exceptions=True)

        for result in results:
            if isinstance(result, BaseException):
                logger.warning("Node query failed: %s", result)
                continue
            all_vms.extend(result)

        logger.info("Discovered %d VMs across %d nodes", len(all_vms), len(nodes))
        return all_vms

    async def find_container(self, vmid: int) -> ContainerLocation:
        """Find which node hosts a specific container.

        Args:
            vmid: The container VMID to locate.

        Returns:
            ContainerLocation with node name, hostname, and container info.

        Raises:
            ValueError: If the container is not found on any node.
        """
        all_containers = await self.list_all_containers()

        for ct in all_containers:
            if ct.vmid == vmid:
                # Resolve hostname via the SSH host overrides or node name
                hostname = ct.node  # best we can do without SSH config
                return ContainerLocation(
                    node=ct.node,
                    hostname=hostname,
                    container=ct,
                )

        raise ValueError(f"Container {vmid} not found on any node")

    async def find_vm(self, vmid: int) -> VMLocation:
        """Find which node hosts a specific VM.

        Args:
            vmid: The VM VMID to locate.

        Returns:
            VMLocation with node name, hostname, and VM info.

        Raises:
            ValueError: If the VM is not found on any node.
        """
        all_vms = await self.list_all_vms()

        for vm in all_vms:
            if vm.vmid == vmid:
                hostname = vm.node
                return VMLocation(
                    node=vm.node,
                    hostname=hostname,
                    vm=vm,
                )

        raise ValueError(f"VM {vmid} not found on any node")

    # ------------------------------------------------------------------
    # Synchronous convenience wrappers
    # ------------------------------------------------------------------

    def list_all_containers_sync(self) -> List[ContainerInfo]:
        """Synchronous wrapper around :meth:`list_all_containers`."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None and loop.is_running():
            # We're inside an async context already -- use a thread
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, self.list_all_containers())
                return future.result()
        else:
            return asyncio.run(self.list_all_containers())

    def list_all_vms_sync(self) -> List[VMInfo]:
        """Synchronous wrapper around :meth:`list_all_vms`."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None and loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, self.list_all_vms())
                return future.result()
        else:
            return asyncio.run(self.list_all_vms())

    def find_container_sync(self, vmid: int) -> ContainerLocation:
        """Synchronous wrapper around :meth:`find_container`."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None and loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, self.find_container(vmid))
                return future.result()
        else:
            return asyncio.run(self.find_container(vmid))

    def find_vm_sync(self, vmid: int) -> VMLocation:
        """Synchronous wrapper around :meth:`find_vm`."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None and loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, self.find_vm(vmid))
                return future.result()
        else:
            return asyncio.run(self.find_vm(vmid))
