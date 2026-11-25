package fnai

// FNA-REQ-530: run a dataset through your own code and get scored results.
//
//	res, err := c.Eval.Run(5, func(in map[string]any, rec *fnai.EvalRecorder) (any, error) {
//	    answer, err := myAgent(in)         // your model calls and tools, on your own computer
//	    rec.Tool("lookup", map[string]any{"id": 1}, map[string]any{"ok": true}, nil, 12)
//	    return map[string]any{"answer": answer}, err
//	}, fnai.EvalOptions{Dataset: "tickets", ScoreSet: 4, Concurrency: 4})
//
// Functional AI hands out the rows, your task runs each one, the steps it recorded are sent with the result, and the
// experiment is finished and scored. See docs/user-run-experiments.md for the three calls underneath.

import (
	"encoding/json"
	"fmt"
	"math"
	"strconv"
	"strings"
	"sync"
	"time"
)

// EvalRecorder collects the steps of one row: model calls and tool calls, in the trace step format. Go has no
// ambient context to find "the current trace", so the task is handed one.
type EvalRecorder struct {
	mu    sync.Mutex
	steps []map[string]any
	cost  float64
}

// Step adds a step you built yourself ({"type": "llm"|"tool", …}). At most 500 are kept (the server's limit).
func (r *EvalRecorder) Step(step map[string]any) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if len(r.steps) < 500 {
		r.steps = append(r.steps, step)
	}
}

// Tool records one tool call. Pass a nil err for success.
func (r *EvalRecorder) Tool(name string, args map[string]any, output any, err error, latencyMs int) {
	if args == nil {
		args = map[string]any{}
	}
	var errText any
	if err != nil {
		errText = err.Error()
	}
	r.Step(map[string]any{"type": "tool", "tool": name, "args": args, "ok": err == nil, "output": output,
		"error": errText, "latency_ms": latencyMs})
}

// LLM records one model call.
func (r *EvalRecorder) LLM(model string, tokensIn, tokensOut, latencyMs int, cost float64) {
	r.Step(map[string]any{"type": "llm", "model": model, "tokens_in": tokensIn, "tokens_out": tokensOut,
		"cost": cost, "latency_ms": latencyMs})
}

// Cost adds to the row's cost, in dollars, if you know it beyond what the LLM steps carry.
func (r *EvalRecorder) Cost(usd float64) {
	r.mu.Lock()
	r.cost += usd
	r.mu.Unlock()
}

// EvalTask runs one dataset row on your side. Return the answer (a map, or any value — wrapped as {"output": v});
// return an error, or panic, and that row is recorded as failed — the run goes on.
type EvalTask func(input map[string]any, rec *EvalRecorder) (any, error)

// EvalOptions are the choices for Run; every field is optional.
type EvalOptions struct {
	Dataset     any    // dataset id (int) or name (string); default: the newest. A fresh snapshot is taken.
	Snapshot    int    // use this snapshot instead of taking one
	ScoreSet    int    // the Score Set id; default: every check on the function
	Name        string // a name for the experiment
	Version     int    // the version NUMBER to test (2 for v2; a draft is fine; default: the function's newest)
	Repeats     int    // default 1
	Concurrency int    // rows at once, default 1
	Resume      int    // carry on an experiment that stopped part-way; rows already sent are skipped
	// WaitTimeout is how long to wait for scoring to finish after the last result is sent (default 10 minutes).
	WaitTimeout time.Duration
	// Progress is called after each row is sent.
	Progress func(done, total, row int, ok bool)
}

// EvalCheck is one check's pass rate.
type EvalCheck struct {
	Score    any      `json:"score"`
	Model    any      `json:"model"`
	PassRate *float64 `json:"pass_rate"`
	N        int      `json:"n"` // rows behind this line
}

// EvalResult is the summary Run returns.
type EvalResult struct {
	ExperimentID int         `json:"experiment_id"`
	FunctionID   int         `json:"function_id"`
	Status       string      `json:"status"`
	Rows         int         `json:"rows"`
	Repeats      int         `json:"repeats"`
	Sent         int         `json:"sent"`
	TaskFailed   int         `json:"task_failed"`
	NotRun       int         `json:"not_run"`
	PassRate     *float64    `json:"pass_rate"` // nil when no check reported one
	Checks       []EvalCheck `json:"checks"`
	URL          string      `json:"url"`
	// FNA-REQ-542: the first ten rows that failed, small enough to read at a glance, so an assistant can edit and run again.
	FailedTotal int             `json:"failed_total"`
	FailedRows  []EvalFailedRow `json:"failed_rows"`
}

