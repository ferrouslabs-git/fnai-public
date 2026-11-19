"""A plain OpenAI agent (a model that can call a tool), tested and monitored with Functional AI.

The agent runs HERE, on your computer, with your OpenAI key. Functional AI keeps the test questions and the scoring, and shows
you the steps the agent took. Nothing is sent to OpenAI by us.

    pip install openai
    export OPENAI_API_KEY=<your OpenAI key>
    export FNAI_TOKEN=<your personal token, or a space key with "Run tests" and "Read functions">
    python3 setup_order_bot.py            # once
    python3 openai_agent.py
"""
import json
import os

import fnai

MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
FUNCTION = os.environ.get("FNAI_FUNCTION", "order_bot")
ORDERS = {"1001": {"status": "shipped", "eta": "Friday"}, "1002": {"status": "processing"}}

TOOLS = [{"type": "function", "function": {
    "name": "lookup_order", "description": "Look up an order's status by its number.",
    "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]}}}]


@fnai.traced                                  # each call of this tool is recorded as a step of the trace it runs in
def lookup_order(order_id: str) -> dict:
    return ORDERS.get(order_id, {"status": "not found"})


def run_agent(client, question: str, max_turns: int = 4) -> dict:
    """The agent loop: ask the model; if it wants a tool, run it and ask again; otherwise that is the answer."""
    messages = [{"role": "system", "content": "You answer customers' order questions. Use the tool to look orders up."},
                {"role": "user", "content": question}]
    for _ in range(max_turns):
        reply = client.chat.completions.create(model=MODEL, messages=messages, tools=TOOLS).choices[0].message
        if not reply.tool_calls:
            return {"answer": reply.content or ""}
        messages.append(reply)
        for call in reply.tool_calls:
            result = lookup_order(**json.loads(call.function.arguments))
            messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(result)})
    return {"answer": "Sorry, I could not finish that."}


def make_client():
    from openai import OpenAI
    return fnai.wrap_openai(OpenAI())         # every model call becomes a recorded step (model, tokens, time)


def serve(client, question: str) -> dict:
    """What your real service does for each customer call: run the agent and report the call to Functional AI."""
    with fnai.trace(input={"question": question}) as t:
        answer = run_agent(client, question)
        t.output(answer)
    return answer


if __name__ == "__main__":
    client = make_client()
    # Test it on the questions you keep in Functional AI. The SDK passes each row to your agent and sends back the result.
    result = fnai.eval(FUNCTION, dataset="orders", task=lambda row: run_agent(client, row["question"]))
    print("pass rate:", result["pass_rate"], "·", result["url"])
