package fnai

// The step-by-step builder: checks as you add, YAML print, the local draft file, SaveYAML, rubric and
// memory, tools, upload, deploy curl, run with findings, compare. Uses a real httptest server, like the
// other tests in this package.

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"testing"
)

type fakeAPI struct {
	mu          sync.Mutex
	functions   []map[string]any
	versions    map[int][]map[string]any
	submits     []map[string]any
	tools       []map[string]any
	exps        map[int]map[string]any
	form        map[string]string
	fileName    string
	creates     int              // POST /api/functions calls
	failSave    bool             // answer submit with a 422
	frozenOnPut bool             // answer a version edit with a 409 (it was deployed meanwhile)
	puts        []string         // PUT /api/versions/{id} paths
	posts       []map[string]any // POST /api/functions/{id}/versions bodies
	clash       bool             // answer create with a 409
}

func newFakeAPI(t *testing.T) (*fakeAPI, *Client) {
	t.Helper()
	f := &fakeAPI{versions: map[int][]map[string]any{}, exps: map[int]map[string]any{},
		tools: []map[string]any{{"name": "ocr", "status": "active"}, {"name": "web_search", "status": "active"},
			{"name": "human_approval", "status": "active"}, {"name": "issue_refund", "status": "active"}}}
	mux := http.NewServeMux()
	reply := func(w http.ResponseWriter, v any) { json.NewEncoder(w).Encode(v) }
	idOf := func(r *http.Request) int { var n int; fmt.Sscan(r.PathValue("id"), &n); return n }
	mux.HandleFunc("POST /api/functions", func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		json.NewDecoder(r.Body).Decode(&body)
		f.mu.Lock()
		defer f.mu.Unlock()
		if f.clash {
			w.WriteHeader(409)
			json.NewEncoder(w).Encode(map[string]any{"detail": "slug already exists"})
			return
		}
		f.creates++
		fn := map[string]any{"id": 60 + len(f.functions), "input_schema": map[string]any{}, "output_schema": map[string]any{}}
		for k, v := range body {
			fn[k] = v
		}
		f.functions = append(f.functions, fn)
		reply(w, fn)
	})
	mux.HandleFunc("GET /api/functions", func(w http.ResponseWriter, r *http.Request) {
		f.mu.Lock()
		defer f.mu.Unlock()
		reply(w, append([]map[string]any{}, f.functions...))
	})
	mux.HandleFunc("GET /api/functions/{id}", func(w http.ResponseWriter, r *http.Request) {
		f.mu.Lock()
		defer f.mu.Unlock()
		for _, fn := range f.functions {
			if fn["id"] == idOf(r) {
				reply(w, fn)
				return
			}
		}
		w.WriteHeader(404)
	})
	mux.HandleFunc("GET /api/functions/{id}/versions", func(w http.ResponseWriter, r *http.Request) {
		f.mu.Lock()
		defer f.mu.Unlock()
		v := f.versions[idOf(r)]
		if v == nil {
			v = []map[string]any{}
		}
		reply(w, v)
	})
	mux.HandleFunc("POST /api/functions/{id}/submit", func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		json.NewDecoder(r.Body).Decode(&body)
		f.mu.Lock()
		defer f.mu.Unlock()
		if f.failSave {
			w.WriteHeader(422)
			json.NewEncoder(w).Encode(map[string]any{"detail": "server said no"})
			return
		}
		f.submits = append(f.submits, body)
		id := idOf(r)
		v := map[string]any{"id": 500 + len(f.versions[id]), "number": len(f.versions[id]) + 1, "status": "draft",
			"snapshot": body["snapshot"], "input_schema": body["input_schema"], "output_schema": body["output_schema"]}
		f.versions[id] = append(f.versions[id], v)
		reply(w, v)
	})
	mux.HandleFunc("PUT /api/versions/{id}", func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		json.NewDecoder(r.Body).Decode(&body)
		f.mu.Lock()
		defer f.mu.Unlock()
		if f.frozenOnPut {
			w.WriteHeader(409)
			json.NewEncoder(w).Encode(map[string]any{"detail": "versions are immutable once deployed"})
			return
		}
		f.puts = append(f.puts, r.URL.Path)
		for _, list := range f.versions {
			for _, v := range list {
				if v["id"] == idOf(r) {
					for k, val := range body {
						v[k] = val
					}
					reply(w, v)
					return
				}
			}
		}
		w.WriteHeader(404)
	})
	mux.HandleFunc("POST /api/functions/{id}/versions", func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		json.NewDecoder(r.Body).Decode(&body)
		f.mu.Lock()
		defer f.mu.Unlock()
		f.posts = append(f.posts, body)
		id := idOf(r)
		n := 0
		for _, v := range f.versions[id] {
			if num := int(v["number"].(int)); num > n {
				n = num
			}
		}
		v := map[string]any{"id": 900 + len(f.versions[id]), "number": n + 1, "status": "draft"}
		for k, val := range body {
			v[k] = val
		}
		f.versions[id] = append(f.versions[id], v)
		reply(w, v)
	})
	mux.HandleFunc("GET /api/versions/{id}/yaml", func(w http.ResponseWriter, r *http.Request) {
		n := idOf(r) - 100
		fmt.Fprintf(w, "meta:\n  function: fn\n  version: %d\nname: from the server v%d\n", n, n)
	})
	mux.HandleFunc("GET /api/tools", func(w http.ResponseWriter, r *http.Request) { reply(w, f.tools) })
	mux.HandleFunc("POST /api/tools", func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		json.NewDecoder(r.Body).Decode(&body)
		body["id"] = 1
		reply(w, body)
	})
	mux.HandleFunc("POST /api/functions/{id}/deploy", func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		json.NewDecoder(r.Body).Decode(&body)
		reply(w, map[string]any{"id": 9, "status": "active", "version_id": body["version_id"]})
	})
	mux.HandleFunc("POST /api/datasets/{id}/upload", func(w http.ResponseWriter, r *http.Request) {
		r.ParseMultipartForm(1 << 20)
		f.form = map[string]string{"mapping": r.FormValue("mapping"), "expected_mapping": r.FormValue("expected_mapping")}
		_, hdr, _ := r.FormFile("file")
		if hdr != nil {
			f.fileName = hdr.Filename
		}
		reply(w, map[string]any{"added": 1, "skipped": 0, "errors": []any{}})
	})
	mux.HandleFunc("POST /api/experiments/{id}/run", func(w http.ResponseWriter, r *http.Request) {
		reply(w, map[string]any{"id": idOf(r), "status": "completed"})
	})
	mux.HandleFunc("GET /api/experiments/{id}/findings", func(w http.ResponseWriter, r *http.Request) {
		reply(w, []map[string]any{{"id": 1, "title": "t"}})
	})
	mux.HandleFunc("GET /api/experiments/{id}", func(w http.ResponseWriter, r *http.Request) { reply(w, f.exps[idOf(r)]) })
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return f, NewClient("fnp_test", WithBaseURL(srv.URL))
}

func newDraft(t *testing.T, c *Client, name string, opts ...DraftOption) *FunctionDraft {
	t.Helper()
	d, err := c.Functions.New(name, append([]DraftOption{NoDraftFile()}, opts...)...)
	if err != nil {
		t.Fatal(err)
	}
	return d
}

func wantDefErr(t *testing.T, err error, substr string) {
	t.Helper()
	var de *DefinitionError
	if err == nil {
		t.Fatalf("expected a DefinitionError containing %q, got no error", substr)
	}
	if e, ok := err.(*DefinitionError); ok {
		de = e
	}
	if de == nil || !strings.Contains(de.Error(), substr) {
		t.Fatalf("expected a DefinitionError containing %q, got %v", substr, err)
	}
}

func must(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatal(err)
	}
}

