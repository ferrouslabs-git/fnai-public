package fnai

// FunctionDraft is the function being built (FUNCTION_CLASS_DESIGN.md, approved 2026-09-24).
//
// Add the parts one call at a time — inputs, outputs, a prompt, definitions, constraints, edge cases,
// examples, steps, tools, a rubric, memory. Each call is checked against the definition rules
// (function-dsl.md) as you make it: a mistake returns a *DefinitionError saying what to fix.
//
// Nothing reaches the server until Submit(). While you build, a YAML draft file on your computer is
// kept up to date after every change (default .fnai/drafts/<slug>.yaml), so a stopped program loses
// nothing: yamlload.Load reads it back. Submit() deletes it. fmt.Println(draft) shows the function as
// YAML and what is still missing.
//
// Named FunctionDraft rather than the design doc's plain "Function" because Function is already taken
// in this package — the public runtime handle on a DEPLOYED function. That type runs a function; this
// one builds one.

import (
	"encoding/json"
	"fmt"
	"io"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
)

// ExampleOpts is one worked example. A positive example needs Input and Output; a negative one needs
// Output (or BadValue) and a Note.
type ExampleOpts struct {
	Polarity string // "positive" (default) or "negative"
	Input    map[string]any
	Output   map[string]any
	BadValue any
	Note     string
}

// ToolOptions are this function's own limits for a tool.
type ToolOptions struct {
	MaxCalls int            // cap on uses per run (0 = none)
	Requires string         // lock the tool until a condition holds, e.g. `human_approval.returned.approved == true`
	Effect   string         // may only tighten the tool's registered effect: read_only | idempotent | side_effecting
	Ref      string         // a version-pinned reference, e.g. `summarizer@3`
	Config   map[string]any // this function's own settings for the tool
}

// Memory says who a function remembers for. Per is "caller", "space" or "organisation"; Remember and
// Never are optional sentences (guidance for the function, not a hard control).
type Memory struct {
	Per      string
	Remember []string
	Never    []string
}

type draftConfig struct {
	slug, description string
	saveTo            string
	noFile            bool
	version           int
	fresh             bool
}

// DraftOption customises a new or resumed draft.
type DraftOption func(*draftConfig)

// WithSlug sets the function's slug (default: the name, slugified).
func WithSlug(slug string) DraftOption { return func(c *draftConfig) { c.slug = slug } }

// WithDescription sets the function's description.
func WithDescription(d string) DraftOption { return func(c *draftConfig) { c.description = d } }

// SaveTo says where this function's draft file goes: a path ending in .yaml/.yml is the file itself,
// anything else is a folder. The client-wide WithDraftsFolder sets a default for all functions.
func SaveTo(path string) DraftOption { return func(c *draftConfig) { c.saveTo = path } }

// WithVersion starts a resumed draft from version N instead of the latest (LoadDraft / LoadDraftBySlug).
// A frozen (deployed) version is never changed: Submit creates a NEW version based on it, and never
// overwrites another draft. A draft version is edited in place by Submit.
func WithVersion(n int) DraftOption { return func(c *draftConfig) { c.version = n } }

// WithFresh keeps the function (same name and slug) but starts it EMPTY: only what you add goes into the new
// version (LoadDraft / LoadDraftBySlug). A fresh draft always saves as a NEW version, whichever of Submit and
// SubmitAsNew you call, and never overwrites the version it grew from. Before sending, it prints what was
// dropped, added or changed (see Changes).
func WithFresh() DraftOption { return func(c *draftConfig) { c.fresh = true } }

// NoDraftFile turns the automatic draft file off for this function.
func NoDraftFile() DraftOption { return func(c *draftConfig) { c.noFile = true } }

type FunctionDraft struct {
	// ID is the function's id on the server. It is 0 until Submit() creates the function.
	ID          int
	Slug        string
	Name        string
	Description string
	LastVersion *VersionOut
	// DraftPath is the local draft file, or "" when it is off.
	DraftPath string

	h           *httpClient
	in, out     *fieldSet
	role        string
	definitions []string
	constraints []string
	edgeCases   []string
	examples    []map[string]any
	tools       []map[string]any
	flow        []string
	rubric      []string
	memory      *OrderedMap
	mode        string // "" = automatic
	loading     bool
	warned      bool
	// which version a save goes to (see WithVersion):
	//   neither set -> the server's rule: update the latest draft, or start a new version
	//   target      -> edit that draft in place
	//   base        -> a frozen version this draft grew from: a save creates a NEW version
	target, base *VersionOut
	cfg          draftConfig
	// parts of a loaded version the builder has no call for, kept so a save does not drop them
	extraTop, extraImpl map[string]any
	fieldExtras         map[string]map[string]*fieldExtra // "input"/"output" -> name -> extras
	fresh               bool                              // WithFresh: a save is always a NEW version
	baselineKeys        []string                          // the loaded version's parts, to list Changes against
	baseline            map[string]string
}

// changesOut is where Submit prints the changes of a fresh draft.
var changesOut io.Writer = os.Stdout

// fieldExtra is what the stored JSON schema cannot hold: the sensitive flag, and derived fields (filled by a
// preprocessor, so never part of the caller's schema).
type fieldExtra struct {
	flags map[string]any
	full  map[string]any // set for a derived field
}

func newFunctionDraft(h *httpClient, fn FunctionOut) *FunctionDraft {
	return &FunctionDraft{
		ID: fn.ID, Slug: fn.Slug, Name: fn.Name, Description: fn.Description, h: h,
		in: newFieldSet(), out: newFieldSet(),
		extraTop: map[string]any{}, extraImpl: map[string]any{},
		fieldExtras: map[string]map[string]*fieldExtra{"input": {}, "output": {}},
	}
}

func buildConfig(opts []DraftOption) draftConfig {
	var cfg draftConfig
	for _, o := range opts {
		o(&cfg)
	}
	return cfg
}

