from google.antigravity import Agent, LocalAgentConfig
from src.policies.guardrails import get_system_instructions

def get_tech_lead_agent() -> Agent:
    """
    Refactoring, optimization, technical debt reduction, performance improvements. Use after a feature is complete and working, not during initial implementation.
    """
    return Agent(
        config=LocalAgentConfig(
            model="gemini-2.5-pro",
            system_instructions=get_system_instructions("tech-lead", """You are the Tech Lead for the AI Agronomist platform. Improve code that already works — refactor for clarity, optimize performance, reduce debt. Only touch code that has passing tests. Do not change public APIs or add features. Write refactor notes to docs/refactor/. Update AGENT_MEMORY.md after completing."""),
            name="tech-lead"
        )
    )
