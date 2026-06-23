import os
import subprocess
from pathlib import Path

def run_formatter(file_path: str):
    """
    Antigravity hook equivalent for post-tool-use.sh
    Runs formatters (prettier, ruff, black, gofmt, rustfmt) on the given file path.
    """
    if not file_path or not os.path.exists(file_path):
        return

    ext = file_path.split('.')[-1]
    
    try:
        if ext in ['js', 'jsx', 'ts', 'tsx', 'css', 'scss', 'json', 'html', 'md', 'mdx', 'yaml', 'yml']:
            subprocess.run(["npx", "prettier", "--write", file_path], check=False, capture_output=True)
        elif ext == 'py':
            # Try ruff first, fallback to black
            result = subprocess.run(["ruff", "format", file_path], check=False, capture_output=True)
            if result.returncode != 0:
                subprocess.run(["black", "--quiet", file_path], check=False, capture_output=True)
        elif ext == 'go':
            subprocess.run(["gofmt", "-w", file_path], check=False, capture_output=True)
        elif ext == 'rs':
            subprocess.run(["rustfmt", file_path], check=False, capture_output=True)
    except Exception as e:
        print(f"Failed to run formatter on {file_path}: {e}")

# This could be attached to agent.hooks.on_tool_call or post_turn in the Orchestrator
def on_tool_call_hook(tool_name: str, kwargs: dict):
    # Example logic: if a tool writes to a file, format it
    if tool_name in ["write_file", "edit_file", "replace_file_content"]:
        file_path = kwargs.get("TargetFile") or kwargs.get("AbsolutePath")
        if file_path:
            run_formatter(file_path)
