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

Namespaces: `Functions` · `Versions` · `Eval.Plans` / `Eval.Experiments` /
`Eval.Scores` · `Datasets` · `Deployments` · `Keys.Provider` / `Keys.Invoke` ·
`Traces` · `Auth.Tokens`. Full walkthrough:
[`examples/goldenpath/main.go`](examples/goldenpath/main.go).

Design note: struct types cover the golden-path surface and are maintained by
hand (the TypeScript client's types are generated from OpenAPI; Go generators
were judged noisier than the hand-written surface is costly — revisit if the
API grows). Dynamic payloads (snapshots, JSON Schemas) are `map[string]any`.

New here? [SDK quickstart](https://github.com/ferrouslabs-git/functionalAI/blob/staging/docs/sdk-quickstart.md)
· [credential guide](https://github.com/ferrouslabs-git/functionalAI/blob/staging/docs/sdk-auth.md).
License: Apache 2.0.
