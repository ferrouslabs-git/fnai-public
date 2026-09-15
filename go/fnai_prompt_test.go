package fnai

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
)

func writeTemp(t *testing.T, name, content string) string {
	t.Helper()
	p := filepath.Join(t.TempDir(), name)
	if err := os.WriteFile(p, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	return p
}

func TestParsePromptFile_FullDocument(t *testing.T) {
	path := writeTemp(t, "p.json", `{
		"role": "You are a helpful assistant.",
		"instructions": ["Do the first thing.", "Do the second thing."],
		"inputs": {"website_url": "string"},
		"outputs": {"confidence": {"type": "number", "required": false}}
	}`)
	doc, err := ParsePromptFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if len(doc.Sections) != 2 {
		t.Fatalf("expected 2 sections, got %d: %+v", len(doc.Sections), doc.Sections)
	}
	if doc.Sections[0]["type"] != "role" || doc.Sections[0]["content"] != "You are a helpful assistant." {
		t.Fatalf("unexpected role section: %+v", doc.Sections[0])
	}
	if len(doc.InputSchema["required"].([]string)) != 1 || doc.InputSchema["required"].([]string)[0] != "website_url" {
		t.Fatalf("unexpected input_schema.required: %+v", doc.InputSchema["required"])
	}
	if len(doc.OutputSchema["required"].([]string)) != 0 {
		t.Fatalf("expected confidence to be non-required, got: %+v", doc.OutputSchema["required"])
	}
}

func TestParsePromptFile_MissingRole(t *testing.T) {
	path := writeTemp(t, "p.json", `{"instructions": ["x"]}`)
	_, err := ParsePromptFile(path)
	if err == nil {
		t.Fatal("expected an error")
	}
	if _, ok := err.(*PromptFileError); !ok {
		t.Fatalf("expected *PromptFileError, got %T", err)
	}
}

func TestParsePromptFile_UnknownKeyNamesIt(t *testing.T) {
	path := writeTemp(t, "p.json", `{"role": "hi", "output_format": []}`)
	_, err := ParsePromptFile(path)
	if err == nil || !contains(err.Error(), "output_format") {
		t.Fatalf("expected error naming output_format, got: %v", err)
	}
}

func TestParsePromptFile_YamlRejectedWithHelpfulMessage(t *testing.T) {
	path := writeTemp(t, "p.yaml", "role: hi\n")
	_, err := ParsePromptFile(path)
	if err == nil || !contains(err.Error(), "ParsePromptDoc") {
		t.Fatalf("expected a message pointing at ParsePromptDoc, got: %v", err)
	}
}

func TestParsePromptFile_MissingFile(t *testing.T) {
	_, err := ParsePromptFile(filepath.Join(t.TempDir(), "nope.json"))
	if err == nil {
		t.Fatal("expected an error")
	}
}

func TestParsePromptFile_RealFile(t *testing.T) {
	// The actual prompt file built earlier this session — proves this SDK's
	// parser agrees with the Python/TypeScript ports on the same real input.
	// (JSON here since ParsePromptFile only reads .json; the .yaml original
	// parses identically via ParsePromptDoc once YAML-decoded.)
	path := writeTemp(t, "real.json", `{
		"inputs": {
			"website_url": {"type": "string", "description": "The canonical URL of the business website."},
			"fetched_pages": {"type": "string", "description": "A JSON-encoded list of pages already fetched."}
		},
		"outputs": {
			"company_name": {"type": "string"},
			"company_description": {"type": "string"},
			"industry": {"type": "string"},
			"key_sales_message_candidates": {"type": "array"},
			"confidence": {"type": "number"}
		},
		"role": "You extract a structured company profile from a website's public pages."
	}`)
	doc, err := ParsePromptFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if len(doc.InputSchema["properties"].(map[string]any)) != 2 {
		t.Fatalf("expected 2 input fields, got %+v", doc.InputSchema["properties"])
	}
	if len(doc.OutputSchema["properties"].(map[string]any)) != 5 {
		t.Fatalf("expected 5 output fields, got %+v", doc.OutputSchema["properties"])
	}
}

func contains(s, sub string) bool {
	return len(s) >= len(sub) && (func() bool {
		for i := 0; i+len(sub) <= len(s); i++ {
			if s[i:i+len(sub)] == sub {
				return true
			}
		}
		return false
	})()
}

// ── CreateFunctionFromDoc / CreateFunction: exercised against a local
// httptest server, same pattern as stub() in fnai_test.go. ───────────────

type capturedCalls struct {
	createBody      FunctionCreate
	versionSnapshot map[string]any
}

func newTestClient(t *testing.T) (*Client, *capturedCalls) {
	t.Helper()
	calls := &capturedCalls{}
	mux := http.NewServeMux()
	mux.HandleFunc("POST /api/functions", func(w http.ResponseWriter, r *http.Request) {
		json.NewDecoder(r.Body).Decode(&calls.createBody)
		w.WriteHeader(201)
		json.NewEncoder(w).Encode(FunctionOut{ID: 60, Slug: calls.createBody.Slug, Name: calls.createBody.Name})
	})
	mux.HandleFunc("POST /api/functions/60/versions", func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Snapshot map[string]any `json:"snapshot"`
		}
		json.NewDecoder(r.Body).Decode(&body)
		calls.versionSnapshot = body.Snapshot
		w.WriteHeader(201)
		json.NewEncoder(w).Encode(VersionOut{ID: 200, Number: 1})
	})
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return NewClient("fnp_test", WithBaseURL(srv.URL)), calls
}

func TestCreateFunctionFromDoc_UsesDocFieldsNotAFile(t *testing.T) {
	client, calls := newTestClient(t)
	doc := &PromptDoc{
		Sections:    []map[string]any{{"type": "role", "content": "hi"}},
		InputSchema: map[string]any{"type": "object", "properties": map[string]any{"a": map[string]any{"type": "string"}}},
	}
	fn, v, err := CreateFunctionFromDoc(client, CreateOpts{Slug: "s", Name: "N"}, doc)
	if err != nil {
		t.Fatal(err)
	}
	if fn.ID != 60 || v.ID != 200 {
		t.Fatalf("unexpected ids: fn=%+v v=%+v", fn, v)
	}
	if calls.createBody.InputSchema["type"] != "object" {
		t.Fatalf("input schema not forwarded: %+v", calls.createBody)
	}
	// snapshot round-tripped through real JSON, so nested arrays decode as
	// []any (not []map[string]any) regardless of what was marshaled.
	if sections, _ := calls.versionSnapshot["sections"].([]any); len(sections) != 1 {
		t.Fatalf("sections not forwarded: %+v", calls.versionSnapshot)
	}
}

func TestCreateFunction_TakesSectionsAndSchemasDirectly(t *testing.T) {
	client, calls := newTestClient(t)
	sections := []map[string]any{{"type": "role", "content": "manual"}}
	inSchema := map[string]any{"type": "object", "properties": map[string]any{"x": map[string]any{"type": "string"}}}
	fn, _, err := CreateFunction(client, CreateOpts{Slug: "manual", Name: "Manual"}, sections, inSchema, nil)
	if err != nil {
		t.Fatal(err)
	}
	if fn.ID != 60 {
		t.Fatalf("unexpected fn id: %d", fn.ID)
	}
	if calls.createBody.OutputSchema["type"] != "object" {
		t.Fatal("output schema should default to an empty object schema")
	}
}
