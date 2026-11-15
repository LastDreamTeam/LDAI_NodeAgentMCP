# SPDX-License-Identifier: AGPL-3.0-only
"""Stdio MCP connector for NodeAgentServer and local NodeAgentClient with self-registration and idempotent jobs."""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import sys
import time
from typing import Annotated, Any, Literal
from urllib.parse import urlencode, urlsplit
import uuid

HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
SENSITIVE_KEYS = ("password", "token", "secret", "apikey", "privatekey", "authorization", "cookie", "registrationkey")


def profile_dir() -> Path:
    """Google: Resolve the private local directory storing Bot registration keys, tokens, and job journals."""
    explicit = os.getenv("NODEAGENT_PROFILE_DIR", "").strip()
    alias = os.getenv("LDAI_ACCOUNT_ALIAS", "default").strip() or "default"
    base = (
        Path(explicit).expanduser()
        if explicit
        else Path.home() / ".config" / "ldai-mcp" / "nodeagent" / "profiles" / alias
    )
    base.mkdir(parents=True, exist_ok=True)
    return base


def write_private_file(path: Path, text: str) -> None:
    """Google: Persist secret material atomically with owner-only 0600 permissions where supported."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    try:
        os.write(fd, (text.strip() + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    tmp.replace(path)


def read_private_file(path: Path) -> str:
    """Google: Read trimmed UTF-8 secret or identity material if the file exists."""
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8-sig").strip()


def scrub_public(value: Any, *, allow_git_credential: bool = False) -> Any:
    """Google: Strip credential-bearing fields from API responses unless explicitly brokering git push auth."""
    if isinstance(value, list):
        return [scrub_public(v, allow_git_credential=allow_git_credential) for v in value]
    if isinstance(value, dict):
        cleaned = {}
        for k, v in value.items():
            norm = k.lower().replace("_", "").replace("-", "")
            if allow_git_credential and norm == "token":
                cleaned[k] = v
                continue
            if any(s in norm for s in SENSITIVE_KEYS):
                continue
            cleaned[k] = scrub_public(v, allow_git_credential=allow_git_credential)
        return cleaned
    return value


def validate_server_url(raw: str) -> tuple[str, str, int, str]:
    """Google: Enforce HTTPS for remote NodeAgentServer endpoints and allow HTTP only on loopback."""
    base = raw.strip().rstrip("/")
    parsed = urlsplit(base)
    if (
        not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not (
            parsed.scheme == "https"
            or (parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1"))
        )
    ):
        raise ValueError("Set NODEAGENT_SERVER_URL to an HTTPS endpoint or an HTTP loopback address.")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.scheme, parsed.hostname, port, parsed.path


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Google: Verify exact leaf certificate DER SHA256 fingerprint when NODEAGENT_TLS_FINGERPRINT is set."""

    def __init__(self, host: str, port: int, fingerprint: str, timeout: float = 30.0):
        """Google: Configure TLS context for either leaf-pin verification or standard CA validation."""
        self.expected_pin = fingerprint.lower()
        if self.expected_pin:
            if not HEX64_RE.fullmatch(self.expected_pin):
                raise ValueError("NODEAGENT_TLS_FINGERPRINT must be a 64-character hex SHA256 digest.")
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        else:
            ctx = ssl.create_default_context()
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        super().__init__(host, port=port, timeout=timeout, context=ctx)

    def connect(self) -> None:
        """Google: Complete TLS handshake and reject the socket immediately if leaf SHA256 mismatches."""
        super().connect()
        if self.expected_pin:
            der = self.sock.getpeercert(binary_form=True)
            if not der or hashlib.sha256(der).hexdigest().lower() != self.expected_pin:
                self.close()
                raise ValueError("Server TLS certificate SHA256 did not match NODEAGENT_TLS_FINGERPRINT.")


def load_bot_token() -> str:
    """Google: Read the Bot bearer token from NODEAGENT_TOKEN_FILE or the profile's bot.token file."""
    explicit = os.getenv("NODEAGENT_TOKEN_FILE", "").strip()
    if explicit:
        return read_private_file(Path(explicit).expanduser())
    return read_private_file(profile_dir() / "bot.token")


