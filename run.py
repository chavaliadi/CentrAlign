#!/usr/bin/env python3
"""CLI entry point.

    python run.py --task invoice_entry                 # one task, approvals prompted
    python run.py --task invoice_entry --auto-approve   # unattended, approvals logged
    python run.py --all --auto-approve                  # the generalisation run
    python run.py --all --chaos off                     # clean baseline, no failure injection

``--start-env`` boots AcmeOps itself, so a reviewer needs one command and not
two terminals.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

# Localhost must not go through any configured HTTP proxy.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_json(url: str, *, method: str = "GET", timeout: float = 10.0) -> dict[str, Any]:
    request = urllib.request.Request(url, method=method)
    with _OPENER.open(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    return json.loads(body) if body.strip() else {}


def wait_for_env(base_url: str, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            http_json(f"{base_url}/healthz", timeout=2.0)
            return True
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            time.sleep(0.4)
    return False


def start_env(base_url: str, chaos: str, seed: int) -> subprocess.Popen[bytes]:
    port = base_url.rsplit(":", 1)[-1]
    env = dict(os.environ, ACMEOPS_CHAOS=chaos, ACMEOPS_CHAOS_SEED=str(seed))
    log = (REPO_ROOT / "runs" / "acmeops.log").open("ab")
    (REPO_ROOT / "runs").mkdir(exist_ok=True)
    process = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "acmeops.app:app",
            "--host", "127.0.0.1", "--port", port, "--log-level", "warning",
        ],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=log,
        stderr=log,
    )
    return process


def load_task(name: str) -> dict[str, Any]:
    path = Path(name)
    if not path.exists():
        path = REPO_ROOT / "tasks" / (name if name.endswith(".json") else f"{name}.json")
    if not path.exists():
        available = sorted(p.stem for p in (REPO_ROOT / "tasks").glob("*.json"))
        raise SystemExit(f"no such task {name!r}. Available: {available}")
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Autonomous AI task worker")
    parser.add_argument("--task", help="task id or path to a task JSON file")
    parser.add_argument("--all", action="store_true", help="run every task in tasks/")
    parser.add_argument("--auto-approve", action="store_true",
                        help="grant write approvals automatically (still recorded in the trace)")
    parser.add_argument("--chaos", choices=["on", "off"], default="on",
                        help="failure injection in the environment (default on)")
    parser.add_argument("--chaos-seed", type=int, default=7)
    parser.add_argument("--max-steps", type=int, default=None)
    parser.add_argument("--start-env", action="store_true", help="boot AcmeOps as a subprocess")
    parser.add_argument("--no-reset", action="store_true",
                        help="keep existing environment state instead of reseeding")
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    parser.add_argument("--provider", choices=["anthropic", "groq", "openai", "local"],
                        help="model provider (default: inferred from whichever API key is set)")
    parser.add_argument("--model", help="model id (default: provider preset, or best available)")
    parser.add_argument("--list-models", action="store_true",
                        help="list the models the configured provider offers, then exit")
    args = parser.parse_args()

    if args.provider:
        os.environ["AGENT_PROVIDER"] = args.provider
    if args.model:
        os.environ["AGENT_MODEL"] = args.model

    if not args.list_models and not args.task and not args.all:
        parser.error("pass --task <id>, --all, or --list-models")

    # Settings must be adjusted before the agent modules read them.
    os.environ.setdefault("ACMEOPS_CHAOS", args.chaos)
    os.environ.setdefault("ACMEOPS_CHAOS_SEED", str(args.chaos_seed))
    if args.headed:
        os.environ["AGENT_HEADLESS"] = "0"

    # Imported in stages on purpose. `--list-models` and the API-key check are
    # diagnostics a person reaches for when their install is still broken, so
    # they must not drag in Playwright or the browser tools. The agent itself is
    # imported only once we know we are actually going to run a task.
    from agent.config import SETTINGS
    from agent.llm import ModelError, available_models, resolve_model

    if args.model:
        SETTINGS.model = args.model
    if args.provider:
        SETTINGS.provider = args.provider

    key_for = {
        "anthropic": "ANTHROPIC_API_KEY",
        "groq": "GROQ_API_KEY",
        "openai": "OPENAI_API_KEY",
        "local": "AGENT_API_KEY",
    }[SETTINGS.provider]
    if not os.environ.get(key_for):
        print(
            f"{key_for} is not set, but provider is {SETTINGS.provider!r}.\n"
            "Export the key, or choose another provider with --provider.",
            file=sys.stderr,
        )
        return 2

    if args.list_models:
        try:
            models = available_models(strict=True)
        except ModelError as exc:
            print(str(exc), file=sys.stderr)
            print(
                "\nCommon causes: the API key is wrong or expired, or this machine "
                "cannot reach the provider.",
                file=sys.stderr,
            )
            return 2
        if not models:
            print(
                f"{SETTINGS.provider} does not expose a model list on this path. "
                "Set AGENT_MODEL explicitly."
            )
            return 0
        print(f"models offered by {SETTINGS.provider}:")
        for model_id in models:
            print(f"  {model_id}")
        return 0

    try:
        resolve_model()
    except ModelError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        from agent.loop import TaskWorker
        from agent.tools.human import ScriptedResponder, console_responder
        from agent.toolsets import browser_toolset
    except ModuleNotFoundError as exc:
        missing = getattr(exc, "name", "") or str(exc)
        hint = (
            "\n  pip install -r requirements.txt"
            "\n  playwright install chromium"
        )
        if missing == "playwright":
            hint += (
                "\n\nPlaywright needs both steps: pip installs the library, "
                "`playwright install` downloads the browser itself."
            )
        print(f"missing dependency: {missing}{hint}", file=sys.stderr)
        return 2

    if args.max_steps:
        SETTINGS.max_steps = args.max_steps
    SETTINGS.auto_approve = args.auto_approve
    base_url = SETTINGS.base_url
    print(f"provider: {SETTINGS.provider}   model: {SETTINGS.model}")

    env_process: subprocess.Popen[bytes] | None = None
    if args.start_env:
        subprocess.run([sys.executable, "-m", "acmeops.make_pdfs"], cwd=str(REPO_ROOT), check=False,
                       env=dict(os.environ, ACMEOPS_CHAOS="off"))
        env_process = start_env(base_url, args.chaos, args.chaos_seed)

    try:
        if not wait_for_env(base_url):
            print(
                f"AcmeOps is not responding at {base_url}.\n"
                "Start it with:  python -m uvicorn acmeops.app:app --port 8099\n"
                "or re-run this command with --start-env",
                file=sys.stderr,
            )
            return 2

        def read_state() -> dict[str, Any]:
            """Ground truth for the verifier, read straight from the data store
            rather than from the UI the agent was driving."""
            return {
                "payables_system": http_json(f"{base_url}/api/payables"),
                "vendor_invoices": http_json(f"{base_url}/api/invoices"),
            }

        task_names = (
            sorted(p.stem for p in (REPO_ROOT / "tasks").glob("*.json"))
            if args.all
            else [args.task]
        )

        results = []
        for name in task_names:
            spec = load_task(name)
            if not args.no_reset:
                # Each task resets to the world its own contract is written
                # against; see /admin/reset for why that matters.
                profile = spec.get("env_profile", "clean")
                http_json(f"{base_url}/admin/reset?profile={profile}", method="POST")

            print("\n" + "=" * 78)
            print(f"TASK {spec['id']}: {spec.get('title', '')}")
            print(f"request: {spec['task']}")
            print(f"chaos: {args.chaos} (seed {args.chaos_seed})   approvals: "
                  f"{'auto' if args.auto_approve else 'interactive'}")
            print("=" * 78)

            responder = ScriptedResponder(
                answers=spec.get("human_answers") or {},
                default=spec.get("human_default", ""),
            ) if args.auto_approve else None

            # The toolset is chosen here, not inside the controller: loop.py
            # has no knowledge of browsers or files at all.
            worker = TaskWorker(
                toolset_factory=browser_toolset,
                task_id=spec["id"],
                task=spec["task"],
                environment_notes=spec.get("environment_notes", "").replace(
                    "the base URL", base_url
                ),
                read_state=read_state,
                responder=responder or console_responder,
            )
            result = worker.run()
            results.append(result)

            print(f"\nstatus: {result.status}   verification: "
                  f"{result.verification.get('verdict', 'n/a')}")
            # An error must be visible where the person is looking - the console -
            # not only buried in a file they have to go and open.
            if result.error:
                print(f"error: {result.error}", file=sys.stderr)
            if result.declared.get("answer"):
                print(f"answer: {result.declared['answer']}")
            print(f"evidence: {result.run_dir}/report.md")

        if len(results) > 1:
            print("\n" + "=" * 78)
            print("SUMMARY - same agent, same code, different tasks")
            print("=" * 78)
            for r in results:
                print(f"  {r.task_id:22s} {r.status:20s} "
                      f"verify={r.verification.get('verdict', 'n/a'):12s} "
                      f"steps={r.steps_used:2d} replans={r.replans}")

        return 0 if all(r.ok for r in results) else 1

    finally:
        if env_process is not None:
            env_process.send_signal(signal.SIGTERM)
            try:
                env_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                env_process.kill()


if __name__ == "__main__":
    raise SystemExit(main())
