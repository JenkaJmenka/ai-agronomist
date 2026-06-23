# Guardrails converted from .claude/guardrails/

UNIVERSAL_GUARDRAILS = """
# Universal Guardrails (ALL AGENTS)
1. **no_tight_coupling**: Components must be independent. Violation: Features share internal state or tightly coupled logic. Consequence: STOP WORK, escalate to architect.
2. **canonical_schema**: All data follows canonical sensor schema. Violation: Adding vendor-specific fields without mapping to canonical schema. Consequence: Request changes, file ADR.
3. **decisions_documented**: Every major decision documented in ADR. Violation: Major design changes without ADR. Consequence: STOP WORK until ADR filed.
4. **no_secrets_in_code**: No credentials, keys, or secrets in code. Violation: API keys, passwords, tokens in code. Consequence: STOP WORK, remove immediately, audit git history.
5. **monitoring_before_deploy**: All critical paths have monitoring configured.
6. **test_coverage_required**: >80% test coverage on critical paths.
"""

ML_GUARDRAILS = """
# ML Engineer Guardrails
1. **no_data_leakage**: Validation/test data must never influence training.
2. **reproducibility**: Fixed random seeds required.
3. **model_versioning**: Save metadata (epoch, loss, config) with all checkpoints.
"""

RED_FLAGS = """
# RED FLAGS (Immediate Stop)
- Ignoring architectural ADRs.
- Bypassing tests.
- Storing passwords in plain text.
- Pushing to production without review.
"""

def get_system_instructions(agent_name: str, base_instructions: str) -> str:
    """Inject guardrails into the agent's system instructions."""
    instructions = f"{base_instructions}\n\n{UNIVERSAL_GUARDRAILS}\n\n{RED_FLAGS}"
    
    if agent_name == "ml-engineer":
        instructions += f"\n\n{ML_GUARDRAILS}"
        
    return instructions
