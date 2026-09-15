"""fnai — Python client for functionalAI.

Zero dependencies (stdlib only). Two entry points:

`Function` — the public runtime surface, keyed by a function invoke key (fnk_*):
    from fnai import Function

    fn = Function("doc_summarizer", api_key="fnk_…",
                  base_url="http://localhost:8000")
    print(fn.contract()["input_schema"])          # discover the shape
    result = fn.run(document="…long text…")       # invoke
    if result.ok:
        print(result.output, "cost:", result.meta["cost_usd"])
    else:
        print("failed:", result.error)

`Client` — the management surface, keyed by a personal access token (fnp_*,
minted under your avatar → API tokens). Namespaces mirror the API families:
    from fnai import Client

    c = Client(token="fnp_…", base_url="http://localhost:8000")
    fn = c.functions.create(slug="summarizer", name="Summarizer",
                            input_schema=IN, output_schema=OUT)
    v = c.functions.create_version(fn["id"], snapshot=SNAPSHOT, message="v1")
    c.versions.playground(v["id"], input={"document": "…"}, model="mock-small")
    c.deployments.deploy(fn["id"], v["id"])   # deploy IS the gate — freezes the draft
    key = c.keys.invoke.mint(fn["id"], name="prod")     # → Function(api_key=key["key"])

    plan = c.eval.plans.create(fn["id"], name="baseline")
    exp = c.eval.experiments.create(fn["id"], version_ids=[v["id"]],
                                    models=[{"name": "mock-small"}])
    c.eval.experiments.run(exp["id"])

Errors: FnaiError carries .status and .detail for API-level failures
(bad key → 401, unknown/undeployed function → 404, invalid input → 422,
timeout → 504). An EXECUTION failure is not an exception — the platform
returns 200 with status="failed"; check result.ok.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

__version__ = "0.3.0"
DEFAULT_TIMEOUT_S = 90  # server caps execution at 60s; headroom for transport


class FnaiError(Exception):
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(f"HTTP {status}: {detail}")


def _slugify(name: str) -> str:
    """A function's stable id, derived from its `name` — lower_snake_case,
    matching the platform's slug convention (create_from_yaml's only source
    of a slug, since a v2 document names a function by `name`, not `slug`)."""
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return slug or "function"


class _Http:
    """Shared transport: bearer auth, JSON in/out, FnaiError on failure."""

    def __init__(self, base_url: str, token: str, timeout: float):
        self._base = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    def __call__(self, method: str, path: str, body: dict | None = None,
                 query: dict | None = None) -> Any:
        if query:
            qs = urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
            if qs:
                path = f"{path}?{qs}"
        req = urllib.request.Request(
            f"{self._base}{path}", method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {self._token}",
                     "Content-Type": "application/json",
                     "User-Agent": f"fnai-python/{__version__}"})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read()).get("detail", e.reason)
            except Exception:
                detail = str(e.reason)
            raise FnaiError(e.code, detail) from None
        except urllib.error.URLError as e:
            raise FnaiError(0, f"cannot reach {self._base}: {e.reason}") from None

    def text(self, method: str, path: str, *, data: bytes | None = None,
             content_type: str = "text/yaml") -> str:
        """Raw text in/out — for the YAML push/pull endpoints (REQ-44), which
        speak text/yaml rather than JSON. Same auth/error handling as
        __call__, just no (de)serialization either direction."""
        req = urllib.request.Request(
            f"{self._base}{path}", method=method, data=data,
            headers={"Authorization": f"Bearer {self._token}",
                     "Content-Type": content_type,
                     "User-Agent": f"fnai-python/{__version__}"})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read()).get("detail", e.reason)
            except Exception:
                detail = str(e.reason)
            raise FnaiError(e.code, detail) from None
        except urllib.error.URLError as e:
            raise FnaiError(0, f"cannot reach {self._base}: {e.reason}") from None


@dataclass
class RunResult:
    output: dict[str, Any] | None
    status: str                      # "success" | "failed"
    meta: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "success"

    @property
    def trace_id(self) -> str | None:
        return self.meta.get("trace_id")


class Function:
    """A handle on one deployed function (public runtime surface, fnk_* key)."""

    def __init__(self, slug: str, api_key: str, base_url: str = "http://localhost:8000",
                 timeout: float = DEFAULT_TIMEOUT_S):
        self.slug = slug
        self._http = _Http(base_url, api_key, timeout)

    def contract(self) -> dict:
        """The ACTIVE deployment's contract: input/output schemas + shapes.
        Changes atomically with deploys and rollbacks."""
        return self._http("GET", f"/v1/run/{self.slug}/contract")

    def run(self, **input: Any) -> RunResult:
        """Invoke the active deployment. Keyword arguments become the input
        object, validated server-side against the deployed input schema."""
        body = self._http("POST", f"/v1/run/{self.slug}", {"input": input})
        return RunResult(output=body.get("output"), status=body.get("status", "failed"),
                         meta=body.get("meta", {}), error=body.get("error"))


# ── management client (fnp_* personal access token) ──────────────────────────

class _Functions:
    def __init__(self, http: _Http):
        self._h = http

    def list(self) -> list[dict]:
        return self._h("GET", "/api/functions")

    def get(self, fn_id: int) -> dict:
        return self._h("GET", f"/api/functions/{fn_id}")

    def create(self, *, slug: str, name: str, input_schema: dict,
               output_schema: dict, **extra: Any) -> dict:
        return self._h("POST", "/api/functions",
                       {"slug": slug, "name": name, "input_schema": input_schema,
                        "output_schema": output_schema, **extra})

    def update(self, fn_id: int, **fields: Any) -> dict:
        return self._h("PATCH", f"/api/functions/{fn_id}", fields)

    def archive(self, fn_id: int) -> None:
        self._h("DELETE", f"/api/functions/{fn_id}")

    def versions(self, fn_id: int) -> list[dict]:
        return self._h("GET", f"/api/functions/{fn_id}/versions")

    def create_version(self, fn_id: int, *, snapshot: dict, message: str = "",
                       input_schema: dict | None = None, output_schema: dict | None = None,
                       parent_id: int | None = None) -> dict:
        return self._h("POST", f"/api/functions/{fn_id}/versions",
                       {"snapshot": snapshot, "message": message,
                        "input_schema": input_schema, "output_schema": output_schema,
                        "parent_id": parent_id})

    def health(self, fn_id: int, window_hours: int = 24) -> dict:
        return self._h("GET", f"/api/functions/{fn_id}/health",
                       query={"window_hours": window_hours})

    def types(self, fn_id: int, lang: str = "python") -> dict:
        """`fn types` — caller-side types for the function's deployed contract.
        Returns {language, function, version, source}; write `source` to a file."""
        return self._h("GET", f"/api/functions/{fn_id}/types", query={"lang": lang})

    # ── v2 DSL: push/pull a function as one YAML document (REQ-44/272/273) ──
    # Deliberately thin: this SDK does not parse or validate the DSL itself —
    # doing so would be exactly the "five separate guesses" problem PRD 09
    # exists to end. The file's bytes go to the backend, which owns the one
    # real parser (Definition.from_yaml) and the one real check
    # (validate_definition); whatever it says is authoritative.

    def create_from_yaml(self, path: str) -> tuple[dict, dict]:
        """Push a function up from a local YAML file — the whole "define a
        function" loop's first step (PRD 09's UX walkthrough). Returns
        (function, version). Raises FnaiError(409, ...) naming every
        diagnostic code if the file doesn't pass the backend's check, or if
        it's built on a stale version (meta.version — see REQ-268)."""
        import pathlib
        text = pathlib.Path(path).read_text(encoding="utf-8")
        return self.create_from_yaml_text(text)

    def create_from_yaml_text(self, text: str) -> tuple[dict, dict]:
        """Same as create_from_yaml(), for YAML already in memory (e.g. after
        editing what get_yaml() returned) rather than freshly read from disk."""
        import yaml  # PyYAML — same optional dependency parse_prompt_file uses
        doc = yaml.safe_load(text)
        if not isinstance(doc, dict) or not doc.get("name"):
            raise PromptFileError("the file must be a YAML mapping with at least a `name` field.")
        slug = _slugify(doc["name"])
        # no lookup-by-slug endpoint exists — resolve against the (small,
        # per-space) function list rather than adding a new backend route
        # for this SDK convenience alone.
        existing = next((f for f in self.list() if f["slug"] == slug), None)
        fn = existing or self._h("POST", "/api/functions", {"slug": slug, "name": doc["name"]})
        v = self._h.text("POST", f"/api/functions/{fn['id']}/versions/yaml",
                         data=text.encode("utf-8"), content_type="text/yaml")
        return fn, json.loads(v)

    def get_yaml(self, fn_id: int) -> str:
        """Pull a function down as YAML — its latest version, regenerated
        fresh from what the platform holds (REQ-264: regenerate, not
        preserve-verbatim). `meta.version` is stamped on the way out, so
        pushing it back unmodified round-trips cleanly (REQ-268)."""
        versions = self.versions(fn_id)
        if not versions:
            raise FnaiError(404, f"function {fn_id} has no versions yet")
        latest = max(versions, key=lambda v: v["number"])
        return self._h.text("GET", f"/api/versions/{latest['id']}/yaml")


