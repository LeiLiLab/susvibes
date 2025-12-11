from __future__ import annotations

import os
import shlex
import shutil
import time
import subprocess
from pathlib import Path
from dotenv import load_dotenv
from prompts import USER_PROMPT_TEMPLATE, ADDITIONAL_INSTRUCTIONS, EXAMPLE_TASK, EXAMPLE_IMAGE

load_dotenv()


ALLOWED_TOOLS = [
    "Bash",
    "Edit",
    "Write",
    "Read",
    "Glob",
    "Grep",
    "LS",
    "WebFetch",
    "NotebookEdit",
    "NotebookRead",
    "TodoRead",
    "TodoWrite",
    "Agent",
]

class DockerIntegration:
    def __init__(self, docker_image: str, container_work_dir: str = "/project", workspace_root: str = ".", keep_workspace: bool = True):
        """
        Initialize Claude Docker Integration with execution feedback
        
        Args:
            docker_image: Your Docker image with embedded code
            container_work_dir: Working directory inside the container
            workspace_root: Root directory for workspaces
            keep_workspace: Whether to keep the workspace directory after cleanup (default: True)
        """
        self.docker_image = docker_image
        self.container_work_dir = container_work_dir
        self.work_container = None
        self.local_work_dir = None
        self.workspace_root = workspace_root
        self.keep_workspace = keep_workspace
        
    def setup_persistent_workspace(self) -> Path:
        """
        Create a persistent container workspace that allows execution feedback
        """
        print(f"🚀 Setting up persistent workspace from {self.docker_image}")
        
        # Create local working directory
        self.local_work_dir = Path(self.workspace_root).resolve() / f"claude_workspace_{int(time.time())}"
        self.local_work_dir.mkdir(exist_ok=True)
        
        try:
            # Step 1: Extract initial code to local directory
            self._extract_code_from_image()
            
            # Step 2: Start persistent container with volume mount
            self._start_persistent_container()
            
            print(f"✅ Workspace ready!")
            print(f"📁 Local: {self.local_work_dir}")
            print(f"🐳 Container: {self.work_container}")
            
            return self.local_work_dir
            
        except Exception as e:
            print(f"❌ Workspace setup failed: {e}")
            self.cleanup()
            raise
    
    def _extract_code_from_image(self):
        """Extract initial code from Docker image"""
        print("📦 Extracting initial code...")

        # Create unique temporary container name to avoid conflicts
        temp_container_name = f"temp_extract_{int(time.time())}_{id(self)}"
        
        # Create temporary container to extract files
        create_result = subprocess.run([
            "docker", "create", "--pull", "always", "--name", temp_container_name, 
            self.docker_image
        ], capture_output=True, text=True, check=True)
        
        try:
            # Copy files from container to local workspace
            subprocess.run([
                "docker", "cp", 
                f"{temp_container_name}:{self.container_work_dir}/.", 
                str(self.local_work_dir)
            ], check=True)
        finally:
            # Clean up temporary container
            subprocess.run(["docker", "rm", temp_container_name], 
                         capture_output=True)

    def _start_persistent_container(self):
        """Start a persistent container with volume mount for live sync"""
        print("🐳 Starting persistent container with live sync...")
        
        container_name = f"claude_work_{int(time.time())}"  
        
        # Start container with volume mount and keep it running
        run_result = subprocess.run([
            "docker", "run", "-d",
            "--name", container_name,
            "--network", "host",
            "-v", f"{self.local_work_dir}:{self.container_work_dir}",
            "-w", self.container_work_dir,
            self.docker_image,
            "tail", "-f", "/dev/null"  # Keep container alive
        ], capture_output=True, text=True, check=True)
        
        self.work_container = container_name
        print(f"✅ Container {container_name} started with live volume sync")
    
    def execute_in_container(self, command: str, env: dict = {}) -> dict:
        """
        Execute command in the persistent container and return detailed results
        
        Returns:
            Dict with stdout, stderr, return_code, and execution_time
        """
        if not self.work_container:
            raise RuntimeError("No persistent container available")
        
        start_time = time.time()

        full_command = f"""
        [ -f /root/.claude_env ] && source /root/.claude_env
        [ -f /root/.nvm/nvm.sh ] && source /root/.nvm/nvm.sh
        [ -f /root/.bashrc ] && source /root/.bashrc
        """

        if env:
            for key, value in env.items():
                full_command += f"export {key}={value}\n"

        full_command += f"{command}"
    
        
        exec_cmd = [
            "docker", "exec",
            "-w", self.container_work_dir,
            self.work_container,
            "bash", "-c", full_command
        ]
        
        try:
            result = subprocess.run(exec_cmd, capture_output=True, text=True, timeout=3000)
            execution_time = time.time() - start_time
            
            return {
                "stdout": result.stdout,
                "stderr": result.stderr,
                "return_code": result.returncode,
                "execution_time": execution_time,
                "command": command,
                "success": result.returncode == 0
            }
            
        except subprocess.TimeoutExpired:
            return {
                "stdout": "",
                "stderr": "Command timed out after 5 minutes",
                "return_code": -1,
                "execution_time": 300,
                "command": command,
                "success": False
            }
        except Exception as e:
            return {
                "stdout": "",
                "stderr": str(e),
                "return_code": -1,
                "execution_time": time.time() - start_time,
                "command": command,
                "success": False
            }
    

    def setup_cli_env(self, setup_script_path: str = "setup-env.sh") -> dict:
        """
        Install packages using setup.sh script in the container environment
        
        Args:
            setup_script_path: Path to setup.sh script (relative to workspace root)
            network_mode: Docker network mode ("host", "bridge", or custom network name)
            
        Returns:
            Dict with setup execution results
        """
        if not self.work_container:
            raise RuntimeError("No persistent container available. Call setup_persistent_workspace() first.")
        
        print(f"🔧 Setting up environment using {setup_script_path}...")
        
        # Check if setup_script_path exists locally
        setup_file = Path("./") / setup_script_path
        if not setup_file.exists():
            return {
                "stdout": "",
                "stderr": f"Setup script not found: {setup_file}",
                "return_code": 1,
                "success": False,
                "command": f"setup from {setup_script_path}"
            }
        
        try:
            # Step 1: Copy setup.sh to the container if it's not already volume-mounted
            container_setup_path = f"{self.container_work_dir}/{setup_script_path}"
            
            # Make sure the script is executable locally first
            subprocess.run(["chmod", "+x", str(setup_file)], check=True)
            
            # Copy the setup script to the container
            copy_result = subprocess.run([
                "docker", "cp", 
                str(setup_file),
                f"{self.work_container}:{container_setup_path}"
            ], capture_output=True, text=True)
            
            if copy_result.returncode != 0:
                return {
                    "stdout": "",
                    "stderr": f"Failed to copy setup script to container: {copy_result.stderr}",
                    "return_code": copy_result.returncode,
                    "success": False,
                    "command": f"copy {setup_script_path} to container"
                }
            
            # Step 4: Make script executable in container and run it
            chmod_result = self.execute_in_container(f"chmod +x {container_setup_path}")
            if not chmod_result["success"]:
                print(f"⚠️  Warning: Could not make script executable: {chmod_result['stderr']}")
            
            # Step 5: Execute setup_script_path
            print(f"🚀 Running setup script...")
            setup_result = self.execute_in_container(f"bash {container_setup_path}")
            
            if setup_result["success"]:
                print(f"✅ Environment setup completed successfully!")
                print(f"⏱️  Execution time: {setup_result['execution_time']:.2f}s")
                
                # Show last few lines of output for feedback
                if setup_result["stdout"]:
                    stdout_lines = setup_result["stdout"].strip().split('\n')
                    if len(stdout_lines) > 10:
                        print("📋 Setup output (last 10 lines):")
                        for line in stdout_lines[-10:]:
                            print(f"   {line}")
                    else:
                        print("📋 Setup output:")
                        print(setup_result["stdout"])
            else:
                print(f"❌ Environment setup failed!")
                print(f"Error: {setup_result['stderr']}")
                if setup_result["stdout"]:
                    print(f"Output: {setup_result['stdout']}")
            
            return setup_result
            
        except subprocess.CalledProcessError as e:
            error_msg = f"Docker command failed: {e.stderr if hasattr(e, 'stderr') else str(e)}"
            print(f"❌ {error_msg}")
            return {
                "stdout": e.stdout if hasattr(e, 'stdout') else "",
                "stderr": error_msg,
                "return_code": e.returncode,
                "success": False,
                "command": f"setup from {setup_script_path}",
                "execution_time": 0
            }
        except Exception as e:
            error_msg = f"Setup failed: {str(e)}"
            print(f"❌ {error_msg}")
            return {
                "stdout": "",
                "stderr": error_msg,
                "return_code": -1,
                "success": False,
                "command": f"setup from {setup_script_path}",
                "execution_time": 0
            }

    def cleanup(self):
        """Clean up containers and temporary files"""
        print("🧹 Cleaning up...")
        
        if self.work_container:
            try:
                subprocess.run(["docker", "stop", self.work_container], 
                             capture_output=True, timeout=10)
                subprocess.run(["docker", "rm", self.work_container], 
                             capture_output=True)
                print(f"✅ Removed container: {self.work_container}")
            except Exception as e:
                print(f"⚠️  Container cleanup issue: {e}")
        
        if self.local_work_dir and self.local_work_dir.exists():
            try:
                if self.keep_workspace:
                    # Keep the workspace - user can delete manually if needed
                    print(f"📁 Workspace preserved at: {self.local_work_dir}")
                    print("   (Delete manually if no longer needed)")
                else:
                    # Delete the workspace
                    shutil.rmtree(self.local_work_dir)
                    print(f"🗑️  Workspace deleted: {self.local_work_dir}")
            except Exception as e:
                print(f"⚠️  Workspace cleanup issue: {e}")
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.cleanup()


