package fnai

// FunctionDraft (FUNCTION_CLASS_DESIGN.md, approved 2026-09-24): the shared
// Create/AddInput/AddOutput/AddSection/AddExample/AddTool/Submit/Deploy flow.
// Uses a real httptest server, matching this file's existing convention.

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func draftStub(t *testing.T) (*httptest.Server, *Client) {
	t.Helper()
	mux := http.NewServeMux()
	nextVersionID := 500

	mux.HandleFunc("GET /api/functions", func(w http.ResponseWriter, r *http.Request) {
		json.NewEncoder(w).Encode([]map[string]any{}) // the read-only name check
	})
	mux.HandleFunc("GET /api/tools", func(w http.ResponseWriter, r *http.Request) {
		json.NewEncoder(w).Encode([]map[string]any{{"name": "ocr", "status": "active"}})
	})
	mux.HandleFunc("POST /api/functions", func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		json.NewDecoder(r.Body).Decode(&body)
		w.WriteHeader(201)
		json.NewEncoder(w).Encode(map[string]any{
			"id": 60, "slug": body["slug"], "name": body["name"],
			"input_schema": map[string]any{}, "output_schema": map[string]any{},
		})
	})
	mux.HandleFunc("GET /api/functions/60", func(w http.ResponseWriter, r *http.Request) {
		json.NewEncoder(w).Encode(map[string]any{
			"id": 60, "slug": "invoice_extractor", "name": "Invoice Extractor",
			"input_schema": map[string]any{}, "output_schema": map[string]any{},
		})
	})
	mux.HandleFunc("GET /api/functions/60/versions", func(w http.ResponseWriter, r *http.Request) {
		json.NewEncoder(w).Encode([]map[string]any{
			{"id": 500, "number": 1, "status": "approved", "snapshot": map[string]any{
				"name": "Invoice Extractor", "output": map[string]any{},
				"implementation": map[string]any{
					"sections": map[string]any{"role": "You extract invoices."},
					"tools":    []map[string]any{{"name": "ocr"}},
				},
			}},
		})
	})
	mux.HandleFunc("POST /api/functions/60/submit", func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		json.NewDecoder(r.Body).Decode(&body)
		nextVersionID++
		json.NewEncoder(w).Encode(map[string]any{
			"id": nextVersionID, "number": nextVersionID - 499, "status": "draft",
			"message": body["message"], "snapshot": body["snapshot"],
		})
	})
	mux.HandleFunc("POST /api/functions/60/deploy", func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		json.NewDecoder(r.Body).Decode(&body)
		json.NewEncoder(w).Encode(map[string]any{
			"id": 9, "status": "active", "version_id": body["version_id"],
		})
	})

	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return srv, NewClient("fnp_test", WithBaseURL(srv.URL))
}

func TestCreateDraftCreatesNothingOnTheServerUntilSubmit(t *testing.T) {
	_, c := draftStub(t)
	draft, err := c.CreateDraft("Invoice Extractor", "", "")
	if err != nil {
		t.Fatal(err)
	}
	if draft.ID != 0 {
		t.Fatalf("the function should not exist on the server yet, got id %d", draft.ID)
	}
	if err := draft.AddInput("document", map[string]any{"type": "file", "media": "document", "required": true}); err != nil {
		t.Fatal(err)
	}
	if err := draft.AddOutput("total", map[string]any{"type": "number", "required": true}); err != nil {
		t.Fatal(err)
	}
	if err := draft.AddSection("role", "You extract data from invoices."); err != nil {
		t.Fatal(err)
	}
	if err := draft.AddTool("ocr", ToolOptions{}); err != nil {
		t.Fatal(err)
	}
	// nothing sent to the server by add* — only Submit would tell us if it had
}

