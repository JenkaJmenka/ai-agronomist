from google.antigravity import Agent, LocalAgentConfig
from src.policies.guardrails import get_system_instructions

def get_git_agent() -> Agent:
    """
    Agent to generate conventional commit messages and pull request descriptions.
    """
    return Agent(
        config=LocalAgentConfig(
            model="gemini-3.5-flash",
            system_instructions=get_system_instructions(
                "git-agent",
                "You are the Git Assistant. Read the git diff and generate: 1) A Conventional Commit message (e.g. feat(ml): add LSTM-VAE model), 2) A detailed PR description listing changes, testing, and consequences."
            ),
            name="git-agent"
        )
    )
