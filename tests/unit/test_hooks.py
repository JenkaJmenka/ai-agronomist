import os
from unittest.mock import patch
from src.hooks.formatting_hooks import run_formatter, on_tool_call_hook

@patch('subprocess.run')
def test_run_formatter_python(mock_run):
    """Test that ruff is called for python files"""
    import subprocess
    mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
    
    dummy_file = "test.py"
    with patch('os.path.exists', return_value=True):
        run_formatter(dummy_file)
        
    mock_run.assert_called_with(["ruff", "format", dummy_file], check=False, capture_output=True)

@patch('subprocess.run')
def test_run_formatter_javascript(mock_run):
    """Test that prettier is called for js files"""
    dummy_file = "test.js"
    with patch('os.path.exists', return_value=True):
        run_formatter(dummy_file)
        
    mock_run.assert_called_with(["npx", "prettier", "--write", dummy_file], check=False, capture_output=True)

@patch('src.hooks.formatting_hooks.run_formatter')
def test_on_tool_call_hook(mock_run_formatter):
    """Test that the hook detects file modifications and calls the formatter."""
    kwargs = {"TargetFile": "/some/path/file.py"}
    on_tool_call_hook("write_file", kwargs)
    mock_run_formatter.assert_called_once_with("/some/path/file.py")
