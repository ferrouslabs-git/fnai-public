"""FNA-REQ-538: the plain-OpenAI example agent in sdk/examples runs its tool loop and records its steps, with no network and no
OpenAI library (a stand-in client plays the model)."""
import json
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "examples"))

import fnai
import openai_agent


def _reply(content=None, calls=()):
    message = SimpleNamespace(content=content, tool_calls=[
        SimpleNamespace(id=f"call{i}", function=SimpleNamespace(name=name, arguments=json.dumps(args)))
        for i, (name, args) in enumerate(calls)] or None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], model="gpt-4o-mini",
                           usage=SimpleNamespace(prompt_tokens=20, completion_tokens=5))


class _Model:
    """A model that first asks for the order tool, then answers from what the tool said."""

    def __init__(self):
        self.seen = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.seen.append(kw["messages"][-1])
        if len(self.seen) == 1:
            return _reply(calls=[("lookup_order", {"order_id": "1001"})])
        return _reply(content="Order 1001 has shipped.")


class OpenAIExample(unittest.TestCase):
    def test_the_agent_calls_its_tool_then_answers_and_both_are_recorded_as_steps(self):
        client = fnai.wrap_openai(_Model())
        with fnai.trace(input={"question": "Where is order 1001?"}) as t:
            answer = openai_agent.run_agent(client, "Where is order 1001?")
        self.assertEqual(answer, {"answer": "Order 1001 has shipped."})
        steps = t._steps
        self.assertEqual([s["type"] for s in steps], ["llm", "tool", "llm"])
        self.assertEqual(steps[1]["tool"], "lookup_order")
        self.assertEqual(steps[1]["output"], {"status": "shipped", "eta": "Friday"})
        self.assertEqual((steps[0]["model"], steps[0]["tokens_in"]), ("gpt-4o-mini", 20))

    def test_an_unknown_order_is_answered_from_the_tool_too_and_a_runaway_loop_stops(self):
        class Loops:
            chat = SimpleNamespace(completions=SimpleNamespace(
                create=lambda **kw: _reply(calls=[("lookup_order", {"order_id": "9999"})])))
        self.assertEqual(openai_agent.run_agent(Loops(), "?", max_turns=3), {"answer": "Sorry, I could not finish that."})

    def test_serve_reports_the_call_as_a_trace(self):
        sent = []
        fnai._TRACER = SimpleNamespace(submit=sent.append, default_function=None, space_key=False)
        try:
            openai_agent.serve(fnai.wrap_openai(_Model()), "Where is order 1001?")
        finally:
            fnai._TRACER = None
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["input"], {"question": "Where is order 1001?"})
        self.assertEqual(sent[0]["output"], {"answer": "Order 1001 has shipped."})
        self.assertEqual([s["type"] for s in sent[0]["steps"]], ["llm", "tool", "llm"])


if __name__ == "__main__":
    unittest.main()
