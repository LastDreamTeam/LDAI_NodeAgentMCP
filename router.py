# SPDX-License-Identifier: AGPL-3.0-only
"""Expose isolated stdio MCP workers for single or multiple NodeAgent Bot profiles."""
import asyncio
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent
PRODUCT = json.loads((ROOT / "connector.json").read_text(encoding="utf-8"))
NAME = PRODUCT["id"]
PROFILE_NAME = re.compile(r"[a-z][a-z0-9_-]{0,15}\Z")
LIMIT = 16 * 1024 * 1024


def load_profiles() -> dict[str, dict]:
    """Google: Load single default profile or validated multi-account definitions from accounts.json."""
    override = os.environ.get("LDAI_ACCOUNTS_FILE")
    path = (
        Path(override).expanduser()
        if override
        else Path.home() / ".config" / "ldai-mcp" / NAME / "accounts.json"
    )
    if not path.is_file() and not override:
        return {"default": {}}
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if data.get("connector") != NAME:
        raise ValueError("Account file is for a different connector.")
    entries = data.get("accounts")
    if not isinstance(entries, dict) or not entries:
        raise ValueError("Define at least one named account.")
    seen = set()
    for alias, entry in entries.items():
        if not PROFILE_NAME.fullmatch(alias) or not isinstance(entry, dict):
            raise ValueError("Invalid account alias or configuration.")
        env = dict(entry.get("env", {}))
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
            raise ValueError("Account environment must contain strings.")
        # Google: Enforce distinct server + profile directory pairs across multi-Bot profiles to prevent token collisions.
        if len(entries) > 1 and "command" not in entry:
            required = PRODUCT["requiredMulti"]
            for key in required:
                if not env.get(key):
                    entry["_invalid"] = f"Configure {key} for this account."
            identity = tuple(env.get(k) for k in required)
            if identity in seen:
                entry["_invalid"] = "This profile duplicates another account."
            if "_invalid" not in entry:
                seen.add(identity)
        entry["env"] = env
    return entries


class Worker:
    """Google: Manage one isolated child stdio MCP process bound to a single Bot profile."""

    def __init__(self, alias: str, config: dict):
        """Google: Initialize worker lock, process handle, and per-worker request sequence counter."""
        self.alias = alias
        self.config = config
        self.lock = asyncio.Lock()
        self.proc: asyncio.subprocess.Process | None = None
        self.seq = 0

    async def close(self) -> None:
        """Google: Terminate the child MCP subprocess cleanly if still running."""
        proc, self.proc = self.proc, None
        if proc and proc.returncode is None:
            if proc.stdin:
                with contextlib.suppress(Exception):
                    proc.stdin.close()
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()

    async def packet(self, method: str, params: dict) -> dict:
        """Google: Send one newline-delimited JSON-RPC request and await its matching response ID."""
        assert self.proc and self.proc.stdin and self.proc.stdout
        self.seq += 1
        ident = self.seq
        msg = {"jsonrpc": "2.0", "id": ident, "method": method, "params": params}
        self.proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
        await self.proc.stdin.drain()
        while raw := await self.proc.stdout.readline():
            event = json.loads(raw)
            if event.get("id") == ident and "method" not in event:
                if "error" in event:
                    raise ValueError(event["error"].get("message") or "Worker rejected the MCP request.")
                return event["result"]
        raise ValueError("Worker closed the connection.")

    async def request(self, method: str, params: dict) -> dict:
        """Google: Lazily spawn and initialize the child MCP worker, then serialize requests under lock."""
        async with self.lock:
            try:
                async with asyncio.timeout(300 if method == "tools/call" else 40):
                    if self.proc is None or self.proc.returncode is not None:
                        env = dict(os.environ)
                        # Google: Strip ambient provider vars when routing multiple accounts so profiles never bleed settings.
                        if len(WORKERS) > 1:
                            for k in PRODUCT["providerEnvironment"]:
                                env.pop(k, None)
                        env.update(self.config.get("env", {}))
                        env["LDAI_ACCOUNT_ALIAS"] = self.alias
                        env["LDAI_PLUGIN_ROOT"] = str(ROOT)
                        # Google: Invoke server.py directly with the selected Python interpreter so Windows does not orphan grandchild pipes.
                        override_py = env.get("LDAI_MCP_PYTHON", "").strip()
                        venv_py = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
                        py_bin = override_py or (str(venv_py) if venv_py.is_file() else sys.executable)
                        argv = (
                            [self.config["command"], *self.config.get("args", [])]
                            if "command" in self.config
                            else [py_bin, "-B", str(ROOT / "server.py")]
                        )
                        self.proc = await asyncio.create_subprocess_exec(
                            *argv,
                            cwd=ROOT,
                            env=env,
                            stdin=asyncio.subprocess.PIPE,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.DEVNULL,
                            limit=LIMIT,
                        )
                        await self.packet(
                            "initialize",
                            {
                                "protocolVersion": "2025-06-18",
                                "capabilities": {},
                                "clientInfo": {"name": "nodeagent-router", "version": PRODUCT["version"]},
                            },
                        )
                        self.proc.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
                        await self.proc.stdin.drain()
                    return await self.packet(method, params)
            except BaseException:
                await self.close()
                raise


WORKERS: dict[str, Worker] = {}
ROUTES: dict[str, tuple[Worker, str]] = {}
ERRORS: dict[str, str] = {}
DISCOVERY_LOCK = asyncio.Lock()


