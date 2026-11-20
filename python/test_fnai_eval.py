"""FNA-REQ-530: fnai.eval / Client.eval.run / `fn eval --task` — a dataset run through your own code. Stdlib only; a tiny
local server stands in for the experiment endpoints."""
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer

import fnai
import fn_cli


class _Fake:
    def __init__(self, rows=3, pass_rate=1.0):
        self.rows = [{"row": i + 1, "input": {"q": f"question {i + 1}"}} for i in range(rows)]
        self.put, self.created, self.finished, self.received, self.polls = {}, [], 0, [], 0
        self.finish_busy = 0
        self.pass_rate = pass_rate
        outer = self

        class H(BaseHTTPRequestHandler):
            def _send(self, obj, code=200):
                payload = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n)) if n else {}

            def do_GET(self):
                p = self.path.split("?")[0]
                if p == "/api/functions/5/datasets":
                    return self._send([{"id": 1, "name": "old"}, {"id": 2, "name": "tickets"}])
                if p == "/api/functions/5/scores":
                    return self._send([{"id": 11}, {"id": 12}])
                if p == "/api/experiments/9/user-run/rows":
                    return self._send({"experiment_id": 9, "status": "pending", "total_rows": len(outer.rows),
                                       "repeats": 1, "rows": outer.rows, "received": outer.received})
                if p == "/api/experiments/9":
                    outer.polls += 1
                    return self._send({"id": 9, "status": "completed" if outer.polls >= 2 else "running"})
                if p == "/api/datasets/2/snapshots":
                    return self._send([{"id": 77}])
                if p == "/api/experiments/9/check-results":
                    items = [{"row": 2, "input": {"q": "question 2"}, "score_name": "has answer", "model": "my-agent",
                              "result": "failed", "feedback": "answer was 'Wrong Co', expected 'Acme'", "trace_id": "t2"}
                             ] if outer.pass_rate < 1 else []
                    return self._send({"total": len(items), "limit": 500, "offset": 0, "items": items})
                if p == "/api/functions/5/score-results":
                    return self._send([{"score": "has answer", "pass_rate": outer.pass_rate}])
                self._send({"detail": "nope " + p}, 404)

            def do_POST(self):
                p, b = self.path, self._body()
                if p == "/api/experiments/9/user-run/finish" and outer.finish_busy > 0:
                    outer.finish_busy -= 1
                    return self._send({"detail": "busy"}, 503)
                if p == "/api/datasets/2/snapshot":
                    return self._send({"id": 77})
                if p == "/api/functions/5/experiments":
                    outer.created.append(b)
                    return self._send({"id": 9, "status": "pending"})
                if p == "/api/experiments/9/user-run/finish":
                    outer.finished += 1
                    return self._send({"id": 9, "status": "pending"})
                self._send({"detail": "nope " + p}, 404)

            def do_PUT(self):
                b = self._body()
                row = int(self.path.rsplit("/", 1)[1])
                outer.put[row] = b
                self._send({"row": row, "repeat": 1, "replaced": False, "received": len(outer.put), "expected": 3})

            def log_message(self, *a):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Eval(unittest.TestCase):
    def setUp(self):
        fnai._EVAL_POLL_S = 0
        self.addCleanup(setattr, fnai, "_EVAL_POLL_S", None)
        self.fake = _Fake()
        self.addCleanup(self.fake.close)
        os.environ["FNAI_TOKEN"] = "fnp_test"
        self.addCleanup(os.environ.pop, "FNAI_TOKEN", None)

    def run_eval(self, task, **kw):
        return fnai.eval(5, task=task, dataset="tickets", score_set=4, base_url=self.fake.url, **kw)

    def test_each_row_goes_through_the_task_and_the_results_are_sent_then_finished(self):
        res = self.run_eval(lambda inp: {"answer": inp["q"].upper()})
        self.assertEqual(self.fake.created, [{"mode": "user_run", "snapshot_id": 77, "repeats": 1, "version_ids": [], "plan_id": 4}])
        self.assertEqual(sorted(self.fake.put), [1, 2, 3])
        self.assertEqual(self.fake.put[2]["output"], {"answer": "QUESTION 2"})
        self.assertTrue(self.fake.put[2]["success"])
        self.assertEqual(self.fake.finished, 1)
        self.assertEqual((res["experiment_id"], res["rows"], res["task_failed"], res["not_run"]), (9, 3, 0, 0))
        self.assertEqual(res["pass_rate"], 1.0)
        self.assertEqual(res["status"], "completed")     # it waited for the scoring, polling until done
        self.assertGreaterEqual(self.fake.polls, 2)
        self.assertIn("/fn/5?panel=experiments", res["url"])

    def test_a_busy_server_when_finishing_is_retried_and_the_run_still_completes(self):
        self.fake.finish_busy = 2
        res = self.run_eval(lambda inp: {"a": 1})
        self.assertEqual((res["status"], res["sent"], self.fake.finished), ("completed", 3, 1))

    def test_a_task_that_raises_fails_that_row_only(self):
        def task(inp):
            if inp["q"].endswith("2"):
                raise RuntimeError("boom")
            return {"ok": True}
        res = self.run_eval(task)
        self.assertEqual(res["task_failed"], 1)
        self.assertFalse(self.fake.put[2]["success"])
        self.assertIn("RuntimeError: boom", self.fake.put[2]["error"])
        self.assertIsNone(self.fake.put[2]["output"])
        self.assertTrue(self.fake.put[1]["success"] and self.fake.put[3]["success"])
        self.assertEqual(self.fake.finished, 1)

    def test_steps_from_traced_tools_and_llm_calls_are_sent_with_the_row(self):
        @fnai.traced
        def lookup(order):
            return {"status": "paid"}

        def task(inp):
            lookup(order=inp["q"])
            t = fnai.current_trace()
            t.llm("gpt-4o-mini", 100, 10, cost=0.001)
            return "done"
        self.run_eval(task)
        body = self.fake.put[1]
        self.assertEqual([s["type"] for s in body["steps"]], ["tool", "llm"])
        self.assertEqual(body["steps"][0]["tool"], "lookup")
        self.assertEqual((body["tokens_in"], body["tokens_out"], body["model"]), (100, 10, "gpt-4o-mini"))
        self.assertEqual(body["output"], {"output": "done"})

    def test_a_row_already_received_is_not_run_again_when_resuming(self):
        self.fake.received = [{"row": 1, "repeat": 1}]
        seen = []
        res = fnai.eval(5, task=lambda i: seen.append(i["q"]) or {"a": 1}, resume=9, base_url=self.fake.url)
        self.assertEqual(seen, ["question 2", "question 3"])
        self.assertEqual(res["sent"], 3)

    def test_concurrency_runs_every_row_once(self):
        res = self.run_eval(lambda inp: {"a": inp["q"]}, concurrency=3)
        self.assertEqual(sorted(self.fake.put), [1, 2, 3])
        self.assertEqual(res["sent"], 3)

    def test_an_async_task_works(self):
        async def task(inp):
            return {"a": 1}
        self.run_eval(task)
        self.assertEqual(len(self.fake.put), 3)

    def test_without_a_score_set_every_check_of_the_function_is_used(self):
        fnai.eval(5, task=lambda i: {"a": 1}, dataset="tickets", base_url=self.fake.url)
        self.assertEqual(self.fake.created[0]["score_ids"], [11, 12])

    def test_a_missing_token_or_dataset_is_a_clear_error(self):
        os.environ.pop("FNAI_TOKEN")
        with self.assertRaises(ValueError):
            fnai.eval(5, task=lambda i: {}, base_url=self.fake.url)
        os.environ["FNAI_TOKEN"] = "x"
        with self.assertRaisesRegex(ValueError, "no dataset named"):
            fnai.eval(5, task=lambda i: {}, dataset="nope", base_url=self.fake.url)


