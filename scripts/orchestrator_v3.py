#!/usr/bin/env python3
"""
orchestrator_v3.py

Architecture:
  - Claude Sonnet (Anthropic API): plans, decomposes, routes, integrates
  - Gemma via Ollama (Windows LAN): executes each agent task via direct SDK call
  - Parallel execution via threading for independent tasks

Usage:
  export ANTHROPIC_API_KEY=sk-...
  python3 scripts/orchestrator_v3.py --file tasks/task.txt --verbose
"""

import os
import sys
import json
import threading
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import anthropic

# ── Guardrail loader ──────────────────────────────────────────────────────────

def _load_guardrails(project_root: Path) -> dict:
    """Load guardrail files from .claude/guardrails/ directory."""
    guardrails_dir = project_root / ".claude/guardrails"
    result = {}
    if not guardrails_dir.exists():
        return result
    for f in guardrails_dir.glob("*.md"):
        result[f.stem] = f.read_text()
    return result


# ── Config ────────────────────────────────────────────────────────────────────
VERBOSE = False  # set to True via --verbose flag

PROJECT_ROOT       = Path("/Users/eugenesidorov/Projects/ai-agronomist")
ORCHESTRATOR_MODEL = "claude-sonnet-4-5"           # Anthropic — plans & routes
AGENT_MODEL        = "gemma4-claude"               # Ollama on Windows — executes
OLLAMA_BASE_URL    = "http://192.168.68.118:11434"
MAX_PARALLEL       = 2  # cap at 2 — Gemma needs ~9GB RAM per instance

# Load guardrails from .claude/guardrails/
GUARDRAILS = _load_guardrails(PROJECT_ROOT)
_UNIVERSAL = GUARDRAILS.get("universal-guardrails", "")
_ML        = GUARDRAILS.get("ml-guardrails", "")
_RED_FLAGS = GUARDRAILS.get("red-flags", "")


def _load_agent_prompt(path: Path) -> str:
    """Load agent .md file, stripping YAML frontmatter."""
    text = path.read_text()
    if text.startswith("---"):
        parts = text.split("---", 2)
        return parts[2].strip() if len(parts) >= 3 else text
    return text


AGENT_SYSTEM_PROMPTS = {
    name: _load_agent_prompt(PROJECT_ROOT / f".claude/agents/{name}.md")
    for name in ["architect", "ml-engineer", "backend-engineer", "qa-engineer",
                 "code-reviewer", "devops-lead", "tech-lead"]
    if (PROJECT_ROOT / f".claude/agents/{name}.md").exists()
}

