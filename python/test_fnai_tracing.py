"""FNA-REQ-532: the SDK's tracing helpers — trace(), @traced, wrap_openai / wrap_anthropic, and the background sender.
Stdlib only, no real network: a tiny local server stands in for POST /v1/traces."""
import asyncio
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import fnai


class _Server:
    """Records every POST /v1/traces; `statuses` is the answers to give, in order (then 201 forever)."""

    def __init__(self, statuses=()):
        self.received, self.auth, self.statuses = [], [], list(statuses)
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.received.append(body)
                outer.auth.append(self.headers.get("Authorization"))
                code = outer.statuses.pop(0) if outer.statuses else 201
                payload = json.dumps({"trace_id": body["trace_id"], "duplicate": False} if code < 300 else {"detail": "no"}).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class _Base(unittest.TestCase):
    def setUp(self):
        self._retry = fnai._RETRY_AFTER_S
        fnai._RETRY_AFTER_S = (0.0, 0.0, 0.0)               # no real waiting in tests
        self.server = _Server()
        fnai.init_tracing(api_key="fnk_test", base_url=self.server.url)

    def tearDown(self):
        fnai._RETRY_AFTER_S = self._retry
        fnai._TRACER = None
        self.server.close()

    def sent(self):
        self.assertTrue(fnai.flush_traces(5))
        return self.server.received


class TraceBlock(_Base):
    def test_a_trace_is_sent_in_the_background_with_the_key_and_the_right_shape(self):
        with fnai.trace(input={"q": "hi"}) as t:
            t.output({"a": "yo"})
        (tr,) = self.sent()
        self.assertEqual(self.server.auth, ["Bearer fnk_test"])
        self.assertEqual((tr["source"], tr["schema_version"], tr["success"], tr["error"]), ("self_hosted", 1, True, None))
        self.assertEqual((tr["input"], tr["output"], tr["steps"]), ({"q": "hi"}, {"a": "yo"}, []))
        self.assertEqual(len(tr["trace_id"]), 32)
        self.assertNotIn("version_id", tr)                    # none named: none sent
        self.assertNotIn("function_id", tr)                   # the key names the function
        self.assertIn("created_at", tr)
        self.assertGreaterEqual(tr["latency_ms"], 0)

    def test_a_named_version_is_sent(self):
        with fnai.trace(input={"q": "hi"}, version_id=7) as t:
            t.output({"a": "yo"})
        self.assertEqual(self.sent()[0]["version_id"], 7)

    def test_a_non_object_input_or_output_is_wrapped_so_the_server_accepts_it(self):
        with fnai.trace(input="just text") as t:
            t.output(42)
        (tr,) = self.sent()
        self.assertEqual((tr["input"], tr["output"]), ({"input": "just text"}, {"output": 42}))

    def test_a_block_that_raises_is_recorded_as_failed_and_the_exception_goes_on(self):
        with self.assertRaises(ValueError):
            with fnai.trace(input={"q": 1}):
                raise ValueError("boom")
        (tr,) = self.sent()
        self.assertFalse(tr["success"])
        self.assertEqual(tr["error"], "ValueError: boom")

    def test_error_marks_the_call_failed_without_raising(self):
        with fnai.trace(input={}) as t:
            t.error("the model said no")
        tr = self.sent()[0]
        self.assertFalse(tr["success"])
        self.assertEqual(tr["error"], "the model said no")

    def test_current_trace_is_the_one_you_are_inside(self):
        self.assertIsNone(fnai.current_trace())
        with fnai.trace(input={}) as t:
            self.assertIs(fnai.current_trace(), t)
        self.assertIsNone(fnai.current_trace())

    def test_steps_tokens_cost_and_model_are_totalled(self):
        with fnai.trace(input={}) as t:
            t.llm("gpt-4o-mini", 10, 5, 100, cost=0.001)
            t.llm("gpt-4o", 20, 8, 200, cost=0.002)
            t.tool("search", {"q": "x"}, output={"n": 1}, latency_ms=5)
            t.cost(0.5)
            t.output({"a": 1})
        (tr,) = self.sent()
        self.assertEqual((tr["tokens_in"], tr["tokens_out"]), (30, 13))
        self.assertAlmostEqual(tr["cost"], 0.503)
        self.assertEqual(tr["model"], "gpt-4o")
        self.assertEqual([s["type"] for s in tr["steps"]], ["llm", "llm", "tool"])

    def test_at_most_500_steps_are_kept_the_servers_limit(self):
        with fnai.trace(input={}) as t:
            for i in range(600):
                t.tool("x", {"i": i})
        self.assertEqual(len(self.sent()[0]["steps"]), 500)

    def test_values_json_cannot_hold_are_turned_into_text_not_errors(self):
        class Odd:
            pass
        with fnai.trace(input={"o": Odd(), "s": {1, 2}, "b": b"x"}) as t:
            t.output({"ok": True})
        (tr,) = self.sent()
        self.assertIn("Odd", tr["input"]["o"])
        self.assertEqual(sorted(tr["input"]["s"]), [1, 2])