func TestBuilder_EveryBadFieldIsRejectedAsItIsAdded(t *testing.T) {
	_, c := newFakeAPI(t)
	d := newDraft(t, c, "X")
	cases := []struct {
		name string
		spec map[string]any
		want string
		out  bool
	}{
		{"a", map[string]any{"type": "text"}, "type 'text' is not valid", false},
		{"b", map[string]any{"minimun": 1}, "did you mean 'minimum'", false},
		{"c", map[string]any{"type": "string", "minimum": 1}, "needs type number or integer", false},
		{"d", map[string]any{"type": "integer", "minimum": 10, "maximum": 5}, "greater than maximum", false},
		{"e", map[string]any{"type": "string", "enum": []any{1, 2}}, "not of type string", false},
		{"f", map[string]any{"type": "string", "pattern": "("}, "not a valid regular expression", false},
		{"g", map[string]any{"type": "string", "format": "phone"}, "format 'phone' is not valid", false},
		{"h", map[string]any{"type": "array"}, "arrays need 'items'", false},
		{"i", map[string]any{"type": "string", "length": map[string]any{"unit": "tokens"}}, "only valid on output", false},
		{"j", map[string]any{"type": "file"}, "file fields need media", false},
		{"k", map[string]any{"type": "string", "min_items": 1}, "needs type array", false},
		{"l", map[string]any{"type": "file", "media": "document"}, "input-only", true},
	}
	for _, tc := range cases {
		var err error
		if tc.out {
			err = d.AddOutput(tc.name, tc.spec)
		} else {
			err = d.AddInput(tc.name, tc.spec)
		}
		wantDefErr(t, err, tc.want)
	}
	must(t, d.AddInput("a", nil))
	wantDefErr(t, d.AddInput("a", nil), "already defined")
	must(t, d.AddOutput("summary", map[string]any{"required": true, "length": map[string]any{"unit": "tokens", "target": 60, "range": []any{40, 80}}}))
}

func TestBuilder_RequiredListAndCheckInput(t *testing.T) {
	_, c := newFakeAPI(t)
	d := newDraft(t, c, "X")
	must(t, d.AddInput("text", map[string]any{"required": true}))
	must(t, d.AddInput("rating", map[string]any{"type": "integer", "minimum": 1, "maximum": 5}))
	must(t, d.AddInput("email", map[string]any{"format": "email"}))
	must(t, d.AddInput("tags", map[string]any{"type": "array", "items": map[string]any{"type": "string"}, "min_items": 1}))
	if p := d.CheckInput(map[string]any{"text": "hi", "rating": 3, "email": "a@b.co", "tags": []any{"x"}}); len(p) != 0 {
		t.Fatalf("expected no problems, got %v", p)
	}
	got := strings.Join(d.CheckInput(map[string]any{"rating": 9, "email": "nope", "tags": []any{}}), " | ")
	for _, want := range []string{"missing required field 'text'", "field 'rating' must be at most 5, got 9", "field 'email' is not a valid email", "field 'tags' must have at least 1 items, got 0"} {
		if !strings.Contains(got, want) {
			t.Fatalf("missing %q in %q", want, got)
		}
	}
}

func TestBuilder_ExamplesToolsGuardsRubricMemory(t *testing.T) {
	_, c := newFakeAPI(t)
	d := newDraft(t, c, "X")
	wantDefErr(t, d.AddExample(ExampleOpts{Polarity: "negative", Output: map[string]any{"o": "bad"}}), "needs a note")
	wantDefErr(t, d.AddExample(ExampleOpts{Polarity: "negative", Note: "why"}), "needs output or bad value")
	wantDefErr(t, d.AddExample(ExampleOpts{Input: map[string]any{"a": 1}}), "needs both input and output")
	wantDefErr(t, d.AddExample(ExampleOpts{Polarity: "maybe", Input: map[string]any{}, Output: map[string]any{}}), "polarity must be")
	must(t, d.AddExample(ExampleOpts{Polarity: "negative", Output: map[string]any{"o": "bad"}, Note: "too vague"}))
	wantDefErr(t, d.AddTool("t", ToolOptions{Effect: "dangerous"}), "effect must be one of")
	wantDefErr(t, d.AddTool("t", ToolOptions{Requires: "no operator here"}), "does not parse")
	must(t, d.AddTool("t", ToolOptions{}))
	wantDefErr(t, d.AddTool("t", ToolOptions{}), "already added")
	must(t, d.AddRubric("Names the problem."))
	wantDefErr(t, d.AddRubric("  "), "can't be empty")
	wantDefErr(t, d.AddRubric("Names the problem."), "already added")
	wantDefErr(t, d.SetMemory(Memory{Per: "everyone"}), "per must be")
	must(t, d.SetMemory(Memory{Per: "caller", Never: []string{"card numbers"}}))
	must(t, d.SetMemory(Memory{Per: "space"})) // a second call replaces the first
	y := d.ToYAML()
	if !strings.Contains(y, "rubric:\n  - \"Names the problem.\"") || !strings.HasSuffix(y, "memory:\n  per: space\n") {
		t.Fatalf("unexpected YAML:\n%s", y)
	}
}

func TestBuilder_PrintShowsYamlAndWhatIsMissing(t *testing.T) {
	_, c := newFakeAPI(t)
	d := newDraft(t, c, "Ticket Triage")
	must(t, d.AddInput("ticket_text", map[string]any{"required": true}))
	must(t, d.AddOutput("summary", map[string]any{"required": true}))
	must(t, d.AddOutput("urgency", map[string]any{"type": "integer", "minimum": 1, "maximum": 5, "required": true}))
	if m := d.Missing(); len(m.Required) != 1 || !strings.Contains(m.Required[0], "no steps") {
		t.Fatalf("expected one required item (no steps), got %+v", m)
	}
	must(t, d.AddPrompt("You triage tickets."))
	must(t, d.AddStep("Read."))
	must(t, d.AddStep("Score."))
	text := fmt.Sprint(d)
	for _, want := range []string{"name: Ticket Triage", "ticket_text: { type: string, required: true }",
		"urgency: { type: integer, required: true, minimum: 1, maximum: 5 }", "flow:\n    - \"Read.\"",
		"role: \"You triage tickets.\"", "# Missing (required): nothing", "no examples"} {
		if !strings.Contains(text, want) {
			t.Fatalf("missing %q in:\n%s", want, text)
		}
	}
}

func TestBuilder_SubmitListsEveryProblemAndSendsAV2Document(t *testing.T) {
	api, c := newFakeAPI(t)
	d := newDraft(t, c, "X")
	_, err := d.Submit("")
	de, ok := err.(*DefinitionError)
	if !ok || de.Problems[0] != "no output fields — AddOutput(...)" || de.Problems[1] != "no steps — AddStep(...)" {
		t.Fatalf("unexpected: %v", err)
	}
	must(t, d.AddOutput("o", map[string]any{"required": true}))
	must(t, d.AddStep("s"))
	must(t, d.AddTool("web_serch", ToolOptions{}))
	_, err = d.Submit("")
	wantDefErr(t, err, "did you mean 'web_search'")

	d2 := newDraft(t, c, "Y")
	must(t, d2.AddInput("amount", map[string]any{"type": "number"}))
	must(t, d2.AddOutput("o", map[string]any{"required": true}))
	must(t, d2.AddStep("s"))
	must(t, d2.AddTool("human_approval", ToolOptions{}))
	must(t, d2.AddTool("issue_refund", ToolOptions{Requires: "human_approval.returned.approved == true && input.amount > 100", MaxCalls: 1, Effect: "side_effecting"}))
	_, err = d2.Submit("")
	must(t, err)
	snap := api.submits[len(api.submits)-1]["snapshot"].(map[string]any)
	tools := snap["implementation"].(map[string]any)["tools"].([]any)
	if tools[1].(map[string]any)["requires"] != "human_approval.returned.approved == true && input.amount > 100" {
		t.Fatalf("guard not sent: %+v", tools)
	}

	d3 := newDraft(t, c, "Z")
	must(t, d3.AddOutput("o", map[string]any{"required": true}))
	must(t, d3.AddStep("s"))
	must(t, d3.AddTool("issue_refund", ToolOptions{Requires: "human_approval.called == true"}))
	_, err = d3.Submit("")
	wantDefErr(t, err, "doesn't declare")

	d4 := newDraft(t, c, "W")
	must(t, d4.AddInput("a", nil))
	must(t, d4.AddOutput("o", map[string]any{"required": true}))
	must(t, d4.AddStep("s"))
	must(t, d4.AddExample(ExampleOpts{Input: map[string]any{"zzz": 1}, Output: map[string]any{"o": "x"}}))
	_, err = d4.Submit("")
	wantDefErr(t, err, "'zzz' is not declared")
}

