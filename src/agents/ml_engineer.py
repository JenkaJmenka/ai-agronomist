from google.antigravity import Agent, LocalAgentConfig
from src.policies.guardrails import get_system_instructions

def get_ml_engineer_agent() -> Agent:
    """
    PyTorch models, LSTM-VAE, training pipelines, data generators, synthetic data, anomaly detection logic. Use for all ML model implementation tasks.
    """
    return Agent(
        config=LocalAgentConfig(
            model="gemini-2.5-pro",
            system_instructions=get_system_instructions("ml-engineer", """You are the ML Engineer for the AI Agronomist platform. Implement PyTorch models, data pipelines, training loops, and inference code. Write to src/models/ and src/config.py only. Read AGENT_MEMORY.md first. Use full type hints and docstrings. After writing code run smoke tests and fix until green. Update AGENT_MEMORY.md after completing."""),
            name="ml-engineer"
        )
    )
