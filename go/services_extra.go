package fnai

// Services added with the step-by-step builder: tools, dataset upload, deploy with a curl, run with
// findings, compare, and checking an input without running a function.

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
)

// Finding is something an experiment turned up.
type Finding struct {
	ID       int    `json:"id"`
	Severity string `json:"severity"`
	Title    string `json:"title"`
	Detail   string `json:"detail"`
}

// UploadResult is what a dataset upload returns.
type UploadResult struct {
	Added   int              `json:"added"`
	Skipped int              `json:"skipped"`
	Errors  []map[string]any `json:"errors"`
}

// Check tests an input against the deployed contract WITHOUT running the function (no model call, no
// cost). It returns the problems, each naming the field, e.g. "field 'rating' must be at most 5, got 9";
// nil means the input would pass. The server runs the same check on every real call and answers 422 with
// the same messages.
func (f *Function) Check(input map[string]any) ([]string, error) {
	c, err := f.Contract()
	if err != nil {
		return nil, err
	}
	schema, _ := asMap(c["input_schema"])
	return validateSample(schema, input), nil
}

// ── Functions: Describe ──────────────────────────────────────────────────────

// Describe returns the function as YAML and what is missing — the same text printing a draft shows. Once
// the function has a version, the prompt, steps and tools come from the latest one.
func (s *FunctionsService) Describe(fnID int) (string, error) {
	if err := requireID(fnID, "fnID"); err != nil {
		return "", err
	}
	var fn struct {
		FunctionOut
		InputSchema  map[string]any `json:"input_schema"`
		OutputSchema map[string]any `json:"output_schema"`
	}
	if err := s.h.do("GET", "/api/functions/"+strconv.Itoa(fnID), nil, nil, &fn); err != nil {
		return "", err
	}
	var versions []VersionOut
	if err := s.h.do("GET", "/api/functions/"+strconv.Itoa(fnID)+"/versions", nil, nil, &versions); err != nil {
		return "", err
	}
	in, out := schemaFieldSet(fn.InputSchema), schemaFieldSet(fn.OutputSchema)
	name := fn.Name
	if name == "" {
		name = fn.Slug
	}
	if len(versions) == 0 {
		m := missingLists(in, out, []string{"-"}, mustOrdered(map[string]any{"role": "-", "examples": []map[string]any{{}}, "constraints": []string{"-"}}))
		m.Recommended = append(m.Recommended, "no version saved yet — build one with Functions.New(...) or LoadDraft(id)")
		return describe(renderYaml(yamlParts{name: name, description: fn.Description, input: in, output: out}), m), nil
	}
	latest := versions[0]
	for _, v := range versions {
		if v.Number > latest.Number {
			latest = v
		}
	}
	if len(latest.InputSchema) > 0 {
		in = schemaFieldSet(latest.InputSchema)
	}
	if len(latest.OutputSchema) > 0 {
		out = schemaFieldSet(latest.OutputSchema)
	}
	impl, _ := asMap(latest.Snapshot["implementation"])
	sections := NewOrderedMap()
	if sm, ok := asMap(impl["sections"]); ok {
		for _, k := range []string{"role", "definitions", "constraints", "edge_cases"} {
			if v, has := sm[k]; has {
				sections.Set(k, v)
			}
		}
		if ex := mapSlice(sm["examples"]); len(ex) > 0 {
			sections.Set("examples", ex)
		}
	}
	mode := "single_shot"
	if exec, ok := asMap(impl["execution"]); ok {
		if m, ok := exec["mode"].(string); ok {
			mode = m
		}
	}
	flow := stringSlice(impl["flow"])
	var memory *OrderedMap
	if mem, ok := asMap(latest.Snapshot["memory"]); ok {
		memory = NewOrderedMap()
		for _, k := range sortedKeys(mem) {
			memory.Set(k, mem[k])
		}
	}
	parts := yamlParts{name: name, description: fn.Description, input: in, output: out, mode: mode, flow: flow,
		rubric: stringSlice(latest.Snapshot["rubric"]), memory: memory, sections: sections, tools: mapSlice(impl["tools"])}
	parts.extraTop, parts.extraImpl = extrasOf(latest.Snapshot)
	return describe(renderYaml(parts), missingLists(in, out, flow, sections)), nil
}