func TestBuilder_SubmitSendsRequiredAsAListAndFileFieldsAsHandles(t *testing.T) {
	api, c := newFakeAPI(t)
	d := newDraft(t, c, "Invoice Extractor")
	must(t, d.AddInput("document", map[string]any{"type": "file", "media": "document", "required": true}))
	must(t, d.AddOutput("total", map[string]any{"type": "number", "required": true}))
	must(t, d.AddStep("Read the invoice."))
	must(t, d.AddTool("ocr", ToolOptions{}))
	_, err := d.Submit("first")
	must(t, err)
	body := api.submits[0]
	in := body["input_schema"].(map[string]any)
	if !reflect.DeepEqual(in["required"], []any{"document"}) {
		t.Fatalf("required should be a list on the schema: %+v", in)
	}
	doc := in["properties"].(map[string]any)["document"].(map[string]any)
	if doc["type"] != "string" || doc["x-media"] != "document" {
		t.Fatalf("file field not projected: %+v", doc)
	}
	snap := body["snapshot"].(map[string]any)
	if snap["output"].(map[string]any)["total"].(map[string]any)["required"] != true {
		t.Fatalf("snapshot output not in field form: %+v", snap["output"])
	}
	if snap["implementation"].(map[string]any)["execution"].(map[string]any)["mode"] != "agent" {
		t.Fatal("a function with a tool should be an agent")
	}
}

func TestBuilder_LoadDraftBringsBackSchemasStepsRubricAndMemory(t *testing.T) {
	api, c := newFakeAPI(t)
	api.functions = []map[string]any{{"id": 60, "slug": "x", "name": "X", "input_schema": map[string]any{}, "output_schema": map[string]any{}}}
	api.versions[60] = []map[string]any{{"id": 500, "number": 1, "status": "draft",
		"input_schema":  map[string]any{"type": "object", "properties": map[string]any{"a": map[string]any{"type": "string"}}, "required": []any{"a"}},
		"output_schema": map[string]any{"type": "object", "properties": map[string]any{"o": map[string]any{"type": "string"}}, "required": []any{"o"}},
		"snapshot": map[string]any{"rubric": []any{"r1"}, "memory": map[string]any{"per": "space"},
			"implementation": map[string]any{"execution": map[string]any{"mode": "single_shot"}, "flow": []any{"Do it."}, "sections": map[string]any{"role": "r"}}}}}
	d, err := c.LoadDraft(60, NoDraftFile())
	must(t, err)
	must(t, d.AddExample(ExampleOpts{Input: map[string]any{"a": "x"}, Output: map[string]any{"o": "y"}}))
	_, err = d.Submit("")
	must(t, err)
	body := api.submits[0]
	snap := body["snapshot"].(map[string]any)
	if !reflect.DeepEqual(snap["rubric"], []any{"r1"}) || !reflect.DeepEqual(snap["implementation"].(map[string]any)["flow"], []any{"Do it."}) {
		t.Fatalf("rubric/flow lost: %+v", snap)
	}
	must(t, d.AddTool("web_search", ToolOptions{}))
	if !strings.Contains(d.ToYAML(), "mode: agent") {
		t.Fatal("a loaded single_shot function should flip to agent once a tool is added")
	}
}

func TestDraftFile_CreatedAndFollowsEveryChange(t *testing.T) {
	_, c := newFakeAPI(t)
	dir := t.TempDir()
	file := filepath.Join(dir, "triage.yaml")
	d, err := c.Functions.New("Ticket Triage", SaveTo(file))
	must(t, err)
	read := func() string { b, _ := os.ReadFile(file); return string(b) }
	if !strings.Contains(read(), "name: Ticket Triage") || !strings.Contains(read(), "meta:\n  function: ticket_triage") {
		t.Fatalf("draft file not created: %q", read())
	}
	must(t, d.AddInput("ticket_text", map[string]any{"required": true}))
	if !strings.Contains(read(), "ticket_text: { type: string, required: true }") {
		t.Fatal("file did not follow AddInput")
	}
	changes := []func() error{
		func() error { return d.AddPrompt("You triage.") }, func() error { return d.AddStep("Read.") },
		func() error { return d.AddRubric("r") }, func() error { return d.SetMemory(Memory{Per: "space"}) },
		func() error { return d.AddTool("t", ToolOptions{}) }, func() error { return d.AddSection("constraints", "c") },
		func() error {
			return d.AddExample(ExampleOpts{Input: map[string]any{"ticket_text": "x"}, Output: map[string]any{}})
		},
	}
	for i, change := range changes {
		before := read()
		must(t, change())
		if before == read() {
			t.Fatalf("change %d did not update the file", i)
		}
	}
	before := read()
	wantDefErr(t, d.AddInput("ticket_text", nil), "already defined")
	if read() != before {
		t.Fatal("a rejected call must leave the file alone")
	}
}

func TestDraftFile_WhereItGoes(t *testing.T) {
	dir := t.TempDir()
	_, c := newFakeAPI(t)
	exists := func(p string) bool { _, err := os.Stat(p); return err == nil }
	a, _ := c.Functions.New("One", SaveTo(filepath.Join(dir, "mine")))
	if !exists(filepath.Join(dir, "mine", a.Slug+".yaml")) {
		t.Fatal("a folder: the file should be named after the function")
	}
	c2 := NewClient("fnp_test", WithBaseURL(c.h.base), WithDraftsFolder(filepath.Join(dir, "all")))
	b, _ := c2.Functions.New("Two")
	if !exists(filepath.Join(dir, "all", b.Slug+".yaml")) {
		t.Fatal("WithDraftsFolder should set the folder")
	}
	c3 := NewClient("fnp_test", WithBaseURL(c.h.base), WithDraftsFolder(filepath.Join(dir, "all2")))
	_, _ = c3.Functions.New("Three", SaveTo(filepath.Join(dir, "x.yaml")))
	if !exists(filepath.Join(dir, "x.yaml")) || exists(filepath.Join(dir, "all2")) {
		t.Fatal("SaveTo should win over WithDraftsFolder")
	}
	d, _ := c.Functions.New("Four")
	if !exists(filepath.Join(os.Getenv("FNAI_DRAFTS_DIR"), d.Slug+".yaml")) {
		t.Fatal("the default should be the drafts folder from the environment")
	}
	e, _ := c.Functions.New("Five", NoDraftFile())
	if e.DraftPath != "" {
		t.Fatal("NoDraftFile should turn the file off")
	}
}

func TestDraftFile_SubmitDeletesItButASaveYAMLCopyStays(t *testing.T) {
	_, c := newFakeAPI(t)
	dir := t.TempDir()
	file, copy := filepath.Join(dir, "draft.yaml"), filepath.Join(dir, "copy.yaml")
	d, err := c.Functions.New("Ticket Triage", SaveTo(file))
	must(t, err)
	must(t, d.AddInput("a", nil))
	must(t, d.AddOutput("o", map[string]any{"required": true}))
	must(t, d.AddStep("s"))
	if _, err := d.SaveYAML(copy); err != nil {
		t.Fatal(err)
	}
	_, err = d.Submit("")
	must(t, err)
	if _, err := os.Stat(file); err == nil {
		t.Fatal("the draft file should be deleted after Submit")
	}
	if _, err := os.Stat(copy); err != nil {
		t.Fatal("the SaveYAML copy should stay")
	}
	p, err := d.SaveYAML(filepath.Join(dir, "deep", "er", "f.yaml"))
	must(t, err)
	if _, err := os.Stat(p); err != nil {
		t.Fatal("SaveYAML should create folders")
	}
	p2, _ := d.SaveYAML(filepath.Join(dir, "folder"))
	if filepath.Base(p2) != d.Slug+".yaml" {
		t.Fatalf("a folder should get a file named after the function, got %s", p2)
	}
}

func TestDraftFile_AWriteProblemNeverStopsTheProgram(t *testing.T) {
	_, c := newFakeAPI(t)
	dir := t.TempDir()
	blocker := filepath.Join(dir, "blocker")
	os.WriteFile(blocker, []byte("a file, not a folder"), 0o644)
	d, err := c.Functions.New("X", SaveTo(filepath.Join(blocker, "x.yaml")))
	must(t, err) // creating the function still works
	must(t, d.AddInput("a", nil))
	must(t, d.AddInput("b", nil)) // and so do later changes
	if _, err := d.SaveYAML(filepath.Join(blocker, "y.yaml")); err == nil || !strings.Contains(err.Error(), "couldn't write") {
		t.Fatalf("an explicit SaveYAML may fail loudly, got %v", err)
	}
}