def raw_server_request(
    method: str,
    path: str,
    body: dict | bytes | None = None,
    *,
    auth_required: bool = True,
    content_type: str = "application/json",
    timeout: float = 30.0,
    max_bytes: int = 18 * 1024 * 1024,
) -> tuple[int, dict[str, str], bytes]:
    """Google: Perform a redirect-free HTTP/HTTPS request to NodeAgentServer with optional leaf cert pinning."""
    server_url = os.getenv("NODEAGENT_SERVER_URL", "")
    scheme, host, port, base_path = validate_server_url(server_url)
    fingerprint = os.getenv("NODEAGENT_TLS_FINGERPRINT", "").strip()

    headers: dict[str, str] = {"Accept": "application/json"}
    token = load_bot_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif auth_required:
        raise ValueError(
            "No Bot token found. Call nodeagent_register_bot first to self-register and wait for Server approval."
        )

    payload: bytes | None = None
    if isinstance(body, dict):
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = content_type
    elif isinstance(body, (bytes, bytearray)):
        payload = bytes(body)
        headers["Content-Type"] = content_type

    conn: http.client.HTTPConnection
    if scheme == "https":
        conn = PinnedHTTPSConnection(host, port, fingerprint=fingerprint, timeout=timeout)
    else:
        conn = http.client.HTTPConnection(host, port=port, timeout=timeout)

    try:
        conn.request(method.upper(), base_path + path, body=payload, headers=headers)
        resp = conn.getresponse()
        # Google: Never follow redirects so Bearer tokens cannot leak across origins.
        if 300 <= resp.status < 400:
            raise ValueError(f"Refusing unexpected HTTP redirect ({resp.status}) from NodeAgentServer.")
        data = resp.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError("Response exceeded maximum safety size limit.")
        resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, resp_headers, data
    except (socket.timeout, TimeoutError, OSError) as exc:
        raise ValueError(f"NodeAgentServer connection failed: {exc}") from None
    finally:
        conn.close()


def server_json(
    method: str,
    path: str,
    body: dict | None = None,
    *,
    auth_required: bool = True,
    allow_git_credential: bool = False,
    timeout: float = 30.0,
) -> Any:
    """Google: Execute a JSON API call against NodeAgentServer and surface structured error codes clearly."""
    status, _headers, data = raw_server_request(
        method, path, body, auth_required=auth_required, timeout=timeout
    )
    parsed: Any = {}
    if data:
        try:
            parsed = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ValueError(f"NodeAgentServer returned non-JSON HTTP {status}.") from None
    if status >= 400:
        err_code = parsed.get("error") if isinstance(parsed, dict) else None
        detail = parsed.get("detail") if isinstance(parsed, dict) else None
        if status == 403 and err_code in ("bot_approval_pending", "bot_not_approved", "capability_not_approved"):
            raise ValueError(
                f"Server denied request ({err_code}): Bot account is awaiting administrator capability approval in NodeAgentServer -> Bot账号."
            )
        msg = f"NodeAgentServer HTTP {status}"
        if err_code:
            msg += f" ({err_code})"
        if detail:
            msg += f": {detail}"
        raise ValueError(msg)
    return scrub_public(parsed, allow_git_credential=allow_git_credential)


def ensure_or_register_bot(
    name: str = "",
    framework: str = "",
    model: str = "",
    webhook_url: str = "",
    a2a_url: str = "",
) -> dict[str, Any]:
    """Google: Self-register a stable Bot identity on first run without rotating existing registration keys."""
    pdir = profile_dir()
    key_file = pdir / "registration.key"
    token_file = pdir / "bot.token"
    meta_file = pdir / "identity.json"

    # Google: Generate and persist registration_key BEFORE contacting Server so retries reuse the same identity.
    reg_key = read_private_file(key_file)
    if not reg_key:
        reg_key = secrets.token_hex(32)
        write_private_file(key_file, reg_key)

    bot_name = (name or os.getenv("NODEAGENT_BOT_NAME") or f"MCP-Agent-{alias_suffix()}").strip()
    bot_framework = (framework or os.getenv("NODEAGENT_FRAMEWORK") or "mcp").strip()
    bot_model = (model or os.getenv("NODEAGENT_MODEL") or "").strip()

    existing_token = load_bot_token()
    if existing_token:
        try:
            me = server_json("GET", "/v1/bots/me")
            return {
                "registered": True,
                "reused_existing_token": True,
                "profile_dir": str(pdir),
                "bot": me,
                "next_step": approval_guidance(me),
            }
        except ValueError:
            # Google: Only re-invoke /v1/bots/register with the SAME registration_key if token is missing/expired.
            pass

    req_body: dict[str, Any] = {
        "name": bot_name,
        "registration_key": reg_key,
    }
    if bot_framework:
        req_body["framework"] = bot_framework
    if bot_model:
        req_body["model"] = bot_model
    if webhook_url:
        req_body["webhook_url"] = webhook_url
    if a2a_url:
        req_body["a2a_url"] = a2a_url

    status, _headers, raw = raw_server_request("POST", "/v1/bots/register", req_body, auth_required=False)
    data = json.loads(raw.decode("utf-8")) if raw else {}
    if status >= 400:
        err = data.get("error", f"http_{status}") if isinstance(data, dict) else f"http_{status}"
        raise ValueError(f"Bot self-registration failed ({err}). Verify NODEAGENT_SERVER_URL and registration policy.")

    issued_token = (data.get("token") or "").strip()
    if not issued_token:
        raise ValueError("Server accepted registration but did not return a Bot token.")
    write_private_file(token_file, issued_token)
    meta = {
        "bot_id": data.get("id") or data.get("bot_id") or "",
        "name": bot_name,
        "framework": bot_framework,
        "registered_at": int(time.time()),
    }
    write_private_file(meta_file, json.dumps(meta, ensure_ascii=False, indent=2))
    me = server_json("GET", "/v1/bots/me")
    return {
        "registered": True,
        "reused_existing_token": False,
        "profile_dir": str(pdir),
        "bot": me,
        "next_step": approval_guidance(me),
    }


