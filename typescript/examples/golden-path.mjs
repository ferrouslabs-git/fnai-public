/**
 * functionalAI TS SDK — the golden path in one script.
 *
 * Run against a dev server (FNAI_AUTH_MODE=test) with FNAI_BASE set, or
 * against staging with FNAI_PAT set to a personal access token:
 *
 *   node examples/golden-path.mjs
 *
 * Build first: npm run build
 */
import { Client } from "../dist/fnai.js";

const BASE = process.env.FNAI_BASE ?? "http://127.0.0.1:8000";
const SLUG = `ts_demo_${Math.floor(Date.now() / 1000)}`;

// bootstrap: in dev-auth mode, mint a PAT as the default admin; on staging,
// pass FNAI_PAT from a token minted in the app (avatar → API tokens).
let pat = process.env.FNAI_PAT;
if (!pat) {
  const r = await fetch(`${BASE}/api/auth/tokens`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: "ts-golden-path" }),
  });
  if (!r.ok) throw new Error(`PAT bootstrap failed: ${r.status}`);
  const minted = await r.json();
  pat = minted.token;
  console.log(`PAT minted: ${minted.prefix}…`);
}

const c = new Client({ token: pat, baseUrl: BASE });
console.log("me():", (await c.me()).roles);

const fn = await c.functions.create({
  slug: SLUG, name: "TS Demo",
  input_schema: { type: "object", properties: { document: { type: "string" } } },
  output_schema: {
    type: "object", required: ["supplier", "total"],
    properties: { supplier: { type: "string" }, total: { type: "number" } },
  },
});
console.log("functions.create →", fn.id, fn.slug);

const v = await c.functions.createVersion(fn.id, {
  snapshot: {
    sections: [{ type: "role", title: "Role", content: "You extract data from {document}." }],
    impl: "prompt",
    model_binding: { model: "mock-small", temperature: 0.2 },
    tools: [],
  },
  message: "via TS SDK",
});
console.log("functions.createVersion →", v.id);

const play = await c.versions.playground(v.id, { input: { document: "Invoice, total 12.30" } });
console.log("versions.playground →", JSON.stringify(play.output).slice(0, 60));

await c.deployments.deploy(fn.id, v.id);   // deploy IS the gate — freezes the draft
console.log("deployments.deploy →", (await c.deployments.current(fn.id)).version_id);

const key = await c.keys.invoke.mint(fn.id, "ts-demo");
const handle = c.function(SLUG, key.key);
const contract = await handle.contract();
console.log("contract →", contract.function, "v", contract.version);
const result = await handle.run({ document: "Invoice from Acme Ltd, total 42.50" });
console.log("run →", result.ok ? "ok" : `FAILED: ${result.error}`,
            "| trace:", result.traceId, "| cost:", result.meta.cost_usd);

const traces = await c.traces.list({ functionId: fn.id, source: "production" });
const health = await c.functions.health(fn.id);
console.log("traces:", traces.length, "| health calls:", health.calls);

await c.functions.archive(fn.id);
console.log("cleanup: demo function archived");
console.log("\nTS golden path complete — every call through the fnai Client");
