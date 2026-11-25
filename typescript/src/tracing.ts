/**
 * Tracing for code that runs on YOUR servers (FNA-REQ-532) — mirrors sdk/python's trace helpers.
 *
 *     import { initTracing, trace, traced, wrapOpenAI } from "fnai";
 *     initTracing({ apiKey: "fnk_…" });                     // or set FNAI_API_KEY; the key names the function
 *     const openai = wrapOpenAI(new OpenAI());              // every model call becomes an LLM step
 *     const search = traced(async (query: string) => …);    // every call becomes a tool step
 *
 *     const answer = await trace({ question }, async (t) => {   // one call of your agent
 *       const a = await runAgent(question);
 *       t.output({ answer: a });
 *       return a;
 *     });
 *
 * Fail-open: a problem in tracing (no network, a refused trace, a bug here) is never thrown into your code, never
 * slows it down (traces go out from a background queue) and never changes what it returns. Turning the network off
 * loses traces, nothing else.
 */

export interface TraceOptions {
  /** The function's slug — only for readability; the key already names the function. */
  function?: string;
  /** Link the trace to a prompt version you pulled from Functional AI. Leave out if you have none. */
  versionId?: number;
  /** A prompt from getPrompt(): names the version you used, so you need not copy its id. */
  prompt?: { versionId: number };
}

export interface TracingOptions {
  /** Your function's invoke key (fnk_…); defaults to FNAI_API_KEY. */
  apiKey?: string;
  baseUrl?: string;
  maxQueue?: number;
  timeoutMs?: number;
  /** With a space key (fns_…), which function the traces are for; or pass `function` to each trace(). */
  function?: string;
}

type Json = Record<string, unknown>;

const MAX_STEPS = 500;          // the server's limit per trace
const MAX_TEXT = 20_000;        // characters kept of any one string value
const RETRY_AFTER_MS = [500, 1000, 2000];
const RETRY_STATUSES = new Set([0, 408, 425, 429]);   // plus every 5xx

/** Overridable in tests. */
export const _tuning = { retryAfterMs: RETRY_AFTER_MS };

const DEFAULT_BASE_URL = "https://app.fnai.dev";

function env(name: string): string | undefined {
  const proc = (globalThis as { process?: { env?: Record<string, string | undefined> } }).process;
  return proc?.env?.[name];
}

/** Something JSON.stringify accepts, whatever you gave: containers are walked, long text is cut. Never throws. */
function jsonable(value: unknown, depth = 0): unknown {
  try {
    if (value === null || value === undefined) return null;
    if (typeof value === "string") return value.length <= MAX_TEXT ? value : value.slice(0, MAX_TEXT) + "…[cut]";
    if (typeof value === "number") return Number.isFinite(value) ? value : null;
    if (typeof value === "boolean") return value;
    if (typeof value === "bigint") return value.toString();
    if (typeof value === "function" || typeof value === "symbol") return String(value).slice(0, 200);
    if (depth > 8) return String(value).slice(0, 200);
    if (value instanceof Error) return `${value.name}: ${value.message}`;
    if (Array.isArray(value)) return value.slice(0, 200).map((v) => jsonable(v, depth + 1));
    if (value instanceof Map) return jsonable(Object.fromEntries(value), depth + 1);
    if (value instanceof Set) return jsonable([...value], depth + 1);
    if (typeof (value as { toJSON?: unknown }).toJSON === "function") return jsonable((value as { toJSON(): unknown }).toJSON(), depth + 1);
    const out: Json = {};
    for (const [k, v] of Object.entries(value as object).slice(0, 200)) out[k] = jsonable(v, depth + 1);
    return out;
  } catch {
    return "<unserialisable>";
  }
}

// ── which trace the running code is inside ────────────────────────────────────────────────────────────────────────
// AsyncLocalStorage follows `await` correctly even with concurrent requests. Where it is missing (a browser) the
// fallback is a plain "current" slot, right for sequential code only.
type Als = { run<T>(store: Trace, fn: () => T): T; getStore(): Trace | undefined };
let als: Als | null = null;
try {
  const mod = await import("node:async_hooks");
  als = new mod.AsyncLocalStorage<Trace>() as unknown as Als;
} catch {
  als = null;
}
let fallbackCurrent: Trace | undefined;

/** The trace the running code is inside, or undefined. */
export function currentTrace(): Trace | undefined {
  return als ? als.getStore() : fallbackCurrent;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => {
    const t = setTimeout(resolve, ms) as unknown as { unref?: () => void };
    t.unref?.();
  });
}

class Tracer {
  private queue: Json[] = [];
  private running = false;
  private pending = 0;
  private waiters: (() => void)[] = [];
  sent = 0;
  dropped = 0;
  private warned = 0;
  defaultFunction: string | undefined;
  get isSpaceKey(): boolean { return this.apiKey.startsWith("fns_"); }

