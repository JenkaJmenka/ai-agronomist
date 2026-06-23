from google.antigravity import Agent, LocalAgentConfig
from src.policies.guardrails import get_system_instructions

def get_devops_lead_agent() -> Agent:
    """
    Dockerfile, docker-compose, CI/CD, GitHub Actions, Jetson deployment configs. Use when containerization or deployment infrastructure is needed.
    """
    return Agent(
        config=LocalAgentConfig(
            model="gemini-2.5-pro",
            system_instructions=get_system_instructions("devops-lead", """You are the DevOps Lead for the AI Agronomist platform. Write Dockerfiles, docker-compose configs, and GitHub Actions CI/CD pipelines. Write to docker/, infra/, .github/workflows/ only. Use multi-stage builds, non-root users, health checks. Read AGENT_MEMORY.md first. Update AGENT_MEMORY.md after completing."""),
            name="devops-lead"
        )
    )
