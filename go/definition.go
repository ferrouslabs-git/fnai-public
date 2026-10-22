package fnai

// The function-definition rules (function-dsl.md, "The field grammar"), shared by the
// step-by-step builder: checking a field, turning fields into the stored schema and back,
// printing a function as YAML, listing what is missing, and testing a sample input.
// Pure functions: no network and no files.

import (
	"encoding/json"
	"fmt"
	"math"
	"regexp"
	"sort"
	"strconv"
	"strings"
)

// DefinitionError means a field or function breaks the definition rules. Problems lists every
// issue found, so one call reports them all.
type DefinitionError struct{ Problems []string }

func (e *DefinitionError) Error() string { return strings.Join(e.Problems, "; ") }

func definitionError(problems ...string) error { return &DefinitionError{Problems: problems} }

// OrderedMap is a JSON object that keeps its keys in order — field order matters to people reading a
// function, and Go's plain maps lose it. Parsers (such as the yamlload sub-package) can hand
// one to Client.LoadDocument; everything else uses ordinary map[string]any.
type OrderedMap struct {
	Keys   []string
	Values map[string]any
}

// NewOrderedMap returns an empty OrderedMap.
func NewOrderedMap() *OrderedMap { return &OrderedMap{Values: map[string]any{}} }

// Set adds or replaces a key, keeping the order keys were first added in.
func (o *OrderedMap) Set(k string, v any) {
	if _, ok := o.Values[k]; !ok {
		o.Keys = append(o.Keys, k)
	}
	o.Values[k] = v
}

// MarshalJSON writes the keys in order.
func (o OrderedMap) MarshalJSON() ([]byte, error) {
	var b strings.Builder
	b.WriteByte('{')
	for i, k := range o.Keys {
		if i > 0 {
			b.WriteByte(',')
		}
		kb, _ := json.Marshal(k)
		vb, err := json.Marshal(o.Values[k])
		if err != nil {
			return nil, err
		}
		b.Write(kb)
		b.WriteByte(':')
		b.Write(vb)
	}
	b.WriteByte('}')
	return []byte(b.String()), nil
}

