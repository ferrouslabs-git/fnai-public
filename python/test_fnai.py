"""Stdlib-only tests for parse_prompt_file / create_function_from_doc.
No network involved — create_function_from_doc is tested against a fake
client double, matching the SDK's own zero-dependency philosophy."""
import json
import tempfile
import unittest
from pathlib import Path

from fnai import (FnaiError, PromptFileError, STARTER_YAML, _Functions,
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

    def __init__(self, existing_functions=None, push_response=None, get_response=""):
        self.existing_functions = existing_functions or []
        self.push_response = push_response or {"id": 200, "number": 2, "artifact_hash": "abc"}
        self.get_response = get_response
        self.calls: list[tuple] = []

    def __call__(self, method, path, body=None, query=None):
        self.calls.append(("json", method, path, body))
        if method == "GET" and path == "/api/functions":
            return self.existing_functions
        if method == "POST" and path == "/api/functions":
            return {"id": 99, "slug": body["slug"], "name": body["name"]}
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