func mustOrdered(m map[string]any) *OrderedMap {
	o := NewOrderedMap()
	for _, k := range sortedKeys(m) {
		o.Set(k, m[k])
	}
	return o
}

// ── Datasets: Upload, Snapshot ───────────────────────────────────────────────

var uploadExtRe = regexp.MustCompile(`(?i)\.(csv|json|xlsx|xlsm)$`)

// doForm sends a multipart/form-data POST, with the same auth and error handling as do().
func (h *httpClient) doForm(path string, body *bytes.Buffer, contentType string, out any) error {
	req, err := http.NewRequest("POST", h.base+path, body)
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+h.token)
	req.Header.Set("Content-Type", contentType)
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
			if s, ok := parsed.Detail.(string); ok {
				detail = s
			} else if b, err := json.Marshal(parsed.Detail); err == nil {
				detail = string(b)
			}
		}
		return &Error{Status: resp.StatusCode, Detail: detail}
	}
	return json.NewDecoder(resp.Body).Decode(out)
}

// Upload bulk-loads rows from a local .csv, .json (array of flat objects) or .xlsx/.xlsm file into an
// existing dataset — what the UI's "Upload rows" does. mapping is {file column: input field}; the
// optional expectedMapping is {file column: expected-output field}. Columns are never guessed.
func (s *DatasetsService) Upload(dsID int, path string, mapping, expectedMapping map[string]string) (*UploadResult, error) {
	if err := requireID(dsID, "dsID"); err != nil {
		return nil, err
	}
	if err := requireField(path, "path"); err != nil {
		return nil, err
	}
	if len(mapping) == 0 {
		return nil, validationErrorf("mapping must assign at least one file column to an input field.")
	}
	if !uploadExtRe.MatchString(path) {
		return nil, validationErrorf("unsupported file type — use .csv, .json, or .xlsx")
	}
	content, err := os.ReadFile(path)
	if err != nil {
		return nil, validationErrorf("no such file: %q", path)
	}
	if expectedMapping == nil {
		expectedMapping = map[string]string{}
	}
	var buf bytes.Buffer
	w := multipart.NewWriter(&buf)
	fw, _ := w.CreateFormFile("file", filepath.Base(path))
	_, _ = io.Copy(fw, bytes.NewReader(content))
	mb, _ := json.Marshal(mapping)
	eb, _ := json.Marshal(expectedMapping)
	_ = w.WriteField("mapping", string(mb))
	_ = w.WriteField("expected_mapping", string(eb))
	_ = w.Close()
	var out UploadResult
	if err := s.h.doForm("/api/datasets/"+strconv.Itoa(dsID)+"/upload", &buf, w.FormDataContentType(), &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// SnapshotOut is a frozen copy of a dataset.
type SnapshotOut struct {
	ID          int    `json:"id"`
	DatasetID   int    `json:"dataset_id"`
	ContentHash string `json:"content_hash"`
}

// Snapshot freezes a copy of the dataset's current rows, for experiments to run against.
func (s *DatasetsService) Snapshot(dsID int) (*SnapshotOut, error) {
	if err := requireID(dsID, "dsID"); err != nil {
		return nil, err
	}
	var out SnapshotOut
	return &out, s.h.do("POST", "/api/datasets/"+strconv.Itoa(dsID)+"/snapshot", map[string]any{}, nil, &out)
}

// AddScore adds a score (a check) to a plan (Score Set), e.g. {"check": "enum", "target": "verdict",
// "gate": false, "params": {"values": [...]}}.
func (s *PlansService) AddScore(planID int, body map[string]any) (*ScoreOut, error) {
	if err := requireID(planID, "planID"); err != nil {
		return nil, err
	}
	var out ScoreOut
	return &out, s.h.do("POST", "/api/plans/"+strconv.Itoa(planID)+"/scores", body, nil, &out)
}

// ── Deployments: curl ────────────────────────────────────────────────────────

// Curl returns the endpoint and a ready-to-run curl for calling this function's public runtime URL. Pass
// apiKey (an fnk_* key) to embed it, else a placeholder is used.
func (s *DeploymentsService) Curl(fnID int, apiKey string) (endpoint, curl string, err error) {
	if err = requireID(fnID, "fnID"); err != nil {
		return
	}
	var fn struct {
		Slug        string         `json:"slug"`
		InputSchema map[string]any `json:"input_schema"`
	}
	if err = s.h.do("GET", "/api/functions/"+strconv.Itoa(fnID), nil, nil, &fn); err != nil {
		return
	}
	endpoint = s.h.base + "/v1/run/" + fn.Slug
	schema := fn.InputSchema
	if props, _ := asMap(schema["properties"]); len(props) == 0 { // built with Submit(): the schema is on the version
		var versions []VersionOut
		if err = s.h.do("GET", "/api/functions/"+strconv.Itoa(fnID)+"/versions", nil, nil, &versions); err != nil {
			return
		}
		latest := -1
		for i, v := range versions {
			if latest < 0 || v.Number > versions[latest].Number {
				latest = i
			}
		}
		if latest >= 0 {
			schema = versions[latest].InputSchema
		}
	}
	sample := NewOrderedMap()
	props, _ := asMap(schema["properties"])
	for _, k := range sortedKeys(props) {
		sample.Set(k, "...")
	}
	sb, _ := json.Marshal(sample)
	if apiKey == "" {
		apiKey = "fnk_YOUR_KEY"
	}
	curl = fmt.Sprintf("curl -X POST %s \\\n  -H \"Authorization: Bearer %s\" \\\n  -H \"Content-Type: application/json\" \\\n  -d '{\"input\": %s}'",
		endpoint, apiKey, sb)
	return
}

// DeployConfirmed deploys a version even over a breaking contract change against the live one.
func (s *DeploymentsService) DeployConfirmed(fnID, versionID int) (*DeploymentOut, error) {
	return s.deploy(fnID, versionID, true, "", nil)
}

func (s *DeploymentsService) deploy(fnID, versionID int, confirm bool, model string, temperature *float64) (*DeploymentOut, error) {
	if err := requireID(fnID, "fnID"); err != nil {
		return nil, err
	}
	if err := requireID(versionID, "versionID"); err != nil {
		return nil, err
	}
	body := map[string]any{"version_id": versionID}
	if confirm {
		body["confirm"] = true
	}
	if model != "" {
		body["model"] = model
	}
	if temperature != nil {
		body["temperature"] = *temperature
	}
	var out DeploymentOut
	if err := s.h.do("POST", "/api/functions/"+strconv.Itoa(fnID)+"/deploy", body, nil, &out); err != nil {
		return nil, err
	}
	// the deploy worked; the snippet is a convenience
	if endpoint, curl, err := s.Curl(fnID, ""); err == nil {
		out.Endpoint, out.Curl = endpoint, curl
	}
	return &out, nil
}

// ── Experiments: Get, Findings, Run with findings, Compare ───────────────────

// Get returns one experiment, including its results.
func (s *ExperimentsService) Get(expID int) (*ExperimentOut, error) {
	if err := requireID(expID, "expID"); err != nil {
		return nil, err
	}
	var out ExperimentOut
	return &out, s.h.do("GET", "/api/experiments/"+strconv.Itoa(expID), nil, nil, &out)
}

// Findings returns what an experiment turned up.
func (s *ExperimentsService) Findings(expID int) ([]Finding, error) {
	if err := requireID(expID, "expID"); err != nil {
		return nil, err
	}
	var out []Finding
	return out, s.h.do("GET", "/api/experiments/"+strconv.Itoa(expID)+"/findings", nil, nil, &out)
}

// RunBare runs a draft experiment and returns it without its findings.
func (s *ExperimentsService) RunBare(expID int) (*ExperimentOut, error) {
	if err := requireID(expID, "expID"); err != nil {
		return nil, err
	}
	var out ExperimentOut
	return &out, s.h.do("POST", "/api/experiments/"+strconv.Itoa(expID)+"/run", map[string]any{}, nil, &out)
}

// ComparedExperiment names one column of a comparison.
type ComparedExperiment struct {
	ID      int
	Name    string
	Version string
	Cost    float64
}

// Comparison is finished experiments side by side — what the Comparisons screen shows.
type Comparison struct {
	Experiments []ComparedExperiment
	Rows        []ComparisonRow
}

// Compare compares finished experiments side by side. An experiment pins exactly one version, so to compare
// v1 with v2 run one experiment on each (same dataset, same Score Set) and pass both ids. Each row has the
// values per experiment, in order, and Best, the index of the winner (nil when there is none). Rows: trust
// score, pass rate, cost per run, p95 latency, each dimension, then every check.
func (s *ExperimentsService) Compare(expIDs []int) (*Comparison, error) {
	if len(expIDs) < 2 {
		return nil, validationErrorf("Compare needs at least two experiment ids.")
	}
	var exps []*ExperimentOut
	for _, id := range expIDs {
		e, err := s.Get(id)
		if err != nil {
			return nil, err
		}
		if e.Status != "completed" {
			return nil, validationErrorf("experiment %d is %q — only completed experiments can be compared.", id, e.Status)
		}
		exps = append(exps, e)
	}
	models := make([]trust, len(exps))
	for i, e := range exps {
		models[i] = trustOf(e.Results)
	}
	f := func(v float64) *float64 { return &v }
	var trustVals, passVals, costVals, latVals []*float64
	cmp := &Comparison{}
	for i, e := range exps {
		trustVals = append(trustVals, f(float64(models[i].score)))
		passVals = append(passVals, models[i].passPct)
		cost := e.Cost
		if n, ok := toFloat(e.Results["n_rows"]); ok && n > 0 {
			cost = e.Cost / n
		}
		costVals = append(costVals, f(cost))
		var lat *float64
		if l, ok := asMap(e.Results["latency"]); ok {
			if p, ok := toFloat(l["p95"]); ok {
				lat = f(p / 1000)
			}
		}
		latVals = append(latVals, lat)
		version := ""
		if pv, ok := asMap(e.Results["per_version"]); ok {
			for _, k := range sortedKeys(pv) {
				if vm, ok := asMap(pv[k]); ok {
					if n, ok := toFloat(vm["number"]); ok && n != 0 {
						if version != "" {
							version += " vs "
						}
						version += "v" + numStr(n)
					}
				}
			}
		}
		if version == "" {
			version = e.Name
		}
		cmp.Experiments = append(cmp.Experiments, ComparedExperiment{ID: e.ID, Name: e.Name, Version: version, Cost: e.Cost})
	}
	cmp.Rows = append(cmp.Rows,
		comparisonRow("Trust score (/100)", trustVals, true),
		comparisonRow("Pass rate %", passVals, true),
		comparisonRow("Cost per run $", costVals, false),
		comparisonRow("p95 latency s", latVals, false))
	for _, d := range dimensions {
		var vals []*float64
		for _, m := range models {
			if s := m.dims[d[0]]; s != nil {
				vals = append(vals, f(*s*100))
			} else {
				vals = append(vals, nil)
			}
		}
		cmp.Rows = append(cmp.Rows, comparisonRow(d[1]+" %", vals, true))
	}
	seen := map[string]bool{}
	var names []string
	for _, m := range models {
		for n := range m.checks {
			if !seen[n] {
				seen[n] = true
				names = append(names, n)
			}
		}
	}
	sortStringsLocal(names)
	for _, n := range names {
		var vals []*float64
		for _, m := range models {
			if v, ok := m.checks[n]; ok {
				vals = append(vals, f(v))
			} else {
				vals = append(vals, nil)
			}
		}
		cmp.Rows = append(cmp.Rows, comparisonRow("check: "+n+" %", vals, true))
	}
	return cmp, nil
}

func sortStringsLocal(s []string) {
	for i := 1; i < len(s); i++ {
		for j := i; j > 0 && s[j] < s[j-1]; j-- {
			s[j], s[j-1] = s[j-1], s[j]
		}
	}
}

// ── Tools ────────────────────────────────────────────────────────────────────

// ToolsService registers and manages customer tools (a REST endpoint, an MCP server, or sandboxed Python)
// — what the UI's tool registration does. Attach one to a function with FunctionDraft.AddTool(name, …).
type ToolsService struct{ h *httpClient }

func (s *ToolsService) List() ([]map[string]any, error) {
	var out []map[string]any
	return out, s.h.do("GET", "/api/tools", nil, nil, &out)
}

func (s *ToolsService) Get(toolID int) (map[string]any, error) {
	if err := requireID(toolID, "toolID"); err != nil {
		return nil, err
	}
	var out map[string]any
	return out, s.h.do("GET", "/api/tools/"+strconv.Itoa(toolID), nil, nil, &out)
}

// Create registers a tool. body uses the API's own field names: name, description, target_type
// ("http" | "mcp" | "python"), url, method, mcp_tool_name, code, input_schema or openapi_spec, effect,
// servicing, config_schema, auth_kind and credential.
func (s *ToolsService) Create(body map[string]any) (map[string]any, error) {
	name, _ := body["name"].(string)
	desc, _ := body["description"].(string)
	if err := requireField(name, "name"); err != nil {
		return nil, err
	}
	if err := requireField(desc, "description"); err != nil {
		return nil, err
	}
	t, _ := body["target_type"].(string)
	if !contains2([]string{"http", "mcp", "python"}, t) {
		return nil, validationErrorf("target_type must be 'http', 'mcp' or 'python', got %q.", t)
	}
	_, hasSchema := body["input_schema"]
	_, hasSpec := body["openapi_spec"]
	switch {
	case t == "python":
		if body["code"] == nil || body["code"] == "" {
			return nil, validationErrorf("a python tool needs \"code\" defining run(args).")
		}
		if !hasSchema {
			return nil, validationErrorf("a python tool needs \"input_schema\" (the arguments run(args) takes).")
		}
	default:
		if body["url"] == nil || body["url"] == "" {
			return nil, validationErrorf("a %s tool needs \"url\".", t)
		}
		if !hasSchema && !hasSpec {
			return nil, validationErrorf("a %s tool needs \"input_schema\" or \"openapi_spec\" so the agent knows what arguments to send.", t)
		}
	}
	var out map[string]any
	return out, s.h.do("POST", "/api/tools", body, nil, &out)
}

// Test tries a tool with a real call before saving it. Nothing is stored. body: target_type, url, method,
// mcp_tool_name, code, auth_kind, credential, sample_args, expected_output_schema.
func (s *ToolsService) Test(body map[string]any) (map[string]any, error) {
	t, _ := body["target_type"].(string)
	if !contains2([]string{"http", "mcp", "python"}, t) {
		return nil, validationErrorf("target_type must be 'http', 'mcp' or 'python', got %q.", t)
	}
	if _, has := body["sample_args"]; !has {
		body["sample_args"] = map[string]any{}
	}
	var out map[string]any
	return out, s.h.do("POST", "/api/tools/test", body, nil, &out)
}

func (s *ToolsService) Update(toolID int, fields map[string]any) (map[string]any, error) {
	if err := requireID(toolID, "toolID"); err != nil {
		return nil, err
	}
	if len(fields) == 0 {
		return nil, validationErrorf("Update needs at least one field to change.")
	}
	var out map[string]any
	return out, s.h.do("PATCH", "/api/tools/"+strconv.Itoa(toolID), fields, nil, &out)
}

// Usage says which functions use a tool.
func (s *ToolsService) Usage(toolID int) (map[string]any, error) {
	if err := requireID(toolID, "toolID"); err != nil {
		return nil, err
	}
	var out map[string]any
	return out, s.h.do("GET", "/api/tools/"+strconv.Itoa(toolID)+"/usage", nil, nil, &out)
}

var _ = strings.TrimSpace

// ── Look a function up by its slug ───────────────────────────────────────────

// GetBySlug returns the function with this slug (e.g. "my_function") in the space you are in. An unknown
// slug returns a 404 *Error that says so, suggests the closest slug and lists the real ones. Get keeps
// taking the numeric id.
func (s *FunctionsService) GetBySlug(slug string) (*FunctionOut, error) {
	if err := requireField(slug, "slug"); err != nil {
		return nil, err
	}
	var all []FunctionOut
	if err := s.h.do("GET", "/api/functions", nil, nil, &all); err != nil {
		return nil, err
	}
	var slugs []string
	for i, f := range all {
		if f.Slug == slug {
			return &all[i], nil
		}
		if f.Slug != "" {
			slugs = append(slugs, f.Slug)
		}
	}
	sort.Strings(slugs)
	hint := ""
	if near := closestMatch(slug, slugs); near != "" {
		hint = fmt.Sprintf(" — did you mean '%s'?", near)
	}
	if regexp.MustCompile(`^\d+$`).MatchString(slug) {
		hint += fmt.Sprintf(" (to look up by id, use Get(%s), not the text)", slug)
	}
	listing := "no functions in this space yet"
	if len(slugs) > 0 {
		listing = strings.Join(slugs[:min(len(slugs), 20)], ", ")
		if len(slugs) > 20 {
			listing += " …"
		}
	}
	return nil, &Error{Status: 404, Detail: fmt.Sprintf("no function with slug %q in this space%s (functions here: %s)", slug, hint, listing)}
}

// LoadDraftBySlug resumes an existing function, found by its slug, to keep building on it. LoadDraft takes the id.
func (c *Client) LoadDraftBySlug(slug string, opts ...DraftOption) (*FunctionDraft, error) {
	fn, err := c.Functions.GetBySlug(slug)
	if err != nil {
		return nil, err
	}
	return c.LoadDraft(fn.ID, opts...)
}

// ── Export every saved version as YAML ───────────────────────────────────────

// doText fetches plain text (the YAML endpoints speak text/yaml), with the same auth and error handling as do().
func (h *httpClient) doText(path string) (string, error) {
	req, err := http.NewRequest("GET", h.base+path, nil)
	if err != nil {
		return "", err
	}
	req.Header.Set("Authorization", "Bearer "+h.token)
	req.Header.Set("User-Agent", "fnai-go/"+Version)
	if h.space != "" {
		req.Header.Set("X-Space", h.space)
	}
	resp, err := h.hc.Do(req)
	if err != nil {
		return "", &Error{Status: 0, Detail: "cannot reach " + h.base + ": " + err.Error()}
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 400 {
		var parsed struct {
			Detail any `json:"detail"`
		}
		detail := resp.Status
		if json.Unmarshal(body, &parsed) == nil && parsed.Detail != nil {
			if s, ok := parsed.Detail.(string); ok {
				detail = s
			}
		}
		return "", &Error{Status: resp.StatusCode, Detail: detail}
	}
	return string(body), nil
}

// ExportYAML writes EVERY saved version of a function as YAML: v1.yaml, v2.yaml, … Read from the server, so it
// is complete and exact, including versions made in the app or by a teammate. folder "" means
// .fnai/versions/<slug>. It returns the paths written, oldest first. The server is the source of truth: run it
// again to refresh the copies.
func (c *Client) ExportYAML(fnID int, folder string) ([]string, error) {
	if err := requireID(fnID, "fnID"); err != nil {
		return nil, err
	}
	var fn FunctionOut
	if err := c.h.do("GET", "/api/functions/"+strconv.Itoa(fnID), nil, nil, &fn); err != nil {
		return nil, err
	}
	var versions []VersionOut
	if err := c.h.do("GET", "/api/functions/"+strconv.Itoa(fnID)+"/versions", nil, nil, &versions); err != nil {
		return nil, err
	}
	if len(versions) == 0 {
		return nil, &Error{Status: 404, Detail: fmt.Sprintf("function %q has no versions yet", fn.Slug)}
	}
	sort.Slice(versions, func(i, j int) bool { return versions[i].Number < versions[j].Number })
	target := folder
	if target == "" {
		target = filepath.Join(versionsDir(c.h.versionsFolder, c.h.draftsFolder), fn.Slug)
	} else if abs, err := filepath.Abs(expandHome(folder)); err == nil {
		target = abs
	}
	var paths []string
	for _, v := range versions {
		text, err := c.h.doText("/api/versions/" + strconv.Itoa(v.ID) + "/yaml")
		if err != nil {
			return paths, err
		}
		path := filepath.Join(target, fmt.Sprintf("v%d.yaml", v.Number))
		if err := writeAtomic(path, text); err != nil {
			return paths, err
		}
		paths = append(paths, path)
	}
	return paths, nil
}

// ExportYAMLBySlug is ExportYAML for a function given by its slug.
func (c *Client) ExportYAMLBySlug(slug, folder string) ([]string, error) {
	fn, err := c.Functions.GetBySlug(slug)
	if err != nil {
		return nil, err
	}
	return c.ExportYAML(fn.ID, folder)
}
