"""Interactive prompt tester: pick a function, type its inputs, see the result.

No hardcoded values — this reads each function's REAL input schema (whatever
it currently is, live) and asks you for each field by name, one at a time.
Run it once per test instead of editing a script every time you want to try
a different input.

    export FNAI_TOKEN=fnp_...          # avatar menu -> API tokens -> Mint
    export FNAI_BASE_URL=http://localhost:8100   # or the staging URL
    python interactive_test.py
"""
from __future__ import annotations

import json
import os
import sys

from fnai import Client, FnaiError

TYPE_HINT = {"string": "text", "integer": "whole number", "number": "number",
            "boolean": "true/false", "array": "JSON array, e.g. [\"a\",\"b\"]",
            "object": "JSON object, e.g. {\"a\": 1}"}


def prompt_field(name: str, prop: dict, required: bool) -> object:
    ptype = prop.get("type", "string")
    hint = TYPE_HINT.get(ptype, ptype)
    desc = f" — {prop['description']}" if prop.get("description") else ""
    mark = "" if required else " (optional, blank to skip)"
    raw = input(f"  {name} [{hint}]{mark}{desc}\n  > ").strip()

    if not raw and not required:
        return None
    if ptype == "integer":
        return int(raw)
    if ptype == "number":
        return float(raw)
    if ptype == "boolean":
        return raw.lower() in ("true", "1", "yes", "y")
    if ptype in ("array", "object"):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            print(f"  (not valid JSON — sending it as a plain string instead)")
            return raw
    return raw


def pick_function(c: Client) -> dict:
    fns = c.functions.list()
    if not fns:
        print("No functions in this space. Create one first.")
        sys.exit(1)
    print("\nFunctions available:")
    for i, fn in enumerate(fns):
        print(f"  [{i}] {fn['slug']}  —  {fn.get('name', '')}")
    while True:
        choice = input(f"\nPick one [0-{len(fns) - 1}]: ").strip()
        if choice.isdigit() and 0 <= int(choice) < len(fns):
            return fns[int(choice)]
        print("  not a valid choice, try again")


def pick_model() -> str:
    print("\nModel:")
    print("  [1] mock-small   (free, no key needed, output is placeholder text)")
    print("  [2] gemini-flash (real output, needs a Gemini key on this org)")
    choice = input("Pick one [1-2, default 2]: ").strip()
    return "mock-small" if choice == "1" else "gemini-flash"


def main() -> int:
    token = os.environ.get("FNAI_TOKEN")
    if not token:
        print("Set FNAI_TOKEN first — avatar menu -> API tokens -> Mint.", file=sys.stderr)
        return 2
    base_url = os.environ.get("FNAI_BASE_URL", "http://localhost:8100")
    c = Client(token=token, base_url=base_url)

    fn = pick_function(c)
    versions = c.functions.versions(fn["id"])
    if not versions:
        print(f"'{fn['slug']}' has no versions yet — write a prompt for it first.")
        return 1
    v = versions[0]  # newest first, per the API's own ordering

    schema = v.get("input_schema") or fn.get("input_schema") or {}
    props = schema.get("properties", {})
    required = set(schema.get("required", []))
    if not props:
        print(f"'{fn['slug']}' declares no input fields — running it as-is.")

    print(f"\n--- {fn['slug']} — type each input, Enter to submit it ---")
    values = {}
    for name, prop in props.items():
        values[name] = prompt_field(name, prop, name in required)
    values = {k: v for k, v in values.items() if v is not None}

    model = pick_model()

    print("\nRunning...")
    try:
        result = c.versions.playground(v["id"], input=values, model=model)
    except FnaiError as e:
        print(f"\nCall failed: {e.status} {e.detail}", file=sys.stderr)
        return 1

    print("\n--- result ---")
    if result.get("success") is False:
        print(f"error: {result.get('error')}")
    else:
        print(json.dumps(result.get("output"), indent=2))
    meta_bits = [f"{k}={result[k]}" for k in ("cost", "latency_ms", "tokens_in", "tokens_out")
                if k in result]
    if meta_bits:
        print("  (" + ", ".join(meta_bits) + ")")

    again = input("\nRun again with different input? [y/N] ").strip().lower()
    if again == "y":
        return main()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (EOFError, KeyboardInterrupt):
        print("\ncancelled")
        raise SystemExit(130)
