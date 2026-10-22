# fnai — TypeScript client

Zero-runtime-dependency TypeScript client for [functionalAI](https://fnai.dev)
(global `fetch`, Node ≥ 18 or any modern runtime). Mirrors the
[Python client](../python/) namespace-for-namespace.

- **`Function`** (`FnaiFunction`) — invoke deployed functions (public runtime
  API, `fnk_…` invoke key)
- **`Client`** — the management API: create, version, evaluate, deploy, observe
  (`fnp_…` personal access token, minted in the app under avatar → API tokens)

Types are generated from the backend's OpenAPI schema into
[`schema.d.ts`](schema.d.ts) by `tools/gen_sdk.py` (pinned
`openapi-typescript`); a CI drift gate keeps them honest. Never edit that file
by hand.

## Install & build

```bash
npm install && npm run build   # emits dist/
```

## Runtime surface

```ts
import { Function } from "fnai";

const fn = new Function("doc_summarizer", {
  apiKey: "fnk_…",                       // Deploy ▸ API keys → Mint
  baseUrl: "https://staging.fnai.dev",
});
const contract = await fn.contract();     // the deployed input/output schemas
const result = await fn.run({ document: "…" });
if (result.ok) console.log(result.output, result.meta.cost_usd, result.traceId);
else console.log("execution failed:", result.error);
```

API-level failures throw `FnaiError` (`.status`, `.detail`); an **execution**
failure is not an exception — check `result.ok`.

## Management surface

```ts
import { Client } from "fnai";

const c = new Client({ token: "fnp_…", baseUrl: "https://staging.fnai.dev" });

const fn = await c.functions.create({ slug, name, input_schema, output_schema });
const v = await c.functions.createVersion(fn.id, { snapshot, message: "v1" });
await c.versions.playground(v.id, { input: { document: "…" } });
await c.deployments.deploy(fn.id, v.id);   // deploy IS the gate — freezes the draft

const key = await c.keys.invoke.mint(fn.id, "prod");       // fnk_… shown once
const result = await c.function(fn.slug, key.key).run({ document: "…" });

const plan = await c.eval.plans.create(fn.id, { name: "baseline" });
const exp = await c.eval.experiments.create(fn.id, {
  version_ids: [v.id], models: [{ name: "mock-small" }],
});
await c.eval.experiments.run(exp.id);

console.log(await c.functions.health(fn.id));
```

Namespaces: `functions` · `spaces` · `versions` · `eval.plans` /
`eval.experiments` / `eval.scores` · `datasets` · `deployments` ·
`keys.provider` / `keys.invoke` · `traces` · `auth.tokens`. Full walkthrough:
[`examples/golden-path.mjs`](examples/golden-path.mjs).

### Working with more than one space

Every call above runs against whichever space this token's account
defaults to — fine as long as the account only has one. If it has several,
pick one explicitly:

```ts
const me = await c.me();
console.log(me.spaces.map((sp) => sp.name));   // what you can choose from

await c.useSpace("Marketing");     // validated against the account's real
                                    // spaces every call — throws
                                    // SpaceNotFoundError on a bad name
console.log(c.currentSpace);       // "Marketing"

await c.functions.create(...);     // now created in "Marketing", not the default
await c.useSpace(null);            // back to the account's default
```

`useSpace()` is opt-in: skip it entirely and nothing changes — no
`X-Space` header is ever sent, same as before this existed.

### Input validation

Every method that creates or changes something validates its required
fields, ids, and (for `functions.create`/`spaces.create`) name/slug
uniqueness client-side, throwing a plain `Error` before any request goes
out — e.g. `c.spaces.create({ name: "" })` or a slug that already exists
in the current space fails immediately with a message naming exactly
what's wrong, rather than a generic 422 from the server.

## License

Apache License 2.0.

## Report a call that ran on your own servers

```ts
const { trace_id, duplicate } = await fn.reportTrace({
  function_id: 3, version_id: 4, artifact_hash: "…64 characters…",
  input: { document: "…" }, output: { summary: "…" }, success: true,
  steps: [{ type: "llm" }], tokens_in: 12, tokens_out: 8, cost: 0.0003, latency_ms: 840,
});
// sending the same trace_id again is harmless: duplicate comes back true
```

It shows in the monitor next to hosted calls. See `docs/self-hosted-traces.md`.
