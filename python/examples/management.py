"""functionalAI management SDK example — the golden path in one script.

Requires a personal access token (avatar menu → API tokens → Mint).
"""
from fnai import Client, FnaiError

c = Client(
    token="fnp_…",                        # your personal access token
    base_url="https://staging.fnai.dev",  # or http://localhost:8000 in dev
)

try:
    fn = c.functions.create(
        slug="doc_summarizer", name="Doc Summarizer",
        input_schema={"type": "object", "properties": {"document": {"type": "string"}}},
        output_schema={"type": "object", "required": ["summary"],
                       "properties": {"summary": {"type": "string"}}})

    v = c.functions.create_version(fn["id"], snapshot={
        "sections": [{"type": "role", "title": "Role",
                      "content": "Summarize {document} in two sentences."}],
        "impl": "prompt",
        "model_binding": {"model": "mock-small", "temperature": 0.2},
        "tools": []}, message="first version")

    # try it before shipping it
    out = c.versions.playground(v["id"], input={"document": "…long text…"})
    print("playground:", out.get("output"))

    # lifecycle → live
    c.versions.submit(v["id"])
    c.versions.approve(v["id"], comment="looks good")
    c.deployments.deploy(fn["id"], v["id"])

    # hand the runtime surface to callers
    key = c.keys.invoke.mint(fn["id"], name="prod")   # plaintext shown once
    handle = c.function(fn["slug"], api_key=key["key"])
    result = handle.run(document="…long text…")
    print("run:", result.ok, result.output, "trace:", result.trace_id)

    # watch it
    print("health:", c.functions.health(fn["id"]))
except FnaiError as e:
    print(f"API error {e.status}: {e.detail}")