// entries returns the (key, value) pairs of a map-like value, in order: an OrderedMap keeps its order,
// a plain map is sorted by key. ok is false when v is not map-like.
func entries(v any) (keys []string, vals map[string]any, ok bool) {
	switch m := v.(type) {
	case OrderedMap:
		return m.Keys, m.Values, true
	case *OrderedMap:
		if m == nil {
			return nil, nil, false
		}
		return m.Keys, m.Values, true
	case map[string]any:
		keys = make([]string, 0, len(m))
		for k := range m {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		return keys, m, true
	}
	return nil, nil, false
}

// plainMap turns a map-like value (including nested OrderedMaps) into plain map[string]any / []any.
func plain(v any) any {
	if keys, vals, ok := entries(v); ok {
		out := make(map[string]any, len(keys))
		for _, k := range keys {
			out[k] = plain(vals[k])
		}
		return out
	}
	if list, ok := v.([]any); ok {
		out := make([]any, len(list))
		for i, x := range list {
			out[i] = plain(x)
		}
		return out
	}
	return v
}

func asMap(v any) (map[string]any, bool) {
	if _, _, ok := entries(v); !ok {
		return nil, false
	}
	m, ok := plain(v).(map[string]any)
	return m, ok
}

var (
	typesIn        = []string{"string", "number", "integer", "boolean", "object", "array", "file"}
	typesOut       = []string{"string", "number", "integer", "boolean", "object", "array"}
	commonKeys     = []string{"type", "required", "description", "sensitive", "enum", "minimum", "maximum", "format", "pattern", "items", "min_items", "max_items", "fields"}
	inputOnlyKeys  = []string{"derived", "media", "formats"}
	outputOnlyKeys = []string{"length", "count"}
	formatNames    = []string{"iso8601", "date", "time", "datetime", "email", "uuid", "url"}
	mediaNames     = []string{"document", "image", "audio", "video", "data"}
	effectNames    = []string{"read_only", "idempotent", "side_effecting"}
	memoryScopes   = []string{"caller", "space", "organisation"}
	// the order a field's keys are written in, so printed YAML is stable
	specKeyOrder = []string{"type", "required", "description", "enum", "minimum", "maximum", "format", "pattern",
		"min_items", "max_items", "items", "fields", "media", "formats", "length", "count", "sensitive", "derived"}
)

func contains2(list []string, s string) bool {
	for _, x := range list {
		if x == s {
			return true
		}
	}
	return false
}

func toFloat(v any) (float64, bool) {
	switch n := v.(type) {
	case int:
		return float64(n), true
	case int8:
		return float64(n), true
	case int16:
		return float64(n), true
	case int32:
		return float64(n), true
	case int64:
		return float64(n), true
	case uint:
		return float64(n), true
	case uint8:
		return float64(n), true
	case uint16:
		return float64(n), true
	case uint32:
		return float64(n), true
	case uint64:
		return float64(n), true
	case float32:
		return float64(n), true
	case float64:
		return n, !math.IsNaN(n)
	case json.Number:
		f, err := n.Float64()
		return f, err == nil
	}
	return 0, false
}

func isNum(v any) bool { _, ok := toFloat(v); return ok }

func isInt(v any) bool {
	f, ok := toFloat(v)
	return ok && f == math.Trunc(f)
}

func show(v any) string {
	if s, ok := v.(string); ok {
		return "'" + s + "'"
	}
	b, _ := json.Marshal(v)
	return string(b)
}

func numStr(f float64) string { return strconv.FormatFloat(f, 'f', -1, 64) }

// anyStr prints a value the way a person would write it: numbers without a trailing ".0".
func anyStr(v any) string {
	if f, ok := toFloat(v); ok {
		return numStr(f)
	}
	return fmt.Sprint(v)
}

// closestMatch returns the closest word in options to word, or "" (a stand-in for Python's difflib).
func closestMatch(word string, options []string) string {
	dist := func(a, b string) int {
		ra, rb := []rune(a), []rune(b)
		d := make([][]int, len(ra)+1)
		for i := range d {
			d[i] = make([]int, len(rb)+1)
			d[i][0] = i
		}
		for j := 0; j <= len(rb); j++ {
			d[0][j] = j
		}
		for i := 1; i <= len(ra); i++ {
			for j := 1; j <= len(rb); j++ {
				cost := 1
				if ra[i-1] == rb[j-1] {
					cost = 0
				}
				d[i][j] = min(d[i-1][j]+1, d[i][j-1]+1, d[i-1][j-1]+cost)
			}
		}
		return d[len(ra)][len(rb)]
	}
	best, bestScore := "", 0.6
	for _, o := range options {
		longest := max(len([]rune(word)), len([]rune(o)))
		if longest == 0 {
			continue
		}
		if score := 1 - float64(dist(word, o))/float64(longest); score >= bestScore {
			best, bestScore = o, score
		}
	}
	return best
}

func checkSoft(label, key string, value any, problems *[]string) {
	m, ok := asMap(value)
	if !ok {
		*problems = append(*problems, fmt.Sprintf("%s: '%s' must be like {unit: tokens, target: 60, range: [40, 80]}", label, key))
		return
	}
	for _, k := range sortedKeys(m) {
		if k != "unit" && k != "target" && k != "range" {
			*problems = append(*problems, fmt.Sprintf("%s: '%s' has unknown key '%s' (allowed: unit, target, range)", label, key, k))
		}
	}
	if u, has := m["unit"]; has && u != "tokens" && u != "chars" {
		*problems = append(*problems, fmt.Sprintf("%s: '%s.unit' must be 'tokens' or 'chars', got %s", label, key, show(u)))
	}
	if t, has := m["target"]; has && !isNum(t) {
		*problems = append(*problems, fmt.Sprintf("%s: '%s.target' must be a number", label, key))
	}
	if r, has := m["range"]; has {
		list, isList := r.([]any)
		if !isList {
			if fl, ok := r.([]float64); ok {
				for _, x := range fl {
					list = append(list, x)
				}
				isList = true
			}
		}
		good := isList && len(list) == 2 && isNum(list[0]) && isNum(list[1])
		if good {
			lo, _ := toFloat(list[0])
			hi, _ := toFloat(list[1])
			good = lo <= hi
		}
		if !good {
			*problems = append(*problems, fmt.Sprintf("%s: '%s.range' must be [low, high] with low <= high", label, key))
		}
	}
}

func sortedKeys(m map[string]any) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

func enumOK(t string, v any) bool {
	switch t {
	case "string":
		_, ok := v.(string)
		return ok
	case "number":
		return isNum(v)
	case "integer":
		return isInt(v)
	case "boolean":
		_, ok := v.(bool)
		return ok
	}
	return false
}

// checkField returns every problem with one field descriptor, per the DSL field grammar.
// side is "input" or "output" (some keys are legal on one side only).
func checkField(side, name string, specAny any) []string {
	label := fmt.Sprintf("%s '%s'", side, name)
	spec, ok := asMap(specAny)
	if !ok {
		return []string{label + `: must be a map like {"type": "string", "required": true}`}
	}
	var problems []string
	legal := append([]string{}, commonKeys...)
	wrongSide := outputOnlyKeys
	if side == "input" {
		legal = append(legal, inputOnlyKeys...)
	} else {
		legal = append(legal, outputOnlyKeys...)
		wrongSide = inputOnlyKeys
	}
	for _, key := range sortedKeys(spec) {
		if contains2(legal, key) {
			continue
		}
		if contains2(wrongSide, key) {
			other := "input"
			if side == "input" {
				other = "output"
			}
			problems = append(problems, fmt.Sprintf("%s: '%s' is only valid on %s fields", label, key, other))
		} else {
			hint := ""
			if near := closestMatch(key, legal); near != "" {
				hint = fmt.Sprintf(" — did you mean '%s'?", near)
			}
			problems = append(problems, fmt.Sprintf("%s: unknown key '%s'%s", label, key, hint))
		}
	}
	types := typesOut
	if side == "input" {
		types = typesIn
	}
	t, _ := spec["type"].(string)
	if !contains2(types, t) {
		extra := ""
		if t == "file" {
			extra = " ('file' is input-only)"
		}
		problems = append(problems, fmt.Sprintf("%s: type %s is not valid%s; use one of %s", label, show(spec["type"]), extra, strings.Join(types, ", ")))
		return problems
	}
	for _, flag := range []string{"required", "sensitive"} {
		if v, has := spec[flag]; has {
			if _, isBool := v.(bool); !isBool {
				problems = append(problems, fmt.Sprintf("%s: '%s' must be true or false", label, flag))
			}
		}
	}
	if d, has := spec["description"]; has {
		if _, isStr := d.(string); !isStr {
			problems = append(problems, label+": 'description' must be text")
		}
	}
	if d, _ := spec["derived"].(bool); d {
		problems = append(problems, label+": derived fields are filled by a preprocessor, which the builder can't author yet — define this function in YAML instead")
	}
	numeric := t == "number" || t == "integer"
	for _, k := range []string{"minimum", "maximum"} {
		if v, has := spec[k]; has {
			if !numeric {
				problems = append(problems, fmt.Sprintf("%s: '%s' needs type number or integer, got %s", label, k, t))
			} else if !isNum(v) {
				problems = append(problems, fmt.Sprintf("%s: '%s' must be a number", label, k))
			}
		}
	}
	if numeric && isNum(spec["minimum"]) && isNum(spec["maximum"]) {
		lo, _ := toFloat(spec["minimum"])
		hi, _ := toFloat(spec["maximum"])
		if lo > hi {
			problems = append(problems, fmt.Sprintf("%s: minimum %s is greater than maximum %s", label, numStr(lo), numStr(hi)))
		}
	}
	if f, has := spec["format"]; has {
		fs, _ := f.(string)
		if t != "string" {
			problems = append(problems, fmt.Sprintf("%s: 'format' needs type string, got %s", label, t))
		} else if !contains2(formatNames, fs) {
			problems = append(problems, fmt.Sprintf("%s: format %s is not valid; use one of %s", label, show(f), strings.Join(formatNames, ", ")))
		}
	}
	if p, has := spec["pattern"]; has {
		if t != "string" {
			problems = append(problems, fmt.Sprintf("%s: 'pattern' needs type string, got %s", label, t))
		} else if ps, isStr := p.(string); !isStr {
			problems = append(problems, label+": 'pattern' must be text")
		} else if _, err := regexp.Compile(ps); err != nil {
			problems = append(problems, fmt.Sprintf("%s: 'pattern' is not a valid regular expression (%s)", label, err))
		}
	}
	if e, has := spec["enum"]; has {
		list, isList := e.([]any)
		if !isList || len(list) == 0 {
			problems = append(problems, label+": 'enum' must be a non-empty list")
		} else if t == "object" || t == "array" {
			problems = append(problems, fmt.Sprintf("%s: 'enum' can't be used with type %s", label, t))
		} else {
			var bad []any
			for _, v := range list {
				if !enumOK(t, v) {
					bad = append(bad, v)
				}
			}
			if len(bad) > 0 {
				b, _ := json.Marshal(bad)
				problems = append(problems, fmt.Sprintf("%s: enum values %s are not of type %s", label, b, t))
			}
		}
	}
	if v, has := spec["length"]; has {
		if t != "string" {
			problems = append(problems, fmt.Sprintf("%s: 'length' needs type string, got %s", label, t))
		}
		checkSoft(label, "length", v, &problems)
	}
	if v, has := spec["count"]; has {
		if t != "array" {
			problems = append(problems, fmt.Sprintf("%s: 'count' needs type array, got %s", label, t))
		}
		checkSoft(label, "count", v, &problems)
	}
	if t == "array" {
		if items, has := spec["items"]; !has {
			problems = append(problems, label+": arrays need 'items', e.g. items: {type: 'string'}")
		} else {
			problems = append(problems, checkField(side, name+"[]", items)...)
		}
	} else if _, has := spec["items"]; has {
		problems = append(problems, fmt.Sprintf("%s: 'items' needs type array, got %s", label, t))
	}
	for _, k := range []string{"min_items", "max_items"} {
		if v, has := spec[k]; has {
			if t != "array" {
				problems = append(problems, fmt.Sprintf("%s: '%s' needs type array, got %s", label, k, t))
			} else if f, ok := toFloat(v); !ok || !isInt(v) || f < 0 {
				problems = append(problems, fmt.Sprintf("%s: '%s' must be a whole number, 0 or more", label, k))
			}
		}
	}
	if isInt(spec["min_items"]) && isInt(spec["max_items"]) {
		lo, _ := toFloat(spec["min_items"])
		hi, _ := toFloat(spec["max_items"])
		if lo > hi {
			problems = append(problems, fmt.Sprintf("%s: min_items %s is greater than max_items %s", label, numStr(lo), numStr(hi)))
		}
	}
	if f, has := spec["fields"]; has {
		keys, vals, isMap := entries(f)
		if t != "object" {
			problems = append(problems, fmt.Sprintf("%s: 'fields' needs type object, got %s", label, t))
		} else if !isMap {
			problems = append(problems, label+": 'fields' must be a map of field descriptors")
		} else {
			for _, sub := range keys {
				problems = append(problems, checkField(side, name+"."+sub, vals[sub])...)
			}
		}
	}
	if _, has := spec["media"]; t == "file" && !has {
		problems = append(problems, fmt.Sprintf("%s: file fields need media, one of %s", label, strings.Join(mediaNames, ", ")))
	}
	if m, has := spec["media"]; has {
		ms, _ := m.(string)
		if t != "file" {
			problems = append(problems, fmt.Sprintf("%s: 'media' needs type file, got %s", label, t))
		} else if !contains2(mediaNames, ms) {
			problems = append(problems, fmt.Sprintf("%s: media %s is not valid; use one of %s", label, show(m), strings.Join(mediaNames, ", ")))
		}
	}
	if f, has := spec["formats"]; has {
		list, isList := f.([]any)
		good := isList && len(list) > 0
		for _, x := range list {
			if _, isStr := x.(string); !isStr {
				good = false
			}
		}
		if t != "file" {
			problems = append(problems, fmt.Sprintf("%s: 'formats' needs type file, got %s", label, t))
		} else if !good {
			problems = append(problems, label+": 'formats' must be a list like ['pdf', 'docx']")
		}
	}
	return problems
}

// projectField turns one DSL field into the JSON-schema property the server stores (mirrors the
// server's own projection): hard gates as JSON Schema, soft targets under x-soft, files as a string handle.
func projectField(spec map[string]any) map[string]any {
	out := map[string]any{"type": spec["type"]}
	if spec["type"] == "file" {
		out["type"] = "string"
	}
	for _, k := range []string{"description", "enum", "minimum", "maximum", "format", "pattern"} {
		if v, has := spec[k]; has {
			out[k] = v
		}
	}
	if v, has := spec["min_items"]; has {
		out["minItems"] = v
	}
	if v, has := spec["max_items"]; has {
		out["maxItems"] = v
	}
	if v, has := spec["items"]; has {
		if im, ok := asMap(v); ok {
			out["items"] = projectField(im)
		}
	}
	if f, has := spec["fields"]; has {
		keys, vals, _ := entries(f)
		props := NewOrderedMap()
		var req []string
		for _, n := range keys {
			sm, _ := asMap(vals[n])
			props.Set(n, projectField(sm))
			if r, _ := sm["required"].(bool); r {
				req = append(req, n)
			}
		}
		out["properties"] = props
		if len(req) > 0 {
			out["required"] = req
		}
	}
	if v, has := spec["media"]; has {
		out["x-media"] = v
		if v == "data" {
			out["x-delivery"] = "text"
		} else {
			out["x-delivery"] = "native"
		}
	}
	if v, has := spec["formats"]; has {
		out["x-formats"] = v
	}
	soft := map[string]any{}
	for _, k := range []string{"length", "count"} {
		if v, has := spec[k]; has {
			soft[k] = plain(v)
		}
	}
	if len(soft) > 0 {
		out["x-soft"] = soft
	}
	return out
}

// unprojectField is the reverse of projectField, for showing a stored schema as DSL.
func unprojectField(prop map[string]any, required bool) map[string]any {
	spec := map[string]any{"type": "string"}
	if t, ok := prop["type"].(string); ok {
		spec["type"] = t
	}
	_, hasMedia := prop["x-media"]
	_, hasFormats := prop["x-formats"]
	if hasMedia || hasFormats {
		spec["type"] = "file"
	}
	if required {
		spec["required"] = true
	}
	for _, k := range []string{"description", "enum", "minimum", "maximum", "format", "pattern"} {
		if v, has := prop[k]; has {
			spec[k] = v
		}
	}
	if v, has := prop["minItems"]; has {
		spec["min_items"] = v
	}
	if v, has := prop["maxItems"]; has {
		spec["max_items"] = v
	}
	if im, ok := asMap(prop["items"]); ok {
		spec["items"] = unprojectField(im, false)
	}
	if pm, ok := asMap(prop["properties"]); ok {
		req := map[string]bool{}
		for _, r := range anyStrings(prop["required"]) {
			req[r] = true
		}
		fields := map[string]any{}
		for n, p := range pm {
			if sub, ok := asMap(p); ok {
				fields[n] = unprojectField(sub, req[n])
			}
		}
		spec["fields"] = fields
	}
	if v, has := prop["x-media"]; has {
		spec["media"] = v
	}
	if v, has := prop["x-formats"]; has {
		spec["formats"] = v
	}
	if soft, ok := asMap(prop["x-soft"]); ok {
		for k, v := range soft {
			spec[k] = v
		}
	}
	return spec
}

func anyStrings(v any) []string {
	switch list := v.(type) {
	case []string:
		return list
	case []any:
		out := make([]string, 0, len(list))
		for _, x := range list {
			if s, ok := x.(string); ok {
				out = append(out, s)
			}
		}
		return out
	}
	return nil
}

// fieldSet is an ordered set of DSL-form fields (the order they were added in).
type fieldSet struct {
	names []string
	specs map[string]map[string]any
}

func newFieldSet() *fieldSet { return &fieldSet{specs: map[string]map[string]any{}} }

func (f *fieldSet) has(name string) bool { _, ok := f.specs[name]; return ok }

func (f *fieldSet) set(name string, spec map[string]any) {
	if !f.has(name) {
		f.names = append(f.names, name)
	}
	f.specs[name] = spec
}

func (f *fieldSet) remove(name string) {
	delete(f.specs, name)
	kept := make([]string, 0, len(f.names))
	for _, n := range f.names {
		if n != name {
			kept = append(kept, n)
		}
	}
	f.names = kept
}

func (f *fieldSet) copy() *fieldSet {
	out := newFieldSet()
	for _, n := range f.names {
		out.set(n, f.specs[n])
	}
	return out
}

// jsonSchema is the stored schema for the set: {"type": "object", "properties": …, "required": […]}.
func (f *fieldSet) jsonSchema() map[string]any {
	props := NewOrderedMap()
	var req []string
	for _, n := range f.names {
		props.Set(n, projectField(f.specs[n]))
		if r, _ := f.specs[n]["required"].(bool); r {
			req = append(req, n)
		}
	}
	out := map[string]any{"type": "object", "properties": props}
	if len(req) > 0 {
		out["required"] = req
	}
	return out
}

// dsl is the set in the definition's field form, in order, for the snapshot.
func (f *fieldSet) dsl() *OrderedMap {
	out := NewOrderedMap()
	for _, n := range f.names {
		out.Set(n, f.specs[n])
	}
	return out
}

// schemaFieldSet reads a stored JSON-schema object back as DSL-form fields.
func schemaFieldSet(schema map[string]any) *fieldSet {
	out := newFieldSet()
	if schema == nil {
		return out
	}
	req := map[string]bool{}
	for _, r := range anyStrings(schema["required"]) {
		req[r] = true
	}
	keys, vals, ok := entries(schema["properties"])
	if !ok {
		return out
	}
	for _, name := range keys {
		prop, isMap := asMap(vals[name])
		if !isMap {
			continue
		}
		// older SDK versions stored `required` inside the property itself
		legacy, _ := prop["required"].(bool)
		out.set(name, unprojectField(prop, req[name] || legacy))
	}
	return out
}

// ── tool options, guards, examples ───────────────────────────────────────────

const (
	guardTool = `[a-z_][a-z0-9_]*`
	guardKey  = `[a-zA-Z_][a-zA-Z0-9_]*`
)

var guardTermRe = regexp.MustCompile(`^\s*(` + `(?:` + guardTool + `\.(?:called|count|returned(?:\.` + guardKey + `)+)|input(?:\.` + guardKey + `)+)` +
	`)\s*(==|!=|>=|<=|>|<)\s*(true|false|-?\d+\.\d+|-?\d+|"[^"]*")\s*$`)

// guardProblems checks a tool's `requires` guard against the definition's grammar: terms joined by &&,
// each `ref op literal`. With tools/inputs given (non-nil), also checks every reference points at
// something this function declares.
func guardProblems(tool, requires string, tools, inputs map[string]bool) []string {
	var problems []string
	for _, raw := range strings.Split(requires, "&&") {
		term := strings.TrimSpace(raw)
		m := guardTermRe.FindStringSubmatch(term)
		if m == nil {
			problems = append(problems, fmt.Sprintf("tool '%s': guard term %q does not parse — use `<tool>.called|count|returned.<field>` or `input.<field>`, compared with == != > >= < <= to true/false, a number or \"text\"; join terms with &&", tool, term))
			continue
		}
		ref := m[1]
		head := strings.SplitN(ref, ".", 2)[0]
		if head == "input" {
			name := strings.Split(ref, ".")[1]
			if inputs != nil && !inputs[name] {
				problems = append(problems, fmt.Sprintf("tool '%s': guard uses input '%s', which this function doesn't declare", tool, name))
			}
		} else if tools != nil && !tools[head] {
			problems = append(problems, fmt.Sprintf("tool '%s': guard uses tool '%s', which this function doesn't declare", tool, head))
		}
	}
	return problems
}

// exampleProblems applies the definition's rules for one example: a positive one needs input and
// output; a negative one needs output (or bad value) and a note.
func exampleProblems(polarity string, input, output map[string]any, badValue any, note string) []string {
	if polarity != "positive" && polarity != "negative" {
		return []string{fmt.Sprintf("example: polarity must be 'positive' or 'negative', got %s", show(polarity))}
	}
	var problems []string
	if polarity == "positive" {
		if input == nil || output == nil {
			problems = append(problems, "a positive example needs both input and output")
		}
	} else {
		if output == nil && badValue == nil {
			problems = append(problems, "a negative example needs output or bad value")
		}
		if strings.TrimSpace(note) == "" {
			problems = append(problems, "a negative example needs a note saying what is wrong with it")
		}
	}
	return problems
}

// ── testing a sample input ───────────────────────────────────────────────────

var formatRe = map[string]*regexp.Regexp{
	"email":    regexp.MustCompile(`^[^@\s]+@[^@\s]+\.[^@\s]+$`),
	"uuid":     regexp.MustCompile(`^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$`),
	"url":      regexp.MustCompile(`^https?://[^\s/$.?#][^\s]*$`),
	"date":     regexp.MustCompile(`^\d{4}-\d{2}-\d{2}$`),
	"time":     regexp.MustCompile(`^\d{2}:\d{2}(:\d{2}(\.\d+)?)?$`),
	"datetime": regexp.MustCompile(`^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$`),
	"iso8601":  regexp.MustCompile(`^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$`),
}

func goType(v any) string {
	switch v.(type) {
	case nil:
		return "null"
	case bool:
		return "boolean"
	case string:
		return "string"
	case []any, []string:
		return "array"
	}
	if isNum(v) {
		return "number"
	}
	if _, _, ok := entries(v); ok {
		return "object"
	}
	return fmt.Sprintf("%T", v)
}

// validateSample checks a sample against a stored input schema, the way the server checks a real call:
// required, type, minimum/maximum, enum, pattern, format and item counts.
func validateSample(schema map[string]any, sample map[string]any) []string {
	var problems []string
	props, _ := asMap(schema["properties"])
	for _, name := range anyStrings(schema["required"]) {
		if _, has := sample[name]; !has {
			problems = append(problems, fmt.Sprintf("missing required field '%s'", name))
		}
	}
	var check func(label string, spec map[string]any, value any)
	check = func(label string, spec map[string]any, value any) {
		if value == nil {
			return
		}
		t, _ := spec["type"].(string)
		ok := true
		switch t {
		case "boolean":
			_, ok = value.(bool)
		case "number":
			ok = isNum(value)
		case "integer":
			ok = isInt(value)
		case "string":
			_, ok = value.(string)
		case "object":
			_, _, ok = entries(value)
		case "array":
			ok = goType(value) == "array"
		}
		if !ok {
			problems = append(problems, fmt.Sprintf("field '%s' must be a %s, got %s", label, t, goType(value)))
			return
		}
		if f, isNumber := toFloat(value); isNumber && goType(value) == "number" {
			if lo, has := toFloat(spec["minimum"]); has && f < lo {
				problems = append(problems, fmt.Sprintf("field '%s' must be at least %s, got %s", label, numStr(lo), numStr(f)))
			}
			if hi, has := toFloat(spec["maximum"]); has && f > hi {
				problems = append(problems, fmt.Sprintf("field '%s' must be at most %s, got %s", label, numStr(hi), numStr(f)))
			}
		}
		if list, isList := spec["enum"].([]any); isList {
			found := false
			for _, e := range list {
				if fmt.Sprint(e) == fmt.Sprint(value) && goType(e) == goType(value) {
					found = true
				}
			}
			if !found {
				b, _ := json.Marshal(list)
				problems = append(problems, fmt.Sprintf("field '%s' must be one of %s, got %s", label, b, show(value)))
			}
		}
		if s, isStr := value.(string); isStr {
			if p, has := spec["pattern"].(string); has {
				if re, err := regexp.Compile(p); err == nil && !re.MatchString(s) {
					problems = append(problems, fmt.Sprintf("field '%s' does not match the required pattern", label))
				}
			}
			if f, has := spec["format"].(string); has {
				if re := formatRe[f]; re != nil && !re.MatchString(s) {
					problems = append(problems, fmt.Sprintf("field '%s' is not a valid %s", label, f))
				}
			}
		}
		if list, isList := value.([]any); isList {
			if lo, has := toFloat(spec["minItems"]); has && float64(len(list)) < lo {
				problems = append(problems, fmt.Sprintf("field '%s' must have at least %s items, got %d", label, numStr(lo), len(list)))
			}
			if hi, has := toFloat(spec["maxItems"]); has && float64(len(list)) > hi {
				problems = append(problems, fmt.Sprintf("field '%s' must have at most %s items, got %d", label, numStr(hi), len(list)))
			}
			if items, ok := asMap(spec["items"]); ok {
				for i, v := range list {
					check(fmt.Sprintf("%s[%d]", label, i), items, v)
				}
			}
		}
	}
	for _, name := range sortedKeys(sample) {
		if spec, ok := asMap(props[name]); ok {
			check(name, spec, sample[name])
		}
	}
	return problems
}

// ── printing a function as YAML ──────────────────────────────────────────────

var plainYaml = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_ .-]*$`)

func yamlScalar(v any) string {
	switch x := v.(type) {
	case rawYaml:
		return string(x)
	case nil:
		return "null"
	case bool:
		if x {
			return "true"
		}
		return "false"
	case string:
		switch strings.ToLower(x) {
		case "true", "false", "null", "yes", "no", "on", "off", "~":
			b, _ := json.Marshal(x)
			return string(b)
		}
		if plainYaml.MatchString(x) {
			return x
		}
		b, _ := json.Marshal(x)
		return strings.NewReplacer(`<`, "<", `>`, ">", `&`, "&").Replace(string(b))
	}
	if f, ok := toFloat(v); ok {
		return numStr(f)
	}
	return yamlScalar(fmt.Sprint(v))
}

func yamlFlow(v any) string { return yamlFlowOrdered(v, nil) }

// yamlFlowOrdered writes a value on one line. keyOrder (optional) puts those keys first, in that order.
func yamlFlowOrdered(v any, keyOrder []string) string {
	if keys, vals, ok := entries(v); ok {
		if len(keys) == 0 {
			return "{}"
		}
		ordered := keys
		if keyOrder != nil {
			ordered = nil
			seen := map[string]bool{}
			for _, k := range keyOrder {
				if _, has := vals[k]; has {
					ordered = append(ordered, k)
					seen[k] = true
				}
			}
			for _, k := range keys {
				if !seen[k] {
					ordered = append(ordered, k)
				}
			}
		}
		parts := make([]string, 0, len(ordered))
		for _, k := range ordered {
			parts = append(parts, k+": "+yamlFlowOrdered(vals[k], nil))
		}
		return "{ " + strings.Join(parts, ", ") + " }"
	}
	switch list := v.(type) {
	case []any:
		parts := make([]string, len(list))
		for i, x := range list {
			parts[i] = yamlFlowOrdered(x, nil)
		}
		return "[" + strings.Join(parts, ", ") + "]"
	case []string:
		parts := make([]string, len(list))
		for i, x := range list {
			parts[i] = yamlScalar(x)
		}
		return "[" + strings.Join(parts, ", ") + "]"
	case []map[string]any:
		parts := make([]string, len(list))
		for i, x := range list {
			parts[i] = yamlFlowOrdered(x, nil)
		}
		return "[" + strings.Join(parts, ", ") + "]"
	}
	return yamlScalar(v)
}

// yamlSpec writes one DSL field on one line, with its keys in the standard order.
func yamlSpec(spec map[string]any) string {
	clone := map[string]any{}
	for k, v := range spec {
		switch k {
		case "items":
			if im, ok := asMap(v); ok {
				clone[k] = rawYaml(yamlSpec(im))
				continue
			}
		case "fields":
			if keys, vals, ok := entries(v); ok {
				parts := make([]string, 0, len(keys))
				for _, n := range keys {
					sm, _ := asMap(vals[n])
					parts = append(parts, n+": "+yamlSpec(sm))
				}
				clone[k] = rawYaml("{ " + strings.Join(parts, ", ") + " }")
				continue
			}
		}
		clone[k] = v
	}
	return yamlFlowOrdered(clone, specKeyOrder)
}

// rawYaml is text that is already YAML and must be written as-is.
type rawYaml string

// YamlParts is everything a printed function shows.
type yamlParts struct {
	name, description string
	input, output     *fieldSet
	mode              string
	flow              []string
	rubric            []string
	memory            *OrderedMap
	sections          *OrderedMap // nil = don't show an implementation block
	tools             []map[string]any
	extraTop          map[string]any // needs, ... — parts the builder has no call for
	extraImpl         map[string]any // preprocessors, ...
}

var (
	managedTop  = []string{"name", "description", "input", "output", "implementation", "rubric", "memory", "meta"}
	managedImpl = []string{"execution", "flow", "sections", "tools"}
)

// extrasOf returns the parts of a saved version the builder has no call for — needs, preprocessors and
// anything else it does not manage — so they travel with the function instead of being dropped.
func extrasOf(snap map[string]any) (top, impl map[string]any) {
	top, impl = map[string]any{}, map[string]any{}
	for k, v := range snap {
		if !contains2(managedTop, k) {
			top[k] = v
		}
	}
	if im, ok := asMap(snap["implementation"]); ok {
		for k, v := range im {
			if !contains2(managedImpl, k) {
				impl[k] = v
			}
		}
	}
	return top, impl
}

func yamlExtras(lines *[]string, extras map[string]any, indent string) {
	for _, key := range sortedKeys(extras) {
		value := extras[key]
		if list, ok := value.([]any); ok && len(list) > 0 {
			allMaps := true
			for _, x := range list {
				if _, _, isMap := entries(x); !isMap {
					allMaps = false
				}
			}
			if allMaps {
				*lines = append(*lines, indent+key+":")
				for _, x := range list {
					*lines = append(*lines, indent+"  - "+yamlFlowOrdered(x, []string{"tool", "from", "into"}))
				}
				continue
			}
		}
		*lines = append(*lines, indent+key+": "+yamlFlow(value))
	}
}

func jsonString(s string) string {
	b, _ := json.Marshal(s)
	return strings.NewReplacer(`<`, "<", `>`, ">", `&`, "&").Replace(string(b))
}

func renderYaml(p yamlParts) string {
	lines := []string{"name: " + yamlScalar(p.name)}
	if p.description != "" {
		lines = append(lines, "description: "+yamlScalar(p.description))
	}
	for _, sec := range []struct {
		label  string
		fields *fieldSet
	}{{"input", p.input}, {"output", p.output}} {
		if len(sec.fields.names) == 0 {
			lines = append(lines, sec.label+": {}")
			continue
		}
		lines = append(lines, sec.label+":")
		width := 0
		for _, n := range sec.fields.names {
			width = max(width, len(n))
		}
		width++
		for _, n := range sec.fields.names {
			lines = append(lines, "  "+padRight(n+":", width)+" "+yamlSpec(sec.fields.specs[n]))
		}
	}
	if p.sections != nil {
		mode := p.mode
		if mode == "" {
			mode = "single_shot"
		}
		lines = append(lines, "implementation:", "  execution: { mode: "+mode+" }")
		yamlExtras(&lines, p.extraImpl, "  ")
		if len(p.flow) > 0 {
			lines = append(lines, "  flow:")
			for _, x := range p.flow {
				lines = append(lines, "    - "+jsonString(x))
			}
		}
		var body []string
		if role, _ := p.sections.Values["role"].(string); role != "" {
			body = append(body, "    role: "+jsonString(role))
		}
		for _, key := range []string{"definitions", "constraints", "edge_cases"} {
			if list := anyStrings(p.sections.Values[key]); len(list) > 0 {
				body = append(body, "    "+key+":")
				for _, x := range list {
					body = append(body, "      - "+jsonString(x))
				}
			}
		}
		if ex, _ := p.sections.Values["examples"].([]map[string]any); len(ex) > 0 {
			body = append(body, "    examples:")
			for _, x := range ex {
				body = append(body, "      - "+yamlFlowOrdered(x, []string{"polarity", "input", "output", "bad_value", "note"}))
			}
		}
		if len(body) > 0 {
			lines = append(lines, "  sections:")
			lines = append(lines, body...)
		}
		if len(p.tools) > 0 {
			lines = append(lines, "  tools:")
			for _, t := range p.tools {
				lines = append(lines, "    - "+yamlFlowOrdered(t, []string{"name", "config", "max_calls", "requires", "effect", "ref"}))
			}
		}
	}
	if len(p.rubric) > 0 {
		lines = append(lines, "rubric:")
		for _, x := range p.rubric {
			lines = append(lines, "  - "+jsonString(x))
		}
	}
	if p.memory != nil && len(p.memory.Keys) > 0 {
		lines = append(lines, "memory:")
		for _, k := range p.memory.Keys {
			lines = append(lines, "  "+k+": "+yamlFlow(p.memory.Values[k]))
		}
	}
	yamlExtras(&lines, p.extraTop, "")
	return strings.Join(lines, "\n") + "\n"
}

func padRight(s string, width int) string {
	if len(s) >= width {
		return s
	}
	return s + strings.Repeat(" ", width-len(s))
}

// Missing says what a function still needs: Required (the server needs it) and Recommended.
type Missing struct {
	Required    []string
	Recommended []string
}

func missingLists(inputs, outputs *fieldSet, flow []string, sections *OrderedMap) Missing {
	var m Missing
	if len(outputs.names) == 0 {
		m.Required = append(m.Required, "no output fields — AddOutput(...)")
	} else {
		any := false
		for _, n := range outputs.names {
			if r, _ := outputs.specs[n]["required"].(bool); r {
				any = true
			}
		}
		if !any {
			m.Required = append(m.Required, "no output field is marked required (\"required\": true) — the function has no valid stopping state")
		}
	}
	if len(flow) == 0 {
		m.Required = append(m.Required, "no steps — AddStep(...) (the server needs at least one)")
	}
	if role, _ := sections.Values["role"].(string); role == "" {
		m.Recommended = append(m.Recommended, "no prompt — AddPrompt(...)")
	}
	if len(inputs.names) == 0 {
		m.Recommended = append(m.Recommended, "no input fields — AddInput(...)")
	}
	if ex, _ := sections.Values["examples"].([]map[string]any); len(ex) == 0 {
		m.Recommended = append(m.Recommended, "no examples — AddExample(...) (they improve accuracy)")
	}
	if len(anyStrings(sections.Values["constraints"])) == 0 && len(anyStrings(sections.Values["edge_cases"])) == 0 {
		m.Recommended = append(m.Recommended, "no constraints or edge cases — AddSection(\"constraints\", ...)")
	}
	return m
}

// describe is the printed form: the YAML, then what is missing and what is recommended.
func describe(yaml string, m Missing) string {
	out := yaml + "\n"
	if len(m.Required) > 0 {
		out += "# Missing (required):\n"
		for _, i := range m.Required {
			out += "#  - " + i + "\n"
		}
	} else {
		out += "# Missing (required): nothing\n"
	}
	if len(m.Recommended) > 0 {
		out += "# Recommended:\n"
		for _, i := range m.Recommended {
			out += "#  - " + i + "\n"
		}
	}
	return out
}

// ── comparing experiments (what the Comparisons screen shows) ────────────────

var dimensions = [][2]string{
	{"outcome_quality", "Outcome quality"}, {"cost", "Cost & latency"}, {"safety", "Safety"},
	{"stability", "Stability"}, {"robustness", "Robustness"},
}

type trust struct {
	dims    map[string]*float64
	score   int
	passPct *float64
	checks  map[string]float64
}

// trustOf computes the trust figures for one experiment (mirrors the app): per-dimension pass rate, their
// mean as a 0-100 trust score, and the overall check pass rate. Pooled across models.
func trustOf(results map[string]any) trust {
	t := trust{dims: map[string]*float64{}, checks: map[string]float64{}}
	var checks []map[string]any
	if list, ok := results["checks"].([]any); ok {
		for _, c := range list {
			if cm, ok := asMap(c); ok {
				checks = append(checks, cm)
			}
		}
	}
	cons, _ := asMap(results["consistency"])
	for _, d := range dimensions {
		key := d[0]
		var sum float64
		n := 0
		for _, c := range checks {
			dim, _ := c["dimension"].(string)
			if dim == "" {
				dim = "outcome_quality"
			}
			if dim == key {
				if pr, ok := toFloat(c["pass_rate"]); ok {
					sum += pr
					n++
				}
			}
		}
		var score *float64
		if n > 0 {
			s := sum / float64(n)
			score = &s
		}
		if avail, _ := cons["available"].(bool); key == "stability" && avail {
			overall, _ := toFloat(cons["overall"])
			c := overall / 100
			if score == nil {
				score = &c
			} else {
				s := (*score + c) / 2
				score = &s
			}
		}
		if key == "cost" && score == nil {
			one := 1.0
			score = &one
		}
		t.dims[key] = score
	}
	var total float64
	count := 0
	for _, s := range t.dims {
		if s != nil {
			total += *s
			count++
		}
	}
	if count > 0 {
		t.score = int(math.Floor(total/float64(count)*100 + 0.5))
	}
	if len(checks) > 0 {
		var sum float64
		for _, c := range checks {
			pr, _ := toFloat(c["pass_rate"])
			sum += pr
			name, _ := c["check_name"].(string)
			t.checks[name] = pr * 100
		}
		p := math.Floor(sum/float64(len(checks))*100 + 0.5)
		t.passPct = &p
	}
	return t
}

// ComparisonRow is one metric across the compared experiments: Values (one per experiment, in order;
// nil when not measured) and Best, the index of the winner (nil when fewer than two values differ).
type ComparisonRow struct {
	Metric string
	Values []*float64
	Best   *int
}

func comparisonRow(metric string, values []*float64, higherBetter bool) ComparisonRow {
	row := ComparisonRow{Metric: metric, Values: values}
	var defined []float64
	for _, v := range values {
		if v != nil {
			defined = append(defined, *v)
		}
	}
	if len(defined) > 1 {
		lo, hi := defined[0], defined[0]
		for _, d := range defined {
			lo, hi = math.Min(lo, d), math.Max(hi, d)
		}
		if lo != hi {
			target := hi
			if !higherBetter {
				target = lo
			}
			for i, v := range values {
				if v != nil && *v == target {
					idx := i
					row.Best = &idx
					break
				}
			}
		}
	}
	return row
}
