import os
from pathlib import Path

# Discover project root path dynamically
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Fallbacks in case .claude configs are missing
DEFAULT_UNIVERSAL = """
1. **no_tight_coupling**: Components must be independent.
2. **canonical_schema**: All data follows canonical sensor schema.
3. **decisions_documented**: Every major decision documented in ADR.
4. **no_secrets_in_code**: No credentials, keys, or secrets in code.
5. **test_coverage_required**: >80% test coverage on critical paths.
"""

DEFAULT_ML = """
1. **no_data_leakage**: Validation/test data must never influence training.
2. **reproducibility**: Fixed random seeds required.
3. **model_versioning**: Save metadata (epoch, loss, config) with all checkpoints.
"""

DEFAULT_RED_FLAGS = """
- Ignoring architectural ADRs.
- Bypassing tests.
- Storing passwords in plain text.
- Pushing to production without review.
"""

def load_guardrail(name: str, fallback: str) -> str:
    """Load a guardrail markdown file dynamically."""
    path = PROJECT_ROOT / ".claude/guardrails" / f"{name}.md"
    if path.exists():
        return path.read_text().strip()
    return f"# {name.replace('-', ' ').title()}\n{fallback.strip()}"

def load_skill_checklists(agent_name: str) -> str:
    """Load relevant skills that apply to the current agent."""
    skills_dir = PROJECT_ROOT / ".claude/skills"
    if not skills_dir.exists():
        return ""
    
    applicable_skills = []
    # Currently we have code-review.md
    cr_path = skills_dir / "code-review.md"
    if cr_path.exists():
        content = cr_path.read_text()
        # Simple applies_to check in markdown list format or string matching
        cr_agents = ["backend-engineer", "ml-engineer", "devops-lead", "architect"]
        if agent_name in cr_agents:
            applicable_skills.append(f"### ACTIVE SKILL: CODE REVIEW CHECKLIST\n{content.strip()}")
            
    # Also support a new automated-testing skill if created
    at_path = skills_dir / "automated-testing.md"
    if at_path.exists() and agent_name in ["qa-engineer", "ml-engineer"]:
        applicable_skills.append(f"### ACTIVE SKILL: AUTOMATED TESTING GATE\n{at_path.read_text().strip()}")
        
    return "\n\n".join(applicable_skills)

def load_handovers(agent_name: str) -> str:
    """Load active handovers and check if they mention the current agent."""
    handovers_dir = PROJECT_ROOT / ".claude/handovers"
    if not handovers_dir.exists():
        return ""
    
    applicable_handovers = []
    for f in handovers_dir.glob("*.md"):
        content = f.read_text()
        # Normalize agent names for matching
        normalized_agent = agent_name.replace("-", "").replace("_", "")
        normalized_content = content.lower().replace("-", "").replace("_", "")
        if normalized_agent in normalized_content or "all" in normalized_content:
            applicable_handovers.append(f"### ACTIVE HANDOVER EXPECTATIONS ({f.stem.upper()})\n{content.strip()}")
            
    return "\n\n".join(applicable_handovers)

def build_dynamic_contexts() -> dict:
    """Build domain contexts by loading guardrails, skills, and handovers dynamically."""
    universal = load_guardrail("universal-guardrails", DEFAULT_UNIVERSAL)
    ml_spec = load_guardrail("ml-guardrails", DEFAULT_ML)
    red_flags = load_guardrail("red-flags", DEFAULT_RED_FLAGS)

    base_contexts = {
        "architect": f"System design, architecture decisions, and ADR drafting. Write to docs/architecture/ only. Do not write implementation code.\n\nSPECIFIC DESIGN RESPONSIBILITIES:\n1. LSTM-VAE Architecture Specification (Input shape: (batch, seq_len=20, input_dim=4), Encoder hidden sizes: [64, 32], 8D latent space).\n2. Loss: MSE reconstruction + KL divergence with beta=0.1.\n3. Latent reparameterization z = mu + std * epsilon.\n4. Design documentation under docs/architecture/lstm_vae_design.md.",
        "ml-engineer": f"PyTorch models, LSTM-VAE, training pipelines, data generators, synthetic data, anomaly detection. Write to src/models/ and src/config.py only.\n\nSPECIFIC RESPONSIBILITIES:\n1. Create LSTMVAE class in src/models/lstm_vae.py.\n2. Create SyntheticDataGenerator in src/models/data_generator.py.\n3. Create central src/config.py.\n4. Create VAETrainer and CheckpointManager in src/models/training.py and scripts/train_lstm_vae.py.",
        "backend-engineer": f"FastAPI sensor API, database integrations, SQLite/SQLAlchemy models, router routes.\n\nSPECIFIC RESPONSIBILITIES:\n1. Implement clean REST API endpoints for sensor telemetry data ingestion and query.\n2. Maintain loose coupling between backend endpoints and machine learning models.",
        "qa-engineer": f"pytest test suites, integration tests, coverage enforcement. Target 90% coverage. Write to tests/ only. Never modify implementation files.\n\nSPECIFIC RESPONSIBILITIES:\n1. Create tests/unit/test_lstm_vae.py.\n2. Create tests/unit/test_data_generator.py.\n3. Create tests/integration/test_model_training.py.\n4. Configure pytest.ini and .coveragerc.",
        "code-reviewer": f"Security, quality, and guardrails audit. Read files and provide report/ADRs only. No edits to source code.\n\nSPECIFIC REVIEW RESPONSIBILITIES:\n1. Verify guardrails compliance and check for performance, security, and correctness.\n2. File ADR-001 (architecture), ADR-002 (synthetic data), and ADR-003 (modular architecture) under docs/team/ADRs/.\n3. Create docs/review/week1-review.md assessment.",
        "devops-lead": f"Docker, Compose, configurations, and CI/CD development environment.\n\nSPECIFIC RESPONSIBILITIES:\n1. docker-compose.yml: 4 services (Postgres 15, Jupyter, TensorBoard, Adminer).\n2. Dockerfile: Multi-stage, builder vs runtime, non-root user (appuser).\n3. Dockerfile.jupyter: Lab dev environment.\n4. Guide & scripts: docs/DOCKER_SETUP.md and scripts/health-check.sh.",
        "tech-lead": f"Refactoring, architectural guidance, addressing technical debt, code health enforcement.\n\nSPECIFIC RESPONSIBILITIES:\n1. Ensure files stay under 300 lines of code.\n2. Ensure single responsibility of functions and clean dependency flow."
    }

    dynamic_contexts = {}
    for agent, base_desc in base_contexts.items():
        ctx = f"{base_desc}\n\nGUARDRAILS:\n{universal}\n\nRED FLAGS:\n{red_flags}"
        if agent == "ml-engineer":
            ctx += f"\n\nML SPECIFIC GUARDRAILS:\n{ml_spec}"
            
        # Inject applicable skills
        skills = load_skill_checklists(agent)
        if skills:
            ctx += f"\n\n{skills}"
            
        # Inject applicable handovers
        handovers = load_handovers(agent)
        if handovers:
            ctx += f"\n\n{handovers}"
            
        dynamic_contexts[agent] = ctx
        
    return dynamic_contexts

# Expose AGENT_CONTEXTS dynamically
AGENT_CONTEXTS = build_dynamic_contexts()
