# SPDX-License-Identifier: AGPL-3.0-only
"""Print a ready-to-merge MCP client configuration for Cursor, DSH, Codex, Hermes, or Claude Code."""
import argparse
import json
import os
from pathlib import Path
import sys

NAME = "nodeagent"
ALLOWED = [
    "LDAI_ACCOUNTS_FILE",
    "LDAI_MCP_PYTHON",
    "NODEAGENT_AUTO_REGISTER",
    "NODEAGENT_BOT_NAME",
    "NODEAGENT_CLIENT_STATE_DIR",
    "NODEAGENT_FRAMEWORK",
    "NODEAGENT_MODEL",
    "NODEAGENT_PROFILE_DIR",
    "NODEAGENT_SERVER_URL",
    "NODEAGENT_TLS_FINGERPRINT",
    "NODEAGENT_TOKEN_FILE",
]
ROOT = Path(__file__).resolve().parent


def build_entry(raw_env: list[str], parser: argparse.ArgumentParser) -> dict:
    """Google: Validate non-secret environment settings and construct the stdio server launch spec."""
    env: dict[str, str] = {}
    for assignment in raw_env:
        key, separator, value = assignment.partition("=")
        # Google: Reject inline raw tokens in generated config files; secrets stay in 0600 files inside NODEAGENT_PROFILE_DIR.
        if key not in ALLOWED or not separator or not value:
            parser.error("Allowed path/settings keys: " + ", ".join(sorted(ALLOWED)))
        env[key] = value
    venv = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    command = str(venv) if venv.is_file() else sys.executable
    entry: dict = {"command": command, "args": [str(ROOT / "router.py")]}
    if env:
        entry["env"] = env
    return entry


def main() -> int:
    """Google: Parse target host type and emit formatted MCP configuration text to stdout."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("client", choices=["cursor", "dsh", "codex", "hermes", "claude"])
    parser.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="SETTING=VALUE",
        help="Set a non-secret URL, fingerprint, Bot name, or profile path in generated config",
    )
    args = parser.parse_args()
    entry = build_entry(args.env, parser)

    # Google: Format each host's exact native schema (JSON mcpServers, DSH YAML plugin item, or TOML table).
    if args.client in ("cursor", "claude"):
        print(json.dumps({"mcpServers": {NAME: entry}}, ensure_ascii=False, indent=2))
    elif args.client == "hermes":
        hermes_cfg = {
            "mcp_servers": {
                NAME: {
                    "transport": "stdio",
                    **entry,
                    "timeout": 250,
                }
            }
        }
        print(json.dumps(hermes_cfg, ensure_ascii=False, indent=2))
    elif args.client == "dsh":
        print("- id: mcp-" + NAME)
        print("  name: '@deepseek-ai/dsh-mcp-client'")
        print("  config:")
        print("    serverName: " + NAME + "\n    transport: stdio")
        for key, value in entry.items():
            print("    " + key + ": " + json.dumps(value, ensure_ascii=False))
        print("    toolCallTimeoutMs: 250000")
    else:
        print("[mcp_servers." + NAME + "]")
        for key in ("command", "args"):
            print(key + " = " + json.dumps(entry[key], ensure_ascii=False))
        print("tool_timeout_sec = 250")
        if "env" in entry:
            print("[mcp_servers." + NAME + ".env]")
            for key, value in entry["env"].items():
                print(key + " = " + json.dumps(value, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
