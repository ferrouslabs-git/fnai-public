"""A LangGraph agent, tested and monitored with Functional AI.

LangGraph runs the agent on your computer, with your model key. A small callback records each model call and each tool call as
steps of the Functional AI trace, so behaviour checks ("used the tool", "at most 2 calls") can score them.

    pip install langgraph langchain-openai
    export OPENAI_API_KEY=<your OpenAI key>
    export FNAI_TOKEN=<your personal token, or a space key with "Run tests" and "Read functions">
    python3 setup_order_bot.py            # once
    python3 langgraph_agent.py
"""
import os

import fnai
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent

FUNCTION = os.environ.get("FNAI_FUNCTION", "order_bot")
ORDERS = {"1001": {"status": "shipped", "eta": "Friday"}, "1002": {"status": "processing"}}


@tool
def lookup_order(order_id: str) -> dict:
    """Look up an order's status by its number."""
    return ORDERS.get(order_id, {"status": "not found"})


class FnaiSteps(BaseCallbackHandler):
    """Records LangChain's model and tool calls as steps of one Functional AI trace."""

    def __init__(self, trace):
        self.trace, self._tools = trace, {}

    def on_llm_end(self, response, **kwargs):
        message = response.generations[0][0].message
        usage = getattr(message, "usage_metadata", None) or {}
        meta = getattr(message, "response_metadata", None) or {}
        self.trace.llm(meta.get("model_name") or meta.get("model") or "model",
                       usage.get("input_tokens", 0), usage.get("output_tokens", 0))

    def on_tool_start(self, serialized, input_str, *, run_id, **kwargs):
        self._tools[run_id] = (serialized.get("name", "tool"), input_str)

    def on_tool_end(self, output, *, run_id, **kwargs):
        name, args = self._tools.pop(run_id, ("tool", ""))
        self.trace.tool(name, {"input": args}, output=getattr(output, "content", output))

    def on_tool_error(self, error, *, run_id, **kwargs):
        name, args = self._tools.pop(run_id, ("tool", ""))
        self.trace.tool(name, {"input": args}, error=error)


def build_agent(model=None):
    if model is None:
        from langchain_openai import ChatOpenAI
        model = ChatOpenAI(model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"))
    return create_react_agent(model, [lookup_order])


def run_agent(agent, question: str) -> dict:
    """Run the agent for one question. Inside a Functional AI trace (a test row, or `serve` below) its steps are recorded."""
    config = {}
    current = fnai.current_trace()
    if current is not None:
        config["callbacks"] = [FnaiSteps(current)]
    state = agent.invoke({"messages": [("user", question)]}, config=config)
    return {"answer": state["messages"][-1].content}


def serve(agent, question: str) -> dict:
    """What your real service does for each customer call: run the agent and report the call to Functional AI."""
    with fnai.trace(input={"question": question}) as t:
        answer = run_agent(agent, question)
        t.output(answer)
    return answer


if __name__ == "__main__":
    agent = build_agent()
    result = fnai.eval(FUNCTION, dataset="orders", task=lambda row: run_agent(agent, row["question"]))
    print("pass rate:", result["pass_rate"], "·", result["url"])
