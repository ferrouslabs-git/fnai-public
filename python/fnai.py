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

import copy
import datetime
import json
import time
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
import os
import pathlib
import warnings
from dataclasses import dataclass, field
from typing import Any, Optional

__version__ = "0.8.0"
DEFAULT_TIMEOUT_S = 90  # server caps execution at 60s; headroom for transport


class FnaiError(Exception):
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(f"HTTP {status}: {detail}")


class SpaceNotFoundError(Exception):
    """Raised by Client.use_space(name) when no space with that name exists
    for this account. Checked fresh against GET /api/me every call — never
    a cached guess — so a stale or mistyped name can never silently bind."""


def _slugify(name: str) -> str:
    """A function's stable id, derived from its `name` — lower_snake_case,
    matching the platform's slug convention (create_from_yaml's only source
    of a slug, since a v2 document names a function by `name`, not `slug`)."""
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return slug or "function"


def _without_meta(doc: dict) -> dict:
    """Same document, minus `meta` — the one block that's SUPPOSED to
    differ between two pulls of the same unchanged content (the platform
    re-stamps meta.version fresh every time — REQ-264). Used to tell a
    genuine edit apart from a no-op re-import."""
    return {k: v for k, v in doc.items() if k != "meta"}


# ── shared input validation — every method below that takes a required
# field, an id, or a body validates it with one of these BEFORE making a
# request, so a caller gets a clear, immediate ValueError instead of a
# confusing 404/422 (or, worse, a silently-wrong request) from the backend. ──

def _require(value: Any, field: str) -> None:
    """`value` must be present and non-blank — for required strings/dicts."""
    if value is None or value == "" or (isinstance(value, str) and not value.strip()):
        raise ValueError(f"{field} is required and cannot be empty.")


def _require_dict(value: Any, field: str, *, allow_empty: bool = False) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be a dict, got {type(value).__name__}.")
    if not allow_empty and not value:
        raise ValueError(f"{field} cannot be empty.")


def _require_id(value: Any, field: str) -> None:
    """Every {fn,version,dataset,score,plan,experiment,key}_id argument goes
    through this — an id is a positive int, never a bool (bool is a subclass
    of int in Python, so `True`/`False` would otherwise sneak past an
    `isinstance(x, int)` check), never a string someone forgot to convert."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer id, got {value!r}.")


class _Http:
    """Shared transport: bearer auth, JSON in/out, FnaiError on failure."""

    def __init__(self, base_url: str, token: str, timeout: float):
        self._base = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        # Name-mangled (__space -> _Http__space), exposed only via the
        # read-only `space` property below — Client.use_space() is the only
        # way to change it, and it always validates first (see use_space()).
        # Opt-in: a caller who never touches use_space() never sends an
        # X-Space header at all, which is exactly today's behavior — the
        # backend resolves its own default space, same as before this
        # feature existed. Only a caller who explicitly targets a space
        # gets that behavior.
        self.__space: str | None = None

    @property
    def space(self) -> str | None:
        """Read-only — see Client.current_space. There is deliberately no
        setter; the only way to CHANGE it is use_space(), which validates
        the name first."""
        return self.__space

    def use_space(self, name: str | None) -> None:
        """Client.use_space()'s actual implementation. Validated against the
        account's REAL spaces on every call (a fresh GET /api/me, not a
        cached list a caller could have gone stale) — so this can never bind
        to a space that doesn't exist or that this token can't see."""
        if name is None:
            self.__space = None
            return
        me = self("GET", "/api/me")
        names = {sp["name"] for sp in me["spaces"]}
        if name not in names:
            raise SpaceNotFoundError(
                f"no space named {name!r} for this account — "
                f"choices: {sorted(names) or '(none yet — create one first)'}")
        self.__space = name

    def _headers(self, content_type: str) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._token}",
                  "Content-Type": content_type,
                  "User-Agent": f"fnai-python/{__version__}"}
        if self.__space:
            headers["X-Space"] = self.__space
        return headers

    def __call__(self, method: str, path: str, body: dict | None = None,
                 query: dict | None = None) -> Any:
        if query:
            qs = urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
            if qs:
                path = f"{path}?{qs}"
        req = urllib.request.Request(
            f"{self._base}{path}", method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers=self._headers("application/json"))
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

    def upload(self, path: str, *, filename: str, content: bytes,
               fields: dict[str, str]) -> Any:
        """multipart/form-data POST with one `file` part plus text fields —
        for the dataset upload endpoint. Same auth/error handling as
        __call__."""
        boundary = uuid.uuid4().hex
        parts: list[bytes] = []
        for k, v in fields.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        safe = filename.replace('"', "")
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{safe}"\r\n'
                     f'Content-Type: application/octet-stream\r\n\r\n'.encode() + content + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        req = urllib.request.Request(
            f"{self._base}{path}", method="POST", data=b"".join(parts),
            headers=self._headers(f"multipart/form-data; boundary={boundary}"))
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
            headers=self._headers(content_type))
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
    status: str                      # "success" | "partial" | "failed" | "escalated"
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

    def check(self, **input: Any) -> list[str]:
        """Check an input against the deployed contract WITHOUT running the
        function (no model call, no cost). Returns the problems, each naming
        the field, e.g. "field 'rating' must be at most 5, got 9"; [] means
        it would pass the input check. The server runs the same check on
        every real call and answers 422 with the same messages."""
        return _validate_sample(self.contract().get("input_schema") or {}, input)

    def run(self, **input: Any) -> RunResult:
        """Invoke the active deployment. Keyword arguments become the input
        object, validated server-side against the deployed input schema."""
        body = self._http("POST", f"/v1/run/{self.slug}", {"input": input})
        return RunResult(output=body.get("output"), status=body.get("status", "failed"),
                         meta=body.get("meta", {}), error=body.get("error"))

    def report_trace(self, trace: dict[str, Any]) -> dict[str, Any]:
        """Report one call that ran on YOUR servers, so it shows in the monitor next to hosted calls.
        You give what only you know: function_id, version_id, artifact_hash, input, output, success, error,
        steps, tokens_in, tokens_out, cost and latency_ms. `trace_id`, `schema_version` (1), `source`
        ("self_hosted") and `created_at` (now) are filled in when you leave them out; set created_at yourself
        when you report a call later than it happened. Hidden (sensitive) fields are hidden on our side.
        Returns {"trace_id": ..., "duplicate": bool}. Reporting the same trace_id again is harmless, so retry
        freely. The server answers 422 with the reason for a trace it refuses."""
        body = {"trace_id": uuid.uuid4().hex, "schema_version": 1, "source": "self_hosted",
                "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), **trace}
        return self._http("POST", "/v1/traces", body)

    # ── background runs: for a function that takes longer than one web request (about 60 s) ──

    def run_async(self, **input: Any) -> int:
        """Start a run in the background and return its run id at once. The input is checked first,
        exactly as in run(), so a bad input fails here, not later. Then call wait(run_id) or get_run(run_id)."""
        return self._http("POST", f"/v1/run/{self.slug}/async", {"input": input})["run_id"]

    def get_run(self, run_id: int) -> RunResult:
        """The state of a background run. While it works, status is "running" (output is None);
        when done it is the same result run() would have returned (success / partial / failed / escalated)."""
        _require_id(run_id, "run_id")
        body = self._http("GET", f"/v1/run/{self.slug}/runs/{run_id}")
        return RunResult(output=body.get("output"), status=body.get("status", "failed"),
                         meta={**(body.get("meta") or {}), "run_id": body.get("run_id", run_id)},
                         error=body.get("error"))

    def wait(self, run_id: int, *, timeout: float = 600.0, poll_every: float = 2.0) -> RunResult:
        """Check a background run until it finishes. Raises TimeoutError if it is still running after `timeout` s."""
        deadline = time.monotonic() + timeout
        while True:
            result = self.get_run(run_id)
            if result.status != "running":
                return result
            if time.monotonic() >= deadline:
                raise TimeoutError(f"run {run_id} is still running after {timeout:.0f}s; "
                                   f"keep checking with get_run({run_id})")
            time.sleep(poll_every)


# ── management client (fnp_* personal access token) ──────────────────────────

def _resolve_function_id(http: "_Http", ref: Any) -> int:
    """A function's id from an id (returned as is) or its slug, e.g. "my_function". A slug is looked
    up among this space's functions; an unknown one says so and lists the real ones."""
    if isinstance(ref, str):
        import difflib
        _require(ref, "slug")
        functions = http("GET", "/api/functions") or []
        found = next((f for f in functions if f.get("slug") == ref), None)
        if found is not None:
            return found["id"]
        slugs = sorted(f["slug"] for f in functions if f.get("slug"))
        near = difflib.get_close_matches(ref, slugs, n=1)
        hint = f" — did you mean {near[0]!r}?" if near else ""
        if ref.isdigit():
            hint += f" (to look up by id, pass the number {ref}, not the text)"
        listing = ", ".join(slugs[:20]) + (" …" if len(slugs) > 20 else "") if slugs else "no functions in this space yet"
        raise FnaiError(404, f"no function with slug {ref!r} in this space{hint} (functions here: {listing})")
    _require_id(ref, "fn_id")
    return ref


