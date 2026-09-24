// REQ-273/274: createFromYaml/createFromYamlText/getYaml had zero TS SDK
// tests — the Python SDK's equivalents (test_fnai.py) are mocked-HTTP unit
// tests too, so this mirrors that same convention rather than standing up a
// real backend: fake global `fetch`, assert on the request sequence/bodies.
import assert from "node:assert/strict";
import { Client, FnaiError, PromptFileError, SpaceNotFoundError } from "./dist/fnai.js";

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
function textResponse(status, body, contentType = "text/yaml") {
  return {
    ok: status >= 200 && status < 300, status, statusText: String(status),
    json: async () => { throw new Error("not json"); }, text: async () => body,
  };
}

// ── createFromYamlText: no existing function → create, then push ───────────
{
  const calls = fakeFetch((url, opts) => {
    if (url === `${BASE}/api/functions` && opts.method === "GET") {
      return jsonResponse(200, []);
    }
    if (url === `${BASE}/api/functions` && opts.method === "POST") {
      const body = JSON.parse(opts.body);
      return jsonResponse(200, { id: 42, slug: body.slug, name: body.name });
    }
    if (url === `${BASE}/api/functions/42/versions/yaml` && opts.method === "POST") {
      assert.equal(opts.headers["Content-Type"], "text/yaml");
      assert.match(opts.body, /Weather Reporter/);
      return textResponse(201, JSON.stringify({ id: 900, number: 1 }));
    }
    throw new Error(`unexpected request: ${opts.method} ${url}`);
  });
  const client = new Client({ token: "fnp_x", baseUrl: BASE });
  const { fn, version } = await client.functions.createFromYamlText(
    "name: Weather Reporter\ninput:\n  city: {type: string, required: true}\n");
  assert.equal(fn.id, 42);
  assert.equal(fn.slug, "weather_reporter");
  assert.equal(version.id, 900);
  assert.equal(calls.length, 3);
  console.log("OK: createFromYamlText creates a new function, then pushes the YAML");
}

// ── createFromYamlText: an existing function with the same slug is reused ──
{
  fakeFetch((url, opts) => {
    if (url === `${BASE}/api/functions` && opts.method === "GET") {
      return jsonResponse(200, [{ id: 7, slug: "weather_reporter", name: "Weather Reporter" }]);
    }
    if (url === `${BASE}/api/functions` && opts.method === "POST") {
      throw new Error("should not create — a function with this slug already exists");
    }
    if (url === `${BASE}/api/functions/7/versions` && opts.method === "GET") {
      return jsonResponse(200, []);   // nothing to compare against yet
    }
    if (url === `${BASE}/api/functions/7/versions/yaml` && opts.method === "POST") {
      return textResponse(201, JSON.stringify({ id: 901, number: 2 }));
    }
    throw new Error(`unexpected request: ${opts.method} ${url}`);
  });
  const client = new Client({ token: "fnp_x", baseUrl: BASE });
  const { fn } = await client.functions.createFromYamlText("name: Weather Reporter\n");
  assert.equal(fn.id, 7);
  console.log("OK: createFromYamlText reuses an existing function by slug, never re-creates it");
}

// ── createFromYamlText: re-pushing unchanged content is a no-op ────────────
{
  const currentText = "name: Weather Reporter\nmeta: {version: 3}\n";
  const calls = fakeFetch((url, opts) => {
    if (url === `${BASE}/api/functions` && opts.method === "GET") {
      return jsonResponse(200, [{ id: 7, slug: "weather_reporter", name: "Weather Reporter" }]);
    }
    if (url === `${BASE}/api/functions/7/versions` && opts.method === "GET") {
      return jsonResponse(200, [{ id: 300, number: 3 }]);
    }
    if (url === `${BASE}/api/versions/300/yaml` && opts.method === "GET") {
      return textResponse(200, currentText);
    }
    throw new Error(`unexpected request: ${opts.method} ${url}`);
  });
  const client = new Client({ token: "fnp_x", baseUrl: BASE });
  // a hand-edited copy with no meta block at all — still "the same
  // document" once meta is ignored on both sides
  const { fn, version } = await client.functions.createFromYamlText("name: Weather Reporter\n");
  assert.equal(fn.id, 7);
  assert.equal(version.id, 300);
  assert.ok(!calls.some((c) => c.url.endsWith("/versions/yaml")), "must not push a no-op version");
  console.log("OK: re-pushing unchanged content is a no-op — no new version minted");
}

// ── createFromYamlText: a genuine edit still creates a new version ─────────
{
  const currentText = "name: Weather Reporter\nmeta: {version: 3}\n";
  const editedText = "name: Weather Reporter\ndescription: now with more detail\n";
  fakeFetch((url, opts) => {
    if (url === `${BASE}/api/functions` && opts.method === "GET") {
      return jsonResponse(200, [{ id: 7, slug: "weather_reporter", name: "Weather Reporter" }]);
    }
    if (url === `${BASE}/api/functions/7/versions` && opts.method === "GET") {
      return jsonResponse(200, [{ id: 300, number: 3 }]);
    }
    if (url === `${BASE}/api/versions/300/yaml` && opts.method === "GET") {
      return textResponse(200, currentText);
    }
    if (url === `${BASE}/api/functions/7/versions/yaml` && opts.method === "POST") {
      return textResponse(201, JSON.stringify({ id: 301, number: 4 }));
    }
    throw new Error(`unexpected request: ${opts.method} ${url}`);
  });
  const client = new Client({ token: "fnp_x", baseUrl: BASE });
  const { fn, version } = await client.functions.createFromYamlText(editedText);
  assert.equal(fn.id, 7);
  assert.equal(version.id, 301);
  console.log("OK: a genuine edit still creates a new version, exactly as before");
}