# Domain context injected into task prompt (not system prompt)
# Keeps system prompts under 500 chars to avoid gemma4:26b empty-response bug
AGENT_TASK_CONTEXT = {
    "architect": (
        "Project: AI Agronomist greenhouse monitoring platform.\n"
        "Sensors: temperature (20-30C), humidity (50-80%), CO2 (300-500ppm), light (0-1000 lux).\n"
        "LSTM-VAE input: [batch, seq_len=20, input_dim=4]. Stack: Python, FastAPI, PyTorch, SQLite.\n"
        "Output must include: data model, key decisions, TODOs for implementing agents.\n\n"
        f"GUARDRAILS YOU MUST FOLLOW:\n{_UNIVERSAL}"
    ),
    "ml-engineer": (
        "Project: AI Agronomist. LSTM-VAE spec (locked):\n"
        "  LSTMVAE(input_dim=4, hidden_dims=[64,32], latent_dim=8, seq_len=20, beta=0.1, dropout=0.2)\n"
        "  Loss: MSE(x, x_recon) + beta * KL. Latent: z = mu + sigma * eps.\n"
        "SyntheticDataGenerator: np.ndarray (1000,20,4) normalized [0,1], random_seed=42.\n"
        "Read src/config.py. No magic numbers. After writing: run smoke tests, fix until green.\n\n"
        f"GUARDRAILS YOU MUST FOLLOW:\n{_UNIVERSAL}\n\nML-SPECIFIC GUARDRAILS:\n{_ML}"
    ),
    "backend-engineer": (
        "Project: AI Agronomist. Greenhouse sensor API: temperature, humidity, CO2, light.\n"
        "Time-series sequences of 20 steps. CSV upload + readings ingestion to SQLite.\n"
        "Use Pydantic v2, SQLAlchemy 2.x async, FastAPI DI. Read src/config.py.\n"
        "After writing: run smoke tests, fix until green, write docs/handoff/*.md.\n\n"
        f"GUARDRAILS YOU MUST FOLLOW:\n{_UNIVERSAL}"
    ),
    "qa-engineer": (
        "Project: AI Agronomist. Coverage target: 90%.\n"
        "Run: python -m pytest tests/ --cov=src --cov-report=term-missing --cov-fail-under=90\n"
        "Iterate until threshold met (max 4 attempts). Write summary to docs/coverage/*.md.\n"
        "Mirror source: src/models/lstm_vae.py -> tests/test_lstm_vae.py. Report bugs, don't fix."
    ),
    "code-reviewer": (
        "Project: AI Agronomist. Review checklist:\n"
        "Security: SQL injection, input validation, no secrets in code.\n"
        "Correctness: logic errors, tensor shapes, wrong dtype.\n"
        "Performance: N+1 queries, missing torch.no_grad().\n"
        "Output format:\n"
        "## Guardrail Check\n"
        "### STOP WORK violations (list each or NONE)\n"
        "### WARNING violations (list each or NONE)\n"
        "## Summary: PASS / PASS WITH NOTES / NEEDS FIXES\n"
        "## Critical Issues / Minor Issues / Observations\n\n"
        f"GUARDRAILS TO ENFORCE:\n{_UNIVERSAL}\n\nRED FLAGS:\n{_RED_FLAGS}\n\nML GUARDRAILS:\n{_ML}"
    ),
    "devops-lead": (
        "Project: AI Agronomist. Targets: Edge=Jetson Orin Nano ARM64 JetPack, Dev=Apple Silicon, Cloud=python:3.11-slim.\n"
        "Multi-stage builds, non-root user, health checks, env vars via .env never hardcoded.\n"
        "GitHub Actions: lint -> test -> build -> push on PR merge."
    ),
    "tech-lead": (
        "Project: AI Agronomist. Only touch code with passing tests.\n"
        "Improve: extract repeated logic, better naming, optimize hot paths.\n"
        "Do NOT change public APIs or add features. Write docs/refactor/<file>-refactor.md."
    ),
}

ORCHESTRATOR_SYSTEM = """
You are the orchestrator for the AI Agronomist project.
Your job is ONLY to plan and decompose tasks — never to write implementation code yourself.

When given a task, respond with a JSON execution plan in this exact format:

{
  "phases": [
    {
      "phase": 1,
      "parallel": true,
      "agents": [
        {
          "agent": "<agent-name>",
          "task": "<specific focused task for this agent>",
          "output_file": "<path/to/output/file>",
          "context_from": []
        }
      ]
    }
  ]
}

Rules:
- Use only these agent names: architect, ml-engineer, backend-engineer, qa-engineer, code-reviewer, devops-lead, tech-lead
- Always include code-reviewer as the FINAL phase — it enforces guardrails
- If a task involves model checkpoints, flag ml-engineer to include metadata (epoch, loss, date)
- If a task involves database schema, flag backend-engineer to ensure multi-tenant scalability
- Never plan tasks that would create tight coupling between ML and API layers
- Keep each agent task FOCUSED on ONE file or ONE concern
- context_from lists which phase agents' outputs to pass as context
- Always end with code-reviewer as the final phase
- Respond with ONLY the JSON, no explanation

Agent roles:
- architect: system design, architecture docs → docs/architecture/
- ml-engineer: PyTorch models, training → src/models/
- backend-engineer: FastAPI, SQLAlchemy → src/api/, src/db/
- qa-engineer: pytest tests, 90% coverage → tests/
- code-reviewer: security + quality review → docs/review/
- devops-lead: Docker, CI/CD → docker/, .github/
- tech-lead: refactoring → docs/refactor/
"""