class _Versions:
    def __init__(self, http: _Http):
        self._h = http

    def get(self, v_id: int) -> dict:
        return self._h("GET", f"/api/versions/{v_id}")

    def update(self, v_id: int, **fields: Any) -> dict:
        return self._h("PUT", f"/api/versions/{v_id}", fields)

    def artifact(self, v_id: int) -> dict:
        return self._h("GET", f"/api/versions/{v_id}/artifact")

    def playground(self, v_id: int, *, input: dict, model: str = "mock-small",
                   temperature: float | None = None) -> dict:
        body: dict[str, Any] = {"input": input, "model": model}
        if temperature is not None:
            body["temperature"] = temperature
        return self._h("POST", f"/api/versions/{v_id}/playground/run", body)

    def gate(self, v_id: int) -> dict:
        return self._h("POST", f"/api/versions/{v_id}/gate", {})

    def plan(self, v_id: int) -> dict:
        return self._h("GET", f"/api/versions/{v_id}/plan")


class _Plans:
    def __init__(self, http: _Http):
        self._h = http

    def list(self, fn_id: int) -> list[dict]:
        return self._h("GET", f"/api/functions/{fn_id}/plans")

    def create(self, fn_id: int, *, name: str) -> dict:
        return self._h("POST", f"/api/functions/{fn_id}/plans", {"name": name})

    def update(self, plan_id: int, **fields: Any) -> dict:
        return self._h("PATCH", f"/api/plans/{plan_id}", fields)

    def accept(self, plan_id: int) -> dict:
        return self._h("POST", f"/api/plans/{plan_id}/accept", {})

    def clone(self, plan_id: int, **body: Any) -> dict:
        return self._h("POST", f"/api/plans/{plan_id}/clone", body)

    def delete(self, plan_id: int) -> None:
        self._h("DELETE", f"/api/plans/{plan_id}")

    def scores(self, plan_id: int) -> list[dict]:
        return self._h("GET", f"/api/plans/{plan_id}/scores")

    def add_score(self, plan_id: int, **body: Any) -> dict:
        return self._h("POST", f"/api/plans/{plan_id}/scores", body)

    def remove_score(self, plan_id: int, score_id: int) -> None:
        self._h("DELETE", f"/api/plans/{plan_id}/scores/{score_id}")

    def catalogue(self) -> list[dict]:
        return self._h("GET", "/api/catalogue")


