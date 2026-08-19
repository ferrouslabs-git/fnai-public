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
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"
)

// Version is the client version.
const Version = "0.2.0"

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

type httpClient struct {
	base  string
	token string
	hc    *http.Client
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
			detail = fmt.Sprint(parsed.Detail)
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

// ── management types (golden-path surface) ──────────────────────────────────

type FunctionOut struct {
	ID          int    `json:"id"`
	Slug        string `json:"slug"`
	Name        string `json:"name"`
	Description string `json:"description"`
	Archived    bool   `json:"archived"`
}

type VersionOut struct {
	ID      int    `json:"id"`
	Number  int    `json:"number"`
	Status  string `json:"status"`
	Message string `json:"message"`
}

type DeploymentOut struct {
	ID            int    `json:"id"`
	FunctionID    int    `json:"function_id"`
	VersionID     int    `json:"version_id"`
	VersionNumber *int   `json:"version_number"`
	Status        string `json:"status"`
	Action        string `json:"action"`
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
	ID     int    `json:"id"`
	Name   string `json:"name"`
	Status string `json:"status"`
}

type ScoreOut struct {
	ID   int    `json:"id"`
	Name string `json:"name"`
	Tier string `json:"tier"`
	Gate bool   `json:"gate"`
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
	var out FunctionOut
	return &out, s.h.do("GET", "/api/functions/"+strconv.Itoa(fnID), nil, nil, &out)
}

func (s *FunctionsService) Create(body FunctionCreate) (*FunctionOut, error) {
	var out FunctionOut
	return &out, s.h.do("POST", "/api/functions", body, nil, &out)
}

func (s *FunctionsService) Archive(fnID int) error {
	return s.h.do("DELETE", "/api/functions/"+strconv.Itoa(fnID), nil, nil, nil)
}

func (s *FunctionsService) Versions(fnID int) ([]VersionOut, error) {
	var out []VersionOut
	return out, s.h.do("GET", "/api/functions/"+strconv.Itoa(fnID)+"/versions", nil, nil, &out)
}

// CreateVersion snapshots the definition (sections, model_binding, tools, …).
func (s *FunctionsService) CreateVersion(fnID int, snapshot map[string]any, message string) (*VersionOut, error) {
	var out VersionOut
	return &out, s.h.do("POST", "/api/functions/"+strconv.Itoa(fnID)+"/versions",
		map[string]any{"snapshot": snapshot, "message": message}, nil, &out)
}

func (s *FunctionsService) Health(fnID, windowHours int) (*HealthOut, error) {
	var out HealthOut
	q := url.Values{"window_hours": {strconv.Itoa(windowHours)}}
	return &out, s.h.do("GET", "/api/functions/"+strconv.Itoa(fnID)+"/health", nil, q, &out)
}

// Types returns compilable caller-side types for the function's contract
// (lang: "go" is not offered server-side yet — "typescript" or "python").
func (s *FunctionsService) Types(fnID int, lang string) (*TypesOut, error) {
	var out TypesOut
	q := url.Values{"lang": {lang}}
	return &out, s.h.do("GET", "/api/functions/"+strconv.Itoa(fnID)+"/types", nil, q, &out)
}

// VersionsService owns the version lifecycle and the playground.
type VersionsService struct{ h *httpClient }

func (s *VersionsService) Get(vID int) (*VersionOut, error) {
	var out VersionOut
	return &out, s.h.do("GET", "/api/versions/"+strconv.Itoa(vID), nil, nil, &out)
}

func (s *VersionsService) Playground(vID int, input map[string]any, model string) (*PlaygroundOut, error) {
	if model == "" {
		model = "mock-small"
	}
	var out PlaygroundOut
	return &out, s.h.do("POST", "/api/versions/"+strconv.Itoa(vID)+"/playground/run",
		map[string]any{"input": input, "model": model}, nil, &out)
}

func (s *VersionsService) Submit(vID int) error {
	return s.h.do("POST", "/api/versions/"+strconv.Itoa(vID)+"/submit",
		map[string]any{}, nil, nil)
}

func (s *VersionsService) Approve(vID int, comment string) error {
	return s.h.do("POST", "/api/versions/"+strconv.Itoa(vID)+"/approve",
		map[string]any{"comment": comment}, nil, nil)
}

func (s *VersionsService) Reject(vID int, comment string) error {
	return s.h.do("POST", "/api/versions/"+strconv.Itoa(vID)+"/reject",
		map[string]any{"comment": comment}, nil, nil)
}

// PlansService is eval.plans — named check collections experiments can pin.
type PlansService struct{ h *httpClient }

func (s *PlansService) List(fnID int) ([]PlanBrief, error) {
	var out []PlanBrief
	return out, s.h.do("GET", "/api/functions/"+strconv.Itoa(fnID)+"/plans", nil, nil, &out)
}

func (s *PlansService) Create(fnID int, name string) (*PlanCreated, error) {
	var out PlanCreated
	return &out, s.h.do("POST", "/api/functions/"+strconv.Itoa(fnID)+"/plans",
		map[string]any{"name": name}, nil, &out)
}

func (s *PlansService) Scores(planID int) ([]ScoreOut, error) {
	var out []ScoreOut
	return out, s.h.do("GET", "/api/plans/"+strconv.Itoa(planID)+"/scores", nil, nil, &out)
}

// ExperimentsService is eval.experiments.
type ExperimentsService struct{ h *httpClient }

func (s *ExperimentsService) List(fnID int) ([]ExperimentOut, error) {
	var out []ExperimentOut
	return out, s.h.do("GET", "/api/functions/"+strconv.Itoa(fnID)+"/experiments", nil, nil, &out)
}

// Create pins an experiment; pins mirrors the API body (version_ids, models, …).
func (s *ExperimentsService) Create(fnID int, pins map[string]any) (*ExperimentOut, error) {
	var out ExperimentOut
	return &out, s.h.do("POST", "/api/functions/"+strconv.Itoa(fnID)+"/experiments", pins, nil, &out)
}

func (s *ExperimentsService) Run(expID int) (*ExperimentOut, error) {
	var out ExperimentOut
	return &out, s.h.do("POST", "/api/experiments/"+strconv.Itoa(expID)+"/run",
		map[string]any{}, nil, &out)
}

// ScoresService is eval.scores — the per-function check library.
type ScoresService struct{ h *httpClient }

func (s *ScoresService) List(fnID int) ([]ScoreOut, error) {
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
	var out []DatasetOut
	return out, s.h.do("GET", "/api/functions/"+strconv.Itoa(fnID)+"/datasets", nil, nil, &out)
}

func (s *DatasetsService) Create(fnID int, name string, rows []map[string]any) (*DatasetOut, error) {
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
	var out DeploymentOut
	return &out, s.h.do("POST", "/api/functions/"+strconv.Itoa(fnID)+"/deploy",
		map[string]any{"version_id": versionID}, nil, &out)
}

func (s *DeploymentsService) Current(fnID int) (*DeploymentOut, error) {
	var out DeploymentOut
	return &out, s.h.do("GET", "/api/functions/"+strconv.Itoa(fnID)+"/deployment", nil, nil, &out)
}

func (s *DeploymentsService) History(fnID int) ([]DeploymentOut, error) {
	var out []DeploymentOut
	return out, s.h.do("GET", "/api/functions/"+strconv.Itoa(fnID)+"/deployments", nil, nil, &out)
}

func (s *DeploymentsService) Rollback(fnID int, versionID *int) (*DeploymentOut, error) {
	body := map[string]any{}
	if versionID != nil {
		body["version_id"] = *versionID
	}
	var out DeploymentOut
	return &out, s.h.do("POST", "/api/functions/"+strconv.Itoa(fnID)+"/rollback", body, nil, &out)
}

// InvokeKeysService mints/revokes per-function fnk_* runtime keys.
type InvokeKeysService struct{ h *httpClient }

func (s *InvokeKeysService) List(fnID int) ([]InvokeKeyOut, error) {
	var out []InvokeKeyOut
	return out, s.h.do("GET", "/api/functions/"+strconv.Itoa(fnID)+"/invoke-keys", nil, nil, &out)
}

func (s *InvokeKeysService) Mint(fnID int, name string) (*InvokeKeyMinted, error) {
	var out InvokeKeyMinted
	return &out, s.h.do("POST", "/api/functions/"+strconv.Itoa(fnID)+"/invoke-keys",
		map[string]any{"name": name}, nil, &out)
}

func (s *InvokeKeysService) Revoke(keyID int) error {
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
	var out map[string]any
	return out, s.h.do("POST", "/api/provider-keys", body, nil, &out)
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
	var out map[string]any
	return out, s.h.do("GET", "/api/traces/"+traceID, nil, nil, &out)
}

func (s *TracesService) Flag(traceID string) (map[string]any, error) {
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
	var out TokenMinted
	return &out, s.h.do("POST", "/api/auth/tokens",
		map[string]any{"name": name}, nil, &out)
}

func (s *AuthTokensService) Revoke(tokenID string) error {
	return s.h.do("DELETE", "/api/auth/tokens/"+tokenID, nil, nil, nil)
}

// AuthService groups auth-owned resources.
type AuthService struct{ Tokens *AuthTokensService }

// Client is the namespaced management client (fnp_* personal access token).
type Client struct {
	h           *httpClient
	Functions   *FunctionsService
	Versions    *VersionsService
	Eval        *EvalService
	Datasets    *DatasetsService
	Deployments *DeploymentsService
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
		Versions:  &VersionsService{h},
		Eval: &EvalService{Plans: &PlansService{h},
			Experiments: &ExperimentsService{h}, Scores: &ScoresService{h}},
		Datasets:    &DatasetsService{h},
		Deployments: &DeploymentsService{h},
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

// Function crosses to the runtime surface on this client's base URL.
func (c *Client) Function(slug, apiKey string) *Function {
	return &Function{Slug: slug,
		h: &httpClient{base: c.h.base, token: apiKey, hc: c.h.hc}}
}