func TestLoadDocument_GivesBackTheSameFunction(t *testing.T) {
	_, c := newFakeAPI(t)
	dir := t.TempDir()
	d, err := c.Functions.New("Ticket Triage", SaveTo(filepath.Join(dir, "d.yaml")))
	must(t, err)
	must(t, d.AddInput("ticket_text", map[string]any{"required": true}))
	must(t, d.AddInput("amount", map[string]any{"type": "number", "minimum": 0}))
	must(t, d.AddOutput("summary", map[string]any{"required": true}))
	must(t, d.AddOutput("urgency", map[string]any{"type": "integer", "minimum": 1, "maximum": 5, "required": true}))
	must(t, d.AddPrompt("You triage tickets."))
	must(t, d.AddSection("definitions", "Urgency: 1 to 5."))
	must(t, d.AddSection("constraints", "Never invent facts."))
	must(t, d.AddSection("edge_cases", "Empty ticket: urgency 1."))
	must(t, d.AddStep("Read."))
	must(t, d.AddStep("Score."))
	must(t, d.AddExample(ExampleOpts{Input: map[string]any{"ticket_text": "crash"}, Output: map[string]any{"summary": "crash", "urgency": 4}}))
	must(t, d.AddExample(ExampleOpts{Polarity: "negative", Output: map[string]any{"summary": "idk", "urgency": 3}, Note: "vague"}))
	must(t, d.AddTool("human_approval", ToolOptions{MaxCalls: 2}))
	must(t, d.AddRubric("Names the problem."))
	must(t, d.SetMemory(Memory{Per: "caller", Never: []string{"card numbers"}}))

	// a parsed document in the same shape a YAML reader hands over (ordered maps, plain lists)
	doc := orderedDocFromYAMLForTest(t, d)
	loaded, err := c.LoadDocument(doc, "d.yaml", NoDraftFile())
	must(t, err)
	if loaded.ToYAML() != d.ToYAML() {
		t.Fatalf("loaded function differs:\n--- original\n%s\n--- loaded\n%s", d.ToYAML(), loaded.ToYAML())
	}
	a, _ := json.Marshal(d.snapshot())
	b, _ := json.Marshal(loaded.snapshot())
	if string(a) != string(b) {
		t.Fatalf("snapshots differ:\n%s\n%s", a, b)
	}
}

func TestLoadDocument_AppliesTheSameChecksAsTheAddCalls(t *testing.T) {
	_, c := newFakeAPI(t)
	var err error
	_, err = c.LoadDocument(map[string]any{"name": "X", "input": map[string]any{"a": map[string]any{"type": "text"}}}, "bad.yaml", NoDraftFile())
	wantDefErr(t, err, "type 'text' is not valid")
	_, err = c.LoadDocument(map[string]any{"description": "no name"}, "noname.yaml", NoDraftFile())
	wantDefErr(t, err, "at least a `name`")
}

func TestFunction_CheckNamesTheFieldWithoutRunning(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /v1/run/wt/contract", func(w http.ResponseWriter, r *http.Request) {
		json.NewEncoder(w).Encode(map[string]any{"input_schema": map[string]any{"type": "object", "required": []any{"text"},
			"properties": map[string]any{"text": map[string]any{"type": "string"}, "rating": map[string]any{"type": "integer", "maximum": 5}}}})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	f := NewFunction("wt", "fnk_x", WithBaseURL(srv.URL))
	if p, err := f.Check(map[string]any{"text": "hi", "rating": 3}); err != nil || len(p) != 0 {
		t.Fatalf("expected a pass, got %v %v", p, err)
	}
	p, _ := f.Check(map[string]any{"rating": 9})
	if !reflect.DeepEqual(p, []string{"missing required field 'text'", "field 'rating' must be at most 5, got 9"}) {
		t.Fatalf("unexpected: %v", p)
	}
	p, _ = f.Check(map[string]any{"text": 5})
	if !reflect.DeepEqual(p, []string{"field 'text' must be a string, got number"}) {
		t.Fatalf("unexpected: %v", p)
	}
}

func TestDescribe_PrintsTheLatestVersion(t *testing.T) {
	api, c := newFakeAPI(t)
	api.functions = []map[string]any{{"id": 60, "slug": "x", "name": "X", "input_schema": map[string]any{}, "output_schema": map[string]any{}}}
	api.versions[60] = []map[string]any{{"id": 500, "number": 1,
		"input_schema":  map[string]any{"type": "object", "properties": map[string]any{"a": map[string]any{"type": "string"}}, "required": []any{"a"}},
		"output_schema": map[string]any{"type": "object", "properties": map[string]any{"o": map[string]any{"type": "string"}}, "required": []any{"o"}},
		"snapshot":      map[string]any{"implementation": map[string]any{"flow": []any{"Do it."}, "sections": map[string]any{"role": "r"}}}}}
	text, err := c.Functions.Describe(60)
	must(t, err)
	for _, want := range []string{"flow:\n    - \"Do it.\"", "role: \"r\"", "a: { type: string, required: true }"} {
		if !strings.Contains(text, want) {
			t.Fatalf("missing %q in:\n%s", want, text)
		}
	}
	api.functions = append(api.functions, map[string]any{"id": 61, "slug": "y", "name": "Y",
		"input_schema":  map[string]any{"type": "object", "properties": map[string]any{"a": map[string]any{"type": "string"}}, "required": []any{"a"}},
		"output_schema": map[string]any{"type": "object", "properties": map[string]any{}}})
	text, _ = c.Functions.Describe(61)
	if !strings.Contains(text, "no output fields") || !strings.Contains(text, "no version saved yet") {
		t.Fatalf("a function with no version should say so:\n%s", text)
	}
}

func TestUploadDeployCurlRunFindingsCompareTools(t *testing.T) {
	api, c := newFakeAPI(t)
	dir := t.TempDir()
	csv := filepath.Join(dir, "cases.csv")
	os.WriteFile(csv, []byte("q,a\nhi,yo\n"), 0o644)
	api.functions = []map[string]any{{"id": 3, "slug": "hi", "name": "Hi",
		"input_schema": map[string]any{"type": "object", "properties": map[string]any{"a": map[string]any{"type": "string"}}}}}

	up, err := c.Datasets.Upload(7, csv, map[string]string{"q": "question"}, map[string]string{"a": "answer"})
	must(t, err)
	if up.Added != 1 || api.form["mapping"] != `{"q":"question"}` || api.fileName != "cases.csv" {
		t.Fatalf("upload not sent correctly: %+v %+v %s", up, api.form, api.fileName)
	}
	if _, err := c.Datasets.Upload(7, filepath.Join(dir, "x.txt"), map[string]string{"a": "b"}, nil); err == nil || !strings.Contains(err.Error(), "unsupported file type") {
		t.Fatalf("expected unsupported file type, got %v", err)
	}

	dep, err := c.Deployments.Deploy(3, 4)
	must(t, err)
	if !strings.HasSuffix(dep.Endpoint, "/v1/run/hi") || !strings.Contains(dep.Curl, "curl -X POST") || !strings.Contains(dep.Curl, `"input": {"a":"..."}`) {
		t.Fatalf("deploy should carry an endpoint and a curl: %+v", dep)
	}

	run, err := c.Eval.Experiments.Run(9)
	must(t, err)
	if len(run.Findings) != 1 {
		t.Fatalf("Run should carry the findings, got %+v", run)
	}

	mk := func(id int, pass, cost float64) map[string]any {
		return map[string]any{"id": id, "name": fmt.Sprintf("e%d", id), "status": "completed", "cost": cost,
			"results": map[string]any{"n_rows": 10, "latency": map[string]any{"p95": 2000},
				"per_version": map[string]any{fmt.Sprint(id): map[string]any{"number": id}},
				"checks":      []any{map[string]any{"check_name": "valid", "pass_rate": pass}}}}
	}
	api.exps[1], api.exps[2] = mk(1, 0.6, 1.0), mk(2, 0.9, 2.0)
	cmp, err := c.Eval.Experiments.Compare([]int{1, 2})
	must(t, err)
	rows := map[string]ComparisonRow{}
	for _, r := range cmp.Rows {
		rows[r.Metric] = r
	}
	if cmp.Experiments[0].Version != "v1" || cmp.Experiments[1].Version != "v2" {
		t.Fatalf("unexpected versions: %+v", cmp.Experiments)
	}
	if b := rows["Pass rate %"].Best; b == nil || *b != 1 {
		t.Fatal("higher pass rate should win")
	}
	if b := rows["Cost per run $"].Best; b == nil || *b != 0 {
		t.Fatal("lower cost should win")
	}
	if v := rows["check: valid %"].Values; *v[0] != 60 || *v[1] != 90 {
		t.Fatalf("unexpected check values: %v %v", *v[0], *v[1])
	}
	if _, err := c.Eval.Experiments.Compare([]int{1}); err == nil {
		t.Fatal("Compare needs two ids")
	}
	api.exps[3] = map[string]any{"id": 3, "status": "running"}
	if _, err := c.Eval.Experiments.Compare([]int{1, 3}); err == nil || !strings.Contains(err.Error(), "only completed") {
		t.Fatalf("expected only-completed error, got %v", err)
	}

	_, err = c.Tools.Create(map[string]any{"name": "t", "description": "d", "target_type": "http", "url": "https://e.x"})
	if err == nil || !strings.Contains(err.Error(), "input_schema") {
		t.Fatalf("an http tool needs a schema: %v", err)
	}
	_, err = c.Tools.Create(map[string]any{"name": "t", "description": "d", "target_type": "python", "code": "def run(a): pass"})
	if err == nil || !strings.Contains(err.Error(), "input_schema") {
		t.Fatalf("a python tool needs a schema: %v", err)
	}
	if _, err = c.Tools.Create(map[string]any{"name": "t", "description": "d", "target_type": "ftp"}); err == nil {
		t.Fatal("unknown target_type should fail")
	}
	_, err = c.Tools.Create(map[string]any{"name": "t", "description": "d", "target_type": "http", "url": "https://e.x",
		"input_schema": map[string]any{"type": "object", "properties": map[string]any{}}})
	must(t, err)
}