class _Experiments:
    def __init__(self, http: _Http):
        self._h = http

    def list(self, fn_id: int) -> list[dict]:
        return self._h("GET", f"/api/functions/{fn_id}/experiments")

    def get(self, exp_id: int) -> dict:
        return self._h("GET", f"/api/experiments/{exp_id}")

    def create(self, fn_id: int, **pins: Any) -> dict:
        return self._h("POST", f"/api/functions/{fn_id}/experiments", pins)

    def update(self, exp_id: int, **fields: Any) -> dict:
        return self._h("PATCH", f"/api/experiments/{exp_id}", fields)

    def run(self, exp_id: int) -> dict:
        return self._h("POST", f"/api/experiments/{exp_id}/run", {})

    def clone(self, exp_id: int) -> dict:
        return self._h("POST", f"/api/experiments/{exp_id}/clone", {})

    def delete(self, exp_id: int) -> None:
        self._h("DELETE", f"/api/experiments/{exp_id}")

    def findings(self, exp_id: int) -> list[dict]:
        return self._h("GET", f"/api/experiments/{exp_id}/findings")


class _Scores:
    def __init__(self, http: _Http):
        self._h = http

    def list(self, fn_id: int) -> list[dict]:
        return self._h("GET", f"/api/functions/{fn_id}/scores")

    def create(self, fn_id: int, **body: Any) -> dict:
        return self._h("POST", f"/api/functions/{fn_id}/scores", body)

    def results(self, fn_id: int, **filters: Any) -> list[dict]:
        return self._h("GET", f"/api/functions/{fn_id}/score-results", query=filters)

    def set_gate(self, score_id: int, gate: bool) -> dict:
        return self._h("POST", f"/api/scores/{score_id}/gate", {"gate": gate})


