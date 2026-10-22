// The function-definition rules (function-dsl.md, "The field grammar"), shared by the
// step-by-step builder: checking a field, turning fields into the stored schema and back,
// printing a function as YAML, listing what is missing, and testing a sample input.
// Pure functions, no network and no files.

export type Json = Record<string, unknown>;

/** A field or function breaks the definition rules. `problems` lists every issue found,
 *  so one call reports them all. */
export class DefinitionError extends Error {
  problems: string[];
  constructor(problems: string[]) {
    super(problems.join("; "));
    this.name = "DefinitionError";
    this.problems = [...problems];
  }
}

const TYPES_IN = ["string", "number", "integer", "boolean", "object", "array", "file"];
const TYPES_OUT = ["string", "number", "integer", "boolean", "object", "array"];
const COMMON_KEYS = ["type", "required", "description", "sensitive", "enum", "minimum", "maximum",
  "format", "pattern", "items", "min_items", "max_items", "fields"];
const INPUT_ONLY_KEYS = ["derived", "media", "formats"];
const OUTPUT_ONLY_KEYS = ["length", "count"];
export const FORMATS = ["iso8601", "date", "time", "datetime", "email", "uuid", "url"];
export const MEDIA = ["document", "image", "audio", "video", "data"];
export const EFFECTS = ["read_only", "idempotent", "side_effecting"];
export const MEMORY_SCOPES = ["caller", "space", "organisation"];

const isRecord = (v: unknown): v is Json => typeof v === "object" && v !== null && !Array.isArray(v);
const isNum = (v: unknown): v is number => typeof v === "number" && !Number.isNaN(v);
const isInt = (v: unknown): v is number => isNum(v) && Number.isInteger(v);
const has = (o: Json, k: string) => Object.prototype.hasOwnProperty.call(o, k);
const show = (v: unknown) => (typeof v === "string" ? `'${v}'` : JSON.stringify(v));

/** The closest word in `options` to `word`, or null (a stand-in for Python's difflib). */
function closest(word: string, options: string[]): string | null {
  const dist = (a: string, b: string): number => {
    const d: number[][] = Array.from({ length: a.length + 1 }, (_, i) => [i, ...Array(b.length).fill(0)]);
    for (let j = 0; j <= b.length; j++) d[0]![j] = j;
    for (let i = 1; i <= a.length; i++) {
      for (let j = 1; j <= b.length; j++) {
        d[i]![j] = Math.min(d[i - 1]![j]! + 1, d[i]![j - 1]! + 1,
          d[i - 1]![j - 1]! + (a[i - 1] === b[j - 1] ? 0 : 1));
      }
    }
    return d[a.length]![b.length]!;
  };
  let best: string | null = null;
  let bestScore = 0.6;
  for (const o of options) {
    const score = 1 - dist(word, o) / Math.max(word.length, o.length);
    if (score >= bestScore) { best = o; bestScore = score; }
  }
  return best;
}
export { closest as closestMatch };

function checkSoft(label: string, key: string, value: unknown, problems: string[]): void {
  if (!isRecord(value)) {
    problems.push(`${label}: '${key}' must be like {unit: tokens, target: 60, range: [40, 80]}`);
    return;
  }
  for (const k of Object.keys(value)) {
    if (!["unit", "target", "range"].includes(k)) {
      problems.push(`${label}: '${key}' has unknown key '${k}' (allowed: unit, target, range)`);
    }
  }
  if (has(value, "unit") && !["tokens", "chars"].includes(value.unit as string)) {
    problems.push(`${label}: '${key}.unit' must be 'tokens' or 'chars', got ${show(value.unit)}`);
  }
  if (has(value, "target") && !isNum(value.target)) problems.push(`${label}: '${key}.target' must be a number`);
  if (has(value, "range")) {
    const r = value.range;
    if (!(Array.isArray(r) && r.length === 2 && r.every(isNum) && (r[0] as number) <= (r[1] as number))) {
      problems.push(`${label}: '${key}.range' must be [low, high] with low <= high`);
    }
  }
}