// MaxFailedRows is how many failed rows a summary lists; the link has the rest.
const MaxFailedRows = 10

const failedInputChars = 400

// EvalFailedRow is one row that failed a check. Why is the check's own explanation. Row is the row's number in the
// dataset (from 1). A long Input is cut to a string.
type EvalFailedRow struct {
	Row     int    `json:"row"`
	Check   string `json:"check"`
	Model   string `json:"model,omitempty"`
	Input   any    `json:"input"`
	Why     string `json:"why"`
	TraceID string `json:"trace_id,omitempty"`
}

// FailedRows reads the rows of an experiment that failed a check: the total, and the first `limit` of them. A failure to
// read them is returned as an error; callers that must still print a summary ignore it.
func (s *ExperimentsService) FailedRows(expID, limit int) (int, []EvalFailedRow, error) {
	if err := requireID(expID, "expID"); err != nil {
		return 0, nil, err
	}
	var page struct {
		Total int `json:"total"`
		Items []struct {
			Row       int    `json:"row"`
			Input     any    `json:"input"`
			ScoreName string `json:"score_name"`
			Model     string `json:"model"`
			Feedback  string `json:"feedback"`
			TraceID   string `json:"trace_id"`
		} `json:"items"`
	}
	q := map[string][]string{"result": {"failed"}, "limit": {strconv.Itoa(limit)}}
	if err := s.h.do("GET", fmt.Sprintf("/api/experiments/%d/check-results", expID), nil, q, &page); err != nil {
		return 0, nil, err
	}
	rows := make([]EvalFailedRow, 0, len(page.Items))
	for _, it := range page.Items {
		var in any = it.Input
		if b, err := json.Marshal(it.Input); err == nil && len(b) > failedInputChars {
			in = string(b[:failedInputChars]) + "…"
		}
		rows = append(rows, EvalFailedRow{Row: it.Row, Check: it.ScoreName, Model: it.Model, Input: in, Why: it.Feedback,
			TraceID: it.TraceID})
	}
	return page.Total, rows, nil
}

// how often Run asks whether scoring has finished
var evalPollEvery = 2 * time.Second

var evalRetryWaits = []time.Duration{500 * time.Millisecond, time.Second, 2 * time.Second}

// doWithRetry makes one call that is safe to repeat (reading, replacing a row, finishing): a brief network problem or a busy
// server is retried a few times with a short wait, anything else is returned at once.
func (s *EvalService) doWithRetry(method, path string, body any, out any) error {
	for attempt := 0; ; attempt++ {
		err := s.h.do(method, path, body, nil, out)
		if err == nil {
			return nil
		}
		e, ok := err.(*Error)
		retryable := ok && (e.Status == 0 || e.Status == 408 || e.Status == 425 || e.Status == 429 || e.Status >= 500)
		if !retryable || attempt >= len(evalRetryWaits) {
			return err
		}
		time.Sleep(evalRetryWaits[attempt])
	}
}

func (s *EvalService) putWithRetry(path string, body map[string]any) error {
	return s.doWithRetry("PUT", path, body, nil)
}

// runOne runs the task on one row and never panics: a failure becomes a failed row.
func runOne(task EvalTask, input map[string]any) (body map[string]any) {
	rec := &EvalRecorder{}
	t0 := time.Now()
	var out any
	var runErr error
	func() {
		defer func() {
			if r := recover(); r != nil {
				runErr = fmt.Errorf("panic: %v", r)
			}
		}()
		out, runErr = task(input, rec)
	}()
	rec.mu.Lock()
	defer rec.mu.Unlock()
	tin, tout, cost, model := 0, 0, rec.cost, ""
	for _, st := range rec.steps {
		if st["type"] != "llm" {
			continue
		}
		tin += toInt(st["tokens_in"])
		tout += toInt(st["tokens_out"])
		if f, ok := st["cost"].(float64); ok {
			cost += f
		}
		if m, ok := st["model"].(string); ok && m != "" {
			model = m
		}
	}
	body = map[string]any{"steps": rec.steps, "tokens_in": tin, "tokens_out": tout,
		"cost": math.Round(cost*1e8) / 1e8, "latency_ms": int(time.Since(t0).Milliseconds())}
	if body["steps"] == nil {
		body["steps"] = []map[string]any{}
	}
	if model != "" {
		if len(model) > 80 {
			model = model[:80]
		}
		body["model"] = model
	}
	if runErr != nil {
		body["output"], body["error"], body["success"] = nil, runErr.Error(), false
		return body
	}
	if m, ok := out.(map[string]any); ok {
		body["output"] = m
	} else {
		body["output"] = map[string]any{"output": out}
	}
	body["error"], body["success"] = nil, true
	return body
}