# ── Orchestrator: plan the task ───────────────────────────────────────────────

def plan_task(user_task: str) -> dict:
    """Ask Claude Sonnet to decompose the task into a parallel execution plan."""
    client = anthropic.Anthropic(
        api_key=os.environ["ANTHROPIC_API_KEY"],
        base_url="https://api.anthropic.com",
    )

    print(f"\n🧠 Orchestrator planning task...")
    print(f"   model : {ORCHESTRATOR_MODEL}")
    print(f"   url   : https://api.anthropic.com\n")

    response = client.messages.create(
        model=ORCHESTRATOR_MODEL,
        max_tokens=4000,
        system=ORCHESTRATOR_SYSTEM,
        messages=[{"role": "user", "content": user_task}]
    )

    print(f"   ✓ response model : {response.model}")
    print(f"   ✓ input tokens   : {response.usage.input_tokens}")
    print(f"   ✓ output tokens  : {response.usage.output_tokens}\n")

    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]

    try:
        plan = json.loads(raw.strip())
        # Normalize context_from: Sonnet sometimes returns phase numbers instead of agent names
        # Build a map of phase_number -> list of agent names in that phase
        phase_agent_map = {}
        for phase in plan.get("phases", []):
            pnum = str(phase["phase"])
            phase_agent_map[pnum] = [a["agent"] for a in phase.get("agents", [])]

        # Replace numeric references with actual agent names
        for phase in plan.get("phases", []):
            for agent in phase.get("agents", []):
                cf = agent.get("context_from", [])
                resolved = []
                for ref in cf:
                    ref_str = str(ref)
                    if ref_str in phase_agent_map:
                        # It's a phase number — expand to all agents in that phase
                        resolved.extend(phase_agent_map[ref_str])
                    else:
                        resolved.append(ref_str)
                agent["context_from"] = list(dict.fromkeys(resolved))  # deduplicate, preserve order
        return plan
    except json.JSONDecodeError as e:
        print(f"❌ Failed to parse orchestrator plan: {e}")
        print(f"Raw response:\n{raw}")
        sys.exit(1)


# ── File writing helper ───────────────────────────────────────────────────────

def _write_file_from_response(response_text: str, output_path: Path) -> bool:
    """Extract content from agent response and write to file."""
    import re
    text = response_text.strip()
    if not text:
        return False

    ext = output_path.suffix.lower()

    # If target is a markdown file and response contains python code that writes a file,
    # extract the string content from inside the python code
    if ext == ".md":
        # Look for triple-quoted string content inside a code block
        triple_quoted = re.search(r'content\s*=\s*"""([\s\S]*?)"""', text)
        if triple_quoted:
            content = triple_quoted.group(1).strip()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(content)
            return True

    # Try markdown code block
    code_block = re.search(r"```(?:\w+)?\n([\s\S]*?)```", text)
    if code_block:
        inner = code_block.group(1).strip()
        # If inner content is Python that writes a file, extract the string inside
        if ext in (".md", ".yaml", ".yml", ".json", ".txt") and "open(" in inner:
            triple_quoted = re.search(r'"""([\s\S]*?)"""', inner)
            if triple_quoted:
                content = triple_quoted.group(1).strip()
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(content)
                return True
        content = inner
    else:
        content = text

    if not content:
        return False
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content)
    return True


# ── Agent execution via direct Ollama SDK call ────────────────────────────────