class _Eval:
    """eval.plans / eval.experiments / eval.scores — the evaluation loop."""

    def __init__(self, http: _Http):
        self.plans = _Plans(http)
        self.experiments = _Experiments(http)
        self.scores = _Scores(http)


class _Spaces:
    def __init__(self, http: _Http):
        self._h = http

    def create(self, *, name: str) -> dict:
        return self._h("POST", "/api/spaces", {"name": name})


class _Datasets:
    def __init__(self, http: _Http):
        self._h = http

    def list(self, fn_id: int, include_archived: bool = False) -> list[dict]:
        q = {"include_archived": "true"} if include_archived else None
        return self._h("GET", f"/api/functions/{fn_id}/datasets", query=q)

    def create(self, fn_id: int, *, name: str, rows: list[dict] | None = None) -> dict:
        return self._h("POST", f"/api/functions/{fn_id}/datasets",
                       {"name": name, "rows": rows or []})

    def update(self, ds_id: int, **fields: Any) -> dict:
        return self._h("PATCH", f"/api/datasets/{ds_id}", fields)

    def delete(self, ds_id: int) -> None:
        self._h("DELETE", f"/api/datasets/{ds_id}")

    def rows(self, ds_id: int) -> list[dict]:
        return self._h("GET", f"/api/datasets/{ds_id}/rows")

    def add_row(self, ds_id: int, **row: Any) -> dict:
        return self._h("POST", f"/api/datasets/{ds_id}/rows", row)

    def snapshot(self, ds_id: int) -> dict:
        return self._h("POST", f"/api/datasets/{ds_id}/snapshot", {})

    def snapshots(self, ds_id: int) -> list[dict]:
        return self._h("GET", f"/api/datasets/{ds_id}/snapshots")


class _Deployments:
    def __init__(self, http: _Http):
        self._h = http

    def deploy(self, fn_id: int, version_id: int) -> dict:
        return self._h("POST", f"/api/functions/{fn_id}/deploy",
                       {"version_id": version_id})

    def current(self, fn_id: int) -> dict:
        return self._h("GET", f"/api/functions/{fn_id}/deployment")

    def history(self, fn_id: int) -> list[dict]:
        return self._h("GET", f"/api/functions/{fn_id}/deployments")

    def rollback(self, fn_id: int, version_id: int | None = None) -> dict:
        body = {"version_id": version_id} if version_id is not None else {}
        return self._h("POST", f"/api/functions/{fn_id}/rollback", body)


class _ProviderKeys:
    """BYOK provider credentials (org/space scope) — model calls run on these."""

    def __init__(self, http: _Http):
        self._h = http

    def list(self) -> list[dict]:
        return self._h("GET", "/api/provider-keys")

    def add(self, *, provider: str, secret: dict, label: str = "", **extra: Any) -> dict:
        """Provider keys are organization-level. Restrict one to selected spaces
        with update(key_id, shared_space_ids=[...]); None means every space."""
        return self._h("POST", "/api/provider-keys",
                       {"provider": provider, "secret": secret, "label": label, **extra})

    def update(self, key_id: int, **fields: Any) -> dict:
        return self._h("PATCH", f"/api/provider-keys/{key_id}", fields)

    def test(self, key_id: int) -> dict:
        return self._h("POST", f"/api/provider-keys/{key_id}/test", {})

    def rotate(self, key_id: int, secret: dict) -> dict:
        return self._h("POST", f"/api/provider-keys/{key_id}/rotate", {"secret": secret})

    def delete(self, key_id: int) -> None:
        self._h("DELETE", f"/api/provider-keys/{key_id}")


