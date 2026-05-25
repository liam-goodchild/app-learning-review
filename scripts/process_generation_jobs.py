#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA = ROOT / "scripts" / "generated_questions.schema.json"


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def api_request(base_url: str, token: str, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {path} failed: HTTP {exc.code}: {detail}") from exc
    return json.loads(body) if body else {}


def run_codex(prompt: str, *, codex_bin: str, schema: Path, timeout_seconds: int, cwd: Path) -> str:
    with tempfile.NamedTemporaryFile("w+", suffix=".json", delete=False) as output_file:
        output_path = Path(output_file.name)
    try:
        command = [
            codex_bin,
            "exec",
            "--ephemeral",
            "--cd",
            str(cwd),
            "-c",
            'approval_policy="never"',
            "--sandbox",
            "read-only",
            "--output-schema",
            str(schema),
            "--output-last-message",
            str(output_path),
            "-",
        ]
        completed = subprocess.run(
            command,
            input=prompt,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
        last_message = output_path.read_text(encoding="utf-8") if output_path.exists() else ""
        if completed.returncode != 0:
            raise RuntimeError(
                "codex exec failed with exit code {}\nSTDOUT:\n{}\nSTDERR:\n{}\nLAST MESSAGE:\n{}".format(
                    completed.returncode, completed.stdout, completed.stderr, last_message
                )
            )
        return last_message.strip() or completed.stdout.strip()
    finally:
        output_path.unlink(missing_ok=True)


def process_jobs(args: argparse.Namespace) -> int:
    load_dotenv(ROOT / ".env")
    base_url = args.base_url or os.getenv("APP_BASE_URL") or "http://127.0.0.1:8081"
    token = args.token or os.getenv("WORKER_TOKEN")
    if not token:
        raise RuntimeError("WORKER_TOKEN is required")
    processed = 0
    fake_output = Path(args.fake_output).read_text(encoding="utf-8") if args.fake_output else None
    for _ in range(args.limit):
        claimed = api_request(base_url, token, "POST", "/worker/generation-jobs/claim")
        job = claimed.get("job")
        if not job:
            print("No pending generation jobs")
            break
        job_id = int(job["id"])
        print(f"Processing generation job {job_id}: {job['source_title']}")
        try:
            raw_output = fake_output if fake_output is not None else run_codex(
                job["prompt"],
                codex_bin=args.codex_bin,
                schema=Path(args.schema),
                timeout_seconds=args.timeout_seconds,
                cwd=ROOT,
            )
            completed = api_request(base_url, token, "POST", f"/worker/generation-jobs/{job_id}/complete", {"raw_output": raw_output})
            print(f"Completed job {job_id}; created {completed.get('draft_questions_created', 0)} draft questions")
            processed += 1
        except Exception as exc:
            error_text = str(exc)
            print(f"Failed job {job_id}: {error_text}", file=sys.stderr)
            api_request(base_url, token, "POST", f"/worker/generation-jobs/{job_id}/fail", {"error": error_text})
            if args.stop_on_failure:
                return 1
    return 0 if processed or not args.fail_if_empty else 2


def main() -> int:
    parser = argparse.ArgumentParser(description="Process queued AI generation jobs with Codex CLI.")
    parser.add_argument("--base-url", default=None, help="App base URL, default APP_BASE_URL or http://127.0.0.1:8081")
    parser.add_argument("--token", default=None, help="Worker token, default WORKER_TOKEN")
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--schema", default=str(DEFAULT_SCHEMA))
    parser.add_argument("--timeout-seconds", type=int, default=420)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--fake-output", default=None, help="Use a JSON file instead of calling Codex; useful for tests")
    parser.add_argument("--stop-on-failure", action="store_true")
    parser.add_argument("--fail-if-empty", action="store_true")
    parser.add_argument("--watch", action="store_true", help="Poll continuously for app-triggered jobs")
    parser.add_argument("--poll-seconds", type=int, default=30)
    args = parser.parse_args()
    if args.watch:
        while True:
            process_jobs(args)
            time.sleep(max(5, args.poll_seconds))
    return process_jobs(args)


if __name__ == "__main__":
    raise SystemExit(main())