// orderedDocFromYAMLForTest builds the document a YAML reader would hand over, from the draft's own
// YAML, without a YAML library: it uses the draft's field sets and sections directly.
func orderedDocFromYAMLForTest(t *testing.T, d *FunctionDraft) *OrderedMap {
	t.Helper()
	doc := NewOrderedMap()
	meta := NewOrderedMap()
	meta.Set("function", d.Slug)
	doc.Set("meta", meta)
	doc.Set("name", d.Name)
	doc.Set("input", d.in.dsl())
	doc.Set("output", d.out.dsl())
	impl := NewOrderedMap()
	exec := NewOrderedMap()
	exec.Set("mode", d.effectiveMode())
	impl.Set("execution", exec)
	flow := make([]any, len(d.flow))
	for i, s := range d.flow {
		flow[i] = s
	}
	impl.Set("flow", flow)
	sec := NewOrderedMap()
	sec.Set("role", d.role)
	toAny := func(in []string) []any {
		out := make([]any, len(in))
		for i, s := range in {
			out[i] = s
		}
		return out
	}
	sec.Set("definitions", toAny(d.definitions))
	sec.Set("constraints", toAny(d.constraints))
	sec.Set("edge_cases", toAny(d.edgeCases))
	exs := make([]any, len(d.examples))
	for i, e := range d.examples {
		exs[i] = e
	}
	sec.Set("examples", exs)
	impl.Set("sections", sec)
	tools := make([]any, len(d.tools))
	for i, tl := range d.tools {
		tools[i] = tl
	}
	impl.Set("tools", tools)
	doc.Set("implementation", impl)
	doc.Set("rubric", toAny(d.rubric))
	doc.Set("memory", d.memory)
	return doc
}

func TestNewCreatesNothingUntilSubmit_AndRetryDoesNotCreateTwice(t *testing.T) {
	api, c := newFakeAPI(t)
	d, err := c.Functions.New("Late Create", NoDraftFile())
	must(t, err)
	if d.ID != 0 || api.creates != 0 {
		t.Fatalf("New should create nothing on the server: id=%d creates=%d", d.ID, api.creates)
	}
	must(t, d.AddOutput("o", map[string]any{"required": true}))
	must(t, d.AddStep("s"))
	if api.creates != 0 {
		t.Fatal("the Add calls should not create anything either")
	}
	api.failSave = true
	if _, err := d.Submit(""); err == nil || !strings.Contains(err.Error(), "server said no") {
		t.Fatalf("expected the server's error, got %v", err)
	}
	if d.ID == 0 || api.creates != 1 {
		t.Fatalf("the function should be created and remembered: id=%d creates=%d", d.ID, api.creates)
	}
	api.failSave = false
	_, err = d.Submit("")
	must(t, err)
	if api.creates != 1 {
		t.Fatalf("the retry must not create the function again, got %d creates", api.creates)
	}
}

func TestNew_ATakenNameIsRefusedAtOnce_ArchivedToo(t *testing.T) {
	api, c := newFakeAPI(t)
	api.functions = []map[string]any{{"id": 7, "slug": "taken_one", "name": "Taken One", "archived": true}}
	_, err := c.Functions.New("Taken One", NoDraftFile())
	if err == nil || !strings.Contains(err.Error(), "(archived)") || !strings.Contains(err.Error(), "LoadDraft(7)") {
		t.Fatalf("expected a clear refusal, got %v", err)
	}
}

func TestSubmit_ANameClashKeepsTheWorkAndSaysWhatToDo(t *testing.T) {
	api, c := newFakeAPI(t)
	d, err := c.Functions.New("Clash", NoDraftFile())
	must(t, err)
	must(t, d.AddOutput("o", map[string]any{"required": true}))
	must(t, d.AddStep("s"))
	api.clash = true
	_, err = d.Submit("")
	if e, ok := err.(*Error); !ok || e.Status != 409 || !strings.Contains(e.Detail, "WithSlug") {
		t.Fatalf("expected a helpful 409, got %v", err)
	}
	if d.ID != 0 || len(d.out.names) != 1 {
		t.Fatal("nothing was created and the work must still be there")
	}
}

func TestLoadDocument_CreatesNothing_AndContinuesAnExistingFunction(t *testing.T) {
	api, c := newFakeAPI(t)
	doc := map[string]any{"meta": map[string]any{"function": "late_one"}, "name": "Late One",
		"output":         map[string]any{"o": map[string]any{"type": "string", "required": true}},
		"implementation": map[string]any{"flow": []any{"s"}}}
	d, err := c.LoadDocument(doc, "x.yaml", NoDraftFile())
	must(t, err)
	if d.ID != 0 || api.creates != 0 {
		t.Fatalf("loading must not create the function: id=%d creates=%d", d.ID, api.creates)
	}
	api.functions = []map[string]any{{"id": 77, "slug": "late_one", "name": "Late One"}}
	again, err := c.LoadDocument(doc, "x.yaml", NoDraftFile())
	must(t, err)
	if again.ID != 77 {
		t.Fatalf("an existing function should be continued, got id %d", again.ID)
	}
}

func TestGetBySlug_FindsSuggestsAndLists(t *testing.T) {
	api, c := newFakeAPI(t)
	api.functions = []map[string]any{
		{"id": 60, "slug": "invoice_extractor", "name": "Invoice Extractor", "input_schema": map[string]any{}, "output_schema": map[string]any{}},
		{"id": 61, "slug": "ticket_triage", "name": "Ticket Triage", "input_schema": map[string]any{}, "output_schema": map[string]any{}},
	}
	fn, err := c.Functions.GetBySlug("ticket_triage")
	must(t, err)
	if fn.ID != 61 {
		t.Fatalf("expected id 61, got %d", fn.ID)
	}
	_, err = c.Functions.GetBySlug("invoice_extracter")
	e, ok := err.(*Error)
	if !ok || e.Status != 404 || !strings.Contains(e.Detail, `no function with slug "invoice_extracter"`) ||
		!strings.Contains(e.Detail, "did you mean 'invoice_extractor'") || !strings.Contains(e.Detail, "ticket_triage") {
		t.Fatalf("expected a helpful 404, got %v", err)
	}
	if _, err = c.Functions.GetBySlug("Invoice Extractor"); err == nil {
		t.Fatal("a display name must not match")
	}
	if _, err = c.Functions.GetBySlug("60"); err == nil || !strings.Contains(err.Error(), "use Get(60)") {
		t.Fatalf("a number as text should hint at Get: %v", err)
	}
	if _, err = c.Functions.GetBySlug(""); err == nil {
		t.Fatal("an empty slug is refused")
	}
	api.functions = nil
	if _, err = c.Functions.GetBySlug("anything"); err == nil || !strings.Contains(err.Error(), "no functions in this space yet") {
		t.Fatalf("an empty space should be said plainly: %v", err)
	}
}

func TestLoadDraftBySlug(t *testing.T) {
	api, c := newFakeAPI(t)
	api.functions = []map[string]any{{"id": 60, "slug": "invoice_extractor", "name": "Invoice Extractor", "input_schema": map[string]any{}, "output_schema": map[string]any{}}}
	d, err := c.LoadDraftBySlug("invoice_extractor", NoDraftFile())
	must(t, err)
	if d.ID != 60 || d.Slug != "invoice_extractor" {
		t.Fatalf("unexpected draft: %+v", d)
	}
}

