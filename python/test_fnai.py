"""Stdlib-only tests for parse_prompt_file / create_function_from_doc.
No network involved — create_function_from_doc is tested against a fake
client double, matching the SDK's own zero-dependency philosophy."""
import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path
import os

# keep draft files from tests out of the working folder
os.environ["FNAI_DRAFTS_DIR"] = tempfile.mkdtemp(prefix="fnai-test-drafts-")

from fnai import (Client, DefinitionError, FnaiError, Function, FunctionDraft, PromptFileError, STARTER_YAML,
                  SpaceNotFoundError, _Datasets, _Functions, _Scores, _Spaces,
                  create_function, create_function_from_doc, parse_prompt_file,
                  write_starter_yaml)


def _write(tmp: Path, name: str, text: str) -> str:
    p = tmp / name
    p.write_text(text)
    return str(p)


class ParsePromptFile(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_parses_a_full_json_document(self):
        path = _write(self.tmp, "p.json", json.dumps({
            "role": "You are a helpful assistant.",
            "instructions": ["Do the first thing.", "Do the second thing."],
            "inputs": {"website_url": "string"},
            "outputs": {"confidence": {"type": "number", "required": False}},
        }))
        doc = parse_prompt_file(path)
        self.assertEqual(doc["sections"], [
            {"type": "role", "content": "You are a helpful assistant."},
            {"type": "instructions", "title": "Instructions",
             "items": ["Do the first thing.", "Do the second thing."]},
        ])
        self.assertEqual(doc["input_schema"], {
            "type": "object", "required": ["website_url"],
            "properties": {"website_url": {"type": "string"}},
        })
        self.assertEqual(doc["output_schema"], {
            "type": "object", "required": [],
            "properties": {"confidence": {"type": "number"}},
        })

    def test_missing_role_raises(self):
        path = _write(self.tmp, "p.json", json.dumps({"instructions": ["x"]}))
        with self.assertRaises(PromptFileError) as ctx:
            parse_prompt_file(path)
        self.assertIn("role is required", str(ctx.exception))

    def test_unknown_top_level_key_names_it(self):
        path = _write(self.tmp, "p.json", json.dumps({"role": "hi", "output_format": []}))
        with self.assertRaises(PromptFileError) as ctx:
            parse_prompt_file(path)
        self.assertIn("output_format", str(ctx.exception))

    def test_bad_field_type_names_it(self):
        path = _write(self.tmp, "p.json", json.dumps({"role": "hi", "inputs": {"a": "banana"}}))
        with self.assertRaises(PromptFileError) as ctx:
            parse_prompt_file(path)
        self.assertIn("inputs.a", str(ctx.exception))

    def test_missing_file_raises(self):
        with self.assertRaises(PromptFileError):
            parse_prompt_file(str(self.tmp / "nope.json"))

    def test_omits_schemas_entirely_when_inputs_outputs_absent(self):
        path = _write(self.tmp, "p.json", json.dumps({"role": "hi"}))
        doc = parse_prompt_file(path)
        self.assertNotIn("input_schema", doc)
        self.assertNotIn("output_schema", doc)

    def test_parses_real_yaml_when_pyyaml_available(self):
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("PyYAML not installed")
        path = _write(self.tmp, "p.yaml", "role: hi\ninputs:\n  a: string\n")
        doc = parse_prompt_file(path)
        self.assertEqual(doc["input_schema"]["required"], ["a"])


class _FakeFunctions:
    def __init__(self):
        self.create_calls = []
        self.create_version_calls = []

    def create(self, **kwargs):
        self.create_calls.append(kwargs)
        return {"id": 60, "slug": kwargs["slug"]}

    def create_version(self, fn_id, **kwargs):
        self.create_version_calls.append((fn_id, kwargs))
        return {"id": 200, "number": 1}


class _FakeClient:
    def __init__(self):
        self.functions = _FakeFunctions()


class CreateFunctionFromDoc(unittest.TestCase):
    def test_never_touches_disk_only_the_client(self):
        client = _FakeClient()
        doc = {"sections": [{"type": "role", "content": "hi"}],
               "input_schema": {"type": "object", "properties": {"a": {"type": "string"}}}}
        fn, v = create_function_from_doc(client, slug="s", name="N", doc=doc)
        self.assertEqual(fn["id"], 60)
        self.assertEqual(v["id"], 200)
        call = client.functions.create_calls[0]
        self.assertEqual(call["slug"], "s")
        self.assertEqual(call["input_schema"], doc["input_schema"])
        self.assertEqual(call["output_schema"], {"type": "object", "properties": {}})  # defaulted
        vc_fn_id, vc_kwargs = client.functions.create_version_calls[0]
        self.assertEqual(vc_fn_id, 60)
        self.assertEqual(vc_kwargs["snapshot"]["sections"], doc["sections"])


class CreateFunctionManual(unittest.TestCase):
    def test_takes_sections_and_schemas_directly_no_doc_no_file(self):
        client = _FakeClient()
        sections = [{"type": "role", "content": "hi"}]
        in_schema = {"type": "object", "properties": {"x": {"type": "string"}}}
        fn, v = create_function(client, slug="manual", name="Manual",
                                sections=sections, input_schema=in_schema)
        self.assertEqual(fn["id"], 60)
        call = client.functions.create_calls[0]
        self.assertEqual(call["input_schema"], in_schema)
        self.assertEqual(call["output_schema"], {"type": "object", "properties": {}})
        self.assertEqual(client.functions.create_version_calls[0][1]["snapshot"]["sections"], sections)


class _FakeV2Http:
    """A double for _Http covering both call shapes create_from_yaml_text/
    get_yaml use — JSON (__call__) and raw text (.text) — with no real
    network, matching this SDK's own stdlib-only test philosophy."""

    def __init__(self, existing_functions=None, push_response=None, get_response="",
                versions=None):
        self.existing_functions = existing_functions or []
        self.push_response = push_response or {"id": 200, "number": 2, "artifact_hash": "abc"}
        self.get_response = get_response
        self.versions = versions if versions is not None else []
        self.calls: list[tuple] = []

    def __call__(self, method, path, body=None, query=None):
        self.calls.append(("json", method, path, body))
        if method == "GET" and path == "/api/functions":
            return self.existing_functions
        if method == "POST" and path == "/api/functions":
            return {"id": 99, "slug": body["slug"], "name": body["name"]}
        if method == "GET" and path.endswith("/versions"):
            return self.versions
        raise AssertionError(f"unexpected JSON call: {method} {path}")

    def text(self, method, path, *, data=None, content_type="text/yaml"):
        self.calls.append(("text", method, path, data))
        if method == "POST" and path.endswith("/versions/yaml"):
            return json.dumps(self.push_response)
        if method == "GET" and path.endswith("/yaml"):
            return self.get_response
        raise AssertionError(f"unexpected text call: {method} {path}")


class CreateFromYaml(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_creates_a_new_function_when_the_slug_does_not_exist_yet(self):
        http = _FakeV2Http()
        fns = _Functions(http)
        text = "name: Summarize Support Ticket\ninput: {}\noutput: {y: {type: string}}\n"
        fn, v = fns.create_from_yaml_text(text)
        self.assertEqual(fn["slug"], "summarize_support_ticket")
        self.assertEqual(v["id"], 200)
        push = next(c for c in http.calls if c[0] == "text")
        self.assertEqual(push[3], text.encode("utf-8"))

    def test_reuses_an_existing_function_with_the_same_slug(self):
        http = _FakeV2Http(existing_functions=[{"id": 5, "slug": "summarize_support_ticket"}])
        fns = _Functions(http)
        fn, _v = fns.create_from_yaml_text("name: Summarize Support Ticket\n")
        self.assertEqual(fn["id"], 5)
        create_calls = [c for c in http.calls if c[1] == "POST" and c[2] == "/api/functions"]
        self.assertEqual(create_calls, [])   # never re-created

    def test_missing_name_raises_before_any_call(self):
        http = _FakeV2Http()
        fns = _Functions(http)
        with self.assertRaises(PromptFileError):
            fns.create_from_yaml_text("input: {}\n")
        self.assertEqual(http.calls, [])

    def test_reimporting_unchanged_content_is_a_noop(self):
        """Re-pushing the exact same document (a fresh copy of what's
        already live) must not mint a duplicate version."""
        current_text = "name: Summarize Support Ticket\nmeta: {version: 3}\n"
        http = _FakeV2Http(
            existing_functions=[{"id": 5, "slug": "summarize_support_ticket"}],
            versions=[{"id": 300, "number": 3}],
            get_response=current_text,
        )
        fns = _Functions(http)
        # a hand-edited copy with NO meta block at all — still "the same
        # document" once meta is ignored on both sides
        fn, v = fns.create_from_yaml_text("name: Summarize Support Ticket\n")
        self.assertEqual(fn["id"], 5)
        self.assertEqual(v["id"], 300)
        push_calls = [c for c in http.calls if c[0] == "text" and c[2].endswith("/versions/yaml")]
        self.assertEqual(push_calls, [])   # no new version minted

    def test_reimporting_edited_content_still_creates_a_new_version(self):
        """A genuine change must still push, exactly as before this
        no-op check existed."""
        current_text = "name: Summarize Support Ticket\nmeta: {version: 3}\n"
        http = _FakeV2Http(
            existing_functions=[{"id": 5, "slug": "summarize_support_ticket"}],
            versions=[{"id": 300, "number": 3}],
            get_response=current_text,
            push_response={"id": 301, "number": 4},
        )
        fns = _Functions(http)
        edited_text = "name: Summarize Support Ticket\ndescription: now with more detail\n"
        fn, v = fns.create_from_yaml_text(edited_text)
        self.assertEqual(fn["id"], 5)
        self.assertEqual(v["id"], 301)
        push = next(c for c in http.calls if c[0] == "text" and c[2].endswith("/versions/yaml"))
        self.assertEqual(push[3], edited_text.encode("utf-8"))

    def test_create_from_yaml_reads_the_file_from_disk(self):
        path = _write(self.tmp, "f.yaml", "name: X\n")
        http = _FakeV2Http()
        fns = _Functions(http)
        fn, _v = fns.create_from_yaml(path)
        self.assertEqual(fn["slug"], "x")

    def test_get_yaml_pulls_the_latest_version(self):
        http = _FakeV2Http(get_response="name: X\nmeta: {version: 3}\n")

        class _FnsWithVersions(_Functions):
            def versions(self, fn_id):
                return [{"id": 1, "number": 1}, {"id": 3, "number": 3}, {"id": 2, "number": 2}]

        fns = _FnsWithVersions(http)
        text = fns.get_yaml(42)
        self.assertIn("version: 3", text)
        get_call = next(c for c in http.calls if c[0] == "text")
        self.assertEqual(get_call[2], "/api/versions/3/yaml")   # picked the highest number, not the last listed

    def test_get_yaml_with_no_versions_raises(self):
        http = _FakeV2Http()

        class _FnsNoVersions(_Functions):
            def versions(self, fn_id):
                return []

        with self.assertRaises(FnaiError):
            _FnsNoVersions(http).get_yaml(1)

    def test_full_format_golden_fixture_is_posted_verbatim(self):
        """REQ-273: one document using every part of the format is sent to the
        backend as the file's exact bytes — this SDK never re-shapes it."""
        fixture = Path(__file__).resolve().parent.parent / "fixtures" / "full-function.yaml"
        text = fixture.read_text(encoding="utf-8")
        http = _FakeV2Http()
        fns = _Functions(http)
        fn, v = fns.create_from_yaml_text(text)
        self.assertEqual(fn["slug"], "summarize_uploaded_contract")
        self.assertEqual(v["id"], 200)
        push = next(c for c in http.calls if c[0] == "text")
        self.assertEqual(push[3], text.encode("utf-8"))
        # the body still contains every part of the format (tools fences, examples, …)
        body = push[3].decode("utf-8")
        self.assertIn("max_calls: 12", body)
        self.assertIn("polarity: positive", body)
        self.assertIn("type: file", body)
        self.assertIn("per: caller", body)


class _FakeMeHttp:
    """A double for _Http covering GET /api/me plus whatever create-style
    call a test exercises — enough for the input-validation and use_space
    tests below without any real network."""

    def __init__(self, spaces=None, existing=None):
        self.spaces = spaces if spaces is not None else [{"id": "s1", "name": "Marketing"}]
        self.existing = existing or []   # pre-existing functions/datasets/etc, by test
        self.calls: list[tuple] = []
        self.space: str | None = None   # mirrors _Http.space for header-sent assertions

    def __call__(self, method, path, body=None, query=None):
        self.calls.append((method, path, body))
        if method == "GET" and path == "/api/me":
            return {"account_id": "a1", "spaces": self.spaces}
        if method == "GET" and path in ("/api/functions", "/api/datasets"):
            return self.existing
        if method == "POST":
            return {"id": 1, **(body or {})}
        raise AssertionError(f"unexpected call: {method} {path}")


class InputValidation(unittest.TestCase):
    """REQ (this session): every SDK method that takes a required field,
    an id, or a body validates it client-side with a clear ValueError
    before any request goes out — not just create_from_yaml_text, which
    already had its own PromptFileError checks."""

    def test_spaces_create_rejects_empty_name(self):
        with self.assertRaises(ValueError):
            _Spaces(_FakeMeHttp()).create(name="")

    def test_spaces_create_rejects_a_name_that_already_exists(self):
        http = _FakeMeHttp(spaces=[{"id": "s1", "name": "Marketing"}])
        with self.assertRaises(ValueError):
            _Spaces(http).create(name="Marketing")
        # never reached the actual create call
        self.assertNotIn(("POST", "/api/spaces", {"name": "Marketing"}), http.calls)

    def test_spaces_create_succeeds_for_a_new_name(self):
        http = _FakeMeHttp(spaces=[{"id": "s1", "name": "Marketing"}])
        _Spaces(http).create(name="Sales")
        self.assertIn(("POST", "/api/spaces", {"name": "Sales"}), http.calls)

    def test_functions_create_rejects_empty_slug(self):
        with self.assertRaises(ValueError):
            _Functions(_FakeMeHttp()).create(slug="", name="X",
                                             input_schema={}, output_schema={})

    def test_functions_create_rejects_a_duplicate_slug(self):
        http = _FakeMeHttp(existing=[{"id": 5, "slug": "existing", "name": "Existing"}])
        with self.assertRaises(ValueError):
            _Functions(http).create(slug="existing", name="X",
                                    input_schema={}, output_schema={})

    def test_functions_get_rejects_a_non_positive_id(self):
        with self.assertRaises(ValueError):
            _Functions(_FakeMeHttp()).get(0)

    def test_functions_get_rejects_a_bool_id(self):
        # bool is a subclass of int in Python — must not sneak past as an id
        with self.assertRaises(ValueError):
            _Functions(_FakeMeHttp()).get(True)

    def test_functions_update_rejects_no_fields(self):
        with self.assertRaises(ValueError):
            _Functions(_FakeMeHttp()).update(1)

    def test_datasets_create_rejects_a_row_missing_input(self):
        with self.assertRaises(ValueError):
            _Datasets(_FakeMeHttp()).create(1, name="ds", rows=[{"no_input": 1}])

    def test_datasets_add_row_rejects_missing_input(self):
        with self.assertRaises(ValueError):
            _Datasets(_FakeMeHttp()).add_row(1, expected={"x": 1})

    def test_scores_create_rejects_missing_config(self):
        with self.assertRaises(ValueError):
            _Scores(_FakeMeHttp()).create(1, name="x")

    def test_scores_create_rejects_missing_name(self):
        with self.assertRaises(ValueError):
            _Scores(_FakeMeHttp()).create(1, config={})

    def test_scores_set_gate_rejects_non_bool(self):
        with self.assertRaises(ValueError):
            _Scores(_FakeMeHttp()).set_gate(1, "yes")


class UseSpace(unittest.TestCase):
    """Client.use_space() is opt-in and additive: a Client that never calls
    it behaves exactly as before this feature existed (no X-Space header,
    same as every existing caller/test already relies on)."""

    def test_never_calling_use_space_sends_no_header(self):
        client = Client(token="fnp_x", base_url="http://fake.local")
        self.assertIsNone(client.current_space)
        self.assertNotIn("X-Space", client._http._headers("application/json"))

    def test_after_use_space_the_header_is_sent(self):
        client = Client(token="fnp_x", base_url="http://fake.local")
        fake_me = lambda self, method, path, body=None, query=None: {  # noqa: E731
            "account_id": "a1", "spaces": [{"id": "s1", "name": "Marketing"}]}
        with unittest.mock.patch("fnai._Http.__call__", fake_me):
            client.use_space("Marketing")
        self.assertEqual(client._http._headers("application/json")["X-Space"], "Marketing")

    def test_use_space_rejects_an_unknown_name(self):
        client = Client(token="fnp_x", base_url="http://fake.local")
        fake_me = lambda self, method, path, body=None, query=None: {  # noqa: E731
            "account_id": "a1", "spaces": [{"id": "s1", "name": "Marketing"}]}
        with unittest.mock.patch("fnai._Http.__call__", fake_me):
            with self.assertRaises(SpaceNotFoundError):
                client.use_space("Sales")
        self.assertIsNone(client.current_space)

    def test_use_space_accepts_a_real_name(self):
        client = Client(token="fnp_x", base_url="http://fake.local")
        fake_me = lambda self, method, path, body=None, query=None: {  # noqa: E731
            "account_id": "a1", "spaces": [{"id": "s1", "name": "Marketing"}]}
        with unittest.mock.patch("fnai._Http.__call__", fake_me):
            client.use_space("Marketing")
        self.assertEqual(client.current_space, "Marketing")

    def test_space_property_has_no_setter(self):
        client = Client(token="fnp_x", base_url="http://fake.local")
        with self.assertRaises(AttributeError):
            client._http.space = "sneaky"


class WriteStarterYaml(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_writes_the_starter_example_and_it_parses_clean(self):
        path = write_starter_yaml(str(self.tmp / "function.yaml"))
        self.assertEqual(Path(path).read_text(), STARTER_YAML)
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed")
        doc = yaml.safe_load(Path(path).read_text())
        self.assertEqual(doc["name"], "Summarize Support Ticket")
        self.assertIn("ticket_text", doc["input"])

    def test_refuses_to_overwrite_an_existing_file(self):
        p = self.tmp / "function.yaml"
        p.write_text("name: already here\n")
        with self.assertRaises(FileExistsError):
            write_starter_yaml(str(p))


class _FakeDraftHttp:
    """A double for _Http covering the calls FunctionDraft makes: create,
    load, submit, deploy. No real network, matching this SDK's stdlib-only
    test philosophy."""

    def __init__(self, fn=None, versions=None):
        self.fn = fn or {"id": 60, "slug": "invoice_extractor", "name": "Invoice Extractor",
                         "input_schema": {}, "output_schema": {}}
        self.versions = versions if versions is not None else []
        self.calls: list[tuple] = []
        self._next_version_id = 500

    def text(self, method, path, *, data=None, content_type="text/yaml"):
        self.calls.append((method, path, None))
        vid = int(path.split("/")[3])
        v = next(x for x in self.versions if x["id"] == vid)
        return f"meta:\n  function: {self.fn['slug']}\n  version: {v['number']}\nname: from the server v{v['number']}\n"

    def __call__(self, method, path, body=None, query=None):
        self.calls.append((method, path, body))
        if method == "GET" and path == "/api/functions":
            return self.listed if hasattr(self, "listed") else []
        if method == "GET" and path == "/api/tools":
            return [{"name": n, "status": "active"} for n in ("ocr", "web_search", "web_fetch", "human_approval", "issue_refund")]
        if method == "POST" and path == "/api/functions":
            return {**self.fn, "name": body["name"], "slug": body["slug"]}
        if method == "GET" and path == f"/api/functions/{self.fn['id']}":
            return self.fn
        if method == "GET" and path == f"/api/functions/{self.fn['id']}/versions":
            return self.versions
        if method == "POST" and path == f"/api/functions/{self.fn['id']}/submit":
            v = {"id": self._next_version_id, "number": len(self.versions) + 1,
                 "status": "draft", "snapshot": body["snapshot"]}
            self._next_version_id += 1
            self.versions.append(v)
            return v
        if method == "PUT" and path.startswith("/api/versions/"):
            vid = int(path.rsplit("/", 1)[1])
            v = next(x for x in self.versions if x["id"] == vid)
            if getattr(self, "frozen_on_put", False):
                raise FnaiError(409, "versions are immutable once deployed")
            v.update(body)
            return v
        if method == "POST" and path == f"/api/functions/{self.fn['id']}/versions":
            v = {"id": self._next_version_id, "number": max([x["number"] for x in self.versions] or [0]) + 1,
                 "status": "draft", **body}
            self._next_version_id += 1
            self.versions.append(v)
            return v
        if method == "POST" and path == f"/api/functions/{self.fn['id']}/deploy":
            return {"id": 9, "status": "active", "version_id": body["version_id"]}
        raise AssertionError(f"unexpected call: {method} {path}")


class FunctionDraftTests(unittest.TestCase):
    def test_new_creates_nothing_on_the_server_until_submit(self):
        http = _FakeDraftHttp()
        draft = FunctionDraft.create(http, name="Invoice Extractor")
        self.assertIsNone(draft.id)                               # not created yet
        self.assertEqual([c[0] for c in http.calls], ["GET"])     # only the read-only name check

        draft.add_input("document", type="file", media="document", required=True)
        draft.add_output("total", type="number", required=True)
        draft.add_section("role", "You extract data from invoices.")
        draft.add_step("Read the invoice.")
        self.assertEqual(len(http.calls), 1)                      # still nothing sent

        draft.submit()
        methods = [(c[0], c[1]) for c in http.calls]
        self.assertIn(("POST", "/api/functions"), methods)        # created now...
        self.assertLess(methods.index(("POST", "/api/functions")),
                        methods.index(("POST", "/api/functions/60/submit")))   # ...then saved
        self.assertEqual(draft.id, 60)
        created = next(c for c in http.calls if c[:2] == ("POST", "/api/functions"))[2]
        self.assertEqual(created["slug"], "invoice_extractor")
        self.assertIn("total", created["output_schema"]["properties"])

    def test_a_taken_name_is_refused_at_new_even_if_archived(self):
        http = _FakeDraftHttp()
        http.listed = [{"id": 7, "slug": "invoice_extractor", "archived": True}]
        with self.assertRaises(ValueError) as e:
            FunctionDraft.create(http, name="Invoice Extractor")
        self.assertIn("(archived)", str(e.exception))
        self.assertIn("c.load_draft(7)", str(e.exception))
        self.assertEqual([c[0] for c in http.calls], ["GET"])

    def test_a_failed_save_does_not_create_the_function_twice(self):
        http = _FakeDraftHttp()
        draft = FunctionDraft.create(http, name="Invoice Extractor")
        draft.add_output("o", required=True).add_step("s")
        real = http.__call__
        state = {"fail": True}

        def flaky(method, path, body=None, query=None):
            if path.endswith("/submit") and state["fail"]:
                http.calls.append((method, path, body))
                raise FnaiError(422, "server said no")
            return real(method, path, body, query)
        http.__call__ = flaky
        draft._h = flaky
        with self.assertRaises(FnaiError):
            draft.submit()
        self.assertEqual(draft.id, 60)                            # created, and remembered
        state["fail"] = False
        draft.submit()
        creates = [c for c in http.calls if c[:2] == ("POST", "/api/functions")]
        self.assertEqual(len(creates), 1)                         # the retry did not create it again

    def test_a_name_clash_at_save_says_what_to_do_and_keeps_the_work(self):
        http = _FakeDraftHttp()
        draft = FunctionDraft.create(http, name="Invoice Extractor", save_to=False)
        draft.add_output("o", required=True).add_step("s")

        def clash(method, path, body=None, query=None):
            if (method, path) == ("POST", "/api/functions"):
                raise FnaiError(409, "slug 'invoice_extractor' already exists in this organization")
            return _FakeDraftHttp.__call__(http, method, path, body, query)
        draft._h = clash
        with self.assertRaises(FnaiError) as e:
            draft.submit()
        self.assertIn("slug='...'", e.exception.detail)
        self.assertIsNone(draft.id)
        self.assertIn("o", draft._output_schema["properties"])    # the work is still there

    def test_submit_sends_the_staged_snapshot_and_schemas(self):
        http = _FakeDraftHttp()
        draft = FunctionDraft.create(http, name="Invoice Extractor")
        draft.add_input("document", type="file", media="document", required=True)
        draft.add_output("total", type="number", required=True)
        draft.add_section("role", "You extract data from invoices.")
        draft.add_step("Read the invoice and extract the total.")
        draft.add_tool("ocr")

        v = draft.submit(message="first draft")
        self.assertEqual(v["id"], 500)
        self.assertEqual(v["number"], 1)
        submit_call = next(c for c in http.calls if c[1].endswith("/submit"))
        body = submit_call[2]
        self.assertEqual(body["message"], "first draft")
        # the server reads `required` as a list on the schema, and stores a
        # file field as a string handle tagged x-media
        self.assertEqual(body["input_schema"]["properties"]["document"],
                         {"type": "string", "x-media": "document", "x-delivery": "native"})
        self.assertEqual(body["input_schema"]["required"], ["document"])
        self.assertEqual(body["output_schema"]["properties"]["total"], {"type": "number"})
        self.assertEqual(body["output_schema"]["required"], ["total"])
        self.assertEqual(body["snapshot"]["implementation"]["sections"]["role"],
                         "You extract data from invoices.")
        self.assertEqual(body["snapshot"]["implementation"]["tools"], [{"name": "ocr"}])
        # a v2 definition document: field-form input/output, flow, mode
        snap = body["snapshot"]
        self.assertEqual(snap["output"]["total"], {"type": "number", "required": True})
        self.assertEqual(snap["input"]["document"]["type"], "file")
        self.assertEqual(snap["implementation"]["flow"], ["Read the invoice and extract the total."])
        self.assertEqual(snap["implementation"]["execution"], {"mode": "agent"})  # a tool was added
        self.assertEqual(draft.last_version, v)

    def test_deploy_uses_the_version_from_the_last_submit(self):
        http = _FakeDraftHttp()
        draft = FunctionDraft.create(http, name="Invoice Extractor")
        draft.add_output("total", type="number")
        draft.add_step("Extract the total.")
        v = draft.submit()

        result = draft.deploy(note="go live")
        deploy_call = next(c for c in http.calls if c[1].endswith("/deploy"))
        self.assertEqual(deploy_call[2], {"version_id": v["id"], "note": "go live"})
        self.assertEqual(result["status"], "active")

    def test_deploy_sends_the_chosen_model(self):
        http = _FakeDraftHttp()
        draft = FunctionDraft.create(http, name="Invoice Extractor")
        draft.add_output("total", type="number")
        draft.add_step("Extract the total.")
        v = draft.submit()
        draft.deploy(note="go live", model="gpt-4o-mini", temperature=0.1)
        deploy_call = next(c for c in http.calls if c[1].endswith("/deploy"))
        self.assertEqual(deploy_call[2], {"version_id": v["id"], "note": "go live",
                                          "model": "gpt-4o-mini", "temperature": 0.1})

    def test_deployments_deploy_sends_the_chosen_model_and_omits_it_otherwise(self):
        from fnai import _Deployments
        h = _RecHttp()
        _Deployments(h).deploy(3, 4, model="gpt-4o-mini")
        self.assertEqual(next(c for c in h.calls if c[1].endswith("/deploy"))[2],
                         {"version_id": 4, "model": "gpt-4o-mini"})
        h = _RecHttp()
        _Deployments(h).deploy(3, 4)
        self.assertEqual(next(c for c in h.calls if c[1].endswith("/deploy"))[2], {"version_id": 4})

    def test_models_list_calls_the_model_list_endpoint(self):
        from fnai import _Models
        h = _RecHttp()
        _Models(h).list()
        self.assertEqual(h.calls[-1][:2], ("GET", "/api/models"))

    def test_deploy_before_any_submit_raises(self):
        http = _FakeDraftHttp()
        draft = FunctionDraft.create(http, name="Invoice Extractor")
        with self.assertRaises(ValueError):
            draft.deploy()

    def test_load_resumes_an_existing_functions_staged_content(self):
        existing_version = {
            "id": 500, "number": 1, "status": "approved",
            "snapshot": {"name": "Invoice Extractor", "output": {},
                        "implementation": {"sections": {"role": "You extract invoices."},
                                          "tools": [{"name": "ocr"}]}},
        }
        http = _FakeDraftHttp(versions=[existing_version])
        draft = FunctionDraft.load(http, http.fn["id"])
        self.assertEqual(draft.last_version["id"], 500)
        self.assertEqual(draft._sections["role"], "You extract invoices.")
        self.assertEqual(draft._tools, [{"name": "ocr"}])

        # adding a field after it's already deployed, then submit — same
        # `submit` call as building it fresh, per the design doc's example
        draft.add_output("tax_amount", type="number")
        draft.add_step("Extract the tax amount too.")
        v = draft.submit(message="add tax_amount")
        self.assertEqual(v["number"], 2)  # a new version, not an overwrite

    def test_load_takes_the_schemas_and_steps_from_the_latest_version(self):
        # found live: the function's own schemas are empty once submit() has been used
        version = {"id": 500, "number": 1, "status": "draft",
                   "input_schema": {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]},
                   "output_schema": {"type": "object", "properties": {"o": {"type": "string"}}, "required": ["o"]},
                   "snapshot": {"implementation": {"flow": ["Do it."], "sections": {"role": "r"}}}}
        http = _FakeDraftHttp(versions=[version])
        draft = FunctionDraft.load(http, http.fn["id"])
        draft.add_example(input={"a": "x"}, output={"o": "y"})
        draft.submit()     # would raise "no output fields" if load() had ignored the version
        body = next(c for c in http.calls if c[1].endswith("/submit"))[2]
        self.assertEqual(body["output_schema"]["required"], ["o"])
        self.assertEqual(body["snapshot"]["implementation"]["flow"], ["Do it."])

    def test_a_loaded_single_shot_function_becomes_agent_when_a_tool_is_added(self):
        version = {"id": 500, "number": 1, "status": "draft",
                   "input_schema": {"type": "object", "properties": {}},
                   "output_schema": {"type": "object", "properties": {"o": {"type": "string"}}},
                   "snapshot": {"implementation": {"execution": {"mode": "single_shot"}, "flow": ["x"]}}}
        http = _FakeDraftHttp(versions=[version])
        draft = FunctionDraft.load(http, http.fn["id"])
        self.assertEqual(draft._effective_mode(), "single_shot")
        draft.add_tool("web_search")
        self.assertEqual(draft._effective_mode(), "agent")

    def test_add_section_rejects_an_unknown_kind(self):
        http = _FakeDraftHttp()
        draft = FunctionDraft.create(http, name="Invoice Extractor")
        with self.assertRaises(ValueError):
            draft.add_section("bogus_kind", "x")

    def test_add_example_requires_input_and_output_when_positive(self):
        http = _FakeDraftHttp()
        draft = FunctionDraft.create(http, name="Invoice Extractor")
        with self.assertRaises(ValueError):
            draft.add_example(input={"document": "x"})  # missing output


class ClientDraftHelpers(unittest.TestCase):
    def test_client_draft_and_load_draft_delegate_to_functiondraft(self):
        c = Client(token="fnp_test")
        http = _FakeDraftHttp()
        c._http = http  # swap the real transport for the fake, same as other Client tests
        draft = c.draft(name="Invoice Extractor")
        self.assertIsInstance(draft, FunctionDraft)
        self.assertIsNone(draft.id)           # created on the server at submit()

        loaded = c.load_draft(60)
        self.assertIsInstance(loaded, FunctionDraft)



class _RecHttp:
    """Records calls; answers the few paths the new helpers touch."""
    _base = "http://x"

    def __init__(self):
        self.calls = []

    def __call__(self, method, path, body=None, query=None):
        self.calls.append((method, path, body))
        if path.endswith("/deploy"):
            return {"id": 1}
        if path == "/api/functions/3":
            return {"slug": "hi", "input_schema": {"properties": {"a": {}}}}
        if path.endswith("/run"):
            return {"id": 9, "status": "completed"}
        if path.endswith("/findings"):
            return [{"title": "t"}]

    def upload(self, path, *, filename, content, fields):
        self.calls.append(("UPLOAD", path, {"filename": filename, "fields": fields}))
        return {"added": 1, "skipped": 0, "errors": []}


class NewSdkSurface(unittest.TestCase):
    def test_deploy_returns_a_curl_for_the_public_runtime(self):
        from fnai import _Deployments
        out = _Deployments(_RecHttp()).deploy(3, 4)
        self.assertEqual(out["endpoint"], "http://x/v1/run/hi")
        self.assertIn("curl -X POST http://x/v1/run/hi", out["curl"])
        self.assertIn('"input": {"a": "..."}', out["curl"])

    def test_experiment_run_includes_findings_by_default(self):
        from fnai import _Experiments
        self.assertEqual(_Experiments(_RecHttp()).run(9)["findings"], [{"title": "t"}])
        self.assertNotIn("findings", _Experiments(_RecHttp()).run(9, include_findings=False))

    def test_dataset_upload_sends_file_and_mapping(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write(Path(d), "rows.csv", "q\nhello\n")
            h = _RecHttp()
            _Datasets(h).upload(5, p, mapping={"q": "question"})
        _, path, meta = h.calls[-1]
        self.assertEqual(path, "/api/datasets/5/upload")
        self.assertEqual(meta["filename"], "rows.csv")
        self.assertEqual(json.loads(meta["fields"]["mapping"]), {"q": "question"})

    def test_dataset_upload_rejects_bad_file_type(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write(Path(d), "rows.txt", "x")
            with self.assertRaises(ValueError):
                _Datasets(_RecHttp()).upload(5, p, mapping={"q": "question"})

    def test_tools_create_validates_target(self):
        from fnai import _Tools
        with self.assertRaises(ValueError):
            _Tools(_RecHttp()).create(name="t", description="d", target_type="http")
        with self.assertRaises(ValueError):   # no input_schema / openapi_spec
            _Tools(_RecHttp()).create(name="t", description="d", target_type="http", url="https://e.x")
        with self.assertRaises(ValueError):   # python needs input_schema too
            _Tools(_RecHttp()).create(name="t", description="d", target_type="python", code="def run(args): pass")
        h = _RecHttp()
        _Tools(h).create(name="t", description="d", target_type="http", url="https://e.x",
                         input_schema={"type": "object", "properties": {}})
        self.assertEqual(h.calls[-1][1], "/api/tools")


class CompareExperiments(unittest.TestCase):
    def _exp(self, i, pass_rate, cost):
        return {"id": i, "name": f"e{i}", "status": "completed", "cost": cost,
                "results": {"n_rows": 10, "latency": {"p95": 2000},
                            "per_version": {str(i): {"number": i}},
                            "checks": [{"check_name": "valid", "pass_rate": pass_rate}]}}

    def test_compare_picks_winner_per_row(self):
        from fnai import _Experiments
        exps = {1: self._exp(1, 0.6, 1.0), 2: self._exp(2, 0.9, 2.0)}

        class H:
            def __call__(s, m, path, *a, **k):
                return exps[int(path.rsplit("/", 1)[1])]
        out = _Experiments(H()).compare([1, 2])
        rows = {r["metric"]: r for r in out["rows"]}
        self.assertEqual([e["version"] for e in out["experiments"]], ["v1", "v2"])
        self.assertEqual(rows["Pass rate %"]["best"], 1)       # higher wins
        self.assertEqual(rows["Cost per run $"]["best"], 0)    # lower wins
        self.assertEqual(rows["check: valid %"]["values"], [60.0, 90.0])

    def test_compare_needs_two_completed(self):
        from fnai import _Experiments
        with self.assertRaises(ValueError):
            _Experiments(_RecHttp()).compare([1])

        class H:
            def __call__(s, *a, **k):
                return {"id": 1, "status": "running"}
        with self.assertRaises(ValueError):
            _Experiments(H()).compare([1, 2])


class StepByStepBuilder(unittest.TestCase):
    """c.functions.new(...) + add_* calls: validated as they are added."""

    def _draft(self):
        return FunctionDraft.create(_FakeDraftHttp(), name="WT Reviewer")

    def test_functions_new_starts_an_empty_draft(self):
        http = _FakeDraftHttp()
        d = _Functions(http).new("WT Reviewer")
        self.assertIsInstance(d, FunctionDraft)
        self.assertEqual(len(http.calls), 1)

    def test_required_goes_in_the_schema_required_list(self):
        d = self._draft()
        d.add_input("a", required=True).add_input("b")
        self.assertEqual(d._input_schema["required"], ["a"])
        self.assertNotIn("required", d._input_schema["properties"]["a"])

    def test_add_prompt_sets_the_role(self):
        d = self._draft().add_prompt("You review tickets.")
        self.assertEqual(d._sections["role"], "You review tickets.")

    def test_unknown_key_suggests_the_right_one(self):
        with self.assertRaises(DefinitionError) as c:
            self._draft().add_input("x", minimun=1)
        self.assertIn("did you mean 'minimum'", str(c.exception))

    def test_bad_rules_are_rejected_at_add_time(self):
        cases = [
            dict(type="text"),                                   # not a type
            dict(type="string", minimum=1),                      # minimum needs a number
            dict(type="integer", minimum=10, maximum=5),         # min above max
            dict(type="string", enum=[1, 2]),                    # enum not of the type
            dict(type="string", pattern="("),                    # bad regex
            dict(type="string", format="phone"),                 # unknown format
            dict(type="array"),                                  # array without items
            dict(type="string", length={"unit": "tokens"}),      # output-only on input
            dict(type="file"),                                   # file without media
            dict(type="string", min_items=1),                    # min_items needs array
        ]
        for spec in cases:
            with self.subTest(spec=spec), self.assertRaises(DefinitionError):
                self._draft().add_input("x", **spec)

    def test_output_only_and_input_only_keys(self):
        d = self._draft()
        d.add_output("summary", length={"unit": "tokens", "target": 60, "range": [40, 80]})
        with self.assertRaises(DefinitionError):
            d.add_output("doc", type="file", media="document")   # file is input-only
        self.assertEqual(d._output_schema["properties"]["summary"]["x-soft"]["length"]["target"], 60)

    def test_duplicate_field_is_rejected(self):
        d = self._draft().add_input("a")
        with self.assertRaises(DefinitionError):
            d.add_input("a")

    def test_valid_nested_field_projects_like_the_server(self):
        d = self._draft().add_input("tags", type="array", items={"type": "string"}, min_items=1)
        self.assertEqual(d._input_schema["properties"]["tags"],
                         {"type": "array", "items": {"type": "string"}, "minItems": 1})

    def test_check_input_runs_the_rules_locally(self):
        d = self._draft().add_input("text", required=True).add_input("rating", type="integer", minimum=1, maximum=5)
        d.add_input("email", format="email")
        self.assertEqual(d.check_input({"text": "hi", "rating": 3, "email": "a@b.co"}), [])
        problems = d.check_input({"rating": 9, "email": "nope"})
        self.assertIn("missing required field 'text'", problems)
        self.assertIn("field 'rating' must be at most 5, got 9", problems)
        self.assertIn("field 'email' is not a valid email", problems)

    def test_submit_reports_an_example_naming_an_undeclared_field(self):
        d = self._draft().add_input("a").add_output("o", required=True)
        d.add_example(input={"zzz": 1}, output={"o": "x"})
        with self.assertRaises(DefinitionError) as c:
            d.submit()
        self.assertIn("'zzz' is not declared", str(c.exception))


class ToolsAndDeployedCheck(unittest.TestCase):
    def test_submit_names_an_unknown_tool_and_suggests_one(self):
        d = FunctionDraft.create(_FakeDraftHttp(), name="X")
        d.add_tool("web_serch")
        with self.assertRaises(DefinitionError) as c:
            d.submit()
        self.assertIn("did you mean 'web_search'", str(c.exception))
        self.assertIn("c.tools.create", str(c.exception))

    def test_known_and_pinned_tools_pass(self):
        d = FunctionDraft.create(_FakeDraftHttp(), name="X")
        d.add_tool("web_search").add_tool("summarizer", ref="summarizer@3")
        d.add_output("o", required=True).add_step("Do it.")
        d.submit()

    def test_function_check_names_the_bad_field_without_running(self):
        from fnai import Function
        f = Function("wt", "fnk_x")
        f._http = lambda m, p, b=None, query=None: {"input_schema": {
            "type": "object", "required": ["text"],
            "properties": {"text": {"type": "string"},
                           "rating": {"type": "integer", "maximum": 5}}}}
        self.assertEqual(f.check(text="hi", rating=3), [])
        self.assertEqual(f.check(rating=9),
                         ["missing required field 'text'", "field 'rating' must be at most 5, got 9"])
        self.assertEqual(f.check(text=5), ["field 'text' must be a string, got int"])


class SubmitMustSatisfyTheServer(unittest.TestCase):
    """Found live: the server refuses a definition with no output field or no flow step."""

    def test_submit_without_output_or_steps_lists_both(self):
        d = FunctionDraft.create(_FakeDraftHttp(), name="X")
        with self.assertRaises(DefinitionError) as c:
            d.submit()
        self.assertEqual(c.exception.problems[:2], ["no output fields — add_output(...)", "no steps — add_step(...)"])

    def test_add_tool_options_go_under_config_or_their_own_keys(self):
        d = FunctionDraft.create(_FakeDraftHttp(), name="X")
        d.add_tool("knowledge_files", mode="read", max_calls=12)
        d.add_tool("issue_refund", requires="human_approval.returned.approved == true", effect="side_effecting")
        self.assertEqual(d._tools[0], {"name": "knowledge_files", "config": {"mode": "read"}, "max_calls": 12})
        self.assertEqual(d._tools[1]["requires"], "human_approval.returned.approved == true")

    def test_steps_show_in_the_printed_yaml(self):
        d = FunctionDraft.create(_FakeDraftHttp(), name="X").add_step("Read it.")
        self.assertIn('flow:\n    - "Read it."', d.to_yaml())

    def test_set_mode_rejects_a_bad_value(self):
        with self.assertRaises(ValueError):
            FunctionDraft.create(_FakeDraftHttp(), name="X").set_mode("workflow")


class ExamplesAndToolsFollowTheDsl(unittest.TestCase):
    def _d(self):
        return FunctionDraft.create(_FakeDraftHttp(), name="X")

    def test_negative_example_needs_a_note_and_an_output_or_bad_value(self):
        with self.assertRaises(DefinitionError) as c:
            self._d().add_example(polarity="negative", output={"o": "bad"})
        self.assertIn("needs a note", str(c.exception))
        with self.assertRaises(DefinitionError):
            self._d().add_example(polarity="negative", note="why")
        self._d().add_example(polarity="negative", output={"o": "bad"}, note="too vague")

    def test_positive_example_needs_both_and_polarity_must_be_known(self):
        with self.assertRaises(DefinitionError):
            self._d().add_example(input={"a": 1})
        with self.assertRaises(DefinitionError):
            self._d().add_example(polarity="maybe", input={}, output={})

    def test_tool_options_are_checked(self):
        for kw in (dict(max_calls=0), dict(max_calls=True), dict(effect="dangerous"),
                   dict(requires="no operator here")):
            with self.subTest(kw=kw), self.assertRaises(DefinitionError):
                self._d().add_tool("t", **kw)
        d = self._d().add_tool("t")
        with self.assertRaises(DefinitionError):
            d.add_tool("t")      # added twice

    def test_a_valid_guard_is_accepted_and_scope_is_checked_at_submit(self):
        d = self._d().add_input("amount", type="number")
        d.add_output("o", required=True).add_step("Do it.")
        d.add_tool("human_approval")
        d.add_tool("issue_refund", requires='human_approval.returned.approved == true && input.amount > 100')
        d.submit()           # both references are declared
        bad = self._d().add_output("o", required=True).add_step("Do it.")
        bad.add_tool("issue_refund", requires="human_approval.called == true")
        with self.assertRaises(DefinitionError) as c:
            bad.submit()
        self.assertIn("doesn't declare", str(c.exception))


class RubricAndMemory(unittest.TestCase):
    def _d(self):
        return FunctionDraft.create(_FakeDraftHttp(), name="X").add_output("o", required=True).add_step("s")

    def test_add_rubric_adds_sentences_and_rejects_empty_or_repeated(self):
        d = self._d().add_rubric("Names the symptom.").add_rubric("Cites the ticket.")
        self.assertEqual(d._rubric, ["Names the symptom.", "Cites the ticket."])
        with self.assertRaises(DefinitionError):
            d.add_rubric("   ")
        with self.assertRaises(DefinitionError):
            d.add_rubric("Names the symptom.")

    def test_set_memory_checks_per_and_the_lists(self):
        d = self._d().set_memory(per="caller", remember=["plan"], never=["card numbers"])
        self.assertEqual(d._memory, {"per": "caller", "remember": ["plan"], "never": ["card numbers"]})
        d.set_memory(per="space")                       # a second call replaces the first
        self.assertEqual(d._memory, {"per": "space"})
        for kw in (dict(per="everyone"), dict(per="caller", remember="plan"), dict(per="caller", never=[""])):
            with self.subTest(kw=kw), self.assertRaises(DefinitionError):
                d.set_memory(**kw)

    def test_both_go_in_the_snapshot_and_the_printed_yaml(self):
        http = _FakeDraftHttp()
        d = FunctionDraft.create(http, name="X").add_output("o", required=True).add_step("s")
        d.add_rubric("Names the symptom.").set_memory(per="caller", never=["card numbers"])
        text = d.to_yaml()
        self.assertIn('rubric:\n  - "Names the symptom."', text)
        self.assertIn("memory:\n  per: caller\n  never: [card numbers]", text)
        d.submit()
        snap = next(c for c in http.calls if c[1].endswith("/submit"))[2]["snapshot"]
        self.assertEqual(snap["rubric"], ["Names the symptom."])
        self.assertEqual(snap["memory"], {"per": "caller", "never": ["card numbers"]})

    def test_load_brings_rubric_and_memory_back(self):
        version = {"id": 500, "number": 1, "status": "draft",
                   "input_schema": {"type": "object", "properties": {}},
                   "output_schema": {"type": "object", "properties": {"o": {"type": "string"}}},
                   "snapshot": {"rubric": ["r1"], "memory": {"per": "space"},
                                "implementation": {"flow": ["x"]}}}
        http = _FakeDraftHttp(versions=[version])
        d = FunctionDraft.load(http, http.fn["id"])
        self.assertEqual((d._rubric, d._memory), (["r1"], {"per": "space"}))


class LocalDraftFiles(unittest.TestCase):
    """The draft file kept on the user's computer, save_yaml, and load_yaml."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _new(self, **kw):
        return FunctionDraft.create(_FakeDraftHttp(), name="Ticket Triage", **kw)

    def _build(self, d):
        d.add_input("ticket_text", required=True).add_input("amount", type="number", minimum=0)
        d.add_output("summary", required=True).add_output("urgency", type="integer", minimum=1, maximum=5, required=True)
        d.add_prompt("You triage tickets.").add_section("definitions", "Urgency: 1 to 5.")
        d.add_section("constraints", "Never invent facts.").add_section("edge_cases", "Empty ticket: urgency 1.")
        d.add_step("Read.").add_step("Score.")
        d.add_example(input={"ticket_text": "crash"}, output={"summary": "crash", "urgency": 4})
        d.add_example(polarity="negative", output={"summary": "idk", "urgency": 3}, note="vague")
        d.add_tool("human_approval", max_calls=2).add_rubric("Names the problem.")
        d.set_memory(per="caller", never=["card numbers"])
        return d

    def test_the_file_is_created_with_the_function_and_follows_every_change(self):
        path = self.dir / "triage.yaml"
        d = self._new(save_to=path)
        self.assertIn("name: Ticket Triage", path.read_text())
        self.assertIn("meta:\n  function: invoice_extractor", path.read_text().replace("ticket_triage", "invoice_extractor"))
        d.add_input("ticket_text", required=True)
        self.assertIn("ticket_text: { type: string, required: true }", path.read_text())
        d.add_prompt("You triage.")
        self.assertIn('role: "You triage."', path.read_text())
        for call in (lambda: d.add_step("Read."), lambda: d.add_rubric("r"), lambda: d.set_memory(per="space"),
                     lambda: d.add_tool("t"), lambda: d.add_example(input={"ticket_text": "x"}, output={})):
            before = path.read_text(); call(); self.assertNotEqual(before, path.read_text())

    def test_a_rejected_call_leaves_the_file_alone(self):
        path = self.dir / "t.yaml"
        d = self._new(save_to=path).add_input("a")
        before = path.read_text()
        with self.assertRaises(DefinitionError):
            d.add_input("a")
        self.assertEqual(path.read_text(), before)

    def test_where_the_file_goes(self):
        # a folder: file named after the function
        d = self._new(save_to=self.dir / "mine")
        self.assertTrue((self.dir / "mine" / f"{d.slug}.yaml").is_file())
        # the client-wide folder
        d2 = FunctionDraft.create(_FakeDraftHttp(), name="Other", drafts_folder=self.dir / "all")
        self.assertTrue((self.dir / "all" / f"{d2.slug}.yaml").is_file())
        # save_to wins over drafts_folder
        d3 = FunctionDraft.create(_FakeDraftHttp(), name="Third", save_to=self.dir / "x.yaml", drafts_folder=self.dir / "all2")
        self.assertTrue((self.dir / "x.yaml").is_file()); self.assertFalse((self.dir / "all2").exists())
        # nothing given: the default (here the env folder the tests set)
        d4 = self._new()
        self.assertTrue((Path(os.environ["FNAI_DRAFTS_DIR"]) / f"{d4.slug}.yaml").is_file())

    def test_save_to_false_turns_it_off(self):
        d = self._new(save_to=False)
        d.add_input("a")
        self.assertIsNone(d._draft_path)

    def test_submit_deletes_the_draft_file_but_not_a_save_yaml_copy(self):
        path = self.dir / "draft.yaml"; copy = self.dir / "copy.yaml"
        d = self._new(save_to=path); self._build(d)
        d.save_yaml(copy)
        d.submit()
        self.assertFalse(path.exists()); self.assertTrue(copy.exists())

    def test_save_yaml_takes_a_file_or_a_folder_and_makes_folders(self):
        d = self._new(save_to=False).add_input("a")
        out = d.save_yaml(self.dir / "deep" / "er" / "f.yaml")
        self.assertTrue(Path(out).is_file())
        out2 = d.save_yaml(self.dir / "folder")
        self.assertEqual(Path(out2).name, f"{d.slug}.yaml")

    def test_a_write_problem_warns_once_and_never_stops_the_script(self):
        blocker = self.dir / "blocker"; blocker.write_text("a file, not a folder")
        import warnings
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            d = FunctionDraft.create(_FakeDraftHttp(), name="X", save_to=blocker / "x.yaml")
            d.add_input("a").add_input("b")
        self.assertEqual(len([x for x in w if "draft file" in str(x.message)]), 1)
        self.assertIn("a", d._input_schema["properties"])
        with self.assertRaises(OSError):
            d.save_yaml(blocker / "y.yaml")      # an explicit save is allowed to fail loudly

    def test_load_yaml_gives_back_the_same_function(self):
        original = self._build(self._new(save_to=self.dir / "d.yaml"))
        c = Client(token="fnp_test"); c._http = _FakeDraftHttp(); c.functions = _Functions(c._http)
        loaded = c.load_yaml(self.dir / "d.yaml", save_to=False)
        self.assertEqual(loaded._snapshot(), original._snapshot())
        self.assertEqual(loaded._input_schema, original._input_schema)
        self.assertEqual(loaded._output_schema, original._output_schema)

    def test_load_yaml_does_not_create_the_function_until_submit(self):
        c = Client(token="fnp_test"); c._http = _FakeDraftHttp(); c.functions = _Functions(c._http)
        original = self._build(self._new(save_to=self.dir / "d.yaml"))
        loaded = c.load_yaml(self.dir / "d.yaml", save_to=False)
        self.assertIsNone(loaded.id)
        self.assertEqual([x for x in c._http.calls if x[0] == "POST"], [])
        c._http.listed = [{"id": 77, "slug": loaded.slug}]
        again = c.load_yaml(self.dir / "d.yaml", save_to=False)
        self.assertEqual(again.id, 77)                    # an existing function is continued

    def test_load_yaml_by_name_from_the_drafts_folder(self):
        c = Client(token="fnp_test", drafts_folder=self.dir / "dr"); c._http = _FakeDraftHttp(); c.functions = _Functions(c._http, self.dir / "dr")
        d = FunctionDraft.create(c._http, name="Ticket Triage", drafts_folder=self.dir / "dr").add_input("a")
        loaded = c.load_yaml("Ticket Triage")
        self.assertIn("a", loaded._input_schema["properties"])
        with self.assertRaises(FileNotFoundError):
            c.load_yaml("nothing here")

    def test_loading_a_copy_never_overwrites_or_deletes_it(self):
        copy = self.dir / "copy.yaml"
        self._build(self._new(save_to=False)).save_yaml(copy)
        text = copy.read_text()
        c = Client(token="fnp_test", drafts_folder=self.dir / "dr"); c._http = _FakeDraftHttp(); c.functions = _Functions(c._http, self.dir / "dr")
        d = c.load_yaml(copy); d.add_step("Another step."); d.submit()
        self.assertEqual(copy.read_text(), text)

    def test_load_yaml_applies_the_same_checks_as_add_calls(self):
        p = self.dir / "bad.yaml"; p.write_text("name: X\ninput:\n  a: { type: text }\n")
        c = Client(token="fnp_test"); c._http = _FakeDraftHttp(); c.functions = _Functions(c._http)
        with self.assertRaises(DefinitionError):
            c.load_yaml(p)

    def test_without_pyyaml_loading_says_how_to_fix_it(self):
        import sys, unittest.mock
        c = Client(token="fnp_test"); c._http = _FakeDraftHttp()
        with unittest.mock.patch.dict(sys.modules, {"yaml": None}):
            with self.assertRaises(ImportError) as e:
                c.load_yaml("whatever")
        self.assertIn("pip install pyyaml", str(e.exception))


class LookUpByName(unittest.TestCase):
    """c.functions.get("my_function") and c.load_draft("my_function")."""

    def _http(self):
        http = _FakeDraftHttp()
        http.listed = [{"id": 60, "slug": "invoice_extractor"}, {"id": 61, "slug": "ticket_triage"}]
        return http

    def test_get_by_slug_returns_the_same_record_as_get_by_id(self):
        http = self._http()
        by_slug = _Functions(http).get("invoice_extractor")
        self.assertEqual(by_slug["id"], 60)
        self.assertIn("name: Invoice Extractor", str(by_slug))
        self.assertEqual(_Functions(http).get(60)["id"], 60)

    def test_an_id_never_triggers_a_lookup(self):
        http = self._http()
        _Functions(http).get(60)
        self.assertNotIn(("GET", "/api/functions"), [(c[0], c[1]) for c in http.calls])

    def test_an_unknown_slug_says_so_suggests_and_lists(self):
        with self.assertRaises(FnaiError) as e:
            _Functions(self._http()).get("invoice_extracter")
        self.assertEqual(e.exception.status, 404)
        msg = e.exception.detail
        self.assertIn("no function with slug 'invoice_extracter'", msg)
        self.assertIn("did you mean 'invoice_extractor'", msg)
        self.assertIn("ticket_triage", msg)

    def test_a_display_name_is_not_matched_and_a_number_as_text_hints(self):
        with self.assertRaises(FnaiError):
            _Functions(self._http()).get("Invoice Extractor")
        with self.assertRaises(FnaiError) as e:
            _Functions(self._http()).get("60")
        self.assertIn("pass the number 60", e.exception.detail)

    def test_no_functions_in_the_space_is_said_plainly(self):
        http = _FakeDraftHttp()
        with self.assertRaises(FnaiError) as e:
            _Functions(http).get("anything")
        self.assertIn("no functions in this space yet", e.exception.detail)

    def test_bad_ids_are_still_refused_locally(self):
        for bad in (0, -1, True, None, 1.5):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                _Functions(self._http()).get(bad)

    def test_load_draft_by_slug(self):
        http = self._http()
        http.fn = {"id": 61, "slug": "ticket_triage", "name": "Ticket Triage", "input_schema": {}, "output_schema": {}}
        c = Client(token="fnp_test"); c._http = http
        draft = c.load_draft("ticket_triage", save_to=False)
        self.assertEqual((draft.id, draft.slug), (61, "ticket_triage"))


class LoadAnyVersion(unittest.TestCase):
    """load_draft(..., version=N): which version a save goes to."""

    def _versions(self):
        def mk(n, status, flow):
            return {"id": 100 + n, "number": n, "status": status,
                    "input_schema": {"type": "object", "properties": {"a": {"type": "string"}}},
                    "output_schema": {"type": "object", "properties": {"o": {"type": "string"}}, "required": ["o"]},
                    "snapshot": {"implementation": {"flow": flow, "sections": {"role": f"role of v{n}"}}}}
        return [mk(1, "approved", ["v1 step"]), mk(2, "approved", ["v2 step"]), mk(3, "draft", ["v3 step"])]

    def _load(self, **kw):
        http = _FakeDraftHttp(versions=self._versions())
        return http, FunctionDraft.load(http, http.fn["id"], save_to=False, **kw)

    def test_no_version_loads_the_latest_and_saves_by_the_server_rule(self):
        http, d = self._load()
        self.assertEqual(d._flow, ["v3 step"])
        d.submit()
        self.assertEqual([c[1] for c in http.calls if c[0] in ("POST", "PUT") and "submit" in c[1]],
                         ["/api/functions/60/submit"])

    def test_a_frozen_version_loads_its_own_content_and_saves_as_a_new_version(self):
        http, d = self._load(version=1)
        self.assertEqual(d._flow, ["v1 step"])
        self.assertIn("role of v1", d._sections["role"])
        self.assertIn("# based on v1 — saving creates a new version", str(d))
        d.add_step("a new step")
        v = d.submit("from v1")
        self.assertEqual(v["number"], 4)                               # a NEW version
        post = next(c for c in http.calls if c[1] == "/api/functions/60/versions" and c[0] == "POST")
        self.assertEqual(post[2]["parent_id"], 101)                    # recorded as based on v1
        self.assertEqual(post[2]["snapshot"]["implementation"]["flow"], ["v1 step", "a new step"])
        # nothing existing was touched
        self.assertFalse([c for c in http.calls if c[0] == "PUT"])
        self.assertEqual(next(x for x in http.versions if x["number"] == 3)["snapshot"]["implementation"]["flow"], ["v3 step"])

    def test_after_the_first_save_the_new_draft_is_updated_in_place(self):
        http, d = self._load(version=1)
        created = d.submit()
        d.add_step("more")
        d.submit()
        creates = [c for c in http.calls if c[0] == "POST" and c[1] == "/api/functions/60/versions"]
        self.assertEqual(len(creates), 1)                              # only one new version
        self.assertEqual([c[1] for c in http.calls if c[0] == "PUT"], [f"/api/versions/{created['id']}"])

    def test_a_draft_version_is_edited_in_place(self):
        http, d = self._load(version=3)
        self.assertIn("# editing draft v3", str(d))
        d.add_step("edited")
        v = d.submit()
        self.assertEqual(v["number"], 3)
        self.assertEqual([c[:2] for c in http.calls if c[0] == "PUT"], [("PUT", "/api/versions/103")])
        self.assertFalse([c for c in http.calls if c[:2] == ("POST", "/api/functions/60/versions")])

    def test_a_draft_that_is_not_the_latest_is_edited_not_the_latest(self):
        versions = self._versions() + [{"id": 104, "number": 4, "status": "draft", "snapshot": {"implementation": {"flow": ["v4 step"]}}}]
        http = _FakeDraftHttp(versions=versions)
        d = FunctionDraft.load(http, 60, version=3, save_to=False)
        d.add_step("x")
        d.submit()
        self.assertEqual([c[1] for c in http.calls if c[0] == "PUT"], ["/api/versions/103"])   # v3, not v4
        self.assertEqual(next(x for x in http.versions if x["number"] == 4)["snapshot"]["implementation"]["flow"], ["v4 step"])

    def test_the_overwrite_case_loading_v1_never_touches_the_unfinished_v3(self):
        http, d = self._load(version=1)
        d.add_step("x")
        d.submit()
        v3 = next(x for x in http.versions if x["number"] == 3)
        self.assertEqual(v3["snapshot"]["implementation"]["flow"], ["v3 step"])
        self.assertEqual(v3["snapshot"]["implementation"]["sections"]["role"], "role of v3")

    def test_a_missing_version_lists_the_real_ones(self):
        http = _FakeDraftHttp(versions=self._versions())
        with self.assertRaises(FnaiError) as e:
            FunctionDraft.load(http, 60, version=9)
        self.assertIn("no version 9", e.exception.detail)
        self.assertIn("versions: 1, 2, 3", e.exception.detail)
        with self.assertRaises(FnaiError) as e:
            FunctionDraft.load(_FakeDraftHttp(versions=[]), 60, version=1)
        self.assertIn("versions: none yet", e.exception.detail)

    def test_bad_version_numbers_are_refused_locally(self):
        for bad in (0, -1, True, "1", 1.5):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                FunctionDraft.load(_FakeDraftHttp(versions=self._versions()), 60, version=bad)

    def test_a_draft_deployed_meanwhile_gives_a_clear_error_and_keeps_the_work(self):
        http, d = self._load(version=3)
        d.add_step("x")
        http.frozen_on_put = True
        with self.assertRaises(FnaiError) as e:
            d.submit()
        self.assertIn("has been deployed since you loaded it", e.exception.detail)
        self.assertIn("x", d._flow)

    def test_the_draft_file_remembers_the_base_so_a_stopped_script_cannot_overwrite(self):
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        http = _FakeDraftHttp(versions=self._versions())
        http.listed = [{"id": 60, "slug": "invoice_extractor"}]
        d = FunctionDraft.load(http, 60, version=1, save_to=tmp / "d.yaml")
        d.add_step("x")
        self.assertIn("  based_on: 1\n", (tmp / "d.yaml").read_text())
        c = Client(token="fnp_test"); c._http = http; c.functions = _Functions(http)
        loaded = c.load_yaml(tmp / "d.yaml", save_to=False)            # a fresh run after the script stopped
        self.assertIn("# based on v1", str(loaded))
        loaded.submit()
        self.assertTrue([c2 for c2 in http.calls if c2[:2] == ("POST", "/api/functions/60/versions")])
        self.assertFalse([c2 for c2 in http.calls if c2[1].endswith("/submit")])
        # and a draft being edited is remembered the same way
        d3 = FunctionDraft.load(http, 60, version=3, save_to=tmp / "e.yaml")
        d3.add_step("y")
        self.assertIn("  editing: 3\n", (tmp / "e.yaml").read_text())


class NothingIsLostWhenLoadingAVersion(unittest.TestCase):
    """load_draft keeps every part of a version, including the ones the builder has no call for."""

    SNAP = {"name": "X", "needs": ["tool_calling"], "rubric": ["r"], "memory": {"per": "caller"},
            "input": {"url": {"type": "string", "required": True},
                      "page_text": {"type": "string", "derived": True, "sensitive": True},
                      "secret": {"type": "string", "sensitive": True}},
            "output": {"summary": {"type": "string", "required": True, "sensitive": True}},
            "implementation": {"execution": {"mode": "agent"},
                               "preprocessors": [{"tool": "web_fetch", "from": "url", "into": "page_text"}],
                               "flow": ["f"], "sections": {"role": "r", "definitions": ["d"]},
                               "tools": [{"name": "web_fetch", "max_calls": 2}, {"server": "crm", "max_calls": 20}]}}

    def _version(self, snap=None, status="approved"):
        return {"id": 1, "number": 1, "status": status,
                # what the server stores: derived fields are never in the JSON schema, and it has no sensitive flag
                "input_schema": {"type": "object", "properties": {"url": {"type": "string"}, "secret": {"type": "string"}},
                                 "required": ["url"]},
                "output_schema": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]},
                "snapshot": snap if snap is not None else self.SNAP}

    def _load(self, **kw):
        http = _FakeDraftHttp(versions=[self._version(**kw)])
        return http, FunctionDraft.load(http, 60, save_to=False)

    def test_the_next_version_carries_everything_over(self):
        http, d = self._load()
        d.add_step("another step")
        snap = d._snapshot()
        self.assertEqual(snap["needs"], ["tool_calling"])
        self.assertEqual(snap["implementation"]["preprocessors"], self.SNAP["implementation"]["preprocessors"])
        self.assertEqual(snap["implementation"]["tools"], self.SNAP["implementation"]["tools"])    # incl. server:
        self.assertEqual(snap["input"]["page_text"], {"type": "string", "derived": True, "sensitive": True})
        self.assertTrue(snap["input"]["secret"]["sensitive"])
        self.assertTrue(snap["output"]["summary"]["sensitive"])
        self.assertEqual(list(snap["input"]), ["url", "page_text", "secret"])        # original order
        self.assertEqual(snap["implementation"]["execution"], {"mode": "agent"})
        self.assertEqual(snap["implementation"]["flow"], ["f", "another step"])

    def test_a_derived_field_is_not_a_caller_input(self):
        http, d = self._load()
        d.submit()
        body = next(c for c in http.calls if c[1].endswith("/submit"))[2]
        self.assertNotIn("page_text", body["input_schema"]["properties"])             # the server never stores it there
        self.assertEqual(d.check_input({"url": "https://x"}), [])                      # and a caller need not send it

    def test_a_loaded_name_cannot_be_added_again(self):
        _, d = self._load()
        with self.assertRaises(DefinitionError):
            d.add_input("page_text")

    def test_printing_shows_the_parts_the_builder_has_no_call_for(self):
        _, d = self._load()
        text = str(d)
        for want in ("needs: [tool_calling]", "preprocessors:", "{ tool: web_fetch, from: url, into: page_text }",
                     "derived: true", "server: crm"):
            self.assertIn(want, text)

    def test_sensitive_added_through_the_builder_is_kept_too(self):
        d = FunctionDraft.create(_FakeDraftHttp(), name="Y", save_to=False)
        d.add_input("card", sensitive=True).add_output("o", required=True).add_step("s")
        self.assertTrue(d._snapshot()["input"]["card"]["sensitive"])

    def test_a_guard_on_a_derived_input_or_next_to_a_server_entry_is_not_a_false_alarm(self):
        _, d = self._load()
        d.add_tool("issue_refund", requires='input.page_text == "x"')
        d.submit()                       # input.page_text is declared; the server entry hides other tool names

    def test_it_survives_a_stopped_script(self):
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        http = _FakeDraftHttp(versions=[self._version()])
        d = FunctionDraft.load(http, 60, save_to=tmp / "d.yaml")
        d.add_step("another step")                                  # the draft file is written
        c = Client(token="fnp_test"); c._http = http; c.functions = _Functions(http)
        again = c.load_yaml(tmp / "d.yaml", save_to=False)          # a fresh run
        self.assertEqual(again._snapshot(), d._snapshot())

    def test_a_version_in_the_old_editor_format_is_refused_not_blanked(self):
        old = {"impl": "prompt", "sections": [{"type": "role", "content": "hi"}]}
        http = _FakeDraftHttp(versions=[self._version(snap=old)])
        with self.assertRaises(DefinitionError) as e:
            FunctionDraft.load(http, 60, save_to=False)
        self.assertIn("older editor format", str(e.exception))

    def test_functions_get_prints_them_too(self):
        http = _FakeDraftHttp(versions=[self._version()])
        http.fn = {"id": 60, "slug": "x", "name": "X", "description": "", "input_schema": {}, "output_schema": {}}
        text = str(_Functions(http).get(60))
        self.assertIn("needs: [tool_calling]", text)
        self.assertIn("preprocessors:", text)


class SubmitAsNewVersion(unittest.TestCase):
    """submit(new_version=True): branch without touching the version you started from."""

    def _http(self, statuses=("draft",)):
        vs = [{"id": 100 + i, "number": i, "status": st, "input_schema": {"type": "object", "properties": {"a": {"type": "string"}}},
               "output_schema": {"type": "object", "properties": {"o": {"type": "string"}}, "required": ["o"]},
               "snapshot": {"implementation": {"flow": [f"v{i} step"]}}} for i, st in enumerate(statuses, 1)]
        return _FakeDraftHttp(versions=vs)

    def test_a_draft_is_branched_not_overwritten(self):
        http = self._http(("draft",))
        d = FunctionDraft.load(http, 60, save_to=False)
        d.add_step("a new idea")
        v = d.submit("try this", new_version=True)
        self.assertEqual(v["number"], 2)                                   # a NEW version
        self.assertEqual(next(c for c in http.calls if c[:2] == ("POST", "/api/functions/60/versions"))[2]["parent_id"], 101)
        self.assertEqual(next(x for x in http.versions if x["number"] == 1)["snapshot"]["implementation"]["flow"], ["v1 step"])
        self.assertFalse([c for c in http.calls if c[0] == "PUT" or c[1].endswith("/submit")])

    def test_after_the_first_branch_plain_saves_update_it_and_the_flag_branches_again(self):
        http = self._http(("draft",))
        d = FunctionDraft.load(http, 60, save_to=False)
        d.add_step("x"); v2 = d.submit(new_version=True)
        d.add_step("y"); d.submit()                                        # plain: updates v2 in place
        self.assertEqual([c[1] for c in http.calls if c[0] == "PUT"], [f"/api/versions/{v2['id']}"])
        d.add_step("z"); v3 = d.submit(new_version=True)                   # branch again from v2
        self.assertEqual(v3["number"], 3)
        creates = [c for c in http.calls if c[:2] == ("POST", "/api/functions/60/versions")]
        self.assertEqual([c[2]["parent_id"] for c in creates], [101, v2["id"]])

    def test_branching_from_a_chosen_old_version_records_that_parent(self):
        http = self._http(("approved", "approved", "draft"))
        d = FunctionDraft.load(http, 60, version=1, save_to=False)
        d.add_step("x")
        v = d.submit(new_version=True)
        self.assertEqual(v["number"], 4)
        self.assertEqual(next(c for c in http.calls if c[:2] == ("POST", "/api/functions/60/versions"))[2]["parent_id"], 101)

    def test_without_the_flag_a_draft_is_still_updated_in_place(self):
        http = self._http(("draft",))
        d = FunctionDraft.load(http, 60, save_to=False)
        d.add_step("x"); d.submit()
        self.assertEqual([c[1] for c in http.calls if c[1].endswith("/submit")], ["/api/functions/60/submit"])
        self.assertFalse([c for c in http.calls if c[:2] == ("POST", "/api/functions/60/versions")])

    def test_a_brand_new_function_with_the_flag_just_makes_version_one(self):
        http = _FakeDraftHttp()
        d = FunctionDraft.create(http, name="Invoice Extractor", save_to=False)
        d.add_output("o", required=True).add_step("s")
        v = d.submit(new_version=True)
        self.assertEqual(v["number"], 1)
        self.assertNotIn("parent_id", next(c for c in http.calls if c[:2] == ("POST", "/api/functions/60/versions"))[2])


class RemovingParts(unittest.TestCase):
    """remove_input / remove_output / remove_step: shrink a loaded version before saving it as a new one."""

    def _loaded(self):
        vs = [{"id": 101, "number": 1, "status": "approved",
               "input_schema": {"type": "object", "required": ["name"], "properties": {
                   "name": {"type": "string"}, "rating": {"type": "integer"}, "email": {"type": "string"}}},
               "output_schema": {"type": "object", "required": ["o"], "properties": {"o": {"type": "string"}, "p": {"type": "string"}}},
               "snapshot": {"implementation": {"flow": ["one", "two", "three"]}}}]
        http = _FakeDraftHttp(versions=vs)
        return http, FunctionDraft.load(http, 60, save_to=False)

    def test_new_version_with_only_name(self):
        http, d = self._loaded()
        d.remove_input("rating").remove_input("email")
        v = d.submit("only name", new_version=True)
        self.assertEqual(v["number"], 2)
        sent = next(c for c in http.calls if c[:2] == ("POST", "/api/functions/60/versions"))[2]
        self.assertEqual(list(sent["input_schema"]["properties"]), ["name"])
        self.assertEqual(sent["input_schema"]["required"], ["name"])
        self.assertEqual(list(http.versions[0]["input_schema"]["properties"]), ["name", "rating", "email"])   # v1 untouched

    def test_removed_field_leaves_the_yaml_and_can_be_added_back(self):
        _, d = self._loaded()
        d.remove_input("rating")
        self.assertNotIn("rating", d.to_yaml())
        d.add_input("rating", type="integer")
        self.assertIn("rating", d.to_yaml())

    def test_removing_a_required_field_drops_it_from_required(self):
        _, d = self._loaded()
        d.remove_input("name")
        self.assertNotIn("required", d._input_schema)

    def test_removing_an_output(self):
        _, d = self._loaded()
        d.remove_output("p")
        self.assertEqual(list(d._output_schema["properties"]), ["o"])

    def test_unknown_field_is_an_error(self):
        _, d = self._loaded()
        with self.assertRaises(DefinitionError):
            d.remove_input("nope")
        with self.assertRaises(DefinitionError):
            d.remove_output("nope")

    def test_remove_step_by_number_or_text(self):
        _, d = self._loaded()
        d.remove_step(2)
        self.assertEqual(d._flow, ["one", "three"])
        d.remove_step("one")
        self.assertEqual(d._flow, ["three"])
        for bad in (0, 5, "missing", True):
            with self.assertRaises(DefinitionError):
                d.remove_step(bad)

    def test_submit_still_refuses_an_example_using_a_removed_field(self):
        _, d = self._loaded()
        d.add_example(input={"rating": 3}, output={"o": "x"})
        d.remove_input("rating")
        with self.assertRaises(DefinitionError) as cm:
            d.submit(new_version=True)
        self.assertIn("rating", str(cm.exception))

    def test_removing_the_last_step_is_caught_at_submit(self):
        _, d = self._loaded()
        d.remove_step(1).remove_step(1).remove_step(1)
        with self.assertRaises(DefinitionError) as cm:
            d.submit(new_version=True)
        self.assertIn("no steps", str(cm.exception))


class FreshStart(unittest.TestCase):
    """load_draft(..., fresh=True): same function, empty content, always a new version, changes listed."""

    def _http(self, status="draft"):
        v = {"id": 101, "number": 1, "status": status,
             "input_schema": {"type": "object", "required": ["name"], "properties": {
                 "name": {"type": "string"}, "rating": {"type": "integer"}}},
             "output_schema": {"type": "object", "required": ["o"], "properties": {"o": {"type": "string"}}},
             "snapshot": {"input": {"name": {"type": "string", "required": True}, "rating": {"type": "integer"}},
                          "output": {"o": {"type": "string", "required": True}},
                          "implementation": {"flow": ["old step"], "sections": {"role": "old role"}}}}
        return _FakeDraftHttp(versions=[v])

    def _quiet_submit(self, d, *a, **k):
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            v = d.submit(*a, **k)
        return v, buf.getvalue()

    def test_it_starts_empty_but_keeps_the_function(self):
        d = FunctionDraft.load(self._http(), 60, save_to=False, fresh=True)
        self.assertEqual(d.id, 60)
        self.assertEqual(d._input_schema, {}); self.assertEqual(d._flow, []); self.assertIsNone(d._sections["role"])

    def test_nothing_is_created_until_submit(self):
        http = self._http()
        FunctionDraft.load(http, 60, save_to=False, fresh=True).add_input("name")
        self.assertFalse([c for c in http.calls if c[0] in ("POST", "PUT")])

    def test_a_draft_is_not_overwritten_even_without_the_flag(self):
        http = self._http("draft")                      # the case that would lose data
        d = FunctionDraft.load(http, 60, save_to=False, fresh=True)
        d.add_input("name", required=True).add_output("o", required=True).add_step("s")
        v, _ = self._quiet_submit(d, "smaller")
        self.assertEqual(v["number"], 2)
        self.assertFalse([c for c in http.calls if c[0] == "PUT"])
        self.assertEqual(next(c for c in http.calls if c[:2] == ("POST", "/api/functions/60/versions"))[2]["parent_id"], 101)
        self.assertEqual(list(http.versions[0]["input_schema"]["properties"]), ["name", "rating"])    # v1 untouched

    def test_new_version_false_is_refused(self):
        d = FunctionDraft.load(self._http(), 60, save_to=False, fresh=True)
        d.add_output("o", required=True).add_step("s")
        with self.assertRaises(ValueError):
            d.submit(new_version=False)

    def test_changes_list_dropped_added_changed(self):
        d = FunctionDraft.load(self._http(), 60, save_to=False, fresh=True)
        d.add_input("name").add_input("company").add_output("o", required=True).add_step("old step")
        ch = d.changes()
        self.assertIn("- input rating", ch); self.assertIn("- prompt", ch); self.assertIn("+ input company", ch)
        self.assertIn("~ input name", ch)               # required -> optional
        self.assertNotIn("- step old step", ch)         # kept
        self.assertTrue(ch.index("- input rating") < ch.index("+ input company"))

    def test_submit_prints_the_changes_and_saves_the_new_version_only(self):
        d = FunctionDraft.load(self._http(), 60, save_to=False, fresh=True)
        d.add_input("company").add_output("o", required=True).add_step("s")
        _, out = self._quiet_submit(d, "x")
        self.assertIn("- input name", out); self.assertIn("+ input company", out)

    def test_after_saving_plain_saves_update_the_new_draft(self):
        http = self._http()
        d = FunctionDraft.load(http, 60, save_to=False, fresh=True)
        d.add_output("o", required=True).add_step("s")
        v2, _ = self._quiet_submit(d)
        d.add_step("t"); d.submit()
        self.assertEqual([c[1] for c in http.calls if c[0] == "PUT"], [f"/api/versions/{v2['id']}"])

    def test_the_check_at_save_still_runs(self):
        d = FunctionDraft.load(self._http(), 60, save_to=False, fresh=True)
        d.add_input("company")
        with self.assertRaises(DefinitionError) as cm:
            d.submit()
        self.assertIn("no output fields", str(cm.exception)); self.assertIn("no steps", str(cm.exception))

    def test_a_plain_load_lists_no_changes_until_you_change_it(self):
        d = FunctionDraft.load(self._http(), 60, save_to=False)
        self.assertEqual(d.changes(), [])
        d.remove_input("rating"); d.add_input("company")
        self.assertEqual(d.changes(), ["- input rating", "+ input company"])


class EveryVersionKeepsItsYaml(unittest.TestCase):
    """One working file per thing you edit; saving turns it into that version's copy instead of deleting it."""

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.drafts = self.tmp / "drafts"

    def _http(self, *statuses):
        vs = [{"id": 100 + i, "number": i, "status": st,
               "input_schema": {"type": "object", "properties": {"a": {"type": "string"}}},
               "output_schema": {"type": "object", "properties": {"o": {"type": "string"}}, "required": ["o"]},
               "snapshot": {"implementation": {"flow": [f"v{i} step"]}}} for i, st in enumerate(statuses, 1)]
        http = _FakeDraftHttp(versions=vs)
        http.fn = {"id": 60, "slug": "fn", "name": "Fn", "input_schema": {}, "output_schema": {}}
        return http

    def _load(self, http, **kw):
        return FunctionDraft.load(http, 60, drafts_folder=self.drafts, **kw)

    def test_each_thing_you_edit_has_its_own_file(self):
        http = self._http("approved", "approved", "draft")
        a = self._load(http, version=1); a.add_step("x")      # based on frozen v1
        b = self._load(http, version=3); b.add_step("y")      # editing draft v3
        c = self._load(http); c.add_step("z")                 # the latest (v3 again): the plain file
        names = sorted(p.name for p in self.drafts.glob("*.yaml"))
        self.assertIn("fn.from-v1.yaml", names)
        self.assertIn("fn.v3.yaml", names)
        self.assertIn("fn.yaml", names)
        self.assertIn("v1 step", (self.drafts / "fn.from-v1.yaml").read_text())
        self.assertNotIn("v1 step", (self.drafts / "fn.v3.yaml").read_text())     # they did not overwrite each other

    def test_saving_turns_the_working_file_into_that_versions_copy(self):
        http = self._http("approved")
        d = self._load(http, version=1)
        d.add_step("a new step")
        v = d.submit("from v1")
        self.assertFalse((self.drafts / "fn.from-v1.yaml").exists())               # the working file is gone...
        record = self.drafts.parent / "versions" / "fn" / f"v{v['number']}.yaml"   # ...and became the copy
        self.assertTrue(record.is_file(), record)
        text = record.read_text()
        self.assertIn(f"meta:\n  function: fn\n  version: {v['number']}\n", text)
        self.assertIn("a new step", text)

    def test_editing_a_draft_again_refreshes_its_copy_and_keeps_the_others(self):
        http = self._http("approved", "draft")
        d = self._load(http, version=2); d.add_step("first"); d.submit()
        v2 = self.drafts.parent / "versions" / "fn" / "v2.yaml"
        self.assertIn("first", v2.read_text())
        d.add_step("second"); d.submit()
        self.assertIn("second", v2.read_text())

    def test_copies_can_be_turned_off_and_then_the_file_is_just_removed(self):
        http = self._http("approved"); http.keep_versions = False
        d = self._load(http, version=1); d.add_step("x"); d.submit()
        self.assertFalse(list(self.drafts.glob("*.yaml")))
        self.assertFalse((self.drafts.parent / "versions").exists())

    def test_save_to_false_means_no_files_at_all(self):
        http = self._http("approved")
        d = FunctionDraft.load(http, 60, save_to=False, drafts_folder=self.drafts)
        d.add_step("x"); d.submit()
        self.assertFalse(self.drafts.exists() or (self.drafts.parent / "versions").exists())

    def test_where_the_copies_go(self):
        http = self._http("approved")
        d = FunctionDraft.load(http, 60, version=1, drafts_folder=self.tmp / "work")     # not called "drafts"
        d.add_step("x"); v = d.submit()
        self.assertTrue((self.tmp / "work" / "versions" / "fn" / f"v{v['number']}.yaml").is_file())
        http2 = self._http("approved"); http2.versions_folder = self.tmp / "history"
        d2 = FunctionDraft.load(http2, 60, version=1, drafts_folder=self.drafts)
        d2.add_step("x"); v2 = d2.submit()
        self.assertTrue((self.tmp / "history" / "fn" / f"v{v2['number']}.yaml").is_file())

    def test_load_yaml_finds_a_draft_by_name_whatever_it_is_called_and_asks_when_unsure(self):
        http = self._http("approved", "draft")
        http.listed = [{"id": 60, "slug": "fn"}]
        c = Client(token="fnp_test", drafts_folder=self.drafts); c._http = http; c.functions = _Functions(http, self.drafts)
        d = self._load(http, version=2); d.add_step("y")                         # writes fn.v2.yaml
        loaded = c.load_yaml("fn", save_to=False)
        self.assertIn("# editing draft v2", str(loaded))
        e = self._load(http, version=1); e.add_step("x")                         # a second draft: fn.from-v1.yaml
        with self.assertRaises(FileNotFoundError) as err:
            c.load_yaml("fn")
        self.assertIn("2 drafts match", str(err.exception))

    def test_a_saved_copy_can_be_loaded_to_carry_on_from_that_version(self):
        http = self._http("approved")
        http.listed = [{"id": 60, "slug": "fn"}]
        d = self._load(http, version=1); d.add_step("x"); v = d.submit()
        c = Client(token="fnp_test"); c._http = http; c.functions = _Functions(http)
        record = self.drafts.parent / "versions" / "fn" / f"v{v['number']}.yaml"
        again = c.load_yaml(record, save_to=False)
        self.assertIn(f"v{v['number']}", str(again).splitlines()[0])             # it knows which version it is

    def test_export_yaml_writes_every_version_from_the_server(self):
        http = self._http("approved", "approved", "draft")
        http.listed = [{"id": 60, "slug": "fn"}]
        http.versions_folder = self.tmp / "vers"
        c = Client(token="fnp_test"); c._http = http
        paths = c.export_yaml("fn")
        self.assertEqual([Path(p).name for p in paths], ["v1.yaml", "v2.yaml", "v3.yaml"])
        self.assertEqual(Path(paths[0]).parent, (self.tmp / "vers" / "fn").resolve())
        self.assertIn("from the server v2", (self.tmp / "vers" / "fn" / "v2.yaml").read_text())
        again = c.export_yaml(60, self.tmp / "elsewhere")
        self.assertTrue((self.tmp / "elsewhere" / "v3.yaml").is_file() and len(again) == 3)
        with self.assertRaises(FnaiError):
            c._http = self._http()
            c.export_yaml(60)


class PrintShowsYaml(unittest.TestCase):
    def test_draft_prints_yaml_and_what_is_missing(self):
        d = FunctionDraft.create(_FakeDraftHttp(), name="WT Reviewer")
        d.add_input("review_text", required=True).add_output("verdict", enum=["approve", "reject"])
        text = str(d)
        self.assertIn("name: WT Reviewer", text)
        self.assertIn("review_text: { type: string, required: true }", text)
        self.assertIn("no output field is marked required", text)
        self.assertIn("no prompt", text)
        d.add_output("verdict2", required=True).add_prompt("You review.").add_step("Decide.")
        self.assertEqual(d.missing()["required"], [])
        self.assertIn("Missing (required): nothing", str(d))

    def test_functions_create_returns_a_dict_that_prints_yaml(self):
        class H:
            def __call__(s, method, path, body=None, query=None):
                if method == "GET":
                    return []
                return {"id": 1, **body}
        rec = _Functions(H()).create(
            slug="s", name="S",
            input_schema={"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]},
            output_schema={"type": "object", "properties": {}})
        self.assertEqual(rec["id"], 1)
        self.assertIsInstance(rec, dict)
        self.assertIn("a: { type: string, required: true }", str(rec))
        self.assertIn("no output fields", str(rec))


class BackgroundRuns(unittest.TestCase):
    """Function.run_async / get_run / wait — against a scripted fake of the HTTP layer."""

    def _fn(self, replies):
        f = Function("rev", api_key="fnk_x", base_url="http://x")
        calls = []

        def fake(method, path, body=None, query=None):
            calls.append((method, path, body))
            r = replies.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        f._http = fake
        return f, calls

    def test_run_async_posts_to_the_async_path_and_returns_the_run_id(self):
        f, calls = self._fn([{"run_id": 7, "status": "running"}])
        self.assertEqual(f.run_async(title="t"), 7)
        self.assertEqual(calls, [("POST", "/v1/run/rev/async", {"input": {"title": "t"}})])

    def test_get_run_running_then_done_maps_to_a_run_result(self):
        f, _ = self._fn([{"run_id": 7, "status": "running"},
                         {"run_id": 7, "status": "success", "output": {"passed": True},
                          "meta": {"version": 2, "latency_ms": 5}}])
        running = f.get_run(7)
        self.assertEqual((running.status, running.output, running.ok), ("running", None, False))
        done = f.get_run(7)
        self.assertTrue(done.ok)
        self.assertEqual(done.output, {"passed": True})
        self.assertEqual(done.meta["run_id"], 7)

    def test_wait_polls_until_the_run_finishes(self):
        f, calls = self._fn([{"run_id": 7, "status": "running"}, {"run_id": 7, "status": "running"},
                             {"run_id": 7, "status": "failed", "error": "boom", "output": None}])
        result = f.wait(7, poll_every=0)
        self.assertEqual((result.status, result.error), ("failed", "boom"))
        self.assertEqual(len(calls), 3)

    def test_wait_gives_up_after_its_timeout_and_says_how_to_keep_checking(self):
        f, _ = self._fn([{"run_id": 7, "status": "running"}] * 50)
        with self.assertRaises(TimeoutError) as ctx:
            f.wait(7, timeout=0, poll_every=0)
        self.assertIn("get_run(7)", str(ctx.exception))

    def test_get_run_rejects_a_bad_id_before_any_request(self):
        f, calls = self._fn([])
        with self.assertRaises(ValueError):
            f.get_run(0)
        self.assertEqual(calls, [])

    def test_report_trace_posts_to_the_ingest_path_and_returns_the_answer(self):
        f, calls = self._fn([{"trace_id": "a" * 32, "duplicate": False}])
        out = f.report_trace({"trace_id": "a" * 32, "function_id": 3, "version_id": 4})
        self.assertEqual(out, {"trace_id": "a" * 32, "duplicate": False})
        self.assertEqual(calls[0][:2], ("POST", "/v1/traces"))

    def test_report_trace_fills_in_what_is_left_out(self):
        f, calls = self._fn([{"trace_id": "x", "duplicate": False}])
        f.report_trace({"function_id": 3, "version_id": 4})
        sent = calls[0][2]
        self.assertEqual((sent["schema_version"], sent["source"]), (1, "self_hosted"))
        self.assertEqual(len(sent["trace_id"]), 32)
        self.assertTrue(sent["created_at"].endswith("+00:00"))
        self.assertEqual((sent["function_id"], sent["version_id"]), (3, 4))

    def test_report_trace_keeps_what_the_caller_set(self):
        f, calls = self._fn([{"trace_id": "x", "duplicate": True}])
        f.report_trace({"trace_id": "b" * 32, "created_at": "2026-10-20T09:00:00Z", "function_id": 3})
        sent = calls[0][2]
        self.assertEqual((sent["trace_id"], sent["created_at"]), ("b" * 32, "2026-10-20T09:00:00Z"))

    def test_report_trace_lets_the_servers_refusal_through(self):
        f, _ = self._fn([FnaiError(422, "source: a reported trace must be 'self_hosted'")])
        with self.assertRaises(FnaiError) as ctx:
            f.report_trace({"function_id": 3})
        self.assertEqual(ctx.exception.status, 422)


if __name__ == "__main__":
    unittest.main()
