// Package fnai is the Go client for functionalAI.
//
// Zero dependencies (net/http, encoding/json). Two surfaces, mirroring the
// Python and TypeScript clients namespace-for-namespace:
//
//   - Function — the public runtime surface, keyed by a per-function invoke
//     key (fnk_…): Contract() and Run().
//   - Client — the management surface, keyed by a personal access token
//     (fnp_…, minted in the app under avatar → API tokens): Functions,
//     Versions, Eval.Plans/Experiments/Scores, Datasets, Deployments,
//     Keys.Provider/Invoke, Traces, Auth.Tokens.
//
// Struct types are hand-written for the golden-path surface (the generated
// OpenAPI schema drives the TypeScript types; Go structs are maintained by
// hand deliberately — see the SDK-6 ticket). Dynamic payloads (snapshots,
// JSON Schemas, artifacts) are map[string]any.
//
// Error semantics match the other clients: API-level failures return *Error
// (Status, Detail); an EXECUTION failure is not an error — check RunResult.OK().
package fnai

import (
	"bytes"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"reflect"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
)

// nonAlnumRe is slugify's run of non-alphanumeric characters to collapse
// into a single underscore (mirrors the Python/TS SDKs' _slugify).
var nonAlnumRe = regexp.MustCompile(`[^a-z0-9]+`)

// Version is the client version.
const Version = "0.8.0"

const defaultTimeout = 90 * time.Second // server caps execution at 60s

// Error is an API-level failure (bad key → 401, not found → 404, invalid
// input → 422, timeout → 504). Status 0 means the server was unreachable.
type Error struct {
	Status int
	Detail string
}

func (e *Error) Error() string {
	return fmt.Sprintf("HTTP %d: %s", e.Status, e.Detail)
}

// ValidationError is returned by every management method when its input
// fails validation client-side — a missing/empty required field, a bad id,
// a duplicate name/slug — before any network call is made. The difference
// from *Error (an API-level failure) is that a ValidationError never
// reaches the network at all.
type ValidationError struct{ msg string }

func (e *ValidationError) Error() string { return e.msg }

func validationErrorf(format string, a ...any) error {
	return &ValidationError{msg: fmt.Sprintf(format, a...)}
}

// SpaceNotFoundError is returned by Client.UseSpace when no space with that
// name exists for this account. Checked fresh against GET /api/me every
// call — never a cached guess — so a stale or mistyped name can never
// silently bind.
type SpaceNotFoundError struct{ msg string }

func (e *SpaceNotFoundError) Error() string { return e.msg }

// requireField validates a required string field is present and non-blank.
func requireField(value, field string) error {
	if strings.TrimSpace(value) == "" {
		return validationErrorf("%s is required and cannot be empty.", field)
	}
	return nil
}

// requireMap validates a required map[string]any field. nil is never
// accepted (Go's zero value for an omitted map — that's "wasn't given",
// not "empty"); an empty-but-non-nil map is accepted when allowEmpty is
// true.
func requireMap(value map[string]any, field string, allowEmpty bool) error {
	if value == nil {
		return validationErrorf("%s is required and cannot be nil.", field)
	}
	if !allowEmpty && len(value) == 0 {
		return validationErrorf("%s cannot be empty.", field)
	}
	return nil
}

// requireID validates every {fn,version,dataset,score,plan,experiment,key}ID
// argument — an id is a positive integer.
func requireID(value int, field string) error {
	if value <= 0 {
		return validationErrorf("%s must be a positive integer id, got %d.", field, value)
	}
	return nil
}

type httpClient struct {
	base  string
	token string
	hc    *http.Client
	// space is set via Client.UseSpace(), which always validates the name
	// first — nothing else in this package writes it. Empty means "use the
	// account's default space", exactly today's behavior before this field
	// existed: opt-in, additive, never sent unless explicitly chosen.
	space string
	// draftsFolder is where FunctionDraft files go (default .fnai/drafts next to the program).
	draftsFolder string
	// versionsFolder is where a copy of every saved version goes (default .fnai/versions).
	versionsFolder  string
	noVersionCopies bool
}

// useSpace is Client.UseSpace()'s actual implementation. Validated against
// the account's REAL spaces on every call (a fresh GET /api/me, not a
// cached list that could have gone stale) — so this can never bind to a
// space that doesn't exist or that this token can't see.
func (h *httpClient) useSpace(name string) error {
	if name == "" {
		h.space = ""
		return nil
	}
	var me struct {
		Spaces []struct {
			ID   string `json:"id"`
			Name string `json:"name"`
		} `json:"spaces"`
	}
	if err := h.do("GET", "/api/me", nil, nil, &me); err != nil {
		return err
	}
	names := make([]string, 0, len(me.Spaces))
	for _, sp := range me.Spaces {
		names = append(names, sp.Name)
		if sp.Name == name {
			h.space = name
			return nil
		}
	}
	sort.Strings(names)
	choices := "(none yet — create one first)"
	if len(names) > 0 {
		choices = strings.Join(names, ", ")
	}
	return &SpaceNotFoundError{msg: fmt.Sprintf(
		"no space named %q for this account — choices: %s", name, choices)}
}

func (h *httpClient) do(method, path string, body any, query url.Values, out any) error {
	u := h.base + path
	if len(query) > 0 {
		u += "?" + query.Encode()
	}
	var rdr *bytes.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return err
		}
		rdr = bytes.NewReader(b)
	} else {
		rdr = bytes.NewReader(nil)
	}
	req, err := http.NewRequest(method, u, rdr)
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+h.token)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("User-Agent", "fnai-go/"+Version)
	if h.space != "" {
		req.Header.Set("X-Space", h.space)
	}
	resp, err := h.hc.Do(req)
	if err != nil {
		return &Error{Status: 0, Detail: "cannot reach " + h.base + ": " + err.Error()}
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 400 {
		var parsed struct {
			Detail any `json:"detail"`
		}
		detail := resp.Status
		if json.NewDecoder(resp.Body).Decode(&parsed) == nil && parsed.Detail != nil {
			if str, ok := parsed.Detail.(string); ok {
				detail = str
			} else if b, err := json.Marshal(parsed.Detail); err == nil {
				detail = string(b)
			}
		}
		return &Error{Status: resp.StatusCode, Detail: detail}
	}
	if out == nil || resp.StatusCode == http.StatusNoContent {
		return nil
	}
	return json.NewDecoder(resp.Body).Decode(out)
}