class _Functions:
    def __init__(self, http: _Http, drafts_folder: Any = None):
        self._h = http
        self._drafts_folder = drafts_folder

    def list(self) -> list[dict]:
        return self._h("GET", "/api/functions")

    def get(self, fn: "int | str") -> dict:
        """The function, by id or by slug (`c.functions.get("my_function")`), as a dict; print() it to
        see its YAML and what is missing. A slug is looked up in the space you are in."""
        fn_id = _resolve_function_id(self._h, fn)
        rec = FunctionRecord(self._h("GET", f"/api/functions/{fn_id}"))
        versions = self._h("GET", f"/api/functions/{fn_id}/versions") or []
        if versions:
            rec._version = max(versions, key=lambda v: v["number"])
        return rec

    def new(self, name: str, *, slug: str | None = None, description: str = "",
            save_to: Any = None) -> "FunctionDraft":
        """Start a function and build it one part at a time:
        add_input / add_prompt / add_output, then submit(). Nothing is created
        on the server until submit(); fn.id is None until then.

        While you build, a draft file (YAML) on your computer is kept up to
        date after every change, so work survives a stopped script. By default
        it is .fnai/drafts/<slug>.yaml next to where the script runs. Choose
        another place with save_to="path/file.yaml" (or a folder), or set
        drafts_folder on the Client. save_to=False turns it off. submit()
        turns the draft file into that version's copy (.fnai/versions/<slug>/v<N>.yaml); load a draft
        back with c.load_yaml(name_or_path)."""
        return FunctionDraft.create(self._h, name=name, slug=slug, description=description,
                                    save_to=save_to, drafts_folder=self._drafts_folder)

    def create(self, *, slug: str, name: str, input_schema: dict,
               output_schema: dict, **extra: Any) -> dict:
        _require(slug, "slug")
        _require(name, "name")
        _require_dict(input_schema, "input_schema", allow_empty=True)
        _require_dict(output_schema, "output_schema", allow_empty=True)
        existing = next((f for f in self.list() if f["slug"] == slug), None)
        if existing is not None:
            raise ValueError(f"a function with slug {slug!r} already exists "
                             f"(id={existing['id']}) — pick a different slug, "
                             f"or use that function's id instead of creating another.")
        return FunctionRecord(self._h("POST", "/api/functions",
                       {"slug": slug, "name": name, "input_schema": input_schema,
                        "output_schema": output_schema, **extra}))

    def update(self, fn_id: int, **fields: Any) -> dict:
        _require_id(fn_id, "fn_id")
        if not fields:
            raise ValueError("update() needs at least one field to change.")
        return self._h("PATCH", f"/api/functions/{fn_id}", fields)

    def archive(self, fn_id: int) -> None:
        _require_id(fn_id, "fn_id")
        self._h("DELETE", f"/api/functions/{fn_id}")

    def versions(self, fn_id: int) -> list[dict]:
        _require_id(fn_id, "fn_id")
        return self._h("GET", f"/api/functions/{fn_id}/versions")

    def create_version(self, fn_id: int, *, snapshot: dict, message: str = "",
                       input_schema: dict | None = None, output_schema: dict | None = None,
                       parent_id: int | None = None) -> dict:
        _require_id(fn_id, "fn_id")
        _require_dict(snapshot, "snapshot")
        if parent_id is not None:
            _require_id(parent_id, "parent_id")
        return self._h("POST", f"/api/functions/{fn_id}/versions",
                       {"snapshot": snapshot, "message": message,
                        "input_schema": input_schema, "output_schema": output_schema,
                        "parent_id": parent_id})

    def health(self, fn_id: int, window_hours: int = 24) -> dict:
        _require_id(fn_id, "fn_id")
        return self._h("GET", f"/api/functions/{fn_id}/health",
                       query={"window_hours": window_hours})

    def types(self, fn_id: int, lang: str = "python") -> dict:
        """`fn types` — caller-side types for the function's deployed contract.
        Returns {language, function, version, source}; write `source` to a file."""
        _require_id(fn_id, "fn_id")
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
        editing what get_yaml() returned) rather than freshly read from disk.

        A push always mints a new version on the backend — versions are
        immutable history, never overwritten in place (REQ-268's stale-push
        guard exists precisely because of that). What this method adds on
        top: if the function already has a version and the document you're
        pushing is IDENTICAL to it (ignoring `meta`, which the platform
        re-stamps fresh on every pull — see get_yaml()), it returns that
        existing version instead of minting a no-op duplicate. Re-importing
        an unedited file is then a no-op; a genuine edit still creates a new
        version exactly as before."""
        import yaml  # PyYAML — same optional dependency parse_prompt_file uses
        doc = yaml.safe_load(text)
        if not isinstance(doc, dict) or not doc.get("name"):
            raise PromptFileError("the file must be a YAML mapping with at least a `name` field.")
        slug = _slugify(doc["name"])
        # no lookup-by-slug endpoint exists — resolve against the (small,
        # per-space) function list rather than adding a new backend route
        # for this SDK convenience alone.
        existing = next((f for f in self.list() if f["slug"] == slug), None)
        if existing is not None:
            versions = self.versions(existing["id"])
            if versions:
                latest = max(versions, key=lambda v: v["number"])
                current_text = self._h.text("GET", f"/api/versions/{latest['id']}/yaml")
                current_doc = yaml.safe_load(current_text)
                if _without_meta(doc) == _without_meta(current_doc):
                    return existing, latest
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
        _require_id(v_id, "v_id")
        return self._h("GET", f"/api/versions/{v_id}")

    def update(self, v_id: int, **fields: Any) -> dict:
        _require_id(v_id, "v_id")
        if not fields:
            raise ValueError("update() needs at least one field to change.")
        return self._h("PUT", f"/api/versions/{v_id}", fields)

    def patch(self, v_id: int, patch: dict, *, message: str = "") -> dict:
        """REQ-275: change one field of a draft without resending the whole
        snapshot. `patch` follows JSON Merge Patch (RFC 7396) — a dict key
        merges recursively, `None` deletes the key, anything else (including
        a whole list) replaces wholesale."""
        _require_id(v_id, "v_id")
        _require_dict(patch, "patch")
        return self._h("PATCH", f"/api/versions/{v_id}", {"patch": patch, "message": message})

    def artifact(self, v_id: int) -> dict:
        _require_id(v_id, "v_id")
        return self._h("GET", f"/api/versions/{v_id}/artifact")

    def playground(self, v_id: int, *, input: dict, model: str = "mock-small",
                   temperature: float | None = None) -> dict:
        _require_id(v_id, "v_id")
        _require_dict(input, "input", allow_empty=True)
        _require(model, "model")
        body: dict[str, Any] = {"input": input, "model": model}
        if temperature is not None:
            body["temperature"] = temperature
        return self._h("POST", f"/api/versions/{v_id}/playground/run", body)

    def gate(self, v_id: int) -> dict:
        _require_id(v_id, "v_id")
        return self._h("POST", f"/api/versions/{v_id}/gate", {})

    def plan(self, v_id: int) -> dict:
        _require_id(v_id, "v_id")
        return self._h("GET", f"/api/versions/{v_id}/plan")


class _Plans:
    def __init__(self, http: _Http):
        self._h = http

    def list(self, fn_id: int) -> list[dict]:
        _require_id(fn_id, "fn_id")
        return self._h("GET", f"/api/functions/{fn_id}/plans")

    def create(self, fn_id: int, *, name: str) -> dict:
        _require_id(fn_id, "fn_id")
        _require(name, "name")
        return self._h("POST", f"/api/functions/{fn_id}/plans", {"name": name})

    def update(self, plan_id: int, **fields: Any) -> dict:
        _require_id(plan_id, "plan_id")
        if not fields:
            raise ValueError("update() needs at least one field to change.")
        return self._h("PATCH", f"/api/plans/{plan_id}", fields)

    def accept(self, plan_id: int) -> dict:
        _require_id(plan_id, "plan_id")
        return self._h("POST", f"/api/plans/{plan_id}/accept", {})

    def clone(self, plan_id: int, **body: Any) -> dict:
        _require_id(plan_id, "plan_id")
        return self._h("POST", f"/api/plans/{plan_id}/clone", body)

    def delete(self, plan_id: int) -> None:
        _require_id(plan_id, "plan_id")
        self._h("DELETE", f"/api/plans/{plan_id}")

    def scores(self, plan_id: int) -> list[dict]:
        _require_id(plan_id, "plan_id")
        return self._h("GET", f"/api/plans/{plan_id}/scores")

    def add_score(self, plan_id: int, **body: Any) -> dict:
        _require_id(plan_id, "plan_id")
        if not body:
            raise ValueError("add_score() needs at least the score fields to add.")
        return self._h("POST", f"/api/plans/{plan_id}/scores", body)

    def remove_score(self, plan_id: int, score_id: int) -> None:
        _require_id(plan_id, "plan_id")
        _require_id(score_id, "score_id")
        self._h("DELETE", f"/api/plans/{plan_id}/scores/{score_id}")

    def catalogue(self) -> list[dict]:
        return self._h("GET", "/api/catalogue")


class _Experiments:
    def __init__(self, http: _Http):
        self._h = http

    def list(self, fn_id: int) -> list[dict]:
        _require_id(fn_id, "fn_id")
        return self._h("GET", f"/api/functions/{fn_id}/experiments")

    def get(self, exp_id: int) -> dict:
        _require_id(exp_id, "exp_id")
        return self._h("GET", f"/api/experiments/{exp_id}")

    def create(self, fn_id: int, **pins: Any) -> dict:
        _require_id(fn_id, "fn_id")
        # ExperimentCreate (backend/app/schemas.py) requires version_ids — a
        # plan_id-only or single_input-only pin still needs SOME version to
        # run against, so this is the one field worth enforcing here rather
        # than letting an empty pins dict reach the backend as a 422.
        if not pins.get("version_ids"):
            raise ValueError("create() needs version_ids=[...] — at least one version to run.")
        return self._h("POST", f"/api/functions/{fn_id}/experiments", pins)

    def update(self, exp_id: int, **fields: Any) -> dict:
        _require_id(exp_id, "exp_id")
        if not fields:
            raise ValueError("update() needs at least one field to change.")
        return self._h("PATCH", f"/api/experiments/{exp_id}", fields)

    def run(self, exp_id: int, *, include_findings: bool = True) -> dict:
        """Run a draft experiment. The result carries the scoring details and,
        by default, a `findings` list alongside them — what the UI shows
        together. Pass include_findings=False for the bare experiment."""
        _require_id(exp_id, "exp_id")
        out = self._h("POST", f"/api/experiments/{exp_id}/run", {})
        if include_findings and isinstance(out, dict):
            out["findings"] = self.findings(exp_id)
        return out

    def clone(self, exp_id: int) -> dict:
        _require_id(exp_id, "exp_id")
        return self._h("POST", f"/api/experiments/{exp_id}/clone", {})

    def delete(self, exp_id: int) -> None:
        _require_id(exp_id, "exp_id")
        self._h("DELETE", f"/api/experiments/{exp_id}")

    def findings(self, exp_id: int) -> list[dict]:
        _require_id(exp_id, "exp_id")
        return self._h("GET", f"/api/experiments/{exp_id}/findings")

    def compare(self, exp_ids: list[int]) -> dict:
        """Compare finished experiments side by side — what the Comparisons
        screen shows. An experiment pins exactly one version, so to compare
        v1 with v2 run one experiment on each (same dataset, same Score Set)
        and pass both ids. Returns {"experiments": [...], "rows": [...]}; each
        row is {metric, values (one per experiment, in order), best (index of
        the winner, or None)}. Rows: trust score, pass rate, cost per run,
        p95 latency, each dimension, then every check."""
        if not isinstance(exp_ids, (list, tuple)) or len(exp_ids) < 2:
            raise ValueError("compare() needs at least two experiment ids.")
        exps = []
        for i in exp_ids:
            _require_id(i, "exp_id")
            e = self.get(i)
            if e.get("status") != "completed":
                raise ValueError(f"experiment {i} is {e.get('status')!r} — only completed experiments can be compared.")
            exps.append(e)
        models = [_trust(e.get("results") or {}, e.get("cost") or 0) for e in exps]

        def cost_per_run(e):
            n = (e.get("results") or {}).get("n_rows")
            return ((e.get("cost") or 0) / n) if n else (e.get("cost") or 0)

        def p95(e):
            v = ((e.get("results") or {}).get("latency") or {}).get("p95")
            return v / 1000 if v is not None else None

        def label(e):
            pv = ((e.get("results") or {}).get("per_version") or {}).values()
            nums = [v.get("number") for v in pv if v.get("number")]
            return " vs ".join(f"v{n}" for n in nums) or e.get("name", "")

        rows = [
            _row("Trust score (/100)", [m["trust"] for m in models]),
            _row("Pass rate %", [m["pass_pct"] for m in models]),
            _row("Cost per run $", [cost_per_run(e) for e in exps], higher_better=False),
            _row("p95 latency s", [p95(e) for e in exps], higher_better=False),
        ]
        for key, name in _DIMENSIONS:
            rows.append(_row(name + " %", [None if m["dims"][key] is None else m["dims"][key] * 100
                                           for m in models]))
        for check in sorted({c for m in models for c in m["checks"]}):
            rows.append(_row(f"check: {check} %", [m["checks"].get(check) for m in models]))
        return {"experiments": [{"id": e["id"], "name": e.get("name"), "version": label(e),
                                 "cost": e.get("cost")} for e in exps],
                "rows": rows}


_DIMENSIONS = (("outcome_quality", "Outcome quality"), ("cost", "Cost & latency"),
               ("safety", "Safety"), ("stability", "Stability"), ("robustness", "Robustness"))


def _trust(results: dict, cost: float) -> dict:
    """The UI's trust figures for one experiment (mirrors the app's
    lib/trust.ts, pooled across models): per-dimension pass rate, their mean
    as a 0-100 trust score, and the overall check pass rate."""
    checks = (results or {}).get("checks") or []
    cons = (results or {}).get("consistency") or {}
    dims: dict[str, float | None] = {}
    for key, _ in _DIMENSIONS:
        cs = [c["pass_rate"] for c in checks if (c.get("dimension") or "outcome_quality") == key]
        score = sum(cs) / len(cs) if cs else None
        if key == "stability" and cons.get("available"):
            c_score = (cons.get("overall") or 0) / 100
            score = c_score if score is None else (score + c_score) / 2
        if key == "cost" and score is None:
            score = 1.0
        dims[key] = score
    measured = [v for v in dims.values() if v is not None]
    trust = round(sum(measured) / len(measured) * 100) if measured else 0
    pass_pct = (round(sum(c["pass_rate"] for c in checks) / len(checks) * 100) if checks else None)
    return {"dims": dims, "trust": trust, "pass_pct": pass_pct,
            "checks": {c["check_name"]: c["pass_rate"] * 100 for c in checks}}


def _row(metric: str, values: list, higher_better: bool = True) -> dict:
    """One comparison row: the values per experiment and which index is best
    (None when fewer than two values differ — nothing to call a winner)."""
    defined = [v for v in values if v is not None]
    best = None
    if len(defined) > 1 and max(defined) != min(defined):
        target = max(defined) if higher_better else min(defined)
        best = values.index(target)
    return {"metric": metric, "values": values, "best": best}


class _Scores:
    def __init__(self, http: _Http):
        self._h = http

    def list(self, fn_id: int) -> list[dict]:
        _require_id(fn_id, "fn_id")
        return self._h("GET", f"/api/functions/{fn_id}/scores")

    def create(self, fn_id: int, **body: Any) -> dict:
        _require_id(fn_id, "fn_id")
        # ScoreCreate (backend/app/schemas.py): name and config are both
        # required with no default — tier defaults to "det" server-side, so
        # it's the only one of the four left genuinely optional here.
        _require(body.get("name"), "name")
        _require_dict(body.get("config"), "config", allow_empty=True)
        return self._h("POST", f"/api/functions/{fn_id}/scores", body)

    def results(self, fn_id: int, **filters: Any) -> list[dict]:
        _require_id(fn_id, "fn_id")
        return self._h("GET", f"/api/functions/{fn_id}/score-results", query=filters)

    def set_gate(self, score_id: int, gate: bool) -> dict:
        _require_id(score_id, "score_id")
        if not isinstance(gate, bool):
            raise ValueError(f"gate must be a bool, got {type(gate).__name__}.")
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
        """Validates `name` and that no space with this name already exists
        for the account (the same exact-name match get_scope's X-Space
        lookup uses — see backend/app/auth_usermanagement/security/scope.py),
        so a typo'd "create" that was meant to be a "switch" fails loudly
        here instead of quietly leaving two same-named spaces."""
        _require(name, "name")
        me = self._h("GET", "/api/me")
        existing = next((sp for sp in me["spaces"] if sp["name"] == name), None)
        if existing is not None:
            raise ValueError(f"a space named {name!r} already exists (id={existing['id']}) — "
                             f"use Client.use_space({name!r}) to switch to it instead of creating another.")
        return self._h("POST", "/api/spaces", {"name": name})


class _Datasets:
    def __init__(self, http: _Http):
        self._h = http

    def list(self, fn_id: int, include_archived: bool = False) -> list[dict]:
        _require_id(fn_id, "fn_id")
        q = {"include_archived": "true"} if include_archived else None
        return self._h("GET", f"/api/functions/{fn_id}/datasets", query=q)

    def create(self, fn_id: int, *, name: str, rows: list[dict] | None = None) -> dict:
        _require_id(fn_id, "fn_id")
        _require(name, "name")
        if rows is not None:
            if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
                raise ValueError("rows must be a list of dicts.")
            for i, r in enumerate(rows):
                if "input" not in r:
                    raise ValueError(f"rows[{i}] is missing required key 'input'.")
        return self._h("POST", f"/api/functions/{fn_id}/datasets",
                       {"name": name, "rows": rows or []})

    def update(self, ds_id: int, **fields: Any) -> dict:
        _require_id(ds_id, "ds_id")
        if not fields:
            raise ValueError("update() needs at least one field to change.")
        return self._h("PATCH", f"/api/datasets/{ds_id}", fields)

    def delete(self, ds_id: int) -> None:
        _require_id(ds_id, "ds_id")
        self._h("DELETE", f"/api/datasets/{ds_id}")

    def rows(self, ds_id: int) -> list[dict]:
        _require_id(ds_id, "ds_id")
        return self._h("GET", f"/api/datasets/{ds_id}/rows")

    def add_row(self, ds_id: int, **row: Any) -> dict:
        _require_id(ds_id, "ds_id")
        # RowIn (backend/app/schemas.py): input is required with no default;
        # expected/tags/source all have defaults, so only this one is worth
        # enforcing before a request goes out.
        _require_dict(row.get("input"), "input", allow_empty=True)
        return self._h("POST", f"/api/datasets/{ds_id}/rows", row)

    def upload(self, ds_id: int, path: str, *, mapping: dict[str, str],
               expected_mapping: dict[str, str] | None = None) -> dict:
        """Bulk-load rows from a local .csv, .json (array of flat objects) or
        .xlsx/.xlsm file into an existing dataset — what the UI's "Upload
        rows" does. `mapping` is {file column: input field}; the optional
        `expected_mapping` is {file column: expected-output field}. Columns
        are never guessed. Returns {added, skipped, errors}."""
        _require_id(ds_id, "ds_id")
        _require(path, "path")
        _require_dict(mapping, "mapping")
        if not os.path.isfile(path):
            raise ValueError(f"no such file: {path!r}")
        if not path.lower().endswith((".csv", ".json", ".xlsx", ".xlsm")):
            raise ValueError("unsupported file type — use .csv, .json, or .xlsx")
        with open(path, "rb") as fh:
            content = fh.read()
        return self._h.upload(f"/api/datasets/{ds_id}/upload",
                              filename=os.path.basename(path), content=content,
                              fields={"mapping": json.dumps(mapping),
                                      "expected_mapping": json.dumps(expected_mapping or {})})

    def snapshot(self, ds_id: int) -> dict:
        _require_id(ds_id, "ds_id")
        return self._h("POST", f"/api/datasets/{ds_id}/snapshot", {})

    def snapshots(self, ds_id: int) -> list[dict]:
        _require_id(ds_id, "ds_id")
        return self._h("GET", f"/api/datasets/{ds_id}/snapshots")


class _Tools:
    """Register and manage customer tools (REST endpoint, MCP server, or
    sandboxed Python) — what the UI's tool registration does. Attach one to a
    function with FunctionDraft.add_tool(name)."""

    def __init__(self, http: _Http):
        self._h = http

    def list(self) -> list[dict]:
        return self._h("GET", "/api/tools")

    def get(self, tool_id: int) -> dict:
        _require_id(tool_id, "tool_id")
        return self._h("GET", f"/api/tools/{tool_id}")

    def create(self, *, name: str, description: str, target_type: str, **extra: Any) -> dict:
        """target_type: "http" (url, method), "mcp" (url, mcp_tool_name) or
        "python" (code defining run(args)). Optional: input_schema,
        openapi_spec/openapi_operation, effect, servicing, config_schema,
        auth_kind ("static" | "oauth2_client_credentials") + credential."""
        _require(name, "name")
        _require(description, "description")
        if target_type not in ("http", "mcp", "python"):
            raise ValueError(f"target_type must be 'http', 'mcp' or 'python', got {target_type!r}.")
        if target_type in ("http", "mcp") and not extra.get("url"):
            raise ValueError(f"a {target_type} tool needs url=...")
        if target_type == "python":
            if not extra.get("code"):
                raise ValueError("a python tool needs code=... defining run(args).")
            if not extra.get("input_schema"):
                raise ValueError("a python tool needs input_schema=... (the arguments run(args) takes).")
        elif not (extra.get("input_schema") or extra.get("openapi_spec")):
            raise ValueError(f"a {target_type} tool needs input_schema=... or openapi_spec=... "
                             f"so the agent knows what arguments to send.")
        return self._h("POST", "/api/tools", {"name": name, "description": description,
                                              "target_type": target_type, **extra})

    def test(self, *, target_type: str, sample_args: dict | None = None, **extra: Any) -> dict:
        """Try a tool with a real call before saving it. Nothing is stored."""
        if target_type not in ("http", "mcp", "python"):
            raise ValueError(f"target_type must be 'http', 'mcp' or 'python', got {target_type!r}.")
        return self._h("POST", "/api/tools/test", {"target_type": target_type,
                                                   "sample_args": sample_args or {}, **extra})

    def update(self, tool_id: int, **fields: Any) -> dict:
        _require_id(tool_id, "tool_id")
        if not fields:
            raise ValueError("update() needs at least one field to change.")
        return self._h("PATCH", f"/api/tools/{tool_id}", fields)

    def usage(self, tool_id: int) -> dict:
        _require_id(tool_id, "tool_id")
        return self._h("GET", f"/api/tools/{tool_id}/usage")


class _Deployments:
    def __init__(self, http: _Http):
        self._h = http

    def deploy(self, fn_id: int, version_id: int, *, confirm: bool = False,
               model: Optional[str] = None, temperature: Optional[float] = None) -> dict:
        """Deploy a version. The result also carries `endpoint` and `curl` —
        a ready-to-run call against the public runtime (put a minted fnk_*
        invoke key in place of the placeholder; see keys.invoke.mint).

        `model` (and optionally `temperature`) is the model production calls are
        served with. A version saved by the builder, `load_yaml` or the MCP tools
        names no model, so it needs one here, or the space needs a default model;
        otherwise the deploy is refused rather than served by the mock."""
        _require_id(fn_id, "fn_id")
        _require_id(version_id, "version_id")
        body: dict[str, Any] = {"version_id": version_id}
        if confirm:
            body["confirm"] = True
        if model:
            body["model"] = model
        if temperature is not None:
            body["temperature"] = temperature
        out = self._h("POST", f"/api/functions/{fn_id}/deploy", body)
        if isinstance(out, dict):
            try:
                out.update(self.curl(fn_id))
            except FnaiError:
                pass  # the deploy itself succeeded; the snippet is a convenience
        return out

    def curl(self, fn_id: int, api_key: str | None = None) -> dict:
        """{endpoint, curl} for calling this function's public runtime URL.
        Pass `api_key` (an fnk_* key) to embed it, else a placeholder is used."""
        _require_id(fn_id, "fn_id")
        fn = self._h("GET", f"/api/functions/{fn_id}")
        endpoint = f"{self._h._base}/v1/run/{fn['slug']}"
        schema = fn.get("input_schema") or {}
        if not schema.get("properties"):   # a function built with submit() keeps its schema on the version
            versions = self._h("GET", f"/api/functions/{fn_id}/versions") or []
            if versions:
                schema = max(versions, key=lambda v: v["number"]).get("input_schema") or {}
        props = schema.get("properties") or {}
        sample = json.dumps({k: "..." for k in props})
        curl = (f"curl -X POST {endpoint} \\\n"
                f"  -H \"Authorization: Bearer {api_key or 'fnk_YOUR_KEY'}\" \\\n"
                f"  -H \"Content-Type: application/json\" \\\n"
                f"  -d '{{\"input\": {sample}}}'")
        return {"endpoint": endpoint, "curl": curl}

    def current(self, fn_id: int) -> dict:
        _require_id(fn_id, "fn_id")
        return self._h("GET", f"/api/functions/{fn_id}/deployment")

    def history(self, fn_id: int) -> list[dict]:
        _require_id(fn_id, "fn_id")
        return self._h("GET", f"/api/functions/{fn_id}/deployments")

    def rollback(self, fn_id: int, version_id: int | None = None) -> dict:
        _require_id(fn_id, "fn_id")
        if version_id is not None:
            _require_id(version_id, "version_id")
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
        _require(provider, "provider")
        _require_dict(secret, "secret")
        return self._h("POST", "/api/provider-keys",
                       {"provider": provider, "secret": secret, "label": label, **extra})

    def update(self, key_id: int, **fields: Any) -> dict:
        _require_id(key_id, "key_id")
        if not fields:
            raise ValueError("update() needs at least one field to change.")
        return self._h("PATCH", f"/api/provider-keys/{key_id}", fields)

    def test(self, key_id: int) -> dict:
        _require_id(key_id, "key_id")
        return self._h("POST", f"/api/provider-keys/{key_id}/test", {})

    def rotate(self, key_id: int, secret: dict) -> dict:
        _require_id(key_id, "key_id")
        _require_dict(secret, "secret")
        return self._h("POST", f"/api/provider-keys/{key_id}/rotate", {"secret": secret})

    def delete(self, key_id: int) -> None:
        _require_id(key_id, "key_id")
        self._h("DELETE", f"/api/provider-keys/{key_id}")


class _InvokeKeys:
    """Per-function fnk_* keys for the public runtime — plaintext returned once."""

    def __init__(self, http: _Http):
        self._h = http

    def list(self, fn_id: int) -> list[dict]:
        _require_id(fn_id, "fn_id")
        return self._h("GET", f"/api/functions/{fn_id}/invoke-keys")

    def mint(self, fn_id: int, name: str = "") -> dict:
        _require_id(fn_id, "fn_id")
        return self._h("POST", f"/api/functions/{fn_id}/invoke-keys", {"name": name})

    def revoke(self, key_id: int) -> None:
        _require_id(key_id, "key_id")
        self._h("DELETE", f"/api/invoke-keys/{key_id}")


class _Models:
    """The models you can name as `model` when deploying or running an experiment."""

    def __init__(self, http: _Http):
        self._h = http

    def list(self) -> list[dict]:
        """Every model the platform can route: `model`, `provider`, `input_per_1k`, `output_per_1k`, and
        `usable` (True when this space has what a call needs: the mock always, a real model once its
        provider has an active key, see keys.provider.add). `reason` says what is missing when it is not."""
        return self._h("GET", "/api/models")


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
        if function_id is not None:
            _require_id(function_id, "function_id")
        return self._h("GET", "/api/traces",
                       query={"function_id": function_id, "source": source, "limit": limit})

    def get(self, trace_id: str) -> dict:
        _require(trace_id, "trace_id")
        return self._h("GET", f"/api/traces/{trace_id}")

    def flag(self, trace_id: str) -> dict:
        """The flywheel: production trace → regression dataset row."""
        _require(trace_id, "trace_id")
        return self._h("POST", f"/api/traces/{trace_id}/flag", {})


class _AuthTokens:
    def __init__(self, http: _Http):
        self._h = http

    def list(self) -> list[dict]:
        return self._h("GET", "/api/auth/tokens")

    def mint(self, name: str) -> dict:
        """Plaintext token is in THIS response only."""
        _require(name, "name")
        return self._h("POST", "/api/auth/tokens", {"name": name})

    def revoke(self, token_id: str) -> None:
        _require(token_id, "token_id")
        self._h("DELETE", f"/api/auth/tokens/{token_id}")


class _Auth:
    def __init__(self, http: _Http):
        self.tokens = _AuthTokens(http)


# ── function-definition rules (function-dsl.md, "The field grammar") ────────
class DefinitionError(ValueError):
    """A field or function breaks the definition rules. `.problems` lists
    every issue found, so one call reports them all."""

    def __init__(self, problems: list[str]):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


_TYPES_IN = ("string", "number", "integer", "boolean", "object", "array", "file")
_TYPES_OUT = ("string", "number", "integer", "boolean", "object", "array")
_COMMON_KEYS = {"type", "required", "description", "sensitive", "enum", "minimum", "maximum",
                "format", "pattern", "items", "min_items", "max_items", "fields"}
_INPUT_ONLY_KEYS = {"derived", "media", "formats"}
_OUTPUT_ONLY_KEYS = {"length", "count"}
_FORMATS = ("iso8601", "date", "time", "datetime", "email", "uuid", "url")
_MEDIA = ("document", "image", "audio", "video", "data")
_PY_TYPES = {"string": (str,), "number": (int, float), "integer": (int,), "boolean": (bool,)}


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _check_soft(label: str, key: str, value: Any, problems: list[str]) -> None:
    if not isinstance(value, dict):
        problems.append(f"{label}: '{key}' must be like {{unit: tokens, target: 60, range: [40, 80]}}")
        return
    for k in value:
        if k not in ("unit", "target", "range"):
            problems.append(f"{label}: '{key}' has unknown key '{k}' (allowed: unit, target, range)")
    if "unit" in value and value["unit"] not in ("tokens", "chars"):
        problems.append(f"{label}: '{key}.unit' must be 'tokens' or 'chars', got {value['unit']!r}")
    if "target" in value and not _is_num(value["target"]):
        problems.append(f"{label}: '{key}.target' must be a number")
    if "range" in value:
        r = value["range"]
        if not (isinstance(r, (list, tuple)) and len(r) == 2 and all(_is_num(x) for x in r) and r[0] <= r[1]):
            problems.append(f"{label}: '{key}.range' must be [low, high] with low <= high")


def _check_field(side: str, name: str, spec: Any) -> list[str]:
    """Every problem with one field descriptor, per the DSL field grammar.
    `side` is "input" or "output" (some keys are legal on one side only)."""
    import difflib
    label = f"{side} '{name}'"
    if not isinstance(spec, dict):
        return [f"{label}: must be a dict like {{type: string, required: true}}"]
    problems: list[str] = []
    legal = _COMMON_KEYS | (_INPUT_ONLY_KEYS if side == "input" else _OUTPUT_ONLY_KEYS)
    wrong_side = (_OUTPUT_ONLY_KEYS if side == "input" else _INPUT_ONLY_KEYS)
    for key in spec:
        if key in legal:
            continue
        if key in wrong_side:
            problems.append(f"{label}: '{key}' is only valid on "
                            f"{'output' if side == 'input' else 'input'} fields")
        else:
            near = difflib.get_close_matches(key, sorted(legal), n=1)
            hint = f" — did you mean '{near[0]}'?" if near else ""
            problems.append(f"{label}: unknown key '{key}'{hint}")
    types = _TYPES_IN if side == "input" else _TYPES_OUT
    t = spec.get("type")
    if t not in types:
        extra = " ('file' is input-only)" if t == "file" else ""
        problems.append(f"{label}: type {t!r} is not valid{extra}; use one of {', '.join(types)}")
        return problems
    for flag in ("required", "sensitive"):
        if flag in spec and not isinstance(spec[flag], bool):
            problems.append(f"{label}: '{flag}' must be true or false")
    if "description" in spec and not isinstance(spec["description"], str):
        problems.append(f"{label}: 'description' must be text")
    if spec.get("derived"):
        problems.append(f"{label}: derived fields are filled by a preprocessor, which the builder "
                        f"can't author yet — define this function in YAML instead")
    numeric = t in ("number", "integer")
    for k in ("minimum", "maximum"):
        if k in spec:
            if not numeric:
                problems.append(f"{label}: '{k}' needs type number or integer, got {t}")
            elif not _is_num(spec[k]):
                problems.append(f"{label}: '{k}' must be a number")
    if numeric and _is_num(spec.get("minimum")) and _is_num(spec.get("maximum")) \
            and spec["minimum"] > spec["maximum"]:
        problems.append(f"{label}: minimum {spec['minimum']} is greater than maximum {spec['maximum']}")
    if "format" in spec:
        if t != "string":
            problems.append(f"{label}: 'format' needs type string, got {t}")
        elif spec["format"] not in _FORMATS:
            problems.append(f"{label}: format {spec['format']!r} is not valid; use one of {', '.join(_FORMATS)}")
    if "pattern" in spec:
        if t != "string":
            problems.append(f"{label}: 'pattern' needs type string, got {t}")
        else:
            try:
                re.compile(spec["pattern"])
            except (re.error, TypeError) as e:
                problems.append(f"{label}: 'pattern' is not a valid regular expression ({e})")
    if "enum" in spec:
        e = spec["enum"]
        py = _PY_TYPES.get(t)
        if not isinstance(e, list) or not e:
            problems.append(f"{label}: 'enum' must be a non-empty list")
        elif py is None:
            problems.append(f"{label}: 'enum' can't be used with type {t}")
        else:
            bad = [v for v in e if isinstance(v, bool) != (t == "boolean") or not isinstance(v, py)]
            if bad:
                problems.append(f"{label}: enum values {bad!r} are not of type {t}")
    if "length" in spec:
        if t != "string":
            problems.append(f"{label}: 'length' needs type string, got {t}")
        _check_soft(label, "length", spec["length"], problems)
    if "count" in spec:
        if t != "array":
            problems.append(f"{label}: 'count' needs type array, got {t}")
        _check_soft(label, "count", spec["count"], problems)
    if t == "array":
        if "items" not in spec:
            problems.append(f"{label}: arrays need 'items', e.g. items={{'type': 'string'}}")
        else:
            problems += _check_field(side, f"{name}[]", spec["items"])
    elif "items" in spec:
        problems.append(f"{label}: 'items' needs type array, got {t}")
    for k in ("min_items", "max_items"):
        if k in spec:
            if t != "array":
                problems.append(f"{label}: '{k}' needs type array, got {t}")
            elif not isinstance(spec[k], int) or isinstance(spec[k], bool) or spec[k] < 0:
                problems.append(f"{label}: '{k}' must be a whole number, 0 or more")
    if isinstance(spec.get("min_items"), int) and isinstance(spec.get("max_items"), int) \
            and spec["min_items"] > spec["max_items"]:
        problems.append(f"{label}: min_items {spec['min_items']} is greater than max_items {spec['max_items']}")
    if "fields" in spec:
        if t != "object":
            problems.append(f"{label}: 'fields' needs type object, got {t}")
        elif not isinstance(spec["fields"], dict):
            problems.append(f"{label}: 'fields' must be a dict of field descriptors")
        else:
            for sub, sub_spec in spec["fields"].items():
                problems += _check_field(side, f"{name}.{sub}", sub_spec)
    if t == "file" and "media" not in spec:
        problems.append(f"{label}: file fields need media, one of {', '.join(_MEDIA)}")
    if "media" in spec:
        if t != "file":
            problems.append(f"{label}: 'media' needs type file, got {t}")
        elif spec["media"] not in _MEDIA:
            problems.append(f"{label}: media {spec['media']!r} is not valid; use one of {', '.join(_MEDIA)}")
    if "formats" in spec:
        if t != "file":
            problems.append(f"{label}: 'formats' needs type file, got {t}")
        elif not (isinstance(spec["formats"], list) and spec["formats"]
                  and all(isinstance(f, str) for f in spec["formats"])):
            problems.append(f"{label}: 'formats' must be a list like ['pdf', 'docx']")
    return problems


_GUARD_TOOL = r"[a-z_][a-z0-9_]*"
_GUARD_KEY = r"[a-zA-Z_][a-zA-Z0-9_]*"
_GUARD_REF = rf"(?:{_GUARD_TOOL}\.(?:called|count|returned(?:\.{_GUARD_KEY})+)|input(?:\.{_GUARD_KEY})+)"
_GUARD_TERM = re.compile(rf'^\s*(?P<ref>{_GUARD_REF})\s*(?P<op>==|!=|>=|<=|>|<)\s*'
                         rf'(?P<lit>true|false|-?\d+\.\d+|-?\d+|"[^"]*")\s*$')
_EFFECTS = ("read_only", "idempotent", "side_effecting")


def _guard_problems(tool: str, requires: str, tools: set[str] | None = None,
                    inputs: set[str] | None = None) -> list[str]:
    """Check a tool's `requires` guard against the definition's grammar:
    terms joined by &&, each `ref op literal`. With `tools`/`inputs` given,
    also checks every reference points at something this function declares."""
    problems = []
    for term in [t.strip() for t in (requires or "").split("&&")]:
        m = _GUARD_TERM.match(term)
        if not m:
            problems.append(f"tool '{tool}': guard term {term!r} does not parse — use "
                            f"`<tool>.called|count|returned.<field>` or `input.<field>`, compared with "
                            f"== != > >= < <= to true/false, a number or \"text\"; join terms with &&")
            continue
        ref = m.group("ref")
        head = ref.split(".", 1)[0]
        if head == "input":
            name = ref.split(".")[1]
            if inputs is not None and name not in inputs:
                problems.append(f"tool '{tool}': guard uses input '{name}', which this function doesn't declare")
        elif tools is not None and head not in tools:
            problems.append(f"tool '{tool}': guard uses tool '{head}', which this function doesn't declare")
    return problems


def _example_problems(polarity: Any, input: Any, output: Any, bad_value: Any, note: Any) -> list[str]:
    """The definition's rules for one example: a positive one needs both input
    and output; a negative one needs output (or bad_value) and a note."""
    problems = []
    if polarity not in ("positive", "negative"):
        return [f"example: polarity must be 'positive' or 'negative', got {polarity!r}"]
    for label, v in (("input", input), ("output", output)):
        if v is not None and not isinstance(v, dict):
            problems.append(f"example: {label} must be a dict of field values")
    if polarity == "positive":
        if input is None or output is None:
            problems.append("a positive example needs both input and output")
    else:
        if output is None and bad_value is None:
            problems.append("a negative example needs output or bad_value")
        if not (isinstance(note, str) and note.strip()):
            problems.append("a negative example needs a note saying what is wrong with it")
    return problems


def _project_field(spec: dict) -> dict:
    """One DSL field -> the JSON-schema property the server stores. Mirrors
    the server's own projection (backend definition/schema.py): hard gates as
    JSON Schema, soft targets under x-soft, files as a string handle."""
    out: dict[str, Any] = {"type": "string" if spec["type"] == "file" else spec["type"]}
    for k in ("description", "enum", "minimum", "maximum", "format", "pattern"):
        if k in spec:
            out[k] = spec[k]
    if "min_items" in spec:
        out["minItems"] = spec["min_items"]
    if "max_items" in spec:
        out["maxItems"] = spec["max_items"]
    if "items" in spec:
        out["items"] = _project_field(spec["items"])
    if "fields" in spec:
        out["properties"] = {n: _project_field(s) for n, s in spec["fields"].items()}
        req = [n for n, s in spec["fields"].items() if s.get("required")]
        if req:
            out["required"] = req
    if "media" in spec:
        out["x-media"] = spec["media"]
        out["x-delivery"] = "text" if spec["media"] == "data" else "native"
    if "formats" in spec:
        out["x-formats"] = spec["formats"]
    soft = {k: spec[k] for k in ("length", "count") if k in spec}
    if soft:
        out["x-soft"] = soft
    return out


def _unproject_field(prop: dict, required: bool) -> dict:
    """The reverse of _project_field, for showing a stored schema as DSL."""
    spec: dict[str, Any] = {"type": prop.get("type", "string")}
    if "x-media" in prop or "x-formats" in prop:
        spec["type"] = "file"
    if required:
        spec["required"] = True
    for k in ("description", "enum", "minimum", "maximum", "format", "pattern"):
        if k in prop:
            spec[k] = prop[k]
    if "minItems" in prop:
        spec["min_items"] = prop["minItems"]
    if "maxItems" in prop:
        spec["max_items"] = prop["maxItems"]
    if isinstance(prop.get("items"), dict):
        spec["items"] = _unproject_field(prop["items"], False)
    if isinstance(prop.get("properties"), dict):
        req = set(prop.get("required") or [])
        spec["fields"] = {n: _unproject_field(p, n in req) for n, p in prop["properties"].items()}
    if "x-media" in prop:
        spec["media"] = prop["x-media"]
    if "x-formats" in prop:
        spec["formats"] = prop["x-formats"]
    spec.update(prop.get("x-soft") or {})
    return spec


def _schema_fields(schema: dict | None) -> dict[str, dict]:
    """A stored JSON-schema object as {field name: DSL field}."""
    schema = schema or {}
    req = set(schema.get("required") or [])
    out = {}
    for name, prop in (schema.get("properties") or {}).items():
        if not isinstance(prop, dict):
            continue
        # older SDK versions stored `required` inside the property itself
        out[name] = _unproject_field(prop, name in req or prop.get("required") is True)
    return out


_FORMAT_RE = {
    "email": r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    "uuid": r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
    "url": r"^https?://[^\s/$.?#][^\s]*$",
    "date": r"^\d{4}-\d{2}-\d{2}$",
    "time": r"^\d{2}:\d{2}(:\d{2}(\.\d+)?)?$",
    "datetime": r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$",
    "iso8601": r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$",
}


def _validate_sample(schema: dict, sample: dict) -> list[str]:
    """Check a sample against a stored input schema, the way the server checks
    a real call: required, type, minimum/maximum, enum, pattern, format and
    item counts. Returns the problems found."""
    problems: list[str] = []
    props = (schema or {}).get("properties") or {}
    for name in (schema or {}).get("required") or []:
        if name not in sample:
            problems.append(f"missing required field '{name}'")

    def check(label: str, spec: dict, value: Any) -> None:
        if value is None:
            return
        t = spec.get("type")
        ok = {"boolean": isinstance(value, bool),
              "number": _is_num(value),
              "integer": isinstance(value, int) and not isinstance(value, bool),
              "string": isinstance(value, str),
              "object": isinstance(value, dict),
              "array": isinstance(value, list)}.get(t, True)
        if not ok:
            got = "boolean" if isinstance(value, bool) else type(value).__name__
            problems.append(f"field '{label}' must be a {t}, got {got}")
            return
        if "minimum" in spec and _is_num(value) and value < spec["minimum"]:
            problems.append(f"field '{label}' must be at least {spec['minimum']}, got {value}")
        if "maximum" in spec and _is_num(value) and value > spec["maximum"]:
            problems.append(f"field '{label}' must be at most {spec['maximum']}, got {value}")
        if "enum" in spec and value not in spec["enum"]:
            problems.append(f"field '{label}' must be one of {spec['enum']}, got {value!r}")
        if isinstance(value, str):
            if "pattern" in spec and not re.search(spec["pattern"], value):
                problems.append(f"field '{label}' does not match the required pattern")
            fmt = spec.get("format")
            if fmt in _FORMAT_RE and not re.match(_FORMAT_RE[fmt], value):
                problems.append(f"field '{label}' is not a valid {fmt}")
        if isinstance(value, list):
            if "minItems" in spec and len(value) < spec["minItems"]:
                problems.append(f"field '{label}' must have at least {spec['minItems']} items, got {len(value)}")
            if "maxItems" in spec and len(value) > spec["maxItems"]:
                problems.append(f"field '{label}' must have at most {spec['maxItems']} items, got {len(value)}")
            if isinstance(spec.get("items"), dict):
                for i, v in enumerate(value):
                    check(f"{label}[{i}]", spec["items"], v)

    for name, value in sample.items():
        spec = props.get(name)
        if isinstance(spec, dict):
            check(name, spec, value)
    return problems


def _yaml_scalar(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    s = str(v)
    plain = re.fullmatch(r"[A-Za-z_][A-Za-z0-9_ .-]*", s) and s.lower() not in (
        "true", "false", "null", "yes", "no", "on", "off", "~")
    return s if plain else json.dumps(s, ensure_ascii=False)


def _yaml_flow(v: Any) -> str:
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{k}: {_yaml_flow(x)}" for k, x in v.items()) + " }" if v else "{}"
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_yaml_flow(x) for x in v) + "]"
    return _yaml_scalar(v)


def _missing_lists(inputs: dict, outputs: dict, flow: list, sections: dict) -> tuple[list[str], list[str]]:
    req, rec = [], []
    if not outputs:
        req.append("no output fields — add_output(...)")
    elif not any(f.get("required") for f in outputs.values()):
        req.append("no output field is marked required (required=True) — "
                   "the function has no valid stopping state")
    if not flow:
        req.append("no steps — add_step(...) (the server needs at least one)")
    if not sections.get("role"):
        rec.append("no prompt — add_prompt(...)")
    if not inputs:
        rec.append("no input fields — add_input(...)")
    if not sections.get("examples"):
        rec.append("no examples — add_example(...) (they improve accuracy)")
    if not (sections.get("constraints") or sections.get("edge_cases")):
        rec.append("no constraints or edge cases — add_section('constraints', ...)")
    return req, rec


_MANAGED_TOP = ("name", "description", "input", "output", "implementation", "rubric", "memory", "meta")
_MANAGED_IMPL = ("execution", "flow", "sections", "tools")


def _extras_of(snap: dict) -> tuple[dict, dict]:
    """The parts of a saved version the builder has no call for — `needs`, `preprocessors` and
    anything else it does not manage — so they travel with the function instead of being dropped."""
    snap = snap or {}
    impl = snap.get("implementation") if isinstance(snap.get("implementation"), dict) else {}
    top = {k: v for k, v in snap.items() if k not in _MANAGED_TOP}
    inner = {k: v for k, v in impl.items() if k not in _MANAGED_IMPL}
    return top, inner


def _yaml_extras(lines: list, label: str, extras: dict, indent: str) -> None:
    for key, value in extras.items():
        if isinstance(value, list) and value and all(isinstance(x, dict) for x in value):
            lines.append(f"{indent}{key}:")
            lines += [f"{indent}  - {_yaml_flow(x)}" for x in value]
        else:
            lines.append(f"{indent}{key}: {_yaml_flow(value)}")


class _FunctionYaml:
    """Shared by the draft and the function record: the DSL as YAML, plus
    the list of what's missing. Subclasses supply `_yaml_parts()`."""

    def _missing(self) -> tuple[list[str], list[str]]:
        raise NotImplementedError

    def _yaml_parts(self) -> dict:
        raise NotImplementedError

    def to_yaml(self) -> str:
        """The function as YAML in the definition format (function-dsl.md)."""
        p = self._yaml_parts()
        lines = [f"name: {_yaml_scalar(p['name'])}"]
        if p.get("description"):
            lines.append(f"description: {_yaml_scalar(p['description'])}")
        for section in ("input", "output"):
            fields = p[section]
            if not fields:
                lines.append(f"{section}: {{}}")
                continue
            lines.append(f"{section}:")
            width = max(len(n) for n in fields) + 1
            for n, spec in fields.items():
                lines.append(f"  {(n + ':').ljust(width)} {_yaml_flow(spec)}")
        sections = p.get("sections")
        if sections is not None:
            lines.append("implementation:")
            lines.append(f"  execution: {{ mode: {p.get('mode', 'single_shot')} }}")
            _yaml_extras(lines, "implementation", p.get("extra_impl") or {}, "  ")
            if p.get("flow"):
                lines.append("  flow:")
                lines += [f"    - {json.dumps(x, ensure_ascii=False)}" for x in p["flow"]]
            body = []
            if sections.get("role"):
                body.append(f"    role: {json.dumps(sections['role'], ensure_ascii=False)}")
            for key in ("definitions", "constraints", "edge_cases"):
                if sections.get(key):
                    body.append(f"    {key}:")
                    body += [f"      - {json.dumps(x, ensure_ascii=False)}" for x in sections[key]]
            if sections.get("examples"):
                body.append("    examples:")
                body += [f"      - {_yaml_flow(x)}" for x in sections["examples"]]
            if body:
                lines.append("  sections:")
                lines += body
            if p.get("tools"):
                lines.append("  tools:")
                lines += [f"    - {_yaml_flow(t)}" for t in p["tools"]]
        if p.get("rubric"):
            lines.append("rubric:")
            lines += [f"  - {json.dumps(x, ensure_ascii=False)}" for x in p["rubric"]]
        if p.get("memory"):
            lines.append("memory:")
            lines += [f"  {k}: {_yaml_flow(v)}" for k, v in p["memory"].items()]
        _yaml_extras(lines, "top", p.get("extra_top") or {}, "")
        return "\n".join(lines) + "\n"

    def missing(self) -> dict:
        """{"required": [...], "recommended": [...]} — what the function
        still needs, so nobody has to guess."""
        required, recommended = self._missing()
        return {"required": required, "recommended": recommended}

    def __str__(self) -> str:
        m = self.missing()
        out = self.to_yaml() + "\n"
        if m["required"]:
            out += "# Missing (required):\n" + "".join(f"#  - {i}\n" for i in m["required"])
        else:
            out += "# Missing (required): nothing\n"
        if m["recommended"]:
            out += "# Recommended:\n" + "".join(f"#  - {i}\n" for i in m["recommended"])
        return out


