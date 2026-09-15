#!/usr/bin/env python3
"""fn — the functionalAI command line (REQ-55).

init / new / run / eval / deploy / pin / diff / rollback / ls / logs.

Chosen over the sibling Go implementation (sdk/go/cmd/fn) as the verified
one: this repo's Python environment is real and testable here, so every
command below has actually been run against a live server, not just
reviewed by hand. Same three decisions, same reasoning, ported directly:

- Auth: the existing personal access token (fnp_*) for every authoring
  command — the same mechanism the SDKs and MCP server already use.
  `fn login` stores it in ~/.fnai/config.json. `fn run` (invoking an
  *already-deployed* function) uses a different, narrower credential — an
  fnk_* invoke key, via --key/FNAI_INVOKE_KEY, never stored.
- `pin`/`diff` operate on versions; `pin` is stored locally, in a
  .fnai/pin.json file in the working directory — not new server-side
  state, the same way a version-control tool scopes a checked-out ref to
  the folder you're standing in.
- Distribution (single-file packaging) is separate from the logic here —
  see the README for the PyInstaller command that turns this into one.

Zero non-stdlib imports beyond the sibling fnai.py module (same
zero-dependency philosophy the SDK itself states).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fnai import Client, FnaiError, write_starter_yaml  # noqa: E402

CONFIG_PATH = Path.home() / ".fnai" / "config.json"
PIN_PATH = Path(".fnai") / "pin.json"


# ── config / pin ──────────────────────────────────────────────────────────

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text())
    except json.JSONDecodeError as e:
        raise CliError(f"{CONFIG_PATH} is corrupt ({e}) — run `fn login` again")


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    os.chmod(CONFIG_PATH, 0o600)  # a token lives in here


def load_pin() -> dict:
    if not PIN_PATH.exists():
        raise CliError("nothing pinned here yet — run `fn pin <function-id> <version-id>` first")
    return json.loads(PIN_PATH.read_text())


def save_pin(p: dict) -> None:
    PIN_PATH.parent.mkdir(parents=True, exist_ok=True)
    PIN_PATH.write_text(json.dumps(p, indent=2))


class CliError(Exception):
    """A readable, user-facing failure — never a raw traceback."""


def friendly(exc: Exception) -> str:
    if isinstance(exc, FnaiError):
        if exc.status == 0:
            return f"can't reach the server — {exc.detail}"
        if exc.status == 401:
            return "not signed in, or your token expired — run `fn login`"
        if exc.status == 403:
            return "you don't have permission to do that"
        if exc.status == 404:
            return "not found — check the id/slug"
        return str(exc.detail)
    return str(exc)


def get_client(base_url: str | None = None) -> Client:
    cfg = load_config()
    token = os.environ.get("FNAI_TOKEN") or cfg.get("token")
    if not token:
        raise CliError("not signed in — run `fn login` first (or set FNAI_TOKEN)")
    base = base_url or os.environ.get("FNAI_BASE_URL") or cfg.get("base_url") or "http://localhost:8000"
    return Client(token=token, base_url=base)


# ── commands ──────────────────────────────────────────────────────────────

def cmd_login(args: argparse.Namespace) -> None:
    token = args.token or input("Personal access token (fnp_...): ").strip()
    if not token.startswith("fnp_"):
        raise CliError("that doesn't look like a personal access token — it should start with fnp_ "
                       "(mint one in the app under your avatar → API tokens)")
    cfg = {"token": token}
    if args.base_url:
        cfg["base_url"] = args.base_url
    save_config(cfg)
    try:
        me = Client(token=token, base_url=cfg.get("base_url", "http://localhost:8000")).me()
    except FnaiError as e:
        raise CliError(f"saved the token, but it doesn't seem to work: {friendly(e)}")
    print(f"Signed in as {me.get('user_id')}.")


def cmd_init(args: argparse.Namespace) -> None:
    path = write_starter_yaml(args.path)
    print(f"Wrote {path}. Edit it, then run: fn new {path}")


def _slugify(name: str) -> str:
    import re
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return slug or "function"


def cmd_new(args: argparse.Namespace) -> None:
    path = Path(args.path)
    text = path.read_text(encoding="utf-8")
    c = get_client()
    fn, version = c.functions.create_from_yaml_text(text)
    print(f"Created function {fn['slug']!r} (id {fn['id']}), version {version['number']}.")


def cmd_ls(args: argparse.Namespace) -> None:
    c = get_client()
    fns = c.functions.list()
    if not fns:
        print("No functions yet — try `fn init` then `fn new`.")
        return
    print(f"{'ID':<6} {'SLUG':<28} NAME")
    for f in fns:
        print(f"{f['id']:<6} {f['slug']:<28} {f['name']}")


def cmd_run(args: argparse.Namespace) -> None:
    key = args.key or os.environ.get("FNAI_INVOKE_KEY")
    if not key:
        raise CliError("no invoke key — pass --key, or set FNAI_INVOKE_KEY "
                       "(mint one for this function in the app — a personal access token from "
                       "`fn login` does not work here, invoke uses its own narrower key)")
    try:
        payload = json.loads(args.input)
    except json.JSONDecodeError as e:
        raise CliError(f"--input must be a JSON object: {e}")
    from fnai import Function
    cfg = load_config()
    base = (args.base_url or os.environ.get("FNAI_BASE_URL")
            or cfg.get("base_url") or "http://localhost:8000")
    f = Function(args.slug, api_key=key, base_url=base)
    res = f.run(**payload)
    if not res.ok:
        print(json.dumps({"output": res.output, "status": res.status, "error": res.error}, indent=2))
        raise CliError(f"run failed: {res.error}")
    print(json.dumps(res.output, indent=2))
    print(f"({res.meta.get('latency_ms')}ms, ${res.meta.get('cost_usd', 0):.4f}, "
         f"trace {res.trace_id})", file=sys.stderr)


def cmd_deploy(args: argparse.Namespace) -> None:
    c = get_client()
    dep = c.deployments.deploy(args.function_id, args.version_id)
    print(f"Deployed. Live deployment id {dep['id']}.")


def cmd_rollback(args: argparse.Namespace) -> None:
    c = get_client()
    dep = c.deployments.rollback(args.function_id, args.to_version)
    print(f"Rolled back. Live deployment id {dep['id']}.")


def cmd_pin(args: argparse.Namespace) -> None:
    c = get_client()
    v = c.versions.get(args.version_id)
    save_pin({"function_id": args.function_id, "version_id": args.version_id, "number": v["number"]})
    print(f"Pinned function {args.function_id} to version {args.version_id} "
         f"(v{v['number']}) in this folder.")


def cmd_diff(args: argparse.Namespace) -> None:
    c = get_client()
    a = c.versions.get(args.version_id_a)
    b = c.versions.get(args.version_id_b)
    a_text = json.dumps(a.get("snapshot"), indent=2, sort_keys=False).splitlines()
    b_text = json.dumps(b.get("snapshot"), indent=2, sort_keys=False).splitlines()
    import difflib
    print(f"--- v{a['number']}\n+++ v{b['number']}")
    for line in list(difflib.unified_diff(a_text, b_text, lineterm="", n=3))[2:]:
        print(line)


def cmd_eval(args: argparse.Namespace) -> None:
    c = get_client()
    version_id = args.version
    if version_id is None:
        version_id = load_pin()["version_id"]

    snapshot_id = args.snapshot
    if snapshot_id is None:
        snapshot_id = _latest_snapshot_id(c, args.function_id)

    if args.scores:
        score_ids = [int(s) for s in args.scores.split(",")]
    else:
        # "Gate" scores were removed from the product (see backend/app/models.py's
        # Score.gate docstring) — the API never returns that field, so there is no
        # such thing as a "gate-tier" subset to auto-select any more. Default to
        # every check defined for the function instead.
        scores = c.eval.scores.list(args.function_id)
        score_ids = [s["id"] for s in scores]
        if not score_ids:
            raise CliError("this function has no checks to evaluate against — "
                           "add one in the app, or pass --scores explicitly")

    # Creating with run=True (the API default) already executes it synchronously
    # server-side (backend/app/experiments/api.py: "v0 synchronous") — calling
    # .run() again here would always 409 ("already run"). The poll below is kept
    # so this keeps working if that ever becomes async.
    print(f"Running experiment against {len(score_ids)} check(s)...")
    exp = c.eval.experiments.create(
        args.function_id, version_ids=[version_id], snapshot_id=snapshot_id,
        score_ids=score_ids, repeats=args.repeats)

    status = _wait_for_experiment(c, exp["id"])
    if status == "failed":
        raise CliError("the experiment itself failed to run (infrastructure error) — see the app for detail")

    results = c.eval.scores.results(args.function_id, experiment_ids=str(exp["id"]), group_by="experiment")
    failed = [r for r in results if r.get("pass_rate", 1.0) < 1.0]
    if failed:
        for r in sorted(failed, key=lambda r: r["score"]):
            print(f"FAIL  {r['score']:<30} pass_rate={r['pass_rate'] * 100:.0f}%")
        raise CliError(f"{len(failed)} check(s) failed")
    print("All checks passed.")


def _wait_for_experiment(c: Client, exp_id: int, timeout_s: int = 600) -> str:
    import time
    deadline = time.monotonic() + timeout_s
    while True:
        exp = c.eval.experiments.get(exp_id)
        if exp["status"] in ("completed", "failed"):
            return exp["status"]
        if time.monotonic() > deadline:
            raise CliError(f"experiment {exp_id} didn't finish within {timeout_s}s — check it in the app")
        time.sleep(2)


def _latest_snapshot_id(c: Client, fn_id: int) -> int:
    datasets = c.datasets.list(fn_id)
    if not datasets:
        raise CliError("this function has no datasets yet — create one in the app first")
    ds = datasets[-1]  # most recently created — pass --snapshot explicitly to be exact
    snapshots = c.raw("GET", f"/api/datasets/{ds['id']}/snapshots")
    if not snapshots:
        raise CliError(f"dataset {ds['name']!r} has no snapshots yet — take one in the app first")
    return snapshots[0]["id"]


def cmd_logs(args: argparse.Namespace) -> None:
    c = get_client()
    traces = c.traces.list(function_id=args.function_id, source=args.source, limit=args.limit)
    if not traces:
        print("No traces yet.")
        return
    print(f"{'STATUS':<7} {'LATENCY':>8}  {'MODEL':<16}  {'COST':<10}  TRACE ID")
    for t in traces:
        status = "ok" if t.get("success") else "FAILED"
        print(f"{status:<7} {t.get('latency_ms', 0):>6}ms  {t.get('model', ''):<16}  "
             f"${t.get('cost', 0):<9.4f}  {t.get('trace_id', '')}")


# ── argument parsing ──────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fn", description="The functionalAI command line.")
    sub = p.add_subparsers(dest="command")

    lg = sub.add_parser("login", help="store your personal access token")
    lg.add_argument("--token", help="paste your token instead of typing it interactively")
    lg.add_argument("--base-url", help="server URL (default: production)")
    lg.set_defaults(func=cmd_login)

    it = sub.add_parser("init", help="write a working example function to a file")
    it.add_argument("path", nargs="?", default="function.yaml")
    it.set_defaults(func=cmd_init)

    nw = sub.add_parser("new", help="push a function file up as a brand-new function")
    nw.add_argument("path")
    nw.set_defaults(func=cmd_new)

    ls = sub.add_parser("ls", help="list your functions")
    ls.set_defaults(func=cmd_ls)

    rn = sub.add_parser("run", help="invoke a deployed function")
    rn.add_argument("slug")
    rn.add_argument("--key", help="the function's fnk_... invoke key")
    rn.add_argument("--input", default="{}", help='JSON object, e.g. \'{"ticket_text": "..."}\'')
    rn.add_argument("--base-url")
    rn.set_defaults(func=cmd_run)

    ev = sub.add_parser("eval", help="run this function's checks; exits nonzero if any fail")
    ev.add_argument("function_id", type=int)
    ev.add_argument("--version", type=int, help="default: the folder's pinned version")
    ev.add_argument("--snapshot", type=int, help="default: the function's latest dataset snapshot")
    ev.add_argument("--scores", help="comma-separated score ids (default: every score on the function)")
    ev.add_argument("--repeats", type=int, default=1)
    ev.set_defaults(func=cmd_eval)

    dp = sub.add_parser("deploy", help="make a version the live deployment")
    dp.add_argument("function_id", type=int)
    dp.add_argument("version_id", type=int)
    dp.set_defaults(func=cmd_deploy)

    rb = sub.add_parser("rollback", help="roll back to the previous deployment")
    rb.add_argument("function_id", type=int)
    rb.add_argument("--to-version", type=int, dest="to_version")
    rb.set_defaults(func=cmd_rollback)

    pn = sub.add_parser("pin", help="remember a version for this folder")
    pn.add_argument("function_id", type=int)
    pn.add_argument("version_id", type=int)
    pn.set_defaults(func=cmd_pin)

    df = sub.add_parser("diff", help="compare two versions")
    df.add_argument("version_id_a", type=int)
    df.add_argument("version_id_b", type=int)
    df.set_defaults(func=cmd_diff)

    lo = sub.add_parser("logs", help="recent production calls")
    lo.add_argument("function_id", type=int)
    lo.add_argument("--limit", type=int, default=20)
    lo.add_argument("--source", default="production")
    lo.set_defaults(func=cmd_logs)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        args.func(args)
    except (CliError, FnaiError) as e:
        print(f"Error: {friendly(e)}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