  constructor(private apiKey: string, private base: string, private maxQueue: number, private timeoutMs: number) {}

  submit(trace: Json): void {
    try {
      if (this.queue.length >= this.maxQueue) {
        this.dropped++;
        return;
      }
      this.pending++;
      this.queue.push(trace);
      if (!this.running) void this.drain();
    } catch {
      /* fail-open */
    }
  }

  private finish(kind: "sent" | "dropped"): void {
    this[kind]++;
    this.pending--;
    if (this.pending <= 0) {
      const w = this.waiters.splice(0);
      for (const f of w) f();
    }
  }

  private async drain(): Promise<void> {
    this.running = true;
    try {
      for (let trace = this.queue.shift(); trace !== undefined; trace = this.queue.shift()) {
        try {
          await this.send(trace);
        } catch {
          this.finish("dropped");
        }
      }
    } finally {
      this.running = false;
    }
  }

  private async post(trace: Json): Promise<number> {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), this.timeoutMs) as unknown as { unref?: () => void };
    timer.unref?.();
    try {
      const res = await fetch(`${this.base}/v1/traces`, {
        method: "POST",
        headers: { Authorization: `Bearer ${this.apiKey}`, "Content-Type": "application/json", "User-Agent": "fnai-typescript" },
        body: JSON.stringify(trace),
        signal: ctl.signal,
      });
      return res.status;
    } catch {
      return 0;                                            // could not reach the server
    } finally {
      clearTimeout(timer as unknown as number);
    }
  }

  private async send(trace: Json): Promise<void> {
    const waits = _tuning.retryAfterMs;
    for (let attempt = 0; attempt <= waits.length; attempt++) {
      const status = await this.post(trace);
      if (status >= 200 && status < 300) return this.finish("sent");
      const retryable = RETRY_STATUSES.has(status) || status >= 500;
      if (retryable && attempt < waits.length) {
        await sleep(waits[attempt] ?? 0);
        continue;
      }
      if (!retryable && this.warned < 3) {                 // refused for good (bad key, bad trace): say so, a few times
        this.warned++;
        console.warn(`fnai tracing: a trace was refused (HTTP ${status}) and dropped`);
      }
      return this.finish("dropped");
    }
  }

  flush(timeoutMs: number): Promise<boolean> {
    if (this.pending <= 0) return Promise.resolve(true);
    return new Promise((resolve) => {
      // not unref'd: someone is awaiting flush(), so keep the process alive until it settles (or times out)
      const timer = setTimeout(() => resolve(false), timeoutMs);
      this.waiters.push(() => {
        clearTimeout(timer);
        resolve(true);
      });
    });
  }

  get waiting(): number {
    return Math.max(0, this.pending);
  }
}

let tracer: Tracer | null = null;
let exitHooked = false;

/** Switch tracing on. `apiKey` is your function's invoke key (fnk_…), or set FNAI_API_KEY; the key names the
 *  function. FNAI_TRACING=0 turns it off without touching your code. Without a key, trace() still works and simply
 *  sends nothing. */
export function initTracing(opts: TracingOptions = {}): void {
  const key = opts.apiKey || env("FNAI_API_KEY");
  const off = ["0", "off", "false", "no"].includes((env("FNAI_TRACING") ?? "1").trim().toLowerCase());
  if (!key || off) {
    tracer = null;
    return;
  }
  const base = (opts.baseUrl || env("FNAI_BASE_URL") || DEFAULT_BASE_URL).replace(/\/+$/, "");
  tracer = new Tracer(key, base, opts.maxQueue ?? 1000, opts.timeoutMs ?? 10_000);
  tracer.defaultFunction = opts.function;
  const proc = (globalThis as { process?: { once?: (e: string, f: () => void) => void } }).process;
  if (!exitHooked && proc?.once) {
    exitHooked = true;
    proc.once("beforeExit", () => void flushTraces(5000));
  }
}

/** Wait (up to timeoutMs) for queued traces to go out — await it before a short script or a serverless handler
 *  ends. True when nothing is left to send. */
export async function flushTraces(timeoutMs = 5000): Promise<boolean> {
  return tracer ? tracer.flush(timeoutMs) : true;
}

/** { sent, dropped, waiting } — how tracing is doing, for a health check or a test. */
export function traceStats(): { sent: number; dropped: number; waiting: number } {
  return tracer ? { sent: tracer.sent, dropped: tracer.dropped, waiting: tracer.waiting } : { sent: 0, dropped: 0, waiting: 0 };
}

/** One recorded call. Add the answer with t.output(...). */
export class Trace {
  readonly traceId: string;
  private _input: unknown;
  private _output: unknown = undefined;
  private _error: string | null = null;
  private _cost = 0;
  private readonly steps: Json[] = [];
  private readonly t0 = performance.now();

