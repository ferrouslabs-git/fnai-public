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
await c.versions.submit(v.id);
await c.versions.approve(v.id);
await c.deployments.deploy(fn.id, v.id);

const key = await c.keys.invoke.mint(fn.id, "prod");       // fnk_… shown once
const result = await c.function(fn.slug, key.key).run({ document: "…" });

const plan = await c.eval.plans.create(fn.id, { name: "baseline" });
const exp = await c.eval.experiments.create(fn.id, {
  version_ids: [v.id], models: [{ name: "mock-small" }],
});
await c.eval.experiments.run(exp.id);

console.log(await c.functions.health(fn.id));
```

Namespaces: `functions` · `versions` · `eval.plans` / `eval.experiments` /
`eval.scores` · `datasets` · `deployments` · `keys.provider` / `keys.invoke` ·
`traces` · `auth.tokens`. Full walkthrough:
[`examples/golden-path.mjs`](examples/golden-path.mjs).

## License

Apache License 2.0.
