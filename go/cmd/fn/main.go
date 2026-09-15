// Command fn is functionalAI's command-line tool (REQ-55): init, new, run,
// eval, deploy, pin, diff, rollback, ls, logs.
//
// A single static binary — build with CGO disabled so it has no runtime
// dependencies:
//
//	CGO_ENABLED=0 go build -o fn ./cmd/fn
//
// Two open questions from the ticket, decided here rather than left open,
// with the reasoning written down so they're easy to revisit:
//
//   - Language: Go. The ticket asks for a static binary; Go compiles to
//     exactly that with no runtime to install, and this repo already has a
//     hand-maintained Go SDK (sdk/go/fnai.go) this command imports directly
//     rather than re-implementing HTTP/auth from scratch.
//   - Auth: a personal access token (fnp_*) for every authoring command
//     (new/ls/deploy/pin/diff/rollback/eval/logs) — the same mechanism the
//     Python/TS/Go SDKs and the MCP server already use, not a new auth
//     system. `fn login` stores it in ~/.fnai/config.json. `fn run` invokes
//     a *deployed* function, which is a different, narrower credential
//     (fnk_* invoke keys only cover invoke, per the ticket's own note) —
//     passed via --key or FNAI_INVOKE_KEY, never stored.
//
// Not decided here, deliberately: how someone actually GETS this binary
// (a release pipeline — GitHub Releases, Homebrew, etc.) is a separate
// distribution decision this command doesn't make. Build it locally with
// the command above until that's settled.
package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"

	fnai "github.com/ferrouslabs-git/fnai-public/go"
)

// newFlagSet builds a flag.FlagSet that fails the way every other error in
// this command does — a readable message and a nonzero exit, never flag's
// own default of printing usage straight to stderr and calling os.Exit(2)
// out from under main's error handling.
func newFlagSet(name string) *flag.FlagSet {
	fs := flag.NewFlagSet(name, flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	return fs
}

const starterYAML = `name: Summarize Support Ticket
description: Produces a one-paragraph summary and urgency score for a support ticket.

input:
  ticket_text: { type: string, required: true }

output:
  summary: { type: string, required: true }
  urgency: { type: integer, required: true, minimum: 1, maximum: 5 }

implementation:
  execution:
    mode: single_shot
  flow:
    - "Summarize the ticket in one paragraph, plain language."
    - "Score urgency from the ticket's severity."
  sections:
    role: You are a support-ticket triage assistant.
`

// ── config: ~/.fnai/config.json (token, base_url) + a per-directory pin ─────

type config struct {
	Token   string `json:"token"`
	BaseURL string `json:"base_url,omitempty"`
}

func configPath() (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", fmt.Errorf("can't find your home directory: %w", err)
	}
	return filepath.Join(home, ".fnai", "config.json"), nil
}

func loadConfig() (*config, error) {
	p, err := configPath()
	if err != nil {
		return nil, err
	}
	b, err := os.ReadFile(p)
	if os.IsNotExist(err) {
		return &config{}, nil
	}
	if err != nil {
		return nil, err
	}
	var c config
	if err := json.Unmarshal(b, &c); err != nil {
		return nil, fmt.Errorf("%s is corrupt (%w) — run `fn login` again", p, err)
	}
	return &c, nil
}

func saveConfig(c *config) error {
	p, err := configPath()
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(p), 0o700); err != nil {
		return err
	}
	b, err := json.MarshalIndent(c, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(p, b, 0o600) // token inside — not group/world readable
}

// pinPath is per-working-directory, mirroring how a version control tool
// scopes a checked-out ref to the folder you're standing in — not a global
// setting, and not a new backend concept (REQ-55's own spec leaves "what
// pin operates on" undecided; this keeps it local rather than inventing new
// server-side state for a CLI convenience).
func pinPath() string { return filepath.Join(".fnai", "pin.json") }

type pin struct {
	FunctionID int `json:"function_id"`
	VersionID  int `json:"version_id"`
	Number     int `json:"number"`
}

func loadPin() (*pin, error) {
	b, err := os.ReadFile(pinPath())
	if os.IsNotExist(err) {
		return nil, fmt.Errorf("nothing pinned here yet — run `fn pin <function-id> <version-id>` first")
	}
	if err != nil {
		return nil, err
	}
	var p pin
	return &p, json.Unmarshal(b, &p)
}

