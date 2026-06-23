# Code Review Skill
skill_id: "code-review"
name: "Code Review"
description: "Capability to review code for quality, architecture, and best practices"

applies_to:
  - "backend-engineer"
  - "ml-engineer"
  - "devops-lead"
  - "architect"

checklist:
  quality:
    - "Code follows project style guide"
    - "Proper error handling"
    - "No code duplication"
    - "Proper logging"

  testing:
    - "Tests provided"
    - "Test coverage >80%"
    - "Edge cases handled"
    - "All tests passing"

  documentation:
    - "Docstrings complete"
    - "Type hints present"
    - "README updated"
    - "Examples provided"

  architecture:
    - "No tight coupling"
    - "Components independent"
    - "Scalable design"
    - "Extensible for Phase 1.5"

output_format:
  - "✅ Passes [category]"
  - "⚠️  Warning: [issue]"
  - "🛑 Blocker: [issue]"
  - "📝 Recommendation: [suggestion]"
