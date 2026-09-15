"""fnai_mcp — local MCP (stdio) server exposing functionalAI tools.

Lets an AI coding tool (Claude Code, Cursor, or any MCP client) inspect and
author in a functionalAI workspace on the developer's behalf: who they are,
what they're allowed to do, the functions/versions they can see, and (with
write permission) creating functions/drafts, running trials, and building
datasets and checks. Enforcement is entirely server-side — this is a thin
adapter over the same REST API the web app uses, same as the hosted MCP
server (backend/app/mcp/server.py); a write call with no write permission
fails the same way it would from the web app.

Experiment launch, polling, findings, and handoff are NOT exposed here yet —
the hosted server runs those non-blocking via an in-process background task,
which a separate local process talking to the backend only over REST can't
replicate without either a backend-side async job endpoint or a client-side
threading workaround. That's an open design question, not a parity gap to
paper over.

Optional extra, kept out of the core zero-dependency `fnai` package.
Not yet published to PyPI under any name — install from source:
    pip install "fnai[mcp] @ git+https://github.com/ferrouslabs-git/fnai-public.git#subdirectory=python"

Run it (reads the same FNAI_TOKEN / FNAI_BASE_URL env vars the rest of the
SDK uses):
    export FNAI_TOKEN=fnp_...          # avatar menu -> API tokens -> Mint
    export FNAI_BASE_URL=http://localhost:8100
    fnai-mcp
"""
from __future__ import annotations

import difflib
import functools
import json
import os
import sys

from fnai import Client, FnaiError

try:
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError as exc:  # pragma: no cover - exercised only without the extra installed
    raise ImportError(
        "fnai_mcp requires the optional 'mcp' dependency — install with: "
        'pip install "fnai[mcp] @ git+https://github.com/ferrouslabs-git/fnai-public.git#subdirectory=python"'
    ) from exc

MAX_LIST_ITEMS = 50

server = MCPServer(
    name="fnai",
    version="0.1.0",
    instructions=(
        "Access to one functionalAI workspace: identify the connected developer, "
        "list the spaces and functions they can see, inspect version history, "
        "and — with write permission — create spaces, functions and drafts, run "
        "trials, and build datasets and checks. Experiment launch/findings and "
        "handoff are not exposed by this local server; use the hosted MCP "
        "endpoint for those."
    ),
)

_client: Client | None = None


def get_client() -> Client:
    """Lazily build the shared Client, failing fast with an actionable message."""
    global _client
    if _client is None:
        token = os.environ.get("FNAI_TOKEN")
        if not token:
            raise RuntimeError(
                "FNAI_TOKEN is not set. Mint one from your avatar menu -> API tokens, "
                "then: export FNAI_TOKEN=fnp_..."
            )
        base_url = os.environ.get("FNAI_BASE_URL", "http://localhost:8000")
        _client = Client(token=token, base_url=base_url)
    return _client


