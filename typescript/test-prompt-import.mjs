import assert from "node:assert/strict";
import { parsePromptFile, createFunctionFromDoc, createFunction, PromptFileError } from "./dist/fnai.js";

// ── parsePromptFile: JSON ────────────────────────────────────────────────
{
  const fs = await import("node:fs/promises");
  const path = "/tmp/fnai-ts-test-prompt.json";
  await fs.writeFile(path, JSON.stringify({
    role: "You are a helpful assistant.",
    instructions: ["Do the first thing.", "Do the second thing."],
    inputs: { website_url: "string" },
    outputs: { confidence: { type: "number", required: false } },
  }));
  const doc = await parsePromptFile(path);
  assert.deepEqual(doc.sections, [
    { type: "role", content: "You are a helpful assistant." },
    { type: "instructions", title: "Instructions", items: ["Do the first thing.", "Do the second thing."] },
  ]);
  assert.deepEqual(doc.input_schema, {
    type: "object", required: ["website_url"],
    properties: { website_url: { type: "string" } },
  });
  assert.deepEqual(doc.output_schema, {
    type: "object", required: [],
    properties: { confidence: { type: "number" } },
  });
  console.log("OK: parses a full JSON document");
}

// ── missing role raises PromptFileError ──────────────────────────────────
{
  const fs = await import("node:fs/promises");
  const path = "/tmp/fnai-ts-test-bad.json";
  await fs.writeFile(path, JSON.stringify({ instructions: ["x"] }));
  try {
    await parsePromptFile(path);
    assert.fail("should have thrown");
  } catch (e) {
    assert.ok(e instanceof PromptFileError);
    assert.match(e.message, /role is required/);
  }
  console.log("OK: missing role raises PromptFileError");
}

// ── unknown top-level key names it ───────────────────────────────────────
{
  const fs = await import("node:fs/promises");
  const path = "/tmp/fnai-ts-test-unknown.json";
  await fs.writeFile(path, JSON.stringify({ role: "hi", output_format: [] }));
  try {
    await parsePromptFile(path);
    assert.fail("should have thrown");
  } catch (e) {
    assert.match(e.message, /output_format/);
  }
  console.log("OK: unknown top-level key names it");
}

// ── real YAML file (the actual bb_p1_website_extraction.yaml) ───────────
{
  const doc = await parsePromptFile("/home/emp-0063/Desktop/bb_p1_website_extraction.yaml");
  assert.equal(doc.sections.length, 4);
  assert.deepEqual(Object.keys(doc.input_schema.properties), ["website_url", "fetched_pages"]);
  assert.deepEqual(Object.keys(doc.output_schema.properties),
    ["company_name", "company_description", "industry", "key_sales_message_candidates", "confidence"]);
  console.log("OK: parses the real bb_p1_website_extraction.yaml file");
}

// ── createFunctionFromDoc: never reads a file, only calls the client ────
{
  const calls = { create: [], createVersion: [] };
  const fakeClient = {
    functions: {
      create: async (body) => { calls.create.push(body); return { id: 60, slug: body.slug }; },
      createVersion: async (fnId, body) => { calls.createVersion.push([fnId, body]); return { id: 200, number: 1 }; },
    },
  };
  const doc = { sections: [{ type: "role", content: "hi" }],
                input_schema: { type: "object", properties: { a: { type: "string" } } } };
  const { fn, version } = await createFunctionFromDoc(fakeClient, { slug: "s", name: "N", doc });
  assert.equal(fn.id, 60);
  assert.equal(version.id, 200);
  assert.deepEqual(calls.create[0].input_schema, doc.input_schema);
  assert.deepEqual(calls.create[0].output_schema, { type: "object", properties: {} });
  assert.deepEqual(calls.createVersion[0][1].snapshot.sections, doc.sections);
  console.log("OK: createFunctionFromDoc uses only the client, not the filesystem");
}

// ── createFunction: manual path, sections/schemas passed directly ───────
{
  const calls = { create: [], createVersion: [] };
  const fakeClient = {
    functions: {
      create: async (body) => { calls.create.push(body); return { id: 61, slug: body.slug }; },
      createVersion: async (fnId, body) => { calls.createVersion.push([fnId, body]); return { id: 201, number: 1 }; },
    },
  };
  const sections = [{ type: "role", content: "manual" }];
  const inputSchema = { type: "object", properties: { x: { type: "string" } } };
  const { fn } = await createFunction(fakeClient, { slug: "manual", name: "Manual", sections, inputSchema });
  assert.equal(fn.id, 61);
  assert.deepEqual(calls.create[0].input_schema, inputSchema);
  assert.deepEqual(calls.createVersion[0][1].snapshot.sections, sections);
  console.log("OK: createFunction takes sections/schemas directly, no doc needed");
}

console.log("\nAll checks passed.");
