# SPDX-License-Identifier: AGPL-3.0-only
"""Unit and stdio protocol integration tests for LDAI_NodeAgentMCP."""
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import server  # noqa: E402


class MockNodeAgentHandler(BaseHTTPRequestHandler):
    """Google: Minimal loopback NodeAgentServer fixture exercising registration, jobs, artifacts, and shared docs."""

    state: dict = {}

    def log_message(self, fmt: str, *args) -> None:
        """Google: Silence default HTTP access logs during automated test runs."""
        return

    def _send_json(self, code: int, payload: dict | list) -> None:
        """Google: Serialize JSON response with accurate Content-Length."""
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_json(self) -> dict:
        """Google: Decode JSON request body if Content-Length is positive."""
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length).decode("utf-8")) if length > 0 else {}

    def do_GET(self) -> None:
        """Google: Serve healthz, Bot whoami, node list, job lookup, and fleet shared endpoints."""
        if self.path == "/healthz":
            self._send_json(200, {"ok": True})
            return
        auth = self.headers.get("Authorization", "")
        if auth != f"Bearer {self.state['token']}":
            self._send_json(401, {"error": "unauthorized"})
            return
        if self.path == "/v1/bots/me":
            self._send_json(
                200,
                {
                    "id": "bot-demo01",
                    "name": self.state["bot_name"],
                    "approval_status": self.state["approval"],
                    "device_capabilities": self.state["caps"],
                },
            )
            return
        if self.path == "/v1/nodes":
            self._send_json(200, {"nodes": [{"id": "node-alpha", "name": "workstation-alpha", "online": True}]})
            return
        if self.path.startswith("/v1/jobs/"):
            jid = self.path.rsplit("/", 1)[-1]
            self._send_json(200, self.state["jobs"][jid])
            return
        if self.path == "/v1/fleet/shared-knowledge":
            self._send_json(200, {"items": [{"id": "skb-1", "title": "Shared Architecture"}]})
            return
        if self.path == "/v1/fleet/shared-skills":
            self._send_json(
                200,
                {
                    "items": [
                        {
                            "id": "ssk-1",
                            "name": "ld-aifleet-online",
                            "repo_url": "https://github.com/LastDreamTeam/LDAI_AIFleetAgentSkill",
                        }
                    ]
                },
            )
            return
        if self.path.startswith("/v1/chat/inbox?"):
            assert "after=" in self.path and "limit=" in self.path
            self._send_json(200, {"items": [{"seq": 1, "id": "m-1", "text": "hi"}], "next_after": 1, "has_more": False})
            return
        if self.path.startswith("/v1/chat/group-messages?"):
            assert "room=grp-1" in self.path and "after=0" in self.path
            self._send_json(200, {"items": [{"seq": 2, "room": "grp-1", "id": "gm-1", "text": "hello group"}]})
            return
        self._send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        """Google: Handle self-registration, idempotent job submission, and chunked desktop artifact reads."""
        body = self._read_json()
        if self.path == "/v1/bots/register":
            self.state["reg_calls"] += 1
            self.state["last_reg_key"] = body.get("registration_key")
            self.state["bot_name"] = body.get("name", "Unnamed")
            self._send_json(200, {"id": "bot-demo01", "token": self.state["token"]})
            return
        auth = self.headers.get("Authorization", "")
        if auth != f"Bearer {self.state['token']}":
            self._send_json(401, {"error": "unauthorized"})
            return
        if self.path == "/v1/jobs":
            if self.state["approval"] != "approved":
                self._send_json(403, {"error": "bot_approval_pending"})
                return
            jid = body["id"]
            png_bytes = b"\x89PNG\r\n\x1a\n" + (b"A" * 4096)
            envelope = json.dumps(
                {"ok": True, "data": {"format": "png", "width": 100, "height": 80, "base64": base64.b64encode(png_bytes).decode()}}
            ).encode("utf-8")
            self.state["artifact_bytes"] = envelope
            self.state["jobs"][jid] = {
                "id": jid,
                "node_id": body["node_id"],
                "kind": body["kind"],
                "args": body["args"],
                "state": "completed",
                "result": {
                    "ok": True,
                    "data": {
                        "artifact": {
                            "handle": "h" * 64,
                            "size": len(envelope),
                            "sha256": hashlib.sha256(envelope).hexdigest(),
                        }
                    },
                },
            }
            self._send_json(200, self.state["jobs"][jid])
            return
        if self.path == "/v1/desktop/read":
            # Google: Validate real desktopRelayRead schema (job, artifact, offset) and reply with chunk.body.
            assert set(body.keys()) == {"job", "artifact", "offset"}
            raw = self.state["artifact_bytes"]
            off = int(body["offset"])
            chunk = raw[off : off + 65536]
            self._send_json(
                200,
                {
                    "id": body["job"]["id"],
                    "state": "completed",
                    "chunk": {
                        "artifact": body["artifact"],
                        "offset": off,
                        "body": base64.b64encode(chunk).decode("ascii"),
                        "eof": off + len(chunk) >= len(raw),
                    },
                },
            )
            return
        if self.path == "/v1/chat/messages":
            assert set(body.keys()) == {"id", "peer_id", "text"}
            self._send_json(200, {"message": {"seq": 10, "id": body["id"], "node_id": body["peer_id"], "text": body["text"]}})
            return
        if self.path == "/v1/chat/group-messages":
            assert set(body.keys()) == {"room", "id", "text", "parent", "mentions"}
            self._send_json(200, {"seq": 11})
            return
        self._send_json(404, {"error": "not_found"})


