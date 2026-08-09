#!/usr/bin/env python
"""Done-means gate for the MCP 2026-07-28 additive migration (dev#111).

Every assertion is made against a REAL running server over HTTP, not against
an in-process convenience API. ``MCPServer.call_tool`` raises where the wire
returns ``isError``, so an in-process assertion would be testing a different
thing than a client sees.

The server under test is pointed at a stub Proxmox API
(``tests/stub_proxmox.py``); no real cluster is contacted and no write verb is
reachable.

Checks
------
1. handshake-free ``tools/call`` -- a single POST, no ``initialize``, no
   session id.
2. ``Mcp-Method`` / ``Mcp-Name`` headers are accepted, and a mismatched
   ``Mcp-Name`` is rejected.
3. strict schemas reject an undeclared parameter **by name**.
4. old-path control: the v1 module still imports its v1 SDK entry point, asserted
   BEFORE and AFTER the v2 checks, so a v2 change that breaks the v1 path fails
   this gate.

Exit code 0 = all green. Non-zero = the branch is not done.

Prove it can fail before trusting it:
    ``--mutate advertise-only``  drops enforcement, keeps the advertisement
    ``--mutate no-strict``       registers the permissive default
    ``--mutate break-old-path``  simulates the v1 entry point going missing
Each mutation MUST turn this script red. A gate that cannot fail is not a gate.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

from tests.stub_proxmox import StubProxmoxAPI  # noqa: E402

PROTOCOL_VERSION = "2026-07-28"

# Bind the LITERAL loopback address, never the name "localhost": on macOS the
# name resolves ::1 first, so the server would bind IPv6-only and every
# 127.0.0.1 client would get ECONNREFUSED that reads as "server down".
BIND_HOST = "127.0.0.1"

_failures: list[str] = []
_checks = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global _checks
    _checks += 1
    if ok:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}" + (f" -- {detail}" if detail else ""))
        _failures.append(label)


def free_port() -> int:
    """Ask the OS for a free port in the 7100-7199 policy band.

    Falls back to an ephemeral port only if the whole band is occupied, and
    says so, rather than silently leaving the band.
    """
    for port in range(7100, 7200):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((BIND_HOST, port))
                return port
            except OSError:
                continue
    raise RuntimeError("no free port in the 7100-7199 band")


def post(url: str, body: dict, headers: dict[str, str]) -> tuple[int, dict]:
    """POST JSON and return ``(status, parsed_body)``.

    HTTP error statuses are captured rather than raised: a 400 carrying a
    JSON-RPC error body is a legitimate, asserted outcome here.
    """
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    for key, value in headers.items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode()
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        status = exc.code

    # A streamable-HTTP server may answer with SSE framing; unwrap the payload.
    if raw.startswith("event:") or raw.startswith("data:"):
        for line in raw.splitlines():
            if line.startswith("data:"):
                raw = line[5:].strip()
                break
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, {"_raw": raw}


def call_tool_body(name: str, arguments: dict, req_id: int = 1) -> dict:
    """A 2026-07-28 ``tools/call`` -- note there is NO preceding handshake."""
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "tools/call",
        "params": {
            "name": name,
            "arguments": arguments,
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
                "io.modelcontextprotocol/clientInfo": {
                    "name": "done-means", "version": "1.0.0",
                },
                "io.modelcontextprotocol/clientCapabilities": {},
            },
        },
    }


def std_headers(method: str, name: str | None) -> dict[str, str]:
    headers = {
        "MCP-Protocol-Version": PROTOCOL_VERSION,
        "Mcp-Method": method,
    }
    if name is not None:
        headers["Mcp-Name"] = name
    return headers


def check_old_path(stage: str) -> None:
    """Control clause: the v1 server module must remain intact and v1-shaped.

    Asserted before AND after the v2 checks. The v1 path is what is actually
    serving production today; a v2 change that disturbs it is a failure of this
    branch even if every v2 assertion passes.
    """
    src = (REPO_ROOT / "src" / "proxmox_mcp" / "server.py").read_text()
    if os.getenv("DONE_MEANS_MUTATE") == "break-old-path":
        src = src.replace("from mcp.server.fastmcp import FastMCP", "")
    check(
        f"[{stage}] v1 server.py still uses the v1 FastMCP entry point",
        "from mcp.server.fastmcp import FastMCP" in src,
        "server.py no longer imports FastMCP -- the old path was modified",
    )
    check(
        f"[{stage}] v1 server.py untouched by the v2 module",
        "server_v2" not in src and "mcpserver" not in src,
        "server.py references v2 symbols -- migration was not additive",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mutate",
        choices=["advertise-only", "no-strict", "break-old-path"],
        help="deliberately break one guarantee to prove this gate can fail",
    )
    args = parser.parse_args()
    if args.mutate:
        os.environ["DONE_MEANS_MUTATE"] = args.mutate
        print(f"!! MUTATION ACTIVE: {args.mutate} -- this run MUST fail\n")

    print("== old-path control (before) ==")
    check_old_path("before")

    from proxmox_mcp.server_v2 import READ_ONLY_TOOLS, build_server
    from proxmox_mcp.strictness import enforce_strict_schemas

    mutation = os.getenv("DONE_MEANS_MUTATE")
    server = build_server(
        proxmox_api=StubProxmoxAPI(),
        strict=(mutation not in {"advertise-only", "no-strict"}),
    )
    if mutation == "advertise-only":
        # Advertise the contract but do NOT enforce it -- the exact defect
        # observed live on mcp 2.0.0.
        for tool in server._tool_manager._tools.values():
            tool.parameters = {**tool.parameters, "additionalProperties": False}

    port = free_port()
    url = f"http://{BIND_HOST}:{port}/mcp"

    thread = threading.Thread(
        target=lambda: server.run(
            transport="streamable-http", host=BIND_HOST, port=port
        ),
        daemon=True,
    )
    thread.start()

    # Readiness: probe with the operation actually used. A GET on the MCP
    # endpoint opens a long-lived SSE stream and would hang forever, calling a
    # healthy server unreachable.
    deadline = time.time() + 30
    ready = False
    while time.time() < deadline:
        try:
            status, _ = post(
                url, call_tool_body("get_nodes", {}), std_headers("tools/call", "get_nodes")
            )
            if status < 500:
                ready = True
                break
        except OSError:
            time.sleep(0.25)
    if not ready:
        print(f"  FAIL  server never became ready on {url}")
        return 1

    print(f"\n== v2 wire checks (server on {url}) ==")

    # 1. Handshake-free tools/call.
    status, body = post(
        url, call_tool_body("get_nodes", {}), std_headers("tools/call", "get_nodes")
    )
    result = body.get("result", {})
    check(
        "handshake-free tools/call succeeds with no initialize and no session id",
        status == 200 and not result.get("isError", result.get("is_error", False)),
        f"status={status} body={str(body)[:200]}",
    )
    check(
        "result carries resultType=complete",
        result.get("resultType") == "complete",
        f"resultType={result.get('resultType')!r}",
    )

    # 2. Mcp-Name mismatch must be rejected (header/body disagreement).
    status, body = post(
        url,
        call_tool_body("get_nodes", {}),
        std_headers("tools/call", "get_storage"),  # deliberately wrong
    )
    err = body.get("error", {})
    check(
        "mismatched Mcp-Name is rejected (HTTP 400 + JSON-RPC error)",
        status == 400 and err.get("code") == -32020,
        f"status={status} error={err}",
    )

    # 3. Strict schemas reject an undeclared parameter BY NAME.
    status, body = post(
        url,
        call_tool_body("get_node_status", {"node": "proxmox01", "BOGUS": "x"}),
        std_headers("tools/call", "get_node_status"),
    )
    payload = json.dumps(body)
    rejected = bool(body.get("error")) or body.get("result", {}).get(
        "isError", body.get("result", {}).get("is_error", False)
    )
    check(
        "undeclared parameter is rejected (not silently dropped)",
        rejected,
        f"server accepted the undeclared param: {payload[:220]}",
    )
    check(
        "rejection names the offending parameter 'BOGUS'",
        "BOGUS" in payload,
        f"rejection did not name the parameter: {payload[:220]}",
    )

    # A valid call must still work -- strictness must not break the flat shape.
    status, body = post(
        url,
        call_tool_body("get_node_status", {"node": "proxmox01"}),
        std_headers("tools/call", "get_node_status"),
    )
    result = body.get("result", {})
    check(
        "valid call still succeeds with flat arguments (no 'args' nesting)",
        status == 200 and not result.get("isError", result.get("is_error", False)),
        f"status={status} body={str(body)[:200]}",
    )

    # 4. Surface is read-only.
    check(
        "served tool surface is exactly the six read-only verbs",
        tuple(sorted(READ_ONLY_TOOLS)) == tuple(sorted(server._tool_manager._tools)),
        f"served={sorted(server._tool_manager._tools)}",
    )

    print("\n== old-path control (after) ==")
    check_old_path("after")

    print(f"\n{_checks - len(_failures)}/{_checks} checks passed")
    if _failures:
        print("FAILED:")
        for failure in _failures:
            print(f"  - {failure}")
        return 1
    print("ALL GREEN")
    return 0


if __name__ == "__main__":
    sys.exit(main())