// New starts a function and builds it one part at a time: AddInput / AddPrompt / AddStep /
// AddOutput …, then Submit(). Nothing is created on the server until Submit() (ID is 0 until then); the one
// thing done here is a read-only check that the name is free, archived functions included.
//
// While you build, a draft file (YAML) on your computer is kept up to date after every change, so work
// survives a stopped program. By default it is .fnai/drafts/<slug>.yaml next to where the program runs.
// Choose another place with SaveTo("path/file.yaml") (or a folder), or set WithDraftsFolder on the
// client. NoDraftFile() turns it off. Submit() turns it into that version's copy (.fnai/versions/<slug>/v<N>.yaml).
func (s *FunctionsService) New(name string, opts ...DraftOption) (*FunctionDraft, error) {
	if err := requireField(name, "name"); err != nil {
		return nil, err
	}
	cfg := buildConfig(opts)
	slug := cfg.slug
	if slug == "" {
		slug = slugify(name)
	}
	var all []struct {
		FunctionOut
	}
	if err := s.h.do("GET", "/api/functions", nil, url.Values{"include_archived": {"true"}}, &all); err != nil {
		return nil, err
	}
	for _, f := range all {
		if f.Slug == slug {
			state := ""
			if f.Archived {
				state = " (archived)"
			}
			return nil, validationErrorf("a function with slug %q already exists%s (id=%d) — pick another name (or WithSlug), or carry on with that one using LoadDraft(%d).",
				slug, state, f.ID, f.ID)
		}
	}
	fn := FunctionOut{Slug: slug, Name: name, Description: cfg.description} // created on the server at Submit()
	d := newFunctionDraft(s.h, fn)
	d.setDraftPath(cfg)
	d.autosave()
	return d, nil
}

// CreateDraft starts a new function. slug defaults to a slugified name when empty. It is
// Functions.New with the older positional arguments.
func (c *Client) CreateDraft(name, slug, description string) (*FunctionDraft, error) {
	return c.Functions.New(name, WithSlug(slug), WithDescription(description))
}

// LoadDraft resumes an existing function to keep building on it — e.g. adding a field after it's already
// deployed. Stages the latest version's schemas, sections, steps and tools so AddX calls extend real
// content instead of starting blank.
func (c *Client) LoadDraft(fnID int, opts ...DraftOption) (*FunctionDraft, error) {
	if err := requireID(fnID, "fnID"); err != nil {
		return nil, err
	}
	var raw struct {
		FunctionOut
		InputSchema  map[string]any `json:"input_schema"`
		OutputSchema map[string]any `json:"output_schema"`
	}
	if err := c.h.do("GET", "/api/functions/"+strconv.Itoa(fnID), nil, nil, &raw); err != nil {
		return nil, err
	}
	draft := newFunctionDraft(c.h, raw.FunctionOut)
	draft.setDraftPath(buildConfig(opts))
	draft.in, draft.out = schemaFieldSet(raw.InputSchema), schemaFieldSet(raw.OutputSchema)
	var versions []VersionOut
	if err := c.h.do("GET", "/api/functions/"+strconv.Itoa(fnID)+"/versions", nil, nil, &versions); err != nil {
		return nil, err
	}
	var chosen *VersionOut
	cfg := buildConfig(opts)
	if cfg.version < 0 {
		return nil, validationErrorf("version must be a version number such as 1, got %d.", cfg.version)
	}
	if cfg.version == 0 {
		if len(versions) > 0 {
			latest := versions[0]
			for _, v := range versions {
				if v.Number > latest.Number {
					latest = v
				}
			}
			chosen = &latest
		}
	} else {
		var numbers []int
		for i, v := range versions {
			numbers = append(numbers, v.Number)
			if v.Number == cfg.version {
				chosen = &versions[i]
			}
		}
		if chosen == nil {
			sort.Ints(numbers)
			have := "none yet"
			if len(numbers) > 0 {
				parts := make([]string, len(numbers))
				for i, n := range numbers {
					parts[i] = strconv.Itoa(n)
				}
				have = strings.Join(parts, ", ")
			}
			return nil, &Error{Status: 404, Detail: fmt.Sprintf("no version %d for %q (versions: %s)", cfg.version, raw.Slug, have)}
		}
		draft.pointAt(chosen)
	}
	if chosen != nil {
		if err := draft.applyVersion(chosen); err != nil {
			return nil, err
		}
		draft.baselineKeys, draft.baseline = draft.outline()
		if cfg.fresh {
			draft.startEmpty()
		}
	}
	return draft, nil
}

// startEmpty forgets the loaded content but keeps the function itself and the version it grew from.
func (d *FunctionDraft) startEmpty() {
	d.in, d.out = newFieldSet(), newFieldSet()
	d.role, d.definitions, d.constraints, d.edgeCases = "", nil, nil, nil
	d.examples, d.tools, d.flow, d.rubric, d.memory, d.mode = nil, nil, nil, nil, nil, ""
	d.extraTop, d.extraImpl = map[string]any{}, map[string]any{}
	d.fieldExtras = map[string]map[string]*fieldExtra{"input": {}, "output": {}}
	d.target, d.base = nil, nil
	d.fresh = true
	d.autosave()
}

// outline is the function as one entry per part (label -> its value as JSON), in a stable order.
func (d *FunctionDraft) outline() ([]string, map[string]string) {
	var keys []string
	vals := map[string]string{}
	put := func(k string, v any) {
		b, _ := json.Marshal(v)
		if _, seen := vals[k]; !seen {
			keys = append(keys, k)
		}
		vals[k] = string(b)
	}
	for _, side := range []string{"input", "output"} {
		fs := d.dslFields(side)
		for _, n := range fs.names {
			put(side+" "+n, fs.specs[n])
		}
	}
	if d.role != "" {
		put("prompt", d.role)
	}
	for _, kv := range []struct {
		kind  string
		items []string
	}{{"definitions", d.definitions}, {"constraints", d.constraints}, {"edge_cases", d.edgeCases}} {
		for _, text := range kv.items {
			put(kv.kind+" "+text, true)
		}
	}
	for _, ex := range d.examples {
		b, _ := json.Marshal(ex)
		put("example "+string(b), true)
	}
	for _, text := range d.flow {
		put("step "+text, true)
	}
	for _, t := range d.tools {
		name, _ := t["name"].(string)
		if name == "" {
			name, _ = t["server"].(string)
		}
		if name == "" {
			b, _ := json.Marshal(t)
			name = string(b)
		}
		put("tool "+name, t)
	}
	for _, text := range d.rubric {
		put("rubric "+text, true)
	}
	if d.memory != nil {
		put("memory", d.memory)
	}
	for _, k := range sortedKeys(d.extraTop) {
		put(k, d.extraTop[k])
	}
	for _, k := range sortedKeys(d.extraImpl) {
		put(k, d.extraImpl[k])
	}
	return keys, vals
}

// Changes lists what differs from the version you loaded: "+ input company" (added), "- input rating"
// (dropped), "~ input name" (changed). Empty when nothing differs or nothing was loaded.
func (d *FunctionDraft) Changes() []string {
	if d.baseline == nil {
		return nil
	}
	keys, now := d.outline()
	var dropped, added, changed []string
	for _, k := range d.baselineKeys {
		if _, ok := now[k]; !ok {
			dropped = append(dropped, "- "+k)
		}
	}
	for _, k := range keys {
		was, ok := d.baseline[k]
		switch {
		case !ok:
			added = append(added, "+ "+k)
		case was != now[k]:
			changed = append(changed, "~ "+k)
		}
	}
	return append(append(dropped, added...), changed...)
}

