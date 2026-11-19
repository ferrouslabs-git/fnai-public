package fnai

import (
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
)

// FNA-REQ-530: Eval.Run — a dataset run through your own code.
type evalFake struct {
	mu       sync.Mutex
	put      map[int]map[string]any
	created  []map[string]any
	finished int
	received []map[string]any
	passRate float64
	busy     int // answer this many PUTs with 503 first
	polls    int
}

func evalServer(t *testing.T, f *evalFake) *Client {
	t.Helper()
	f.put = map[int]map[string]any{}
	if f.passRate == 0 {
		f.passRate = 1
	}
	evalRetryWaits = []time.Duration{0, 0, 0}
	evalPollEvery = 0
	mux := http.NewServeMux()
	reply := func(w http.ResponseWriter, v any) { json.NewEncoder(w).Encode(v) }
	mux.HandleFunc("GET /api/functions/5/datasets", func(w http.ResponseWriter, r *http.Request) {
		reply(w, []map[string]any{{"id": 1, "name": "old"}, {"id": 2, "name": "tickets"}})
	})
	mux.HandleFunc("GET /api/functions/5/scores", func(w http.ResponseWriter, r *http.Request) {
		reply(w, []map[string]any{{"id": 11}, {"id": 12}})
	})
	mux.HandleFunc("POST /api/datasets/2/snapshot", func(w http.ResponseWriter, r *http.Request) { reply(w, map[string]any{"id": 77}) })
	mux.HandleFunc("POST /api/functions/5/experiments", func(w http.ResponseWriter, r *http.Request) {
		var b map[string]any
		json.NewDecoder(r.Body).Decode(&b)
		f.mu.Lock()
		f.created = append(f.created, b)
		f.mu.Unlock()
		reply(w, map[string]any{"id": 9})
	})
	mux.HandleFunc("GET /api/experiments/9/user-run/rows", func(w http.ResponseWriter, r *http.Request) {
		rows := []map[string]any{}
		for i := 1; i <= 3; i++ {
			rows = append(rows, map[string]any{"row": i, "input": map[string]any{"q": "question " + strconv.Itoa(i)}})
		}
		reply(w, map[string]any{"rows": rows, "repeats": 1, "received": f.received})
	})
	mux.HandleFunc("PUT /api/experiments/9/user-run/rows/{row}", func(w http.ResponseWriter, r *http.Request) {
		f.mu.Lock()
		defer f.mu.Unlock()
		if f.busy > 0 {
			f.busy--
			w.WriteHeader(503)
			reply(w, map[string]any{"detail": "busy"})
			return
		}
		var b map[string]any
		json.NewDecoder(r.Body).Decode(&b)
		n, _ := strconv.Atoi(r.PathValue("row"))
		f.put[n] = b
		reply(w, map[string]any{"ok": true})
	})
	mux.HandleFunc("POST /api/experiments/9/user-run/finish", func(w http.ResponseWriter, r *http.Request) {
		f.mu.Lock()
		f.finished++
		f.mu.Unlock()
		reply(w, map[string]any{"id": 9, "status": "pending"})
	})
	mux.HandleFunc("GET /api/experiments/9", func(w http.ResponseWriter, r *http.Request) {
		f.mu.Lock()
		f.polls++
		n := f.polls
		f.mu.Unlock()
		status := "running"
		if n >= 2 {
			status = "completed"
		}
		reply(w, map[string]any{"id": 9, "status": status})
	})
	mux.HandleFunc("GET /api/functions/5/score-results", func(w http.ResponseWriter, r *http.Request) {
		reply(w, []map[string]any{{"score": "has answer", "pass_rate": f.passRate}})
	})
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return NewClient("fnp_test", WithBaseURL(srv.URL))
}

