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
    c.versions.submit(v["id"]); c.versions.approve(v["id"])
    c.deployments.deploy(fn["id"], v["id"])
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
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

__version__ = "0.2.0"
DEFAULT_TIMEOUT_S = 90  # server caps execution at 60s; headroom for transport


class FnaiError(Exception):
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(f"HTTP {status}: {detail}")


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

    def create_version(self, fn_id: int, *, snapshot: dict, message: str = "") -> dict:
        return self._h("POST", f"/api/functions/{fn_id}/versions",
                       {"snapshot": snapshot, "message": message})

    def health(self, fn_id: int, window_hours: int = 24) -> dict:
        return self._h("GET", f"/api/functions/{fn_id}/health",
                       query={"window_hours": window_hours})


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

    def submit(self, v_id: int) -> dict:
        return self._h("POST", f"/api/versions/{v_id}/submit", {})

    def approve(self, v_id: int, comment: str = "") -> dict:
        return self._h("POST", f"/api/versions/{v_id}/approve", {"comment": comment})

    def reject(self, v_id: int, comment: str = "") -> dict:
        return self._h("POST", f"/api/versions/{v_id}/reject", {"comment": comment})

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

    def add(self, *, provider: str, secret: dict, label: str = "",
            scope_level: str = "org", **extra: Any) -> dict:
        return self._h("POST", "/api/provider-keys",
                       {"provider": provider, "secret": secret, "label": label,
                        "scope_level": scope_level, **extra})

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
        self.versions = _Versions(http)
        self.eval = _Eval(http)
        self.datasets = _Datasets(http)
        self.deployments = _Deployments(http)
        self.keys = _Keys(http)
        self.traces = _Traces(http)
        self.auth = _Auth(http)

    def me(self) -> dict:
        return self._http("GET", "/api/me")

    def function(self, slug: str, api_key: str) -> Function:
        """Cross over to the runtime surface: a Function handle on this base URL."""
        return Function(slug, api_key=api_key, base_url=self._http._base,
                        timeout=self._http._timeout)