def alias_suffix() -> str:
    """Google: Produce a deterministic short suffix from the local profile path without exposing hostnames."""
    return hashlib.sha256(str(profile_dir()).encode("utf-8")).hexdigest()[:6]


def approval_guidance(me: Any) -> str:
    """Google: Explain current Bot approval status so agents pause cleanly until initial approval completes."""
    if not isinstance(me, dict):
        return "Verify Bot identity via nodeagent_status."
    bot_obj = me.get("bot") if isinstance(me.get("bot"), dict) else me
    approval = str(bot_obj.get("approval_status") or "")
    caps = bot_obj.get("device_capabilities") or []
    if approval == "pending":
        return (
            "首次注册已完成，当前状态为待审批 (pending)。请等待管理员在 NodeAgentServer「Bot账号」页面勾选能力并批准后再执行终端操作。"
        )
    if approval == "approved":
        return f"账号已获批，已授权能力：{', '.join(caps) if caps else '无终端能力（仅聊天）'}。"
    if approval in ("revoked", "rejected") or bot_obj.get("disabled"):
        return "账号当前已被撤权或停用，请勿尝试更换身份绕过限制，需由管理员恢复授权。"
    return "账号已就绪。"


def local_client_session() -> dict[str, str] | None:
    """Google: Discover a co-located NodeAgentClient loopback panel-session.json when running on a workstation."""
    candidates = []
    explicit = os.getenv("NODEAGENT_CLIENT_STATE_DIR", "").strip()
    if explicit:
        candidates.append(Path(explicit).expanduser())
    appdata = os.getenv("APPDATA", "").strip()
    if appdata:
        candidates.append(Path(appdata) / "NodeAgentClient")
    candidates.append(Path.home() / ".config" / "NodeAgentClient")
    for folder in candidates:
        sess_file = folder / "panel-session.json"
        if sess_file.is_file():
            try:
                info = json.loads(sess_file.read_text(encoding="utf-8-sig"))
                addr = str(info.get("address") or "").strip()
                tok = str(info.get("token") or "").strip()
                if addr and tok:
                    return {"address": addr, "token": tok, "state_dir": str(folder)}
            except (OSError, ValueError):
                continue
    return None