func TestSubmitSendsTheStagedSnapshotAndSchemas(t *testing.T) {
	_, c := draftStub(t)
	draft, err := c.CreateDraft("Invoice Extractor", "", "")
	if err != nil {
		t.Fatal(err)
	}
	draft.AddInput("document", map[string]any{"type": "file", "media": "document", "required": true})
	draft.AddOutput("total", map[string]any{"type": "number", "required": true})
	draft.AddSection("role", "You extract data from invoices.")
	draft.AddStep("Read the invoice and extract the total.")
	draft.AddTool("ocr", ToolOptions{})

	v, err := draft.Submit("first draft")
	if err != nil {
		t.Fatal(err)
	}
	if v.ID != 501 || v.Number != 2 {
		t.Fatalf("unexpected version: %+v", v)
	}
	impl, ok := v.Snapshot["implementation"].(map[string]any)
	if !ok {
		t.Fatalf("snapshot missing implementation: %+v", v.Snapshot)
	}
	sections := impl["sections"].(map[string]any)
	if sections["role"] != "You extract data from invoices." {
		t.Fatalf("unexpected sections: %+v", sections)
	}
	if draft.LastVersion == nil || draft.LastVersion.ID != v.ID {
		t.Fatalf("LastVersion not updated: %+v", draft.LastVersion)
	}
}

func TestDeployUsesTheVersionFromTheLastSubmit(t *testing.T) {
	_, c := draftStub(t)
	draft, _ := c.CreateDraft("Invoice Extractor", "", "")
	draft.AddOutput("total", map[string]any{"type": "number"})
	draft.AddStep("Extract the total.")
	v, err := draft.Submit("")
	if err != nil {
		t.Fatal(err)
	}
	dep, err := draft.Deploy("go live")
	if err != nil {
		t.Fatal(err)
	}
	if dep.VersionID != v.ID || dep.Status != "active" {
		t.Fatalf("unexpected deployment: %+v", dep)
	}
}

func TestDeployBeforeAnySubmitFails(t *testing.T) {
	_, c := draftStub(t)
	draft, _ := c.CreateDraft("Invoice Extractor", "", "")
	if _, err := draft.Deploy(""); err == nil {
		t.Fatal("expected an error deploying before any Submit()")
	}
}

func TestLoadDraftResumesAnExistingFunctionsStagedContent(t *testing.T) {
	_, c := draftStub(t)
	draft, err := c.LoadDraft(60)
	if err != nil {
		t.Fatal(err)
	}
	if draft.LastVersion == nil || draft.LastVersion.ID != 500 {
		t.Fatalf("expected LastVersion 500, got %+v", draft.LastVersion)
	}
	if draft.role != "You extract invoices." {
		t.Fatalf("expected role loaded from latest version, got %q", draft.role)
	}
	if len(draft.tools) != 1 || draft.tools[0]["name"] != "ocr" {
		t.Fatalf("expected tools loaded from latest version, got %+v", draft.tools)
	}

	// adding a field after it's already deployed, then Submit — same call
	// as building it fresh, per the design doc's example
	draft.AddOutput("tax_amount", map[string]any{"type": "number"})
	draft.AddStep("Extract the tax amount too.")
	v, err := draft.Submit("add tax_amount")
	if err != nil {
		t.Fatal(err)
	}
	if v.Number != 2 { // a new version, not an overwrite
		t.Fatalf("expected version 2, got %d", v.Number)
	}
}

func TestAddSectionRejectsAnUnknownKind(t *testing.T) {
	_, c := draftStub(t)
	draft, _ := c.CreateDraft("Invoice Extractor", "", "")
	if err := draft.AddSection("bogus_kind", "x"); err == nil {
		t.Fatal("expected an error for an unknown section kind")
	}
}

func TestAddExampleRequiresInputAndOutputWhenPositive(t *testing.T) {
	_, c := draftStub(t)
	draft, _ := c.CreateDraft("Invoice Extractor", "", "")
	err := draft.AddExample(ExampleOpts{Input: map[string]any{"document": "x"}}) // missing Output
	if err == nil {
		t.Fatal("expected an error for a positive example missing output")
	}
}