const ENUM_OK: Record<string, (v: unknown) => boolean> = {
  string: (v) => typeof v === "string",
  number: isNum,
  integer: isInt,
  boolean: (v) => typeof v === "boolean",
};

/** Every problem with one field descriptor, per the DSL field grammar. `side` is "input" or
 *  "output" (some keys are legal on one side only). */
export function checkField(side: "input" | "output", name: string, spec: unknown): string[] {
  const label = `${side} '${name}'`;
  if (!isRecord(spec)) return [`${label}: must be an object like {type: "string", required: true}`];
  const problems: string[] = [];
  const legal = [...COMMON_KEYS, ...(side === "input" ? INPUT_ONLY_KEYS : OUTPUT_ONLY_KEYS)];
  const wrongSide = side === "input" ? OUTPUT_ONLY_KEYS : INPUT_ONLY_KEYS;
  for (const key of Object.keys(spec)) {
    if (legal.includes(key)) continue;
    if (wrongSide.includes(key)) {
      problems.push(`${label}: '${key}' is only valid on ${side === "input" ? "output" : "input"} fields`);
    } else {
      const near = closest(key, legal);
      problems.push(`${label}: unknown key '${key}'${near ? ` — did you mean '${near}'?` : ""}`);
    }
  }
  const types = side === "input" ? TYPES_IN : TYPES_OUT;
  const t = spec.type as string;
  if (!types.includes(t)) {
    problems.push(`${label}: type ${show(t)} is not valid${t === "file" ? " ('file' is input-only)" : ""}; ` +
      `use one of ${types.join(", ")}`);
    return problems;
  }
  for (const flag of ["required", "sensitive"]) {
    if (has(spec, flag) && typeof spec[flag] !== "boolean") problems.push(`${label}: '${flag}' must be true or false`);
  }
  if (has(spec, "description") && typeof spec.description !== "string") problems.push(`${label}: 'description' must be text`);
  if (spec.derived) {
    problems.push(`${label}: derived fields are filled by a preprocessor, which the builder can't author yet — ` +
      `define this function in YAML instead`);
  }
  const numeric = t === "number" || t === "integer";
  for (const k of ["minimum", "maximum"]) {
    if (has(spec, k)) {
      if (!numeric) problems.push(`${label}: '${k}' needs type number or integer, got ${t}`);
      else if (!isNum(spec[k])) problems.push(`${label}: '${k}' must be a number`);
    }
  }
  if (numeric && isNum(spec.minimum) && isNum(spec.maximum) && spec.minimum > spec.maximum) {
    problems.push(`${label}: minimum ${spec.minimum} is greater than maximum ${spec.maximum}`);
  }
  if (has(spec, "format")) {
    if (t !== "string") problems.push(`${label}: 'format' needs type string, got ${t}`);
    else if (!FORMATS.includes(spec.format as string)) {
      problems.push(`${label}: format ${show(spec.format)} is not valid; use one of ${FORMATS.join(", ")}`);
    }
  }
  if (has(spec, "pattern")) {
    if (t !== "string") problems.push(`${label}: 'pattern' needs type string, got ${t}`);
    else {
      try { new RegExp(spec.pattern as string); }
      catch (e) { problems.push(`${label}: 'pattern' is not a valid regular expression (${(e as Error).message})`); }
    }
  }
  if (has(spec, "enum")) {
    const e = spec.enum;
    const ok = ENUM_OK[t];
    if (!Array.isArray(e) || e.length === 0) problems.push(`${label}: 'enum' must be a non-empty list`);
    else if (!ok) problems.push(`${label}: 'enum' can't be used with type ${t}`);
    else {
      const bad = e.filter((v) => !ok(v));
      if (bad.length) problems.push(`${label}: enum values ${JSON.stringify(bad)} are not of type ${t}`);
    }
  }
  if (has(spec, "length")) {
    if (t !== "string") problems.push(`${label}: 'length' needs type string, got ${t}`);
    checkSoft(label, "length", spec.length, problems);
  }
  if (has(spec, "count")) {
    if (t !== "array") problems.push(`${label}: 'count' needs type array, got ${t}`);
    checkSoft(label, "count", spec.count, problems);
  }
  if (t === "array") {
    if (!has(spec, "items")) problems.push(`${label}: arrays need 'items', e.g. items: {type: 'string'}`);
    else problems.push(...checkField(side, `${name}[]`, spec.items));
  } else if (has(spec, "items")) {
    problems.push(`${label}: 'items' needs type array, got ${t}`);
  }
  for (const k of ["min_items", "max_items"]) {
    if (has(spec, k)) {
      if (t !== "array") problems.push(`${label}: '${k}' needs type array, got ${t}`);
      else if (!isInt(spec[k]) || (spec[k] as number) < 0) problems.push(`${label}: '${k}' must be a whole number, 0 or more`);
    }
  }
  if (isInt(spec.min_items) && isInt(spec.max_items) && spec.min_items > spec.max_items) {
    problems.push(`${label}: min_items ${spec.min_items} is greater than max_items ${spec.max_items}`);
  }
  if (has(spec, "fields")) {
    if (t !== "object") problems.push(`${label}: 'fields' needs type object, got ${t}`);
    else if (!isRecord(spec.fields)) problems.push(`${label}: 'fields' must be an object of field descriptors`);
    else for (const [sub, subSpec] of Object.entries(spec.fields)) problems.push(...checkField(side, `${name}.${sub}`, subSpec));
  }
  if (t === "file" && !has(spec, "media")) {
    problems.push(`${label}: file fields need media, one of ${MEDIA.join(", ")}`);
  }
  if (has(spec, "media")) {
    if (t !== "file") problems.push(`${label}: 'media' needs type file, got ${t}`);
    else if (!MEDIA.includes(spec.media as string)) {
      problems.push(`${label}: media ${show(spec.media)} is not valid; use one of ${MEDIA.join(", ")}`);
    }
  }
  if (has(spec, "formats")) {
    const f = spec.formats;
    if (t !== "file") problems.push(`${label}: 'formats' needs type file, got ${t}`);
    else if (!(Array.isArray(f) && f.length > 0 && f.every((x) => typeof x === "string"))) {
      problems.push(`${label}: 'formats' must be a list like ['pdf', 'docx']`);
    }
  }
  return problems;
}

