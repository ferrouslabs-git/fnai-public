"""Sample app: a real system calling a deployed functionalAI function.

This is the shape a customer's backend takes once a prompt has been onboarded.
The prompt is NOT in this file — the app knows a slug and a key, and the prompt
is edited, evaluated and deployed on the platform without this code changing.

Run it:

    export FNAI_INVOKE_KEY=fnk_...          # Function → API keys → Mint
    export FNAI_BASE_URL=http://localhost:8100
    python gg_seo_editor.py

Onboarded from the client's blog_beaver/seo_editor_prompt.py (epic E18).
"""
from __future__ import annotations

import os
import sys

from fnai import Function, FnaiError

SLUG = "seo_editor_prompt"

DRAFT = """# kitchen renovation costs

We renovate kitchens. Our kitchens are very good and we have done a lot of them
over the years. Customers are usually happy with the result.

Costs depend on what you want. Some kitchens cost more and some cost less. It
depends on the materials and how big the room is.

If you want a new kitchen you should get in touch with us. We can give you a
quote. Contact us today to find out more about our great kitchens."""


def main() -> int:
    key = os.environ.get("FNAI_INVOKE_KEY")
    if not key:
        print("FNAI_INVOKE_KEY is not set — mint one under the function's API keys",
              file=sys.stderr)
        return 2

    fn = Function(SLUG, api_key=key,
                  base_url=os.environ.get("FNAI_BASE_URL", "http://localhost:8100"))

    # The contract comes from the ACTIVE deployment, so it is the honest answer
    # to "what does this function take today" — it moves with deploys and
    # rollbacks. Worth reading at startup rather than hardcoding field names.
    try:
        contract = fn.contract()
    except FnaiError as e:
        print(f"cannot read contract: {e.status} {e.detail}", file=sys.stderr)
        return 1
    print(f"{SLUG} v{contract['version']} "
          f"expects {list(contract['input_schema']['properties'])}")

    try:
        res = fn.run(blog_post=DRAFT)
    except FnaiError as e:
        # 503 here usually means the deployed model is not routable, or the org
        # has no active provider credential for it — both are config, not code.
        print(f"call failed: {e.status} {e.detail}", file=sys.stderr)
        return 1

    if not res.ok:
        print(f"function returned an error: {res.error}", file=sys.stderr)
        return 1

    print(f"\n--- edited ---\n{res.output['blog_post']}\n")
    # Log these against your own request id: trace_id opens the run on the
    # platform, cost_usd is what this single call actually cost.
    print(f"trace {res.trace_id} · ${res.meta.get('cost_usd')} · "
          f"{res.meta.get('latency_ms')}ms · "
          f"{res.meta.get('tokens_in')}/{res.meta.get('tokens_out')} tokens")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