class TracedTools(_Base):
    def test_a_tool_call_becomes_a_tool_step_with_its_arguments_and_output(self):
        @fnai.traced
        def search(query, limit=3):
            return {"hits": [query] * limit}
        with fnai.trace(input={}) as t:
            self.assertEqual(search("cats", limit=2), {"hits": ["cats", "cats"]})   # the return value is unchanged
            t.output({"a": 1})
        (step,) = self.sent()[0]["steps"]
        self.assertEqual((step["type"], step["tool"], step["ok"]), ("tool", "search", True))
        self.assertEqual(step["args"], {"query": "cats", "limit": 2})
        self.assertEqual(step["output"], {"hits": ["cats", "cats"]})
        self.assertGreaterEqual(step["latency_ms"], 0)

    def test_a_failing_tool_is_recorded_and_its_exception_is_raised_again(self):
        @fnai.traced(name="lookup")
        def lookup(x):
            raise KeyError("missing")
        with fnai.trace(input={}):
            with self.assertRaises(KeyError):
                lookup(1)
        (step,) = self.sent()[0]["steps"]
        self.assertEqual((step["tool"], step["ok"]), ("lookup", False))
        self.assertIn("missing", step["error"])

    def test_an_async_tool_is_recorded_too(self):
        @fnai.traced
        async def fetch(url):
            await asyncio.sleep(0)
            return url.upper()

        async def run():
            with fnai.trace(input={}):
                return await fetch("a")
        self.assertEqual(asyncio.run(run()), "A")
        (step,) = self.sent()[0]["steps"]
        self.assertEqual((step["tool"], step["output"]), ("fetch", "A"))

    def test_outside_a_trace_a_traced_function_just_runs(self):
        @fnai.traced
        def double(n):
            return n * 2
        self.assertEqual(double(4), 8)
        self.assertEqual(self.sent(), [])


class _Completions:
    def create(self, **kw):
        return SimpleNamespace(model=kw.get("model"), usage=SimpleNamespace(prompt_tokens=11, completion_tokens=4),
                               choices=["hi"])


class _Messages:
    def create(self, **kw):
        return SimpleNamespace(model=kw.get("model"), usage=SimpleNamespace(input_tokens=7, output_tokens=3), content=["hi"])


class Wrappers(_Base):
    def test_an_openai_call_becomes_an_llm_step_and_the_response_is_untouched(self):
        client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))
        same = fnai.wrap_openai(client)
        self.assertIs(same, client)
        with fnai.trace(input={}) as t:
            resp = client.chat.completions.create(model="gpt-4o-mini", messages=[])
            t.output({"a": 1})
        self.assertEqual(resp.choices, ["hi"])
        (tr,) = self.sent()
        (step,) = tr["steps"]
        self.assertEqual((step["type"], step["model"], step["tokens_in"], step["tokens_out"]), ("llm", "gpt-4o-mini", 11, 4))
        self.assertEqual((tr["tokens_in"], tr["tokens_out"], tr["model"]), (11, 4, "gpt-4o-mini"))

    def test_an_anthropic_call_becomes_an_llm_step(self):
        client = fnai.wrap_anthropic(SimpleNamespace(messages=_Messages()))
        with fnai.trace(input={}):
            client.messages.create(model="claude-sonnet-4-5", max_tokens=5, messages=[])
        (step,) = self.sent()[0]["steps"]
        self.assertEqual((step["model"], step["tokens_in"], step["tokens_out"]), ("claude-sonnet-4-5", 7, 3))

    def test_wrapping_twice_records_once(self):
        client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))
        fnai.wrap_openai(fnai.wrap_openai(client))
        with fnai.trace(input={}):
            client.chat.completions.create(model="m", messages=[])
        self.assertEqual(len(self.sent()[0]["steps"]), 1)

    def test_a_call_outside_a_trace_is_not_recorded_and_still_works(self):
        client = fnai.wrap_openai(SimpleNamespace(chat=SimpleNamespace(completions=_Completions())))
        self.assertEqual(client.chat.completions.create(model="m", messages=[]).choices, ["hi"])
        self.assertEqual(self.sent(), [])

    def test_an_error_from_the_provider_is_not_swallowed(self):
        class Boom:
            def create(self, **kw):
                raise RuntimeError("rate limited")
        client = fnai.wrap_openai(SimpleNamespace(chat=SimpleNamespace(completions=Boom())))
        with fnai.trace(input={}):
            with self.assertRaises(RuntimeError):
                client.chat.completions.create(model="m", messages=[])

    def test_an_async_client_is_wrapped_too(self):
        class ACompletions:
            async def create(self, **kw):
                return SimpleNamespace(model=kw["model"], usage=SimpleNamespace(prompt_tokens=2, completion_tokens=1))
        client = fnai.wrap_openai(SimpleNamespace(chat=SimpleNamespace(completions=ACompletions())))

        async def run():
            with fnai.trace(input={}):
                return await client.chat.completions.create(model="m", messages=[])
        asyncio.run(run())
        (step,) = self.sent()[0]["steps"]
        self.assertEqual((step["tokens_in"], step["tokens_out"]), (2, 1))

    def test_a_client_without_the_expected_parts_is_left_alone(self):
        odd = SimpleNamespace()
        self.assertIs(fnai.wrap_openai(odd), odd)
        self.assertIs(fnai.wrap_anthropic(odd), odd)