def run_agent(agent_name: str, task: str, output_file: str, context: str = "") -> dict:
    """
    Call Ollama directly via Anthropic SDK — bypasses claude -p and its
    broken thinking block parser (Content block not found bug).
    Thinking mode works correctly via native SDK streaming.
    """
    system_prompt = AGENT_SYSTEM_PROMPTS.get(agent_name, "")
    agent_context = AGENT_TASK_CONTEXT.get(agent_name, "")
    output_path = PROJECT_ROOT / output_file
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Build user message
    user_message = ""
    if agent_context:
        user_message += f"<agent_context>\n{agent_context}\n</agent_context>\n\n"
    if context:
        user_message += f"<context_from_previous_agents>\n{context}\n</context_from_previous_agents>\n\n"
    user_message += task
    user_message += (
        f"\n\nIMPORTANT: Write your complete output directly to the file: {output_file}\n"
        "Use the Write tool to create this file. Write the CONTENT ITSELF — not Python code that would create it.\n"
        "For .md files: write markdown text directly. For .py files: write Python code directly.\n"
        "Do not wrap the content in a Python script. Do not use open() or with open(). Just write the file content."
    )

    print(f"  🚀 [{agent_name}] starting → {output_file}")

    ollama_client = anthropic.Anthropic(
        api_key="ollama",
        base_url=OLLAMA_BASE_URL,
    )

    start_time = time.time()

    try:
        response_text = ""
        thinking_shown = False

        with ollama_client.messages.stream(
            model=AGENT_MODEL,
            max_tokens=8192,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        ) as stream:
            for event in stream:
                if not hasattr(event, "type"):
                    continue
                if event.type == "content_block_delta":
                    delta = event.delta
                    if not hasattr(delta, "type"):
                        continue
                    if delta.type == "text_delta":
                        response_text += delta.text
                        if VERBOSE:
                            # Print complete lines as they arrive
                            while "\n" in response_text:
                                line, response_text = response_text.split("\n", 1)
                                if line.strip():
                                    print(f"  │ [{agent_name}] {line}", flush=True)
                    elif delta.type == "thinking_delta" and VERBOSE and not thinking_shown:
                        print(f"  💭 [{agent_name}] thinking...", flush=True)
                        thinking_shown = True

        # Flush remaining text
        if VERBOSE and response_text.strip():
            print(f"  │ [{agent_name}] {response_text}", flush=True)

        # Get full response
        message = stream.get_final_message()
        full_text = ""
        for block in message.content:
            if hasattr(block, "text"):
                full_text += block.text

        elapsed = int(time.time() - start_time)

        # Try to write file from response content
        file_written = _write_file_from_response(full_text, output_path)

        # Check if already on disk (agent used Write tool directly)
        if not file_written:
            file_written = output_path.exists()

        if file_written:
            size = output_path.stat().st_size
            print(f"  ✅ [{agent_name}] done → {output_file} ({size} bytes, {elapsed}s)")
        else:
            print(f"  ❌ [{agent_name}] no file written after {elapsed}s")
            if full_text:
                print(f"     response preview: {full_text[:300]}")
            else:
                print(f"     response: (empty)")

        return {
            "agent": agent_name,
            "output_file": output_file,
            "success": file_written,
            "output": full_text,
        }

    except anthropic.APITimeoutError:
        file_written = output_path.exists()
        if file_written:
            print(f"  ⚠️  [{agent_name}] timed out but file exists → {output_file}")
            return {"agent": agent_name, "output_file": output_file, "success": True, "output": ""}
        print(f"  ⏱️  [{agent_name}] timed out — no file written")
        return {"agent": agent_name, "output_file": output_file, "success": False, "output": "timeout"}

    except Exception as exc:
        print(f"  ❌ [{agent_name}] error: {exc}")
        return {"agent": agent_name, "output_file": output_file, "success": False, "output": str(exc)}


# ── Execute a phase ───────────────────────────────────────────────────────────