func savePin(p *pin) error {
	if err := os.MkdirAll(filepath.Dir(pinPath()), 0o755); err != nil {
		return err
	}
	b, _ := json.MarshalIndent(p, "", "  ")
	return os.WriteFile(pinPath(), b, 0o644)
}

// ── entry point — every error prints as one readable line, never a stack trace ──

func main() {
	if err := run(os.Args[1:]); err != nil {
		fmt.Fprintln(os.Stderr, "Error:", friendly(err))
		os.Exit(1)
	}
}

// friendly turns the SDK's *fnai.Error into what it actually means to a
// human at a keyboard — "run fn eval exits nonzero, readable messages, not
// stack traces" is an explicit acceptance criterion.
func friendly(err error) string {
	if e, ok := err.(*fnai.Error); ok {
		switch e.Status {
		case 0:
			return fmt.Sprintf("can't reach the server — %s", e.Detail)
		case 401:
			return "not signed in, or your token expired — run `fn login`"
		case 403:
			return "you don't have permission to do that"
		case 404:
			return "not found — check the id/slug"
		case 422, 409:
			return e.Detail
		}
	}
	return err.Error()
}

func run(args []string) error {
	if len(args) == 0 {
		printUsage()
		return nil
	}
	cmd, rest := args[0], args[1:]
	switch cmd {
	case "help", "-h", "--help":
		printUsage()
		return nil
	case "version", "--version":
		fmt.Println("fn", fnai.Version)
		return nil
	case "login":
		return cmdLogin(rest)
	case "init":
		return cmdInit(rest)
	case "new":
		return cmdNew(rest)
	case "ls":
		return cmdLs(rest)
	case "run":
		return cmdRun(rest)
	case "deploy":
		return cmdDeploy(rest)
	case "rollback":
		return cmdRollback(rest)
	case "pin":
		return cmdPin(rest)
	case "diff":
		return cmdDiff(rest)
	case "eval":
		return cmdEval(rest)
	case "logs":
		return cmdLogs(rest)
	default:
		printUsage()
		return fmt.Errorf("unknown command %q", cmd)
	}
}

func printUsage() {
	fmt.Println(`fn — the functionalAI command line

Setup
  fn login                     store your personal access token (fnp_...)

Author
  fn init [path]                write a working example function to a file (no login needed)
  fn new <path.yaml>             push a function file up as a brand-new function

Operate
  fn ls                          list your functions
  fn run <slug> --key <fnk_...> --input '{"...": "..."}'   invoke a deployed function
  fn eval <function-id>          run this function's checks; exits nonzero if any fail
  fn deploy <function-id> <version-id>   make a version the live deployment
  fn rollback <function-id>      roll back to the previous deployment
  fn pin <function-id> <version-id>      remember a version for this folder
  fn diff <function-id> <version-id-a> <version-id-b>   compare two versions
  fn logs <function-id> [--limit N]      recent production calls

Every command needs `+"`fn login`"+` first except init and version.`)
}

func client() (*fnai.Client, error) {
	c, err := loadConfig()
	if err != nil {
		return nil, err
	}
	token := os.Getenv("FNAI_TOKEN")
	if token == "" {
		token = c.Token
	}
	if token == "" {
		return nil, fmt.Errorf("not signed in — run `fn login` first (or set FNAI_TOKEN)")
	}
	var opts []fnai.Option
	base := os.Getenv("FNAI_BASE_URL")
	if base == "" {
		base = c.BaseURL
	}
	if base != "" {
		opts = append(opts, fnai.WithBaseURL(base))
	}
	return fnai.NewClient(token, opts...), nil
}

// ── fn login ──────────────────────────────────────────────────────────────