func versionFixture(n int, status string, flow []any) map[string]any {
	return map[string]any{"id": 100 + n, "number": n, "status": status,
		"input_schema":  map[string]any{"type": "object", "properties": map[string]any{"a": map[string]any{"type": "string"}}},
		"output_schema": map[string]any{"type": "object", "properties": map[string]any{"o": map[string]any{"type": "string"}}, "required": []any{"o"}},
		"snapshot":      map[string]any{"implementation": map[string]any{"flow": flow, "sections": map[string]any{"role": fmt.Sprintf("role of v%d", n)}}}}
}

func versionSetup(t *testing.T) (*fakeAPI, *Client) {
	api, c := newFakeAPI(t)
	api.functions = []map[string]any{{"id": 60, "slug": "fn", "name": "Fn", "input_schema": map[string]any{}, "output_schema": map[string]any{}}}
	api.versions[60] = []map[string]any{versionFixture(1, "approved", []any{"v1 step"}), versionFixture(2, "approved", []any{"v2 step"}), versionFixture(3, "draft", []any{"v3 step"})}
	return api, c
}

func flowOfVersion(api *fakeAPI, n int) any {
	for _, v := range api.versions[60] {
		if v["number"] == n {
			return v["snapshot"].(map[string]any)["implementation"].(map[string]any)["flow"]
		}
	}
	return nil
}

func TestLoadAnyVersion_AFrozenVersionBecomesANewOne_AndNothingIsOverwritten(t *testing.T) {
	api, c := versionSetup(t)
	d, err := c.LoadDraft(60, NoDraftFile(), WithVersion(1))
	must(t, err)
	if !reflect.DeepEqual(d.flow, []string{"v1 step"}) || d.role != "role of v1" {
		t.Fatalf("should load v1's own content: %v %q", d.flow, d.role)
	}
	if !strings.HasPrefix(fmt.Sprint(d), "# based on v1 — saving creates a new version") {
		t.Fatalf("missing the based-on note:\n%s", d)
	}
	must(t, d.AddStep("a new step"))
	v, err := d.Submit("from v1")
	must(t, err)
	if v.Number != 4 {
		t.Fatalf("a frozen base must become a NEW version, got v%d", v.Number)
	}
	if len(api.posts) != 1 || api.posts[0]["parent_id"] != float64(101) {
		t.Fatalf("expected one create with parent v1, got %+v", api.posts)
	}
	if len(api.puts) != 0 || len(api.submits) != 0 {
		t.Fatal("no existing version may be touched")
	}
	if !reflect.DeepEqual(flowOfVersion(api, 3), []any{"v3 step"}) || !reflect.DeepEqual(flowOfVersion(api, 1), []any{"v1 step"}) {
		t.Fatal("v1 and the unfinished v3 must be untouched")
	}
	// after the first save the new draft is updated in place
	must(t, d.AddStep("more"))
	_, err = d.Submit("")
	must(t, err)
	if len(api.posts) != 1 || len(api.puts) != 1 || !strings.HasPrefix(api.puts[0], "/api/versions/9") {
		t.Fatalf("the second save should update v4 in place: posts=%d puts=%v", len(api.posts), api.puts)
	}
}

func TestLoadAnyVersion_ADraftIsEditedInPlace_EvenWhenItIsNotTheLatest(t *testing.T) {
	api, c := versionSetup(t)
	api.versions[60] = append(api.versions[60], versionFixture(4, "draft", []any{"v4 step"}))
	d, err := c.LoadDraftBySlug("fn", NoDraftFile(), WithVersion(3))
	must(t, err)
	if !strings.HasPrefix(fmt.Sprint(d), "# editing draft v3") {
		t.Fatalf("missing the editing note:\n%s", d)
	}
	must(t, d.AddStep("edited"))
	v, err := d.Submit("")
	must(t, err)
	if v.Number != 3 || len(api.puts) != 1 || api.puts[0] != "/api/versions/103" {
		t.Fatalf("v3 should be edited in place: v%d %v", v.Number, api.puts)
	}
	if !reflect.DeepEqual(flowOfVersion(api, 4), []any{"v4 step"}) {
		t.Fatal("the latest draft (v4) must be untouched")
	}
}

func TestLoadAnyVersion_NoVersionLoadsTheLatestByTheServersRule(t *testing.T) {
	api, c := versionSetup(t)
	d, err := c.LoadDraft(60, NoDraftFile())
	must(t, err)
	if !reflect.DeepEqual(d.flow, []string{"v3 step"}) {
		t.Fatalf("should load the latest: %v", d.flow)
	}
	_, err = d.Submit("")
	must(t, err)
	if len(api.submits) != 1 {
		t.Fatal("the server's own save rule should be used")
	}
}

func TestLoadAnyVersion_MissingAndBadVersionsAreClear(t *testing.T) {
	_, c := versionSetup(t)
	_, err := c.LoadDraft(60, NoDraftFile(), WithVersion(9))
	if e, ok := err.(*Error); !ok || e.Status != 404 || !strings.Contains(e.Detail, "no version 9") || !strings.Contains(e.Detail, "versions: 1, 2, 3") {
		t.Fatalf("expected a helpful 404, got %v", err)
	}
	if _, err = c.LoadDraft(60, NoDraftFile(), WithVersion(-1)); err == nil {
		t.Fatal("a negative version must be refused")
	}
}

func TestLoadAnyVersion_ADraftDeployedMeanwhileGivesAClearErrorAndKeepsTheWork(t *testing.T) {
	api, c := versionSetup(t)
	d, err := c.LoadDraft(60, NoDraftFile(), WithVersion(3))
	must(t, err)
	must(t, d.AddStep("x"))
	api.frozenOnPut = true
	_, err = d.Submit("")
	if e, ok := err.(*Error); !ok || e.Status != 409 || !strings.Contains(e.Detail, "has been deployed since you loaded it") {
		t.Fatalf("expected a clear 409, got %v", err)
	}
	if !contains2(d.flow, "x") {
		t.Fatal("the work must still be there")
	}
}

func TestLoadAnyVersion_TheDraftFileRemembersTheBase(t *testing.T) {
	api, c := versionSetup(t)
	file := filepath.Join(t.TempDir(), "d.yaml")
	d, err := c.LoadDraft(60, SaveTo(file), WithVersion(1))
	must(t, err)
	must(t, d.AddStep("x"))
	b, _ := os.ReadFile(file)
	if !strings.HasPrefix(string(b), "meta:\n  function: fn\n  based_on: 1\n") {
		t.Fatalf("the file should record the base:\n%s", b)
	}
	// a fresh run after the program stopped: the document goes back in through LoadDocument
	doc := map[string]any{"meta": map[string]any{"function": "fn", "based_on": 1}, "name": "Fn",
		"output":         map[string]any{"o": map[string]any{"type": "string", "required": true}},
		"implementation": map[string]any{"flow": []any{"s"}}}
	loaded, err := c.LoadDocument(doc, "d.yaml", NoDraftFile())
	must(t, err)
	if !strings.HasPrefix(fmt.Sprint(loaded), "# based on v1") {
		t.Fatalf("the base must survive a restart:\n%s", loaded)
	}
	_, err = loaded.Submit("")
	must(t, err)
	if len(api.posts) != 1 || len(api.submits) != 0 {
		t.Fatalf("a frozen base must create a new version, not use the server's rule: posts=%d submits=%d", len(api.posts), len(api.submits))
	}
}

func richVersion() map[string]any {
	return map[string]any{"id": 101, "number": 1, "status": "approved",
		"input_schema":  map[string]any{"type": "object", "properties": map[string]any{"url": map[string]any{"type": "string"}, "secret": map[string]any{"type": "string"}}, "required": []any{"url"}},
		"output_schema": map[string]any{"type": "object", "properties": map[string]any{"summary": map[string]any{"type": "string"}}, "required": []any{"summary"}},
		"snapshot": map[string]any{"name": "X", "needs": []any{"tool_calling"}, "rubric": []any{"r"}, "memory": map[string]any{"per": "caller"},
			"input": map[string]any{
				"url":       map[string]any{"type": "string", "required": true},
				"page_text": map[string]any{"type": "string", "derived": true, "sensitive": true, "required": false},
				"secret":    map[string]any{"type": "string", "sensitive": true}},
			"output": map[string]any{"summary": map[string]any{"type": "string", "required": true, "sensitive": true}},
			"implementation": map[string]any{"execution": map[string]any{"mode": "agent"},
				"preprocessors": []any{map[string]any{"tool": "web_fetch", "from": "url", "into": "page_text"}},
				"flow":          []any{"f"}, "sections": map[string]any{"role": "r", "definitions": []any{"d"}},
				"tools": []any{map[string]any{"name": "web_fetch", "max_calls": 2}, map[string]any{"server": "crm", "max_calls": 20}}}}}
}

