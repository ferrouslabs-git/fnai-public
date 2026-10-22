// FunctionDraft (FUNCTION_CLASS_DESIGN.md, approved 2026-09-24): the shared
// create/addInput/addOutput/addSection/addExample/addTool/submit/deploy flow.
// Mocked-HTTP unit tests, matching this SDK's own convention (see
// test-yaml-endpoints.mjs) rather than standing up a real backend.
import assert from "node:assert/strict";
import os from "node:os";
import fs from "node:fs";
import path from "node:path";

// keep draft files from tests out of the working folder
process.env.FNAI_DRAFTS_DIR = fs.mkdtempSync(path.join(os.tmpdir(), "fnai-ts-drafts-"));
import { Client, FunctionDraft } from "./dist/fnai.js";

const BASE = "http://fake.local";

function fakeFetch(handler) {
  const calls = [];
  globalThis.fetch = async (url, opts) => {
    calls.push({ url: String(url), ...opts });
    return handler(String(url), opts, calls.length);
  };
  return calls;
}

function jsonResponse(status, body) {
  return {
    ok: status >= 200 && status < 300, status, statusText: String(status),
    json: async () => body, text: async () => JSON.stringify(body),
  };
}

// ── create() saves nothing; the function is created at submit() ─────────────
{
  const calls = fakeFetch((url, opts) => {
    if (url.startsWith(`${BASE}/api/functions?`) && opts.method === "GET") return jsonResponse(200, []);
    if (url === `${BASE}/api/functions` && opts.method === "POST") {
      const body = JSON.parse(opts.body);
      return jsonResponse(200, { id: 60, slug: body.slug, name: body.name, ...body });
    }
    if (url === `${BASE}/api/functions/60/submit` && opts.method === "POST") {
      return jsonResponse(200, { id: 500, number: 1, status: "draft" });
    }
    if (url === `${BASE}/api/tools` && opts.method === "GET") return jsonResponse(200, [{ name: "ocr", status: "active" }]);
    throw new Error(`unexpected request: ${opts.method} ${url}`);
  });
  const client = new Client({ token: "fnp_x", baseUrl: BASE });
  const draft = await client.draft({ name: "Invoice Extractor" });
  assert.equal(draft.id, undefined);                     // not created yet
  assert.ok(draft instanceof FunctionDraft);
  assert.deepEqual(calls.map((c) => c.method), ["GET"]); // only the read-only name check

  draft.addInput("document", { type: "file", media: "document", required: true });
  draft.addOutput("total", { type: "number", required: true });
  draft.addSection("role", "You extract data from invoices.");
  draft.addStep("Read the invoice.");
  draft.addTool("ocr");
  assert.equal(calls.length, 1); // still nothing sent

  await draft.submit();
  const urls = calls.map((c) => `${c.method} ${c.url.replace(BASE, "")}`);
  assert.ok(urls.indexOf("POST /api/functions") > -1);
  assert.ok(urls.indexOf("POST /api/functions") < urls.indexOf("POST /api/functions/60/submit"));
  assert.equal(draft.id, 60);
  console.log("OK: create() sends nothing; the function is created at submit(), then saved");
}

