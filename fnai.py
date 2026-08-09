"""fnai — minimal Python client for functionalAI's public API.

Zero dependencies (stdlib only). Wraps exactly the public surface:
  GET  /v1/run/{slug}/contract   — what the function accepts and returns
  POST /v1/run/{slug}            — execute the active deployment

Usage:
    from fnai import Function

    fn = Function("doc_summarizer", api_key="fnk_…",
                  base_url="http://localhost:8000")
    print(fn.contract()["input_schema"])          # discover the shape
    result = fn.run(document="…long text…")       # invoke
    if result.ok:
        print(result.output, "cost:", result.meta["cost_usd"])
    else:
        print("failed:", result.error)

Errors: FnaiError carries .status and .detail for API-level failures
(bad key → 401, unknown/undeployed function → 404, invalid input → 422,
timeout → 504). An EXECUTION failure is not an exception — the platform
returns 200 with status="failed"; check result.ok.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

__version__ = "0.1.0"
DEFAULT_TIMEOUT_S = 90  # server caps execution at 60s; headroom for transport


class FnaiError(Exception):
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(f"HTTP {status}: {detail}")


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
    """A handle on one deployed function."""

    def __init__(self, slug: str, api_key: str, base_url: str = "http://localhost:8000",
                 timeout: float = DEFAULT_TIMEOUT_S):
        self.slug = slug
        self._key = api_key
        self._base = base_url.rstrip("/")
        self._timeout = timeout

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(
            f"{self._base}{path}", method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {self._key}",
                     "Content-Type": "application/json",
                     "User-Agent": f"fnai-python/{__version__}"})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read()).get("detail", e.reason)
            except Exception:
                detail = str(e.reason)
            raise FnaiError(e.code, detail) from None
        except urllib.error.URLError as e:
            raise FnaiError(0, f"cannot reach {self._base}: {e.reason}") from None

    def contract(self) -> dict:
        """The ACTIVE deployment's contract: input/output schemas + shapes.
        Changes atomically with deploys and rollbacks."""
        return self._request("GET", f"/v1/run/{self.slug}/contract")

    def run(self, **input: Any) -> RunResult:
        """Invoke the active deployment. Keyword arguments become the input
        object, validated server-side against the deployed input schema."""
        body = self._request("POST", f"/v1/run/{self.slug}", {"input": input})
        return RunResult(output=body.get("output"), status=body.get("status", "failed"),
                         meta=body.get("meta", {}), error=body.get("error"))
