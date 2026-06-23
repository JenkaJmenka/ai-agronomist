"""
HTTP Server for Gemma Agents
Runs on Windows machine and exposes agents as REST API endpoints

Usage:
    python agent_servers.py

Then agents are accessible at:
    http://192.168.68.118:8000/backend/generate
    http://192.168.68.118:8000/qa/generate
    http://192.168.68.118:8000/review/generate
    etc.
"""

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import subprocess
import logging
from typing import Optional
import os

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Gemma Agents Server", version="1.0.0")

# Request/Response models
class AgentRequest(BaseModel):
    prompt: str
    context: Optional[str] = None
    max_tokens: Optional[int] = None

class AgentResponse(BaseModel):
    response: str
    error: Optional[str] = None
    agent: str
    status: str

# Agent command mapping - use full paths to agent scripts
AGENTS = {
    "backend": "/Users/eugenesidorov/Projects/ai-agronomist/local/bin/agent-backend.sh",
    "architect": "/Users/eugenesidorov/Projects/ai-agronomist/local/bin/agent-architect.sh",
    "ml": "/Users/eugenesidorov/Projects/ai-agronomist/local/bin/agent-ml.sh",
    "devops": "/Users/eugenesidorov/Projects/ai-agronomist/local/bin/agent-devops.sh",
    "qa": "/Users/eugenesidorov/Projects/ai-agronomist/local/bin/agent-qa.sh",
    "review": "/Users/eugenesidorov/Projects/ai-agronomist/local/bin/agent-review.sh",
    "tech-lead": "/Users/eugenesidorov/Projects/ai-agronomist/local/bin/agent-lead.sh",
}

def call_agent(agent_name: str, prompt: str, context: Optional[str] = None) -> tuple[str, Optional[str]]:
    """
    Call a Gemma agent and return the response
    
    Args:
        agent_name: Name of agent (backend, qa, review, etc.)
        prompt: The prompt to send to the agent
        context: Additional context to include
        
    Returns:
        (response, error) tuple
    """
    if agent_name not in AGENTS:
        raise ValueError(f"Unknown agent: {agent_name}. Available: {list(AGENTS.keys())}")
    
    agent_cmd = AGENTS[agent_name]
    
    # Prepare the full prompt
    full_prompt = prompt
    if context:
        full_prompt = f"{context}\n\n{prompt}"
    
    logger.info(f"Calling agent: {agent_name} ({agent_cmd})")
    
    try:
        # Call the agent script
        result = subprocess.run(
            [agent_cmd],
            input=full_prompt,
            capture_output=True,
            text=True,
            timeout=86400,  # 24 hours timeout for Gemma processing
        )
        
        if result.returncode != 0:
            error_msg = f"Agent {agent_name} failed: {result.stderr}"
            logger.error(error_msg)
            return "", error_msg
        
        return result.stdout, None
        
    except subprocess.TimeoutExpired:
        error_msg = f"Agent {agent_name} timed out after 180 seconds"
        logger.error(error_msg)
        return "", error_msg
    except FileNotFoundError:
        error_msg = f"Agent command not found: {agent_cmd}. Make sure it's in PATH"
        logger.error(error_msg)
        return "", error_msg
    except Exception as e:
        error_msg = f"Error calling agent {agent_name}: {str(e)}"
        logger.error(error_msg)
        return "", error_msg

# Health check
@app.get("/health")
async def health_check():
    """Check if server is running"""
    return {
        "status": "healthy",
        "agents": list(AGENTS.keys()),
        "url": "http://192.168.68.118:8000"
    }

# Backend Agent
@app.post("/backend/generate")
async def backend_generate(request: AgentRequest) -> AgentResponse:
    """Generate code using Backend Agent"""
    logger.info("Backend agent request received")
    response, error = call_agent("backend", request.prompt, request.context)
    
    return AgentResponse(
        response=response,
        error=error,
        agent="backend",
        status="error" if error else "success"
    )

# Architect Agent
@app.post("/architect/generate")
async def architect_generate(request: AgentRequest) -> AgentResponse:
    """Generate architecture using Architect Agent"""
    logger.info("Architect agent request received")
    response, error = call_agent("architect", request.prompt, request.context)
    
    return AgentResponse(
        response=response,
        error=error,
        agent="architect",
        status="error" if error else "success"
    )

# ML Agent
@app.post("/ml/generate")
async def ml_generate(request: AgentRequest) -> AgentResponse:
    """Generate ML code using ML Agent"""
    logger.info("ML agent request received")
    response, error = call_agent("ml", request.prompt, request.context)
    
    return AgentResponse(
        response=response,
        error=error,
        agent="ml",
        status="error" if error else "success"
    )

# DevOps Agent
@app.post("/devops/generate")
async def devops_generate(request: AgentRequest) -> AgentResponse:
    """Generate infrastructure code using DevOps Agent"""
    logger.info("DevOps agent request received")
    response, error = call_agent("devops", request.prompt, request.context)
    
    return AgentResponse(
        response=response,
        error=error,
        agent="devops",
        status="error" if error else "success"
    )

# QA Agent
@app.post("/qa/generate")
async def qa_generate(request: AgentRequest) -> AgentResponse:
    """Generate tests using QA Agent"""
    logger.info("QA agent request received")
    response, error = call_agent("qa", request.prompt, request.context)
    
    return AgentResponse(
        response=response,
        error=error,
        agent="qa",
        status="error" if error else "success"
    )

# Code Review Agent
@app.post("/review/generate")
async def review_generate(request: AgentRequest) -> AgentResponse:
    """Review code using Code Review Agent"""
    logger.info("Review agent request received")
    response, error = call_agent("review", request.prompt, request.context)
    
    return AgentResponse(
        response=response,
        error=error,
        agent="review",
        status="error" if error else "success"
    )

# Tech Lead Agent
@app.post("/tech-lead/generate")
async def tech_lead_generate(request: AgentRequest) -> AgentResponse:
    """Get optimization advice using Tech Lead Agent"""
    logger.info("Tech Lead agent request received")
    response, error = call_agent("tech-lead", request.prompt, request.context)
    
    return AgentResponse(
        response=response,
        error=error,
        agent="tech-lead",
        status="error" if error else "success"
    )

# List all available agents
@app.get("/agents")
async def list_agents():
    """List all available agents and their endpoints"""
    return {
        "agents": [
            {
                "name": name,
                "endpoint": f"POST /api/{name}/generate",
                "command": cmd
            }
            for name, cmd in AGENTS.items()
        ]
    }

if __name__ == "__main__":
    import uvicorn
    
    print("=" * 60)
    print("🚀 Gemma Agents HTTP Server")
    print("=" * 60)
    print()
    print("Starting server on: http://0.0.0.0:8000")
    print("Available at: http://192.168.68.118:8000")
    print()
    print("Available agents:")
    for name in AGENTS.keys():
        print(f"  - POST /api/{name}/generate")
    print()
    print("Health check: http://192.168.68.118:8000/health")
    print()
    print("=" * 60)
    
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_level="info"
    )