// ── a taken name is refused at once, archived or not; a failed save is not created twice ──
{
  fakeFetch((url, opts) => {
    if (url.startsWith(`${BASE}/api/functions?`)) return jsonResponse(200, [{ id: 7, slug: "invoice_extractor", archived: true }]);
    throw new Error(`unexpected request: ${opts.method} ${url}`);
  });
  const client = new Client({ token: "fnp_x", baseUrl: BASE });
  await assert.rejects(() => client.draft({ name: "Invoice Extractor" }), /already exists \(archived\).*loadDraft\(7\)/);

  let creates = 0, failSave = true;
  fakeFetch((url, opts) => {
    if (url.startsWith(`${BASE}/api/functions?`)) return jsonResponse(200, []);
    if (url === `${BASE}/api/functions` && opts.method === "POST") { creates++; return jsonResponse(200, { id: 61, ...JSON.parse(opts.body) }); }
    if (url === `${BASE}/api/functions/61/submit`) {
      if (failSave) return jsonResponse(422, { detail: "server said no" });
      return jsonResponse(200, { id: 501, number: 1, status: "draft" });
    }
    throw new Error(`unexpected request: ${opts.method} ${url}`);
  });
  const d = await new Client({ token: "fnp_x", baseUrl: BASE }).functions.new("Retry Me", { saveTo: false });
  d.addOutput("o", { required: true }).addStep("s");
  await assert.rejects(() => d.submit(), /server said no/);
  assert.equal(d.id, 61);                       // created, and remembered
  failSave = false;
  await d.submit();
  assert.equal(creates, 1);                     // the retry did not create it again

  fakeFetch((url, opts) => {
    if (url.startsWith(`${BASE}/api/functions?`)) return jsonResponse(200, []);
    if (url === `${BASE}/api/functions` && opts.method === "POST") return jsonResponse(409, { detail: "slug already exists" });
    throw new Error(`unexpected request: ${opts.method} ${url}`);
  });
  const e = await new Client({ token: "fnp_x", baseUrl: BASE }).functions.new("Clash", { saveTo: false });
  e.addOutput("o", { required: true }).addStep("s");
  await assert.rejects(() => e.submit(), /Start again with a different slug/);
  assert.equal(e.id, undefined);
  console.log("OK: a taken name is refused at once; a failed save is not created twice; a clash keeps the work");
}

// ── submit() sends the staged snapshot + schemas ────────────────────────────
{
  let submitBody;
  const calls = fakeFetch((url, opts) => {
    if (url.startsWith(`${BASE}/api/functions?`) && opts.method === "GET") return jsonResponse(200, []);
    if (url === `${BASE}/api/functions` && opts.method === "POST") {
      const body = JSON.parse(opts.body);
      return jsonResponse(200, { id: 60, slug: body.slug, name: body.name,
                                 input_schema: {}, output_schema: {} });
    }
    if (url === `${BASE}/api/functions/60/submit` && opts.method === "POST") {
      submitBody = JSON.parse(opts.body);
      return jsonResponse(200, { id: 500, number: 1, status: "draft",
                                 snapshot: submitBody.snapshot });
    }
    throw new Error(`unexpected request: ${opts.method} ${url}`);
  });
  const client = new Client({ token: "fnp_x", baseUrl: BASE });
  const draft = await client.draft({ name: "Invoice Extractor" });
  draft.addInput("document", { type: "file", media: "document", required: true });
  draft.addOutput("total", { type: "number", required: true });
  draft.addSection("role", "You extract data from invoices.");
  draft.addStep("Read the invoice and extract the total.");
  draft.addTool("ocr");

  const v = await draft.submit("first draft");
  assert.equal(v.id, 500);
  assert.equal(v.number, 1);
  assert.equal(submitBody.message, "first draft");
  // the server reads `required` as a list on the schema, and stores a file field as a string
  // handle tagged x-media
  assert.deepEqual(submitBody.input_schema.properties.document,
                   { type: "string", "x-media": "document", "x-delivery": "native" });
  assert.deepEqual(submitBody.input_schema.required, ["document"]);
  assert.deepEqual(submitBody.output_schema.properties.total, { type: "number" });
  assert.deepEqual(submitBody.output_schema.required, ["total"]);
  // a v2 definition document: field-form input/output, flow, mode
  assert.deepEqual(submitBody.snapshot.output.total, { type: "number", required: true });
  assert.equal(submitBody.snapshot.input.document.type, "file");
  assert.deepEqual(submitBody.snapshot.implementation.flow, ["Read the invoice and extract the total."]);
  assert.deepEqual(submitBody.snapshot.implementation.execution, { mode: "agent" });
  assert.equal(submitBody.snapshot.implementation.sections.role,
              "You extract data from invoices.");
  assert.deepEqual(submitBody.snapshot.implementation.tools, [{ name: "ocr" }]);
  assert.equal(draft.lastVersion.id, 500);
  console.log("OK: submit() sends the staged snapshot and schemas");
}

