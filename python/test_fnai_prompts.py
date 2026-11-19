"""FNA-REQ-531: fnai.get_prompt / Prompt.render / trace(prompt=). Stdlib only; a tiny local server stands in for GET /v1/prompts."""
import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

import fnai


def _prompt(version=3, **over):
    return {"function": "ticket_summary", "name": "Ticket summary", "version": version, "version_id": 100 + version,
            "live": True, "artifact_hash": "h" * 64,
            "template": "Summarise in one sentence: {ticket_text}. Tone: {tone}. Keep {unknown_left} as is.",
            "substitution": {"syntax": "curly_lowercase_v1", "vars": ["ticket_text", "tone", "unknown_left"]},
            "input_schema": {"type": "object"}, "output_schema": {"type": "object"},
            "model": "gpt-4o-mini", "settings": {"temperature": 0.2}, **over}


class _Server:
    def __init__(self):
        self.requests, self.answers = [], []       # answers: (status, body) in order; the last repeats
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.requests.append((self.path, self.headers.get("Authorization")))
                status, body = outer.answers[min(len(outer.requests) - 1, len(outer.answers) - 1)]
                payload = json.dumps(body).encode()
                self.send_response(status)
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


class GetPrompt(unittest.TestCase):
    def setUp(self):
        fnai._PROMPT_CACHE.clear()
        self.s = _Server()
        self.addCleanup(self.s.close)
        os.environ["FNAI_API_KEY"] = "fnk_test"
        self.addCleanup(os.environ.pop, "FNAI_API_KEY", None)

    def get(self, *a, **kw):
        return fnai.get_prompt("ticket_summary", *a, base_url=self.s.url, **kw)

    def test_it_fetches_the_live_version_with_the_key_and_exposes_its_parts(self):
        self.s.answers = [(200, _prompt())]
        p = self.get()
        self.assertEqual(self.s.requests, [("/v1/prompts/ticket_summary?version=live", "Bearer fnk_test")])
        self.assertEqual((p.version, p.version_id, p.live, p.model, p.temperature), (3, 103, True, "gpt-4o-mini", 0.2))
        self.assertEqual(p.variables, ["ticket_text", "tone", "unknown_left"])

    def test_a_named_version_is_asked_for_by_number(self):
        self.s.answers = [(200, _prompt(version=2, live=False))]
        p = self.get(2)
        self.assertIn("version=2", self.s.requests[0][0])
        self.assertFalse(p.live)

    def test_render_fills_the_blanks_and_leaves_a_name_with_no_value_as_written(self):
        self.s.answers = [(200, _prompt())]
        p = self.get()
        self.assertEqual(p.render(ticket_text="Printer on fire", tone="calm"),
                         "Summarise in one sentence: Printer on fire. Tone: calm. Keep {unknown_left} as is.")
        self.assertIn("5", p.render({"ticket_text": 5, "tone": "x"}))             # str(value), like the server

    def test_the_answer_is_kept_for_a_while_and_cache_seconds_zero_always_asks(self):
        self.s.answers = [(200, _prompt())]
        self.get(); self.get()
        self.assertEqual(len(self.s.requests), 1)
        self.get(cache_seconds=0)
        self.assertEqual(len(self.s.requests), 2)

    def test_a_struggling_server_gives_the_last_copy_but_a_404_is_an_error(self):
        self.s.answers = [(200, _prompt()), (503, {"detail": "busy"}), (404, {"detail": "not found"})]
        first = self.get(cache_seconds=0)
        again = self.get(cache_seconds=0)                      # 503: the copy from earlier
        self.assertIs(again, first)
        with self.assertRaises(fnai.FnaiError) as cm:
            self.get(cache_seconds=0)                          # 404: say so, never hide it
        self.assertEqual(cm.exception.status, 404)

    def test_with_no_copy_yet_a_failure_is_raised(self):
        self.s.answers = [(503, {"detail": "busy"})]
        with self.assertRaises(fnai.FnaiError):
            self.get()

    def test_no_key_is_a_clear_error(self):
        os.environ.pop("FNAI_API_KEY")
        with self.assertRaisesRegex(ValueError, "FNAI_API_KEY"):
            self.get()

    def test_a_trace_names_the_prompt_version_it_used(self):
        self.s.answers = [(200, _prompt())]
        p = self.get()
        with fnai.trace(input={"a": 1}, prompt=p) as t:
            t.output({"b": 2})
        self.assertEqual(t._envelope(None)["version_id"], 103)
        with fnai.trace(input={}, prompt=p, version_id=7) as t2:      # an explicit id still wins
            pass
        self.assertEqual(t2._envelope(None)["version_id"], 7)


if __name__ == "__main__":
    unittest.main()
