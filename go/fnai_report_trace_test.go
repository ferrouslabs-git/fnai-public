package fnai

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"regexp"
	"strings"
	"testing"
)

// REQ-202: Function.ReportTrace posts a trace from the customer's own servers to /v1/traces.
func reportStub(t *testing.T, status int, answer map[string]any) (*Function, *map[string]any, *string) {
	t.Helper()
	var sent map[string]any
	var auth string
	mux := http.NewServeMux()
	mux.HandleFunc("POST /v1/traces", func(w http.ResponseWriter, r *http.Request) {
		auth = r.Header.Get("Authorization")
		json.NewDecoder(r.Body).Decode(&sent)
		w.WriteHeader(status)
		json.NewEncoder(w).Encode(answer)
	})
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return NewFunction("demo", "fnk_x", WithBaseURL(srv.URL)), &sent, &auth
}

func TestReportTracePostsWithTheFunctionKeyAndReturnsTheAnswer(t *testing.T) {
	fn, sent, auth := reportStub(t, 201, map[string]any{"trace_id": strings.Repeat("a", 32), "duplicate": false})
	got, err := fn.ReportTrace(map[string]any{"trace_id": strings.Repeat("a", 32), "function_id": 3, "version_id": 4})
	if err != nil {
		t.Fatal(err)
	}
	if got.TraceID != strings.Repeat("a", 32) || got.Duplicate {
		t.Fatalf("bad receipt: %+v", got)
	}
	if *auth != "Bearer fnk_x" {
		t.Fatalf("expected the function key, got %q", *auth)
	}
	if (*sent)["function_id"] != float64(3) {
		t.Fatalf("function_id not sent: %+v", *sent)
	}
}

func TestReportTraceFillsInWhatIsLeftOut(t *testing.T) {
	fn, sent, _ := reportStub(t, 201, map[string]any{"trace_id": "x", "duplicate": false})
	if _, err := fn.ReportTrace(map[string]any{"function_id": 3}); err != nil {
		t.Fatal(err)
	}
	if (*sent)["schema_version"] != float64(1) || (*sent)["source"] != "self_hosted" {
		t.Fatalf("defaults missing: %+v", *sent)
	}
	if id, _ := (*sent)["trace_id"].(string); len(id) != 32 {
		t.Fatalf("trace_id should be 32 characters: %v", (*sent)["trace_id"])
	}
	if at, _ := (*sent)["created_at"].(string); !regexp.MustCompile(`^\d{4}-\d{2}-\d{2}T[\d:.]+Z$`).MatchString(at) {
		t.Fatalf("created_at should be an ISO time in UTC: %v", (*sent)["created_at"])
	}
}

func TestReportTraceKeepsWhatTheCallerSetAndSaysWhenItWasAlreadyReceived(t *testing.T) {
	fn, sent, _ := reportStub(t, 200, map[string]any{"trace_id": "x", "duplicate": true})
	got, err := fn.ReportTrace(map[string]any{"trace_id": strings.Repeat("b", 32), "created_at": "2026-10-20T09:00:00Z"})
	if err != nil {
		t.Fatal(err)
	}
	if (*sent)["trace_id"] != strings.Repeat("b", 32) || (*sent)["created_at"] != "2026-10-20T09:00:00Z" {
		t.Fatalf("caller values were overwritten: %+v", *sent)
	}
	if !got.Duplicate {
		t.Fatal("expected duplicate to be true")
	}
}

func TestReportTraceLetsTheServersRefusalThrough(t *testing.T) {
	fn, _, _ := reportStub(t, 422, map[string]any{"detail": "source: a reported trace must be 'self_hosted'"})
	_, err := fn.ReportTrace(map[string]any{"function_id": 3})
	if err == nil || !strings.Contains(err.Error(), "self_hosted") {
		t.Fatalf("expected the refusal reason, got %v", err)
	}
}
