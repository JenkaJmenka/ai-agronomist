from google.antigravity import Agent, LocalAgentConfig
from src.policies.guardrails import get_system_instructions

def get_adr_agent() -> Agent:
    """
    Agent to analyze recent changes and verify if new Architecture Decision Records are needed or update existing ones.
    """
    return Agent(
        config=LocalAgentConfig(
            model="gemini-2.5-pro",
            system_instructions=get_system_instructions(
                "adr-agent",
                "You are the ADR Assistant. Check code changes for architectural decisions. If a new architectural decision has been made, draft an ADR under docs/team/ADRs/ using standard templates."
            ),
            name="adr-agent"
        )
    )
