# Code Review Tool (Skill)
from typing import Dict

def code_review(category: str) -> Dict[str, str]:
    """
    Code Review Skill Tool.
    Capability to review code for quality, architecture, and best practices.
    """
    checklists = {
        "quality": [
            "Code follows project style guide",
            "Proper error handling",
            "No code duplication",
            "Proper logging"
        ],
        "testing": [
            "Tests provided",
            "Test coverage >80%",
            "Edge cases handled",
            "All tests passing"
        ],
        "documentation": [
            "Docstrings complete",
            "Type hints present",
            "README updated",
            "Examples provided"
        ],
        "architecture": [
            "No tight coupling",
            "Components independent",
            "Scalable design",
            "Extensible for Phase 1.5"
        ]
    }
    
    if category not in checklists:
        return {"error": f"Unknown category: {category}"}
    
    # In a real tool execution, the agent would evaluate the codebase against this checklist.
    # Here, we just return the checklist expectations for the agent to consider.
    return {
        "category": category,
        "checklist": checklists[category],
        "output_format": "✅ Passes / ⚠️ Warning / 🛑 Blocker / 📝 Recommendation"
    }
