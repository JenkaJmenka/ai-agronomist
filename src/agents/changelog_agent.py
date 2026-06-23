from google.antigravity import Agent, LocalAgentConfig
from src.policies.guardrails import get_system_instructions

def get_changelog_agent() -> Agent:
    """
    Agent to automate generating updates for CHANGELOG.md based on recent code diffs.
    """
    return Agent(
        config=LocalAgentConfig(
            model="gemini-3.5-flash",
            system_instructions=get_system_instructions(
                "changelog-agent",
                "You are the Changelog Assistant. Analyze the git diff and summarize the changes. Output clean markdown list items for CHANGELOG.md (e.g. - Added X, - Fixed Y)."
            ),
            name="changelog-agent"
        )
    )