  constructor(input: unknown, private opts: TraceOptions = {}) {
    this._input = input;
    this.traceId = randomHex32();
  }

  /** The call's answer. An object is sent as it is; anything else is wrapped as { output: value }. */
  output(value: unknown): void {
    this._output = value;
  }

  /** Mark the call as failed, with a reason (a message or an Error). */
  error(problem: unknown): void {
    this._error = problem instanceof Error ? `${problem.name}: ${problem.message}` : String(problem);
  }

  /** Add to the call's cost, in dollars, if you know it. */
  cost(usd: number): void {
    if (Number.isFinite(usd)) this._cost += usd;
  }

  /** Add a step you built yourself, in the trace step format ({ type: "llm" | "tool", … }). */
  step(step: Json): void {
    if (this.steps.length < MAX_STEPS) this.steps.push(jsonable(step) as Json);
  }

  /** Record one tool call. */
  tool(name: string, args: unknown = null, output: unknown = null, error: unknown = null, latencyMs = 0): void {
    this.step({
      type: "tool", tool: name, args: isPlainObject(args) ? args : { args }, ok: error === null || error === undefined,
      output, error: error === null || error === undefined ? null : String(error), latency_ms: Math.round(latencyMs),
    });
  }

  /** Record one model call. */
  llm(model: string, tokensIn = 0, tokensOut = 0, latencyMs = 0, cost = 0, extra: Json = {}): void {
    this.step({
      type: "llm", model, tokens_in: Math.round(tokensIn || 0), tokens_out: Math.round(tokensOut || 0),
      cost: cost || 0, latency_ms: Math.round(latencyMs), ...extra,
    });
  }

  /** @internal */
  envelope(failedWith: unknown): Json {
    let error = this._error;
    if (failedWith !== undefined && error === null) error = failedWith instanceof Error ? `${failedWith.name}: ${failedWith.message}` : String(failedWith);
    const llm = this.steps.filter((s) => s.type === "llm");
    const model = [...llm].reverse().map((s) => s.model).find((m) => typeof m === "string" && m) as string | undefined;
    const out = this._output === undefined || this._output === null ? null : isPlainObject(this._output) ? this._output : { output: this._output };
    const inp = this._input === undefined || this._input === null ? {} : isPlainObject(this._input) ? this._input : { input: this._input };
    const num = (s: Json, k: string) => Number(s[k] ?? 0) || 0;
    const body: Json = {
      trace_id: this.traceId, schema_version: 1, source: "self_hosted",
      input: jsonable(inp), output: jsonable(out), success: error === null, error,
      steps: this.steps,
      tokens_in: llm.reduce((n, s) => n + num(s, "tokens_in"), 0),
      tokens_out: llm.reduce((n, s) => n + num(s, "tokens_out"), 0),
      cost: Math.round((this._cost + llm.reduce((n, s) => n + num(s, "cost"), 0)) * 1e8) / 1e8,
      latency_ms: Math.max(0, Math.round(performance.now() - this.t0)),
      created_at: new Date().toISOString(),
    };
    if (model) body.model = model;
    const versionId = this.opts.versionId ?? this.opts.prompt?.versionId;
    if (versionId !== undefined) body.version_id = versionId;
    return body;
  }
}

