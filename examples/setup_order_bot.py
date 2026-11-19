"""One-time setup for the two agent examples next to this file: a function called `order_bot` (the contract and prompt
Functional AI keeps), a dataset of questions, and one check.

    export FNAI_TOKEN=<your personal token, fnp_…>        # avatar → API tokens
    python3 setup_order_bot.py
"""
import os

import fnai

c = fnai.Client(os.environ["FNAI_TOKEN"])
if os.environ.get("FNAI_SPACE"):
    c.use_space(os.environ["FNAI_SPACE"])               # only if you are not in your default space
slug = os.environ.get("FNAI_FUNCTION", "order_bot")

fn = c.functions.create(
    slug=slug, name="Order bot",
    input_schema={"type": "object", "properties": {"question": {"type": "string"}}},
    output_schema={"type": "object", "required": ["answer"], "properties": {"answer": {"type": "string"}}})
c.functions.create_version(fn["id"], snapshot={
    "sections": [{"type": "role", "title": "Role", "content": "Answer this customer's order question: {question}"}],
    "impl": "prompt", "model_binding": {"model": "mock-small", "temperature": 0.2}, "tools": []},
    message="first version")
c.datasets.create(fn["id"], name="orders", rows=[
    {"input": {"question": "Where is order 1001?"}},
    {"input": {"question": "What is the status of order 1002?"}},
    {"input": {"question": "Can you check order 9999 for me?"}}])
c.eval.scores.create(fn["id"], name="has an answer", tier="det", config={"rule": "required", "field": "answer"})
print(f"ready: {slug}")
