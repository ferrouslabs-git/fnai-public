package main

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
)

// FNA-REQ-542: `fn eval` names the failed rows and why, as readable lines and as JSON only.
func evalCLIServer(t *testing.T, passRate float64) {
	t.Helper()
	mux := http.NewServeMux()
	reply := func(w http.ResponseWriter, v any) { json.NewEncoder(w).Encode(v) }
	mux.HandleFunc("POST /api/functions/5/experiments", func(w http.ResponseWriter, r *http.Request) { reply(w, map[string]any{"id": 9}) })
	mux.HandleFunc("POST /api/experiments/9/run", func(w http.ResponseWriter, r *http.Request) { reply(w, map[string]any{"id": 9, "status": "completed"}) })
	mux.HandleFunc("GET /api/experiments/9/findings", func(w http.ResponseWriter, r *http.Request) { reply(w, []map[string]any{}) })
	mux.HandleFunc("GET /api/experiments/9", func(w http.ResponseWriter, r *http.Request) {
		reply(w, map[string]any{"id": 9, "status": "completed", "results": map[string]any{"baseline": map[string]any{"verdict": "worse"}}})
	})
	mux.HandleFunc("GET /api/functions/5/score-results", func(w http.ResponseWriter, r *http.Request) {
		reply(w, []map[string]any{{"score": "has answer", "pass_rate": passRate, "n": 2}})
	})
	mux.HandleFunc("GET /api/experiments/9/check-results", func(w http.ResponseWriter, r *http.Request) {
		items := []map[string]any{}
		if passRate < 1 {
			items = append(items, map[string]any{"row": 2, "input": map[string]any{"q": "question 2"}, "score_name": "has answer",
				"model": "mock-small", "feedback": "answer was 'Wrong Co', expected 'Acme'", "trace_id": "t2"})
		}
		reply(w, map[string]any{"total": len(items), "limit": 10, "offset": 0, "items": items})
	})
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	t.Setenv("HOME", t.TempDir())
	t.Setenv("USERPROFILE", t.TempDir())
	t.Setenv("FNAI_TOKEN", "fnp_test")
	t.Setenv("FNAI_BASE_URL", srv.URL)
}

// capture runs f with stdout and stderr redirected and returns what each received.
func capture(t *testing.T, f func() error) (out, errOut string, err error) {
	t.Helper()
	ro, wo, _ := os.Pipe()
	re, we, _ := os.Pipe()
	oldO, oldE := os.Stdout, os.Stderr
	os.Stdout, os.Stderr = wo, we
	err = f()
	wo.Close()
	we.Close()
	os.Stdout, os.Stderr = oldO, oldE
	bo, _ := io.ReadAll(ro)
	be, _ := io.ReadAll(re)
	return string(bo), string(be), err
}

var evalArgs = []string{"--version", "3", "--snapshot", "77", "--scores", "11", "5"}

func TestFnEvalNamesTheFailedRowsAndStillFails(t *testing.T) {
	evalCLIServer(t, 0.5)
	out, _, err := capture(t, func() error { return cmdEval(evalArgs) })
	if err == nil || !strings.Contains(err.Error(), "1 check(s) failed") {
		t.Fatalf("a failing run must still return an error, got %v", err)
	}
	for _, want := range []string{"FAIL  has answer", "1 of 1 failed row(s):", "row 2  has answer  mock-small",
		"answer was 'Wrong Co', expected 'Acme'", "/fn/5?panel=experiments"} {
		if !strings.Contains(out, want) {
			t.Fatalf("output is missing %q:\n%s", want, out)
		}
	}
}

func TestFnEvalJSONIsOnlyJSONAndSaysHowTheRunCompares(t *testing.T) {
	evalCLIServer(t, 0.5)
	out, errOut, err := capture(t, func() error { return cmdEval(append([]string{"--json"}, evalArgs...)) })
	if err == nil {
		t.Fatal("a failing run must still return an error")
	}
	var res struct {
		Passed      bool `json:"passed"`
		FailedTotal int  `json:"failed_total"`
		FailedRows  []struct {
			Row int
			Why string
		} `json:"failed_rows"`
		URL          string         `json:"url"`
		ComparedWith map[string]any `json:"compared_with"`
	}
	if jerr := json.Unmarshal([]byte(out), &res); jerr != nil {
		t.Fatalf("stdout must be only JSON: %v\n%s", jerr, out)
	}
	if res.Passed || res.FailedTotal != 1 || res.FailedRows[0].Row != 2 || res.ComparedWith["verdict"] != "worse" ||
		!strings.HasSuffix(res.URL, "/fn/5?panel=experiments") {
		t.Fatalf("bad summary: %+v", res)
	}
	if !strings.Contains(errOut, "Running experiment") {
		t.Fatalf("progress should go to stderr, got %q", errOut)
	}
}

func TestFnEvalPassingRunSaysSo(t *testing.T) {
	evalCLIServer(t, 1)
	out, _, err := capture(t, func() error { return cmdEval(evalArgs) })
	if err != nil || !strings.Contains(out, "All checks passed.") {
		t.Fatalf("want a pass, got err=%v out=%s", err, out)
	}
	jout, _, _ := capture(t, func() error { return cmdEval(append([]string{"--json"}, evalArgs...)) })
	if !strings.Contains(jout, `"failed_rows": []`) || strings.Contains(jout, "All checks passed") {
		t.Fatalf("a passing run lists no failed rows and prints only JSON:\n%s", jout)
	}
}