class Cli(unittest.TestCase):
    def setUp(self):
        fnai._EVAL_POLL_S = 0
        self.addCleanup(setattr, fnai, "_EVAL_POLL_S", None)
        self.fake = _Fake()
        self.addCleanup(self.fake.close)
        self.dir = tempfile.mkdtemp()
        with open(os.path.join(self.dir, "my_agent.py"), "w") as f:
            f.write("def run(inp):\n    return {'answer': inp['q']}\n")
        self.old = os.getcwd()
        os.chdir(self.dir)
        self.addCleanup(os.chdir, self.old)
        os.environ.update(FNAI_TOKEN="fnp_test", FNAI_BASE_URL=self.fake.url)
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("FNAI_TOKEN", "FNAI_BASE_URL")])
        self.addCleanup(sys.modules.pop, "my_agent", None)

    def run_cli(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = fn_cli.main(["eval", "5", "--task", "my_agent:run", "--dataset", "tickets", "--score-set", "4", *extra])
        return code, out.getvalue(), err.getvalue()

    def test_fn_eval_task_runs_the_rows_and_prints_a_summary(self):
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(len(self.fake.put), 3)
        self.assertIn("pass rate 100%", out)

    def test_json_prints_only_json(self):
        code, out, _ = self.run_cli("--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["experiment_id"], 9)

    def test_it_exits_nonzero_when_the_pass_rate_is_under_the_threshold(self):
        self.fake.pass_rate = 0.5
        code, _, err = self.run_cli("--min-pass-rate", "0.9")
        self.assertEqual(code, 1)
        self.assertIn("below the required 90%", err)
        self.assertEqual(self.run_cli("--min-pass-rate", "0.4")[0], 0)

    # FNA-REQ-542: the summary names the failed rows and why, so an assistant can fix one without opening the app
    def test_a_failing_run_lists_the_failed_rows_with_the_checks_own_explanation(self):
        self.fake.pass_rate = 0.5
        code, out, _ = self.run_cli()
        self.assertIn("1 of 1 failed row(s):", out)
        self.assertIn("row 2  has answer  my-agent", out)
        self.assertIn("answer was 'Wrong Co', expected 'Acme'", out)
        self.assertIn("/fn/5?panel=experiments", out)

    def test_json_carries_the_failed_rows_and_nothing_else_prints(self):
        self.fake.pass_rate = 0.5
        _, out, _ = self.run_cli("--json")
        res = json.loads(out)
        self.assertEqual(res["failed_total"], 1)
        self.assertEqual(res["failed_rows"][0], {"row": 2, "check": "has answer", "model": "my-agent",
                                                 "input": {"q": "question 2"},
                                                 "why": "answer was 'Wrong Co', expected 'Acme'", "trace_id": "t2"})

    def test_a_passing_run_lists_no_failed_rows(self):
        _, out, _ = self.run_cli("--json")
        self.assertEqual(json.loads(out)["failed_rows"], [])

    def test_a_long_input_is_cut_and_only_the_first_ten_rows_are_shown(self):
        items = [{"row": i, "input": {"q": "x" * 900}, "score_name": "c", "result": "failed", "feedback": "bad"}
                 for i in range(1, 16)]

        exps = fnai._Experiments(None)
        exps.check_results = lambda *a, **k: items
        res = fnai.failed_rows(exps, 9)
        self.assertEqual((res["failed_total"], len(res["failed_rows"])), (15, 10))
        self.assertTrue(res["failed_rows"][0]["input"].endswith("…") and len(res["failed_rows"][0]["input"]) <= 401)

    def test_a_bad_task_spec_is_a_readable_error(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = fn_cli.main(["eval", "5", "--task", "nothere:run"])
        self.assertEqual(code, 1)
        self.assertIn("couldn't import nothere", err.getvalue())


if __name__ == "__main__":
    unittest.main()


class PlainFnEval(unittest.TestCase):
    """`fn eval 5` (the app runs the model, not your code) prints the same failed-row summary."""

    def setUp(self):
        self.fake = _Fake()
        self.addCleanup(self.fake.close)
        os.environ.update(FNAI_TOKEN="fnp_test", FNAI_BASE_URL=self.fake.url)
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("FNAI_TOKEN", "FNAI_BASE_URL")])
        orig = self.fake.httpd.RequestHandlerClass.do_GET

        def get(h):
            if h.path.split("?")[0] == "/api/experiments/9":       # a finished run, as the app creates it
                return h._send({"id": 9, "status": "completed", "results": {"baseline": {"verdict": "worse"}}})
            return orig(h)
        self.fake.httpd.RequestHandlerClass.do_GET = get

    def run_cli(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = fn_cli.main(["eval", "5", "--version", "3", *extra])
        return code, out.getvalue(), err.getvalue()

    def test_a_failing_run_names_the_failed_rows_and_still_exits_nonzero(self):
        self.fake.pass_rate = 0.5
        code, out, err = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("FAIL  has answer", out)
        self.assertIn("answer was 'Wrong Co', expected 'Acme'", out)
        self.assertIn("1 check(s) failed", err)

    def test_json_is_only_json_and_says_how_the_run_compares(self):
        self.fake.pass_rate = 0.5
        code, out, _ = self.run_cli("--json")
        res = json.loads(out)                                         # progress text went to stderr, so this parses
        self.assertEqual(code, 1)
        self.assertFalse(res["passed"])
        self.assertEqual(res["failed_rows"][0]["row"], 2)
        self.assertEqual(res["compared_with"], {"verdict": "worse"})
        self.assertIn("/fn/5?panel=experiments", res["url"])

    def test_a_passing_run_says_so(self):
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("All checks passed.", out)