func cmdLogin(args []string) error {
	fs := newFlagSet("login")
	base := fs.String("base-url", "", "server URL (default: production)")
	token := fs.String("token", "", "paste your token instead of typing it interactively")
	if err := fs.Parse(args); err != nil {
		return err
	}
	t := *token
	if t == "" {
		fmt.Print("Personal access token (fnp_...): ")
		reader := bufio.NewReader(os.Stdin)
		line, err := reader.ReadString('\n')
		if err != nil && err != io.EOF {
			return err
		}
		t = strings.TrimSpace(line)
	}
	if !strings.HasPrefix(t, "fnp_") {
		return fmt.Errorf("that doesn't look like a personal access token — it should start with fnp_ " +
			"(mint one in the app under your avatar → API tokens)")
	}
	c := &config{Token: t, BaseURL: *base}
	if err := saveConfig(c); err != nil {
		return err
	}
	cl := fnai.NewClient(t, urlOpt(*base)...)
	me, err := cl.Me()
	if err != nil {
		return fmt.Errorf("saved the token, but it doesn't seem to work: %w", err)
	}
	fmt.Printf("Signed in as %v.\n", me["user_id"])
	return nil
}

func urlOpt(base string) []fnai.Option {
	if base == "" {
		return nil
	}
	return []fnai.Option{fnai.WithBaseURL(base)}
}

// ── fn init ───────────────────────────────────────────────────────────────

func cmdInit(args []string) error {
	path := "function.yaml"
	if len(args) > 0 {
		path = args[0]
	}
	if _, err := os.Stat(path); err == nil {
		return fmt.Errorf("%s already exists — pick a different path, or edit it directly", path)
	}
	if err := os.WriteFile(path, []byte(starterYAML), 0o644); err != nil {
		return err
	}
	fmt.Printf("Wrote %s. Edit it, then run: fn new %s\n", path, path)
	return nil
}

// ── fn new ────────────────────────────────────────────────────────────────

func cmdNew(args []string) error {
	fs := newFlagSet("new")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if fs.NArg() != 1 {
		return fmt.Errorf("usage: fn new <path.yaml>")
	}
	raw, err := os.ReadFile(fs.Arg(0))
	if err != nil {
		return err
	}
	c, err := client()
	if err != nil {
		return err
	}
	cfg, _ := loadConfig()
	// YAML is a superset of JSON; a hand-written .yaml file needs real YAML
	// parsing, which the zero-dependency SDK deliberately doesn't carry —
	// so `new` sends the file's bytes straight to the server, the one real
	// parser (same principle as REQ-272: no second parser here to drift).
	slug := slugFromFile(raw)
	fnRow, err := findOrCreateFunctionBySlug(c, slug, raw)
	if err != nil {
		return err
	}
	v, err := pushYAML(cfg, fmt.Sprintf("%v", fnRow["id"]), raw)
	if err != nil {
		return err
	}
	fmt.Printf("Created function %q (id %v), version %v.\n", fnRow["slug"], fnRow["id"], v["number"])
	return nil
}

func slugFromFile(raw []byte) string {
	// best-effort: pull `name:` out of the raw text without a YAML parser —
	// good enough to check for an existing function by slug before pushing;
	// the server is the source of truth on the real parsed name either way.
	for _, line := range strings.Split(string(raw), "\n") {
		line = strings.TrimSpace(line)
		if strings.HasPrefix(line, "name:") {
			name := strings.TrimSpace(strings.TrimPrefix(line, "name:"))
			name = strings.Trim(name, `"'`)
			return slugify(name)
		}
	}
	return ""
}

func slugify(name string) string {
	var b strings.Builder
	prevUnderscore := false
	for _, r := range strings.ToLower(name) {
		if r >= 'a' && r <= 'z' || r >= '0' && r <= '9' {
			b.WriteRune(r)
			prevUnderscore = false
		} else if !prevUnderscore {
			b.WriteByte('_')
			prevUnderscore = true
		}
	}
	s := strings.Trim(b.String(), "_")
	if s == "" {
		return "function"
	}
	return s
}

func findOrCreateFunctionBySlug(c *fnai.Client, slug string, raw []byte) (map[string]any, error) {
	fns, err := c.Functions.List()
	if err != nil {
		return nil, err
	}
	for _, f := range fns {
		if f.Slug == slug {
			return map[string]any{"id": f.ID, "slug": f.Slug}, nil
		}
	}
	name := slug
	for _, line := range strings.Split(string(raw), "\n") {
		line = strings.TrimSpace(line)
		if strings.HasPrefix(line, "name:") {
			name = strings.Trim(strings.TrimSpace(strings.TrimPrefix(line, "name:")), `"'`)
		}
	}
	created, err := c.Functions.Create(fnai.FunctionCreate{
		Slug: slug, Name: name,
		InputSchema: map[string]any{}, OutputSchema: map[string]any{},
	})
	if err != nil {
		return nil, err
	}
	return map[string]any{"id": created.ID, "slug": created.Slug}, nil
}