// pointAt says where the next save goes: a draft is edited in place; a frozen version becomes the base of a new one.
func (d *FunctionDraft) pointAt(v *VersionOut) {
	if v.Status == "draft" {
		d.target, d.base = v, nil
	} else {
		d.base, d.target = v, nil
	}
	d.refreshPath()
}

// applyVersion stages a saved version's content, so AddX calls extend it.
func (d *FunctionDraft) applyVersion(latest *VersionOut) error {
	if _, hasImpl := asMap(latest.Snapshot["implementation"]); len(latest.Snapshot) > 0 && !hasImpl &&
		(latest.Snapshot["sections"] != nil || latest.Snapshot["impl"] != nil) {
		return definitionError(fmt.Sprintf("v%d was saved in the older editor format, which the builder can't edit without losing content. Edit it in the app, or build a new version with the builder.", latest.Number))
	}
	d.LastVersion = latest
	// a function built with Submit() keeps its schemas on the version
	if len(latest.InputSchema) > 0 {
		d.in = schemaFieldSet(latest.InputSchema)
	}
	if len(latest.OutputSchema) > 0 {
		d.out = schemaFieldSet(latest.OutputSchema)
	}
	if impl, ok := latest.Snapshot["implementation"].(map[string]any); ok {
		if sections, ok := impl["sections"].(map[string]any); ok {
			if role, ok := sections["role"].(string); ok {
				d.role = role
			}
			d.definitions = stringSlice(sections["definitions"])
			d.constraints = stringSlice(sections["constraints"])
			d.edgeCases = stringSlice(sections["edge_cases"])
			d.examples = mapSlice(sections["examples"])
		}
		d.tools = mapSlice(impl["tools"])
		d.flow = stringSlice(impl["flow"])
		// only an explicit "agent" is kept; single_shot is the default and flips to agent once a tool is added
		if exec, ok := impl["execution"].(map[string]any); ok && exec["mode"] == "agent" {
			d.mode = "agent"
		}
	}
	d.rubric = stringSlice(latest.Snapshot["rubric"])
	if mem, ok := asMap(latest.Snapshot["memory"]); ok {
		om := NewOrderedMap()
		for _, k := range sortedKeys(mem) {
			om.Set(k, mem[k])
		}
		d.memory = om
	}
	d.extraTop, d.extraImpl = extrasOf(latest.Snapshot)
	for _, side := range []string{"input", "output"} {
		d.fieldExtras[side] = map[string]*fieldExtra{}
		dsl, ok := asMap(latest.Snapshot[side])
		if !ok {
			continue
		}
		for name, raw := range dsl {
			spec, ok := asMap(raw)
			if !ok {
				continue
			}
			if derived, _ := spec["derived"].(bool); derived { // filled by a preprocessor: never in the stored schema
				full := map[string]any{}
				for k, v := range spec {
					if b, isBool := v.(bool); (isBool && !b) || v == nil {
						continue
					}
					full[k] = v
				}
				d.fieldExtras[side][name] = &fieldExtra{flags: map[string]any{}, full: full}
			} else if sens, _ := spec["sensitive"].(bool); sens {
				d.fieldExtras[side][name] = &fieldExtra{flags: map[string]any{"sensitive": true}}
			}
		}
	}
	return nil
}

// dslFields is the fields in the definition's form: the stored schema plus what it cannot hold (derived
// fields, the sensitive flag).
func (d *FunctionDraft) dslFields(side string) *fieldSet {
	base := d.in
	if side == "output" {
		base = d.out
	}
	out := base.copy()
	var extraNames []string
	for name := range d.fieldExtras[side] {
		extraNames = append(extraNames, name)
	}
	sort.Strings(extraNames)
	for _, name := range extraNames {
		ex := d.fieldExtras[side][name]
		if out.has(name) {
			spec := map[string]any{}
			for k, v := range out.specs[name] {
				spec[k] = v
			}
			for k, v := range ex.flags {
				spec[k] = v
			}
			out.set(name, spec)
		} else if ex.full != nil {
			out.set(name, ex.full)
		}
	}
	return out
}