class FunctionRecord(_FunctionYaml, dict):
    """A function as the server returns it (functions.create / .get). It is
    still a plain dict — fn["id"] works as before — but print(fn) shows the
    definition as YAML and what is missing. Once the function has a version,
    the prompt, steps and tools shown come from the latest one."""

    _version: dict | None = None

    def _schemas(self) -> tuple[dict, dict]:
        v = self._version or {}
        return (v.get("input_schema") or self.get("input_schema") or {},
                v.get("output_schema") or self.get("output_schema") or {})

    def _impl(self) -> dict:
        snap = (self._version or {}).get("snapshot") or {}
        return snap.get("implementation") if isinstance(snap.get("implementation"), dict) else {}

    def _yaml_parts(self) -> dict:
        ins, outs = self._schemas()
        impl = self._impl()
        return {"name": self.get("name") or self.get("slug") or "",
                "description": self.get("description"),
                "input": _schema_fields(ins), "output": _schema_fields(outs),
                "mode": (impl.get("execution") or {}).get("mode", "single_shot"),
                "flow": impl.get("flow") or [],
                "extra_top": _extras_of((self._version or {}).get("snapshot") or {})[0],
                "extra_impl": _extras_of((self._version or {}).get("snapshot") or {})[1],
                "rubric": ((self._version or {}).get("snapshot") or {}).get("rubric") or [],
                "memory": ((self._version or {}).get("snapshot") or {}).get("memory"),
                "sections": impl.get("sections") if self._version is not None else None,
                "tools": impl.get("tools") or []}

    def _missing(self) -> tuple[list[str], list[str]]:
        ins, outs = self._schemas()
        if self._version is None:
            req, rec = _missing_lists(_schema_fields(ins), _schema_fields(outs), ["-"], {"role": "-", "examples": ["-"], "constraints": ["-"]})
            rec.append("no version saved yet — build one with c.functions.new(...) or c.load_draft(id)")
            return req, rec
        impl = self._impl()
        return _missing_lists(_schema_fields(ins), _schema_fields(outs), impl.get("flow") or [],
                              impl.get("sections") if isinstance(impl.get("sections"), dict) else {})



