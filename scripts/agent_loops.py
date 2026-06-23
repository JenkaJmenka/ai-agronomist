import os
import sys
import argparse
import asyncio
import subprocess
from pathlib import Path
from dotenv import load_dotenv

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv()

from src.agents.git_agent import get_git_agent
from src.agents.changelog_agent import get_changelog_agent
from src.agents.memory_agent import get_memory_agent
from src.agents.adr_agent import get_adr_agent
from src.agents.code_reviewer import get_code_reviewer_agent

def get_git_diff(cached_only: bool = False) -> str:
    """Get the current git diff."""
    cmd = ["git", "diff", "--cached"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    diff = res.stdout.strip()
    if not diff and not cached_only:
        res = subprocess.run(["git", "diff"], capture_output=True, text=True)
        diff = res.stdout.strip()
    return diff

async def execute_agent(agent, prompt: str) -> str:
    """Helper to run agent chat safely."""
    async with agent:
        response = await agent.chat(prompt)
        text = await response.text()
        return text.strip()

async def run_git_loop():
    """Git commit and PR description loop."""
    print("🤖 Git Loop: Generating commit message and PR description...")
    diff = get_git_diff()
    if not diff:
        print("⚠️ No changes detected in Git.")
        return
    agent = get_git_agent()
    prompt = f"Analyze this git diff and write a Conventional Commit message and a PR description:\n\n{diff}"
    result = await execute_agent(agent, prompt)
    print("\n" + "="*40 + "\n🤖 Generated Git Output:\n" + "="*40 + f"\n{result}\n" + "="*40)
    
    # Save PR description to docs/PR_DESCRIPTION.md for convenience
    pr_desc_path = PROJECT_ROOT / "docs/PR_DESCRIPTION.md"
    pr_desc_path.parent.mkdir(parents=True, exist_ok=True)
    pr_desc_path.write_text(result)
    print(f"✅ Saved PR description to {pr_desc_path}")

async def run_changelog_loop():
    """Changelog update loop."""
    print("🤖 Changelog Loop: Updating CHANGELOG.md...")
    # Get last commit diff
    res = subprocess.run(["git", "diff", "HEAD~1"], capture_output=True, text=True)
    diff = res.stdout.strip() or get_git_diff()
    if not diff:
        print("⚠️ No changes detected to add to CHANGELOG.md.")
        return
    agent = get_changelog_agent()
    prompt = f"Generate bullet points for CHANGELOG.md based on this diff:\n\n{diff}"
    entry = await execute_agent(agent, prompt)
    
    changelog_path = PROJECT_ROOT / "CHANGELOG.md"
    current_content = changelog_path.read_text() if changelog_path.exists() else "# Changelog\n"
    
    # Prepend new entry
    new_content = f"{current_content}\n## Recent Updates\n{entry}\n"
    changelog_path.write_text(new_content)
    print(f"✅ Prepend changelog entry in {changelog_path}")

def run_memory_loop(task: str, all_results: dict):
    """Memory update helper called by orchestrator."""
    asyncio.run(run_memory_loop_async(task, all_results))

async def run_memory_loop_async(task: str, all_results: dict):
    """Memory update loop."""
    print("🤖 Memory Loop: Updating AGENT_MEMORY.md...")
    memory_path = PROJECT_ROOT / "AGENT_MEMORY.md"
    current_memory = memory_path.read_text() if memory_path.exists() else "# Agent Memory\n"
    
    built = [f"- [x] {r['output_file']}" for r in all_results.values() if r.get("success")]
    failed = [f"- [ ] {r['output_file']} ❌" for r in all_results.values() if not r.get("success")]
    
    execution_context = f"Task: {task}\nBuilt files:\n" + "\n".join(built) + "\nFailed files:\n" + "\n".join(failed)
    
    agent = get_memory_agent()
    prompt = f"Here is the current memory:\n\n{current_memory}\n\nHere is the new execution result:\n\n{execution_context}\n\nReturn the updated AGENT_MEMORY.md file content."
    updated_memory = await execute_agent(agent, prompt)
    
    if updated_memory and "# Agent Memory" in updated_memory or "## Project:" in updated_memory:
        memory_path.write_text(updated_memory)
        print(f"✅ Updated AGENT_MEMORY.md successfully.")
    else:
        # Fallback if model output is malformed
        entry = f"\n### Task completed via Antigravity\n**Task:** {task[:100]}\n"
        if built:
            entry += "**Built:**\n" + "\n".join(built) + "\n"
        content = current_memory + entry
        memory_path.write_text(content)
        print(f"✅ Appended changes to AGENT_MEMORY.md (fallback).")

async def run_adr_loop():
    """ADR update loop."""
    print("🤖 ADR Loop: checking architectural changes...")
    diff = get_git_diff()
    if not diff:
        print("⚠️ No changes to inspect for ADR updates.")
        return
    agent = get_adr_agent()
    prompt = f"Analyze these codebase changes and draft or update ADRs if they contain structural decisions. Git diff:\n\n{diff}"
    result = await execute_agent(agent, prompt)
    print("\n" + "="*40 + "\n🤖 Generated ADR / Architecture assessment:\n" + "="*40 + f"\n{result}\n" + "="*40)

async def run_validate_guardrails_loop():
    """Verify workspace against active guardrails."""
    print("🤖 Guardrails Loop: Auditing codebase against universal and ML guardrails...")
    from src.policies.agent_contexts import load_guardrail
    universal = load_guardrail("universal-guardrails", "")
    ml = load_guardrail("ml-guardrails", "")
    red_flags = load_guardrail("red-flags", "")
    
    diff = get_git_diff()
    if not diff:
        res = subprocess.run(["git", "diff", "HEAD~1"], capture_output=True, text=True)
        diff = res.stdout.strip()
        
    src_files_context = []
    models_dir = PROJECT_ROOT / "src/models"
    if models_dir.exists():
        for f in models_dir.glob("*.py"):
            src_files_context.append(f"=== {f.name} ===\n{f.read_text()}")
    
    config_file = PROJECT_ROOT / "src/config.py"
    if config_file.exists():
        src_files_context.append(f"=== config.py ===\n{config_file.read_text()}")
        
    code_context = "\n\n".join(src_files_context)
    agent = get_code_reviewer_agent()
    prompt = (
        "AUDIT THE CODEBASE FOR GUARDRAILS COMPLIANCE.\n\n"
        f"UNIVERSAL GUARDRAILS:\n{universal}\n\n"
        f"ML GUARDRAILS:\n{ml}\n\n"
        f"RED FLAGS:\n{red_flags}\n\n"
        f"GIT DIFF:\n{diff}\n\n"
        f"CODE SAMPLES:\n{code_context}\n\n"
        "Output format: list passes, warnings, and blockers. If there are any blockers (RED FLAGS), output 'STATUS: FAILED'. Otherwise, output 'STATUS: PASSED'."
    )
    result = await execute_agent(agent, prompt)
    print("\n" + "="*40 + "\n🤖 Guardrails Audit Results:\n" + "="*40 + f"\n{result}\n" + "="*40)
    if "STATUS: FAILED" in result:
        print("❌ Codebase failed guardrails audit!")
        sys.exit(1)
    print("✅ Codebase passed guardrails audit.")

async def main():
    parser = argparse.ArgumentParser(description="Automated Developer Workflows (Loops)")
    parser.add_argument("--git", "-g", action="store_true", help="Run commit and PR generator")
    parser.add_argument("--changelog", "-c", action="store_true", help="Run changelog update")
    parser.add_argument("--memory", "-m", action="store_true", help="Run memory update (standalone)")
    parser.add_argument("--adr", "-a", action="store_true", help="Run ADR analysis")
    parser.add_argument("--validate-guardrails", "-vg", action="store_true", help="Audit guardrails compliance")
    parser.add_argument("--all", action="store_true", help="Run all loops sequentially")
    args = parser.parse_args()
    
    if args.git or args.all:
        await run_git_loop()
    if args.changelog or args.all:
        await run_changelog_loop()
    if args.adr or args.all:
        await run_adr_loop()
    if args.validate_guardrails or args.all:
        await run_validate_guardrails_loop()
    if args.memory or args.all:
        await run_memory_loop_async("Manual repo maintenance", {})

if __name__ == "__main__":
    if not os.environ.get("GEMINI_API_KEY"):
        print("❌ GEMINI_API_KEY environment variable not set.")
        sys.exit(1)
    asyncio.run(main())