class _InvokeKeys:
    """Per-function fnk_* keys for the public runtime — plaintext returned once."""

    def __init__(self, http: _Http):
        self._h = http

    def list(self, fn_id: int) -> list[dict]:
        return self._h("GET", f"/api/functions/{fn_id}/invoke-keys")

    def mint(self, fn_id: int, name: str = "") -> dict:
        return self._h("POST", f"/api/functions/{fn_id}/invoke-keys", {"name": name})

    def revoke(self, key_id: int) -> None:
        self._h("DELETE", f"/api/invoke-keys/{key_id}")


class _Keys:
    """keys.provider (BYOK model credentials) / keys.invoke (runtime fnk_* keys)."""

    def __init__(self, http: _Http):
        self.provider = _ProviderKeys(http)
        self.invoke = _InvokeKeys(http)


class _Traces:
    def __init__(self, http: _Http):
        self._h = http

    def list(self, function_id: int | None = None, source: str | None = None,
             limit: int = 50) -> list[dict]:
        return self._h("GET", "/api/traces",
                       query={"function_id": function_id, "source": source, "limit": limit})

    def get(self, trace_id: str) -> dict:
        return self._h("GET", f"/api/traces/{trace_id}")

    def flag(self, trace_id: str) -> dict:
        """The flywheel: production trace → regression dataset row."""
        return self._h("POST", f"/api/traces/{trace_id}/flag", {})


class _AuthTokens:
    def __init__(self, http: _Http):
        self._h = http

    def list(self) -> list[dict]:
        return self._h("GET", "/api/auth/tokens")

    def mint(self, name: str) -> dict:
        """Plaintext token is in THIS response only."""
        return self._h("POST", "/api/auth/tokens", {"name": name})

    def revoke(self, token_id: str) -> None:
        self._h("DELETE", f"/api/auth/tokens/{token_id}")


class _Auth:
    def __init__(self, http: _Http):
        self.tokens = _AuthTokens(http)


class Client:
    """Namespaced management client. Auth: personal access token (fnp_*)."""

    def __init__(self, token: str, base_url: str = "http://localhost:8000",
                 timeout: float = DEFAULT_TIMEOUT_S):
        http = _Http(base_url, token, timeout)
        self._http = http
        self.functions = _Functions(http)
        self.spaces = _Spaces(http)
        self.versions = _Versions(http)
        self.eval = _Eval(http)
        self.datasets = _Datasets(http)
        self.deployments = _Deployments(http)
        self.keys = _Keys(http)
        self.traces = _Traces(http)
        self.auth = _Auth(http)

    def me(self) -> dict:
        return self._http("GET", "/api/me")

    def raw(self, method: str, path: str, body: dict | None = None,
           query: dict | None = None) -> Any:
        """Call any management-API path directly — same authenticated
        transport and error semantics as every typed method above. For the
        handful of endpoints this hand-maintained surface doesn't (yet) have
        a typed method for. Prefer a typed method when one exists."""
        return self._http(method, path, body, query)

    def function(self, slug: str, api_key: str) -> Function:
        """Cross over to the runtime surface: a Function handle on this base URL."""
        return Function(slug, api_key=api_key, base_url=self._http._base,
                        timeout=self._http._timeout)


# ── the v2 DSL starter file (REQ-276) — a ready-made, valid example, so the
# first thing a new user edits is a real function, not a blank page. ──

STARTER_YAML = """\
name: Summarize Support Ticket
description: Produces a one-paragraph summary and urgency score for a support ticket.

input:
  ticket_text: { type: string, required: true }

output:
  summary: { type: string, required: true }
  urgency: { type: integer, required: true, minimum: 1, maximum: 5 }

implementation:
  execution:
    mode: single_shot
  flow:
    - "Summarize the ticket in one paragraph, plain language."
    - "Score urgency from the ticket's severity."
  sections:
    role: You are a support-ticket triage assistant.
"""