/** One DSL field -> the JSON-schema property the server stores (mirrors the server's own
 *  projection): hard gates as JSON Schema, soft targets under x-soft, files as a string handle. */
export function projectField(spec: Json): Json {
  const out: Json = { type: spec.type === "file" ? "string" : spec.type };
  for (const k of ["description", "enum", "minimum", "maximum", "format", "pattern"]) {
    if (has(spec, k)) out[k] = spec[k];
  }
  if (has(spec, "min_items")) out.minItems = spec.min_items;
  if (has(spec, "max_items")) out.maxItems = spec.max_items;
  if (has(spec, "items")) out.items = projectField(spec.items as Json);
  if (has(spec, "fields")) {
    const fields = spec.fields as Record<string, Json>;
    out.properties = Object.fromEntries(Object.entries(fields).map(([n, s]) => [n, projectField(s)]));
    const req = Object.entries(fields).filter(([, s]) => s.required).map(([n]) => n);
    if (req.length) out.required = req;
  }
  if (has(spec, "media")) {
    out["x-media"] = spec.media;
    out["x-delivery"] = spec.media === "data" ? "text" : "native";
  }
  if (has(spec, "formats")) out["x-formats"] = spec.formats;
  const soft: Json = {};
  for (const k of ["length", "count"]) if (has(spec, k)) soft[k] = spec[k];
  if (Object.keys(soft).length) out["x-soft"] = soft;
  return out;
}