def execute_phase(phase: dict, prior_results: dict) -> dict:
    """Execute all agents in a phase, skipping if dependencies missing."""
    agents = phase["agents"]
    parallel = phase.get("parallel", True)
    phase_num = phase["phase"]

    # Check dependencies — skip if any required dep has no file on disk
    all_deps = set()
    for a in agents:
        all_deps.update(str(x) for x in a.get("context_from", []))

    if all_deps:
        missing = [
            d for d in all_deps
            if not prior_results.get(d, {}).get("success")
            or not (PROJECT_ROOT / prior_results[d]["output_file"]).exists()
        ]
        if missing:
            print(f"\n{'='*60}")
            print(f"Phase {phase_num} — SKIPPED (missing: {', '.join(missing)})")
            print(f"{'='*60}")
            return {
                a["agent"]: {
                    "agent": a["agent"],
                    "output_file": a["output_file"],
                    "success": False,
                    "output": f"skipped — deps not ready: {', '.join(missing)}",
                }
                for a in agents
            }

    print(f"\n{'='*60}")
    print(f"Phase {phase_num} — {'parallel' if parallel else 'sequential'} ({len(agents)} agents)")
    print(f"{'='*60}")

    def run_with_context(agent_spec):
        context_parts = []
        for dep in agent_spec.get("context_from", []):
            if dep in prior_results and prior_results[dep].get("success"):
                dep_file = PROJECT_ROOT / prior_results[dep]["output_file"]
                if dep_file.exists():
                    context_parts.append(
                        f"=== {dep} output ({prior_results[dep]['output_file']}) ===\n"
                        + dep_file.read_text()
                    )
        context = "\n\n".join(context_parts)
        return run_agent(
            agent_name=agent_spec["agent"],
            task=agent_spec["task"],
            output_file=agent_spec["output_file"],
            context=context,
        )

    phase_results = {}
    if parallel and len(agents) > 1:
        with ThreadPoolExecutor(max_workers=min(len(agents), MAX_PARALLEL)) as executor:
            futures = {executor.submit(run_with_context, a): a for a in agents}
            for future in as_completed(futures):
                result = future.result()
                phase_results[result["agent"]] = result
    else:
        for agent_spec in agents:
            result = run_with_context(agent_spec)
            phase_results[result["agent"]] = result

    return phase_results


# ── Update AGENT_MEMORY.md ────────────────────────────────────────────────────