// pushYAML posts a function's raw YAML text (POST .../versions/yaml). The
// SDK's own transport always JSON-encodes its body, so this speaks
// text/yaml directly instead, over the same bearer auth — matching
// create_from_yaml_text in the Python/TS SDKs.
func pushYAML(cfg *config, fnID string, raw []byte) (map[string]any, error) {
	token := os.Getenv("FNAI_TOKEN")
	if token == "" {
		token = cfg.Token
	}
	base := os.Getenv("FNAI_BASE_URL")
	if base == "" {
		base = cfg.BaseURL
	}
	if base == "" {
		base = "http://localhost:8000"
	}
	req, err := http.NewRequest("POST", strings.TrimRight(base, "/")+"/api/functions/"+fnID+"/versions/yaml",
		bytes.NewReader(raw))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Content-Type", "text/yaml")
	req.Header.Set("User-Agent", "fnai-cli/"+fnai.Version)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, &fnai.Error{Status: 0, Detail: "cannot reach " + base + ": " + err.Error()}
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
		return nil, &fnai.Error{Status: resp.StatusCode, Detail: detail}
	}
	var out map[string]any
	return out, json.NewDecoder(resp.Body).Decode(&out)
}

// ── fn ls ─────────────────────────────────────────────────────────────────

func cmdLs(args []string) error {
	c, err := client()
	if err != nil {
		return err
	}
	fns, err := c.Functions.List()
	if err != nil {
		return err
	}
	if len(fns) == 0 {
		fmt.Println("No functions yet — try `fn init` then `fn new`.")
		return nil
	}
	fmt.Printf("%-6s  %-28s  %s\n", "ID", "SLUG", "NAME")
	for _, f := range fns {
		fmt.Printf("%-6d  %-28s  %s\n", f.ID, f.Slug, f.Name)
	}
	return nil
}

// ── fn run ────────────────────────────────────────────────────────────────

func cmdRun(args []string) error {
	fs := newFlagSet("run")
	key := fs.String("key", os.Getenv("FNAI_INVOKE_KEY"), "the function's fnk_... invoke key")
	input := fs.String("input", "{}", "JSON object, e.g. '{\"ticket_text\": \"...\"}'")
	baseURL := fs.String("base-url", "", "server URL (default: production)")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if fs.NArg() != 1 {
		return fmt.Errorf("usage: fn run <slug> --key <fnk_...> --input '{...}'")
	}
	if *key == "" {
		return fmt.Errorf("no invoke key — pass --key, or set FNAI_INVOKE_KEY " +
			"(mint one for this function in the app, or via the management API — a personal " +
			"access token from `fn login` does not work here, invoke uses its own narrower key)")
	}
	var in map[string]any
	if err := json.Unmarshal([]byte(*input), &in); err != nil {
		return fmt.Errorf("--input must be a JSON object: %w", err)
	}
	f := fnai.NewFunction(fs.Arg(0), *key, urlOpt(*baseURL)...)
	res, err := f.Run(in)
	if err != nil {
		return err
	}
	if !res.OK() {
		out, _ := json.MarshalIndent(res, "", "  ")
		fmt.Println(string(out))
		return fmt.Errorf("run failed: %s", res.Error)
	}
	out, _ := json.MarshalIndent(res.Output, "", "  ")
	fmt.Println(string(out))
	fmt.Fprintf(os.Stderr, "(%dms, $%.4f, trace %s)\n", res.Meta.LatencyMS, res.Meta.CostUSD, res.Meta.TraceID)
	return nil
}

// ── fn deploy / rollback / pin ───────────────────────────────────────────