func richSetup(t *testing.T) (*fakeAPI, *Client) {
	api, c := newFakeAPI(t)
	api.functions = []map[string]any{{"id": 60, "slug": "x", "name": "X", "description": "", "input_schema": map[string]any{}, "output_schema": map[string]any{}}}
	api.versions[60] = []map[string]any{richVersion()}
	api.tools = append(api.tools, map[string]any{"name": "web_fetch", "status": "active"})
	return api, c
}

func TestLoadVersion_NothingIsLost(t *testing.T) {
	api, c := richSetup(t)
	d, err := c.LoadDraft(60, NoDraftFile())
	must(t, err)
	must(t, d.AddStep("another step"))
	snap := d.snapshot()
	jsonOf := func(v any) string { b, _ := json.Marshal(v); return string(b) }
	if jsonOf(snap["needs"]) != `["tool_calling"]` {
		t.Fatalf("needs lost: %v", snap["needs"])
	}
	impl := snap["implementation"].(map[string]any)
	orig := richVersion()["snapshot"].(map[string]any)["implementation"].(map[string]any)
	if jsonOf(impl["preprocessors"]) != jsonOf(orig["preprocessors"]) {
		t.Fatalf("preprocessors lost: %v", impl["preprocessors"])
	}
	if len(impl["tools"].([]map[string]any)) != 2 || impl["tools"].([]map[string]any)[1]["server"] != "crm" {
		t.Fatalf("the server: tool entry was lost: %v", impl["tools"])
	}
	in := snap["input"].(*OrderedMap)
	pt := in.Values["page_text"].(map[string]any)
	if pt["derived"] != true || pt["sensitive"] != true {
		t.Fatalf("derived/sensitive lost on page_text: %v", pt)
	}
	if _, has := pt["required"]; has {
		t.Fatalf("a false flag should not be written back: %v", pt)
	}
	if in.Values["secret"].(map[string]any)["sensitive"] != true || snap["output"].(*OrderedMap).Values["summary"].(map[string]any)["sensitive"] != true {
		t.Fatal("the sensitive flag was lost")
	}
	// a derived field is not a caller input
	_, err = d.Submit("")
	must(t, err)
	sent := api.submits[0]["input_schema"].(map[string]any)["properties"].(map[string]any)
	if _, has := sent["page_text"]; has {
		t.Fatal("a derived field must not be in the caller's schema")
	}
	if p := d.CheckInput(map[string]any{"url": "https://x"}); len(p) != 0 {
		t.Fatalf("a caller need not send the derived field: %v", p)
	}
	wantDefErr(t, d.AddInput("page_text", nil), "already defined")
	text := fmt.Sprint(d)
	for _, want := range []string{"needs: [tool_calling]", "preprocessors:", "{ tool: web_fetch, from: url, into: page_text }", "derived: true", "server: crm"} {
		if !strings.Contains(text, want) {
			t.Fatalf("printing should show %q:\n%s", want, text)
		}
	}
}

func TestLoadVersion_SensitiveAddedThroughTheBuilderIsKept_AndGuardsAreNotFalseAlarms(t *testing.T) {
	_, c := richSetup(t)
	e := newDraft(t, c, "Y")
	must(t, e.AddInput("card", map[string]any{"sensitive": true}))
	if e.snapshot()["input"].(*OrderedMap).Values["card"].(map[string]any)["sensitive"] != true {
		t.Fatal("sensitive added through the builder was dropped")
	}
	d, err := c.LoadDraft(60, NoDraftFile())
	must(t, err)
	must(t, d.AddTool("issue_refund", ToolOptions{Requires: `input.page_text == "x"`}))
	_, err = d.Submit("")
	must(t, err) // input.page_text is declared; the server entry hides other tool names
}

func TestLoadVersion_SurvivesAStoppedProgram(t *testing.T) {
	_, c := richSetup(t)
	file := filepath.Join(t.TempDir(), "d.yaml")
	d, err := c.LoadDraft(60, SaveTo(file))
	must(t, err)
	must(t, d.AddStep("another step"))
	// a fresh run: the same content comes back through LoadDocument (what yamlload does with the file)
	doc := orderedDocFromYAMLForTest(t, d)
	for k, v := range d.extraTop {
		doc.Set(k, v)
	}
	impl := doc.Values["implementation"].(*OrderedMap)
	for k, v := range d.extraImpl {
		impl.Set(k, v)
	}
	doc.Set("input", d.dslFields("input").dsl())
	doc.Set("output", d.dslFields("output").dsl())
	loaded, err := c.LoadDocument(doc, "d.yaml", NoDraftFile())
	must(t, err)
	a, _ := json.Marshal(d.snapshot())
	b, _ := json.Marshal(loaded.snapshot())
	if string(a) != string(b) {
		t.Fatalf("a restart must not change the function:\n%s\n%s", a, b)
	}
}

func TestLoadVersion_AnOldEditorFormatVersionIsRefusedNotBlanked(t *testing.T) {
	api, c := richSetup(t)
	api.versions[60] = []map[string]any{{"id": 101, "number": 1, "status": "approved",
		"snapshot": map[string]any{"impl": "prompt", "sections": []any{map[string]any{"type": "role", "content": "hi"}}}}}
	_, err := c.LoadDraft(60, NoDraftFile())
	wantDefErr(t, err, "older editor format")
}

func TestDescribe_ShowsThePartsTheBuilderHasNoCallFor(t *testing.T) {
	_, c := richSetup(t)
	text, err := c.Functions.Describe(60)
	must(t, err)
	if !strings.Contains(text, "needs: [tool_calling]") || !strings.Contains(text, "preprocessors:") {
		t.Fatalf("Describe should show them:\n%s", text)
	}
}

func TestSubmitAsNew_BranchesWithoutTouchingTheVersionItGrewFrom(t *testing.T) {
	api, c := newFakeAPI(t)
	api.functions = []map[string]any{{"id": 60, "slug": "fn", "name": "Fn", "input_schema": map[string]any{}, "output_schema": map[string]any{}}}
	api.versions[60] = []map[string]any{versionFixture(1, "draft", []any{"v1 step"})}
	d, err := c.LoadDraft(60, NoDraftFile())
	must(t, err)
	must(t, d.AddStep("a new idea"))
	v2, err := d.SubmitAsNew("try this")
	must(t, err)
	if v2.Number != 2 || len(api.posts) != 1 || api.posts[0]["parent_id"] != float64(101) {
		t.Fatalf("expected a new v2 with parent v1: v%d %+v", v2.Number, api.posts)
	}
	if !reflect.DeepEqual(flowOfVersion(api, 1), []any{"v1 step"}) || len(api.puts) != 0 || len(api.submits) != 0 {
		t.Fatal("v1 must be untouched and nothing else written")
	}
	must(t, d.AddStep("y"))
	_, err = d.Submit("") // a plain save updates the new draft in place
	must(t, err)
	if len(api.puts) != 1 || len(api.posts) != 1 {
		t.Fatalf("a plain save should update v2 in place: puts=%v posts=%d", api.puts, len(api.posts))
	}
	must(t, d.AddStep("z"))
	v3, err := d.SubmitAsNew("")
	must(t, err)
	if v3.Number != 3 || len(api.posts) != 2 || api.posts[1]["parent_id"] != float64(v2.ID) {
		t.Fatalf("branching again should be from v2: v%d %+v", v3.Number, api.posts)
	}
}

func listYAML(dir string) []string {
	entries, _ := os.ReadDir(dir)
	var out []string
	for _, e := range entries {
		if strings.HasSuffix(e.Name(), ".yaml") {
			out = append(out, e.Name())
		}
	}
	return out
}