def update_memory(task: str, all_results: dict):
    memory_path = PROJECT_ROOT / "AGENT_MEMORY.md"
    if not memory_path.exists():
        return
    built = [f"- [x] {r['output_file']}" for r in all_results.values() if r.get("success")]
    failed = [f"- [ ] {r['output_file']} ❌" for r in all_results.values() if not r.get("success")]
    entry = f"\n### Task completed\n**Task:** {task[:100]}\n"
    if built:
        entry += "**Built:**\n" + "\n".join(built) + "\n"
    if failed:
        entry += "**Failed:**\n" + "\n".join(failed) + "\n"
    content = memory_path.read_text()
    content = content.replace("## Last Task Log", f"## Last Task Log{entry}")
    memory_path.write_text(content)
    print(f"\n📝 Updated AGENT_MEMORY.md")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("🎯 AI Agronomist Orchestrator v3")
    print(f"   Orchestrator: {ORCHESTRATOR_MODEL} (Anthropic)")
    print(f"   Agents: {AGENT_MODEL} via Ollama ({OLLAMA_BASE_URL})")
    print("=" * 60)

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("❌ ANTHROPIC_API_KEY not set")
        sys.exit(1)

    # Verify Ollama is reachable
    import urllib.request
    try:
        urllib.request.urlopen(f"{OLLAMA_BASE_URL}/api/tags", timeout=5)
    except Exception:
        print(f"❌ Ollama not reachable at {OLLAMA_BASE_URL}")
        sys.exit(1)

    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", "-t", help="Task string directly")
    parser.add_argument("--file", "-f", help="Path to .txt file containing the task")
    parser.add_argument("--verbose", "-v", action="store_true", help="Stream agent output live")
    args, _ = parser.parse_known_args()

    global VERBOSE
    VERBOSE = args.verbose
    if VERBOSE:
        print("🔍 Verbose mode — streaming agent output live")

    def get_tasks():
        if args.task:
            yield args.task
        elif args.file:
            yield open(args.file).read().strip()
        else:
            print("\nType your task (or 'quit' to exit).")
            print("For long tasks: python3 orchestrator_v3.py --file task.txt")
            print("─" * 60)
            while True:
                try:
                    user_input = input("\nYou: ").strip()
                except (KeyboardInterrupt, EOFError):
                    print("\n👋 Exiting")
                    return
                if user_input.lower() in ("quit", "exit", "q"):
                    print("👋 Exiting")
                    return
                if user_input:
                    yield user_input

    for user_input in get_tasks():
        plan = plan_task(user_input)

        print("\n📋 Execution plan:")
        for phase in plan["phases"]:
            ptype = "parallel" if phase.get("parallel") else "sequential"
            print(f"\n  Phase {phase['phase']} ({ptype}):")
            for a in phase["agents"]:
                ctx = f" [uses: {', '.join(str(x) for x in a['context_from'])}]" if a.get("context_from") else ""
                print(f"    • {a['agent']} → {a['output_file']}{ctx}")

        confirm = input("\nProceed? (y/n/auto): ").strip().lower()
        if confirm == "n":
            print("Cancelled.")
            continue

        all_results = {}
        for phase in plan["phases"]:
            phase_results = execute_phase(phase, all_results)
            all_results.update(phase_results)

        # Re-verify all files on disk
        print(f"\n{'='*60}")
        print("Verifying files on disk...")
        for r in all_results.values():
            actual = (PROJECT_ROOT / r["output_file"]).exists()
            if actual and not r.get("success"):
                print(f"  🔍 [{r['agent']}] found on disk → {r['output_file']}")
                r["success"] = True
            elif not actual and r.get("success"):
                print(f"  ⚠️  [{r['agent']}] marked success but missing → {r['output_file']}")
                r["success"] = False

        # Auto-retry failed agents once
        failed = [r for r in all_results.values()
                  if not r.get("success") and "skipped" not in r.get("output", "")]
        if failed:
            print(f"\n🔄 Retrying {len(failed)} failed agent(s)...")
            for r in failed:
                for phase in plan["phases"]:
                    for a in phase["agents"]:
                        if a["agent"] == r["agent"] and a["output_file"] == r["output_file"]:
                            ctx_parts = []
                            for dep in a.get("context_from", []):
                                if all_results.get(dep, {}).get("success"):
                                    dep_file = PROJECT_ROOT / all_results[dep]["output_file"]
                                    if dep_file.exists():
                                        ctx_parts.append(dep_file.read_text())
                            retry = run_agent(r["agent"], a["task"], r["output_file"],
                                              "\n\n".join(ctx_parts))
                            all_results[r["agent"]] = retry

        # Final summary
        succeeded = [r for r in all_results.values() if r.get("success")]
        failed_final = [r for r in all_results.values() if not r.get("success")]

        print(f"\n{'='*60}")
        print(f"Complete: {len(succeeded)} succeeded, {len(failed_final)} failed")
        for r in succeeded:
            size = (PROJECT_ROOT / r["output_file"]).stat().st_size if (PROJECT_ROOT / r["output_file"]).exists() else 0
            print(f"   ✓ {r['output_file']} ({size} bytes)")
        for r in failed_final:
            print(f"   ✗ {r['output_file']} — {r.get('output', '')[:80]}")
        print(f"{'='*60}")

        update_memory(user_input, all_results)


if __name__ == "__main__":
    main()