# ── local draft files ────────────────────────────────────────────────────────
def _drafts_dir(drafts_folder: Any = None) -> pathlib.Path:
    """Where draft files go when no exact path is given: the folder the user
    set, else $FNAI_DRAFTS_DIR, else .fnai/drafts next to where the script runs."""
    base = drafts_folder or os.environ.get("FNAI_DRAFTS_DIR") or pathlib.Path.cwd() / ".fnai" / "drafts"
    return pathlib.Path(base).expanduser().resolve()


def _versions_dir(versions_folder: Any, drafts_folder: Any) -> pathlib.Path:
    """Where a copy of every saved version goes: the folder the user set, else $FNAI_VERSIONS_DIR, else
    .fnai/versions beside .fnai/drafts (inside the drafts folder if that is not called "drafts")."""
    base = versions_folder or os.environ.get("FNAI_VERSIONS_DIR")
    if base:
        return pathlib.Path(base).expanduser().resolve()
    drafts = _drafts_dir(drafts_folder)
    return drafts.parent / "versions" if drafts.name == "drafts" else drafts / "versions"


def _resolve_draft_path(save_to: Any, drafts_folder: Any, slug: str) -> pathlib.Path | None:
    """The draft file for a function. save_to wins (a .yaml/.yml path is the
    file itself, anything else is a folder), then the drafts folder, then the
    default. save_to=False turns the draft file off."""
    if save_to is False:
        return None
    if save_to:
        p = pathlib.Path(save_to).expanduser()
        return (p if p.suffix.lower() in (".yaml", ".yml") else p / f"{slug}.yaml").resolve()
    return _drafts_dir(drafts_folder) / f"{slug}.yaml"