func readFile(t *testing.T, p string) string {
	t.Helper()
	b, err := os.ReadFile(p)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

func TestVersionCopies_EachThingYouEditHasItsOwnFile(t *testing.T) {
	api, c := versionSetup(t)
	api.versions[60] = []map[string]any{versionFixture(1, "approved", []any{"v1 step"}), versionFixture(2, "approved", []any{"v2 step"}), versionFixture(3, "draft", []any{"v3 step"})}
	drafts := filepath.Join(t.TempDir(), "drafts")
	cc := NewClient("fnp_test", WithBaseURL(c.h.base), WithDraftsFolder(drafts))
	a, err := cc.LoadDraft(60, WithVersion(1))
	must(t, err)
	must(t, a.AddStep("x"))
	b, err := cc.LoadDraft(60, WithVersion(3))
	must(t, err)
	must(t, b.AddStep("y"))
	l, err := cc.LoadDraft(60)
	must(t, err)
	must(t, l.AddStep("z"))
	got := strings.Join(listYAML(drafts), ",")
	if got != "fn.from-v1.yaml,fn.v3.yaml,fn.yaml" {
		t.Fatalf("each draft needs its own file, got %s", got)
	}
	if !strings.Contains(readFile(t, filepath.Join(drafts, "fn.from-v1.yaml")), "v1 step") || strings.Contains(readFile(t, filepath.Join(drafts, "fn.v3.yaml")), "v1 step") {
		t.Fatal("the drafts overwrote each other")
	}
}

func TestVersionCopies_SavingTurnsTheWorkingFileIntoThatVersionsCopy(t *testing.T) {
	_, c := versionSetup(t)
	root := t.TempDir()
	drafts := filepath.Join(root, "drafts")
	cc := NewClient("fnp_test", WithBaseURL(c.h.base), WithDraftsFolder(drafts))
	d, err := cc.LoadDraft(60, WithVersion(1))
	must(t, err)
	must(t, d.AddStep("a new step"))
	v, err := d.Submit("from v1")
	must(t, err)
	if len(listYAML(drafts)) != 0 {
		t.Fatal("the working file should be gone")
	}
	record := filepath.Join(root, "versions", "fn", fmt.Sprintf("v%d.yaml", v.Number))
	text := readFile(t, record)
	if !strings.HasPrefix(text, fmt.Sprintf("meta:\n  function: fn\n  version: %d\n", v.Number)) || !strings.Contains(text, "a new step") {
		t.Fatalf("unexpected copy:\n%s", text)
	}
}

func TestVersionCopies_EditingADraftAgainRefreshesItsCopy(t *testing.T) {
	_, c := versionSetup(t)
	root := t.TempDir()
	cc := NewClient("fnp_test", WithBaseURL(c.h.base), WithDraftsFolder(filepath.Join(root, "drafts")))
	d, err := cc.LoadDraft(60, WithVersion(3))
	must(t, err)
	must(t, d.AddStep("first"))
	_, err = d.Submit("")
	must(t, err)
	v3 := filepath.Join(root, "versions", "fn", "v3.yaml")
	if !strings.Contains(readFile(t, v3), "first") {
		t.Fatal("the copy should have the first edit")
	}
	must(t, d.AddStep("second"))
	_, err = d.Submit("")
	must(t, err)
	if !strings.Contains(readFile(t, v3), "second") {
		t.Fatal("the copy should be refreshed")
	}
}

func TestVersionCopies_OffMeansTheFileIsJustRemoved_AndNoDraftFileMeansNoFiles(t *testing.T) {
	_, c := versionSetup(t)
	root := t.TempDir()
	drafts := filepath.Join(root, "drafts")
	off := NewClient("fnp_test", WithBaseURL(c.h.base), WithDraftsFolder(drafts), WithoutVersionCopies())
	d, err := off.LoadDraft(60, WithVersion(3))
	must(t, err)
	must(t, d.AddStep("x"))
	_, err = d.Submit("")
	must(t, err)
	if len(listYAML(drafts)) != 0 {
		t.Fatal("the draft file should be removed")
	}
	if _, err := os.Stat(filepath.Join(root, "versions")); err == nil {
		t.Fatal("no copies with copies off")
	}
	root2 := t.TempDir()
	none := NewClient("fnp_test", WithBaseURL(c.h.base), WithDraftsFolder(filepath.Join(root2, "drafts")))
	d2, err := none.LoadDraft(60, WithVersion(3), NoDraftFile())
	must(t, err)
	must(t, d2.AddStep("x"))
	_, err = d2.Submit("")
	must(t, err)
	if entries, _ := os.ReadDir(root2); len(entries) != 0 {
		t.Fatalf("NoDraftFile must mean no files at all: %v", entries)
	}
}

func TestVersionCopies_WhereTheyGo(t *testing.T) {
	_, c := versionSetup(t)
	root := t.TempDir()
	a := NewClient("fnp_test", WithBaseURL(c.h.base), WithDraftsFolder(filepath.Join(root, "work"))) // not called "drafts"
	d, _ := a.LoadDraft(60, WithVersion(3))
	must(t, d.AddStep("x"))
	v, err := d.Submit("")
	must(t, err)
	if _, err := os.Stat(filepath.Join(root, "work", "versions", "fn", fmt.Sprintf("v%d.yaml", v.Number))); err != nil {
		t.Fatalf("copies should go inside a drafts folder that is not called drafts: %v", err)
	}
	b := NewClient("fnp_test", WithBaseURL(c.h.base), WithDraftsFolder(filepath.Join(root, "drafts")), WithVersionsFolder(filepath.Join(root, "history")))
	d2, _ := b.LoadDraft(60, WithVersion(3))
	must(t, d2.AddStep("x"))
	v2, err := d2.Submit("")
	must(t, err)
	if _, err := os.Stat(filepath.Join(root, "history", "fn", fmt.Sprintf("v%d.yaml", v2.Number))); err != nil {
		t.Fatalf("WithVersionsFolder should win: %v", err)
	}
}

func TestVersionCopies_FindADraftByNameWhateverItIsCalled_AndAskWhenUnsure(t *testing.T) {
	_, c := versionSetup(t)
	root := t.TempDir()
	drafts := filepath.Join(root, "drafts")
	cc := NewClient("fnp_test", WithBaseURL(c.h.base), WithDraftsFolder(drafts))
	d, _ := cc.LoadDraft(60, WithVersion(3))
	must(t, d.AddStep("y"))
	p, err := cc.DraftFile("fn")
	if err != nil || filepath.Base(p) != "fn.v3.yaml" {
		t.Fatalf("expected fn.v3.yaml, got %q %v", p, err)
	}
	e, _ := cc.LoadDraft(60, WithVersion(1))
	must(t, e.AddStep("x"))
	if _, err := cc.DraftFile("fn"); err == nil || !strings.Contains(err.Error(), "2 drafts match") {
		t.Fatalf("expected a clear ask, got %v", err)
	}
	// a saved copy knows which version it is: it goes back in through LoadDocument
	doc := map[string]any{"meta": map[string]any{"function": "fn", "version": 3}, "name": "Fn",
		"output": map[string]any{"o": map[string]any{"type": "string", "required": true}}, "implementation": map[string]any{"flow": []any{"s"}}}
	loaded, err := cc.LoadDocument(doc, "v3.yaml", NoDraftFile())
	must(t, err)
	if !strings.HasPrefix(fmt.Sprint(loaded), "# editing draft v3") {
		t.Fatalf("a copy should carry on from its version:\n%s", loaded)
	}
}

func TestExportYAML_WritesEveryVersionFromTheServer(t *testing.T) {
	api, c := versionSetup(t)
	root := t.TempDir()
	cc := NewClient("fnp_test", WithBaseURL(c.h.base), WithVersionsFolder(filepath.Join(root, "vers")))
	paths, err := cc.ExportYAMLBySlug("fn", "")
	must(t, err)
	names := make([]string, len(paths))
	for i, p := range paths {
		names[i] = filepath.Base(p)
	}
	if strings.Join(names, ",") != "v1.yaml,v2.yaml,v3.yaml" {
		t.Fatalf("expected v1..v3, got %v", names)
	}
	if !strings.Contains(readFile(t, filepath.Join(root, "vers", "fn", "v2.yaml")), "from the server v2") {
		t.Fatal("the copy should be the server's YAML")
	}
	again, err := cc.ExportYAML(60, filepath.Join(root, "elsewhere"))
	must(t, err)
	if len(again) != 3 {
		t.Fatalf("expected 3 files, got %d", len(again))
	}
	api.versions[60] = nil
	if _, err := cc.ExportYAML(60, ""); err == nil || !strings.Contains(err.Error(), "no versions yet") {
		t.Fatalf("expected a clear error, got %v", err)
	}
}
