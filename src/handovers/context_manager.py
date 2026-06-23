import os
from pathlib import Path

def get_context(handover_name: str) -> str:
    """
    Loads handover or context markdown text.
    In a real system, these might be stored in the database or dynamically fetched.
    """
    handovers_dir = Path("src/handovers")
    file_path = handovers_dir / f"{handover_name}.md"
    
    if file_path.exists():
        return file_path.read_text()
    return ""

def inject_context_into_prompt(prompt: str, handover_names: list) -> str:
    """Injects content of specified handovers into a prompt."""
    contexts = [get_context(name) for name in handover_names]
    combined_context = "\n\n".join([c for c in contexts if c])
    
    if combined_context:
        return f"{combined_context}\n\n{prompt}"
    return prompt
