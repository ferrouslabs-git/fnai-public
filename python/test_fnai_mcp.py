"""Stdlib-only tests for fnai_mcp's tools, against a fake Client double —
matching the SDK's own zero-dependency test philosophy. No real MCP protocol
or network call involved: the tool functions are plain callables (the
@server.tool() decorator registers and returns them unchanged), so they're
exercised directly."""
import os
import unittest
from unittest.mock import patch

import fnai_mcp
from fnai import FnaiError
from mcp.server.mcpserver.exceptions import ToolError


class _FakeFunctions:
    def __init__(self, functions, versions_by_fn):
        self._functions = functions
        self._versions_by_fn = versions_by_fn
        self.create_calls = []
        self.create_version_calls = []

    def list(self):
        return self._functions

    def get(self, fn_id):
        return next(f for f in self._functions if f["id"] == fn_id)

    def versions(self, fn_id):
        return self._versions_by_fn[fn_id]

    def create(self, **body):
        self.create_calls.append(body)
        return {"id": 99, **body}

    def create_version(self, fn_id, **body):
        self.create_version_calls.append((fn_id, body))
        return {"id": 20, "number": 1, **body}


class _FakeVersions:
    def __init__(self, versions_by_id):
        self._versions_by_id = versions_by_id
        self.update_calls = []
        self.playground_calls = []

    def get(self, v_id):
        return self._versions_by_id[v_id]

    def update(self, v_id, **body):
        self.update_calls.append((v_id, body))
        return {"id": v_id, **body}

    def playground(self, v_id, **body):
        self.playground_calls.append((v_id, body))
        return {"output": {"answer": "42"}, "trace": [], "cost_usd": 0.001}


class _FakeDatasets:
    def __init__(self, existing=None):
        self._by_fn = {}
        for d in existing or []:
            self._by_fn.setdefault(d["function_id"], []).append(d)
        self.create_calls = []
        self.add_row_calls = []

    def list(self, fn_id):
        return self._by_fn.get(fn_id, [])

    def create(self, fn_id, **body):
        self.create_calls.append((fn_id, body))
        row = {"id": 30, "function_id": fn_id, **body}
        self._by_fn.setdefault(fn_id, []).append(row)
        return row

    def add_row(self, ds_id, **body):
        self.add_row_calls.append((ds_id, body))
        return {"id": 40, "dataset_id": ds_id, **body}


class _FakeScores:
    def __init__(self):
        self.create_calls = []

    def create(self, fn_id, **body):
        self.create_calls.append((fn_id, body))
        return {"id": 50, "function_id": fn_id, **body}


class _FakeEval:
    def __init__(self):
        self.scores = _FakeScores()


class _FakeClient:
    def __init__(self, me, functions, versions_by_fn, versions_by_id, datasets=None):
        self._me = me
        self.functions = _FakeFunctions(functions, versions_by_fn)
        self.versions = _FakeVersions(versions_by_id)
        self.datasets = _FakeDatasets(datasets)
        self.eval = _FakeEval()

    def me(self):
        return self._me


ME = {
    "user_id": "u1", "roles": ["space_member"],
    "permissions": ["functions.read", "functions.write"],
    "account_id": "a1", "space_id": "s1",
    "spaces": [{"id": "s1", "name": "Support Team"}],
}
FUNCTIONS = [
    {"id": 1, "slug": "customer-support", "name": "Customer Support",
     "description": "Answers support questions", "archived": False, "extra_field": "should not leak"},
]
VERSIONS_BY_FN = {1: [{"id": 10, "number": 6, "status": "released", "message": "initial", "created_at": "t0"}]}
VERSIONS_BY_ID = {
    10: {"id": 10, "number": 6, "snapshot": {"prompt": "Answer questions."}},
    11: {"id": 11, "number": 7, "snapshot": {"prompt": "Answer questions, including refunds."}},
}


def _fake_client():
    return _FakeClient(ME, FUNCTIONS, VERSIONS_BY_FN, VERSIONS_BY_ID)


