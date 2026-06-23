from google.antigravity import Agent, LocalAgentConfig
from src.policies.guardrails import get_system_instructions

def get_memory_agent() -> Agent:
    """
    Agent to update AGENT_MEMORY.md based on recent task execution outputs.
    """
    return Agent(
        config=LocalAgentConfig(
            model="gemini-3.5-flash",
            system_instructions=get_system_instructions(
                "memory-agent",
                "You are the Memory Assistant. Read the current AGENT_MEMORY.md and the new task/execution results, then output the updated AGENT_MEMORY.md. Keep its structure: Current Phase, Completed Tasks, Pending/Next Steps, Technical Notes."
            ),
            name="memory-agent"
        )
    )
