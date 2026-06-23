#!/bin/bash
cd ~/Projects/ai-agronomist
export ANTHROPIC_BASE_URL="http://192.168.68.118:11434"
export ANTHROPIC_AUTH_TOKEN="local"
claude --model gemma4:26b --append-system-prompt "$(cat .claude/agents/architect.md)"