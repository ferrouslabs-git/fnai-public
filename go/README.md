# fnai — Go client

Zero-dependency Go client for [functionalAI](https://fnai.dev) (`net/http`,
`encoding/json`, Go ≥ 1.22). Mirrors the [Python](../python/) and
[TypeScript](../typescript/) clients namespace-for-namespace.

```bash
go get github.com/ferrouslabs-git/fnai-public/go
```

## Runtime surface (`fnk_…` invoke key)

```go
fn := fnai.NewFunction("doc_summarizer", "fnk_…",
    fnai.WithBaseURL("https://staging.fnai.dev"))
result, err := fn.Run(map[string]any{"document": "…"})
if err != nil { /* API-level failure: *fnai.Error with Status/Detail */ }
if result.OK() {
    fmt.Println(result.Output, result.Meta.CostUSD, result.Meta.TraceID)
} else {
    fmt.Println("execution failed:", result.Error) // NOT a Go error
}
```

## Management surface (`fnp_…` personal access token)

```go
c := fnai.NewClient("fnp_…", fnai.WithBaseURL("https://staging.fnai.dev"))

fn, _ := c.Functions.Create(fnai.FunctionCreate{Slug: "summarizer", Name: "Summarizer",
    InputSchema: in, OutputSchema: out})
v, _ := c.Functions.CreateVersion(fn.ID, snapshot, "v1")
c.Versions.Submit(v.ID); c.Versions.Approve(v.ID, "ok")
c.Deployments.Deploy(fn.ID, v.ID)

key, _ := c.Keys.Invoke.Mint(fn.ID, "prod")           // plaintext shown once
result, _ := c.Function(fn.Slug, key.Key).Run(map[string]any{"document": "…"})

health, _ := c.Functions.Health(fn.ID, 24)
```

Namespaces: `Functions` · `Spaces` · `Versions` · `Eval.Plans` /
`Eval.Experiments` / `Eval.Scores` · `Datasets` · `Deployments` ·
`Keys.Provider` / `Keys.Invoke` · `Traces` · `Auth.Tokens`. Full walkthrough:
[`examples/goldenpath/main.go`](examples/goldenpath/main.go).

### Working with more than one space

Every call above runs against whichever space this token's account
defaults to — fine as long as the account only has one. If it has several,
pick one explicitly:

```go
me, _ := c.Me()
fmt.Println(me["spaces"])          // what you can choose from

err := c.UseSpace("Marketing")     // validated against the account's real
                                    // spaces every call — returns
                                    // *fnai.SpaceNotFoundError on a bad name
fmt.Println(c.CurrentSpace())      // "Marketing"

c.Functions.Create(...)            // now created in "Marketing", not the default
c.UseSpace("")                     // back to the account's default
```

`UseSpace` is opt-in: skip it entirely and nothing changes — no `X-Space`
header is ever sent, same as before this existed.

### Input validation

Every method that creates or changes something validates its required
fields, ids, and (for `Functions.Create`/`Spaces.Create`) name/slug
uniqueness client-side, returning a `*fnai.ValidationError` before any
request goes out — e.g. `c.Spaces.Create("")` or a slug that already
exists in the current space fails immediately with a message naming
exactly what's wrong, rather than a generic 422 from the server.

Design note: struct types cover the golden-path surface and are maintained by
hand (the TypeScript client's types are generated from OpenAPI; Go generators
were judged noisier than the hand-written surface is costly — revisit if the
API grows). Dynamic payloads (snapshots, JSON Schemas) are `map[string]any`.

New here? [SDK quickstart](https://github.com/ferrouslabs-git/functionalAI/blob/staging/docs/sdk-quickstart.md)
· [credential guide](https://github.com/ferrouslabs-git/functionalAI/blob/staging/docs/sdk-auth.md).
License: Apache 2.0.

## Which function do I use?

See [GUIDE.md](GUIDE.md) — a task-by-task list, including building a function one part at a time
(`c.Functions.New(...)`), the YAML draft file kept on your computer, and loading it back with the `yamlload`
package. The main `fnai` package still has no dependencies: only `yamlload` uses a YAML library
(`gopkg.in/yaml.v3`), so programs that do not import it never download it.

## Report a call that ran on your own servers

```go
receipt, err := fn.ReportTrace(map[string]any{
    "function_id": 3, "version_id": 4, "artifact_hash": "…64 characters…",
    "input": map[string]any{"document": "…"}, "output": map[string]any{"summary": "…"},
    "success": true, "steps": []any{map[string]any{"type": "llm"}},
    "tokens_in": 12, "tokens_out": 8, "cost": 0.0003, "latency_ms": 840,
})
// receipt.TraceID, receipt.Duplicate — sending the same trace_id again is harmless
```

It shows in the monitor next to hosted calls. See `docs/self-hosted-traces.md`.
