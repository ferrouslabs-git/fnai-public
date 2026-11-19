package fnai

// FNA-REQ-531: fetch a prompt version, fill it in on your own computer.
//
//	f := fnai.NewFunction("ticket_summary", os.Getenv("FNAI_API_KEY"))
//	p, err := f.Prompt("live")                         // or "3" for version 3
//	text := p.Render(map[string]any{"ticket_text": ticket})   // filled in here; nothing is sent to us
//	// call your own model with p.Model and p.Temperature; put p.VersionID in the trace you report
//
// "Live" is whatever was last deployed in the app (rollback included), so a change there reaches your code on its next
// fetch with no redeploy of yours. Nothing runs on Functional AI's side.

import (
	"fmt"
	"net/url"
	"regexp"
	"strings"
	"sync"
	"time"
)

var promptVar = regexp.MustCompile(`\{([a-z_][a-z0-9_]*)\}`) // the server's rule: {name}; anything else stays as written

// PromptCacheTTL is how long a fetched prompt is kept (default 60 s; set to 0 to always ask). If Functional AI can't
// be reached, the last copy is used.
var PromptCacheTTL = 60 * time.Second

// Prompt is one prompt version: its text, how to fill it in, and the model and settings it is marked live with.
type Prompt struct {
	Function     string `json:"function"`
	Name         string `json:"name"`
	Version      int    `json:"version"`
	VersionID    int    `json:"version_id"` // what a trace names to link back to this version
	Live         bool   `json:"live"`
	ArtifactHash string `json:"artifact_hash"`
	Template     string `json:"template"`
	Substitution struct {
		Syntax string   `json:"syntax"`
		Vars   []string `json:"vars"`
	} `json:"substitution"`
	InputSchema  map[string]any `json:"input_schema"`
	OutputSchema map[string]any `json:"output_schema"`
	Model        string         `json:"model"` // empty: nothing names a model for it
	Settings     struct {
		Temperature *float64 `json:"temperature"`
	} `json:"settings"`
}

// Temperature is the marked-live temperature, or nil when none is set.
func (p *Prompt) Temperature() *float64 { return p.Settings.Temperature }

// Render fills in the {variables}: each {name} becomes fmt.Sprint(value). A name you gave no value for is left as
// written, exactly as Functional AI's own renderer does.
func (p *Prompt) Render(values map[string]any) string {
	return promptVar.ReplaceAllStringFunc(p.Template, func(whole string) string {
		name := promptVar.FindStringSubmatch(whole)[1]
		if v, ok := values[name]; ok {
			return fmt.Sprint(v)
		}
		return whole
	})
}

type promptHit struct {
	at time.Time
	p  *Prompt
}

var (
	promptMu    sync.Mutex
	promptCache = map[string]promptHit{}
)

// Prompt fetches a prompt version: "live" or a version number as text ("3"). Needs the function's invoke key.
func (f *Function) Prompt(version string) (*Prompt, error) {
	if version == "" {
		version = "live"
	}
	ck := f.h.base + "|" + f.h.token + "|" + f.Slug + "|" + version
	promptMu.Lock()
	hit, have := promptCache[ck]
	promptMu.Unlock()
	if have && PromptCacheTTL > 0 && time.Since(hit.at) < PromptCacheTTL {
		return hit.p, nil
	}
	var out Prompt
	err := f.h.do("GET", "/v1/prompts/"+url.PathEscape(f.Slug), nil, url.Values{"version": {version}}, &out)
	if err != nil {
		if e, ok := err.(*Error); ok && have && (e.Status == 0 || e.Status >= 500) {
			return hit.p, nil // unreachable or struggling: the copy from earlier
		}
		return nil, err
	}
	if strings.TrimSpace(out.Template) == "" && out.VersionID == 0 {
		return nil, fmt.Errorf("unexpected answer from the prompts route")
	}
	promptMu.Lock()
	promptCache[ck] = promptHit{time.Now(), &out}
	promptMu.Unlock()
	return &out, nil
}
