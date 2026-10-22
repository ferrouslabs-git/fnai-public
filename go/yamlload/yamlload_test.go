package yamlload_test

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	fnai "github.com/ferrouslabs-git/fnai-public/go"
	"github.com/ferrouslabs-git/fnai-public/go/yamlload"
)

func client(t *testing.T) *fnai.Client {
	t.Helper()
	var funcs []map[string]any
	mux := http.NewServeMux()
	mux.HandleFunc("GET /api/functions", func(w http.ResponseWriter, r *http.Request) {
		json.NewEncoder(w).Encode(append([]map[string]any{}, funcs...))
	})
	mux.HandleFunc("POST /api/functions", func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		json.NewDecoder(r.Body).Decode(&body)
		fn := map[string]any{"id": 60 + len(funcs), "input_schema": map[string]any{}, "output_schema": map[string]any{}}
		for k, v := range body {
			fn[k] = v
		}
		funcs = append(funcs, fn)
		json.NewEncoder(w).Encode(fn)
	})
	mux.HandleFunc("POST /api/functions/{id}/submit", func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		json.NewDecoder(r.Body).Decode(&body)
		json.NewEncoder(w).Encode(map[string]any{"id": 500, "number": 1, "status": "draft", "snapshot": body["snapshot"]})
	})
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return fnai.NewClient("fnp_test", fnai.WithBaseURL(srv.URL))
}

func build(t *testing.T, c *fnai.Client, saveTo string) *fnai.FunctionDraft {
	t.Helper()
	d, err := c.Functions.New("Ticket Triage", fnai.SaveTo(saveTo))
	if err != nil {
		t.Fatal(err)
	}
	steps := []error{
		d.AddInput("ticket_text", map[string]any{"required": true}),
		d.AddInput("amount", map[string]any{"type": "number", "minimum": 0}),
		d.AddOutput("summary", map[string]any{"required": true, "length": map[string]any{"unit": "tokens", "target": 60, "range": []any{40, 80}}}),
		d.AddOutput("urgency", map[string]any{"type": "integer", "minimum": 1, "maximum": 5, "required": true}),
		d.AddPrompt("You triage tickets."),
		d.AddSection("definitions", "Urgency: 1 to 5."),
		d.AddSection("constraints", "Never invent facts."),
		d.AddSection("edge_cases", "Empty ticket: urgency 1."),
		d.AddStep("Read."), d.AddStep("Score."),
		d.AddExample(fnai.ExampleOpts{Input: map[string]any{"ticket_text": "crash"}, Output: map[string]any{"summary": "crash", "urgency": 4}}),
		d.AddExample(fnai.ExampleOpts{Polarity: "negative", Output: map[string]any{"summary": "idk", "urgency": 3}, Note: "too vague: names no symptom"}),
		d.AddTool("human_approval", fnai.ToolOptions{MaxCalls: 2, Config: map[string]any{"prompt": "Approve?"}}),
		d.AddRubric("Names the problem."),
		d.SetMemory(fnai.Memory{Per: "caller", Remember: []string{"the plan"}, Never: []string{"card numbers"}}),
	}
	for i, err := range steps {
		if err != nil {
			t.Fatalf("step %d: %v", i, err)
		}
	}
	return d
}

func TestLoad_ReadsARealYAMLFileBackIntoTheSameFunction(t *testing.T) {
	c := client(t)
	dir := t.TempDir()
	file := filepath.Join(dir, "d.yaml")
	d := build(t, c, file)

	loaded, err := yamlload.Load(c, file, fnai.NoDraftFile())
	if err != nil {
		t.Fatal(err)
	}
	if loaded.ToYAML() != d.ToYAML() {
		t.Fatalf("loaded function differs:\n--- original\n%s\n--- loaded\n%s", d.ToYAML(), loaded.ToYAML())
	}
	if loaded.Slug != d.Slug {
		t.Fatalf("should continue the same server function: %s vs %s", loaded.Slug, d.Slug)
	}
}

