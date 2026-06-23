#!/bin/bash

# Start Managed Agents API Orchestrator on Mac
# Usage: ./start_orchestrator.sh

clear

echo "============================================================"
echo ""
echo "  Managed Agents API Orchestrator"
echo ""
echo "============================================================"
echo ""

# Check if Python is installed
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 not found. Please install Python 3.10+ first."
    echo "   Download from: https://www.python.org/downloads/"
    exit 1
fi

echo "✅ Python found: $(python3 --version)"
echo ""

# Check for API key
if [ -z "$ANTHROPIC_API_KEY" ]; then
    echo "❌ ANTHROPIC_API_KEY not set"
    echo ""
    echo "Set it with:"
    echo "   export ANTHROPIC_API_KEY=sk-your-key"
    echo ""
    echo "Get your key from: https://console.anthropic.com/api_keys"
    echo ""
    exit 1
fi

echo "✅ API key is set"
echo ""

# Check dependencies
echo "Checking dependencies..."
python3 -c "import anthropic" 2>/dev/null
if [ $? -ne 0 ]; then
    echo "Installing Anthropic SDK..."
    pip install anthropic -q
    echo "✅ Anthropic SDK installed"
fi

python3 -c "import httpx" 2>/dev/null
if [ $? -ne 0 ]; then
    echo "Installing httpx..."
    pip install httpx -q
    echo "✅ httpx installed"
fi

echo "✅ All dependencies installed"
echo ""

# Check Windows server connectivity
echo "Checking Windows agent server..."
AGENT_SERVER="http://192.168.68.118:8000"
TIMEOUT=3

if curl -s --max-time $TIMEOUT "$AGENT_SERVER/health" > /dev/null 2>&1; then
    echo "✅ Agent server is running at $AGENT_SERVER"
else
    echo "⚠️  Warning: Agent server not responding at $AGENT_SERVER"
    echo ""
    echo "Make sure Windows server is running:"
    echo "   On Windows: python agent_servers.py"
    echo ""
    read -p "Continue anyway? (y/n) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

echo ""
echo "============================================================"
echo "Starting Orchestrator..."
echo "============================================================"
echo ""
echo "You are now connected to:"
echo "  - Managed Agents API (orchestration)"
echo "  - Gemma agents via HTTP (code generation)"
echo ""
echo "Type 'quit' to exit"
echo ""
echo "============================================================"
echo ""

# Run orchestrator
python3 orchestrator.py

exit 0
