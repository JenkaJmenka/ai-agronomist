"""
Enhanced Managed Agents API Orchestrator with File Management
Coordinates code generation workflow using Managed Agents API
Handles file creation/modification based on agent outputs

Usage:
    export ANTHROPIC_API_KEY=sk-your-key
    python3 orchestrator_v2.py
"""

import os
import sys
import httpx
import json
import logging
from typing import Optional
from pathlib import Path
from anthropic import Anthropic

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class ManagedAgentOrchestrator:
    """Orchestrate code generation using Managed Agents API with file management"""
    
    def __init__(
        self, 
        gemma_server_url: str = "http://localhost:8000",
        project_root: str = "/Users/eugenesidorov/Projects/ai-agronomist"
    ):
        """
        Initialize orchestrator with file management
        
        Args:
            gemma_server_url: URL of Gemma agents HTTP server
            project_root: Root directory of the project
        """
        self.client = Anthropic()
        self.gemma_url = gemma_server_url
        self.project_root = Path(project_root)
        # 24-hour timeout (86400 seconds)
        self.http_client = httpx.Client(timeout=86400)
        
        # Validate API key
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise ValueError("ANTHROPIC_API_KEY not set")
        
        logger.info("Orchestrator initialized")
        logger.info(f"Gemma server: {self.gemma_url}")
        logger.info(f"Project root: {self.project_root}")
        
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
                name="Code Generation Orchestrator with File Management",
                system="""You are a code generation orchestrator. Your job is to:
1. Understand user requirements
2. Plan what code/tests/reviews are needed
3. Delegate to specialized agents via HTTP
4. Collect and synthesize results
5. Generate comprehensive reports

Always be thorough and ensure quality.

Note: You will also be responsible for creating and organizing files in the project based on agent outputs."""
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
                title="Code Generation Session with File Management"
            )
            logger.info(f"✅ Session created: {self.session.id}")
            
        except Exception as e:
            logger.error(f"Failed to setup Managed Agents: {str(e)}")
            raise
    
    def create_file(self, file_path: str, content: str, description: str = "") -> bool:
        """
        Create or overwrite a file in the project
        
        Args:
            file_path: Path relative to project root
            content: File content
            description: Why this file is being created
            
        Returns:
            True if successful, False otherwise
        """
        try:
            full_path = self.project_root / file_path
            full_path.parent.mkdir(parents=True, exist_ok=True)
            
            with open(full_path, 'w') as f:
                f.write(content)
            
            logger.info(f"✅ Created file: {file_path}")
            if description:
                print(f"   📄 {description}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to create file {file_path}: {str(e)}")
            return False
    
    def modify_file(self, file_path: str, content: str, description: str = "") -> bool:
        """
        Modify an existing file in the project
        
        Args:
            file_path: Path relative to project root
            content: New file content
            description: What was changed
            
        Returns:
            True if successful, False otherwise
        """
        try:
            full_path = self.project_root / file_path
            
            if not full_path.exists():
                logger.warning(f"File does not exist: {file_path}, creating it")
                return self.create_file(file_path, content, description)
            
            with open(full_path, 'w') as f:
                f.write(content)
            
            logger.info(f"✅ Modified file: {file_path}")
            if description:
                print(f"   ✏️  {description}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to modify file {file_path}: {str(e)}")
            return False
    
    def read_file(self, file_path: str) -> Optional[str]:
        """
        Read a file from the project
        
        Args:
            file_path: Path relative to project root
            
        Returns:
            File content or None if not found
        """
        try:
            full_path = self.project_root / file_path
            
            if not full_path.exists():
                logger.warning(f"File does not exist: {file_path}")
                return None
            
            with open(full_path, 'r') as f:
                return f.read()
            
        except Exception as e:
            logger.error(f"Failed to read file {file_path}: {str(e)}")
            return None
    
    def delegate_to_agent(self, agent_name: str, prompt: str) -> str:
        """
        Delegate a task to a specific Gemma agent via HTTP
        
        Args:
            agent_name: Agent to delegate to
            prompt: The prompt to send
            
        Returns:
            Agent's response
        """
        logger.info(f"Delegating to {agent_name} agent...")
        
        try:
            response = self.http_client.post(
                f"{self.gemma_url}/{agent_name}/generate",
                json={"prompt": prompt},
                timeout=86400  # 24 hours
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
        Main orchestration flow - delegates to specialist agents and manages files
        
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
3. You will receive agent responses
4. You'll be responsible for organizing the outputs into the project structure

Suggest which agents to use and how to organize the files in the project."""
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
            backend_output = self.delegate_to_agent("backend", user_request)
            if backend_output:
                agents_used.append("backend")
                agents_output["backend"] = backend_output
                print(backend_output)
                print("\n")
        
        if any(word in request_lower for word in ["test", "unit test", "comprehensive test", "test suite"]):
            logger.info("Delegating to QA Agent")
            print("🧪 QA Agent: Generating tests...\n")
            qa_prompt = user_request
            if "backend_output" in locals():
                qa_prompt += f"\n\nBase your tests on this code:\n{backend_output}"
            qa_output = self.delegate_to_agent("qa", qa_prompt)
            if qa_output:
                agents_used.append("qa")
                agents_output["qa"] = qa_output
                print(qa_output)
                print("\n")
        
        if any(word in request_lower for word in ["review", "check", "issue", "security", "quality"]):
            logger.info("Delegating to Code Reviewer Agent")
            print("👁️  Code Reviewer: Analyzing code...\n")
            code_to_review = agents_output.get("backend", user_request)
            review_output = self.delegate_to_agent(
                "review", 
                f"Review this code for security, logic, and quality issues:\n\n{code_to_review}"
            )
            if review_output:
                agents_used.append("review")
                agents_output["review"] = review_output
                print(review_output)
                print("\n")
        
        if any(word in request_lower for word in ["architecture", "design", "structure", "api design"]):
            logger.info("Delegating to Architect Agent")
            print("🏛️  Architect: Designing system...\n")
            arch_output = self.delegate_to_agent("architect", user_request)
            if arch_output:
                agents_used.append("architect")
                agents_output["architect"] = arch_output
                print(arch_output)
                print("\n")
        
        if any(word in request_lower for word in ["ml", "machine learning", "model", "neural", "pytorch", "lstm"]):
            logger.info("Delegating to ML Agent")
            print("🤖 ML Agent: Generating ML code...\n")
            ml_output = self.delegate_to_agent("ml", user_request)
            if ml_output:
                agents_used.append("ml")
                agents_output["ml"] = ml_output
                print(ml_output)
                print("\n")
        
        # If no specific agents matched, delegate to backend as default
        if not agents_used:
            logger.info("No specific agents matched, delegating to Backend Agent")
            print("⚙️  Backend Agent: Generating code...\n")
            backend_output = self.delegate_to_agent("backend", user_request)
            if backend_output:
                agents_used.append("backend")
                agents_output["backend"] = backend_output
                print(backend_output)
                print("\n")
        
        # FILE MANAGEMENT: Save generated content to project
        print("=" * 60)
        print("📁 Managing generated files...")
        print("=" * 60 + "\n")
        
        # Extract filenames from agent outputs and save them
        self.save_generated_files(agents_output, user_request)
        
        # Summary
        print("=" * 60)
        print(f"✅ Orchestration Complete!")
        print(f"Agents used: {', '.join(agents_used)}")
        print(f"Files created/modified: Check project structure")
        print("=" * 60)
    
    def save_generated_files(self, agents_output: dict, request: str):
        """
        Save generated content to project files
        
        Args:
            agents_output: Dictionary of agent outputs
            request: Original user request
        """
        # Backend output - save as Python file
        if "backend" in agents_output:
            content = agents_output["backend"]
            
            # Try to extract code blocks and save
            if "```python" in content:
                # Extract Python code
                start = content.find("```python") + 9
                end = content.find("```", start)
                if start > 9 and end > start:
                    code = content[start:end].strip()
                    filename = "src/generated_code.py"
                    self.create_file(filename, code, "Generated Python code")
        
        # QA output - save as test file
        if "qa" in agents_output:
            content = agents_output["qa"]
            
            if "```python" in content:
                start = content.find("```python") + 9
                end = content.find("```", start)
                if start > 9 and end > start:
                    tests = content[start:end].strip()
                    filename = "tests/generated_tests.py"
                    self.create_file(filename, tests, "Generated test file")
        
        # Review output - save as documentation
        if "review" in agents_output:
            content = agents_output["review"]
            filename = "docs/code_review.md"
            self.create_file(
                filename, 
                f"# Code Review\n\n{content}", 
                "Code review findings"
            )
        
        # Architecture output - save as design doc
        if "architect" in agents_output:
            content = agents_output["architect"]
            filename = "docs/architecture.md"
            self.create_file(
                filename, 
                f"# Architecture Design\n\n{content}", 
                "Architecture design document"
            )
        
        # ML output - save as ML code
        if "ml" in agents_output:
            content = agents_output["ml"]
            
            if "```python" in content:
                start = content.find("```python") + 9
                end = content.find("```", start)
                if start > 9 and end > start:
                    code = content[start:end].strip()
                    filename = "src/ml_models.py"
                    self.create_file(filename, code, "Generated ML model code")
        
        print("✅ Files organized in project structure")
    
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
    
    # Initialize orchestrator
    try:
        orchestrator = ManagedAgentOrchestrator()
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
    print("  - Managed Agents API (orchestration & file management)")
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