function isPlainObject(v: unknown): v is Json {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function randomHex32(): string {
  const c = (globalThis as { crypto?: { randomUUID?: () => string } }).crypto;
  if (c?.randomUUID) return c.randomUUID().replace(/-/g, "");
  let s = "";
  for (let i = 0; i < 32; i++) s += Math.floor(Math.random() * 16).toString(16);
  return s;
}

/** Record one call of your code. `fn` receives the trace (call t.output(...) in it); trace() returns whatever `fn`
 *  returns. If `fn` throws, the trace is recorded as failed and your error goes on as normal. Inside `fn`, traced()
 *  tools and wrapped OpenAI / Anthropic clients add their steps by themselves. */
export async function trace<T>(input: unknown, fn: (t: Trace) => T | Promise<T>, opts: TraceOptions = {}): Promise<T> {
  const t = new Trace(input, opts);
  let failed: unknown = undefined;
  const previous = fallbackCurrent;
  try {
    if (als) return await als.run(t, () => fn(t));
    fallbackCurrent = t;
    return await fn(t);
  } catch (e) {
    failed = e;
    throw e;
  } finally {
    if (!als) fallbackCurrent = previous;
    try {
      if (tracer) {
        const env = t.envelope(failed);
        const fnName = opts.function ?? tracer.defaultFunction;
        if (tracer.isSpaceKey && fnName) env.function = fnName;    // a space key covers many functions: name the one
        tracer.submit(env);
      }
    } catch {
      /* fail-open */
    }
  }
}

/** @internal Run `fn` as a call of your code, record its steps like trace() does, but send nothing: the caller gets the
 *  recorded envelope back (used by evaluate(), which sends the result to an experiment instead). Never throws for
 *  a failure in `fn` — that is returned as `error`. */
export async function captureTrace(input: unknown, fn: (t: Trace) => unknown): Promise<{ value: unknown; error: string | null; envelope: Json }> {
  const t = new Trace(input);
  let value: unknown = undefined;
  let failed: unknown = undefined;
  const previous = fallbackCurrent;
  try {
    value = als ? await als.run(t, () => fn(t)) : await (fallbackCurrent = t, fn(t));
  } catch (e) {
    failed = e;
  } finally {
    if (!als) fallbackCurrent = previous;
  }
  const envelope = t.envelope(failed);
  return { value, error: (envelope.error as string | null) ?? null, envelope };
}

function callArgs(args: unknown[]): Json {
  return { args: jsonable(args) } as Json;
}

/** Wrap a tool function so each call is recorded as a tool step of the trace it runs inside (name, arguments,
 *  output or error, time taken). Works for plain and async functions; outside a trace it just calls the function; an
 *  error is recorded and then thrown again, unchanged. */
export function traced<A extends unknown[], R>(fn: (...args: A) => R, opts: { name?: string } = {}): (...args: A) => R {
  const name = opts.name || fn.name || "tool";
  const wrapped = function (this: unknown, ...args: A): R {
    const t = currentTrace();
    const t0 = performance.now();
    const record = (output: unknown, error: unknown) => {
      if (!t) return;
      try {
        t.tool(name, callArgs(args), output, error, performance.now() - t0);
      } catch {
        /* fail-open */
      }
    };
    let out: R;
    try {
      out = fn.apply(this, args);
    } catch (e) {
      record(null, e);
      throw e;
    }
    if (out && typeof (out as { then?: unknown }).then === "function") {
      return (out as unknown as Promise<unknown>).then(
        (v) => { record(v, null); return v; },
        (e) => { record(null, e); throw e; },
      ) as unknown as R;
    }
    record(out, null);
    return out;
  };
  Object.defineProperty(wrapped, "name", { value: name });
  return wrapped;
}

function usageOf(resp: unknown): [number, number] {
  const u = (resp as { usage?: Json } | null)?.usage;
  if (!u) return [0, 0];
  const tin = u.prompt_tokens ?? u.input_tokens;
  const tout = u.completion_tokens ?? u.output_tokens;
  return [Number(tin) || 0, Number(tout) || 0];
}

const WRAPPED = Symbol.for("fnai.wrapped");

function wrapMethod(owner: unknown, attr: string, label: string): void {
  const o = owner as Record<string, unknown> | undefined;
  const original = o?.[attr];
  if (typeof original !== "function" || (original as unknown as Record<symbol, boolean>)[WRAPPED]) return;
  const replacement = function (this: unknown, ...args: unknown[]): unknown {
    const t = currentTrace();
    const t0 = performance.now();
    const params = (args[0] ?? {}) as Json;
    const note = (resp: unknown, streamed: boolean) => {
      if (!t) return;
      try {
        const [tin, tout] = usageOf(resp);
        const model = params.model ?? (resp as { model?: unknown } | null)?.model ?? label;
        t.llm(String(model), tin, tout, performance.now() - t0, 0, streamed ? { streamed: true } : {});
      } catch {
        /* fail-open */
      }
    };
    const result = (original as (...a: unknown[]) => unknown).apply(this, args);
    if (result && typeof (result as { then?: unknown }).then === "function") {
      // keep the provider's own promise (it may carry helpers such as .withResponse()) and just listen on the side
      (result as Promise<unknown>).then((resp) => note(resp, Boolean(params.stream)), () => undefined);
    } else {
      note(result, Boolean(params.stream));
    }
    return result;
  };
  (replacement as unknown as Record<symbol, boolean>)[WRAPPED] = true;
  o![attr] = replacement;
}

/** Record every model call made through an OpenAI client (chat.completions.create and responses.create) as an LLM
 *  step — model, tokens, time — of the trace it runs inside. Returns the same client. A streamed call is recorded
 *  when it starts, without token counts. */
export function wrapOpenAI<C>(client: C): C {
  const c = client as unknown as { chat?: { completions?: unknown }; responses?: unknown };
  wrapMethod(c.chat?.completions, "create", "openai");
  wrapMethod(c.responses, "create", "openai");
  return client;
}

/** Record every model call made through an Anthropic client (messages.create) as an LLM step. Returns the same client. */
export function wrapAnthropic<C>(client: C): C {
  wrapMethod((client as unknown as { messages?: unknown }).messages, "create", "anthropic");
  return client;
}