func TestEvalRunSendsEveryRowThenFinishes(t *testing.T) {
	f := &evalFake{}
	c := evalServer(t, f)
	res, err := c.Eval.Run(5, func(in map[string]any, rec *EvalRecorder) (any, error) {
		rec.Tool("lookup", map[string]any{"q": in["q"]}, map[string]any{"ok": true}, nil, 5)
		rec.LLM("gpt-4o-mini", 100, 10, 20, 0.001)
		return map[string]any{"answer": strings.ToUpper(in["q"].(string))}, nil
	}, EvalOptions{Dataset: "tickets", ScoreSet: 4, Concurrency: 2})
	if err != nil {
		t.Fatal(err)
	}
	if len(f.put) != 3 || f.finished != 1 {
		t.Fatalf("expected 3 rows and 1 finish, got %d / %d", len(f.put), f.finished)
	}
	if got := f.created[0]; got["mode"] != "user_run" || got["snapshot_id"] != float64(77) || got["plan_id"] != float64(4) {
		t.Fatalf("bad create body: %v", got)
	}
	b := f.put[2]
	if out := b["output"].(map[string]any); out["answer"] != "QUESTION 2" || b["success"] != true {
		t.Fatalf("bad row body: %v", b)
	}
	if steps := b["steps"].([]any); len(steps) != 2 || b["model"] != "gpt-4o-mini" || b["tokens_in"] != float64(100) {
		t.Fatalf("steps/usage not sent: %v", b)
	}
	if res.Status != "completed" || f.polls < 2 {
		t.Fatalf("it should wait for scoring, got status %q after %d polls", res.Status, f.polls)
	}
	if res.ExperimentID != 9 || res.Rows != 3 || res.TaskFailed != 0 || res.NotRun != 0 || res.PassRate == nil || *res.PassRate != 1 {
		t.Fatalf("bad summary: %+v", res)
	}
	if !strings.HasSuffix(res.URL, "/fn/5?panel=experiments") {
		t.Fatalf("bad url %q", res.URL)
	}
}

func TestEvalRunErrorOrPanicFailsThatRowOnly(t *testing.T) {
	f := &evalFake{}
	c := evalServer(t, f)
	res, err := c.Eval.Run(5, func(in map[string]any, rec *EvalRecorder) (any, error) {
		switch in["q"] {
		case "question 2":
			return nil, errors.New("boom")
		case "question 3":
			panic("kaboom")
		}
		return map[string]any{"ok": true}, nil
	}, EvalOptions{Dataset: 2, ScoreSet: 4})
	if err != nil {
		t.Fatal(err)
	}
	if res.TaskFailed != 2 || f.put[1]["success"] != true || f.put[2]["success"] != false || f.put[3]["success"] != false {
		t.Fatalf("unexpected: %+v %v", res, f.put)
	}
	if f.put[2]["error"] != "boom" || !strings.Contains(f.put[3]["error"].(string), "kaboom") || f.put[2]["output"] != nil {
		t.Fatalf("bad failure bodies: %v %v", f.put[2], f.put[3])
	}
	if f.finished != 1 {
		t.Fatal("the experiment should still be finished")
	}
}

func TestEvalRunResumeSkipsRowsAlreadySentAndRetriesABusyServer(t *testing.T) {
	f := &evalFake{received: []map[string]any{{"row": 1, "repeat": 1}}, busy: 2}
	c := evalServer(t, f)
	var seen []string
	res, err := c.Eval.Run(5, func(in map[string]any, rec *EvalRecorder) (any, error) {
		seen = append(seen, in["q"].(string))
		return map[string]any{"a": 1}, nil
	}, EvalOptions{Resume: 9})
	if err != nil {
		t.Fatal(err)
	}
	if len(seen) != 2 || res.Sent != 3 || len(f.put) != 2 {
		t.Fatalf("seen=%v res=%+v put=%d", seen, res, len(f.put))
	}
}

func TestEvalRunWithoutScoreSetUsesEveryCheckAndRejectsBadInput(t *testing.T) {
	f := &evalFake{}
	c := evalServer(t, f)
	if _, err := c.Eval.Run(5, func(map[string]any, *EvalRecorder) (any, error) { return map[string]any{}, nil }, EvalOptions{Dataset: "tickets"}); err != nil {
		t.Fatal(err)
	}
	if ids := f.created[0]["score_ids"].([]any); len(ids) != 2 {
		t.Fatalf("expected both checks, got %v", ids)
	}
	if _, err := c.Eval.Run(5, nil, EvalOptions{}); err == nil {
		t.Fatal("a nil task must be refused")
	}
	if _, err := c.Eval.Run(5, func(map[string]any, *EvalRecorder) (any, error) { return nil, nil }, EvalOptions{Dataset: "nope"}); err == nil || !strings.Contains(err.Error(), "no dataset named") {
		t.Fatalf("expected a no-dataset error, got %v", err)
	}
}