// LoadDocument rebuilds a draft from a parsed definition document (a map, or an OrderedMap to keep field
// order) — what yamlload.Load calls after reading a YAML file. The document is the whole truth: it
// replaces what the function held. If the function already exists on the server (matched by
// meta.function) you continue it; otherwise it is created. source is only used in error messages.
func (c *Client) LoadDocument(docAny any, source string, opts ...DraftOption) (*FunctionDraft, error) {
	docKeys, doc, ok := entries(docAny)
	_ = docKeys
	name, _ := doc["name"].(string)
	if !ok || name == "" {
		return nil, definitionError(source + ": the file must be YAML with at least a `name`")
	}
	impl := map[string]any{}
	if im, ok := asMap(doc["implementation"]); ok {
		impl = im
	}
	slug := slugify(name)
	if meta, ok := asMap(doc["meta"]); ok {
		if f, ok := meta["function"].(string); ok && f != "" {
			slug = f
		}
	}
	var existing []FunctionOut
	if err := c.h.do("GET", "/api/functions", nil, nil, &existing); err != nil {
		return nil, err
	}
	var fn FunctionOut
	found := false
	for _, f := range existing {
		if f.Slug == slug {
			fn, found = f, true
			break
		}
	}
	if !found { // not on the server yet: created at Submit()
		desc, _ := doc["description"].(string)
		fn = FunctionOut{Slug: slug, Name: name, Description: desc}
	}
	d := newFunctionDraft(c.h, fn)
	if desc, ok := doc["description"].(string); ok && desc != "" {
		d.Description = desc
	}
	d.setDraftPath(buildConfig(opts))
	d.loading = true
	defer func() { d.loading = false }()

	addFields := func(side string, v any, add func(string, map[string]any) error) error {
		keys, vals, isMap := entries(v)
		if !isMap {
			return nil
		}
		for _, n := range keys {
			spec, _ := asMap(vals[n])
			if spec == nil {
				spec = map[string]any{}
			}
			if derived, _ := spec["derived"].(bool); side == "input" && derived {
				// filled by a preprocessor: not a caller input, so not in the stored schema
				d.fieldExtras[side][n] = &fieldExtra{flags: map[string]any{}, full: spec}
				continue
			}
			if err := add(n, spec); err != nil {
				return err
			}
		}
		return nil
	}
	d.extraTop, d.extraImpl = extrasOf(doc)
	if err := addFields("input", doc["input"], d.AddInput); err != nil {
		return nil, err
	}
	if err := addFields("output", doc["output"], d.AddOutput); err != nil {
		return nil, err
	}
	if exec, ok := asMap(impl["execution"]); ok && exec["mode"] == "agent" {
		_ = d.SetMode("agent")
	}
	for _, step := range anyStrings(impl["flow"]) {
		if err := d.AddStep(step); err != nil {
			return nil, err
		}
	}
	if sections, ok := asMap(impl["sections"]); ok {
		if role, _ := sections["role"].(string); role != "" {
			if err := d.AddPrompt(role); err != nil {
				return nil, err
			}
		}
		for _, kind := range []string{"definitions", "constraints", "edge_cases"} {
			for _, text := range anyStrings(sections[kind]) {
				if err := d.AddSection(kind, text); err != nil {
					return nil, err
				}
			}
		}
		if exs, ok := sections["examples"].([]any); ok {
			for _, e := range exs {
				em, _ := asMap(e)
				pol, _ := em["polarity"].(string)
				in, _ := asMap(em["input"])
				out, _ := asMap(em["output"])
				note, _ := em["note"].(string)
				if err := d.AddExample(ExampleOpts{Polarity: pol, Input: in, Output: out, BadValue: em["bad_value"], Note: note}); err != nil {
					return nil, err
				}
			}
		}
	}
	if tools, ok := impl["tools"].([]any); ok {
		for _, t := range tools {
			tm, _ := asMap(t)
			if _, isServer := tm["server"]; isServer {
				d.tools = append(d.tools, tm) // expanded by the platform; kept as written
				continue
			}
			tn, _ := tm["name"].(string)
			opt := ToolOptions{}
			if mc, ok := toFloat(tm["max_calls"]); ok {
				opt.MaxCalls = int(mc)
			}
			opt.Requires, _ = tm["requires"].(string)
			opt.Effect, _ = tm["effect"].(string)
			opt.Ref, _ = tm["ref"].(string)
			opt.Config, _ = asMap(tm["config"])
			if err := d.AddTool(tn, opt); err != nil {
				return nil, err
			}
		}
	}
	for _, sentence := range anyStrings(doc["rubric"]) {
		if err := d.AddRubric(sentence); err != nil {
			return nil, err
		}
	}
	if mem, ok := asMap(doc["memory"]); ok {
		per, _ := mem["per"].(string)
		if err := d.SetMemory(Memory{Per: per, Remember: anyStrings(mem["remember"]), Never: anyStrings(mem["never"])}); err != nil {
			return nil, err
		}
	}
	d.loading = false
	// a draft made with WithVersion remembers which version it grew from, so a stopped program can't turn
	// into an accidental overwrite of another draft
	if meta, ok := asMap(doc["meta"]); ok && d.ID != 0 {
		number := 0
		for _, key := range []string{"editing", "based_on", "version"} {
			if n, ok := toFloat(meta[key]); ok && number == 0 {
				number = int(n)
			}
		}
		if number != 0 {
			var versions []VersionOut
			if err := c.h.do("GET", "/api/functions/"+strconv.Itoa(d.ID)+"/versions", nil, nil, &versions); err != nil {
				return nil, err
			}
			var found *VersionOut
			for i, v := range versions {
				if v.Number == number {
					found = &versions[i]
				}
			}
			if found == nil {
				return nil, &Error{Status: 404, Detail: fmt.Sprintf("%s: it was based on v%d, which no longer exists on the server.", source, number)}
			}
			d.pointAt(found) // a draft that has been deployed since is now frozen: a new version
		}
	}
	d.autosave()
	return d, nil
}

func truthy(v any) bool {
	switch x := v.(type) {
	case nil:
		return false
	case []any:
		return len(x) > 0
	case string:
		return x != ""
	case bool:
		return x
	}
	if keys, _, ok := entries(v); ok {
		return len(keys) > 0
	}
	return true
}

var nonAlnumRe2 = regexp.MustCompile(`[^a-z0-9]+`)

func slugify(name string) string {
	slug := strings.Trim(nonAlnumRe2.ReplaceAllString(strings.ToLower(name), "_"), "_")
	if slug == "" {
		return "function"
	}
	return slug
}

func stringSlice(v any) []string { return anyStrings(v) }

func mapSlice(v any) []map[string]any {
	raw, ok := v.([]any)
	if !ok {
		return nil
	}
	out := make([]map[string]any, 0, len(raw))
	for _, item := range raw {
		if m, ok := asMap(item); ok {
			out = append(out, m)
		}
	}
	return out
}

// ── the local draft file ─────────────────────────────────────────────────────

