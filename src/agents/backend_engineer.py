from google.antigravity import Agent, LocalAgentConfig
from src.policies.guardrails import get_system_instructions

def get_backend_engineer_agent() -> Agent:
    """
    FastAPI endpoints, SQLite/SQLAlchemy models, data ingestion, API routing, CSV upload handling. Use for all API and database implementation.
    """
    return Agent(
        config=LocalAgentConfig(
            model="gemini-3.5-flash",
            system_instructions=get_system_instructions("backend-engineer", """You are the Backend Engineer for the AI Agronomist platform. Implement FastAPI routes, SQLAlchemy models, and data ingestion pipelines. Write to src/api/ and src/db/ only. Use Pydantic v2, SQLAlchemy 2.x, full type hints. Read AGENT_MEMORY.md first. After writing code run smoke tests and fix until green. Update AGENT_MEMORY.md after completing."""),
            name="backend-engineer"
        )
    )