def _write_atomic(path: pathlib.Path, text: str) -> None:
    """Write to a temporary file beside the real one, then swap it in, so a
    crash halfway never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _autosaves(method):
    """After a call that changes the draft, update the draft file."""
    import functools

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        result = method(self, *args, **kwargs)
        self._autosave()
        return result
    return wrapper


class FunctionDraft(_FunctionYaml):
    """The one shared "save" flow (FUNCTION_CLASS_DESIGN.md, approved
    2026-09-24): create/add_input/add_output/add_section/add_example/
    add_tool/submit/deploy — the same actions the canvas UI performs against
    the same POST /api/functions/{id}/submit endpoint, so the SDK and the UI
    can never drift into different save behaviour again (see the old
    create_from_yaml_text() vs. the canvas's PUT /versions/{id} split this
    replaces going forward).

    Named FunctionDraft rather than the design doc's plain `Function` because
    `Function` is already taken in this SDK — the public runtime handle on a
    DEPLOYED function, keyed by an fnk_* invoke key (see the top of this
    file). That class runs a function; this one builds one.

    Every add_* call only stages a change locally — nothing reaches the
    server until submit(). submit() is the one place "update the current
    draft in place" vs. "start a new version" gets decided, and it's decided
    the same way regardless of who calls it."""

    def __init__(self, http: _Http, fn: dict):
        self._h = http
        self.id: int | None = fn.get("id")      # None until submit() creates the function
        self.slug = fn.get("slug")
        self.name = fn.get("name")
        self._input_schema: dict[str, Any] = dict(fn.get("input_schema") or {})
        self._output_schema: dict[str, Any] = dict(fn.get("output_schema") or {})
        self._sections: dict[str, Any] = {
            "role": None, "definitions": [], "constraints": [], "edge_cases": [], "examples": [],
        }
        self._tools: list[dict] = []
        self._flow: list[str] = []
        self._rubric: list[str] = []
        self._memory: dict | None = None
        self._mode: str | None = None
        self.description = fn.get("description") or ""
        self.last_version: dict | None = None
        # fields added through add_input/add_output in DSL form (validated)
        self._in_added: dict[str, dict] = {}
        self._out_added: dict[str, dict] = {}
        # which version a save goes to (see load(version=...)):
        #   neither set  -> the server's rule: update the latest draft, or start a new version
        #   _target      -> edit that draft in place
        #   _base        -> a frozen version this draft grew from: a save creates a NEW version
        self._target: dict | None = None
        self._base: dict | None = None
        # parts of a loaded version the builder has no call for, kept so a save does not drop them
        self._extra_top: dict = {}                  # needs, ...
        self._extra_impl: dict = {}                 # preprocessors, ...
        self._field_extras: dict = {"input": {}, "output": {}}   # name -> {"flags": {...}, "full": spec | None}
        self._field_order: dict = {"input": [], "output": []}
        self._fresh = False                            # load(fresh=True): started empty, so a save is always a NEW version
        self._baseline: dict | None = None             # the loaded version's outline, to list changes() against
        self._save_to: Any = None                      # where the draft file goes (see _set_draft_path)
        self._drafts_cfg: Any = None
        self._loading = False                          # True while a file is being read back in
        self._warned = False

    @classmethod
    def create(cls, http: _Http, *, name: str, slug: str | None = None,
               description: str = "", save_to: Any = None,
               drafts_folder: Any = None) -> "FunctionDraft":
        """Start a new function on your computer. Nothing is created on the
        server until submit(): the one thing done here is a read-only check
        that the name is free (archived functions keep their name, so they
        count). While you build, the draft file is kept up to date."""
        _require(name, "name")
        slug = slug or _slugify(name)
        taken = next((f for f in http("GET", "/api/functions", query={"include_archived": "true"}) or []
                      if f.get("slug") == slug), None)
        if taken is not None:
            state = " (archived)" if taken.get("archived") else ""
            raise ValueError(
                f"a function with slug {slug!r} already exists{state} (id={taken['id']}) — pick another "
                f"name (or slug=...), or carry on with that one using c.load_draft({taken['id']}).")
        fn = {"slug": slug, "name": name, "description": description}
        draft = cls(http, fn)
        draft._set_draft_path(save_to, drafts_folder)
        draft._autosave()
        return draft

    @classmethod
    def load(cls, http: _Http, fn_id: "int | str", *, version: int | None = None,
             save_to: Any = None, drafts_folder: Any = None, fresh: bool = False) -> "FunctionDraft":
        """Resume an existing function to keep building on it — e.g. adding
        a field after it's already deployed. `fn_id` is the function's id or its slug.

        By default you get the LATEST version's content. Pass `version=N` to
        start from version N instead:
          - a frozen (deployed) version is never changed: saving creates a NEW
            version based on it, and never overwrites another draft;
          - a draft is edited in place when you save.

        fresh=True keeps the function (same name and slug) but starts it EMPTY: only what
        you add is in the new version. A fresh draft always saves as a NEW version
        (it grows from the one you loaded and never overwrites it), and submit() lists what
        was dropped, added or changed before sending."""
        fn_id = _resolve_function_id(http, fn_id)
        if version is not None and (isinstance(version, bool) or not isinstance(version, int) or version < 1):
            raise ValueError(f"version must be a version number such as 1, got {version!r}.")
        fn = http("GET", f"/api/functions/{fn_id}")
        draft = cls(http, fn)
        draft._set_draft_path(save_to, drafts_folder)
        versions = http("GET", f"/api/functions/{fn_id}/versions") or []
        chosen = None
        if version is None:
            if versions:
                chosen = max(versions, key=lambda v: v["number"])
        else:
            chosen = next((v for v in versions if v["number"] == version), None)
            if chosen is None:
                have = ", ".join(str(n) for n in sorted(v["number"] for v in versions)) or "none yet"
                raise FnaiError(404, f"no version {version} for {fn.get('slug')!r} (versions: {have})")
            draft._point_at(chosen)
        if chosen is not None:
            draft._apply_version(chosen)
            draft._baseline = draft._outline()
            if fresh:
                draft._start_empty()
        return draft

    def _start_empty(self) -> None:
        """Forget the loaded content but keep the function itself and the version it grew from."""
        self._input_schema, self._output_schema = {}, {}
        self._sections = {"role": None, "definitions": [], "constraints": [], "edge_cases": [], "examples": []}
        self._tools, self._flow, self._rubric, self._memory, self._mode = [], [], [], None, None
        self._extra_top, self._extra_impl = {}, {}
        self._field_extras, self._field_order = {"input": {}, "output": {}}, {"input": [], "output": []}
        self._in_added, self._out_added = {}, {}
        self._target = self._base = None
        self._fresh = True
        self._autosave()

    def _outline(self) -> dict:
        """The function as {label: value}, one entry per part, for listing changes."""
        out: dict[str, Any] = {}
        for side in ("input", "output"):
            for n, spec in self._dsl_fields(side).items():
                out[f"{side} {n}"] = spec
        if self._sections.get("role"):
            out["prompt"] = self._sections["role"]
        for kind in ("definitions", "constraints", "edge_cases"):
            for text in self._sections.get(kind) or []:
                out[f"{kind} {text}"] = True
        for i, ex in enumerate(self._sections.get("examples") or [], 1):
            out["example " + json.dumps(ex, sort_keys=True, default=str)] = True
        for text in self._flow:
            out[f"step {text}"] = True
        for t in self._tools:
            out[f"tool {t.get('name') or t.get('server') or json.dumps(t, sort_keys=True, default=str)}"] = t
        for text in self._rubric:
            out[f"rubric {text}"] = True
        if self._memory:
            out["memory"] = self._memory
        for k, v in {**self._extra_top, **self._extra_impl}.items():
            out[k] = v
        return out

    def changes(self) -> list[str]:
        """What differs from the version you loaded: "+ input company" (added), "- input rating"
        (dropped), "~ input name" (changed). Empty when nothing differs or nothing was loaded."""
        if self._baseline is None:
            return []
        now, was = self._outline(), self._baseline
        lines = [f"- {k}" for k in was if k not in now]
        lines += [f"+ {k}" for k in now if k not in was]
        lines += [f"~ {k}" for k in now if k in was and now[k] != was[k]]
        order = {"-": 0, "+": 1, "~": 2}
        return sorted(lines, key=lambda l: order[l[0]])

    def _point_at(self, version: dict) -> None:
        """Where the next save goes: a draft is edited in place; a frozen version
        becomes the base of a new one."""
        if version.get("status") == "draft":
            self._target, self._base = version, None
        else:
            self._base, self._target = version, None

    def _apply_version(self, v: dict) -> None:
        """Stage a saved version's content, so add_* calls extend it. Everything in the version is
        kept, including parts the builder has no call for (needs, preprocessors, derived and
        sensitive fields, server tool entries)."""
        snap = v.get("snapshot") or {}
        if snap and not isinstance(snap.get("implementation"), dict) and (snap.get("sections") or snap.get("impl")):
            raise DefinitionError([
                f"v{v.get('number')} was saved in the older editor format, which the builder can't edit "
                f"without losing content. Edit it in the app, or build a new version with the builder."])
        self.last_version = v
        # a function built with submit() keeps its schemas on the version
        if v.get("input_schema"):
            self._input_schema = copy.deepcopy(v["input_schema"])
        if v.get("output_schema"):
            self._output_schema = copy.deepcopy(v["output_schema"])
        impl = snap.get("implementation") or {}
        sections = impl.get("sections") or {}
        for key in ("role", "definitions", "constraints", "edge_cases", "examples"):
            if key in sections:
                self._sections[key] = sections[key]
        self._tools = list(impl.get("tools") or [])
        self._flow = list(impl.get("flow") or [])
        self._rubric = list(snap.get("rubric") or [])
        self._memory = dict(snap["memory"]) if isinstance(snap.get("memory"), dict) else None
        self._extra_top, self._extra_impl = _extras_of(snap)
        for side in ("input", "output"):
            self._field_extras[side], self._field_order[side] = {}, []
            dsl = snap.get(side)
            if isinstance(dsl, dict):
                self._field_order[side] = list(dsl)
                for name, spec in dsl.items():
                    if not isinstance(spec, dict):
                        continue
                    if spec.get("derived"):        # filled by a preprocessor: never in the stored JSON schema
                        self._field_extras[side][name] = {"flags": {}, "full": {k: x for k, x in spec.items() if x not in (False, None)}}
                    elif spec.get("sensitive"):
                        self._field_extras[side][name] = {"flags": {"sensitive": True}, "full": None}
        # only an explicit "agent" is kept; single_shot is the default and
        # should flip to agent on its own once a tool is added
        if (impl.get("execution") or {}).get("mode") == "agent":
            self._mode = "agent"

    def _dsl_fields(self, side: str) -> dict:
        """The fields in the definition's form: the stored schema plus what it cannot hold
        (derived fields, the sensitive flag), in their original order."""
        schema = self._input_schema if side == "input" else self._output_schema
        fields = _schema_fields(schema)
        for name, ex in self._field_extras[side].items():
            if name in fields:
                fields[name].update(ex["flags"])
            elif ex.get("full") is not None:
                fields[name] = dict(ex["full"])
        order = [n for n in self._field_order[side] if n in fields]
        return {**{n: fields[n] for n in order}, **{n: f for n, f in fields.items() if n not in order}}

    @_autosaves
    def _add_field(self, side: str, field_name: str, type: str, required: bool,
                   spec: dict) -> "FunctionDraft":
        _require(field_name, "field_name")
        full = {"type": type, "required": required, **spec}
        problems = _check_field(side, field_name, full)
        schema = self._input_schema if side == "input" else self._output_schema
        if field_name in (schema.get("properties") or {}) or field_name in self._field_extras[side]:
            problems.append(f"{side} '{field_name}' is already defined")
        if problems:
            raise DefinitionError(problems)
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})[field_name] = _project_field(full)
        req = [n for n in schema.get("required", []) if n != field_name]
        if required:
            req.append(field_name)
        if req:
            schema["required"] = req
        else:
            schema.pop("required", None)
        if full.get("sensitive") is True:       # the stored schema cannot hold it: keep it alongside
            self._field_extras[side][field_name] = {"flags": {"sensitive": True}, "full": None}
        self._field_order[side] = [n for n in self._field_order[side] if n != field_name] + [field_name]
        (self._in_added if side == "input" else self._out_added)[field_name] = full
        return self

    def add_input(self, field_name: str, *, type: str = "string", required: bool = False,
                 **spec: Any) -> "FunctionDraft":
        """Attach one input field. Checked now against the definition rules
        (function-dsl.md) — a mistake raises DefinitionError here, not later.
        Just remembered for now, not saved yet."""
        return self._add_field("input", field_name, type, required, spec)

    def add_output(self, field_name: str, *, type: str = "string", required: bool = False,
                  **spec: Any) -> "FunctionDraft":
        """Attach one output field. Checked now, same as add_input.
        Just remembered for now."""
        return self._add_field("output", field_name, type, required, spec)

    @_autosaves
    def _remove_field(self, side: str, field_name: str) -> "FunctionDraft":
        _require(field_name, "field_name")
        schema = self._input_schema if side == "input" else self._output_schema
        props = schema.get("properties") or {}
        if field_name not in props and field_name not in self._field_extras[side]:
            raise DefinitionError([f"{side} '{field_name}' is not defined, nothing to remove"])
        props.pop(field_name, None)
        req = [n for n in schema.get("required", []) if n != field_name]
        if req:
            schema["required"] = req
        else:
            schema.pop("required", None)
        self._field_extras[side].pop(field_name, None)
        self._field_order[side] = [n for n in self._field_order[side] if n != field_name]
        (self._in_added if side == "input" else self._out_added).pop(field_name, None)
        return self

    def remove_input(self, field_name: str) -> "FunctionDraft":
        """Take one input field out of this draft. Raises DefinitionError if
        there is no such field. Just remembered for now; an example that still
        uses the field is reported when you submit()."""
        return self._remove_field("input", field_name)

    def remove_output(self, field_name: str) -> "FunctionDraft":
        """Take one output field out of this draft. Same rules as remove_input."""
        return self._remove_field("output", field_name)

    @_autosaves
    def remove_step(self, step: int | str) -> "FunctionDraft":
        """Take one step out of the flow. Pass its number (1 = the first step,
        as print(fn) lists them) or its exact text."""
        if isinstance(step, int) and not isinstance(step, bool):
            if not 1 <= step <= len(self._flow):
                raise DefinitionError([f"step {step} does not exist (the flow has {len(self._flow)} step(s))"])
            del self._flow[step - 1]
        elif isinstance(step, str) and step in self._flow:
            self._flow.remove(step)
        else:
            raise DefinitionError([f"no step matches {step!r}"])
        return self

    def add_prompt(self, text: str) -> "FunctionDraft":
        """Set the prompt: who the function is and what it does (the role
        section). Replaces any earlier prompt. Just remembered for now."""
        return self.add_section("role", text)

    def check_input(self, sample: dict) -> list[str]:
        """Test a sample input against this function's input rules without
        calling the server. Returns the problems found ([] means it passes)."""
        _require_dict(sample, "sample", allow_empty=True)
        return _validate_sample(self._input_schema, sample)

    @_autosaves
    def add_section(self, kind: str, content: str) -> "FunctionDraft":
        """Attach a piece of prompt text — its role, its rules, its edge
        cases. Just remembered for now. `kind` is one of "role" (replaces —
        a function has exactly one role), or "definitions"/"constraints"/
        "edge_cases" (appends — a function can have many of each)."""
        _require(kind, "kind")
        _require(content, "content")
        if kind == "role":
            self._sections["role"] = content
        elif kind in ("definitions", "constraints", "edge_cases"):
            self._sections[kind].append(content)
        else:
            raise ValueError(f'kind must be "role", "definitions", "constraints", or '
                             f'"edge_cases", got {kind!r}.')
        return self

    @_autosaves
    def add_example(self, *, polarity: str = "positive", input: dict | None = None,
                    output: dict | None = None, bad_value: Any = None,
                    note: str | None = None) -> "FunctionDraft":
        """Attach one worked example. A positive example needs input and
        output; a negative one needs output (or bad_value) and a note.
        Checked now. Just remembered for now."""
        problems = _example_problems(polarity, input, output, bad_value, note)
        if problems:
            raise DefinitionError(problems)
        example: dict[str, Any] = {"polarity": polarity}
        if input is not None:
            example["input"] = input
        if output is not None:
            example["output"] = output
        if bad_value is not None:
            example["bad_value"] = bad_value
        if note is not None:
            example["note"] = note
        self._sections["examples"].append(example)
        return self

    @_autosaves
    def add_step(self, text: str) -> "FunctionDraft":
        """Add one step the function follows, in order (the definition's
        `flow`). At least one is required. Just remembered for now."""
        _require(text, "text")
        self._flow.append(text)
        return self

    @_autosaves
    def add_rubric(self, text: str) -> "FunctionDraft":
        """Add one sentence describing what a good answer looks like. Use it
        when the output is free text, which a rule like "a number from 1 to
        5" cannot judge. Just remembered for now."""
        if not (isinstance(text, str) and text.strip()):
            raise DefinitionError(["rubric: the sentence can't be empty"])
        if text in self._rubric:
            raise DefinitionError([f"rubric: this sentence is already added: {text!r}"])
        self._rubric.append(text)
        return self

    @_autosaves
    def set_memory(self, *, per: str, remember: list[str] | None = None,
                   never: list[str] | None = None) -> "FunctionDraft":
        """Let the function remember things between runs. `per` is who shares
        one memory: "caller", "space" or "organisation". `remember` and
        `never` are optional lists of sentences saying what is worth keeping
        and what to keep out (guidance for the function, not a hard control).
        A second call replaces the first. Just remembered for now."""
        problems = []
        if per not in ("caller", "space", "organisation"):
            problems.append(f"memory: per must be 'caller', 'space' or 'organisation', got {per!r}")
        for label, items in (("remember", remember), ("never", never)):
            if items is not None and not (isinstance(items, (list, tuple))
                                          and all(isinstance(i, str) and i.strip() for i in items)):
                problems.append(f"memory: {label} must be a list of non-empty sentences")
        if problems:
            raise DefinitionError(problems)
        mem: dict[str, Any] = {"per": per}
        if remember:
            mem["remember"] = list(remember)
        if never:
            mem["never"] = list(never)
        self._memory = mem
        return self

    @_autosaves
    def set_mode(self, mode: str) -> "FunctionDraft":
        """"single_shot" (one pass) or "agent" (loops, can use tools). Left
        alone, it is "agent" once a tool is added and "single_shot" otherwise."""
        if mode not in ("single_shot", "agent"):
            raise ValueError(f'mode must be "single_shot" or "agent", got {mode!r}.')
        self._mode = mode
        return self

    @_autosaves
    def add_tool(self, name: str, *, max_calls: int | None = None, requires: str | None = None,
                 effect: str | None = None, ref: str | None = None, **config: Any) -> "FunctionDraft":
        """Attach one tool it's allowed to use. Extra keyword arguments are
        this function's own settings for the tool (its `config`); max_calls
        caps uses per run, requires locks the tool until a condition holds,
        effect may only tighten the tool's registered effect. Just remembered
        for now."""
        _require(name, "name")
        problems = []
        if any(t.get("name") == name for t in self._tools):
            problems.append(f"tool '{name}' is already added")
        if max_calls is not None and (isinstance(max_calls, bool) or not isinstance(max_calls, int) or max_calls < 1):
            problems.append(f"tool '{name}': max_calls must be a whole number, 1 or more")
        if effect is not None and effect not in _EFFECTS:
            problems.append(f"tool '{name}': effect must be one of {', '.join(_EFFECTS)}, got {effect!r}")
        if requires is not None:
            problems += _guard_problems(name, requires)
        if problems:
            raise DefinitionError(problems)
        entry: dict[str, Any] = {"name": name}
        if config:
            entry["config"] = config
        for key, value in (("max_calls", max_calls), ("requires", requires),
                           ("effect", effect), ("ref", ref)):
            if value is not None:
                entry[key] = value
        self._tools.append(entry)
        return self

    def _set_draft_path(self, save_to: Any, drafts_folder: Any) -> None:
        self._save_to, self._drafts_cfg = save_to, drafts_folder

    @property
    def _draft_path(self) -> pathlib.Path | None:
        """The working draft file, named for what you are editing, so two drafts of one function never
        share a file: <slug>.yaml (a new function or the latest), <slug>.v3.yaml (editing draft v3),
        <slug>.from-v1.yaml (a new version being built from v1). An exact save_to file is used as given."""
        slug = self.slug or _slugify(self.name or "function")
        if self._save_to is False:
            return None
        if self._save_to:
            p = pathlib.Path(self._save_to).expanduser()
            if p.suffix.lower() in (".yaml", ".yml"):
                return p.resolve()
            folder = p
        else:
            folder = _drafts_dir(self._drafts_cfg)
        suffix = (f".v{self._target['number']}" if self._target is not None
                  else f".from-v{self._base['number']}" if self._base is not None else "")
        return (folder / f"{slug}{suffix}.yaml").resolve()

    def _version_record_path(self, number: int) -> pathlib.Path | None:
        """Where the copy of saved version `number` goes, or None when copies are off."""
        if self._save_to is False or not getattr(self._h, "keep_versions", True):
            return None
        folder = _versions_dir(getattr(self._h, "versions_folder", None), self._drafts_cfg)
        return folder / (self.slug or _slugify(self.name or "function")) / f"v{number}.yaml"

    def _file_yaml(self) -> str:
        """The YAML written to files: the same text print() shows, with a
        `meta` header naming the function so it can be found again on load."""
        meta = f"meta:\n  function: {_yaml_scalar(self.slug)}\n"
        if self._base is not None:
            meta += f"  based_on: {self._base['number']}\n"
        if self._target is not None:
            meta += f"  editing: {self._target['number']}\n"
        return meta + self.to_yaml()

    def _autosave(self) -> None:
        """Keep the local draft file in step with the function. A problem
        writing the file warns once and never stops the script."""
        if self._draft_path is None or self._loading:
            return
        try:
            _write_atomic(self._draft_path, self._file_yaml())
        except OSError as e:
            if not self._warned:
                self._warned = True
                warnings.warn(f"fnai: couldn't write the draft file {self._draft_path}: {e}. "
                              f"Your work is still in memory.", stacklevel=3)

    def _finish_save(self, working: pathlib.Path | None, version: dict) -> None:
        """After a successful save the server has the real copy. The working draft file becomes that
        version's record (versions/<slug>/v<N>.yaml) instead of being deleted, so you keep a readable
        copy of every version you save. With copies off (keep_versions=False) it is just removed."""
        record = self._version_record_path(version["number"]) if working is not None else None
        try:
            if record is not None:
                text = (f"meta:\n  function: {_yaml_scalar(self.slug)}\n  version: {version['number']}\n"
                        + self.to_yaml())
                _write_atomic(record, text)
        except OSError as e:
            warnings.warn(f"fnai: couldn't write the copy of v{version['number']} at {record}: {e}", stacklevel=3)
        if working is not None:
            try:
                working.unlink(missing_ok=True)
            except OSError:
                pass

    def save_yaml(self, path: Any) -> str:
        """Write the function as YAML to a path you choose, any time. A path
        ending in .yaml/.yml is the file; anything else is a folder, and the
        file is named after the function. Folders are created. Returns the
        path written. Unlike the automatic draft file, this copy stays after
        submit()."""
        p = pathlib.Path(path).expanduser()
        if p.suffix.lower() not in (".yaml", ".yml"):
            p = p / f"{self.slug}.yaml"
        try:
            _write_atomic(p, self._file_yaml())
        except OSError as e:
            raise OSError(f"couldn't write {p}: {e}") from e
        return str(p.resolve())

    def __str__(self) -> str:
        text = super().__str__()
        if self._base is not None:
            return f"# based on v{self._base['number']} — saving creates a new version\n" + text
        if self._target is not None:
            return f"# editing draft v{self._target['number']} — saving updates it\n" + text
        return text

    def _yaml_parts(self) -> dict:
        return {"name": self.name or self.slug or "",
                "input": self._dsl_fields("input"),
                "output": self._dsl_fields("output"),
                "extra_top": self._extra_top, "extra_impl": self._extra_impl,
                "description": self.description,
                "mode": self._effective_mode(),
                "flow": self._flow,
                "rubric": self._rubric, "memory": self._memory,
                "sections": {k: v for k, v in self._sections.items() if v},
                "tools": self._tools}

    def _effective_mode(self) -> str:
        return self._mode or ("agent" if self._tools else "single_shot")

    def _missing(self) -> tuple[list[str], list[str]]:
        return _missing_lists(self._dsl_fields("input"), self._dsl_fields("output"),
                              self._flow, {k: v for k, v in self._sections.items() if v})

    def _tool_problems(self) -> list[str]:
        """Names in add_tool() that don't match a tool this space can use.
        One GET at submit time; skipped if the lookup itself fails (the
        server rejects an unknown tool on submit anyway)."""
        import difflib
        names = [t["name"] for t in self._tools if t.get("name") and not t.get("ref")]
        if not names:
            return []
        try:
            known = sorted({t["name"] for t in self._h("GET", "/api/tools") or []
                            if t.get("status") in (None, "active", "deprecated")})
        except (FnaiError, KeyError, TypeError):
            return []
        problems = []
        for n in names:
            if n not in known:
                near = difflib.get_close_matches(n, known, n=1)
                hint = f" — did you mean '{near[0]}'?" if near else ""
                problems.append(f"tool '{n}' not found{hint} (available: {', '.join(known) or 'none'}; "
                                f"register your own with c.tools.create)")
        return problems

    def _structure_problems(self) -> list[str]:
        problems = []
        if not (self._output_schema.get("properties") or {}):
            problems.append("no output fields — add_output(...)")
        if not self._flow:
            problems.append("no steps — add_step(...)")
        has_server = any("server" in t for t in self._tools)     # a server entry expands to tools we can't see here
        declared_tools = None if has_server else {t["name"] for t in self._tools if t.get("name")}
        declared_inputs = set(self._input_schema.get("properties") or {}) | set(self._field_extras["input"])
        for t in self._tools:
            if t.get("requires") and t.get("name"):
                problems += _guard_problems(t["name"], t["requires"], declared_tools, declared_inputs)
        problems += self._tool_problems()
        ins = set((self._input_schema.get("properties") or {}))
        outs = set((self._output_schema.get("properties") or {}))
        for i, ex in enumerate(self._sections.get("examples") or [], 1):
            for key in (ex.get("input") or {}):
                if ins and key not in ins:
                    problems.append(f"example {i}: input field '{key}' is not declared (add_input)")
            for key in (ex.get("output") or {}):
                if outs and key not in outs:
                    problems.append(f"example {i}: output field '{key}' is not declared (add_output)")
        return problems

    def _snapshot(self) -> dict:
        """The function as a v2 definition document: input and output in the
        definition's field form, plus implementation (mode, flow, sections,
        tools) — what the server validates and compiles."""
        sections = {k: v for k, v in self._sections.items() if v}
        snap: dict[str, Any] = {
            "name": self.name,
            "input": self._dsl_fields("input"),
            "output": self._dsl_fields("output"),
            "implementation": {"execution": {"mode": self._effective_mode()}, **self._extra_impl,
                               "flow": list(self._flow), "sections": sections,
                               "tools": self._tools},
        }
        if self.description:
            snap["description"] = self.description
        if self._rubric:
            snap["rubric"] = list(self._rubric)
        if self._memory:
            snap["memory"] = dict(self._memory)
        snap.update(self._extra_top)
        return snap

    def _ensure_created(self) -> None:
        """First save: create the function on the server. Remembered, so a
        retry after a failed save carries on with the same function instead
        of creating it twice."""
        if self.id is not None:
            return
        try:
            fn = self._h("POST", "/api/functions",
                         {"slug": self.slug, "name": self.name, "description": self.description,
                          "input_schema": self._input_schema, "output_schema": self._output_schema})
        except FnaiError as e:
            if e.status == 409:
                raise FnaiError(409, f"a function with slug {self.slug!r} already exists in this organization "
                                     f"(archived functions keep their name). Nothing was saved; your work is "
                                     f"still in memory and in the draft file. Start again with a different "
                                     f"slug: c.functions.new(name, slug='...').") from None
            raise
        self.id = fn["id"]

    def submit(self, message: str = "", *, new_version: bool | None = None) -> dict:
        """This is Save. Everything remembered since the last save gets
        committed now. By default it updates the latest draft in place, or
        starts a new version if the latest is already deployed and frozen —
        the one rule that decision follows, used by the UI and every SDK
        caller alike (POST /api/functions/{id}/submit). A draft loaded with
        load_draft(..., version=N) saves to that version instead (a frozen
        one always becomes a NEW version).

        new_version=True always creates a NEW version, even if the one you
        started from is a draft: that version is left exactly as it was, and
        the new one records it as its parent. Nothing is deployed. Later plain
        saves update the new draft in place.

        Before sending, checks the whole function and raises one
        DefinitionError listing every problem, e.g. an example that names a
        field the function doesn't declare."""
        if self._fresh:
            if new_version is False:
                raise ValueError("this draft was started with fresh=True, so it can only be saved as a NEW version "
                                 "(it would overwrite the one it grew from). Drop new_version=False.")
            new_version = True
        new_version = bool(new_version)
        problems = self._structure_problems()
        if problems:
            raise DefinitionError(problems)
        if self._fresh:
            shown = self.changes()
            print("Changes from the version you started from:" if shown else "No changes from the version you started from.")
            for line in shown:
                print("  " + line)
        self._ensure_created()
        working = self._draft_path          # the file you were working in, before a save changes what you are editing
        body = {"snapshot": self._snapshot(), "message": message,
                "input_schema": self._input_schema, "output_schema": self._output_schema}
        if new_version:
            # branch: always a NEW version; the one it grew from is left untouched
            parent = self._target or self._base or self.last_version
            payload = {**body, **({"parent_id": parent["id"]} if parent else {})}
            version = self._h("POST", f"/api/functions/{self.id}/versions", payload)
            self._target, self._base = version, None        # it is a draft now: later saves update it
        elif self._target is not None:
            # a draft chosen with load(version=N): edit it in place
            try:
                version = self._h("PUT", f"/api/versions/{self._target['id']}", body)
            except FnaiError as e:
                if e.status == 409:
                    raise FnaiError(409, f"v{self._target['number']} has been deployed since you loaded it, so it "
                                         f"can no longer be edited. Your work is still in memory and in the draft "
                                         f"file. Load it again to base a new version on it: "
                                         f"load_draft(..., version={self._target['number']}).") from None
                raise
        elif self._base is not None:
            # based on a frozen version: always a NEW version, never an overwrite
            version = self._h("POST", f"/api/functions/{self.id}/versions", {**body, "parent_id": self._base["id"]})
            self._target, self._base = version, None       # it is a draft now: later saves update it
        else:
            version = self._h("POST", f"/api/functions/{self.id}/submit", body)
        self.last_version = version
        self._fresh = False                 # saved: later plain saves update the new draft
        self._baseline = self._outline()
        self._finish_save(working, version)     # the draft file becomes this version's copy
        return version

    def deploy(self, note: str = "", *, model: Optional[str] = None,
               temperature: Optional[float] = None) -> dict:
        """Make a version live. Deploys the version from the most recent
        submit() — call submit() first if there are unsaved add_* changes.
        Pass `model` (e.g. "gpt-4o-mini") to choose what production calls run on;
        without one the space's default model is used, and with neither the deploy
        is refused."""
        if self.last_version is None:
            raise ValueError("nothing to deploy yet — call submit() first.")
        body: dict[str, Any] = {"version_id": self.last_version["id"], "note": note}
        if model:
            body["model"] = model
        if temperature is not None:
            body["temperature"] = temperature
        return self._h("POST", f"/api/functions/{self.id}/deploy", body)