def call_local_client(method: str, path: str, body: dict | None = None) -> Any:
    """Google: Invoke the local workstation's NodeAgentClient loopback API using same-user panel-session auth."""
    sess = local_client_session()
    if not sess:
        raise ValueError("Local NodeAgentClient session not found on this machine.")
    addr = sess["address"]
    host, _, port_str = addr.rpartition(":")
    host = host.strip("[]") or "127.0.0.1"
    port = int(port_str)
    headers = {
        "Host": addr,
        "Accept": "application/json",
        "Authorization": f"Bearer {sess['token']}",
    }
    payload = None
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    conn = http.client.HTTPConnection(host, port=port, timeout=20.0)
    try:
        conn.request(method.upper(), path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read(16 * 1024 * 1024)
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
        if resp.status >= 400:
            err = parsed.get("error") if isinstance(parsed, dict) else f"http_{resp.status}"
            raise ValueError(f"Local NodeAgentClient returned HTTP {resp.status} ({err}).")
        return scrub_public(parsed)
    finally:
        conn.close()


def resolve_or_create_job_id(request_key: str, node_id: str, kind: str, args: dict) -> tuple[str, bool]:
    """Google: Bind caller request_key to a single persistent job_id before dispatch to prevent replay."""
    journals = profile_dir() / "requests"
    journals.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(request_key.encode("utf-8")).hexdigest()
    entry_file = journals / f"{digest}.json"
    canonical = hashlib.sha256(
        json.dumps({"node_id": node_id, "kind": kind, "args": args}, sort_keys=True).encode("utf-8")
    ).hexdigest()
    if entry_file.is_file():
        saved = json.loads(entry_file.read_text(encoding="utf-8"))
        if saved.get("payload_sha256") != canonical:
            raise ValueError("request_key already exists with different node_id/kind/args; use a new request_key.")
        return str(saved["job_id"]), True
    job_id = f"job-{uuid.uuid4().hex}"
    record = {
        "request_key": request_key,
        "job_id": job_id,
        "node_id": node_id,
        "kind": kind,
        "payload_sha256": canonical,
        "created_at": int(time.time()),
    }
    write_private_file(entry_file, json.dumps(record, ensure_ascii=False, indent=2))
    return job_id, False


def fetch_desktop_artifact(job: dict[str, Any], artifact: dict[str, Any]) -> bytes:
    """Google: Pull chunked desktop snapshot or screenshot bytes via /v1/desktop/read and verify SHA256."""
    buf = bytearray()
    offset = 0
    total_size = int(artifact["size"])
    expected_sha256 = str(artifact.get("sha256") or "")
    # Google: Construct strict wire.Job and desktopRelayArtifact envelopes matching Hub DisallowUnknownFields decoding.
    wire_job: dict[str, Any] = {
        "id": str(job["id"]),
        "node_id": str(job["node_id"]),
        "kind": str(job["kind"]),
        "args": job["args"],
    }
    if job.get("auth"):
        wire_job["auth"] = job["auth"]
    wire_artifact = {
        "handle": str(artifact["handle"]),
        "sha256": expected_sha256,
        "size": total_size,
    }
    while offset < total_size:
        res = server_json(
            "POST",
            "/v1/desktop/read",
            {
                "job": wire_job,
                "artifact": wire_artifact,
                "offset": offset,
            },
            timeout=30.0,
        )
        chunk_obj = res.get("chunk") if isinstance(res, dict) and isinstance(res.get("chunk"), dict) else {}
        b64 = chunk_obj.get("body") or res.get("data_base64") or res.get("chunk_base64") or res.get("data") or ""
        chunk = base64.b64decode(b64) if b64 else b""
        if not chunk:
            raise ValueError("Desktop artifact read ended prematurely or was revoked mid-transfer.")
        buf.extend(chunk)
        offset += len(chunk)
    raw = bytes(buf)
    if expected_sha256 and hashlib.sha256(raw).hexdigest().lower() != expected_sha256.lower():
        raise ValueError("Desktop artifact SHA256 verification failed.")
    return raw


def wait_for_job_completion(job_id: str, wait_seconds: float = 20.0) -> dict[str, Any]:
    """Google: Poll original job_id until completion or timeout without ever re-submitting a new job."""
    deadline = time.monotonic() + max(1.0, min(wait_seconds, 120.0))
    last: dict[str, Any] = {}
    while True:
        last = server_json("GET", f"/v1/jobs/{job_id}")
        job = last.get("job") if isinstance(last, dict) and isinstance(last.get("job"), dict) else last
        if isinstance(job, dict) and job.get("state") == "completed":
            return job
        if time.monotonic() >= deadline:
            return {
                "job": job,
                "timed_out_waiting": True,
                "notice": "Job has not reached completed state yet. Query nodeagent_get_job with this SAME job_id; do NOT replay with a new ID.",
            }
        time.sleep(0.35)


# ---------------------------------------------------------------------------
# Tool Implementations (exposed via FastMCP or fallback stdio JSON-RPC)
# ---------------------------------------------------------------------------

def nodeagent_status() -> dict[str, Any]:
    """Read Server health, current Bot approval/capabilities, and co-located NodeAgentClient status if present."""
    result: dict[str, Any] = {"profile_dir": str(profile_dir())}
    server_url = os.getenv("NODEAGENT_SERVER_URL", "").strip()
    if server_url:
        result["server_url"] = server_url
        result["health"] = server_json("GET", "/healthz", auth_required=False)
        if load_bot_token():
            me = server_json("GET", "/v1/bots/me")
            result["bot"] = me
            result["guidance"] = approval_guidance(me)
        elif os.getenv("NODEAGENT_AUTO_REGISTER", "").strip() in ("1", "true", "yes"):
            reg = ensure_or_register_bot()
            result["bot"] = reg["bot"]
            result["guidance"] = reg["next_step"]
        else:
            result["bot"] = None
            result["guidance"] = "尚未注册本机 Bot 令牌；请调用 nodeagent_register_bot 完成首次自助注册并等待管理员审批。"
    local_sess = local_client_session()
    if local_sess:
        try:
            state = call_local_client("GET", "/api/state")
            result["local_client"] = {
                "detected": True,
                "node_id": state.get("node_id"),
                "connection_state": state.get("connection_state"),
                "paused": state.get("paused"),
            }
        except Exception as exc:
            result["local_client"] = {"detected": True, "error": str(exc)}
    else:
        result["local_client"] = {"detected": False}
    return result


def nodeagent_register_bot(
    name: str = "",
    framework: str = "mcp",
    model: str = "",
    webhook_url: str = "",
    a2a_url: str = "",
) -> dict[str, Any]:
    """Self-register this Agent/Bot on NodeAgentServer on first use and await initial administrator approval."""
    return ensure_or_register_bot(
        name=name, framework=framework, model=model, webhook_url=webhook_url, a2a_url=a2a_url
    )


def nodeagent_update_profile(
    display_name: str = "",
    model: str = "",
    framework: str = "",
    webhook_url: str = "",
    a2a_url: str = "",
) -> Any:
    """Update this Bot's runtime model, framework, or Webhook/A2A push endpoints on NodeAgentServer."""
    body = {
        k: v
        for k, v in {
            "display_name": display_name,
            "model": model,
            "framework": framework,
            "webhook_url": webhook_url,
            "a2a_url": a2a_url,
        }.items()
        if v
    }
    if not body:
        raise ValueError("Provide at least one profile field to update.")
    return server_json("POST", "/v1/bots/me/profile", body)


def nodeagent_list_nodes() -> Any:
    """List deduplicated terminals visible to this Bot along with OS, online status, and capabilities."""
    return server_json("GET", "/v1/nodes")


def nodeagent_get_node(node_id: str) -> Any:
    """Read detailed status and granted capabilities for a single terminal node_id."""
    if not SAFE_ID_RE.fullmatch(node_id):
        raise ValueError("Invalid node_id.")
    return server_json("GET", f"/v1/nodes/{node_id}")


def nodeagent_submit_job(
    node_id: str,
    kind: Literal["info", "exec", "http_bridge", "native_dialog", "remote_control"],
    args: dict[str, Any],
    request_key: str,
    wait_seconds: float = 15.0,
) -> Any:
    """Submit a capability-authorized job idempotently (no operation_code) and optionally wait on original job_id."""
    if not SAFE_ID_RE.fullmatch(node_id):
        raise ValueError("Invalid node_id.")
    if not request_key or len(request_key) > 200:
        raise ValueError("Provide a stable unique request_key (1..200 chars) for idempotency.")
    job_id, existed = resolve_or_create_job_id(request_key, node_id, kind, args)
    if not existed:
        server_json(
            "POST",
            "/v1/jobs",
            {"id": job_id, "node_id": node_id, "kind": kind, "args": args},
        )
    if wait_seconds > 0:
        return wait_for_job_completion(job_id, wait_seconds=wait_seconds)
    return server_json("GET", f"/v1/jobs/{job_id}")


def nodeagent_get_job(job_id: str) -> Any:
    """Inspect the status, non-terminal phase, or completed result of an existing job_id without replaying."""
    if not SAFE_ID_RE.fullmatch(job_id):
        raise ValueError("Invalid job_id.")
    return server_json("GET", f"/v1/jobs/{job_id}")


def nodeagent_wait_job(job_id: str, wait_seconds: float = 25.0) -> Any:
    """Wait up to wait_seconds for an existing job_id to finish and return its authoritative state."""
    if not SAFE_ID_RE.fullmatch(job_id):
        raise ValueError("Invalid job_id.")
    return wait_for_job_completion(job_id, wait_seconds=wait_seconds)


# Google: nodeagent_desktop_observe submits a read-only desktop observation job and reassembles chunked PNG/UIA artifacts.
def nodeagent_desktop_observe(
    node_id: str,
    command: Literal["list-windows", "snapshot", "screenshot", "status", "target", "version", "permissions", "get"],
    request_key: str,
    window_id: str = "",
    element_id: str = "",
    backend: str = "",
) -> dict[str, Any]:
    """Observe a terminal's desktop windows, UIA control tree, control value, or PNG screenshot via capability approval."""
    desktop_req: dict[str, Any] = {"command": command}
    # Google: Only attach window_id or ref on commands permitted by NodeAgentClient's strict DisallowUnknownFields parser.
    if element_id and command == "get":
        desktop_req["ref"] = element_id
    elif window_id:
        desktop_req["window_id"] = window_id
    if backend:
        desktop_req["backend"] = backend
    job = nodeagent_submit_job(
        node_id=node_id,
        kind="remote_control",
        args={"action": "desktop", "desktop": desktop_req},
        request_key=request_key,
        wait_seconds=25.0,
    )
    result = job.get("result") if isinstance(job, dict) else None
    if not isinstance(result, dict) or not result.get("ok"):
        return {"job": job}
    data = result.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            pass
    artifact = data.get("artifact") if isinstance(data, dict) else None
    if not isinstance(artifact, dict) or not artifact.get("handle"):
        return {"job_id": job.get("id"), "desktop": data, "result": result}

    raw_bytes = fetch_desktop_artifact(job=job, artifact=artifact)
    envelope = json.loads(raw_bytes.decode("utf-8"))
    if command == "screenshot":
        payload_data = envelope.get("data") if isinstance(envelope, dict) else {}
        # Google: NodeAgentClient fullscreen_windows.go and agent-desktop screenshot emit base64 PNG under payload_data["data"].
        png_b64 = (
            payload_data.get("data")
            or payload_data.get("base64")
            or payload_data.get("png_base64")
            or ""
        ) if isinstance(payload_data, dict) else ""
        if png_b64:
            png_bytes = base64.b64decode(png_b64)
            out_dir = profile_dir() / "artifacts"
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / f"{job['id']}.png"
            out_path.write_bytes(png_bytes)
            return {
                "job_id": job["id"],
                "file_path": str(out_path),
                "bytes": len(png_bytes),
                "sha256": hashlib.sha256(png_bytes).hexdigest(),
                "width": payload_data.get("width"),
                "height": payload_data.get("height"),
            }
    return {"job_id": job["id"], "desktop": envelope}


# Google: nodeagent_desktop_act builds a strict NodeAgentClient desktop input payload while honoring human-input yield gates.
def nodeagent_desktop_act(
    node_id: str,
    command: Literal["launch", "click", "set-value", "press", "point-click", "window-click", "focus-window", "sequence"],
    request_key: str,
    app: str = "",
    window_id: str = "",
    element_id: str = "",
    element_role: str = "",
    element_name: str = "",
    text: str | None = None,
    key: str = "",
    keys: list[str] | None = None,
    interval_ms: int = 0,
    input_idle_ms: int = 0,
    x: int | None = None,
    y: int | None = None,
) -> Any:
    """Execute a capability-approved desktop input operation while preserving human input priority and pause gates."""
    desktop_req: dict[str, Any] = {"command": command}
    if app:
        desktop_req["app"] = app
    # Google: Ref-based UIA commands (click/set-value without element_role) forbid window_id in desktop.Parse.
    if element_id and not element_role:
        desktop_req["ref"] = element_id
    elif window_id:
        desktop_req["window_id"] = window_id
    if element_role:
        desktop_req["element_role"] = element_role
    if element_name:
        desktop_req["element_name"] = element_name
    if text is not None and command == "set-value":
        desktop_req["text"] = text
    if key:
        desktop_req["key"] = key
    if keys:
        desktop_req["keys"] = keys
    if interval_ms > 0 and command == "sequence":
        desktop_req["interval_ms"] = interval_ms
    if input_idle_ms > 0:
        desktop_req["input_idle_ms"] = input_idle_ms
    if x is not None:
        desktop_req["x"] = x
    if y is not None:
        desktop_req["y"] = y
    return nodeagent_submit_job(
        node_id=node_id,
        kind="remote_control",
        args={"action": "desktop", "desktop": desktop_req},
        request_key=request_key,
        wait_seconds=25.0,
    )


# Google: nodeagent_remote_desktop_frame opens or polls a continuous remote desktop session and saves the latest JPEG frame locally.
def nodeagent_remote_desktop_frame(
    node_id: str,
    mode: Literal["watch", "control"] = "watch",
    session_id: str = "",
    after_seq: int = 0,
) -> dict[str, Any]:
    """Open or poll a continuous remote desktop session and save the latest JPEG frame locally."""
    if not SAFE_ID_RE.fullmatch(node_id):
        raise ValueError("Invalid node_id.")
    sid = session_id.strip()
    if not sid:
        created = server_json("POST", "/v1/rdp/sessions", {"node_id": node_id, "mode": mode})
        sid = str(created.get("session_id") or "")
    # Google: NodeAgentClient idle-polls /v1/node/rdp/sync every 2000ms, so wait up to 3500ms (with one retry on seq=0) for initial frame delivery.
    frame = server_json("GET", f"/v1/rdp/sessions/{sid}/frame?{urlencode({'after_seq': after_seq, 'wait_ms': 3500})}")
    if not (frame.get("data_base64") or "") and int(frame.get("seq") or 0) == 0:
        frame = server_json("GET", f"/v1/rdp/sessions/{sid}/frame?{urlencode({'after_seq': after_seq, 'wait_ms': 3500})}")
    b64 = frame.get("data_base64") or ""
    saved_path = ""
    if b64:
        raw = base64.b64decode(b64)
        out_dir = profile_dir() / "artifacts"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / f"rdp-{sid}-{frame.get('seq', 0)}.jpg"
        out_file.write_bytes(raw)
        saved_path = str(out_file)
    return {
        "session_id": sid,
        "seq": frame.get("seq"),
        "width": frame.get("width"),
        "height": frame.get("height"),
        "mode": frame.get("mode"),
        "transport": frame.get("transport"),
        "file_path": saved_path,
    }


def nodeagent_remote_desktop_input(
    session_id: str,
    event_type: Literal["mouse_move", "mouse_click", "mouse_dblclick", "mouse_down", "mouse_up", "mouse_wheel", "key_press"],
    x: int = 0,
    y: int = 0,
    button: str = "left",
    delta_y: int = 0,
    key: str = "",
) -> Any:
    """Send a mouse or keyboard input event to an active control-mode remote desktop session."""
    if not SAFE_ID_RE.fullmatch(session_id):
        raise ValueError("Invalid session_id.")
    return server_json(
        "POST",
        f"/v1/rdp/sessions/{session_id}/input",
        {"type": event_type, "x": x, "y": y, "button": button, "delta_y": delta_y, "key": key},
    )


def nodeagent_chat_bots() -> Any:
    """List Bots on NodeAgentServer with avatars, models, frameworks, and online heartbeat presence."""
    return server_json("GET", "/v1/chat/bots")


def nodeagent_chat_presence(enabled: bool = True, model: str = "", framework: str = "") -> Any:
    """Send a Bot online heartbeat presence update to NodeAgentServer."""
    body: dict[str, Any] = {"enabled": enabled}
    if model:
        body["model"] = model
    if framework:
        body["framework"] = framework
    return server_json("POST", "/v1/chat/presence", body)


# Google: nodeagent_chat_inbox polls direct Chat inbox messages delivered to this Bot using NodeAgentServer's after/limit pagination.
def nodeagent_chat_inbox(after_seq: int = 0, wait_seconds: int = 0, limit: int = 50) -> Any:
    """Poll direct Chat inbox messages delivered to this Bot."""
    # Google: NodeAgentServer chatPage reads query keys "after" and "limit", plus bounded "wait_seconds" (0..20).
    q = urlencode({"after": max(0, after_seq), "limit": max(1, min(limit, 100)), "wait_seconds": max(0, min(wait_seconds, 20))})
    return server_json("GET", f"/v1/chat/inbox?{q}", timeout=30.0)


# Google: nodeagent_chat_send sends an idempotent direct Chat message from this Bot to a target peer_id (node_id or admin-console).
def nodeagent_chat_send(node_id: str, text: str, message_id: str = "") -> Any:
    """Send an instant direct Chat message from this Bot to a target node or console conversation."""
    mid = message_id.strip() or f"msg-{uuid.uuid4().hex}"
    # Google: NodeAgentServer chatSend decodes strict JSON fields {"id", "peer_id", "text", "attachment_ids"}.
    return server_json("POST", "/v1/chat/messages", {"id": mid, "peer_id": node_id, "text": text})


def nodeagent_group_list() -> Any:
    """List multi-Bot group chat rooms where this Bot is a member."""
    return server_json("GET", "/v1/chat/groups")


# Google: nodeagent_group_history reads paginated message history in a multi-Bot group chat room via NodeAgentServer's room/after/limit parameters.
def nodeagent_group_history(room_id: str, after_seq: int = 0, limit: int = 50) -> Any:
    """Read message history in a multi-Bot group chat room."""
    # Google: NodeAgentServer listRoomPosts parses "room", "after", and "limit" (1..100).
    q = urlencode({"room": room_id, "after": max(0, after_seq), "limit": max(1, min(limit, 100))})
    return server_json("GET", f"/v1/chat/group-messages?{q}")


# Google: nodeagent_group_send posts or replies inside a multi-Bot group chat room using NodeAgentServer's strict roomPost JSON contract.
def nodeagent_group_send(room_id: str, text: str = "", body: str = "", parent_seq: int = 0, mentions: list[str] | None = None, message_id: str = "") -> Any:
    """Post or reply inside a multi-Bot group chat room."""
    content = text if text != "" else body
    if not content.strip():
        raise ValueError("Provide non-empty text for group message.")
    mid = message_id.strip() or f"gmsg-{uuid.uuid4().hex}"
    # Google: NodeAgentServer roomPost uses JSON keys "room", "id", "text", "parent", and "mentions".
    return server_json(
        "POST",
        "/v1/chat/group-messages",
        {
            "room": room_id,
            "id": mid,
            "text": content,
            "parent": parent_seq,
            "mentions": mentions or [],
        },
    )


def nodeagent_shared_knowledge_list() -> Any:
    """List fleet-wide shared Markdown knowledge articles stored on NodeAgentServer."""
    return server_json("GET", "/v1/fleet/shared-knowledge")


def nodeagent_shared_knowledge_get(article_id: str) -> Any:
    """Read the full Markdown body of an authorized fleet-wide shared knowledge article."""
    if not SAFE_ID_RE.fullmatch(article_id):
        raise ValueError("Invalid article_id.")
    return server_json("GET", f"/v1/fleet/shared-knowledge/{article_id}")


def nodeagent_shared_skills_list() -> Any:
    """List fleet-wide shared Skill GitHub repository references registered on NodeAgentServer."""
    return server_json("GET", "/v1/fleet/shared-skills")


def nodeagent_shared_skills_get(skill_id: str) -> Any:
    """Read metadata and GitHub URL reference details for a fleet-wide shared Skill."""
    if not SAFE_ID_RE.fullmatch(skill_id):
        raise ValueError("Invalid skill_id.")
    return server_json("GET", f"/v1/fleet/shared-skills/{skill_id}")


def nodeagent_git_credential(repo: str = "") -> Any:
    """Request the Server-bound GitHub account credential assigned to this Bot for authorized repository pushes."""
    return server_json("POST", "/v1/bots/me/git-credential", {"repo": repo}, allow_git_credential=True)


def nodeagent_local_client_call(
    action: Literal["status", "guest", "register", "chat_bots"],
    agent_name: str = "",
    webhook_url: str = "",
) -> Any:
    """Interact with a co-located local NodeAgentClient daemon (guest onboarding, local registration, or state)."""
    if action == "status":
        return call_local_client("GET", "/api/state")
    if action == "guest":
        return call_local_client("POST", "/api/agent/guest", {"name": agent_name or "Local-Guest-Agent", "webhook_url": webhook_url})
    if action == "register":
        return call_local_client("POST", "/api/agent/register", {"name": agent_name or "Local-Agent", "webhook_url": webhook_url})
    return call_local_client("GET", "/api/chat/bots")


ALL_TOOLS = [
    nodeagent_status,
    nodeagent_register_bot,
    nodeagent_update_profile,
    nodeagent_list_nodes,
    nodeagent_get_node,
    nodeagent_submit_job,
    nodeagent_get_job,
    nodeagent_wait_job,
    nodeagent_desktop_observe,
    nodeagent_desktop_act,
    nodeagent_remote_desktop_frame,
    nodeagent_remote_desktop_input,
    nodeagent_chat_bots,
    nodeagent_chat_presence,
    nodeagent_chat_inbox,
    nodeagent_chat_send,
    nodeagent_group_list,
    nodeagent_group_history,
    nodeagent_group_send,
    nodeagent_shared_knowledge_list,
    nodeagent_shared_knowledge_get,
    nodeagent_shared_skills_list,
    nodeagent_shared_skills_get,
    nodeagent_git_credential,
    nodeagent_local_client_call,
]


def run_server() -> None:
    """Google: Run FastMCP stdio server when mcp is installed, or fallback stdlib JSON-RPC stdio server."""
    try:
        from mcp.server.fastmcp import FastMCP

        mcp = FastMCP("nodeagent")
        for fn in ALL_TOOLS:
            mcp.tool()(fn)
        mcp.run(transport="stdio")
        return
    except ImportError:
        pass

    # Google: Pure-stdlib MCP stdio fallback so environments without pip packages installed can still connect immediately.
    tool_map = {fn.__name__: fn for fn in ALL_TOOLS}
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        req = json.loads(line)
        if "id" not in req:
            continue
        resp: dict[str, Any] = {"jsonrpc": "2.0", "id": req["id"]}
        method = req.get("method")
        try:
            if method == "initialize":
                resp["result"] = {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "nodeagent", "version": "0.2.0"},
                }
            elif method == "ping":
                resp["result"] = {}
            elif method == "tools/list":
                resp["result"] = {
                    "tools": [
                        {
                            "name": fn.__name__,
                            "description": (fn.__doc__ or "").strip(),
                            "inputSchema": {"type": "object", "properties": {}},
                        }
                        for fn in ALL_TOOLS
                    ]
                }
            elif method == "tools/call":
                params = req.get("params", {})
                fn = tool_map.get(params.get("name", ""))
                if not fn:
                    raise KeyError("Unknown tool")
                out = fn(**(params.get("arguments") or {}))
                resp["result"] = {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}], "isError": False}
            else:
                resp["error"] = {"code": -32601, "message": "Method not found"}
        except Exception as exc:
            resp["result"] = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    run_server()