class ToolsAgainstFakeClient(unittest.TestCase):
    def setUp(self):
        self.client = _fake_client()
        patcher = patch("fnai_mcp.get_client", lambda: self.client)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_who_am_i_reports_identity_and_permissions_only(self):
        result = fnai_mcp.who_am_i()
        self.assertEqual(result, {
            "user_id": "u1", "roles": ["space_member"],
            "permissions": ["functions.read", "functions.write"],
            "account_id": "a1", "space_id": "s1",
        })

    def test_list_spaces(self):
        self.assertEqual(fnai_mcp.list_spaces(), [{"id": "s1", "name": "Support Team"}])

    def test_get_my_permissions(self):
        self.assertEqual(fnai_mcp.get_my_permissions(), ["functions.read", "functions.write"])

    def test_list_functions_is_summarized(self):
        result = fnai_mcp.list_functions()
        self.assertEqual(result, [{
            "id": 1, "slug": "customer-support", "name": "Customer Support",
            "description": "Answers support questions", "archived": False,
        }])
        self.assertNotIn("extra_field", result[0])

    def test_get_function_returns_full_detail(self):
        self.assertEqual(fnai_mcp.get_function(1)["extra_field"], "should not leak")

    def test_list_versions_is_summarized(self):
        result = fnai_mcp.list_versions(1)
        self.assertEqual(result, [{
            "id": 10, "number": 6, "status": "released",
            "message": "initial", "created_at": "t0",
        }])

    def test_get_version_returns_full_detail(self):
        self.assertEqual(fnai_mcp.get_version(10)["snapshot"], {"prompt": "Answer questions."})

    def test_diff_versions_shows_the_change(self):
        diff = fnai_mcp.diff_versions(10, 11)
        self.assertIn("-  \"prompt\": \"Answer questions.\"", diff)
        self.assertIn("+  \"prompt\": \"Answer questions, including refunds.\"", diff)

    def test_diff_versions_no_differences(self):
        self.assertEqual(fnai_mcp.diff_versions(10, 10), "no differences")

    def test_create_function_forwards_fields(self):
        result = fnai_mcp.create_function("new-fn", "New Fn", description="d")
        self.assertEqual(result["slug"], "new-fn")
        client = self.client
        self.assertEqual(client.functions.create_calls, [{
            "slug": "new-fn", "name": "New Fn", "description": "d",
            "input_schema": {}, "output_schema": {},
        }])

    def test_create_draft_forwards_snapshot_and_optional_fields(self):
        result = fnai_mcp.create_draft(1, {"prompt": "hi"}, message="m", parent_id=10)
        self.assertEqual(result["snapshot"], {"prompt": "hi"})
        client = self.client
        self.assertEqual(client.functions.create_version_calls, [(1, {
            "snapshot": {"prompt": "hi"}, "message": "m",
            "input_schema": None, "output_schema": None, "parent_id": 10,
        })])

    def test_update_draft_overwrites_in_place(self):
        result = fnai_mcp.update_draft(10, {"prompt": "v2"}, message="m")
        self.assertEqual(result["snapshot"], {"prompt": "v2"})
        client = self.client
        self.assertEqual(client.versions.update_calls, [(10, {
            "snapshot": {"prompt": "v2"}, "message": "m",
            "input_schema": None, "output_schema": None,
        })])

    def test_run_trial_returns_output_trace_cost(self):
        result = fnai_mcp.run_trial(10, {"q": "hi"})
        self.assertEqual(result, {"output": {"answer": "42"}, "trace": [], "cost_usd": 0.001})
        client = self.client
        self.assertEqual(client.versions.playground_calls, [(10, {
            "input": {"q": "hi"}, "model": "mock-small", "temperature": 0.2,
        })])

    def test_create_dataset_creates_a_new_one(self):
        result = fnai_mcp.create_dataset(1, "evals")
        self.assertEqual(result["name"], "evals")
        client = self.client
        self.assertEqual(client.datasets.create_calls, [(1, {"name": "evals"})])

    def test_create_dataset_is_idempotent_on_retry(self):
        fnai_mcp.create_dataset(1, "evals")
        client = self.client
        result = fnai_mcp.create_dataset(1, "evals")
        self.assertEqual(result["id"], 30)
        self.assertEqual(len(client.datasets.create_calls), 1)  # not called again

    def test_add_dataset_row_appends(self):
        result = fnai_mcp.add_dataset_row(30, {"q": "hi"}, expected={"a": "42"}, tags=["gold"])
        self.assertEqual(result["dataset_id"], 30)
        client = self.client
        self.assertEqual(client.datasets.add_row_calls, [(30, {
            "input": {"q": "hi"}, "expected": {"a": "42"}, "tags": ["gold"], "source": "manual",
        })])

    def test_create_check_forwards_fields(self):
        result = fnai_mcp.create_check(1, "no-profanity", {"regex": "..."}, tier="det")
        self.assertEqual(result["function_id"], 1)
        client = self.client
        self.assertEqual(client.eval.scores.create_calls, [(1, {
            "name": "no-profanity", "tier": "det", "config": {"regex": "..."},
        })])


class _RaisingClient:
    """A fake Client whose every call raises FnaiError, like a real 403 from
    the backend — proves the tool surfaces it as an actionable ToolError
    instead of letting the MCP framework sanitize it to a generic
    "Error executing tool X"."""
    def me(self):
        raise FnaiError(403, "missing permission 'functions.read'")


class ActionableErrors(unittest.TestCase):
    def setUp(self):
        patcher = patch("fnai_mcp.get_client", lambda: _RaisingClient())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_fnai_error_becomes_an_actionable_tool_error(self):
        with self.assertRaises(ToolError) as ctx:
            fnai_mcp.who_am_i()
        self.assertIn("403", str(ctx.exception))
        self.assertIn("missing permission 'functions.read'", str(ctx.exception))


class GetClientFailsFast(unittest.TestCase):
    def setUp(self):
        fnai_mcp._client = None
        self._old_token = os.environ.pop("FNAI_TOKEN", None)

    def tearDown(self):
        fnai_mcp._client = None
        if self._old_token is not None:
            os.environ["FNAI_TOKEN"] = self._old_token

    def test_missing_token_raises_actionable_error(self):
        with self.assertRaises(RuntimeError) as ctx:
            fnai_mcp.get_client()
        self.assertIn("FNAI_TOKEN is not set", str(ctx.exception))
        self.assertIn("API tokens", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