class Client:
    """Namespaced management client. Auth: personal access token (fnp_*)."""

    def __init__(self, token: str, base_url: str = "http://localhost:8000",
                 timeout: float = DEFAULT_TIMEOUT_S, drafts_folder: Any = None,
                 versions_folder: Any = None, keep_versions: bool = True):
        http = _Http(base_url, token, timeout)
        http.versions_folder = versions_folder     # where a copy of every saved version goes
        http.keep_versions = keep_versions         # False: no copies, the draft file is just removed on save
        self._http = http
        self._drafts_folder = drafts_folder   # where draft files go (default .fnai/drafts)
        self.functions = _Functions(http, drafts_folder)
        self.spaces = _Spaces(http)
        self.versions = _Versions(http)
        self.eval = _Eval(http)
        self.datasets = _Datasets(http)
        self.deployments = _Deployments(http)
        self.tools = _Tools(http)
        self.keys = _Keys(http)
        self.models = _Models(http)
        self.traces = _Traces(http)
        self.auth = _Auth(http)

    def me(self) -> dict:
        return self._http("GET", "/api/me")

    def draft(self, *, name: str, slug: str | None = None, description: str = "",
              save_to: Any = None) -> "FunctionDraft":
        """Start building a function through the shared create/add_*/submit/
        deploy flow (FUNCTION_CLASS_DESIGN.md) — the same actions the canvas
        UI performs. See FunctionDraft for the full method list."""
        return FunctionDraft.create(self._http, name=name, slug=slug, description=description,
                                    save_to=save_to, drafts_folder=self._drafts_folder)

    def load_draft(self, fn_id: "int | str", *, version: int | None = None,
                   save_to: Any = None, fresh: bool = False) -> "FunctionDraft":
        """Resume an existing function through that same flow — e.g. to
        add_output() a new field after it's already deployed. Pass its id or
        its slug: c.load_draft("my_function"). By default you get the latest
        version; version=N starts from version N (a frozen one is never
        changed: saving makes a new version from it; a draft is edited in place).

        fresh=True keeps the function but starts it empty: only what you add goes into
        the new version, which is always saved as a NEW version (nothing is overwritten).
        submit() lists what was dropped or added first; fn.changes() gives the same list."""
        return FunctionDraft.load(self._http, fn_id, version=version, save_to=save_to,
                                  drafts_folder=self._drafts_folder, fresh=fresh)

    def export_yaml(self, fn_ref: "int | str", folder: Any = None) -> list[str]:
        """Write EVERY saved version of a function as YAML: v1.yaml, v2.yaml, ... Read from the server, so it
        is complete and exact, including versions made in the app or by a teammate. Pass the function's id or
        slug. Default folder: .fnai/versions/<slug>. Returns the paths written (oldest first). The server is
        the source of truth: run it again to refresh the copies."""
        fn_id = _resolve_function_id(self._http, fn_ref)
        fn = self._http("GET", f"/api/functions/{fn_id}")
        versions = sorted(self._http("GET", f"/api/functions/{fn_id}/versions") or [], key=lambda v: v["number"])
        if not versions:
            raise FnaiError(404, f"function {fn.get('slug')!r} has no versions yet")
        target = pathlib.Path(folder).expanduser() if folder else (
            _versions_dir(getattr(self._http, "versions_folder", None), self._drafts_folder) / fn["slug"])
        paths = []
        for v in versions:
            text = self._http.text("GET", f"/api/versions/{v['id']}/yaml")
            path = target / f"v{v['number']}.yaml"
            _write_atomic(path, text)
            paths.append(str(path.resolve()))
        return paths

    def load_yaml(self, path_or_name: Any, *, save_to: Any = None) -> "FunctionDraft":
        """Read a function back from a YAML file (a draft file, or a copy made
        with save_yaml) and carry on building it. Pass the file's path, or just
        the function's name to look in the drafts folder. The file is the whole
        truth: it replaces what the draft held. If the function already exists
        on the server (matched by the file's meta.function) you continue it;
        otherwise it is created. Needs PyYAML (pip install pyyaml)."""
        try:
            import yaml
        except ImportError:
            raise ImportError("Loading a YAML file needs PyYAML: pip install pyyaml "
                              "(saving YAML does not need it).") from None
        p = pathlib.Path(str(path_or_name)).expanduser()
        if not p.is_file():
            folder = _drafts_dir(self._drafts_folder)
            slug = _slugify(str(path_or_name))
            exact = folder / f"{slug}.yaml"
            # a draft is named for what it edits: <slug>.yaml, <slug>.v3.yaml, <slug>.from-v1.yaml
            matches = [exact] if exact.is_file() else sorted(folder.glob(f"{slug}.*yaml"))
            matches = [m for m in matches if m.name == f"{slug}.yaml" or re.fullmatch(
                rf"{re.escape(slug)}\.(v|from-v)\d+\.yaml", m.name)]
            if not matches:
                raise FileNotFoundError(f"no YAML file at {p} and no draft named {path_or_name!r} in {folder}")
            if len(matches) > 1:
                raise FileNotFoundError(f"{len(matches)} drafts match {path_or_name!r} in {folder}: "
                                        f"{', '.join(m.name for m in matches)} — pass the file's path")
            p = matches[0]
        doc = yaml.safe_load(p.read_text(encoding="utf-8"))
        if not isinstance(doc, dict) or not doc.get("name"):
            raise DefinitionError([f"{p}: the file must be YAML with at least a `name`"])
        slug = (doc.get("meta") or {}).get("function") or _slugify(doc["name"])
        existing = next((f for f in self.functions.list() if f["slug"] == slug), None)
        fn = existing or {"slug": slug, "name": doc["name"], "description": doc.get("description") or ""}
        draft = FunctionDraft(self._http, fn)           # not on the server yet: created at submit()
        draft._input_schema, draft._output_schema = {}, {}      # the file replaces, not merges
        draft.description = doc.get("description") or draft.description
        draft._set_draft_path(save_to, self._drafts_folder)   # the file read stays untouched
        draft._loading = True
        try:
            for side, add in (("input", draft.add_input), ("output", draft.add_output)):
                fields = doc.get(side) or {}
                draft._field_order[side] = list(fields)
                for name, spec in fields.items():
                    spec = dict(spec or {})
                    if side == "input" and spec.get("derived"):
                        # filled by a preprocessor: not a caller input, so not in the stored schema
                        draft._field_extras[side][name] = {"flags": {}, "full": spec}
                        continue
                    add(name, **spec)
            impl = doc.get("implementation") or {}
            draft._extra_top = {k: v for k, v in doc.items() if k not in _MANAGED_TOP}
            draft._extra_impl = {k: v for k, v in impl.items() if k not in _MANAGED_IMPL}
            if (impl.get("execution") or {}).get("mode") == "agent":
                draft.set_mode("agent")
            for step in impl.get("flow") or []:
                draft.add_step(step)
            sections = impl.get("sections") or {}
            if sections.get("role"):
                draft.add_prompt(sections["role"])
            for kind in ("definitions", "constraints", "edge_cases"):
                for text in sections.get(kind) or []:
                    draft.add_section(kind, text)
            for ex in sections.get("examples") or []:
                draft.add_example(**ex)
            for t in impl.get("tools") or []:
                if "server" in t:
                    draft._tools.append(dict(t))       # expanded by the platform; kept as written
                    continue
                cfg = t.get("config") or {}
                clash = set(cfg) & {"name", "max_calls", "requires", "effect", "ref"}
                if clash:
                    raise DefinitionError([f"tool '{t.get('name')}': config keys {sorted(clash)} clash with tool options"])
                draft.add_tool(t["name"], max_calls=t.get("max_calls"), requires=t.get("requires"),
                               effect=t.get("effect"), ref=t.get("ref"), **cfg)
            for sentence in doc.get("rubric") or []:
                draft.add_rubric(sentence)
            if doc.get("memory"):
                draft.set_memory(**doc["memory"])
        finally:
            draft._loading = False
        # a draft made with load_draft(version=N) remembers which version it grew from, so a stopped
        # script can't turn into an accidental overwrite of another draft
        meta = doc.get("meta") or {}
        number = meta.get("editing") or meta.get("based_on") or meta.get("version")
        if draft.id is not None and isinstance(number, int) and not isinstance(number, bool):
            versions = self._http("GET", f"/api/functions/{draft.id}/versions") or []
            found = next((v for v in versions if v["number"] == number), None)
            if found is None:
                raise FnaiError(404, f"{p}: it was based on v{number}, which no longer exists on the server.")
            draft._point_at(found)          # a draft that has been deployed since is now frozen: new version
        draft._autosave()
        return draft

    def use_space(self, name: str | None) -> None:
        """Scope every later call to one space by name (the `X-Space` header
        get_scope reads — see
        backend/app/auth_usermanagement/security/scope.py), instead of
        whatever this token's default space resolves to. `name` is a
        space's `name` from me()["spaces"], not its id.

        Opt-in: if you never call this, nothing changes — no X-Space header
        is ever sent, same as before this method existed, and the backend
        resolves its own default space exactly as it always has. Use this
        only when an account has more than one space and you need to target
        a specific one.

        Delegates to _Http.use_space(), which re-validates `name` against a
        FRESH GET /api/me every time — raising SpaceNotFoundError rather
        than accepting a name nobody checked. None clears the override."""
        self._http.use_space(name)

    @property
    def current_space(self) -> str | None:
        """The space name Client.use_space() last set, or None if nothing
        has been selected (the default state — see use_space())."""
        return self._http.space

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