def main():

    TASK = EXAMPLE_TASK
    DOCKER_IMAGE = EXAMPLE_IMAGE

    env = {}
    env["ANTHROPIC_MODEL"] = os.environ.get("ANTHROPIC_MODEL", "")
    env["ANTHROPIC_BASE_URL"] = os.environ.get("ANTHROPIC_BASE_URL", "")
    env["ANTHROPIC_AUTH_TOKEN"] = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
    env["ANTHROPIC_API_KEY"] = os.environ.get("ANTHROPIC_API_KEY", "")
    print(f"🔧 Environment: {env}")

    prompt = USER_PROMPT_TEMPLATE.format(local_work_dir="/project", problem_statement=TASK)
    escaped_instruction = shlex.quote(prompt)

    with DockerIntegration(DOCKER_IMAGE, container_work_dir="/project", workspace_root=".", keep_workspace=False) as integration:
        # Set up the workspace
        workspace = integration.setup_persistent_workspace()

        integration.setup_cli_env()

        print(f"🔧 Setting up environment...")

        print(f"🔧 Running Claude...")
        print(f"🔧 Allowed Tools: {' '.join(ALLOWED_TOOLS)}")
        print(f"🔧 Task: {TASK}")
        claude_command = (
            "claude --verbose --output-format stream-json "
            f"-p {escaped_instruction} --allowedTools {' '.join(ALLOWED_TOOLS)}"
        )

        result = integration.execute_in_container(claude_command, env=env)
        print(f"🔧 Result: {result}")

        print(f"\n🎉 Session complete!")
        print(f"📁 Your improved code is at: {workspace}")

        return workspace


    

if __name__ == "__main__":
    main()