/** The reverse of projectField, for showing a stored schema as DSL. */
export function unprojectField(prop: Json, required: boolean): Json {
  const spec: Json = { type: (prop.type as string) ?? "string" };
  if (has(prop, "x-media") || has(prop, "x-formats")) spec.type = "file";
  if (required) spec.required = true;
  for (const k of ["description", "enum", "minimum", "maximum", "format", "pattern"]) {
    if (has(prop, k)) spec[k] = prop[k];
  }
  if (has(prop, "minItems")) spec.min_items = prop.minItems;
  if (has(prop, "maxItems")) spec.max_items = prop.maxItems;
  if (isRecord(prop.items)) spec.items = unprojectField(prop.items, false);
  if (isRecord(prop.properties)) {
    const req = new Set((prop.required as string[]) ?? []);
    spec.fields = Object.fromEntries(Object.entries(prop.properties as Record<string, Json>)
      .map(([n, p]) => [n, unprojectField(p, req.has(n))]));
  }
  if (has(prop, "x-media")) spec.media = prop["x-media"];
  if (has(prop, "x-formats")) spec.formats = prop["x-formats"];
  Object.assign(spec, (prop["x-soft"] as Json) ?? {});
  return spec;
}

/** A stored JSON-schema object as {field name: DSL field}. */
export function schemaFields(schema: Json | null | undefined): Record<string, Json> {
  const s = schema ?? {};
  const req = new Set((s.required as string[]) ?? []);
  const out: Record<string, Json> = {};
  for (const [name, prop] of Object.entries((s.properties as Record<string, unknown>) ?? {})) {
    if (!isRecord(prop)) continue;
    // older SDK versions stored `required` inside the property itself
    out[name] = unprojectField(prop, req.has(name) || prop.required === true);
  }
  return out;
}

// ── tool options, guards, examples ───────────────────────────────────────────
const GUARD_TOOL = "[a-z_][a-z0-9_]*";
const GUARD_KEY = "[a-zA-Z_][a-zA-Z0-9_]*";
const GUARD_REF = `(?:${GUARD_TOOL}\\.(?:called|count|returned(?:\\.${GUARD_KEY})+)|input(?:\\.${GUARD_KEY})+)`;
const GUARD_TERM = new RegExp(`^\\s*(?<ref>${GUARD_REF})\\s*(?<op>==|!=|>=|<=|>|<)\\s*` +
  `(?<lit>true|false|-?\\d+\\.\\d+|-?\\d+|"[^"]*")\\s*$`);

/** Check a tool's `requires` guard against the definition's grammar: terms joined by &&, each
 *  `ref op literal`. With `tools`/`inputs` given, also checks every reference points at
 *  something this function declares. */
export function guardProblems(tool: string, requires: string, tools?: Set<string>, inputs?: Set<string>): string[] {
  const problems: string[] = [];
  for (const raw of requires.split("&&")) {
    const term = raw.trim();
    const m = GUARD_TERM.exec(term);
    if (!m || !m.groups) {
      problems.push(`tool '${tool}': guard term ${JSON.stringify(term)} does not parse — use ` +
        `\`<tool>.called|count|returned.<field>\` or \`input.<field>\`, compared with == != > >= < <= ` +
        `to true/false, a number or "text"; join terms with &&`);
      continue;
    }
    const ref = m.groups.ref!;
    const head = ref.split(".")[0]!;
    if (head === "input") {
      const name = ref.split(".")[1]!;
      if (inputs && !inputs.has(name)) problems.push(`tool '${tool}': guard uses input '${name}', which this function doesn't declare`);
    } else if (tools && !tools.has(head)) {
      problems.push(`tool '${tool}': guard uses tool '${head}', which this function doesn't declare`);
    }
  }
  return problems;
}

/** The definition's rules for one example: a positive one needs input and output; a negative
 *  one needs output (or badValue) and a note. */
export function exampleProblems(polarity: unknown, input: unknown, output: unknown, badValue: unknown, note: unknown): string[] {
  if (polarity !== "positive" && polarity !== "negative") {
    return [`example: polarity must be 'positive' or 'negative', got ${show(polarity)}`];
  }
  const problems: string[] = [];
  for (const [label, v] of [["input", input], ["output", output]] as const) {
    if (v !== undefined && v !== null && !isRecord(v)) problems.push(`example: ${label} must be an object of field values`);
  }
  if (polarity === "positive") {
    if (input == null || output == null) problems.push("a positive example needs both input and output");
  } else {
    if (output == null && badValue == null) problems.push("a negative example needs output or badValue");
    if (!(typeof note === "string" && note.trim())) problems.push("a negative example needs a note saying what is wrong with it");
  }
  return problems;
}