func cmdDeploy(args []string) error {
	fs := newFlagSet("deploy")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if fs.NArg() != 2 {
		return fmt.Errorf("usage: fn deploy <function-id> <version-id>")
	}
	fnID, err := strconv.Atoi(fs.Arg(0))
	if err != nil {
		return fmt.Errorf("function-id must be a number")
	}
	vID, err := strconv.Atoi(fs.Arg(1))
	if err != nil {
		return fmt.Errorf("version-id must be a number")
	}
	c, err := client()
	if err != nil {
		return err
	}
	dep, err := c.Deployments.Deploy(fnID, vID)
	if err != nil {
		return err
	}
	fmt.Printf("Deployed. Live deployment id %d.\n", dep.ID)
	return nil
}

func cmdRollback(args []string) error {
	fs := newFlagSet("rollback")
	toVersion := fs.Int("to-version", 0, "roll back to this specific version id (default: the previous deployment)")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if fs.NArg() != 1 {
		return fmt.Errorf("usage: fn rollback <function-id> [--to-version <version-id>]")
	}
	fnID, err := strconv.Atoi(fs.Arg(0))
	if err != nil {
		return fmt.Errorf("function-id must be a number")
	}
	c, err := client()
	if err != nil {
		return err
	}
	var vp *int
	if *toVersion != 0 {
		vp = toVersion
	}
	dep, err := c.Deployments.Rollback(fnID, vp)
	if err != nil {
		return err
	}
	fmt.Printf("Rolled back. Live deployment id %d.\n", dep.ID)
	return nil
}

func cmdPin(args []string) error {
	fs := newFlagSet("pin")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if fs.NArg() != 2 {
		return fmt.Errorf("usage: fn pin <function-id> <version-id>")
	}
	fnID, err := strconv.Atoi(fs.Arg(0))
	if err != nil {
		return fmt.Errorf("function-id must be a number")
	}
	vID, err := strconv.Atoi(fs.Arg(1))
	if err != nil {
		return fmt.Errorf("version-id must be a number")
	}
	c, err := client()
	if err != nil {
		return err
	}
	v, err := c.Versions.Get(vID)
	if err != nil {
		return err
	}
	if err := savePin(&pin{FunctionID: fnID, VersionID: vID, Number: v.Number}); err != nil {
		return err
	}
	fmt.Printf("Pinned function %d to version %d (v%d) in this folder.\n", fnID, vID, v.Number)
	return nil
}

// ── fn diff ───────────────────────────────────────────────────────────────

func cmdDiff(args []string) error {
	fs := newFlagSet("diff")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if fs.NArg() != 2 {
		return fmt.Errorf("usage: fn diff <version-id-a> <version-id-b>")
	}
	aID, err := strconv.Atoi(fs.Arg(0))
	if err != nil {
		return fmt.Errorf("version-id-a must be a number")
	}
	bID, err := strconv.Atoi(fs.Arg(1))
	if err != nil {
		return fmt.Errorf("version-id-b must be a number")
	}
	c, err := client()
	if err != nil {
		return err
	}
	a, err := c.Versions.Get(aID)
	if err != nil {
		return err
	}
	b, err := c.Versions.Get(bID)
	if err != nil {
		return err
	}
	aJSON, _ := json.MarshalIndent(a.Snapshot, "", "  ")
	bJSON, _ := json.MarshalIndent(b.Snapshot, "", "  ")
	printLineDiff(fmt.Sprintf("v%d", a.Number), fmt.Sprintf("v%d", b.Number),
		string(aJSON), string(bJSON))
	return nil
}

// printLineDiff is a minimal line-based diff — no external dependency, and
// this only ever compares two small, already-JSON-pretty-printed snapshots,
// so a longest-common-subsequence algorithm would be overkill. Lines unique
// to `a` print with a leading `-`, unique to `b` with `+`, shared lines with
// a space, same spirit as `diff -u` without chasing its exact output format.
func printLineDiff(labelA, labelB, a, b string) {
	linesA := strings.Split(a, "\n")
	linesB := strings.Split(b, "\n")
	inB := make(map[string]int, len(linesB))
	for _, l := range linesB {
		inB[l]++
	}
	fmt.Printf("--- %s\n+++ %s\n", labelA, labelB)
	for _, l := range linesA {
		if inB[l] > 0 {
			fmt.Println(" ", l)
			inB[l]--
		} else {
			fmt.Println("-", l)
		}
	}
	seen := make(map[string]int, len(linesA))
	for _, l := range linesB {
		if seen[l] < countBefore(linesA, l) {
			seen[l]++
			continue
		}
		fmt.Println("+", l)
	}
}