func toInt(v any) int {
	switch n := v.(type) {
	case int:
		return n
	case int64:
		return int(n)
	case float64:
		return int(n)
	}
	return 0
}

// Run runs a dataset through YOUR code and returns the scored summary. fnID is the function's id.
func (s *EvalService) Run(fnID int, task EvalTask, opts EvalOptions) (*EvalResult, error) {
	if err := requireID(fnID, "fnID"); err != nil {
		return nil, err
	}
	if task == nil {
		return nil, validationErrorf("task must be a function that takes a row's input and returns the answer.")
	}
	if opts.Concurrency < 1 {
		opts.Concurrency = 1
	}
	if opts.Repeats < 1 {
		opts.Repeats = 1
	}
	if opts.WaitTimeout <= 0 {
		opts.WaitTimeout = 10 * time.Minute
	}
	fn := strconv.Itoa(fnID)

	expID := opts.Resume
	if expID == 0 {
		snap := opts.Snapshot
		if snap == 0 {
			var sets []DatasetOut
			if err := s.h.do("GET", "/api/functions/"+fn+"/datasets", nil, nil, &sets); err != nil {
				return nil, err
			}
			dsID := 0
			switch d := opts.Dataset.(type) {
			case nil:
				if len(sets) == 0 {
					return nil, validationErrorf("this function has no datasets yet — create one in the app, or set Dataset.")
				}
				dsID = sets[len(sets)-1].ID
			case int:
				dsID = d
			case string:
				for _, x := range sets {
					if x.Name == d {
						dsID = x.ID
					}
				}
				if dsID == 0 {
					return nil, validationErrorf("no dataset named %q on this function.", d)
				}
			default:
				return nil, validationErrorf("Dataset must be an id (int) or a name (string).")
			}
			var sn struct {
				ID int `json:"id"`
			}
			if err := s.h.do("POST", fmt.Sprintf("/api/datasets/%d/snapshot", dsID), map[string]any{}, nil, &sn); err != nil {
				return nil, err
			}
			snap = sn.ID
		}
		// the server wants version_ids present; an empty list means "the function's newest version"
		pins := map[string]any{"mode": "user_run", "snapshot_id": snap, "repeats": opts.Repeats, "version_ids": []int{}}
		if opts.Name != "" {
			pins["name"] = opts.Name
		}
		if opts.Version != 0 {
			// Version is the version NUMBER (2 for v2); the server pins an id, so look it up. Sending the number as an id
			// pinned another function's version, or none, and the run sat "pending".
			var known []struct {
				ID     int `json:"id"`
				Number int `json:"number"`
			}
			if err := s.h.do("GET", "/api/functions/"+fn+"/versions", nil, nil, &known); err != nil {
				return nil, err
			}
			versionID, have := 0, []string{}
			for _, v := range known {
				have = append(have, strconv.Itoa(v.Number))
				if v.Number == opts.Version {
					versionID = v.ID
				}
			}
			if versionID == 0 {
				return nil, validationErrorf("this function has no version %d (it has: %s). Version is the version number you see in the app, such as 2 for v2.",
					opts.Version, strings.Join(have, ", "))
			}
			pins["version_ids"] = []int{versionID}
		}
		if opts.ScoreSet != 0 {
			pins["plan_id"] = opts.ScoreSet
		} else {
			scores, err := s.Scores.List(fnID)
			if err != nil {
				return nil, err
			}
			if len(scores) == 0 {
				return nil, validationErrorf("this function has no checks yet — set ScoreSet or add checks in the app.")
			}
			ids := make([]int, 0, len(scores))
			for _, sc := range scores {
				ids = append(ids, sc.ID)
			}
			pins["score_ids"] = ids
		}
		var created struct {
			ID int `json:"id"`
		}
		if err := s.h.do("POST", "/api/functions/"+fn+"/experiments", pins, nil, &created); err != nil {
			return nil, err
		}
		expID = created.ID
	}

	var given struct {
		Rows []struct {
			Row   int            `json:"row"`
			Input map[string]any `json:"input"`
		} `json:"rows"`
		Repeats  int `json:"repeats"`
		Received []struct {
			Row    int `json:"row"`
			Repeat int `json:"repeat"`
		} `json:"received"`
	}
	exp := "/api/experiments/" + strconv.Itoa(expID)
	if err := s.doWithRetry("GET", exp+"/user-run/rows", nil, &given); err != nil {
		return nil, err
	}
	if given.Repeats < 1 {
		given.Repeats = 1
	}
	done := map[[2]int]bool{}
	for _, r := range given.Received {
		done[[2]int{r.Row, r.Repeat}] = true
	}
	type job struct {
		row, rep int
		input    map[string]any
	}
	var jobs []job
	for _, r := range given.Rows {
		for rep := 1; rep <= given.Repeats; rep++ {
			if !done[[2]int{r.Row, rep}] {
				in := r.Input
				if in == nil {
					in = map[string]any{}
				}
				jobs = append(jobs, job{r.Row, rep, in})
			}
		}
	}
	total := len(given.Rows) * given.Repeats

	var mu sync.Mutex
	sent, failed := len(done), 0
	var firstErr error
	ch := make(chan job)
	var wg sync.WaitGroup
	for w := 0; w < opts.Concurrency; w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := range ch {
				body := runOne(task, j.input)
				body["repeat"] = j.rep
				err := s.putWithRetry(fmt.Sprintf("%s/user-run/rows/%d", exp, j.row), body)
				mu.Lock()
				if err != nil {
					if firstErr == nil {
						firstErr = err
					}
				} else {
					sent++
					ok := body["success"] == true
					if !ok {
						failed++
					}
					if opts.Progress != nil {
						opts.Progress(sent, total, j.row, ok)
					}
				}
				mu.Unlock()
			}
		}()
	}
	for _, j := range jobs {
		ch <- j
	}
	close(ch)
	wg.Wait()
	if firstErr != nil {
		return nil, fmt.Errorf("could not send a result (experiment %d keeps what arrived; call Run again with Resume: %d): %w", expID, expID, firstErr)
	}

	var fin struct {
		Status string `json:"status"`
	}
	if err := s.doWithRetry("POST", exp+"/user-run/finish", map[string]any{}, &fin); err != nil {
		return nil, err
	}
	deadline := time.Now().Add(opts.WaitTimeout)
	for fin.Status != "completed" && fin.Status != "failed" && time.Now().Before(deadline) {
		time.Sleep(evalPollEvery)
		if err := s.doWithRetry("GET", exp, nil, &fin); err != nil {
			return nil, err
		}
	}
	var results []EvalCheck
	q := map[string][]string{"experiment_ids": {strconv.Itoa(expID)}, "group_by": {"experiment"}}
	_ = s.h.do("GET", "/api/functions/"+fn+"/score-results", nil, q, &results)
	res := &EvalResult{ExperimentID: expID, FunctionID: fnID, Status: fin.Status, Rows: len(given.Rows),
		Repeats: given.Repeats, Sent: sent, TaskFailed: failed, NotRun: maxInt(0, total-sent), Checks: []EvalCheck{},
		URL: fmt.Sprintf("%s/fn/%d?panel=experiments", s.h.base, fnID)}
	// overall pass rate: each check group weighted by the rows it covers (a row that crashed in your code has no
	// model, so it forms its own group)
	sum, n := 0.0, 0
	for _, r := range results {
		res.Checks = append(res.Checks, r)
		if r.PassRate != nil {
			w := r.N
			if w < 1 {
				w = 1
			}
			sum += *r.PassRate * float64(w)
			n += w
		}
	}
	if n > 0 {
		avg := sum / float64(n)
		res.PassRate = &avg
	}
	res.FailedRows = []EvalFailedRow{}
	if fin.Status == "completed" {
		if total, rows, err := s.Experiments.FailedRows(expID, MaxFailedRows); err == nil { // best effort: the summary still prints
			res.FailedTotal, res.FailedRows = total, rows
		}
	}
	return res, nil
}

func maxInt(a, b int) int {
	if a > b {
		return a
	}
	return b
}
