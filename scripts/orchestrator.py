"""
Managed Agents API Orchestrator
Coordinates code generation workflow using Managed Agents API
Delegates to Gemma agents via HTTP endpoints

Usage:
    export ANTHROPIC_API_KEY=sk-your-key
    python orchestrator.py
"""

import os
import sys
import httpx
import json
import logging
from typing import Optional
from anthropic import Anthropic

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class ManagedAgentOrchestrator:
    """Orchestrate code generation using Managed Agents API"""
    
    def __init__(self, gemma_server_url: str = "http://192.168.68.118:8000"):
        """
        Initialize orchestrator
        
        Args:
            gemma_server_url: URL of Gemma agents HTTP server
        """
        self.client = Anthropic()
        self.gemma_url = gemma_server_url
        self.http_client = httpx.Client(timeout=600)  # 10 minutes timeout for Gemma processing
        
        # Validate API key
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise ValueError("ANTHROPIC_API_KEY not set")
        
        logger.info("Orchestrator initialized")
        logger.info(f"Gemma server: {self.gemma_url}")
        
        # Initialize Managed Agents
        self.agent = None
        self.environment = None
        self.session = None
    
    def setup_managed_agents(self):
        """Create agent, environment, and session for Managed Agents API"""
        logger.info("Setting up Managed Agents...")
        
        try:
            # Create agent
            logger.info("Creating agent...")
            self.agent = self.client.beta.agents.create(
                model="claude-opus-4-7",
                name="Code Generation Orchestrator",
                system="""You are a code generation orchestrator. Your job is to:
1. Understand user requirements
2. Plan what code/tests/reviews are needed
3. Delegate to specialized agents via HTTP
4. Collect and synthesize results
5. Generate comprehensive reports

Always be thorough and ensure quality."""
            )
            logger.info(f"✅ Agent created: {self.agent.id}")
            
            # Create environment
            logger.info("Creating environment...")
            self.environment = self.client.beta.environments.create(
                name="orchestrator-env",
                config={
                    "type": "cloud",
                    "networking": {"type": "unrestricted"}
                }
            )
            logger.info(f"✅ Environment created: {self.environment.id}")
            
            # Create session
            logger.info("Creating session...")
            self.session = self.client.beta.sessions.create(
                agent=self.agent.id,
                environment_id=self.environment.id,
                title="Code Generation Session"
            )
            logger.info(f"✅ Session created: {self.session.id}")
            
        except Exception as e:
            logger.error(f"Failed to setup Managed Agents: {str(e)}")
            raise
    
    async def call_gemma_agent(self, agent: str, prompt: str) -> str:
        """
        Call a Gemma agent via HTTP
        
        Args:
            agent: Agent name (backend, qa, review, etc.)
            prompt: Prompt to send to agent
            
        Returns:
            Agent response
        """
        logger.info(f"Calling Gemma agent: {agent}")
        
        try:
            response = self.http_client.post(
                f"{self.gemma_url}/{agent}/generate",
                json={"prompt": prompt},
                timeout=180
            )
            
            if response.status_code != 200:
                logger.error(f"Agent {agent} returned {response.status_code}")
                return ""
            
            data = response.json()
            if data.get("error"):
                logger.error(f"Agent error: {data['error']}")
                return ""
            
            logger.info(f"✅ Got response from {agent}")
            return data.get("response", "")
            
        except Exception as e:
            logger.error(f"Failed to call {agent}: {str(e)}")
            return ""
    
    async def delegate_to_agent(self, agent_name: str, prompt: str) -> str:
        """
        Delegate a task to a specific Gemma agent via HTTP
        
        Args:
            agent_name: Agent to delegate to (backend, qa, review, etc.)
            prompt: The prompt to send
            
        Returns:
            Agent's response
        """
        logger.info(f"Delegating to {agent_name} agent...")
        
        try:
            response = self.http_client.post(
                f"{self.gemma_url}/{agent_name}/generate",
                json={"prompt": prompt},
                timeout=180
            )
            
            if response.status_code != 200:
                logger.error(f"Agent {agent_name} returned {response.status_code}")
                return ""
            
            data = response.json()
            if data.get("error"):
                logger.error(f"Agent error: {data['error']}")
                return ""
            
            logger.info(f"✅ Got response from {agent_name}")
            return data.get("response", "")
            
        except Exception as e:
            logger.error(f"Failed to call {agent_name}: {str(e)}")
            return ""
    
    def orchestrate(self, user_request: str):
        """
        Main orchestration flow - delegates to specialist agents
        
        Args:
            user_request: User's code generation request
        """
        if not self.session:
            logger.error("Session not initialized. Call setup_managed_agents() first")
            return
        
        logger.info("=" * 60)
        logger.info("Starting orchestration")
        logger.info("=" * 60)
        
        print(f"\n📝 User Request: {user_request}\n")
        
        # Use Managed Agents to understand the request and plan delegation
        print("🎯 Orchestrator is analyzing your request...\n")
        
        # Stream response from Managed Agents API for planning
        with self.client.beta.sessions.events.stream(self.session.id) as stream:
            # Send user message
            logger.info("Sending request to orchestrator agent...")
            self.client.beta.sessions.events.send(
                self.session.id,
                events=[
                    {
                        "type": "user.message",
                        "content": [
                            {
                                "type": "text",
                                "text": f"""You are orchestrating code generation. For this request: "{user_request}"

IMPORTANT: Do NOT generate code yourself. Instead:
1. Analyze what's needed
2. Decide which agents to delegate to (backend, qa, review, architect, ml, devops, tech-lead)
3. Ask me to wait while you delegate to agents
4. You will receive agent responses that I will collect

Start by saying what you need and which agents you'll use."""
                            }
                        ]
                    }
                ]
            )
            
            # Process streaming events - get orchestrator's plan
            plan_text = ""
            for event in stream:
                if event.type == "agent.message":
                    for block in event.content:
                        if hasattr(block, 'text'):
                            print(block.text, end="", flush=True)
                            plan_text += block.text
                
                elif event.type == "session.status_idle":
                    logger.info("✅ Orchestrator finished planning")
                    print("\n")
                    break
        
        # Now delegate to specialist agents based on request
        print("\n" + "=" * 60)
        print("🔄 Delegating to specialist agents...")
        print("=" * 60 + "\n")
        
        agents_used = []
        agents_output = {}
        
        # Decide which agents to use based on keywords in request
        request_lower = user_request.lower()
        
        if any(word in request_lower for word in ["function", "code", "api", "endpoint", "generate", "write"]):
            logger.info("Delegating to Backend Agent")
            print("⚙️  Backend Agent: Generating code...\n")
            backend_output = self.http_client.post(
                f"{self.gemma_url}/backend/generate",
                json={"prompt": user_request},
                timeout=180
            ).json().get("response", "")
            if backend_output:
                agents_used.append("backend")
                agents_output["backend"] = backend_output
                print(backend_output)
                print("\n")
        
        if any(word in request_lower for word in ["test", "unit test", "comprehensive test", "test suite"]):
            logger.info("Delegating to QA Agent")
            print("🧪 QA Agent: Generating tests...\n")
            qa_output = self.http_client.post(
                f"{self.gemma_url}/qa/generate",
                json={"prompt": user_request + "\n\nBase your tests on this code if provided: " + backend_output if "backend_output" in locals() else user_request},
                timeout=180
            ).json().get("response", "")
            if qa_output:
                agents_used.append("qa")
                agents_output["qa"] = qa_output
                print(qa_output)
                print("\n")
        
        if any(word in request_lower for word in ["review", "check", "issue", "security", "quality"]):
            logger.info("Delegating to Code Reviewer Agent")
            print("👁️  Code Reviewer: Analyzing code...\n")
            code_to_review = backend_output if "backend_output" in locals() else user_request
            review_output = self.http_client.post(
                f"{self.gemma_url}/review/generate",
                json={"prompt": f"Review this code for security, logic, and quality issues:\n\n{code_to_review}"},
                timeout=180
            ).json().get("response", "")
            if review_output:
                agents_used.append("review")
                agents_output["review"] = review_output
                print(review_output)
                print("\n")
        
        if any(word in request_lower for word in ["architecture", "design", "structure", "api design"]):
            logger.info("Delegating to Architect Agent")
            print("🏛️  Architect: Designing system...\n")
            arch_output = self.http_client.post(
                f"{self.gemma_url}/architect/generate",
                json={"prompt": user_request},
                timeout=180
            ).json().get("response", "")
            if arch_output:
                agents_used.append("architect")
                agents_output["architect"] = arch_output
                print(arch_output)
                print("\n")
        
        if any(word in request_lower for word in ["ml", "machine learning", "model", "neural", "pytorch", "lstm"]):
            logger.info("Delegating to ML Agent")
            print("🤖 ML Agent: Generating ML code...\n")
            ml_output = self.http_client.post(
                f"{self.gemma_url}/ml/generate",
                json={"prompt": user_request},
                timeout=180
            ).json().get("response", "")
            if ml_output:
                agents_used.append("ml")
                agents_output["ml"] = ml_output
                print(ml_output)
                print("\n")
        
        # If no specific agents matched, delegate to backend as default
        if not agents_used:
            logger.info("No specific agents matched, delegating to Backend Agent")
            print("⚙️  Backend Agent: Generating code...\n")
            backend_output = self.http_client.post(
                f"{self.gemma_url}/backend/generate",
                json={"prompt": user_request},
                timeout=180
            ).json().get("response", "")
            if backend_output:
                agents_used.append("backend")
                agents_output["backend"] = backend_output
                print(backend_output)
                print("\n")
        
        # Summary
        print("=" * 60)
        print(f"✅ Orchestration Complete!")
        print(f"Agents used: {', '.join(agents_used)}")
        print("=" * 60)
    
    def close(self):
        """Cleanup"""
        if self.http_client:
            self.http_client.close()
        logger.info("Orchestrator closed")

