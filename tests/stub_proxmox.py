"""A fake Proxmox API for tests -- never touches a real cluster.

Why a stub and not a live connection
------------------------------------
The tools under test call a real Proxmox cluster's API. Pointing the test
suite at the live cluster would put cluster mutation one typo away from a test
run. This stub mimics ``proxmoxer.ProxmoxAPI``'s fluent chain
(``api.nodes("proxmox01").status.get()``) closely enough for the read-only
tool surface, and makes writes structurally impossible rather than merely
discouraged:

* ``.get()`` is the ONLY terminal verb implemented.
* ``.post()``, ``.put()``, ``.delete()`` and ``.create()`` raise
  :class:`ForbiddenWriteError` on contact.

So a test that accidentally reaches a write endpoint fails loudly with a
message naming the path, instead of silently succeeding against a real host.
"""

from __future__ import annotations

from typing import Any


class ForbiddenWriteError(AssertionError):
    """Raised when test code attempts a Proxmox write verb."""


#: Canned read-only fixtures keyed by the resolved API path.
_RESPONSES: dict[str, Any] = {
    "nodes": [
        {
            "node": "proxmox01",
            "status": "online",
            "uptime": 123456,
            "maxcpu": 8,
            "maxmem": 34359738368,
            "mem": 17179869184,
        },
        {
            "node": "proxmox02",
            "status": "online",
            "uptime": 654321,
            "maxcpu": 16,
            "maxmem": 68719476736,
            "mem": 21474836480,
        },
    ],
    "nodes/proxmox01/status": {
        "uptime": 123456,
        "cpu": 0.05,
        "cpuinfo": {"cpus": 8},
        "memory": {"used": 17179869184, "total": 34359738368},
    },
    "nodes/proxmox02/status": {
        "uptime": 654321,
        "cpu": 0.11,
        "cpuinfo": {"cpus": 16},
        "memory": {"used": 21474836480, "total": 68719476736},
    },
    "nodes/proxmox01/qemu": [
        {"vmid": "100", "name": "vm-one", "status": "running",
         "cpus": 4, "mem": 4294967296, "maxmem": 8589934592},
    ],
    "nodes/proxmox02/qemu": [],
    "nodes/proxmox01/lxc": [
        {"vmid": "215", "name": "proxmox-mcp", "status": "running",
         "cpus": 2, "mem": 536870912, "maxmem": 1073741824},
    ],
    "nodes/proxmox02/lxc": [],
    "storage": [
        {"storage": "local", "type": "dir", "content": "iso",
         "used": 1073741824, "total": 107374182400, "avail": 106300440576},
    ],
    "cluster/status": [
        {"type": "cluster", "name": "rodaddy", "quorate": 1, "nodes": 2},
        {"type": "node", "name": "proxmox01", "online": 1},
        {"type": "node", "name": "proxmox02", "online": 1},
    ],
}


class _Node:
    """One segment of the fluent path; resolves to a canned response."""

    def __init__(self, path: str) -> None:
        self._path = path

    def __getattr__(self, name: str) -> Any:
        if name in {"post", "put", "delete", "create"}:
            def _forbidden(*_args: Any, **_kwargs: Any) -> Any:
                raise ForbiddenWriteError(
                    f"test attempted Proxmox write {name!r} on {self._path!r}; "
                    "the stub is read-only by construction"
                )
            return _forbidden
        if name == "get":
            def _get(*_args: Any, **_kwargs: Any) -> Any:
                try:
                    return _RESPONSES[self._path]
                except KeyError:
                    raise KeyError(
                        f"stub has no fixture for path {self._path!r}; "
                        "add one to _RESPONSES"
                    ) from None
            return _get
        return _Node(f"{self._path}/{name}" if self._path else name)

    def __call__(self, segment: Any) -> "_Node":
        return _Node(f"{self._path}/{segment}" if self._path else str(segment))


class StubProxmoxAPI(_Node):
    """Drop-in stand-in for ``proxmoxer.ProxmoxAPI`` covering read-only use."""

    def __init__(self) -> None:
        super().__init__("")