func expandHome(p string) string {
	if p == "~" || strings.HasPrefix(p, "~/") || strings.HasPrefix(p, `~\`) {
		if home, err := os.UserHomeDir(); err == nil {
			return filepath.Join(home, p[1:])
		}
	}
	return p
}

// draftsDir is where draft files go when no exact path is given: the folder the user set, else
// $FNAI_DRAFTS_DIR, else .fnai/drafts next to where the program runs.
func draftsDir(folder string) string {
	base := folder
	if base == "" {
		base = os.Getenv("FNAI_DRAFTS_DIR")
	}
	if base == "" {
		wd, _ := os.Getwd()
		base = filepath.Join(wd, ".fnai", "drafts")
	}
	abs, err := filepath.Abs(expandHome(base))
	if err != nil {
		return base
	}
	return abs
}

var yamlExtRe = regexp.MustCompile(`(?i)\.ya?ml$`)

func (d *FunctionDraft) setDraftPath(cfg draftConfig) {
	d.cfg = cfg
	d.refreshPath()
}

func (d *FunctionDraft) refreshPath() { d.DraftPath = d.draftFile() }

// draftFile is the working draft file, "" when files are off. It is named for what you are editing, so two
// drafts of one function never share a file: <slug>.yaml (a new function or the latest), <slug>.v3.yaml
// (editing draft v3), <slug>.from-v1.yaml (a new version being built from v1). An exact SaveTo file is used
// as given.
func (d *FunctionDraft) draftFile() string {
	if d.cfg.noFile {
		return ""
	}
	slug := d.Slug
	if slug == "" {
		slug = slugify(d.Name)
	}
	folder := ""
	if d.cfg.saveTo != "" {
		p, err := filepath.Abs(expandHome(d.cfg.saveTo))
		if err != nil {
			p = d.cfg.saveTo
		}
		if yamlExtRe.MatchString(p) {
			return p
		}
		folder = p
	} else {
		folder = draftsDir(d.h.draftsFolder)
	}
	suffix := ""
	if d.target != nil {
		suffix = fmt.Sprintf(".v%d", d.target.Number)
	} else if d.base != nil {
		suffix = fmt.Sprintf(".from-v%d", d.base.Number)
	}
	return filepath.Join(folder, slug+suffix+".yaml")
}

// versionsDir is where a copy of every saved version goes: the folder the user set, else $FNAI_VERSIONS_DIR,
// else .fnai/versions beside .fnai/drafts (inside the drafts folder if that is not called "drafts").
func versionsDir(versionsFolder, draftsFolder string) string {
	base := versionsFolder
	if base == "" {
		base = os.Getenv("FNAI_VERSIONS_DIR")
	}
	if base != "" {
		if abs, err := filepath.Abs(expandHome(base)); err == nil {
			return abs
		}
		return base
	}
	drafts := draftsDir(draftsFolder)
	if filepath.Base(drafts) == "drafts" {
		return filepath.Join(filepath.Dir(drafts), "versions")
	}
	return filepath.Join(drafts, "versions")
}

// versionRecordPath is where the copy of saved version n goes, "" when copies are off.
func (d *FunctionDraft) versionRecordPath(n int) string {
	if d.cfg.noFile || d.h.noVersionCopies {
		return ""
	}
	slug := d.Slug
	if slug == "" {
		slug = slugify(d.Name)
	}
	return filepath.Join(versionsDir(d.h.versionsFolder, d.h.draftsFolder), slug, fmt.Sprintf("v%d.yaml", n))
}

// writeAtomic writes to a temporary file beside the real one, then swaps it in, so a crash halfway never
// leaves a half-written file.
func writeAtomic(path, text string) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, []byte(text), 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

func (d *FunctionDraft) fileYaml() string {
	meta := "meta:\n  function: " + yamlScalar(d.Slug) + "\n"
	if d.base != nil {
		meta += fmt.Sprintf("  based_on: %d\n", d.base.Number)
	}
	if d.target != nil {
		meta += fmt.Sprintf("  editing: %d\n", d.target.Number)
	}
	return meta + d.ToYAML()
}

// autosave keeps the draft file in step with the function. A problem writing it warns once on stderr and
// never stops the program.
func (d *FunctionDraft) autosave() {
	d.refreshPath()
	if d.DraftPath == "" || d.loading {
		return
	}
	if err := writeAtomic(d.DraftPath, d.fileYaml()); err != nil && !d.warned {
		d.warned = true
		fmt.Fprintf(os.Stderr, "fnai: couldn't write the draft file %s: %v. Your work is still in memory.\n", d.DraftPath, err)
	}
}

func (d *FunctionDraft) finishSave(working string, n int) {
	if working == "" {
		return
	}
	if record := d.versionRecordPath(n); record != "" {
		text := fmt.Sprintf("meta:\n  function: %s\n  version: %d\n", yamlScalar(d.Slug), n) + d.ToYAML()
		if err := writeAtomic(record, text); err != nil {
			fmt.Fprintf(os.Stderr, "fnai: couldn't write the copy of v%d at %s: %v\n", n, record, err)
		}
	}
	_ = os.Remove(working)
}

// SaveYAML writes the function as YAML to a path you choose, any time. A path ending in .yaml/.yml is the
// file; anything else is a folder, and the file is named after the function. Folders are created. It
// returns the path written. Unlike the automatic draft file, this copy stays after Submit().
func (d *FunctionDraft) SaveYAML(path string) (string, error) {
	p, err := filepath.Abs(expandHome(path))
	if err != nil {
		return "", err
	}
	if !yamlExtRe.MatchString(p) {
		p = filepath.Join(p, d.Slug+".yaml")
	}
	if err := writeAtomic(p, d.fileYaml()); err != nil {
		return "", fmt.Errorf("couldn't write %s: %w", p, err)
	}
	return p, nil
}

// DraftFile returns the path of an existing YAML file, or — when pathOrName isn't a file — the draft file
// of that name in the drafts folder. It is what yamlload.Load uses to find the file to read.
func (c *Client) DraftFile(pathOrName string) (string, error) {
	p, err := filepath.Abs(expandHome(pathOrName))
	if err == nil {
		if st, err := os.Stat(p); err == nil && !st.IsDir() {
			return p, nil
		}
	}
	folder := draftsDir(c.h.draftsFolder)
	slug := slugify(pathOrName)
	exact := filepath.Join(folder, slug+".yaml")
	if _, err := os.Stat(exact); err == nil {
		return exact, nil
	}
	// a draft is named for what it edits: <slug>.yaml, <slug>.v3.yaml, <slug>.from-v1.yaml
	own := regexp.MustCompile("^" + regexp.QuoteMeta(slug) + `\.(v|from-v)\d+\.yaml$`)
	entries, _ := os.ReadDir(folder)
	var matches []string
	for _, e := range entries {
		if own.MatchString(e.Name()) {
			matches = append(matches, e.Name())
		}
	}
	switch len(matches) {
	case 0:
		return "", fmt.Errorf("no YAML file at %s and no draft named %q in %s", p, pathOrName, folder)
	case 1:
		return filepath.Join(folder, matches[0]), nil
	}
	return "", fmt.Errorf("%d drafts match %q in %s: %s — pass the file's path", len(matches), pathOrName, folder, strings.Join(matches, ", "))
}

// ── adding the parts ─────────────────────────────────────────────────────────

func (d *FunctionDraft) addField(side, name string, spec map[string]any, set *fieldSet) error {
	if err := requireField(name, "fieldName"); err != nil {
		return err
	}
	full := map[string]any{"type": "string"}
	for k, v := range spec {
		full[k] = v
	}
	problems := checkField(side, name, full)
	if _, isExtra := d.fieldExtras[side][name]; set.has(name) || isExtra {
		problems = append(problems, fmt.Sprintf("%s '%s' is already defined", side, name))
	}
	if len(problems) > 0 {
		return &DefinitionError{Problems: problems}
	}
	if r, _ := full["required"].(bool); !r {
		delete(full, "required")
	}
	set.set(name, full)
	if sens, _ := full["sensitive"].(bool); sens { // the stored schema can't hold it: keep it alongside
		d.fieldExtras[side][name] = &fieldExtra{flags: map[string]any{"sensitive": true}}
	}
	d.autosave()
	return nil
}

// AddInput attaches one input field (spec: "type", "required", "minimum", "enum", "format", …).
// Checked now against the definition rules — a mistake returns a *DefinitionError here, not later.
// Just remembered for now, not saved yet.
func (d *FunctionDraft) AddInput(fieldName string, spec map[string]any) error {
	return d.addField("input", fieldName, spec, d.in)
}

// AddOutput attaches one output field. Checked now, same as AddInput. Just remembered for now.
func (d *FunctionDraft) AddOutput(fieldName string, spec map[string]any) error {
	return d.addField("output", fieldName, spec, d.out)
}

func (d *FunctionDraft) removeField(side, name string, set *fieldSet) error {
	if err := requireField(name, "fieldName"); err != nil {
		return err
	}
	if _, isExtra := d.fieldExtras[side][name]; !set.has(name) && !isExtra {
		return definitionError(fmt.Sprintf("%s '%s' is not defined, nothing to remove", side, name))
	}
	set.remove(name)
	delete(d.fieldExtras[side], name)
	d.autosave()
	return nil
}

// RemoveInput takes one input field out of this draft. Returns a *DefinitionError if there is no such field.
// Just remembered for now; an example that still uses the field is reported when you Submit.
func (d *FunctionDraft) RemoveInput(fieldName string) error {
	return d.removeField("input", fieldName, d.in)
}

// RemoveOutput takes one output field out of this draft. Same rules as RemoveInput.
func (d *FunctionDraft) RemoveOutput(fieldName string) error {
	return d.removeField("output", fieldName, d.out)
}

// RemoveStep takes one step out of the flow, by its number (1 = the first step, as printing lists them).
func (d *FunctionDraft) RemoveStep(n int) error {
	if n < 1 || n > len(d.flow) {
		return definitionError(fmt.Sprintf("step %d does not exist (the flow has %d step(s))", n, len(d.flow)))
	}
	d.flow = append(d.flow[:n-1:n-1], d.flow[n:]...)
	d.autosave()
	return nil
}

// RemoveStepText takes one step out of the flow, by its exact text.
func (d *FunctionDraft) RemoveStepText(text string) error {
	for i, s := range d.flow {
		if s == text {
			return d.RemoveStep(i + 1)
		}
	}
	return definitionError(fmt.Sprintf("no step matches %q", text))
}

// AddPrompt sets the prompt: who the function is and what it does (the role section). Replaces any
// earlier prompt. Just remembered for now.
func (d *FunctionDraft) AddPrompt(text string) error { return d.AddSection("role", text) }

// AddSection attaches a piece of prompt text. kind is "role" (replaces — a function has exactly one
// role), or "definitions" / "constraints" / "edge_cases" (appends — many of each). Just remembered for now.
func (d *FunctionDraft) AddSection(kind, content string) error {
	if err := requireField(content, "content"); err != nil {
		return err
	}
	switch kind {
	case "role":
		d.role = content
	case "definitions":
		d.definitions = append(d.definitions, content)
	case "constraints":
		d.constraints = append(d.constraints, content)
	case "edge_cases":
		d.edgeCases = append(d.edgeCases, content)
	default:
		return validationErrorf(`kind must be "role", "definitions", "constraints", or "edge_cases", got %q.`, kind)
	}
	d.autosave()
	return nil
}

// AddExample attaches one worked example. A positive example needs Input and Output; a negative one needs
// Output (or BadValue) and a Note. Checked now. Just remembered for now.
func (d *FunctionDraft) AddExample(opts ExampleOpts) error {
	pol := opts.Polarity
	if pol == "" {
		pol = "positive"
	}
	if problems := exampleProblems(pol, opts.Input, opts.Output, opts.BadValue, opts.Note); len(problems) > 0 {
		return &DefinitionError{Problems: problems}
	}
	ex := map[string]any{"polarity": pol}
	if opts.Input != nil {
		ex["input"] = opts.Input
	}
	if opts.Output != nil {
		ex["output"] = opts.Output
	}
	if opts.BadValue != nil {
		ex["bad_value"] = opts.BadValue
	}
	if opts.Note != "" {
		ex["note"] = opts.Note
	}
	d.examples = append(d.examples, ex)
	d.autosave()
	return nil
}

// AddStep adds one step the function follows, in order (the definition's flow). At least one is required.
// Just remembered for now.
func (d *FunctionDraft) AddStep(text string) error {
	if err := requireField(text, "text"); err != nil {
		return err
	}
	d.flow = append(d.flow, text)
	d.autosave()
	return nil
}

// AddRubric adds one sentence describing what a good answer looks like. Use it when the output is free
// text, which a rule like "a number from 1 to 5" cannot judge. Just remembered for now.
func (d *FunctionDraft) AddRubric(text string) error {
	if strings.TrimSpace(text) == "" {
		return definitionError("rubric: the sentence can't be empty")
	}
	for _, r := range d.rubric {
		if r == text {
			return definitionError(fmt.Sprintf("rubric: this sentence is already added: %q", text))
		}
	}
	d.rubric = append(d.rubric, text)
	d.autosave()
	return nil
}

// SetMemory lets the function remember things between runs. A second call replaces the first. Just
// remembered for now.
func (d *FunctionDraft) SetMemory(m Memory) error {
	var problems []string
	if !contains2(memoryScopes, m.Per) {
		problems = append(problems, fmt.Sprintf("memory: per must be 'caller', 'space' or 'organisation', got %s", show(m.Per)))
	}
	for label, items := range map[string][]string{"remember": m.Remember, "never": m.Never} {
		for _, i := range items {
			if strings.TrimSpace(i) == "" {
				problems = append(problems, fmt.Sprintf("memory: %s must be a list of non-empty sentences", label))
				break
			}
		}
	}
	if len(problems) > 0 {
		return &DefinitionError{Problems: problems}
	}
	mem := NewOrderedMap()
	mem.Set("per", m.Per)
	if len(m.Remember) > 0 {
		mem.Set("remember", m.Remember)
	}
	if len(m.Never) > 0 {
		mem.Set("never", m.Never)
	}
	d.memory = mem
	d.autosave()
	return nil
}

// SetMode picks "single_shot" (one pass) or "agent" (loops, can use tools). Left alone, it is "agent"
// once a tool is added and "single_shot" otherwise.
func (d *FunctionDraft) SetMode(mode string) error {
	if mode != "single_shot" && mode != "agent" {
		return validationErrorf(`mode must be "single_shot" or "agent", got %q.`, mode)
	}
	d.mode = mode
	d.autosave()
	return nil
}

// AddTool attaches one tool it's allowed to use, with this function's own limits (see ToolOptions).
// Checked now. Just remembered for now.
func (d *FunctionDraft) AddTool(name string, opts ToolOptions) error {
	if err := requireField(name, "name"); err != nil {
		return err
	}
	var problems []string
	for _, t := range d.tools {
		if t["name"] == name {
			problems = append(problems, fmt.Sprintf("tool '%s' is already added", name))
		}
	}
	if opts.MaxCalls < 0 {
		problems = append(problems, fmt.Sprintf("tool '%s': MaxCalls must be a whole number, 1 or more", name))
	}
	if opts.Effect != "" && !contains2(effectNames, opts.Effect) {
		problems = append(problems, fmt.Sprintf("tool '%s': effect must be one of %s, got %s", name, strings.Join(effectNames, ", "), show(opts.Effect)))
	}
	if opts.Requires != "" {
		problems = append(problems, guardProblems(name, opts.Requires, nil, nil)...)
	}
	if len(problems) > 0 {
		return &DefinitionError{Problems: problems}
	}
	entry := map[string]any{"name": name}
	if len(opts.Config) > 0 {
		entry["config"] = opts.Config
	}
	if opts.MaxCalls > 0 {
		entry["max_calls"] = opts.MaxCalls
	}
	if opts.Requires != "" {
		entry["requires"] = opts.Requires
	}
	if opts.Effect != "" {
		entry["effect"] = opts.Effect
	}
	if opts.Ref != "" {
		entry["ref"] = opts.Ref
	}
	d.tools = append(d.tools, entry)
	d.autosave()
	return nil
}

// CheckInput tests a sample input against this function's input rules without calling the server. It
// returns the problems found (nil means it passes).
func (d *FunctionDraft) CheckInput(sample map[string]any) []string {
	return validateSample(d.in.jsonSchema(), sample)
}

// ── printing ─────────────────────────────────────────────────────────────────

func (d *FunctionDraft) effectiveMode() string {
	if d.mode != "" {
		return d.mode
	}
	if len(d.tools) > 0 {
		return "agent"
	}
	return "single_shot"
}

func (d *FunctionDraft) sections() *OrderedMap {
	s := NewOrderedMap()
	if d.role != "" {
		s.Set("role", d.role)
	}
	if len(d.definitions) > 0 {
		s.Set("definitions", d.definitions)
	}
	if len(d.constraints) > 0 {
		s.Set("constraints", d.constraints)
	}
	if len(d.edgeCases) > 0 {
		s.Set("edge_cases", d.edgeCases)
	}
	if len(d.examples) > 0 {
		s.Set("examples", d.examples)
	}
	return s
}

// ToYAML returns the function as YAML in the definition format (function-dsl.md).
func (d *FunctionDraft) ToYAML() string {
	return renderYaml(yamlParts{
		name: d.Name, description: d.Description, input: d.dslFields("input"), output: d.dslFields("output"),
		mode: d.effectiveMode(), flow: d.flow, rubric: d.rubric, memory: d.memory,
		sections: d.sections(), tools: d.tools, extraTop: d.extraTop, extraImpl: d.extraImpl,
	})
}

// Missing says what the function still needs: Required (the server needs it) and Recommended.
func (d *FunctionDraft) Missing() Missing {
	return missingLists(d.dslFields("input"), d.dslFields("output"), d.flow, d.sections())
}

// String shows the YAML, then what is missing. fmt.Println(draft) uses it.
func (d *FunctionDraft) String() string {
	note := ""
	if d.base != nil {
		note = fmt.Sprintf("# based on v%d — saving creates a new version\n", d.base.Number)
	} else if d.target != nil {
		note = fmt.Sprintf("# editing draft v%d — saving updates it\n", d.target.Number)
	}
	return note + describe(d.ToYAML(), d.Missing())
}

// ── saving ───────────────────────────────────────────────────────────────────

// snapshot is the function as a v2 definition document: input and output in the definition's field form,
// plus implementation (mode, flow, sections, tools) — what the server validates and compiles.
func (d *FunctionDraft) snapshot() map[string]any {
	sections := map[string]any{}
	for _, k := range d.sections().Keys {
		sections[k] = d.sections().Values[k]
	}
	flow := d.flow
	if flow == nil {
		flow = []string{}
	}
	tools := d.tools
	if tools == nil {
		tools = []map[string]any{}
	}
	snap := map[string]any{
		"name":   d.Name,
		"input":  d.dslFields("input").dsl(),
		"output": d.dslFields("output").dsl(),
		"implementation": map[string]any{
			"execution": map[string]any{"mode": d.effectiveMode()},
			"flow":      flow, "sections": sections, "tools": tools,
		},
	}
	for k, v := range d.extraImpl {
		snap["implementation"].(map[string]any)[k] = v
	}
	for k, v := range d.extraTop {
		snap[k] = v
	}
	if d.Description != "" {
		snap["description"] = d.Description
	}
	if len(d.rubric) > 0 {
		snap["rubric"] = d.rubric
	}
	if d.memory != nil {
		snap["memory"] = d.memory
	}
	return snap
}

// toolProblems names tools in AddTool() that don't match a tool this space can use. One GET at submit time;
// skipped if the lookup itself fails (the server rejects an unknown tool on submit anyway).
func (d *FunctionDraft) toolProblems() []string {
	var names []string
	for _, t := range d.tools {
		if _, pinned := t["ref"]; pinned {
			continue
		}
		if n, ok := t["name"].(string); ok {
			names = append(names, n)
		}
	}
	if len(names) == 0 {
		return nil
	}
	var list []struct {
		Name   string `json:"name"`
		Status string `json:"status"`
	}
	if err := d.h.do("GET", "/api/tools", nil, nil, &list); err != nil {
		return nil
	}
	seen := map[string]bool{}
	var known []string
	for _, t := range list {
		if (t.Status == "" || t.Status == "active" || t.Status == "deprecated") && !seen[t.Name] {
			seen[t.Name] = true
			known = append(known, t.Name)
		}
	}
	sort.Strings(known)
	var problems []string
	for _, n := range names {
		if seen[n] {
			continue
		}
		hint := ""
		if near := closestMatch(n, known); near != "" {
			hint = fmt.Sprintf(" — did you mean '%s'?", near)
		}
		avail := strings.Join(known, ", ")
		if avail == "" {
			avail = "none"
		}
		problems = append(problems, fmt.Sprintf("tool '%s' not found%s (available: %s; register your own with Tools.Create)", n, hint, avail))
	}
	return problems
}

func (d *FunctionDraft) structureProblems() []string {
	var problems []string
	if len(d.out.names) == 0 {
		problems = append(problems, "no output fields — AddOutput(...)")
	}
	if len(d.flow) == 0 {
		problems = append(problems, "no steps — AddStep(...)")
	}
	declaredTools, declaredInputs := map[string]bool{}, map[string]bool{}
	hasServer := false // a server entry expands to tools we can't see here
	for _, t := range d.tools {
		if _, isServer := t["server"]; isServer {
			hasServer = true
		}
		if n, ok := t["name"].(string); ok {
			declaredTools[n] = true
		}
	}
	if hasServer {
		declaredTools = nil
	}
	for _, n := range d.in.names {
		declaredInputs[n] = true
	}
	for n := range d.fieldExtras["input"] {
		declaredInputs[n] = true
	}
	for _, t := range d.tools {
		if req, ok := t["requires"].(string); ok && req != "" {
			if tn, ok := t["name"].(string); ok {
				problems = append(problems, guardProblems(tn, req, declaredTools, declaredInputs)...)
			}
		}
	}
	problems = append(problems, d.toolProblems()...)
	for i, ex := range d.examples {
		if in, ok := asMap(ex["input"]); ok && len(d.in.names) > 0 {
			for _, k := range sortedKeys(in) {
				if !d.in.has(k) {
					problems = append(problems, fmt.Sprintf("example %d: input field '%s' is not declared (AddInput)", i+1, k))
				}
			}
		}
		if out, ok := asMap(ex["output"]); ok && len(d.out.names) > 0 {
			for _, k := range sortedKeys(out) {
				if !d.out.has(k) {
					problems = append(problems, fmt.Sprintf("example %d: output field '%s' is not declared (AddOutput)", i+1, k))
				}
			}
		}
	}
	return problems
}

// ensureCreated is the first save: it creates the function on the server. The id is remembered, so a retry
// after a failed save carries on with the same function instead of creating it twice.
func (d *FunctionDraft) ensureCreated() error {
	if d.ID != 0 {
		return nil
	}
	var fn FunctionOut
	err := d.h.do("POST", "/api/functions", map[string]any{
		"slug": d.Slug, "name": d.Name, "description": d.Description,
		"input_schema": d.in.jsonSchema(), "output_schema": d.out.jsonSchema(),
	}, nil, &fn)
	if err != nil {
		if e, ok := err.(*Error); ok && e.Status == 409 {
			return &Error{Status: 409, Detail: fmt.Sprintf("a function with slug %q already exists in this organization (archived functions keep their name). Nothing was saved; your work is still in memory and in the draft file. Start again with a different slug: Functions.New(name, WithSlug(\"...\")).", d.Slug)}
		}
		return err
	}
	d.ID = fn.ID
	return nil
}

// Submit is Save. Everything remembered since the last save gets committed now. Updates the current draft
// in place, or starts a new version if the current one is already deployed and frozen — the one rule that
// decision follows, used by the UI and every SDK caller alike (POST /api/functions/{id}/submit).
//
// Before sending, it checks the whole function and returns one *DefinitionError listing every problem.
// On success the local draft file becomes that version's copy (.fnai/versions/<slug>/v<N>.yaml).
func (d *FunctionDraft) Submit(message string) (*VersionOut, error) { return d.submit(message, false) }

// SubmitAsNew saves as a NEW version, even if the one you started from is a draft: that version is left
// exactly as it was, and the new one records it as its parent. Nothing is deployed. Later plain Submit calls
// update the new draft in place.
func (d *FunctionDraft) SubmitAsNew(message string) (*VersionOut, error) {
	return d.submit(message, true)
}

func (d *FunctionDraft) submit(message string, asNew bool) (*VersionOut, error) {
	asNew = asNew || d.fresh // a fresh draft can only become a NEW version: it must not overwrite the one it grew from
	if problems := d.structureProblems(); len(problems) > 0 {
		return nil, &DefinitionError{Problems: problems}
	}
	if d.fresh {
		shown := d.Changes()
		if len(shown) == 0 {
			fmt.Fprintln(changesOut, "No changes from the version you started from.")
		} else {
			fmt.Fprintln(changesOut, "Changes from the version you started from:")
			for _, line := range shown {
				fmt.Fprintln(changesOut, "  "+line)
			}
		}
	}
	if err := d.ensureCreated(); err != nil {
		return nil, err
	}
	working := d.draftFile() // the file you were working in, before a save changes what you are editing
	body := map[string]any{
		"snapshot": d.snapshot(), "message": message,
		"input_schema": d.in.jsonSchema(), "output_schema": d.out.jsonSchema(),
	}
	var out VersionOut
	var err error
	switch {
	case asNew:
		// branch: always a NEW version; the one it grew from is left untouched
		parent := d.target
		if parent == nil {
			parent = d.base
		}
		if parent == nil {
			parent = d.LastVersion
		}
		if parent != nil {
			body["parent_id"] = parent.ID
		}
		err = d.h.do("POST", "/api/functions/"+strconv.Itoa(d.ID)+"/versions", body, nil, &out)
		if err == nil {
			created := out
			d.target, d.base = &created, nil // it is a draft now: later saves update it
		}
	case d.target != nil:
		// a draft chosen with WithVersion: edit it in place
		err = d.h.do("PUT", "/api/versions/"+strconv.Itoa(d.target.ID), body, nil, &out)
		if e, ok := err.(*Error); ok && e.Status == 409 {
			return nil, &Error{Status: 409, Detail: fmt.Sprintf("v%d has been deployed since you loaded it, so it can no longer be edited. Your work is still in memory and in the draft file. Load it again to base a new version on it: LoadDraft(..., WithVersion(%d)).", d.target.Number, d.target.Number)}
		}
	case d.base != nil:
		// based on a frozen version: always a NEW version, never an overwrite
		body["parent_id"] = d.base.ID
		err = d.h.do("POST", "/api/functions/"+strconv.Itoa(d.ID)+"/versions", body, nil, &out)
		if err == nil {
			created := out
			d.target, d.base = &created, nil // it is a draft now: later saves update it
		}
	default:
		err = d.h.do("POST", "/api/functions/"+strconv.Itoa(d.ID)+"/submit", body, nil, &out)
	}
	if err != nil {
		return nil, err
	}
	d.LastVersion = &out
	d.fresh = false // saved: later plain saves update the new draft
	d.baselineKeys, d.baseline = d.outline()
	d.finishSave(working, out.Number) // the draft file becomes this version's copy
	return &out, nil
}

// Deploy makes a version live. Deploys the version from the most recent Submit() — call Submit() first if
// there are unsaved AddX changes.
func (d *FunctionDraft) Deploy(note string) (*DeploymentOut, error) {
	return d.DeployWithModel(note, "", nil)
}

// DeployWithModel is Deploy with the model production calls run on. Without one the space's default model
// is used, and with neither the deploy is refused.
func (d *FunctionDraft) DeployWithModel(note, model string, temperature *float64) (*DeploymentOut, error) {
	if d.LastVersion == nil {
		return nil, validationErrorf("nothing to deploy yet — call Submit() first.")
	}
	body := map[string]any{"version_id": d.LastVersion.ID, "note": note}
	if model != "" {
		body["model"] = model
	}
	if temperature != nil {
		body["temperature"] = *temperature
	}
	var out DeploymentOut
	return &out, d.h.do("POST", "/api/functions/"+strconv.Itoa(d.ID)+"/deploy", body, nil, &out)
}
