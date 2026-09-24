"""Stdlib-only tests for parse_prompt_file / create_function_from_doc.
No network involved — create_function_from_doc is tested against a fake
client double, matching the SDK's own zero-dependency philosophy."""
import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from fnai import (Client, FnaiError, PromptFileError, STARTER_YAML,
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


if __name__ == "__main__":
    unittest.main()
