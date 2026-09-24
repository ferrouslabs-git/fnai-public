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
        _require_id(fn_id, "fn_id")
        return self._h("GET", f"/api/functions/{fn_id}")

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
        return self._h("POST", "/api/functions",
                       {"slug": slug, "name": name, "input_schema": input_schema,
                        "output_schema": output_schema, **extra})

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

    def run(self, exp_id: int) -> dict:
        _require_id(exp_id, "exp_id")
        return self._h("POST", f"/api/experiments/{exp_id}/run", {})

    def clone(self, exp_id: int) -> dict:
        _require_id(exp_id, "exp_id")
        return self._h("POST", f"/api/experiments/{exp_id}/clone", {})

    def delete(self, exp_id: int) -> None:
        _require_id(exp_id, "exp_id")
        self._h("DELETE", f"/api/experiments/{exp_id}")

    def findings(self, exp_id: int) -> list[dict]:
        _require_id(exp_id, "exp_id")
        return self._h("GET", f"/api/experiments/{exp_id}/findings")


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

    def snapshot(self, ds_id: int) -> dict:
        _require_id(ds_id, "ds_id")
        return self._h("POST", f"/api/datasets/{ds_id}/snapshot", {})

    def snapshots(self, ds_id: int) -> list[dict]:
        _require_id(ds_id, "ds_id")
        return self._h("GET", f"/api/datasets/{ds_id}/snapshots")


class _Deployments:
    def __init__(self, http: _Http):
        self._h = http

    def deploy(self, fn_id: int, version_id: int) -> dict:
        _require_id(fn_id, "fn_id")
        _require_id(version_id, "version_id")
        return self._h("POST", f"/api/functions/{fn_id}/deploy",
                       {"version_id": version_id})

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
