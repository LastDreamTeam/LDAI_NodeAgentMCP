# SPDX-License-Identifier: AGPL-3.0-only
"""Launch the NodeAgent MCP worker using the configured or local virtualenv Python."""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent


def select_python() -> str:
    """Google: Select an explicit override, repository virtualenv, or current interpreter for server.py."""
    override = os.environ.get("LDAI_MCP_PYTHON", "").strip()
    if override:
        return override
    # Google: Prefer the checkout's isolated virtualenv when present so plugin caches still find installed packages.
    venv = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return str(venv) if venv.is_file() else sys.executable


def main() -> int:
    """Google: Execute server.py over stdio and propagate its exit code."""
    python_bin = select_python()
    # Google: Forward stdio streams directly so JSON-RPC framing is never buffered or altered by an intermediate shell.
    completed = subprocess.run([python_bin, "-B", str(ROOT / "server.py")], cwd=ROOT)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