class FailOpen(_Base):
    def test_a_trace_refused_for_good_is_dropped_once_and_never_retried_or_raised(self):
        self.server.statuses[:] = [422]
        with fnai.trace(input={}) as t:
            t.output({"a": 1})
        self.sent()
        self.assertEqual(len(self.server.received), 1)         # one attempt, no retry on a 422
        self.assertEqual(fnai.trace_stats()["dropped"], 1)

    def test_a_struggling_server_is_retried_then_succeeds(self):
        self.server.statuses[:] = [503, 500]
        with fnai.trace(input={}) as t:
            t.output({"a": 1})
        self.sent()
        self.assertEqual(len(self.server.received), 3)         # two failures, then it went through
        self.assertEqual(fnai.trace_stats(), {"sent": 1, "dropped": 0, "waiting": 0})

    def test_it_gives_up_after_the_last_retry(self):
        self.server.statuses[:] = [500] * 10
        with fnai.trace(input={}):
            pass
        self.sent()
        self.assertEqual(len(self.server.received), 4)
        self.assertEqual(fnai.trace_stats()["dropped"], 1)

    def test_no_network_loses_the_trace_and_nothing_else(self):
        fnai.init_tracing(api_key="fnk_test", base_url="http://127.0.0.1:1")        # nothing listens there
        result = []
        with fnai.trace(input={"q": 1}) as t:
            result.append("my own code ran")
            t.output({"a": 1})
        self.assertEqual(result, ["my own code ran"])
        self.assertTrue(fnai.flush_traces(10))
        self.assertEqual(fnai.trace_stats()["dropped"], 1)

    def test_a_full_queue_drops_instead_of_blocking(self):
        fnai.init_tracing(api_key="fnk_test", base_url="http://127.0.0.1:1", max_queue=1)
        fnai._RETRY_AFTER_S = (0.3, 0.3, 0.3)                  # keep the sender busy
        started = time.monotonic()
        for _ in range(20):
            with fnai.trace(input={}):
                pass
        self.assertLess(time.monotonic() - started, 1.0)       # never waited on the network
        self.assertGreaterEqual(fnai.trace_stats()["dropped"], 1)
        fnai.flush_traces(10)


class Switches(unittest.TestCase):
    def tearDown(self):
        fnai._TRACER = None

    def test_without_a_key_trace_still_works_and_sends_nothing(self):
        import os
        os.environ.pop("FNAI_API_KEY", None)
        fnai.init_tracing()
        with fnai.trace(input={"q": 1}) as t:
            t.output({"a": 1})
        self.assertEqual(fnai.trace_stats(), {"sent": 0, "dropped": 0, "waiting": 0})
        self.assertTrue(fnai.flush_traces(1))

    def test_the_key_can_come_from_the_environment_and_tracing_can_be_switched_off(self):
        import os
        server = _Server()
        try:
            os.environ["FNAI_API_KEY"] = "fnk_env"
            fnai.init_tracing(base_url=server.url)
            with fnai.trace(input={}):
                pass
            fnai.flush_traces(5)
            self.assertEqual(server.auth, ["Bearer fnk_env"])
            os.environ["FNAI_TRACING"] = "0"
            fnai.init_tracing(base_url=server.url)
            with fnai.trace(input={}):
                pass
            fnai.flush_traces(5)
            self.assertEqual(len(server.received), 1)          # the second was never sent
        finally:
            os.environ.pop("FNAI_API_KEY", None)
            os.environ.pop("FNAI_TRACING", None)
            server.close()


class SpaceKeyTracing(_Base):
    """FNA-REQ-534: a space key (fns_…) covers every function in the space, so each trace names its function."""

    def test_a_space_key_sends_the_function_name_and_a_function_key_does_not(self):
        fnai.init_tracing(api_key="fns_test", base_url=self.server.url, function="ticket_summary")
        with fnai.trace(input={"a": 1}) as t:
            t.output({"b": 2})
        with fnai.trace(input={"a": 1}, function="other_fn") as t2:      # a trace's own name wins
            pass
        sent = self.sent()
        self.assertEqual([s.get("function") for s in sent], ["ticket_summary", "other_fn"])
        fnai.init_tracing(api_key="fnk_test", base_url=self.server.url, function="ticket_summary")
        with fnai.trace(input={"a": 1}, function="ticket_summary") as t3:
            pass
        self.assertTrue(fnai.flush_traces(5))
        self.assertNotIn("function", self.server.received[-1])       # an older server refuses unknown fields


if __name__ == "__main__":
    unittest.main()