// ── createFromYamlText: not valid YAML raises PromptFileError ──────────────
{
  fakeFetch(() => { throw new Error("should never reach the network"); });
  const client = new Client({ token: "fnp_x", baseUrl: BASE });
  await assert.rejects(
    () => client.functions.createFromYamlText(":\n  - this: is: not: valid"),
    (e) => e instanceof PromptFileError && /not valid YAML/.test(e.message));
  console.log("OK: invalid YAML raises PromptFileError before any request is made");
}

// ── createFromYamlText: missing `name` raises PromptFileError ──────────────
{
  fakeFetch(() => { throw new Error("should never reach the network"); });
  const client = new Client({ token: "fnp_x", baseUrl: BASE });
  await assert.rejects(
    () => client.functions.createFromYamlText("input:\n  city: {type: string}\n"),
    (e) => e instanceof PromptFileError && /must be a YAML mapping with at least a `name`/.test(e.message));
  console.log("OK: a document with no `name` raises PromptFileError before any request is made");
}

// ── createFromYamlText: a stale meta.version 409 surfaces as FnaiError ─────
{
  fakeFetch((url, opts) => {
    if (url === `${BASE}/api/functions` && opts.method === "GET") return jsonResponse(200, []);
    if (url === `${BASE}/api/functions` && opts.method === "POST") {
      return jsonResponse(200, { id: 5, slug: "x" });
    }
    if (opts.method === "POST" && url.endsWith("/versions/yaml")) {
      return jsonResponse(409, { detail: "stale meta.version: file built on 3, live is 7" });
    }
    throw new Error(`unexpected request: ${opts.method} ${url}`);
  });
  const client = new Client({ token: "fnp_x", baseUrl: BASE });
  await assert.rejects(
    () => client.functions.createFromYamlText("name: X\n"),
    (e) => e instanceof FnaiError && e.status === 409 && /stale meta.version/.test(e.detail));
  console.log("OK: a 409 from the push endpoint surfaces as FnaiError, not swallowed");
}

// ── getYaml: pulls the latest version's YAML by number, not by id order ────
{
  fakeFetch((url, opts) => {
    if (url === `${BASE}/api/functions/9/versions` && opts.method === "GET") {
      return jsonResponse(200, [{ id: 100, number: 3 }, { id: 55, number: 1 }, { id: 210, number: 2 }]);
    }
    if (url === `${BASE}/api/versions/100/yaml` && opts.method === "GET") {
      return textResponse(200, "name: Pulled Function\nmeta:\n  version: 3\n");
    }
    throw new Error(`unexpected request: ${opts.method} ${url}`);
  });
  const client = new Client({ token: "fnp_x", baseUrl: BASE });
  const text = await client.functions.getYaml(9);
  assert.match(text, /name: Pulled Function/);
  console.log("OK: getYaml resolves the highest version.number, not the highest id or list order");
}

// ── getYaml: a function with no versions raises a 404 FnaiError ────────────
{
  fakeFetch((url, opts) => {
    if (url === `${BASE}/api/functions/3/versions` && opts.method === "GET") return jsonResponse(200, []);
    throw new Error(`unexpected request: ${opts.method} ${url}`);
  });
  const client = new Client({ token: "fnp_x", baseUrl: BASE });
  await assert.rejects(
    () => client.functions.getYaml(3),
    (e) => e instanceof FnaiError && e.status === 404 && /has no versions yet/.test(e.detail));
  console.log("OK: getYaml on a function with no versions raises FnaiError(404), not a crash");
}

// ── REQ-273: one full-format fixture POSTs as the file's exact bytes ────────
{
  const { readFileSync } = await import("node:fs");
  const { fileURLToPath } = await import("node:url");
  const { dirname, join } = await import("node:path");
  const fixturePath = join(dirname(fileURLToPath(import.meta.url)), "..", "fixtures", "full-function.yaml");
  const text = readFileSync(fixturePath, "utf8");
  const calls = fakeFetch((url, opts) => {
    if (url === `${BASE}/api/functions` && opts.method === "GET") {
      return jsonResponse(200, []);
    }
    if (url === `${BASE}/api/functions` && opts.method === "POST") {
      const body = JSON.parse(opts.body);
      return jsonResponse(200, { id: 11, slug: body.slug, name: body.name });
    }
    if (url === `${BASE}/api/functions/11/versions/yaml` && opts.method === "POST") {
      assert.equal(opts.headers["Content-Type"], "text/yaml");
      assert.equal(opts.body, text);
      return textResponse(201, JSON.stringify({ id: 910, number: 1 }));
    }
    throw new Error(`unexpected request: ${opts.method} ${url}`);
  });
  const client = new Client({ token: "fnp_x", baseUrl: BASE });
  const { fn, version } = await client.functions.createFromYamlText(text);
  assert.equal(fn.slug, "summarize_uploaded_contract");
  assert.equal(version.id, 910);
  const push = calls.find((c) => c.url.endsWith("/versions/yaml"));
  assert.ok(push, "YAML push never happened");
  assert.match(push.body, /max_calls: 12/);
  assert.match(push.body, /polarity: positive/);
  assert.match(push.body, /type: file/);
  assert.match(push.body, /per: caller/);
  console.log("OK: full-format golden fixture is posted verbatim (tools, examples, file, memory)");
}