func countBefore(lines []string, target string) int {
	n := 0
	for _, l := range lines {
		if l == target {
			n++
		}
	}
	return n
}

// ── fn eval ───────────────────────────────────────────────────────────────

// cmdEval runs the function's checks and exits nonzero if any of them fail,
// so `fn eval` can gate a CI build (the ticket's own acceptance criterion,
// and E11's stated reason this CLI exists at all).
//
// Auto-selection, when --snapshot/--scores are omitted: the function's most
// recently created dataset's most recent snapshot, and every one of the
// function's scores. This is a reasonable default, not a guarantee it's the
// RIGHT dataset or check set for every function — pass the flags explicitly
// in CI once you know which one you mean.
func cmdEval(args []string) error {
	fs := newFlagSet("eval")
	versionID := fs.Int("version", 0, "version id to evaluate (default: the folder's pinned version)")
	snapshotID := fs.Int("snapshot", 0, "dataset snapshot id (default: the function's latest)")
	scoresFlag := fs.String("scores", "", "comma-separated score ids (default: every score on the function)")
	repeats := fs.Int("repeats", 1, "repeats per row")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if fs.NArg() != 1 {
		return fmt.Errorf("usage: fn eval <function-id> [--version <id>] [--snapshot <id>] [--scores <id,id>]")
	}
	fnID, err := strconv.Atoi(fs.Arg(0))
	if err != nil {
		return fmt.Errorf("function-id must be a number")
	}
	c, err := client()
	if err != nil {
		return err
	}

	vID := *versionID
	if vID == 0 {
		p, perr := loadPin()
		if perr != nil {
			return fmt.Errorf("no --version given, and %w", perr)
		}
		vID = p.VersionID
	}

	snapID := *snapshotID
	if snapID == 0 {
		snapID, err = latestSnapshotID(c, fnID)
		if err != nil {
			return fmt.Errorf("couldn't find a dataset snapshot to evaluate against: %w", err)
		}
	}

	var scoreIDs []int
	if *scoresFlag != "" {
		for _, s := range strings.Split(*scoresFlag, ",") {
			id, err := strconv.Atoi(strings.TrimSpace(s))
			if err != nil {
				return fmt.Errorf("--scores must be comma-separated numbers")
			}
			scoreIDs = append(scoreIDs, id)
		}
	} else {
		// "Gate" scores were removed from the product (see backend/app/models.py's
		// Score.gate docstring) — the API never serializes that field, so a
		// sc.Gate check here is always false and this path could never select
		// anything. Verified against a live server while porting this CLI to
		// Python (see sdk/python/fn_cli.py); fixed here too even though this
		// Go build is otherwise still unverified. Default to every score
		// defined for the function instead.
		scores, err := c.Eval.Scores.List(fnID)
		if err != nil {
			return err
		}
		for _, sc := range scores {
			scoreIDs = append(scoreIDs, sc.ID)
		}
		if len(scoreIDs) == 0 {
			return fmt.Errorf("this function has no checks to evaluate against — " +
				"add one in the app, or pass --scores explicitly")
		}
	}

	exp, err := c.Eval.Experiments.Create(fnID, map[string]any{
		"version_ids": []int{vID}, "snapshot_id": snapID,
		"score_ids": scoreIDs, "repeats": *repeats,
	})
	if err != nil {
		return err
	}
	fmt.Printf("Running experiment %d against %d check(s)...\n", exp.ID, len(scoreIDs))
	if _, err := c.Eval.Experiments.Run(exp.ID); err != nil {
		return err
	}

	status, err := waitForExperiment(c, exp.ID)
	if err != nil {
		return err
	}
	if status == "failed" {
		return fmt.Errorf("the experiment itself failed to run (infrastructure error) — see the app for detail")
	}

	failed, err := gateFailures(c, fnID, exp.ID, scoreIDs)
	if err != nil {
		return err
	}
	if len(failed) > 0 {
		for _, f := range failed {
			fmt.Printf("FAIL  %-30s pass_rate=%.0f%%\n", f.name, f.passRate*100)
		}
		return fmt.Errorf("%d check(s) failed", len(failed))
	}
	fmt.Println("All checks passed.")
	return nil
}

