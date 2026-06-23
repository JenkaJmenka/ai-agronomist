#!/bin/bash
cd ~/Projects/ai-agronomist
export ANTHROPIC_BASE_URL="https://api.anthropic.com"
export ANTHROPIC_API_KEY="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"
claude --model claude-sonnet-4-6 --append-system-prompt "$(cat .claude/agents/orchestrator.md)"