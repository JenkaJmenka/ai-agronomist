import os, sys, logging, time, argparse, asyncio, re
from pathlib import Path
import pydantic
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv()

from google.antigravity import Agent, LocalAgentConfig, types
# Import generated agents
from src.agents.architect import get_architect_agent
from src.agents.backend_engineer import get_backend_engineer_agent
from src.agents.code_reviewer import get_code_reviewer_agent
from src.agents.devops_lead import get_devops_lead_agent
from src.agents.ml_engineer import get_ml_engineer_agent
from src.agents.qa_engineer import get_qa_engineer_agent
from src.agents.tech_lead import get_tech_lead_agent

# Route logger to file so it doesn't pollute the formatted terminal output
_log_dir = PROJECT_ROOT / "logs"
_log_dir.mkdir(exist_ok=True)
_fh = logging.FileHandler(_log_dir / "orchestrator.log")
_fh.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
logging.basicConfig(level=logging.INFO, handlers=[_fh])
logger = logging.getLogger("Orchestrator")

# ── Pydantic Schemas ──

class AgentTaskSpec(pydantic.BaseModel):
    agent: str
    task: str
    output_file: str
    context_from: list[str] = []

class PhaseSpec(pydantic.BaseModel):
    phase: int
    parallel: bool = True
    agents: list[AgentTaskSpec]

class ExecutionPlan(pydantic.BaseModel):
    phases: list[PhaseSpec]

# ── Helpers ──

def _load_guardrails(root_path: Path) -> dict:
    """Load guardrail files from .claude/guardrails/ directory."""
    gd = root_path / ".claude/guardrails"
    return {f.stem: f.read_text() for f in gd.glob("*.md")} if gd.exists() else {}

def _extract_content(response_text: str, ext: str) -> str:
    """Extract code/content from the agent response text."""
    text = response_text.strip()
    if not text:
        return ""
    code_block = re.search(r"```(?:\w+)?\n([\s\S]*?)```", text)
    if code_block:
        inner = code_block.group(1).strip()
        if ext in (".md", ".yaml", ".yml", ".json", ".txt") and "open(" in inner:
            triple_quoted = re.search(r'"""([\s\S]*?)"""', inner)
            if triple_quoted:
                return triple_quoted.group(1).strip()
        return inner
    if ext == ".md":
        triple_quoted = re.search(r'content\s*=\s*"""([\s\S]*?)"""', text)
        if triple_quoted:
            return triple_quoted.group(1).strip()
    return text

def _write_output_file(content: str, path: Path) -> bool:
    """Write the content to the target path."""
    if not content:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return True
    except Exception as e:
        logger.error(f"Failed to write file {path}: {e}")
        return False