def main():
    """Main entry point"""
    
    # Check for API key
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("❌ ANTHROPIC_API_KEY not set")
        print("Set it with: export ANTHROPIC_API_KEY=sk-your-key")
        sys.exit(1)
    
    # Initialize orchestrator with localhost (agent server on Mac)
    try:
        orchestrator = ManagedAgentOrchestrator(gemma_server_url="http://localhost:8000")
    except ValueError as e:
        print(f"❌ {str(e)}")
        sys.exit(1)
    
    # Setup Managed Agents
    try:
        orchestrator.setup_managed_agents()
    except Exception as e:
        print(f"❌ Failed to setup Managed Agents: {str(e)}")
        sys.exit(1)
    
    # Interactive mode
    print("\n" + "=" * 60)
    print("🎯 Code Generation Orchestrator")
    print("=" * 60)
    print("\nYou are now connected to:")
    print("  - Managed Agents API (orchestration)")
    print("  - Gemma agents via HTTP (code generation)")
    print("\nType 'quit' to exit")
    print("=" * 60 + "\n")
    
    try:
        while True:
            # Get user input
            user_input = input("You: ").strip()
            
            if not user_input:
                continue
            
            if user_input.lower() in ['quit', 'exit', 'q']:
                print("👋 Goodbye!")
                break
            
            # Orchestrate
            try:
                orchestrator.orchestrate(user_input)
            except KeyboardInterrupt:
                print("\n⚠️  Interrupted by user")
                break
            except Exception as e:
                logger.error(f"Orchestration error: {str(e)}")
                print(f"\n❌ Error: {str(e)}\n")
    
    finally:
        orchestrator.close()

if __name__ == "__main__":
    main()