def write_starter_yaml(path: str = "function.yaml") -> str:
    """A ready-made, structurally valid starting example (REQ-276) — zero
    setup, nothing to look up first. Writes it to `path` (default
    function.yaml in the current directory, never overwriting an existing
    file by accident) and returns the path actually written.

    >>> path = write_starter_yaml()
    >>> fn, version = client.functions.create_from_yaml(path)   # works as-is
    """
    import pathlib
    p = pathlib.Path(path)
    if p.exists():
        raise FileExistsError(f"{path} already exists — pick a different path, or edit it directly")
    p.write_text(STARTER_YAML, encoding="utf-8")
    return str(p)


# ── legacy prompt-file import (pre-v2) — same .yaml/.yml/.json shape the
# canvas UI's Import button ALSO still reads for a legacy (non-v2) draft
# (frontend/src/pages/canvas/yamlImport.ts). Superseded by create_from_yaml()
# above for anything new — kept working, unchanged, for existing callers, the
# same "leave the legacy path alone" choice the backend and canvas both made
# (see REQ-351's note). Deliberately two separate steps: parse_prompt_file()
# only reads a file and validates it — no network call, nothing created — so
# the caller can inspect or edit the result before create_function_from_doc()
# is the one that actually talks to the API. ──

class PromptFileError(Exception):
    """A .yaml/.yml/.json prompt file is missing something required or malformed.
    Raised by parse_prompt_file() before any network call is made."""


_ITEM_KEYS = {"definitions", "instructions", "examples",
             "negative_examples", "edge_cases", "non_goals"}
_LABEL = {"definitions": "Definitions", "instructions": "Instructions", "examples": "Examples",
         "negative_examples": "Negative examples", "edge_cases": "Edge cases", "non_goals": "Non-goals"}
_KNOWN_KEYS = {"role", "freeform", "inputs", "outputs"} | _ITEM_KEYS
_FIELD_TYPES = {"string", "number", "integer", "boolean", "array", "object"}


def _parse_field_map(raw: Any, label: str) -> dict:
    if not isinstance(raw, dict):
        raise PromptFileError(f"{label} must be a mapping of field name to type.")
    properties: dict = {}
    required: list[str] = []
    for name, spec in raw.items():
        description, is_required = None, True
        if isinstance(spec, str):
            type_ = spec
        elif isinstance(spec, dict):
            type_ = spec.get("type")
            description = spec.get("description")
            if "required" in spec:
                is_required = bool(spec["required"])
        else:
            raise PromptFileError(f"{label}.{name} must be a type name or {{type, description, required}}.")
        if type_ not in _FIELD_TYPES:
            raise PromptFileError(f"{label}.{name}: type must be one of {', '.join(sorted(_FIELD_TYPES))}.")
        if description is not None and not isinstance(description, str):
            raise PromptFileError(f"{label}.{name}.description must be text.")
        properties[name] = {"type": type_, **({"description": description} if description else {})}
        if is_required:
            required.append(name)
    return {"type": "object", "required": required, "properties": properties}


