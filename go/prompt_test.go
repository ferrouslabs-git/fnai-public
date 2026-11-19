package fnai

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

// FNA-REQ-531: Function.Prompt and Prompt.Render.
const promptJSON = `{"function":"ticket_summary","name":"Ticket summary","version":3,"version_id":103,"live":true,
"artifact_hash":"abc","template":"Summarise in one sentence: {ticket_text}. Tone: {tone}. Keep {unknown_left} as is.",
"substitution":{"syntax":"curly_lowercase_v1","vars":["ticket_text","tone","unknown_left"]},
"input_schema":{"type":"object"},"output_schema":{"type":"object"},"model":"gpt-4o-mini","settings":{"temperature":0.2}}`

type promptFake struct {
	mu       sync.Mutex
	requests []string
	auth     string
	statuses []int // answer with these in order, then the last repeats; 200 serves the prompt
}

func promptServer(t *testing.T, f *promptFake) *Function {
	t.Helper()
	promptCache = map[string]promptHit{}
	PromptCacheTTL = 60 * time.Second
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		f.mu.Lock()
		f.requests = append(f.requests, r.URL.String())
		f.auth = r.Header.Get("Authorization")
		i := len(f.requests) - 1
		if i >= len(f.statuses) {
			i = len(f.statuses) - 1
		}
		status := f.statuses[i]
		f.mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		if status == 200 {
			w.Write([]byte(promptJSON))
		} else {
			w.Write([]byte(`{"detail":"nope"}`))
		}
	}))
	t.Cleanup(srv.Close)
	return NewFunction("ticket_summary", "fnk_test", WithBaseURL(srv.URL))
}

func TestPromptFetchesLiveWithTheKeyAndExposesItsParts(t *testing.T) {
	f := &promptFake{statuses: []int{200}}
	p, err := promptServer(t, f).Prompt("live")
	if err != nil {
		t.Fatal(err)
	}
	if f.requests[0] != "/v1/prompts/ticket_summary?version=live" || f.auth != "Bearer fnk_test" {
		t.Fatalf("bad request %v / %q", f.requests, f.auth)
	}
	if p.Version != 3 || p.VersionID != 103 || !p.Live || p.Model != "gpt-4o-mini" || p.Temperature() == nil || *p.Temperature() != 0.2 {
		t.Fatalf("bad prompt: %+v", p)
	}
}

func TestPromptRenderFillsBlanksAndLeavesAMissingNameAsWritten(t *testing.T) {
	f := &promptFake{statuses: []int{200}}
	p, _ := promptServer(t, f).Prompt("")
	got := p.Render(map[string]any{"ticket_text": "Printer on fire", "tone": "calm"})
	want := "Summarise in one sentence: Printer on fire. Tone: calm. Keep {unknown_left} as is."
	if got != want {
		t.Fatalf("got %q", got)
	}
	if !strings.Contains(p.Render(map[string]any{"ticket_text": 5, "tone": "x"}), "5") {
		t.Fatal("a number should be written as text")
	}
}

func TestPromptIsKeptForAWhileAndAStruggingServerGivesTheLastCopy(t *testing.T) {
	f := &promptFake{statuses: []int{200, 503, 404}}
	fn := promptServer(t, f)
	first, _ := fn.Prompt("live")
	if again, _ := fn.Prompt("live"); again != first || len(f.requests) != 1 {
		t.Fatalf("should be cached, %d requests", len(f.requests))
	}
	PromptCacheTTL = 0
	if again, err := fn.Prompt("live"); err != nil || again != first { // 503: the copy from earlier
		t.Fatalf("expected the earlier copy, got %v %v", again, err)
	}
	if _, err := fn.Prompt("live"); err == nil { // 404: say so
		t.Fatal("a 404 must be an error")
	}
}