// Option configures a Client or Function.
type Option func(*httpClient)

// WithBaseURL points the client at a deployment (default http://localhost:8000).
// WithDraftsFolder says where draft files (the YAML a FunctionDraft keeps up to date on your computer)
// go. Without it they go to $FNAI_DRAFTS_DIR, else .fnai/drafts next to where the program runs.
func WithDraftsFolder(dir string) Option {
	return func(h *httpClient) { h.draftsFolder = dir }
}

// WithVersionsFolder says where a copy of every saved version goes: <dir>/<slug>/v<N>.yaml. Without it they go
// to $FNAI_VERSIONS_DIR, else .fnai/versions beside the drafts folder.
func WithVersionsFolder(dir string) Option {
	return func(h *httpClient) { h.versionsFolder = dir }
}

// WithoutVersionCopies turns the copies of saved versions off: on save the draft file is just removed.
func WithoutVersionCopies() Option {
	return func(h *httpClient) { h.noVersionCopies = true }
}

func WithBaseURL(base string) Option {
	return func(h *httpClient) { h.base = strings.TrimRight(base, "/") }
}

// WithTimeout overrides the transport timeout (default 90s).
func WithTimeout(d time.Duration) Option {
	return func(h *httpClient) { h.hc.Timeout = d }
}

func newHTTP(token string, opts []Option) *httpClient {
	h := &httpClient{base: "http://localhost:8000", token: token,
		hc: &http.Client{Timeout: defaultTimeout}}
	for _, o := range opts {
		o(h)
	}
	return h
}

// ── runtime surface (fnk_* invoke keys) ─────────────────────────────────────

// RunMeta is the per-invocation metadata the platform returns.
type RunMeta struct {
	Version   int     `json:"version"`
	LatencyMS int     `json:"latency_ms"`
	CostUSD   float64 `json:"cost_usd"`
	TokensIn  int     `json:"tokens_in"`
	TokensOut int     `json:"tokens_out"`
	TraceID   string  `json:"trace_id"`
}

// RunResult is one execution. An execution failure is NOT a Go error —
// check OK()/Status; Error holds the execution error text when failed.
type RunResult struct {
	Output map[string]any `json:"output"`
	Status string         `json:"status"`
	Error  string         `json:"error"`
	Meta   RunMeta        `json:"meta"`
}

// OK reports whether the execution succeeded.
func (r *RunResult) OK() bool { return r.Status == "success" }

// Function is a handle on one deployed function (public runtime surface).
type Function struct {
	Slug string
	h    *httpClient
}

// NewFunction creates a runtime handle keyed by an fnk_* invoke key.
func NewFunction(slug, apiKey string, opts ...Option) *Function {
	return &Function{Slug: slug, h: newHTTP(apiKey, opts)}
}

// Contract returns the ACTIVE deployment's input/output schemas.
func (f *Function) Contract() (map[string]any, error) {
	var out map[string]any
	err := f.h.do("GET", "/v1/run/"+f.Slug+"/contract", nil, nil, &out)
	return out, err
}

// Run invokes the active deployment; input is validated server-side.
func (f *Function) Run(input map[string]any) (*RunResult, error) {
	var out RunResult
	err := f.h.do("POST", "/v1/run/"+f.Slug,
		map[string]any{"input": input}, nil, &out)
	if err != nil {
		return nil, err
	}
	return &out, nil
}

// TraceReceipt is the server's answer to ReportTrace.
type TraceReceipt struct {
	TraceID   string `json:"trace_id"`
	Duplicate bool   `json:"duplicate"` // true when this trace had already been received
}