class Orchestrator:
    """Orchestrates code generation using the Google Antigravity SDK."""
    
    def __init__(self, root_path: Path):
        self.root = root_path
        self.guardrails = _load_guardrails(root_path)
        self.agent_contexts = self._build_agent_contexts()
        self.agent = Agent(config=LocalAgentConfig(
            model="gemini-2.5-pro", response_schema=ExecutionPlan,
            system_instructions=self._get_orchestrator_instructions(), name="orchestrator"
        ))

    def _build_agent_contexts(self) -> dict:
        """Load domain contexts from policies module."""
        from src.policies.agent_contexts import AGENT_CONTEXTS
        return AGENT_CONTEXTS

    def _get_orchestrator_instructions(self) -> str:
        return (
            "You are the main orchestrator. Decompose requests into a parallel execution plan.\n"
            "Rules:\n- Use agents: architect, ml-engineer, backend-engineer, qa-engineer, code-reviewer, devops-lead, tech-lead\n"
            "- Always include code-reviewer as the FINAL phase\n"
            "- Keep tasks FOCUSED on ONE file/concern\n"
            "- context_from specifies agent outputs from previous phases."
        )

    async def plan_task(self, task_prompt: str) -> ExecutionPlan:
        """Query orchestrator agent to decompose task into an ExecutionPlan."""
        logger.info("Planning task with Orchestrator agent...")
        backoff = 65
        for attempt in range(3):
            try:
                async with self.agent as planner:
                    response = await planner.chat(task_prompt)
                    plan_data = await response.structured_output()
                    if plan_data:
                        return ExecutionPlan.model_validate(plan_data)
            except Exception as e:
                logger.warning(f"Planning attempt {attempt + 1} failed: {e}")
                if attempt == 2:
                    raise
                logger.info(f"Retrying in {backoff} seconds...")
                await asyncio.sleep(backoff)
                backoff *= 2
        raise ValueError("Failed to generate execution plan.")

    def _get_agent_instance(self, agent_name: str) -> Agent:
        """Map agent name to its corresponding module generator."""
        mapping = {
            "architect": get_architect_agent,
            "backend-engineer": get_backend_engineer_agent,
            "code-reviewer": get_code_reviewer_agent,
            "devops-lead": get_devops_lead_agent,
            "ml-engineer": get_ml_engineer_agent,
            "qa-engineer": get_qa_engineer_agent,
            "tech-lead": get_tech_lead_agent
        }
        if agent_name not in mapping:
            raise ValueError(f"Unknown agent: {agent_name}")
        return mapping[agent_name]()

    def _build_user_message(self, spec: AgentTaskSpec, context: str) -> str:
        """Construct prompt to send to the subagent."""
        agent_ctx = self.agent_contexts.get(spec.agent, "")
        prompt = f"<agent_context>\n{agent_ctx}\n</agent_context>\n\n"
        if context:
            prompt += f"<context_from_previous_agents>\n{context}\n</context_from_previous_agents>\n\n"
        prompt += f"TASK: {spec.task}\n\n"
        prompt += f"IMPORTANT: Write your complete output directly to the file: {spec.output_file}\n"
        prompt += "Use the Write/Create file tools to write the content itself, not Python code that creates it."
        return prompt

    async def _execute_agent(self, agent: Agent, message: str, verbose: bool = False) -> str:
        """Execute the agent chat with retry logic. Returns full response text."""
        backoff = 65
        for attempt in range(3):
            try:
                async with agent:
                    response = await agent.chat(message)
                    if verbose:
                        async for token in response:
                            print(token, end="", flush=True)
                        print()
                    return await response.text()
            except Exception as e:
                logger.warning(f"Agent execution attempt {attempt + 1} failed: {e}")
                if attempt == 2:
                    raise
                logger.info(f"Retrying in {backoff} seconds...")
                await asyncio.sleep(backoff)
                backoff *= 2
        return ""

    async def run_subagent(self, spec: AgentTaskSpec, context: str = "", verbose: bool = False) -> dict:
        """Instantiate and run a single subagent."""
        print(f"\n[START] Agent: {spec.agent} -> {spec.output_file}")
        start_time = time.time()
        output_path = self.root / spec.output_file
        
        try:
            agent = self._get_agent_instance(spec.agent)
            prompt = self._build_user_message(spec, context)
            
            full_text = await self._execute_agent(agent, prompt, verbose)
            
            written = output_path.exists()
            if not written:
                extracted = _extract_content(full_text, output_path.suffix.lower())
                written = _write_output_file(extracted, output_path)
            if written:
                try:
                    from src.hooks.formatting_hooks import run_formatter
                    run_formatter(str(output_path))
                except Exception as e:
                    logger.warning(f"Failed to run formatting hook: {e}")
            
            elapsed = int(time.time() - start_time)
            status = "SUCCESS" if written else "FAILED"
            print(f"[DONE] Agent: {spec.agent} ({elapsed}s) - {status}")
            return {"agent": spec.agent, "output_file": spec.output_file, "success": written, "output": full_text}
        except Exception as e:
            elapsed = int(time.time() - start_time)
            print(f"[ERROR] Agent: {spec.agent} ({elapsed}s) - FAILED: {e}")
            return {"agent": spec.agent, "output_file": spec.output_file, "success": False, "output": str(e)}

    async def execute_phase(self, phase: PhaseSpec, prior_results: dict, verbose: bool = False) -> dict:
        """Execute all subagents in a phase sequentially or in parallel."""
        tasks = []
        for spec in phase.agents:
            context_parts = []
            for dep in spec.context_from:
                if dep in prior_results and prior_results[dep].get("success"):
                    dep_file = self.root / prior_results[dep]["output_file"]
                    if dep_file.exists():
                        context_parts.append(f"=== {dep} output ===\n{dep_file.read_text()}")
            context = "\n\n".join(context_parts)
            tasks.append(self.run_subagent(spec, context, verbose))
        
        results = {}
        if phase.parallel and len(tasks) > 1:
            phase_outputs = await asyncio.gather(*tasks)
            for res in phase_outputs:
                results[res["agent"]] = res
        else:
            for task in tasks:
                res = await task
                results[res["agent"]] = res
        return results

    async def update_memory(self, task: str, all_results: dict):
        """Update the AGENT_MEMORY.md log using the memory agent loop."""
        try:
            from scripts.agent_loops import run_memory_loop_async
            await run_memory_loop_async(task, all_results)
        except Exception as e:
            logger.error(f"Failed to run automated memory loop: {e}")

    async def run_orchestration(self, user_task: str, verbose: bool = False, auto_approve: bool = False):
        """Run the full planning and execution pipeline."""
        self.auto_approve = auto_approve
        plan = await self.plan_task(user_task)
        
        print("\n--- EXECUTION PLAN ---")
        for phase in plan.phases:
            mode = "parallel" if phase.parallel else "sequential"
            print(f"Phase {phase.phase} ({mode}):")
            for spec in phase.agents:
                print(f"  - {spec.agent} -> {spec.output_file}")
        print("----------------------\n")
        
        if not getattr(self, 'auto_approve', False):
            if input("\nProceed with execution? (y/n): ").strip().lower() != "y":
                print("Execution cancelled.")
                return
            
        all_results = {}
        for phase in plan.phases:
            mode = "parallel" if phase.parallel else "sequential"
            print(f"\n>>> Starting Phase {phase.phase} ({mode}) <<<")
            res = await self.execute_phase(phase, all_results, verbose)
            all_results.update(res)
        
        print("\n--- RESULTS ---")
        for agent, res in all_results.items():
            status = "OK" if res['success'] else "FAILED"
            print(f"{agent}: {status} -> {res['output_file']}")
            
        await self.update_memory(user_task, all_results)
        print("\nOrchestration complete!")


async def main_async():
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", "-t")
    parser.add_argument("--file", "-f")
    parser.add_argument("-v", "--verbose", action="store_true", help="Stream agent output to terminal")
    parser.add_argument("-y", "--yes", action="store_true", help="Auto-approve execution plan")
    args = parser.parse_args()
    
    task = args.task or (Path(args.file).read_text().strip() if args.file else None)
    if not task:
        parser.error("Must provide either --task or --file")
        
    await Orchestrator(PROJECT_ROOT).run_orchestration(task, args.verbose, args.yes)

if __name__ == "__main__":
    if not os.environ.get("GEMINI_API_KEY"):
        print("❌ GEMINI_API_KEY environment variable not set.")
        sys.exit(1)
    asyncio.run(main_async())
