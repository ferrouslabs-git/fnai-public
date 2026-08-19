"""Minimal functionalAI public API example.

Requires a deployed function and an invoke key (Deploy ▸ API keys → Mint).
"""
from fnai import Function, FnaiError

fn = Function(
    "doc_summarizer",  # your function slug
    api_key="fnk_…",
    base_url="https://staging.fnai.dev",  # or your hosted endpoint
)

try:
    contract = fn.contract()
    print("version:", contract.get("version"))
    print("input fields:", list(contract.get("input_schema", {}).get("properties", {})))

    result = fn.run(document="Hello from the fnai open-source client.")
    if result.ok:
        print("output:", result.output)
        print("cost_usd:", result.meta.get("cost_usd"), "trace:", result.trace_id)
    else:
        print("execution failed:", result.error)
except FnaiError as e:
    print(f"API error {e.status}: {e.detail}")