// ReportTrace reports one call that ran on YOUR servers, so it shows in the monitor next to hosted
// calls. Give what only you know: function_id, version_id, artifact_hash, input, output, success,
// error, steps, tokens_in, tokens_out, cost and latency_ms. trace_id, schema_version (1), source
// ("self_hosted") and created_at (now) are filled in when you leave them out; set created_at
// yourself when you report a call later than it happened. Hidden (sensitive) fields are hidden on
// our side. Reporting the same trace_id again is harmless, so retry freely. The server answers 422
// with the reason for a trace it refuses.
func (f *Function) ReportTrace(trace map[string]any) (*TraceReceipt, error) {
	id := make([]byte, 16)
	if _, err := rand.Read(id); err != nil {
		return nil, err
	}
	body := map[string]any{
		"trace_id":       hex.EncodeToString(id),
		"schema_version": 1,
		"source":         "self_hosted",
		"created_at":     time.Now().UTC().Format("2006-01-02T15:04:05.000000Z07:00"),
	}
	for k, v := range trace {
		body[k] = v
	}
	var out TraceReceipt
	if err := f.h.do("POST", "/v1/traces", body, nil, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// ── management types (golden-path surface) ──────────────────────────────────

type FunctionOut struct {
	ID          int    `json:"id"`
	Slug        string `json:"slug"`
	Name        string `json:"name"`
	Description string `json:"description"`
	Archived    bool   `json:"archived"`
}

type VersionOut struct {
	ID           int            `json:"id"`
	Number       int            `json:"number"`
	Status       string         `json:"status"`
	Message      string         `json:"message"`
	Snapshot     map[string]any `json:"snapshot"`
	InputSchema  map[string]any `json:"input_schema"`
	OutputSchema map[string]any `json:"output_schema"`
}

type DeploymentOut struct {
	ID            int    `json:"id"`
	FunctionID    int    `json:"function_id"`
	VersionID     int    `json:"version_id"`
	VersionNumber *int   `json:"version_number"`
	Status        string `json:"status"`
	Action        string `json:"action"`
	// Model and Temperature are what this deployment serves with; Model is empty when the version names its own.
	Model       string   `json:"model,omitempty"`
	Temperature *float64 `json:"temperature,omitempty"`
	// Endpoint and Curl are filled by Deploy: a ready-to-run call against the public runtime (put a
	// minted fnk_* invoke key in place of the placeholder).
	Endpoint string `json:"endpoint,omitempty"`
	Curl     string `json:"curl,omitempty"`
}

type InvokeKeyOut struct {
	ID     int    `json:"id"`
	Name   string `json:"name"`
	Hint   string `json:"hint"`
	Status string `json:"status"`
}

// InvokeKeyMinted carries the plaintext key — in the mint response ONLY.
type InvokeKeyMinted struct {
	InvokeKeyOut
	Key string `json:"key"`
}

type PlaygroundOut struct {
	Output    map[string]any `json:"output"`
	Success   bool           `json:"success"`
	Error     string         `json:"error"`
	TraceID   string         `json:"trace_id"`
	TokensIn  int            `json:"tokens_in"`
	TokensOut int            `json:"tokens_out"`
	Cost      float64        `json:"cost"`
	LatencyMS int            `json:"latency_ms"`
}

type HealthOut struct {
	WindowHours int     `json:"window_hours"`
	Calls       int     `json:"calls"`
	Errors      int     `json:"errors"`
	ErrorRate   float64 `json:"error_rate"`
	P50MS       int     `json:"p50_ms"`
	P95MS       int     `json:"p95_ms"`
	Cost        float64 `json:"cost"`
}

type TraceSummary struct {
	TraceID   string  `json:"trace_id"`
	Source    string  `json:"source"`
	Success   bool    `json:"success"`
	Model     string  `json:"model"`
	Cost      float64 `json:"cost"`
	LatencyMS int     `json:"latency_ms"`
}

type TokenOut struct {
	ID         string  `json:"id"`
	Name       string  `json:"name"`
	Prefix     string  `json:"prefix"`
	CreatedAt  string  `json:"created_at"`
	LastUsedAt *string `json:"last_used_at"`
	RevokedAt  *string `json:"revoked_at"`
}

// TokenMinted carries the plaintext token — in the mint response ONLY.
type TokenMinted struct {
	TokenOut
	Token string `json:"token"`
}

type PlanBrief struct {
	ID         int    `json:"id"`
	Name       string `json:"name"`
	Stage      string `json:"stage"`
	ScoreCount int    `json:"score_count"`
	GateCount  int    `json:"gate_count"`
}

type PlanCreated struct {
	ID   int    `json:"id"`
	Name string `json:"name"`
}

type ExperimentOut struct {
	ID      int            `json:"id"`
	Name    string         `json:"name"`
	Status  string         `json:"status"`
	Cost    float64        `json:"cost"`
	Results map[string]any `json:"results"`
	// Findings is filled by Run (what the experiment turned up, alongside the scores).
	Findings []Finding `json:"findings,omitempty"`
}

type ScoreOut struct {
	ID   int    `json:"id"`
	Name string `json:"name"`
	Tier string `json:"tier"`
}

type DatasetOut struct {
	ID   int    `json:"id"`
	Name string `json:"name"`
}

type TypesOut struct {
	Language string `json:"language"`
	Function string `json:"function"`
	Version  *int   `json:"version"`
	Source   string `json:"source"`
}

// ── management services ──────────────────────────────────────────────────────

// FunctionsService owns functions, their versions, health, and contract types.
type FunctionsService struct{ h *httpClient }

// FunctionCreate is the create payload; schemas are JSON Schema objects.
type FunctionCreate struct {
	Slug         string         `json:"slug"`
	Name         string         `json:"name"`
	InputSchema  map[string]any `json:"input_schema"`
	OutputSchema map[string]any `json:"output_schema"`
}

func (s *FunctionsService) List() ([]FunctionOut, error) {
	var out []FunctionOut
	return out, s.h.do("GET", "/api/functions", nil, nil, &out)
}

func (s *FunctionsService) Get(fnID int) (*FunctionOut, error) {
	if err := requireID(fnID, "fnID"); err != nil {
		return nil, err
	}
	var out FunctionOut
	return &out, s.h.do("GET", "/api/functions/"+strconv.Itoa(fnID), nil, nil, &out)
}

func (s *FunctionsService) Create(body FunctionCreate) (*FunctionOut, error) {
	if err := requireField(body.Slug, "Slug"); err != nil {
		return nil, err
	}
	if err := requireField(body.Name, "Name"); err != nil {
		return nil, err
	}
	if err := requireMap(body.InputSchema, "InputSchema", true); err != nil {
		return nil, err
	}
	if err := requireMap(body.OutputSchema, "OutputSchema", true); err != nil {
		return nil, err
	}
	existing, err := s.List()
	if err != nil {
		return nil, err
	}
	for _, f := range existing {
		if f.Slug == body.Slug {
			return nil, validationErrorf(
				"a function with slug %q already exists (id=%d) — pick a different slug, "+
					"or use that function's id instead of creating another.", body.Slug, f.ID)
		}
	}
	var out FunctionOut
	return &out, s.h.do("POST", "/api/functions", body, nil, &out)
}

func (s *FunctionsService) Archive(fnID int) error {
	if err := requireID(fnID, "fnID"); err != nil {
		return err
	}
	return s.h.do("DELETE", "/api/functions/"+strconv.Itoa(fnID), nil, nil, nil)
}

func (s *FunctionsService) Versions(fnID int) ([]VersionOut, error) {
	if err := requireID(fnID, "fnID"); err != nil {
		return nil, err
	}
	var out []VersionOut
	return out, s.h.do("GET", "/api/functions/"+strconv.Itoa(fnID)+"/versions", nil, nil, &out)
}

// CreateVersion snapshots the definition (sections, model_binding, tools, …).
func (s *FunctionsService) CreateVersion(fnID int, snapshot map[string]any, message string) (*VersionOut, error) {
	if err := requireID(fnID, "fnID"); err != nil {
		return nil, err
	}
	if err := requireMap(snapshot, "snapshot", false); err != nil {
		return nil, err
	}
	var out VersionOut
	return &out, s.h.do("POST", "/api/functions/"+strconv.Itoa(fnID)+"/versions",
		map[string]any{"snapshot": snapshot, "message": message}, nil, &out)
}

func (s *FunctionsService) Health(fnID, windowHours int) (*HealthOut, error) {
	if err := requireID(fnID, "fnID"); err != nil {
		return nil, err
	}
	var out HealthOut
	q := url.Values{"window_hours": {strconv.Itoa(windowHours)}}
	return &out, s.h.do("GET", "/api/functions/"+strconv.Itoa(fnID)+"/health", nil, q, &out)
}

// Types returns compilable caller-side types for the function's contract
// (lang: "typescript", "python", or "go").
func (s *FunctionsService) Types(fnID int, lang string) (*TypesOut, error) {
	if err := requireID(fnID, "fnID"); err != nil {
		return nil, err
	}
	var out TypesOut
	q := url.Values{"lang": {lang}}
	return &out, s.h.do("GET", "/api/functions/"+strconv.Itoa(fnID)+"/types", nil, q, &out)
}

// VersionsService owns the version lifecycle and the playground.
type VersionsService struct{ h *httpClient }

func (s *VersionsService) Get(vID int) (*VersionOut, error) {
	if err := requireID(vID, "vID"); err != nil {
		return nil, err
	}
	var out VersionOut
	return &out, s.h.do("GET", "/api/versions/"+strconv.Itoa(vID), nil, nil, &out)
}

func (s *VersionsService) Playground(vID int, input map[string]any, model string) (*PlaygroundOut, error) {
	if err := requireID(vID, "vID"); err != nil {
		return nil, err
	}
	if err := requireMap(input, "input", true); err != nil {
		return nil, err
	}
	if model == "" {
		model = "mock-small"
	}
	var out PlaygroundOut
	return &out, s.h.do("POST", "/api/versions/"+strconv.Itoa(vID)+"/playground/run",
		map[string]any{"input": input, "model": model}, nil, &out)
}

func (s *VersionsService) Submit(vID int) error {
	if err := requireID(vID, "vID"); err != nil {
		return err
	}
	return s.h.do("POST", "/api/versions/"+strconv.Itoa(vID)+"/submit",
		map[string]any{}, nil, nil)
}

func (s *VersionsService) Approve(vID int, comment string) error {
	if err := requireID(vID, "vID"); err != nil {
		return err
	}
	return s.h.do("POST", "/api/versions/"+strconv.Itoa(vID)+"/approve",
		map[string]any{"comment": comment}, nil, nil)
}

func (s *VersionsService) Reject(vID int, comment string) error {
	if err := requireID(vID, "vID"); err != nil {
		return err
	}
	return s.h.do("POST", "/api/versions/"+strconv.Itoa(vID)+"/reject",
		map[string]any{"comment": comment}, nil, nil)
}

// PlansService is eval.plans — named check collections experiments can pin.
type PlansService struct{ h *httpClient }

func (s *PlansService) List(fnID int) ([]PlanBrief, error) {
	if err := requireID(fnID, "fnID"); err != nil {
		return nil, err
	}
	var out []PlanBrief
	return out, s.h.do("GET", "/api/functions/"+strconv.Itoa(fnID)+"/plans", nil, nil, &out)
}

func (s *PlansService) Create(fnID int, name string) (*PlanCreated, error) {
	if err := requireID(fnID, "fnID"); err != nil {
		return nil, err
	}
	if err := requireField(name, "name"); err != nil {
		return nil, err
	}
	var out PlanCreated
	return &out, s.h.do("POST", "/api/functions/"+strconv.Itoa(fnID)+"/plans",
		map[string]any{"name": name}, nil, &out)
}

func (s *PlansService) Scores(planID int) ([]ScoreOut, error) {
	if err := requireID(planID, "planID"); err != nil {
		return nil, err
	}
	var out []ScoreOut
	return out, s.h.do("GET", "/api/plans/"+strconv.Itoa(planID)+"/scores", nil, nil, &out)
}

// ExperimentsService is eval.experiments.
type ExperimentsService struct{ h *httpClient }

func (s *ExperimentsService) List(fnID int) ([]ExperimentOut, error) {
	if err := requireID(fnID, "fnID"); err != nil {
		return nil, err
	}
	var out []ExperimentOut
	return out, s.h.do("GET", "/api/functions/"+strconv.Itoa(fnID)+"/experiments", nil, nil, &out)
}

// Create pins an experiment; pins mirrors the API body (version_ids, models, …).
func (s *ExperimentsService) Create(fnID int, pins map[string]any) (*ExperimentOut, error) {
	if err := requireID(fnID, "fnID"); err != nil {
		return nil, err
	}
	// ExperimentCreate (backend/app/schemas.py) requires version_ids — a
	// planID-only or singleInput-only pin still needs SOME version to run
	// against, so this is the one field worth enforcing here rather than
	// letting an empty pins map reach the backend as a 422.
	v, ok := pins["version_ids"]
	empty := !ok || v == nil
	if !empty {
		rv := reflect.ValueOf(v)
		if (rv.Kind() == reflect.Slice || rv.Kind() == reflect.Array) && rv.Len() == 0 {
			empty = true
		}
	}
	if empty {
		return nil, validationErrorf(`Create needs pins["version_ids"] — at least one version to run.`)
	}
	var out ExperimentOut
	return &out, s.h.do("POST", "/api/functions/"+strconv.Itoa(fnID)+"/experiments", pins, nil, &out)
}

// Run runs a draft experiment. The result carries the scoring details and a Findings list alongside them
// — what the UI shows together. Use RunBare for the experiment without its findings.
func (s *ExperimentsService) Run(expID int) (*ExperimentOut, error) {
	out, err := s.RunBare(expID)
	if err != nil {
		return nil, err
	}
	findings, err := s.Findings(expID)
	if err != nil {
		return nil, err
	}
	out.Findings = findings
	return out, nil
}

// ScoresService is eval.scores — the per-function check library.
type ScoresService struct{ h *httpClient }

func (s *ScoresService) List(fnID int) ([]ScoreOut, error) {
	if err := requireID(fnID, "fnID"); err != nil {
		return nil, err
	}
	var out []ScoreOut
	return out, s.h.do("GET", "/api/functions/"+strconv.Itoa(fnID)+"/scores", nil, nil, &out)
}

// EvalService groups the evaluation loop: Plans, Experiments, Scores.
type EvalService struct {
	Plans       *PlansService
	Experiments *ExperimentsService
	Scores      *ScoresService
}

// DatasetsService owns datasets and rows.
type DatasetsService struct{ h *httpClient }

func (s *DatasetsService) List(fnID int) ([]DatasetOut, error) {
	if err := requireID(fnID, "fnID"); err != nil {
		return nil, err
	}
	var out []DatasetOut
	return out, s.h.do("GET", "/api/functions/"+strconv.Itoa(fnID)+"/datasets", nil, nil, &out)
}

func (s *DatasetsService) Create(fnID int, name string, rows []map[string]any) (*DatasetOut, error) {
	if err := requireID(fnID, "fnID"); err != nil {
		return nil, err
	}
	if err := requireField(name, "name"); err != nil {
		return nil, err
	}
	for i, r := range rows {
		if _, ok := r["input"]; !ok {
			return nil, validationErrorf("rows[%d] is missing required key 'input'.", i)
		}
	}
	if rows == nil {
		rows = []map[string]any{}
	}
	var out DatasetOut
	return &out, s.h.do("POST", "/api/functions/"+strconv.Itoa(fnID)+"/datasets",
		map[string]any{"name": name, "rows": rows}, nil, &out)
}

// DeploymentsService owns deploy/rollback/history.
type DeploymentsService struct{ h *httpClient }

func (s *DeploymentsService) Deploy(fnID, versionID int) (*DeploymentOut, error) {
	return s.deploy(fnID, versionID, false, "", nil)
}

// DeployWithModel deploys a version served by `model` (and optionally `temperature`). A version saved by the
// builder, YAML or the MCP tools names no model, so it needs one here (or a default model on the space);
// otherwise the deploy is refused rather than served by the mock.
func (s *DeploymentsService) DeployWithModel(fnID, versionID int, model string, temperature *float64) (*DeploymentOut, error) {
	return s.deploy(fnID, versionID, false, model, temperature)
}

func (s *DeploymentsService) Current(fnID int) (*DeploymentOut, error) {
	if err := requireID(fnID, "fnID"); err != nil {
		return nil, err
	}
	var out DeploymentOut
	return &out, s.h.do("GET", "/api/functions/"+strconv.Itoa(fnID)+"/deployment", nil, nil, &out)
}

func (s *DeploymentsService) History(fnID int) ([]DeploymentOut, error) {
	if err := requireID(fnID, "fnID"); err != nil {
		return nil, err
	}
	var out []DeploymentOut
	return out, s.h.do("GET", "/api/functions/"+strconv.Itoa(fnID)+"/deployments", nil, nil, &out)
}

func (s *DeploymentsService) Rollback(fnID int, versionID *int) (*DeploymentOut, error) {
	if err := requireID(fnID, "fnID"); err != nil {
		return nil, err
	}
	body := map[string]any{}
	if versionID != nil {
		if err := requireID(*versionID, "versionID"); err != nil {
			return nil, err
		}
		body["version_id"] = *versionID
	}
	var out DeploymentOut
	return &out, s.h.do("POST", "/api/functions/"+strconv.Itoa(fnID)+"/rollback", body, nil, &out)
}

// InvokeKeysService mints/revokes per-function fnk_* runtime keys.
type InvokeKeysService struct{ h *httpClient }

func (s *InvokeKeysService) List(fnID int) ([]InvokeKeyOut, error) {
	if err := requireID(fnID, "fnID"); err != nil {
		return nil, err
	}
	var out []InvokeKeyOut
	return out, s.h.do("GET", "/api/functions/"+strconv.Itoa(fnID)+"/invoke-keys", nil, nil, &out)
}

func (s *InvokeKeysService) Mint(fnID int, name string) (*InvokeKeyMinted, error) {
	if err := requireID(fnID, "fnID"); err != nil {
		return nil, err
	}
	var out InvokeKeyMinted
	return &out, s.h.do("POST", "/api/functions/"+strconv.Itoa(fnID)+"/invoke-keys",
		map[string]any{"name": name}, nil, &out)
}

func (s *InvokeKeysService) Revoke(keyID int) error {
	if err := requireID(keyID, "keyID"); err != nil {
		return err
	}
	return s.h.do("DELETE", "/api/invoke-keys/"+strconv.Itoa(keyID), nil, nil, nil)
}

// ProviderKeysService manages BYOK provider credentials (map-typed: shapes
// vary per provider; see the API reference).
type ProviderKeysService struct{ h *httpClient }

func (s *ProviderKeysService) List() ([]map[string]any, error) {
	var out []map[string]any
	return out, s.h.do("GET", "/api/provider-keys", nil, nil, &out)
}

func (s *ProviderKeysService) Add(body map[string]any) (map[string]any, error) {
	provider, _ := body["provider"].(string)
	if err := requireField(provider, "provider"); err != nil {
		return nil, err
	}
	secret, ok := body["secret"].(map[string]any)
	if !ok {
		return nil, validationErrorf("secret must be a map[string]any.")
	}
	if err := requireMap(secret, "secret", false); err != nil {
		return nil, err
	}
	var out map[string]any
	return out, s.h.do("POST", "/api/provider-keys", body, nil, &out)
}

// ModelsService lists the models you can name when deploying or running an experiment.
type ModelsService struct{ h *httpClient }

// ModelOut is one routable model. Usable is true when this space has what a call needs: the mock always, a
// real model once its provider has an active key. Reason says what is missing when it is not.
type ModelOut struct {
	Model       string  `json:"model"`
	Provider    string  `json:"provider"`
	InputPer1k  float64 `json:"input_per_1k"`
	OutputPer1k float64 `json:"output_per_1k"`
	Usable      bool    `json:"usable"`
	Reason      string  `json:"reason,omitempty"`
}

func (s *ModelsService) List() ([]ModelOut, error) {
	var out []ModelOut
	return out, s.h.do("GET", "/api/models", nil, nil, &out)
}

// KeysService groups the two key families.
type KeysService struct {
	Provider *ProviderKeysService
	Invoke   *InvokeKeysService
}

// TracesService reads traces and health; Flag is the flywheel.
type TracesService struct{ h *httpClient }

func (s *TracesService) List(fnID int, source string, limit int) ([]TraceSummary, error) {
	q := url.Values{}
	if fnID > 0 {
		q.Set("function_id", strconv.Itoa(fnID))
	}
	if source != "" {
		q.Set("source", source)
	}
	if limit > 0 {
		q.Set("limit", strconv.Itoa(limit))
	}
	var out []TraceSummary
	return out, s.h.do("GET", "/api/traces", nil, q, &out)
}

func (s *TracesService) Get(traceID string) (map[string]any, error) {
	if err := requireField(traceID, "traceID"); err != nil {
		return nil, err
	}
	var out map[string]any
	return out, s.h.do("GET", "/api/traces/"+traceID, nil, nil, &out)
}

func (s *TracesService) Flag(traceID string) (map[string]any, error) {
	if err := requireField(traceID, "traceID"); err != nil {
		return nil, err
	}
	var out map[string]any
	return out, s.h.do("POST", "/api/traces/"+traceID+"/flag",
		map[string]any{}, nil, &out)
}

// AuthTokensService manages the caller's own fnp_* personal access tokens.
type AuthTokensService struct{ h *httpClient }

func (s *AuthTokensService) List() ([]TokenOut, error) {
	var out []TokenOut
	return out, s.h.do("GET", "/api/auth/tokens", nil, nil, &out)
}

// Mint returns the plaintext token — in this response ONLY.
func (s *AuthTokensService) Mint(name string) (*TokenMinted, error) {
	if err := requireField(name, "name"); err != nil {
		return nil, err
	}
	var out TokenMinted
	return &out, s.h.do("POST", "/api/auth/tokens",
		map[string]any{"name": name}, nil, &out)
}

func (s *AuthTokensService) Revoke(tokenID string) error {
	if err := requireField(tokenID, "tokenID"); err != nil {
		return err
	}
	return s.h.do("DELETE", "/api/auth/tokens/"+tokenID, nil, nil, nil)
}

// SpaceCreated is the response from SpacesService.Create.
type SpaceCreated struct {
	ID   string `json:"id"`
	Name string `json:"name"`
}

// SpacesService manages spaces.
type SpacesService struct{ h *httpClient }

// Create validates name and that no space with this name already exists
// for the account (the same exact-name match get_scope's X-Space lookup
// uses — see backend/app/auth_usermanagement/security/scope.go), so a
// typo'd "create" that was meant to be a "switch" fails loudly here
// instead of quietly leaving two same-named spaces.
func (s *SpacesService) Create(name string) (*SpaceCreated, error) {
	if err := requireField(name, "name"); err != nil {
		return nil, err
	}
	var me struct {
		Spaces []struct {
			ID   string `json:"id"`
			Name string `json:"name"`
		} `json:"spaces"`
	}
	if err := s.h.do("GET", "/api/me", nil, nil, &me); err != nil {
		return nil, err
	}
	for _, sp := range me.Spaces {
		if sp.Name == name {
			return nil, validationErrorf(
				"a space named %q already exists (id=%s) — use Client.UseSpace(%q) "+
					"to switch to it instead of creating another.", name, sp.ID, name)
		}
	}
	var out SpaceCreated
	return &out, s.h.do("POST", "/api/spaces", map[string]any{"name": name}, nil, &out)
}

// AuthService groups auth-owned resources.
type AuthService struct{ Tokens *AuthTokensService }

// Client is the namespaced management client (fnp_* personal access token).
type Client struct {
	h           *httpClient
	Functions   *FunctionsService
	Spaces      *SpacesService
	Versions    *VersionsService
	Eval        *EvalService
	Datasets    *DatasetsService
	Deployments *DeploymentsService
	Models      *ModelsService
	Tools       *ToolsService
	Keys        *KeysService
	Traces      *TracesService
	Auth        *AuthService
}

// NewClient builds a management client from a personal access token.
func NewClient(token string, opts ...Option) *Client {
	h := newHTTP(token, opts)
	return &Client{
		h:         h,
		Functions: &FunctionsService{h},
		Spaces:    &SpacesService{h},
		Versions:  &VersionsService{h},
		Eval: &EvalService{Plans: &PlansService{h},
			Experiments: &ExperimentsService{h}, Scores: &ScoresService{h}},
		Datasets:    &DatasetsService{h},
		Deployments: &DeploymentsService{h},
		Models:      &ModelsService{h},
		Tools:       &ToolsService{h},
		Keys:        &KeysService{Provider: &ProviderKeysService{h}, Invoke: &InvokeKeysService{h}},
		Traces:      &TracesService{h},
		Auth:        &AuthService{Tokens: &AuthTokensService{h}},
	}
}

// Me returns the caller's identity, roles, and permissions.
func (c *Client) Me() (map[string]any, error) {
	var out map[string]any
	return out, c.h.do("GET", "/api/me", nil, nil, &out)
}

// UseSpace scopes every later space-scoped call to one space by name (the
// X-Space header get_scope reads — see
// backend/app/auth_usermanagement/security/scope.go), instead of whatever
// this token's default space resolves to.
//
// Opt-in: if you never call this, nothing changes — no X-Space header is
// ever sent, same as before this method existed, and the backend resolves
// its own default space exactly as it always has.
//
// Re-validates name against a FRESH GET /api/me every call — returning
// *SpaceNotFoundError rather than accepting a name nobody checked. Pass ""
// to clear the override.
func (c *Client) UseSpace(name string) error {
	return c.h.useSpace(name)
}

// CurrentSpace is the space name UseSpace last set, or "" if nothing has
// been selected (the default state — see UseSpace).
func (c *Client) CurrentSpace() string {
	return c.h.space
}

// Raw calls any management-API path directly, decoding the JSON response
// into out (a pointer, or nil to discard the body). For endpoints this
// hand-maintained golden-path surface doesn't (yet) have a typed method
// for — same authenticated transport, same error semantics as every typed
// call above. Prefer a typed method when one exists; this is the escape
// hatch, not the front door.
func (c *Client) Raw(method, path string, body any, out any) error {
	return c.h.do(method, path, body, nil, out)
}

// Function crosses to the runtime surface on this client's base URL.
func (c *Client) Function(slug, apiKey string) *Function {
	return &Function{Slug: slug,
		h: &httpClient{base: c.h.base, token: apiKey, hc: c.h.hc}}
}

// ── prompt file import — same .yaml/.yml/.json format the canvas UI's Import
// button reads (frontend/src/pages/canvas/yamlImport.ts is the source of
// truth for this format; keep them in sync). Deliberately two separate
// steps: ParsePromptFile/ParsePromptDoc only read and validate — no network
// call, nothing created; CreateFunctionFromDoc is the one that talks to the
// API. ────────────────────────────────────────────────────────────────────

// PromptFileError is returned by ParsePromptFile/ParsePromptDoc when a
// prompt document is missing something required or malformed. It never
// wraps a network error — those come back as *Error instead.
type PromptFileError struct{ msg string }

func (e *PromptFileError) Error() string { return e.msg }

func promptFileErrorf(format string, a ...any) error {
	return &PromptFileError{msg: fmt.Sprintf(format, a...)}
}

var itemKeys = []string{"definitions", "instructions", "examples",
	"negative_examples", "edge_cases", "non_goals"}

var itemLabel = map[string]string{
	"definitions": "Definitions", "instructions": "Instructions", "examples": "Examples",
	"negative_examples": "Negative examples", "edge_cases": "Edge cases", "non_goals": "Non-goals",
}

var knownKeys = func() map[string]bool {
	m := map[string]bool{"role": true, "freeform": true, "inputs": true, "outputs": true}
	for _, k := range itemKeys {
		m[k] = true
	}
	return m
}()

var fieldTypes = map[string]bool{
	"string": true, "number": true, "integer": true, "boolean": true, "array": true, "object": true,
}

func parseFieldMap(raw any, label string) (map[string]any, error) {
	m, ok := raw.(map[string]any)
	if !ok {
		return nil, promptFileErrorf("%s must be a mapping of field name to type.", label)
	}
	properties := map[string]any{}
	var required []string
	for name, spec := range m {
		var typ, description any
		isRequired := true
		switch v := spec.(type) {
		case string:
			typ = v
		case map[string]any:
			typ = v["type"]
			description = v["description"]
			if req, present := v["required"]; present {
				b, _ := req.(bool)
				isRequired = b
			}
		default:
			return nil, promptFileErrorf("%s.%s must be a type name or {type, description, required}.", label, name)
		}
		typStr, ok := typ.(string)
		if !ok || !fieldTypes[typStr] {
			return nil, promptFileErrorf(
				"%s.%s: type must be one of string, number, integer, boolean, array, object.", label, name)
		}
		if description != nil {
			if _, ok := description.(string); !ok {
				return nil, promptFileErrorf("%s.%s.description must be text.", label, name)
			}
		}
		prop := map[string]any{"type": typStr}
		if description != nil {
			prop["description"] = description
		}
		properties[name] = prop
		if isRequired {
			required = append(required, name)
		}
	}
	// Go map iteration order is randomized — sort for a deterministic result
	// (Python/TS preserve the file's declaration order instead; the SET of
	// required fields is what matters for validation, not its order).
	sort.Strings(required)
	if required == nil {
		required = []string{}
	}
	return map[string]any{"type": "object", "required": required, "properties": properties}, nil
}

// PromptDoc is the parsed shape CreateFunctionFromDoc consumes.
type PromptDoc struct {
	Sections     []map[string]any
	InputSchema  map[string]any // nil if the document had no inputs: block
	OutputSchema map[string]any // nil if the document had no outputs: block
}

// ParsePromptFile reads and validates a canvas-format prompt file into a
// PromptDoc. Read-and-validate only — makes no network call and creates
// nothing.
//
// Only .json is supported directly: this package stays dependency-free, and
// Go has no standard-library YAML support. For a .yaml/.yml file, decode it
// yourself with a YAML library of your choice (e.g. gopkg.in/yaml.v3) into a
// map[string]interface{} and pass that to ParsePromptDoc instead — the
// validation rules are identical either way.
func ParsePromptFile(path string) (*PromptDoc, error) {
	if !strings.HasSuffix(strings.ToLower(path), ".json") {
		return nil, promptFileErrorf(
			"ParsePromptFile only reads .json directly (this package has zero " +
				"dependencies) — for YAML, decode the file yourself with a YAML " +
				"library into map[string]interface{} and call ParsePromptDoc")
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, promptFileErrorf("file not found: %s", path)
	}
	var raw map[string]any
	if err := json.Unmarshal(data, &raw); err != nil {
		return nil, promptFileErrorf("not valid JSON — %s", err.Error())
	}
	return ParsePromptDoc(raw)
}

// ParsePromptDoc validates an already-decoded document — from
// json.Unmarshal, your own YAML decoder, or built by hand — into the same
// shape ParsePromptFile produces. Raises PromptFileError naming exactly
// what's missing/wrong (role is required; inputs:/outputs:, if present, must
// be a field-name -> type mapping; unknown top-level keys are rejected
// rather than silently dropped).
func ParsePromptDoc(doc map[string]any) (*PromptDoc, error) {
	var unknown []string
	for k := range doc {
		if !knownKeys[k] {
			unknown = append(unknown, k)
		}
	}
	if len(unknown) > 0 {
		sort.Strings(unknown)
		plural := ""
		if len(unknown) > 1 {
			plural = "s"
		}
		return nil, promptFileErrorf(
			"unknown section%s: %s. expected: role, %s, freeform, inputs, outputs.",
			plural, strings.Join(unknown, ", "), strings.Join(itemKeys, ", "))
	}

	role, _ := doc["role"].(string)
	if strings.TrimSpace(role) == "" {
		return nil, promptFileErrorf("role is required and must be non-empty text.")
	}

	sections := []map[string]any{{"type": "role", "content": role}}

	for _, key := range itemKeys {
		raw, present := doc[key]
		if !present {
			continue
		}
		items, ok := raw.([]any)
		if !ok {
			return nil, promptFileErrorf("%s must be a list of non-empty text lines.", key)
		}
		strs := make([]string, 0, len(items))
		for _, it := range items {
			s, ok := it.(string)
			if !ok || strings.TrimSpace(s) == "" {
				return nil, promptFileErrorf("%s must be a list of non-empty text lines.", key)
			}
			strs = append(strs, s)
		}
		if len(strs) > 0 {
			sections = append(sections, map[string]any{"type": key, "title": itemLabel[key], "items": strs})
		}
	}

	if rawFree, present := doc["freeform"]; present {
		blocks, ok := rawFree.([]any)
		if !ok {
			return nil, promptFileErrorf("freeform must be a list of {title, content} blocks.")
		}
		for _, b := range blocks {
			block, ok := b.(map[string]any)
			content, cok := block["content"].(string)
			if !ok || !cok || strings.TrimSpace(content) == "" {
				return nil, promptFileErrorf("each freeform block needs a non-empty content field (title is optional).")
			}
			title, _ := block["title"].(string)
			if strings.TrimSpace(title) == "" {
				title = "Freeform"
			}
			sections = append(sections, map[string]any{"type": "freeform", "title": title, "content": content})
		}
	}

	out := &PromptDoc{Sections: sections}
	if rawIn, present := doc["inputs"]; present {
		schema, err := parseFieldMap(rawIn, "inputs")
		if err != nil {
			return nil, err
		}
		out.InputSchema = schema
	}
	if rawOut, present := doc["outputs"]; present {
		schema, err := parseFieldMap(rawOut, "outputs")
		if err != nil {
			return nil, err
		}
		out.OutputSchema = schema
	}
	return out, nil
}

// CreateOpts configures CreateFunctionFromDoc / CreateFunction. Temperature
// is a pointer so an unset value (nil) can default to 0.2 without colliding
// with an intentional temperature of 0.
type CreateOpts struct {
	Slug, Name  string
	Model       string // default "mock-small"
	Temperature *float64
}

func createFunctionAndVersion(client *Client, o CreateOpts,
	sections []map[string]any, inputSchema, outputSchema map[string]any) (*FunctionOut, *VersionOut, error) {
	if inputSchema == nil {
		inputSchema = map[string]any{"type": "object", "properties": map[string]any{}}
	}
	if outputSchema == nil {
		outputSchema = map[string]any{"type": "object", "properties": map[string]any{}}
	}
	model := o.Model
	if model == "" {
		model = "mock-small"
	}
	temperature := 0.2
	if o.Temperature != nil {
		temperature = *o.Temperature
	}
	fn, err := client.Functions.Create(FunctionCreate{
		Slug: o.Slug, Name: o.Name, InputSchema: inputSchema, OutputSchema: outputSchema,
	})
	if err != nil {
		return nil, nil, err
	}
	snapshot := map[string]any{
		"impl": "prompt", "tools": []any{}, "sections": sections,
		"model_binding": map[string]any{"model": model, "temperature": temperature},
	}
	v, err := client.Functions.CreateVersion(fn.ID, snapshot, "")
	if err != nil {
		return nil, nil, err
	}
	return fn, v, nil
}

// CreateFunctionFromDoc creates a function + its first version from data
// already parsed by ParsePromptFile/ParsePromptDoc. This is the step that
// makes network calls — it never reads a file itself, so callers can
// inspect or edit doc in between the two. doc supplies the prompt's
// sections and, if the document declared inputs:/outputs:, their schemas
// (an empty object schema is used for whichever one was omitted). Slug/Name
// are yours to choose via opts, not derived from the file.
//
// For creating a function from sections/schemas you already have in code —
// not parsed from a document — use CreateFunction instead.
func CreateFunctionFromDoc(client *Client, opts CreateOpts, doc *PromptDoc) (*FunctionOut, *VersionOut, error) {
	return createFunctionAndVersion(client, opts, doc.Sections, doc.InputSchema, doc.OutputSchema)
}

// CreateFunction creates a function + its first version from sections/
// schemas you supply directly — the manual counterpart to
// CreateFunctionFromDoc, for when you aren't parsing a prompt document at
// all. inputSchema/outputSchema may each be nil, defaulting to an empty
// object schema.
func CreateFunction(client *Client, opts CreateOpts, sections []map[string]any,
	inputSchema, outputSchema map[string]any) (*FunctionOut, *VersionOut, error) {
	return createFunctionAndVersion(client, opts, sections, inputSchema, outputSchema)
}