// ── deploy() uses the version from the last submit() ────────────────────────
{
  let deployBody;
  const calls = fakeFetch((url, opts) => {
    if (url.startsWith(`${BASE}/api/functions?`) && opts.method === "GET") return jsonResponse(200, []);
    if (url === `${BASE}/api/functions` && opts.method === "POST") {
      const body = JSON.parse(opts.body);
      return jsonResponse(200, { id: 60, slug: body.slug, name: body.name,
                                 input_schema: {}, output_schema: {} });
    }
    if (url === `${BASE}/api/functions/60/submit` && opts.method === "POST") {
      return jsonResponse(200, { id: 500, number: 1, status: "draft" });
    }
    if (url === `${BASE}/api/functions/60/deploy` && opts.method === "POST") {
      deployBody = JSON.parse(opts.body);
      return jsonResponse(200, { id: 9, status: "active", version_id: deployBody.version_id });
    }
    throw new Error(`unexpected request: ${opts.method} ${url}`);
  });
  const client = new Client({ token: "fnp_x", baseUrl: BASE });
  const draft = await client.draft({ name: "Invoice Extractor" });
  draft.addOutput("total", { type: "number" });
  draft.addStep("Extract the total.");
  const v = await draft.submit();
  const result = await draft.deploy("go live");
  assert.equal(deployBody.version_id, v.id);
  assert.equal(deployBody.note, "go live");
  assert.equal(result.status, "active");
  console.log("OK: deploy() uses the version from the last submit()");
}

// ── deploy() before any submit() throws ─────────────────────────────────────
{
  fakeFetch((url, opts) => {
    if (url.startsWith(`${BASE}/api/functions?`) && opts.method === "GET") return jsonResponse(200, []);
    if (url === `${BASE}/api/functions` && opts.method === "POST") {
      const body = JSON.parse(opts.body);
      return jsonResponse(200, { id: 60, slug: body.slug, name: body.name,
                                 input_schema: {}, output_schema: {} });
    }
    throw new Error(`unexpected request: ${opts.method} ${url}`);
  });
  const client = new Client({ token: "fnp_x", baseUrl: BASE });
  const draft = await client.draft({ name: "Invoice Extractor" });
  await assert.rejects(() => draft.deploy(), /call submit\(\) first/);
  console.log("OK: deploy() before any submit() throws");
}

// ── load() resumes an existing function's staged content ────────────────────
{
  const existingVersion = {
    id: 500, number: 1, status: "approved",
    snapshot: { name: "Invoice Extractor", output: {},
               implementation: { sections: { role: "You extract invoices." },
                                 tools: [{ name: "ocr" }] } },
  };
  fakeFetch((url, opts) => {
    if (url === `${BASE}/api/functions/60` && opts.method === "GET") {
      return jsonResponse(200, { id: 60, slug: "invoice_extractor", name: "Invoice Extractor",
                                 input_schema: {}, output_schema: {} });
    }
    if (url === `${BASE}/api/functions/60/versions` && opts.method === "GET") {
      return jsonResponse(200, [existingVersion]);
    }
    if (url === `${BASE}/api/functions/60/submit` && opts.method === "POST") {
      const body = JSON.parse(opts.body);
      return jsonResponse(200, { id: 501, number: 2, status: "draft", snapshot: body.snapshot });
    }
    throw new Error(`unexpected request: ${opts.method} ${url}`);
  });
  const client = new Client({ token: "fnp_x", baseUrl: BASE });
  const draft = await client.loadDraft(60);
  assert.equal(draft.lastVersion.id, 500);

  // adding a field after it's already deployed, then submit — same submit()
  // call as building it fresh, per the design doc's example
  draft.addOutput("tax_amount", { type: "number" });
  draft.addStep("Extract the tax amount too.");
  const v = await draft.submit("add tax_amount");
  assert.equal(v.number, 2); // a new version, not an overwrite
  console.log("OK: load() resumes an existing function, submit() still applies");
}

// ── addSection rejects an unknown kind ───────────────────────────────────────
{
  fakeFetch((url, opts) => {
    if (opts.method === "GET") return jsonResponse(200, []);
    const body = JSON.parse(opts.body);
    return jsonResponse(200, { id: 60, slug: body.slug, name: body.name,
                               input_schema: {}, output_schema: {} });
  });
  const client = new Client({ token: "fnp_x", baseUrl: BASE });
  const draft = await client.draft({ name: "Invoice Extractor" });
  assert.throws(() => draft.addSection("bogus_kind", "x"));
  console.log("OK: addSection rejects an unknown kind");
}

console.log("\nAll FunctionDraft tests passed.");