// waitForExperiment polls GET /api/experiments/{id} (no typed SDK method for
// a single experiment yet, hence Raw) — .Run() itself only ever fires once,
// per experiments/api.py: calling it again on an already-running experiment
// is a 409, not a status check.
func waitForExperiment(c *fnai.Client, expID int) (string, error) {
	deadline := time.Now().Add(10 * time.Minute)
	for {
		var exp struct {
			Status string `json:"status"`
		}
		if err := c.Raw("GET", fmt.Sprintf("/api/experiments/%d", expID), nil, &exp); err != nil {
			return "", err
		}
		if exp.Status == "completed" || exp.Status == "failed" {
			return exp.Status, nil
		}
		if time.Now().After(deadline) {
			return "", fmt.Errorf("experiment %d didn't finish within 10 minutes — check it in the app", expID)
		}
		time.Sleep(2 * time.Second)
	}
}

type gateFailure struct {
	name     string
	passRate float64
}

// gateFailures reads score-results for exactly this experiment — since the
// experiment was created with precisely scoreIDs, every row this returns
// already belongs to one of them; no separate filtering needed.
func gateFailures(c *fnai.Client, fnID, expID int, scoreIDs []int) ([]gateFailure, error) {
	var results []map[string]any
	path := fmt.Sprintf("/api/functions/%d/score-results?experiment_ids=%d&group_by=experiment", fnID, expID)
	if err := c.Raw("GET", path, nil, &results); err != nil {
		return nil, err
	}
	var failures []gateFailure
	for _, r := range results {
		rate, _ := r["pass_rate"].(float64)
		if rate < 1.0 {
			name, _ := r["score"].(string)
			failures = append(failures, gateFailure{name: name, passRate: rate})
		}
	}
	sort.Slice(failures, func(i, j int) bool { return failures[i].name < failures[j].name })
	return failures, nil
}

func latestSnapshotID(c *fnai.Client, fnID int) (int, error) {
	datasets, err := c.Datasets.List(fnID)
	if err != nil {
		return 0, err
	}
	if len(datasets) == 0 {
		return 0, fmt.Errorf("this function has no datasets yet — create one in the app first")
	}
	// most-recently-created dataset — Datasets.List has no documented
	// ordering guarantee, so this takes the last entry defensively rather
	// than assuming; pass --snapshot explicitly to be exact.
	ds := datasets[len(datasets)-1]
	var snapshots []map[string]any
	if err := c.Raw("GET", fmt.Sprintf("/api/datasets/%d/snapshots", ds.ID), nil, &snapshots); err != nil {
		return 0, err
	}
	if len(snapshots) == 0 {
		return 0, fmt.Errorf("dataset %q has no snapshots yet — take one in the app first", ds.Name)
	}
	id, ok := snapshots[0]["id"].(float64)
	if !ok {
		return 0, fmt.Errorf("unexpected response shape from the snapshots endpoint")
	}
	return int(id), nil
}

// ── fn logs ───────────────────────────────────────────────────────────────

func cmdLogs(args []string) error {
	fs := newFlagSet("logs")
	limit := fs.Int("limit", 20, "how many recent calls to show")
	source := fs.String("source", "production", "production | playground | experiment")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if fs.NArg() != 1 {
		return fmt.Errorf("usage: fn logs <function-id> [--limit N] [--source production]")
	}
	fnID, err := strconv.Atoi(fs.Arg(0))
	if err != nil {
		return fmt.Errorf("function-id must be a number")
	}
	c, err := client()
	if err != nil {
		return err
	}
	traces, err := c.Traces.List(fnID, *source, *limit)
	if err != nil {
		return err
	}
	if len(traces) == 0 {
		fmt.Println("No traces yet.")
		return nil
	}
	fmt.Printf("%-7s  %8s  %-16s  %-10s  %s\n", "STATUS", "LATENCY", "MODEL", "COST", "TRACE ID")
	for _, t := range traces {
		status := "ok"
		if !t.Success {
			status = "FAILED"
		}
		fmt.Printf("%-7s  %6dms  %-16s  $%-9.4f  %s\n", status, t.LatencyMS, t.Model, t.Cost, t.TraceID)
	}
	return nil
}