def _actionable(fn):
    """Translate FnaiError (the real 401/403/404/... from the backend) into
    ToolError — the one exception type the MCP framework treats as
    "anticipated" and shows the actual message for. Anything else gets
    sanitized to a generic "Error executing tool X", which would violate
    the doc's "refusals are actionable, never a bare 403" requirement.
    Applied closest to the function, under @server.tool(), so the schema
    introspection still sees the real signature via functools.wraps."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except FnaiError as exc:
            raise ToolError(f"{exc.status}: {exc.detail}") from None
    return wrapper


def _summarize_function(fn: dict) -> dict:
    return {
        "id": fn.get("id"),
        "slug": fn.get("slug"),
        "name": fn.get("name"),
        "description": fn.get("description"),
        "archived": fn.get("archived", False),
    }


def _summarize_version(v: dict) -> dict:
    return {
        "id": v.get("id"),
        "number": v.get("number"),
        "status": v.get("status"),
        "message": v.get("message"),
        "created_at": v.get("created_at"),
    }


@server.tool(description="Identify this connection: user id, roles, resolved permissions, account and space.")
@_actionable
def who_am_i() -> dict:
    me = get_client().me()
    return {
        "user_id": me["user_id"],
        "roles": me["roles"],
        "permissions": me["permissions"],
        "account_id": me["account_id"],
        "space_id": me["space_id"],
    }


@server.tool(description="List every space this connection can reach in the current account.")
@_actionable
def list_spaces() -> list[dict]:
    return get_client().me()["spaces"]


@server.tool(description="Report the effective permissions for this connection (e.g. functions.read, functions.write).")
@_actionable
def get_my_permissions() -> list[str]:
    return get_client().me()["permissions"]


@server.tool(description="List functions in the current space (id, slug, name, description) — summarized, up to 50 at a time.")
@_actionable
def list_functions() -> list[dict]:
    fns = get_client().functions.list()
    return [_summarize_function(f) for f in fns[:MAX_LIST_ITEMS]]


@server.tool(description="Get one function's full detail by id.")
@_actionable
def get_function(function_id: int) -> dict:
    return get_client().functions.get(function_id)


@server.tool(description="List a function's versions (id, number, status, message) — summarized.")
@_actionable
def list_versions(function_id: int) -> list[dict]:
    versions = get_client().functions.versions(function_id)
    return [_summarize_version(v) for v in versions[:MAX_LIST_ITEMS]]


@server.tool(description="Get one version's full detail (snapshot, schemas) by id.")
@_actionable
def get_version(version_id: int) -> dict:
    return get_client().versions.get(version_id)


@server.tool(description="Line-based diff between two versions' snapshots.")
@_actionable
def diff_versions(version_id_a: int, version_id_b: int) -> str:
    client = get_client()
    a = client.versions.get(version_id_a)
    b = client.versions.get(version_id_b)
    a_lines = json.dumps(a.get("snapshot", {}), indent=2, sort_keys=True).splitlines(keepends=True)
    b_lines = json.dumps(b.get("snapshot", {}), indent=2, sort_keys=True).splitlines(keepends=True)
    diff = difflib.unified_diff(
        a_lines, b_lines,
        fromfile=f"version {version_id_a}", tofile=f"version {version_id_b}",
    )
    return "".join(diff) or "no differences"


@server.tool(name="create_space", description="Create a new space in the current account. Needs write access.")
@_actionable
def create_space(name: str) -> dict:
    return get_client().spaces.create(name=name)


@server.tool(name="create_function", description="Create a new function. Needs write access.")
@_actionable
def create_function(
    slug: str, name: str, description: str = "",
    input_schema: dict | None = None, output_schema: dict | None = None,
) -> dict:
    return get_client().functions.create(
        slug=slug, name=name, description=description,
        input_schema=input_schema or {}, output_schema=output_schema or {},
    )


@server.tool(name="create_draft", description="Create a new draft version of a function from a prompt snapshot. Needs write access.")
@_actionable
def create_draft(
    function_id: int, snapshot: dict, message: str = "",
    input_schema: dict | None = None, output_schema: dict | None = None, parent_id: int | None = None,
) -> dict:
    return get_client().functions.create_version(
        function_id, snapshot=snapshot, message=message,
        input_schema=input_schema, output_schema=output_schema, parent_id=parent_id,
    )


@server.tool(name="update_draft", description="Overwrite an existing draft's snapshot in place — fails once that version has been deployed (immutable). Needs write access.")
@_actionable
def update_draft(
    version_id: int, snapshot: dict, message: str = "",
    input_schema: dict | None = None, output_schema: dict | None = None,
) -> dict:
    return get_client().versions.update(
        version_id, snapshot=snapshot, message=message,
        input_schema=input_schema, output_schema=output_schema,
    )


@server.tool(name="run_trial", description="Run a draft against one input, returning its output, reasoning trace, and cost. Needs write access.")
@_actionable
def run_trial(version_id: int, input: dict, model: str = "mock-small", temperature: float = 0.2) -> dict:
    return get_client().versions.playground(version_id, input=input, model=model, temperature=temperature)


def _create_dataset_idempotent(client: Client, function_id: int, name: str) -> dict:
    """Mirrors the hosted server's _create_dataset_idempotent: a retried create
    with the same name returns the existing dataset instead of duplicating it.
    Same accepted TOCTOU race as the hosted version — see backend/app/mcp/server.py."""
    existing = client.datasets.list(function_id)
    match = next((d for d in existing if d.get("name") == name), None)
    if match is not None:
        return match
    return client.datasets.create(function_id, name=name)


@server.tool(name="create_dataset", description="Create a dataset for a function (or return the existing one if a dataset with this name already exists — safe to retry). Needs write access.")
@_actionable
def create_dataset(function_id: int, name: str) -> dict:
    return _create_dataset_idempotent(get_client(), function_id, name)


@server.tool(name="add_dataset_row", description="Append one row to a dataset. Needs write access.")
@_actionable
def add_dataset_row(
    dataset_id: int, input: dict, expected: dict | None = None,
    tags: list[str] | None = None, source: str = "manual",
) -> dict:
    return get_client().datasets.add_row(
        dataset_id, input=input, expected=expected, tags=tags or [], source=source,
    )


@server.tool(name="create_check", description="Create a scoring check (a named, reusable pass/fail rule) for a function. Needs write access.")
@_actionable
def create_check(function_id: int, name: str, config: dict, tier: str = "det") -> dict:
    return get_client().eval.scores.create(function_id, name=name, tier=tier, config=config)


def main() -> None:
    try:
        get_client()  # fail fast, before the stdio protocol loop starts
    except RuntimeError as exc:
        print(f"fnai-mcp: {exc}", file=sys.stderr)
        sys.exit(1)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
