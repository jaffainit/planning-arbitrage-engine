"""Local `agy` CLI subprocess wrapper for zero-cost structured JSON LLM inference."""

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


def resolve_agy_binary() -> Optional[str]:
    """Locate the `agy` CLI binary on the host."""
    found = shutil.which("agy")
    if found:
        return found
    candidate_paths = [
        Path.home() / ".local" / "bin" / "agy",
        Path("/home/jaffy/.local/bin/agy"),
        Path("/usr/local/bin/agy"),
        Path("/usr/bin/agy"),
    ]
    for candidate in candidate_paths:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def call_agy_json(
    prompt: str,
    json_schema: dict[str, Any],
    timeout_seconds: int = 90,
) -> dict[str, Any]:
    """Execute `agy -p "<prompt>" --output-format json --json-schema '<schema>'` as a subprocess and return structured JSON."""
    if os.environ.get("PYTEST_CURRENT_TEST") and os.environ.get("ENABLE_LIVE_AGY_IN_TESTS") != "1":
        raise RuntimeError("Skipping live agy subprocess inside unit test run.")

    agy_bin = resolve_agy_binary()
    if not agy_bin:
        raise FileNotFoundError("Local `agy` binary not found on PATH or ~/.local/bin/agy")

    schema_str = json.dumps(json_schema, separators=(",", ":"))
    cmd = [
        agy_bin,
        "--dangerously-skip-permissions",
        "--disable-slash-commands",
        "--effort",
        "low",
        "-p",
        prompt,
        "--output-format",
        "json",
        "--json-schema",
        schema_str,
    ]

    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"agy CLI exited with code {proc.returncode}: {proc.stderr.strip() or proc.stdout.strip()}"
        )

    stdout = proc.stdout.strip()
    if not stdout:
        raise RuntimeError("agy CLI returned empty stdout")

    # Find the JSON object line in stdout
    envelope: Optional[dict[str, Any]] = None
    for line in reversed(stdout.splitlines()):
        line_str = line.strip()
        if line_str.startswith("{") and line_str.endswith("}"):
            try:
                parsed_line = json.loads(line_str)
                if isinstance(parsed_line, dict):
                    envelope = parsed_line
                    break
            except json.JSONDecodeError:
                continue

    if envelope is None:
        envelope = json.loads(stdout)

    if isinstance(envelope, dict):
        if isinstance(envelope.get("structured_output"), dict):
            return envelope["structured_output"]
        if isinstance(envelope.get("response"), str):
            for resp_line in envelope["response"].splitlines():
                resp_line = resp_line.strip()
                if resp_line.startswith("{") and resp_line.endswith("}"):
                    try:
                        parsed_resp = json.loads(resp_line)
                        if isinstance(parsed_resp, dict) and "toolAction" not in parsed_resp:
                            return parsed_resp
                    except json.JSONDecodeError:
                        continue
        return envelope

    raise ValueError(f"Unable to parse structured_output from agy stdout: {stdout[:300]}")
