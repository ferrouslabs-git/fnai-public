"""Tests for the fnai-mcp bridge (FNA-REQ-412): it holds no tools of its own, it forwards to the hosted MCP server.

A small in-process HTTP server stands in for the hosted endpoint and speaks just enough of the Streamable HTTP
protocol (initialize, tools/list, tools/call, a session id, a reply as a server-sent event). Needs the optional
`mcp` extra, like the bridge itself."""
import asyncio
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

import fnai_mcp
from mcp import types

TOOLS = [{"name": "who_am_i", "description": "Identify this connection.",
          "inputSchema": {"type": "object", "properties": {"space": {"type": "string"}}}},
         {"name": "launch_experiment", "description": "Run an experiment.",
          "inputSchema": {"type": "object", "properties": {}}}]


class _Hosted(BaseHTTPRequestHandler):
    sessions: set = set()
    seen: list = []              # (auth header, user agent, method, params)
    forget_next = False          # make the next call look like an expired session
    opened = 0

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        cls = type(self)
        cls.seen.append((self.headers.get("Authorization"), self.headers.get("User-Agent"),
                         body.get("method"), body.get("params")))
        if self.headers.get("Authorization") != "Bearer fnp_good":
            return self._send(401, {"detail": "invalid or revoked token"})
        method = body.get("method")
        if method == "initialize":
            cls.opened += 1
            sid = f"s{cls.opened}"
            cls.sessions.add(sid)
            return self._sse({"jsonrpc": "2.0", "id": body["id"], "result": {
                "instructions": "Hosted instructions.", "capabilities": {"tools": {}},
                "protocolVersion": "2025-03-26", "serverInfo": {"name": "functionalai", "version": "0.1.0"}}}, sid)
        if method == "notifications/initialized":
            return self._send(202, None)
        sid = self.headers.get("Mcp-Session-Id")
        if cls.forget_next:
            cls.forget_next = False
            cls.sessions.discard(sid)
        if sid not in cls.sessions:
            return self._send(404, {"detail": "unknown session"})
        if method == "tools/list":
            return self._sse({"jsonrpc": "2.0", "id": body["id"], "result": {"tools": TOOLS}})
        if method == "tools/call":
            name = body["params"]["name"]
            if name == "nope":
                return self._sse({"jsonrpc": "2.0", "id": body["id"], "result": {
                    "content": [{"type": "text", "text": "Error executing tool nope: 403: no permission"}], "isError": True}})
            return self._sse({"jsonrpc": "2.0", "id": body["id"], "result": {
                "content": [{"type": "text", "text": json.dumps({"called": name, "args": body["params"]["arguments"]})}],
                "isError": False}})
        return self._sse({"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32601, "message": "no such method"}})

    def _sse(self, payload, sid=None):
        data = f"event: message\ndata: {json.dumps(payload)}\n\n".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        if sid:
            self.send_header("Mcp-Session-Id", sid)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send(self, code, payload):
        data = json.dumps(payload).encode() if payload is not None else b""
        self.send_response(code)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class BridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = HTTPServer(("127.0.0.1", 0), _Hosted)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_port}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def setUp(self):
        _Hosted.sessions, _Hosted.seen, _Hosted.forget_next, _Hosted.opened = set(), [], False, 0
        fnai_mcp._remote = None

    def _remote(self, token="fnp_good"):
        fnai_mcp._remote = fnai_mcp.HostedMcp(self.base, token)
        return fnai_mcp._remote

    def test_the_bridge_lists_exactly_the_hosted_servers_tools(self):
        self._remote()
        res = asyncio.run(fnai_mcp.list_tools(None))
        self.assertEqual([t.name for t in res.tools], ["who_am_i", "launch_experiment"])
        self.assertEqual(res.tools[0].input_schema["properties"], {"space": {"type": "string"}})

    def test_a_call_is_forwarded_with_the_token_and_the_result_comes_back_unchanged(self):
        self._remote()
        res = asyncio.run(fnai_mcp.call_tool(None, types.CallToolRequestParams(name="who_am_i", arguments={"space": "s"})))
        self.assertFalse(res.is_error)
        self.assertEqual(json.loads(res.content[0].text), {"called": "who_am_i", "args": {"space": "s"}})
        auth, agent, method, params = _Hosted.seen[-1]
        self.assertEqual((auth, method, params["name"]), ("Bearer fnp_good", "tools/call", "who_am_i"))
        self.assertTrue(agent.startswith("fnai-mcp/"))

    def test_the_hosted_servers_own_refusal_is_passed_through_as_an_error(self):
        self._remote()
        res = asyncio.run(fnai_mcp.call_tool(None, types.CallToolRequestParams(name="nope", arguments={})))
        self.assertTrue(res.is_error)
        self.assertIn("403: no permission", res.content[0].text)

    def test_a_bad_token_is_a_clear_error_not_a_crash(self):
        self._remote(token="fnp_bad")
        res = asyncio.run(fnai_mcp.call_tool(None, types.CallToolRequestParams(name="who_am_i", arguments={})))
        self.assertTrue(res.is_error)
        self.assertIn("401", res.content[0].text)
        self.assertIn("invalid or revoked token", res.content[0].text)

    def test_an_unreachable_server_is_a_clear_error(self):
        fnai_mcp._remote = fnai_mcp.HostedMcp("http://127.0.0.1:9", "fnp_good", timeout=2)
        res = asyncio.run(fnai_mcp.call_tool(None, types.CallToolRequestParams(name="who_am_i", arguments={})))
        self.assertTrue(res.is_error)
        self.assertIn("could not reach", res.content[0].text)

    def test_an_expired_session_is_reopened_once_and_the_call_still_succeeds(self):
        r = self._remote()
        asyncio.run(fnai_mcp.list_tools(None))
        first = r.session_id
        _Hosted.forget_next = True
        res = asyncio.run(fnai_mcp.call_tool(None, types.CallToolRequestParams(name="who_am_i", arguments={})))
        self.assertFalse(res.is_error)
        self.assertNotEqual(r.session_id, first)

    def test_the_bridge_adopts_the_hosted_servers_instructions(self):
        r = self._remote()
        r._open()
        self.assertEqual(fnai_mcp.build_server(r.instructions).instructions, "Hosted instructions.")

    def test_no_token_fails_fast_with_how_to_get_one(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(RuntimeError) as cm:
                fnai_mcp.get_remote()
        self.assertIn("FNAI_TOKEN", str(cm.exception))

    def test_the_bridge_defines_no_tools_of_its_own(self):
        self.assertFalse(any(n for n in dir(fnai_mcp) if n.startswith("_summarize")))
        self.assertEqual({n for n in ("list_tools", "call_tool") if hasattr(fnai_mcp, n)}, {"list_tools", "call_tool"})


if __name__ == "__main__":
    unittest.main()