// ── testing a sample input ───────────────────────────────────────────────────
const FORMAT_RE: Record<string, RegExp> = {
  email: /^[^@\s]+@[^@\s]+\.[^@\s]+$/,
  uuid: /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/,
  url: /^https?:\/\/[^\s/$.?#][^\s]*$/,
  date: /^\d{4}-\d{2}-\d{2}$/,
  time: /^\d{2}:\d{2}(:\d{2}(\.\d+)?)?$/,
  datetime: /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$/,
  iso8601: /^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$/,
};

const jsType = (v: unknown) => (v === null ? "null" : Array.isArray(v) ? "array" : typeof v);

/** Check a sample against a stored input schema, the way the server checks a real call:
 *  required, type, minimum/maximum, enum, pattern, format and item counts. */
export function validateSample(schema: Json | null | undefined, sample: Json): string[] {
  const problems: string[] = [];
  const props = ((schema?.properties as Record<string, Json>) ?? {});
  for (const name of ((schema?.required as string[]) ?? [])) {
    if (!has(sample, name)) problems.push(`missing required field '${name}'`);
  }
  const check = (label: string, spec: Json, value: unknown): void => {
    if (value === null || value === undefined) return;
    const t = spec.type as string;
    const okType: Record<string, boolean> = {
      boolean: typeof value === "boolean", number: isNum(value), integer: isInt(value),
      string: typeof value === "string", object: isRecord(value), array: Array.isArray(value),
    };
    if (t in okType && !okType[t]) {
      problems.push(`field '${label}' must be a ${t}, got ${jsType(value)}`);
      return;
    }
    if (isNum(value)) {
      if (isNum(spec.minimum) && value < spec.minimum) problems.push(`field '${label}' must be at least ${spec.minimum}, got ${value}`);
      if (isNum(spec.maximum) && value > spec.maximum) problems.push(`field '${label}' must be at most ${spec.maximum}, got ${value}`);
    }
    if (Array.isArray(spec.enum) && !spec.enum.includes(value)) {
      problems.push(`field '${label}' must be one of ${JSON.stringify(spec.enum)}, got ${show(value)}`);
    }
    if (typeof value === "string") {
      if (typeof spec.pattern === "string" && !new RegExp(spec.pattern).test(value)) {
        problems.push(`field '${label}' does not match the required pattern`);
      }
      const re = FORMAT_RE[spec.format as string];
      if (re && !re.test(value)) problems.push(`field '${label}' is not a valid ${spec.format}`);
    }
    if (Array.isArray(value)) {
      if (isNum(spec.minItems) && value.length < spec.minItems) problems.push(`field '${label}' must have at least ${spec.minItems} items, got ${value.length}`);
      if (isNum(spec.maxItems) && value.length > spec.maxItems) problems.push(`field '${label}' must have at most ${spec.maxItems} items, got ${value.length}`);
      if (isRecord(spec.items)) value.forEach((v, i) => check(`${label}[${i}]`, spec.items as Json, v));
    }
  };
  for (const [name, value] of Object.entries(sample)) {
    const spec = props[name];
    if (isRecord(spec)) check(name, spec, value);
  }
  return problems;
}

// ── printing a function as YAML ──────────────────────────────────────────────
export function yamlScalar(v: unknown): string {
  if (v === null || v === undefined) return "null";
  if (typeof v === "boolean") return v ? "true" : "false";
  if (typeof v === "number") return String(v);
  const s = String(v);
  const plain = /^[A-Za-z_][A-Za-z0-9_ .-]*$/.test(s) &&
    !["true", "false", "null", "yes", "no", "on", "off", "~"].includes(s.toLowerCase());
  return plain ? s : JSON.stringify(s);
}

export function yamlFlow(v: unknown): string {
  if (isRecord(v)) {
    const entries = Object.entries(v);
    return entries.length ? "{ " + entries.map(([k, x]) => `${k}: ${yamlFlow(x)}`).join(", ") + " }" : "{}";
  }
  if (Array.isArray(v)) return "[" + v.map(yamlFlow).join(", ") + "]";
  return yamlScalar(v);
}

export const MANAGED_TOP = ["name", "description", "input", "output", "implementation", "rubric", "memory", "meta"];
export const MANAGED_IMPL = ["execution", "flow", "sections", "tools"];

/** The parts of a saved version the builder has no call for — `needs`, `preprocessors` and anything else
 *  it does not manage — so they travel with the function instead of being dropped. */
export function extrasOf(snap: Json | null | undefined): { top: Json; impl: Json } {
  const s = snap ?? {};
  const impl = isRecord(s.implementation) ? s.implementation : {};
  return {
    top: Object.fromEntries(Object.entries(s).filter(([k]) => !MANAGED_TOP.includes(k))),
    impl: Object.fromEntries(Object.entries(impl).filter(([k]) => !MANAGED_IMPL.includes(k))),
  };
}

function yamlExtras(lines: string[], extras: Json, indent: string): void {
  for (const [key, value] of Object.entries(extras)) {
    if (Array.isArray(value) && value.length && value.every(isRecord)) {
      lines.push(`${indent}${key}:`);
      lines.push(...value.map((x) => `${indent}  - ${yamlFlow(x)}`));
    } else {
      lines.push(`${indent}${key}: ${yamlFlow(value)}`);
    }
  }
}

export interface YamlParts {
  extraTop?: Json;
  extraImpl?: Json;
  name: string;
  description?: string;
  input: Record<string, Json>;
  output: Record<string, Json>;
  mode?: string;
  flow?: string[];
  rubric?: string[];
  memory?: Json | null;
  sections?: Json | null;
  tools?: Json[];
}

/** The function as YAML in the definition format. */
export function renderYaml(p: YamlParts): string {
  const lines = [`name: ${yamlScalar(p.name)}`];
  if (p.description) lines.push(`description: ${yamlScalar(p.description)}`);
  for (const section of ["input", "output"] as const) {
    const fields = p[section];
    const names = Object.keys(fields);
    if (!names.length) { lines.push(`${section}: {}`); continue; }
    lines.push(`${section}:`);
    const width = Math.max(...names.map((n) => n.length)) + 1;
    for (const n of names) lines.push(`  ${(n + ":").padEnd(width)} ${yamlFlow(fields[n])}`);
  }
  const sections = p.sections;
  if (sections !== null && sections !== undefined) {
    lines.push("implementation:");
    lines.push(`  execution: { mode: ${p.mode ?? "single_shot"} }`);
    yamlExtras(lines, p.extraImpl ?? {}, "  ");
    if (p.flow?.length) {
      lines.push("  flow:");
      lines.push(...p.flow.map((x) => `    - ${JSON.stringify(x)}`));
    }
    const body: string[] = [];
    if (sections.role) body.push(`    role: ${JSON.stringify(sections.role)}`);
    for (const key of ["definitions", "constraints", "edge_cases"]) {
      const list = sections[key] as string[] | undefined;
      if (list?.length) {
        body.push(`    ${key}:`);
        body.push(...list.map((x) => `      - ${JSON.stringify(x)}`));
      }
    }
    const examples = sections.examples as Json[] | undefined;
    if (examples?.length) {
      body.push("    examples:");
      body.push(...examples.map((x) => `      - ${yamlFlow(x)}`));
    }
    if (body.length) { lines.push("  sections:"); lines.push(...body); }
    if (p.tools?.length) {
      lines.push("  tools:");
      lines.push(...p.tools.map((t) => `    - ${yamlFlow(t)}`));
    }
  }
  if (p.rubric?.length) {
    lines.push("rubric:");
    lines.push(...p.rubric.map((x) => `  - ${JSON.stringify(x)}`));
  }
  if (p.memory && Object.keys(p.memory).length) {
    lines.push("memory:");
    for (const [k, v] of Object.entries(p.memory)) lines.push(`  ${k}: ${yamlFlow(v)}`);
  }
  yamlExtras(lines, p.extraTop ?? {}, "");
  return lines.join("\n") + "\n";
}

/** What the function still needs: `required` (the server needs it) and `recommended`. */
export function missingLists(inputs: Record<string, Json>, outputs: Record<string, Json>,
                             flow: string[], sections: Json): { required: string[]; recommended: string[] } {
  const required: string[] = [];
  const recommended: string[] = [];
  const outs = Object.values(outputs);
  if (!outs.length) required.push("no output fields — addOutput(...)");
  else if (!outs.some((f) => f.required)) {
    required.push("no output field is marked required (required: true) — the function has no valid stopping state");
  }
  if (!flow.length) required.push("no steps — addStep(...) (the server needs at least one)");
  if (!sections.role) recommended.push("no prompt — addPrompt(...)");
  if (!Object.keys(inputs).length) recommended.push("no input fields — addInput(...)");
  if (!(sections.examples as Json[] | undefined)?.length) recommended.push("no examples — addExample(...) (they improve accuracy)");
  if (!(sections.constraints as string[] | undefined)?.length && !(sections.edge_cases as string[] | undefined)?.length) {
    recommended.push("no constraints or edge cases — addSection('constraints', ...)");
  }
  return { required, recommended };
}

/** The printed form: the YAML, then what is missing and what is recommended. */
export function describe(yaml: string, m: { required: string[]; recommended: string[] }): string {
  let out = yaml + "\n";
  out += m.required.length
    ? "# Missing (required):\n" + m.required.map((i) => `#  - ${i}\n`).join("")
    : "# Missing (required): nothing\n";
  if (m.recommended.length) out += "# Recommended:\n" + m.recommended.map((i) => `#  - ${i}\n`).join("");
  return out;
}

// ── comparing experiments (what the Comparisons screen shows) ────────────────
export const DIMENSIONS: [string, string][] = [
  ["outcome_quality", "Outcome quality"], ["cost", "Cost & latency"], ["safety", "Safety"],
  ["stability", "Stability"], ["robustness", "Robustness"],
];

/** The trust figures for one experiment (mirrors the app): per-dimension pass rate, their mean as
 *  a 0-100 trust score, and the overall check pass rate. Pooled across models. */
export function trustOf(results: Json | null | undefined): {
  dims: Record<string, number | null>; trust: number; passPct: number | null; checks: Record<string, number>;
} {
  const r = results ?? {};
  const checks = ((r.checks as Json[]) ?? []);
  const cons = (r.consistency as Json) ?? {};
  const dims: Record<string, number | null> = {};
  for (const [key] of DIMENSIONS) {
    const cs = checks.filter((c) => ((c.dimension as string) || "outcome_quality") === key).map((c) => c.pass_rate as number);
    let score: number | null = cs.length ? cs.reduce((a, b) => a + b, 0) / cs.length : null;
    if (key === "stability" && cons.available) {
      const cScore = ((cons.overall as number) ?? 0) / 100;
      score = score === null ? cScore : (score + cScore) / 2;
    }
    if (key === "cost" && score === null) score = 1;
    dims[key] = score;
  }
  const measured = Object.values(dims).filter((v): v is number => v !== null);
  const trust = measured.length ? Math.round((measured.reduce((a, b) => a + b, 0) / measured.length) * 100) : 0;
  const passPct = checks.length
    ? Math.round((checks.reduce((a, c) => a + (c.pass_rate as number), 0) / checks.length) * 100) : null;
  return { dims, trust, passPct, checks: Object.fromEntries(checks.map((c) => [c.check_name as string, (c.pass_rate as number) * 100])) };
}

/** One comparison row: the values per experiment and which index is best (null when fewer than
 *  two values differ — nothing to call a winner). */
export function comparisonRow(metric: string, values: (number | null)[], higherBetter = true):
    { metric: string; values: (number | null)[]; best: number | null } {
  const defined = values.filter((v): v is number => v !== null);
  let best: number | null = null;
  if (defined.length > 1 && Math.max(...defined) !== Math.min(...defined)) {
    const target = higherBetter ? Math.max(...defined) : Math.min(...defined);
    best = values.indexOf(target);
  }
  return { metric, values, best };
}