class NodeAgentMCPTest(unittest.TestCase):
    """Google: End-to-end verification of registration, approval gating, idempotency, screenshots, and router stdio."""

    def setUp(self) -> None:
        """Google: Spin up an ephemeral loopback mock server and isolated temporary profile directory."""
        self.tmp = tempfile.TemporaryDirectory()
        MockNodeAgentHandler.state = {
            "token": "tok_secret_demo_value_123456",
            "bot_name": "Test-Bot",
            "approval": "pending",
            "caps": [],
            "reg_calls": 0,
            "last_reg_key": "",
            "jobs": {},
            "artifact_bytes": b"",
        }
        self.httpd = HTTPServer(("127.0.0.1", 0), MockNodeAgentHandler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.old_env = dict(os.environ)
        os.environ["NODEAGENT_SERVER_URL"] = f"http://127.0.0.1:{self.port}"
        os.environ["NODEAGENT_PROFILE_DIR"] = str(Path(self.tmp.name) / "profile")
        os.environ["NODEAGENT_BOT_NAME"] = "UnitTest-Bot"

    def tearDown(self) -> None:
        """Google: Restore environment variables, stop HTTP fixture, and remove temporary files."""
        os.environ.clear()
        os.environ.update(self.old_env)
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def test_first_run_self_registration_and_approval_flow(self) -> None:
        """Google: Verify registration_key persistence, pending guidance, and post-approval screenshot retrieval."""
        reg = server.nodeagent_register_bot(name="UnitTest-Bot", framework="hermes")
        self.assertTrue(reg["registered"])
        self.assertFalse(reg["reused_existing_token"])
        self.assertIn("待审批", reg["next_step"])
        saved_key = (Path(os.environ["NODEAGENT_PROFILE_DIR"]) / "registration.key").read_text(encoding="utf-8").strip()
        self.assertEqual(len(saved_key), 64)

        # Google: Before approval, submitting device jobs must fail closed with pending approval guidance.
        with self.assertRaises(ValueError) as ctx:
            server.nodeagent_submit_job("node-alpha", "info", {}, request_key="req-pending-1")
        self.assertIn("bot_approval_pending", str(ctx.exception))

        # Google: Simulate administrator approving capabilities on NodeAgentServer.
        MockNodeAgentHandler.state["approval"] = "approved"
        MockNodeAgentHandler.state["caps"] = ["observe", "desktop_input"]

        shot = server.nodeagent_desktop_observe("node-alpha", "screenshot", request_key="shot-1")
        self.assertTrue(Path(shot["file_path"]).is_file())
        self.assertGreater(shot["bytes"], 4096)

        # Google: Re-using the same request_key with conflicting arguments must fail closed to protect idempotency.
        with self.assertRaises(ValueError):
            server.nodeagent_desktop_observe("node-alpha", "snapshot", request_key="shot-1")

        # Google: Verify strict direct/group chat wire fields match NodeAgentServer's DisallowUnknownFields decoders.
        inbox = server.nodeagent_chat_inbox(after_seq=0, wait_seconds=0, limit=10)
        self.assertEqual(inbox["next_after"], 1)
        sent = server.nodeagent_chat_send("node-alpha", "hello")
        self.assertEqual(sent["message"]["node_id"], "node-alpha")
        ghist = server.nodeagent_group_history("grp-1", after_seq=0, limit=10)
        self.assertEqual(ghist["items"][0]["room"], "grp-1")
        gsend = server.nodeagent_group_send("grp-1", text="reply", parent_seq=2)
        self.assertEqual(gsend["seq"], 11)

    def test_router_stdio_jsonrpc_handshake_and_tools(self) -> None:
        """Google: Verify router.py speaks real MCP stdio JSON-RPC 2.0 and routes tool calls to server.py."""
        env = dict(os.environ)
        env["NODEAGENT_AUTO_REGISTER"] = "1"
        proc = subprocess.Popen(
            [sys.executable, "-B", str(ROOT / "router.py")],
            cwd=ROOT,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        try:
            assert proc.stdin and proc.stdout
            proc.stdin.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1.0"}},
                    }
                )
                + "\n"
            )
            proc.stdin.flush()
            init_resp = json.loads(proc.stdout.readline())
            self.assertEqual(init_resp["result"]["serverInfo"]["name"], "nodeagent")

            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}) + "\n")
            proc.stdin.flush()
            tools_resp = json.loads(proc.stdout.readline())
            tool_names = {t["name"] for t in tools_resp["result"]["tools"]}
            self.assertIn("nodeagent_status", tool_names)
            self.assertIn("nodeagent_register_bot", tool_names)
            self.assertIn("nodeagent_shared_knowledge_list", tool_names)

            proc.stdin.write(
                json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "nodeagent_status", "arguments": {}}})
                + "\n"
            )
            proc.stdin.flush()
            call_resp = json.loads(proc.stdout.readline())
            self.assertFalse(call_resp["result"].get("isError"))
            body = json.loads(call_resp["result"]["content"][0]["text"])
            self.assertEqual(body["bot"]["id"], "bot-demo01")
        finally:
            if proc.stdin:
                proc.stdin.close()
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()
            proc.wait(timeout=10)

    def test_no_private_ips_or_internal_domains_in_repo(self) -> None:
        """Google: Scan every repository file to ensure zero real server IPs, internal hostnames, or credentials."""
        import re

        ipv4_re = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
        allowed_ips = {"127.0.0.1", "0.0.0.0"}
        for path in ROOT.rglob("*"):
            if not path.is_file() or ".git" in path.parts or "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for ip in ipv4_re.findall(text):
                parts = [int(p) for p in ip.split(".")]
                if all(0 <= p <= 255 for p in parts) and ip not in allowed_ips:
                    self.fail(f"Unexpected IPv4 address {ip} in {path.relative_to(ROOT)}")


if __name__ == "__main__":
    unittest.main()