// ── input validation: required fields, ids, and duplicate names/slugs ──────
{
  const client = new Client({ token: "fnp_x", baseUrl: BASE });

  fakeFetch(() => { throw new Error("should never reach the network"); });
  await assert.rejects(() => client.functions.create(
    { slug: "", name: "X", input_schema: {}, output_schema: {} }));
  console.log("OK: functions.create rejects an empty slug before any request");

  await assert.rejects(async () => client.functions.get(0));
  await assert.rejects(async () => client.functions.update(1, {}));
  console.log("OK: functions.get/update reject a bad id / an empty patch");

  fakeFetch((url, opts) => {
    if (url === `${BASE}/api/functions` && opts.method === "GET") {
      return jsonResponse(200, [{ id: 5, slug: "existing", name: "Existing" }]);
    }
    throw new Error(`unexpected request: ${opts.method} ${url}`);
  });
  await assert.rejects(() => client.functions.create(
    { slug: "existing", name: "X", input_schema: {}, output_schema: {} }));
  console.log("OK: functions.create rejects a slug that already exists in this space");

  fakeFetch(() => { throw new Error("should never reach the network"); });
  await assert.rejects(async () => client.eval.scores.create(1, { name: "x" }));
  await assert.rejects(async () => client.eval.scores.create(1, { config: {} }));
  await assert.rejects(async () => client.eval.scores.setGate(1, "yes"));
  await assert.rejects(async () => client.datasets.addRow(1, { expected: { x: 1 } }));
  console.log("OK: scores.create/setGate and datasets.addRow reject missing/bad fields");

  fakeFetch((url, opts) => {
    if (url === `${BASE}/api/me` && opts.method === "GET") {
      return jsonResponse(200, { spaces: [{ id: "s1", name: "Marketing" }] });
    }
    throw new Error(`unexpected request: ${opts.method} ${url}`);
  });
  await assert.rejects(() => client.spaces.create({ name: "" }));
  await assert.rejects(() => client.spaces.create({ name: "Marketing" }));
  console.log("OK: spaces.create rejects an empty name and a name that already exists");
}

// ── useSpace: opt-in, validated, no header until explicitly set ────────────
{
  const client = new Client({ token: "fnp_x", baseUrl: BASE });

  // never calling useSpace() -> no X-Space header, same as before this
  // feature existed (backward compatibility with every existing caller)
  const calls = fakeFetch((url, opts) => {
    if (url === `${BASE}/api/functions` && opts.method === "GET") return jsonResponse(200, []);
    throw new Error(`unexpected request: ${opts.method} ${url}`);
  });
  await client.functions.list();
  assert.ok(!("X-Space" in calls[0].headers), "X-Space must not be sent by default");
  assert.equal(client.currentSpace, null);
  console.log("OK: never calling useSpace() sends no X-Space header");

  // an unknown space name is rejected against a fresh GET /api/me
  fakeFetch((url, opts) => {
    if (url === `${BASE}/api/me` && opts.method === "GET") {
      return jsonResponse(200, { spaces: [{ id: "s1", name: "Marketing" }] });
    }
    throw new Error(`unexpected request: ${opts.method} ${url}`);
  });
  await assert.rejects(() => client.useSpace("Sales"), (e) => e instanceof SpaceNotFoundError);
  assert.equal(client.currentSpace, null);
  console.log("OK: useSpace() rejects a name that doesn't exist for this account");

  // a real name is accepted and sent on every later request
  fakeFetch((url, opts) => {
    if (url === `${BASE}/api/me` && opts.method === "GET") {
      return jsonResponse(200, { spaces: [{ id: "s1", name: "Marketing" }] });
    }
    throw new Error(`unexpected request: ${opts.method} ${url}`);
  });
  await client.useSpace("Marketing");
  assert.equal(client.currentSpace, "Marketing");
  const afterCalls = fakeFetch((url, opts) => {
    if (url === `${BASE}/api/functions` && opts.method === "GET") return jsonResponse(200, []);
    throw new Error(`unexpected request: ${opts.method} ${url}`);
  });
  await client.functions.list();
  assert.equal(afterCalls[0].headers["X-Space"], "Marketing");
  console.log("OK: after useSpace(), the X-Space header is sent on every later request");
}

console.log("\nAll checks passed.");