def parse_prompt_file(path: str) -> dict:
    """Parse a canvas-format prompt file into
    {"sections": [...], "input_schema": {...}?, "output_schema": {...}?}.

    Read-and-validate only — makes no network calls and creates nothing.
    Raises PromptFileError naming exactly what's missing/wrong if the file
    doesn't satisfy the format (role is required; inputs:/outputs:, if
    present, must be a field-name -> type mapping; unknown top-level keys
    are rejected rather than silently dropped)."""
    import pathlib
    p = pathlib.Path(path)
    if not p.is_file():
        raise PromptFileError(f"file not found: {path}")
    text = p.read_text()

    if p.suffix.lower() == ".json":
        try:
            doc = json.loads(text)
        except json.JSONDecodeError as e:
            raise PromptFileError(f"not valid JSON — {e}") from e
    else:
        try:
            import yaml  # PyYAML — optional; only imported for .yaml/.yml files
        except ImportError as e:
            raise PromptFileError(
                "parsing a .yaml file needs PyYAML (`pip install pyyaml`) — "
                "or save the file as .json instead, which needs no extra package") from e
        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError as e:
            raise PromptFileError(f"not valid YAML — {e}") from e

    if not isinstance(doc, dict):
        raise PromptFileError("the file must be an object/mapping (key: value pairs) "
                              "at the top level, not a list or plain text.")

    unknown = [k for k in doc if k not in _KNOWN_KEYS]
    if unknown:
        raise PromptFileError(
            f"unknown section{'s' if len(unknown) > 1 else ''}: {', '.join(unknown)}. "
            f"expected: role, {', '.join(sorted(_ITEM_KEYS))}, freeform, inputs, outputs.")

    role = doc.get("role")
    if not isinstance(role, str) or not role.strip():
        raise PromptFileError("role is required and must be non-empty text.")

    sections: list[dict] = [{"type": "role", "content": role}]

    for key in _ITEM_KEYS:
        raw = doc.get(key)
        if raw is None:
            continue
        if not isinstance(raw, list) or any(not isinstance(x, str) or not x.strip() for x in raw):
            raise PromptFileError(f"{key} must be a list of non-empty text lines.")
        if raw:
            sections.append({"type": key, "title": _LABEL[key], "items": raw})

    raw_free = doc.get("freeform")
    if raw_free is not None:
        if not isinstance(raw_free, list):
            raise PromptFileError("freeform must be a list of {title, content} blocks.")
        for block in raw_free:
            if (not isinstance(block, dict) or not isinstance(block.get("content"), str)
                    or not block["content"].strip()):
                raise PromptFileError("each freeform block needs a non-empty content field (title is optional).")
            title = block.get("title")
            sections.append({"type": "freeform",
                             "title": title if isinstance(title, str) and title.strip() else "Freeform",
                             "content": block["content"]})

    result: dict = {"sections": sections}
    if "inputs" in doc:
        result["input_schema"] = _parse_field_map(doc["inputs"], "inputs")
    if "outputs" in doc:
        result["output_schema"] = _parse_field_map(doc["outputs"], "outputs")
    return result


def _create_function_and_version(client: Client, *, slug: str, name: str, sections: list,
                                 input_schema: dict | None, output_schema: dict | None,
                                 model: str, temperature: float, extra: dict) -> tuple[dict, dict]:
    fn = client.functions.create(
        slug=slug, name=name,
        input_schema=input_schema or {"type": "object", "properties": {}},
        output_schema=output_schema or {"type": "object", "properties": {}}, **extra)
    v = client.functions.create_version(fn["id"], snapshot={
        "impl": "prompt", "tools": [], "sections": sections,
        "model_binding": {"model": model, "temperature": temperature},
    })
    return fn, v


def create_function_from_doc(client: Client, *, slug: str, name: str, doc: dict,
                             model: str = "mock-small", temperature: float = 0.2,
                             **extra: Any) -> tuple[dict, dict]:
    """Create a function + its first version from data already parsed by
    parse_prompt_file(). This is the step that makes network calls — it never
    reads a file itself, so callers can inspect or edit `doc` in between the
    two. `doc` supplies the prompt's sections and, if the file declared
    inputs:/outputs:, their schemas (an empty object schema is used for
    whichever one the file omitted). `slug`/`name` are yours to choose, not
    derived from the file. Returns (function, version).

    For creating a function from sections/schemas you already have in code —
    not loaded from a file — use create_function() instead."""
    return _create_function_and_version(
        client, slug=slug, name=name, sections=doc["sections"],
        input_schema=doc.get("input_schema"), output_schema=doc.get("output_schema"),
        model=model, temperature=temperature, extra=extra)


def create_function(client: Client, *, slug: str, name: str, sections: list,
                    input_schema: dict | None = None, output_schema: dict | None = None,
                    model: str = "mock-small", temperature: float = 0.2,
                    **extra: Any) -> tuple[dict, dict]:
    """Create a function + its first version from sections/schemas you supply
    directly — the manual counterpart to create_function_from_doc(), for when
    you aren't loading a prompt from a file. input_schema/output_schema each
    default to an empty object schema if omitted. Returns (function, version)."""
    return _create_function_and_version(
        client, slug=slug, name=name, sections=sections,
        input_schema=input_schema, output_schema=output_schema,
        model=model, temperature=temperature, extra=extra)
