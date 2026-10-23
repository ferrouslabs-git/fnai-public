"""fnai_mcp — `fnai-mcp`, a local stdio bridge to the hosted functionalAI MCP server.

There is ONE MCP implementation: the hosted server (backend/app/mcp/server.py, at <base_url>/mcp). This
program exists only so a desktop AI tool that speaks stdio (Claude Code, Cursor, ...) can reach it with a
personal access token, exactly as before. It adds no tools of its own: it asks the hosted server for its
tool list and forwards every call, so the two can never drift apart again (FNA-REQ-412). Enforcement is
entirely server-side: the token is recorded as the `mcp` door, held to the same Enterprise elevation gate as
an OAuth connection, and a write without permission fails the same way it would from the web app.

You do not need this to use a client that speaks HTTP. Claude Code can connect to the hosted server directly:

    claude mcp add --transport http fnai https://uat.fnai.dev/mcp --header "Authorization: Bearer fnp_..."

Optional extra, kept out of the core zero-dependency `fnai` package:
    pip install "fnai[mcp] @ git+https://github.com/ferrouslabs-git/fnai-public.git#subdirectory=python"

Run it (reads the same FNAI_TOKEN / FNAI_BASE_URL env vars the rest of the SDK uses):
    export FNAI_TOKEN=fnp_...          # avatar menu -> API tokens -> Mint
    export FNAI_BASE_URL=https://uat.fnai.dev
    fnai-mcp
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

try:
    import anyio
    from mcp import types
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server
except ImportError as exc:  # pragma: no cover - exercised only without the extra installed
    raise ImportError(
        "fnai_mcp requires the optional 'mcp' dependency — install with: "
        'pip install "fnai[mcp] @ git+https://github.com/ferrouslabs-git/fnai-public.git#subdirectory=python"'
    ) from exc

from fnai import __version__

PROTOCOL_VERSION = "2025-03-26"
TIMEOUT_S = 120


class HostedMcpError(Exception):
    """The hosted server could not be reached, or refused the connection (bad token, wrong URL)."""


class HostedMcp:
    """A minimal client for the hosted server's Streamable HTTP endpoint: one session, re-opened if the
    server forgets it. Stdlib only, so the bridge adds no dependency beyond the MCP server library."""

    def __init__(self, base_url: str, token: str, timeout: float = TIMEOUT_S):
        self.url = base_url.rstrip("/") + "/mcp/"
        self.token = token
        self.timeout = timeout
        self.session_id: str | None = None
        self.instructions: str | None = None

    def _post(self, body: dict) -> tuple[Any, str | None]:
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream",
                   "User-Agent": f"fnai-mcp/{__version__}"}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        req = urllib.request.Request(self.url, method="POST", headers=headers, data=json.dumps(body).encode())
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode()
                sid = resp.headers.get("Mcp-Session-Id")
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:300].decode(errors="replace")
            raise HostedMcpError(f"{exc.code} from {self.url}: {detail}") from None
        except urllib.error.URLError as exc:
            raise HostedMcpError(f"could not reach {self.url}: {exc.reason}") from None
        for line in raw.splitlines():                      # a reply arrives as one server-sent event...
            if line.startswith("data:"):
                return json.loads(line[5:]), sid
        return (json.loads(raw) if raw.strip() else None), sid  # ...or as plain JSON

    def _open(self) -> None:
        self.session_id = None
        reply, sid = self._post({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "fnai-mcp", "version": __version__}}})
        self.session_id = sid
        self.instructions = ((reply or {}).get("result") or {}).get("instructions")
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def request(self, method: str, params: dict | None = None) -> dict:
        """One JSON-RPC request; opens the session first, and once more if the server dropped it."""
        for attempt in (0, 1):
            if self.session_id is None:
                self._open()
            body = {"jsonrpc": "2.0", "id": 1, "method": method, **({"params": params} if params is not None else {})}
            try:
                reply, _ = self._post(body)
            except HostedMcpError as exc:
                if attempt == 0 and str(exc).startswith(("400", "404")):   # session expired on the server
                    self.session_id = None
                    continue
                raise
            if reply is None or "error" in reply:
                raise HostedMcpError(f"{method}: {(reply or {}).get('error') or 'empty reply'}")
            return reply["result"]
        raise HostedMcpError(f"{method}: the hosted server kept refusing the session")


_remote: HostedMcp | None = None


def get_remote() -> HostedMcp:
    """Lazily build the shared connection, failing fast with an actionable message."""
    global _remote
    if _remote is None:
        token = os.environ.get("FNAI_TOKEN")
        if not token:
            raise RuntimeError("FNAI_TOKEN is not set. Mint one from your avatar menu -> API tokens, "
                               "then: export FNAI_TOKEN=fnp_...")
        _remote = HostedMcp(os.environ.get("FNAI_BASE_URL", "http://localhost:8000"), token)
    return _remote


async def list_tools(ctx: Any, params: Any = None) -> types.ListToolsResult:
    result = await asyncio.to_thread(get_remote().request, "tools/list")
    return types.ListToolsResult(tools=[types.Tool.model_validate(t) for t in result.get("tools", [])])


async def call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
    try:
        result = await asyncio.to_thread(get_remote().request, "tools/call",
                                         {"name": params.name, "arguments": params.arguments or {}})
    except HostedMcpError as exc:
        return types.CallToolResult(content=[types.TextContent(type="text", text=str(exc))], isError=True)
    return types.CallToolResult.model_validate(result)


def build_server(instructions: str | None = None) -> Server:
    return Server(
        name="fnai", version=__version__, on_list_tools=list_tools, on_call_tool=call_tool,
        instructions=instructions or "functionalAI, through its hosted MCP server. Every tool and rule is the "
                                     "hosted server's; this program only forwards.")


async def _serve() -> None:
    try:                                   # learn the hosted server's own instructions before the first request
        await asyncio.to_thread(get_remote()._open)
    except HostedMcpError as exc:
        print(f"fnai-mcp: {exc}", file=sys.stderr)
        sys.exit(1)
    server = build_server(get_remote().instructions)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main() -> None:
    try:
        get_remote()  # fail fast, before the stdio protocol loop starts
    except RuntimeError as exc:
        print(f"fnai-mcp: {exc}", file=sys.stderr)
        sys.exit(1)
    anyio.run(_serve)


if __name__ == "__main__":
    main()
