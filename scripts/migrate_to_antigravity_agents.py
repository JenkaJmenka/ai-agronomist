import os
import re
import yaml
from pathlib import Path

# Constants and paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKUP_DIR = PROJECT_ROOT / '.claude' / 'agents'
AGENTS_ROOT_DIR = PROJECT_ROOT / '.agents'
AGENTS_SUB_DIR = AGENTS_ROOT_DIR / 'agents'

# Ensure directories exist
AGENTS_ROOT_DIR.mkdir(parents=True, exist_ok=True)
AGENTS_SUB_DIR.mkdir(parents=True, exist_ok=True)

# Tool mapping: Claude tools to Antigravity tools
TOOL_MAPPING = {
    "Read": ["view_file", "list_directory", "find_file", "search_directory"],
    "Write": ["create_file", "edit_file"],
    "Glob": ["find_file", "list_directory"],
    "Bash": ["run_command"],
}

# Model mapping: Claude models to Gemini models
MODEL_MAPPING = {
    "claude-opus-4-6": "gemini-2.5-pro",
    "claude-sonnet-4-6": "gemini-2.5-flash",
}

def translate_tools(claude_tools):
    if not claude_tools:
        return []
    antigravity_tools = []
    for tool in claude_tools:
        if tool in TOOL_MAPPING:
            antigravity_tools.extend(TOOL_MAPPING[tool])
        else:
            antigravity_tools.append(tool)
    # Deduplicate while preserving order (using dict keys)
    return list(dict.fromkeys(antigravity_tools))

def translate_model(agent_name, claude_model):
    if agent_name == "devops-lead":
        return "gemini-2.5-pro"
    return MODEL_MAPPING.get(claude_model, claude_model)

def migrate_agents():
    if not BACKUP_DIR.exists():
        print(f"Error: Claude agents directory not found at {BACKUP_DIR}")
        return

    migrated_agents = []

    for file_path in BACKUP_DIR.glob('*.md'):
        content = file_path.read_text()
        
        parts = content.split('---', 2)
        if len(parts) >= 3:
            try:
                frontmatter = yaml.safe_load(parts[1])
            except Exception as e:
                print(f"Error parsing YAML in {file_path.name}: {e}")
                continue
                
            instructions = parts[2].strip()
            
            name = frontmatter.get('name', file_path.stem)
            description = frontmatter.get('description', '')
            claude_model = frontmatter.get('model', '')
            claude_tools = frontmatter.get('tools', [])
            max_turns = frontmatter.get('max_turns', 30)
            
            # Translate values
            gemini_model = translate_model(name, claude_model)
            gemini_tools = translate_tools(claude_tools)
            
            # Construct new frontmatter
            new_frontmatter = {
                "name": name,
                "description": description,
                "tools": gemini_tools,
                "max_turns": max_turns,
                "model": gemini_model
            }
            
            # Serialize frontmatter
            fm_str = yaml.dump(new_frontmatter, default_flow_style=False, sort_keys=False).strip()
            new_content = f"---\n{fm_str}\n---\n{instructions}\n"
            
            # Write to both .agents/ and .agents/agents/
            file_name = file_path.name
            target_path_root = AGENTS_ROOT_DIR / file_name
            target_path_sub = AGENTS_SUB_DIR / file_name
            
            target_path_root.write_text(new_content)
            target_path_sub.write_text(new_content)
            
            migrated_agents.append({
                "name": name,
                "description": description,
                "model": gemini_model,
                "tools": gemini_tools,
                "max_turns": max_turns,
                "file": file_name
            })
            
            print(f"Migrated: {file_name} -> .agents/{file_name} and .agents/agents/{file_name}")

    # Generate .agents/agents.md router / documentation
    if migrated_agents:
        generate_agents_readme(migrated_agents)

def generate_agents_readme(agents):
    readme_content = """# Workspace Agents Config

This folder contains the specialized AI agents configured for the AI Agronomist workspace, migrated from Claude Code personas to the Google Antigravity SDK environment.

## Agent Team Overview

| Agent | Model | Max Turns | Description | Mapped Tools |
| :--- | :--- | :---: | :--- | :--- |
"""
    for agent in sorted(agents, key=lambda x: x["name"]):
        tools_str = ", ".join(f"`{t}`" for t in agent["tools"])
        readme_content += f"| **{agent['name']}** | `{agent['model']}` | {agent['max_turns']} | {agent['description']} | {tools_str} |\n"
        
    readme_content += "\n## Files\n\n"
    for agent in sorted(agents, key=lambda x: x["name"]):
        readme_content += f"- [{agent['file']}](file:///Users/eugenesidorov/Projects/ai-agronomist/.agents/agents/{agent['file']}): {agent['description']}\n"

    readme_path = AGENTS_ROOT_DIR / "agents.md"
    readme_path.write_text(readme_content)
    print(f"Generated registry: .agents/agents.md")

if __name__ == "__main__":
    migrate_agents()