def exposed_name(alias: str, tool: str) -> str:
    """Google: Prefix tool names with account alias when multiple profiles are configured."""
    name = f"{alias}__{tool}" if len(WORKERS) > 1 else tool
    if len(name) > 64:
        name = name[:51] + "_" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]
    return name


def make_payload(data, failed: bool = False) -> dict:
    """Google: Wrap structured tool output in standard MCP CallToolResult content blocks."""
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return {"content": [{"type": "text", "text": text}], "isError": failed}


async def sync_profiles() -> None:
    """Google: Reconcile active Worker instances with the latest accounts configuration on disk."""
    async with DISCOVERY_LOCK:
        config = load_profiles()
        if list(config) == list(WORKERS) and all(WORKERS[a].config == v for a, v in config.items()):
            return
        previous = dict(WORKERS)
        WORKERS.clear()
        for alias, settings in config.items():
            existing = previous.pop(alias, None)
            if existing and existing.config == settings:
                WORKERS[alias] = existing
            else:
                if existing:
                    previous[f"_old_{alias}"] = existing
                WORKERS[alias] = Worker(alias, settings)
        ROUTES.clear()
        ERRORS.clear()
        for w in previous.values():
            async with w.lock:
                await w.close()


async def discover_tools() -> list[dict]:
    """Google: Aggregate tool schemas across all valid profile workers plus ldai_accounts."""
    async with DISCOVERY_LOCK:
        async def tools_for(alias: str, worker: Worker) -> list[tuple[str, tuple[Worker, str], dict]]:
            if worker.config.get("_invalid"):
                ERRORS[alias] = worker.config["_invalid"]
                return []
            found = []
            try:
                result = await worker.request("tools/list", {})
                for tool in result.get("tools", []):
                    raw_name = tool["name"]
                    if not raw_name.startswith(tuple(PRODUCT["toolPrefixes"])):
                        continue
                    pub = exposed_name(alias, raw_name)
                    prefix = f"[{alias}] " if len(WORKERS) > 1 else ""
                    schema = {**tool, "name": pub, "description": prefix + tool.get("description", "")}
                    found.append((pub, (worker, raw_name), schema))
            except Exception as exc:
                ERRORS[alias] = f"Worker unavailable: {exc}"
                return []
            ERRORS.pop(alias, None)
            return found

        groups = await asyncio.gather(*(tools_for(k, w) for k, w in WORKERS.items()))
        ROUTES.clear()
        schemas = []
        for group in groups:
            for pub, route, schema in group:
                ROUTES[pub] = route
                schemas.append(schema)
        return [
            {
                "name": "ldai_accounts",
                "description": "List configured NodeAgent Bot profiles and connection discovery status.",
                "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            *schemas,
        ]


async def handle_rpc(req: dict) -> dict:
    """Google: Dispatch MCP initialize, ping, tools/list, and tools/call requests."""
    method = req.get("method")
    if method == "initialize":
        supported = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
        version = req.get("params", {}).get("protocolVersion")
        return {
            "protocolVersion": version if version in supported else "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": NAME, "version": PRODUCT["version"]},
        }
    if method == "ping":
        return {}
    if method in ("tools/list", "tools/call"):
        await sync_profiles()
    if method == "tools/list":
        return {"tools": await discover_tools()}
    if method == "tools/call":
        params = req.get("params", {})
        name = params.get("name", "")
        if name == "ldai_accounts":
            return make_payload(
                {
                    "accounts": list(WORKERS),
                    "labels": {a: w.config.get("label", a) for a, w in WORKERS.items()},
                    "discoveryErrors": dict(ERRORS),
                }
            )
        if not ROUTES:
            await discover_tools()
        route = ROUTES.get(name)
        if not route:
            return make_payload("Unknown tool or profile. Refresh the MCP tool list.", True)
        worker, raw_name = route
        try:
            return await worker.request("tools/call", {**params, "name": raw_name})
        except Exception as exc:
            # Google: Never auto-retry side-effecting NodeAgent calls; guide caller to inspect original job ID.
            return make_payload(
                f"Worker call failed ({exc}). Inspect original job_id before retrying side-effecting tasks.",
                True,
            )
    raise LookupError(method or "")


async def main() -> None:
    """Google: Run newline-delimited JSON-RPC 2.0 event loop over stdio."""
    pending: dict = {}

    async def respond(req: dict) -> None:
        resp: dict = {"jsonrpc": "2.0", "id": req["id"]}
        try:
            resp["result"] = await handle_rpc(req)
        except LookupError:
            resp["error"] = {"code": -32601, "message": "Method not found"}
        except Exception as exc:
            resp["error"] = {"code": -32603, "message": f"Connector request failed: {exc}"}
        sys.stdout.buffer.write((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
        sys.stdout.buffer.flush()

    try:
        while line := await asyncio.to_thread(sys.stdin.buffer.readline, LIMIT):
            try:
                req = json.loads(line)
            except ValueError:
                continue
            if not isinstance(req, dict):
                continue
            if req.get("method") == "notifications/cancelled":
                task = pending.get(req.get("params", {}).get("requestId"))
                if task:
                    task.cancel()
            elif "id" in req:
                task = asyncio.create_task(respond(req))
                pending[req["id"]] = task
                task.add_done_callback(lambda _done, ident=req["id"]: pending.pop(ident, None))
        if pending:
            await asyncio.gather(*pending.values(), return_exceptions=True)
    finally:
        await asyncio.gather(*(w.close() for w in WORKERS.values()), return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