func TestLoad_ByNameFromTheDraftsFolder(t *testing.T) {
	dir := t.TempDir()
	var url string
	{
		mux := http.NewServeMux()
		mux.HandleFunc("GET /api/functions", func(w http.ResponseWriter, r *http.Request) { w.Write([]byte("[]")) })
		mux.HandleFunc("POST /api/functions", func(w http.ResponseWriter, r *http.Request) {
			var body map[string]any
			json.NewDecoder(r.Body).Decode(&body)
			json.NewEncoder(w).Encode(map[string]any{"id": 60, "slug": body["slug"], "name": body["name"]})
		})
		srv := httptest.NewServer(mux)
		t.Cleanup(srv.Close)
		url = srv.URL
	}
	c := fnai.NewClient("fnp_test", fnai.WithBaseURL(url), fnai.WithDraftsFolder(dir))
	d, err := c.Functions.New("Other Fn")
	if err != nil {
		t.Fatal(err)
	}
	if err := d.AddInput("a", nil); err != nil {
		t.Fatal(err)
	}
	loaded, err := yamlload.Load(c, "Other Fn", fnai.NoDraftFile())
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(loaded.ToYAML(), "a: { type: string }") {
		t.Fatalf("loaded by name should have the input:\n%s", loaded.ToYAML())
	}
}

func TestLoad_ACopyIsNeverOverwrittenOrDeleted(t *testing.T) {
	c := client(t)
	dir := t.TempDir()
	d := build(t, c, filepath.Join(dir, "d.yaml"))
	copy, err := d.SaveYAML(filepath.Join(dir, "copy.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	before, _ := os.ReadFile(copy)
	loaded, err := yamlload.Load(c, copy, fnai.SaveTo(filepath.Join(dir, "work.yaml")))
	if err != nil {
		t.Fatal(err)
	}
	loaded.AddStep("Another step.")
	if _, err := loaded.Submit("x"); err != nil {
		t.Fatal(err)
	}
	after, _ := os.ReadFile(copy)
	if string(before) != string(after) {
		t.Fatal("the copy that was loaded must not change")
	}
}

func TestLoad_BadFilesAndMissingFilesAreClear(t *testing.T) {
	c := client(t)
	dir := t.TempDir()
	write := func(name, text string) string {
		p := filepath.Join(dir, name)
		os.WriteFile(p, []byte(text), 0o644)
		return p
	}
	if _, err := yamlload.Load(c, write("bad.yaml", "name: X\ninput:\n  a: { type: text }\n"), fnai.NoDraftFile()); err == nil || !strings.Contains(err.Error(), "type 'text' is not valid") {
		t.Fatalf("expected the same checks as the Add calls, got %v", err)
	}
	if _, err := yamlload.Load(c, write("broken.yaml", "name: [unclosed\n"), fnai.NoDraftFile()); err == nil || !strings.Contains(err.Error(), "not valid YAML") {
		t.Fatalf("expected a YAML error, got %v", err)
	}
	if _, err := yamlload.Load(c, "no such draft"); err == nil || !strings.Contains(err.Error(), "no YAML file") {
		t.Fatalf("expected a not-found error, got %v", err)
	}
}

func TestLoad_AFileWithNeedsPreprocessorsDerivedAndServerToolsRoundTrips(t *testing.T) {
	c := client(t)
	file := filepath.Join(t.TempDir(), "rich.yaml")
	text := `meta:
  function: rich
name: Rich
input:
  url: { type: string, required: true }
  page_text: { type: string, derived: true }
  card: { type: string, sensitive: true }
output:
  summary: { type: string, required: true }
implementation:
  execution: { mode: single_shot }
  preprocessors:
    - { tool: web_fetch, from: url, into: page_text }
  flow:
    - "Summarize the page."
  tools:
    - { server: crm, max_calls: 20 }
needs: [long_context]
`
	os.WriteFile(file, []byte(text), 0o644)
	d, err := yamlload.Load(c, file, fnai.NoDraftFile())
	if err != nil {
		t.Fatal(err)
	}
	out := d.ToYAML()
	for _, want := range []string{"needs: [long_context]", "preprocessors:", "{ tool: web_fetch, from: url, into: page_text }", "derived: true", "sensitive: true", "server: crm"} {
		if !strings.Contains(out, want) {
			t.Fatalf("loading should keep %q:\n%s", want, out)
		}
	}
}
