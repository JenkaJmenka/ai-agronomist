from google.antigravity import Agent, LocalAgentConfig
from src.policies.guardrails import get_system_instructions

def get_architect_agent() -> Agent:
    """
    System design, data models, API contracts, architecture decisions. Use when starting a new feature or when structural decisions need to be made before implementation begins.
    """
    return Agent(
        config=LocalAgentConfig(
            model="gemini-2.5-pro",
            system_instructions=get_system_instructions("architect", """You are the Architect for the AI Agronomist greenhouse monitoring platform. Design systems before they are built. Write to docs/architecture/ only. Do not write implementation code. Read AGENT_MEMORY.md first. Update AGENT_MEMORY.md after completing."""),
            name="architect"
        )
    )
