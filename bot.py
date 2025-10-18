import os
import requests
import subprocess
import shutil
import time
import google.generativeai as genai
import re
import json
import hashlib
from pathlib import Path
from typing import Dict, List, Set, Optional, Tuple
import threading
from dataclasses import dataclass

# --- CONFIGURATION ---
@dataclass
class Config:
    # GitHub Configuration
    GITHUB_TOKEN: str = os.getenv('GH_PAT')
    GITHUB_USERNAME: str = os.getenv('GITHUB_USERNAME', 'shanaya-Gupta')
    
    # Gemini Configuration
    GEMINI_API_KEY: str = os.getenv('GEMINI_API_KEY')
    
    # Search Configuration
    SEARCH_QUERY: str = 'is:issue is:open label:"good first issue"'
    MAX_ISSUE_AGE_DAYS: int = 60
    MAX_ISSUE_COMMENTS: int = 5
    
    # Safety Configuration
    MAX_CONTEXT_SIZE: int = 400000
    MAX_FILE_SIZE: int = 50000
    MIN_ISSUE_BODY_LENGTH: int = 50  # Reduced threshold
    
    # File Safety - MUCH MORE PERMISSIVE
    SAFE_FILE_EXTENSIONS: Set[str] = None
    UNSAFE_FILE_PATTERNS: List[str] = None
    BLACKLISTED_DIRS: Set[str] = None
    UNSAFE_FILE_NAMES: Set[str] = None
    
    # Rate Limiting
    REQUESTS_PER_MINUTE_FLASH: int = 10
    REQUESTS_PER_MINUTE_PRO: int = 5
    TOKENS_PER_MINUTE_FLASH: int = 250000
    TOKENS_PER_MINUTE_PRO: int = 125000
    
    def __post_init__(self):
        # EXPANDED safe file extensions - include all common config files
        self.SAFE_FILE_EXTENSIONS = {
            '.py', '.js', '.ts', '.md', '.txt', '.html', '.css', '.json', 
            '.yaml', '.yml', '.toml', '.ini', '.cfg', '.conf', '.xml',
            '.java', '.cpp', '.c', '.h', '.hpp', '.rs', '.go', '.rb',
            '.php', '.sh', '.bash', '.zsh', '.ps1', '.bat'
        }
        
        # ONLY block actual dangerous patterns
        self.UNSAFE_FILE_PATTERNS = [
            r'\.(key|pem|p12|pfx|crt|cer|pub|priv|env|secret|token)$',
            r'/(\.env|\.aws|\.ssh)/',  # Config directories with secrets
        ]
        
        # Standard build/output directories
        self.BLACKLISTED_DIRS = {
            'node_modules', '__pycache__', '.git', 'venv', 'dist', 
            'build', 'target', '.idea', '.vscode', '__pycache__'
        }
        
        # Actually dangerous file names
        self.UNSAFE_FILE_NAMES = {
            '.env', 'secrets.json', 'config.prod.json', 
            'private.key', 'service-account.json'
        }

CONFIG = Config()
PROCESSED_ISSUES_FILE = "processed_issues.json"
METRICS_FILE = "bot_metrics.json"

# --- Rate Limiting Manager ---
class RateLimiter:
    def __init__(self):
        self.flash_requests = []
        self.pro_requests = []
        self.lock = threading.Lock()
    
    def wait_if_needed(self, model_type: str):
        """Simple RPM-based rate limiting"""
        with self.lock:
            now = time.time()
            one_minute_ago = now - 60
            
            if model_type == "flash":
                requests = [r for r in self.flash_requests if r > one_minute_ago]
                if len(requests) >= CONFIG.REQUESTS_PER_MINUTE_FLASH:
                    sleep_time = 61 - (now - min(requests))
                    print(f"⏳ Rate limit: Waiting {sleep_time:.1f}s for Flash")
                    time.sleep(max(1, sleep_time))
                self.flash_requests.append(now)
            else:  # pro
                requests = [r for r in self.pro_requests if r > one_minute_ago]
                if len(requests) >= CONFIG.REQUESTS_PER_MINUTE_PRO:
                    sleep_time = 61 - (now - min(requests))
                    print(f"⏳ Rate limit: Waiting {sleep_time:.1f}s for Pro")
                    time.sleep(max(1, sleep_time))
                self.pro_requests.append(now)

# Initialize rate limiter and AI models
rate_limiter = RateLimiter()
genai.configure(api_key=CONFIG.GEMINI_API_KEY)
model_flash = genai.GenerativeModel('gemini-1.5-flash-latest')
model_pro = genai.GenerativeModel('gemini-1.5-pro-latest')

# --- Simplified Metrics ---
class Metrics:
    @staticmethod
    def update(key, value=1):
        try:
            if os.path.exists(METRICS_FILE):
                with open(METRICS_FILE, 'r') as f:
                    metrics = json.load(f)
            else:
                metrics = {"issues_processed": 0, "prs_created": 0, "errors": 0}
            
            metrics[key] = metrics.get(key, 0) + value
            metrics["last_run"] = time.time()
            
            with open(METRICS_FILE, 'w') as f:
                json.dump(metrics, f, indent=2)
        except:
            pass  # Don't break on metrics errors

# --- Helper Functions ---
def get_processed_issues() -> Set[str]:
    if not os.path.exists(PROCESSED_ISSUES_FILE):
        return set()
    try:
        with open(PROCESSED_ISSUES_FILE, 'r') as f:
            data = json.load(f)
            return set(data.get("processed_issues", []))
    except:
        return set()

def add_issue_to_processed(issue_url: str):
    processed = get_processed_issues()
    processed.add(issue_url)
    data = {"processed_issues": list(processed)}
    with open(PROCESSED_ISSUES_FILE, 'w') as f:
        json.dump(data, f, indent=2)

def is_safe_to_modify(file_path: str) -> bool:
    """RELAXED safety checks - only block actually dangerous files"""
    
    # Check for blacklisted directories in path
    if any(f"/{d}/" in f"/{file_path}/" for d in CONFIG.BLACKLISTED_DIRS):
        return False
    
    # Check for unsafe file names
    file_name = os.path.basename(file_path)
    if file_name in CONFIG.UNSAFE_FILE_NAMES:
        return False
    
    # Check for unsafe patterns in path
    for pattern in CONFIG.UNSAFE_FILE_PATTERNS:
        if re.search(pattern, file_path, re.IGNORECASE):
            return False
    
    # Allow ALL safe extensions and any file without extension (like Dockerfile, Makefile)
    file_ext = Path(file_path).suffix.lower()
    if file_ext and file_ext not in CONFIG.SAFE_FILE_EXTENSIONS:
        # For unknown extensions, check if it's a common config file
        known_configs = {'Dockerfile', 'Makefile', 'docker-compose.yml', 'README', 'LICENSE'}
        if file_name not in known_configs:
            print(f"⚠️ Unknown file extension: {file_path}")
            return False
    
    return True

def call_gemini_with_limits(model, prompt: str, model_type: str = "flash") -> str:
    """Make API call with rate limiting"""
    rate_limiter.wait_if_needed(model_type)
    
    try:
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        print(f"❌ Gemini API error: {e}")
        raise

# --- Smart Issue Discovery ---
def is_good_issue_candidate(issue) -> bool:
    """Better issue qualification"""
    if not issue:
        return False
    
    # Basic checks
    if issue.get('comments', 0) > CONFIG.MAX_ISSUE_COMMENTS:
        return False
        
    body = issue.get('body', '')
    if len(body) < CONFIG.MIN_ISSUE_BODY_LENGTH:
        return False
    
    # Check title for obviously unsupported tasks
    title = issue.get('title', '').lower()
    unsupported_indicators = {
        'ui/ux', 'design', 'mobile', 'ios', 'android', 'illustration',
        'logo', 'graphic', 'artwork', 'translation', 'i18n'
    }
    
    if any(indicator in title for indicator in unsupported_indicators):
        return False
    
    return True

def find_github_issues():
    """Find qualified GitHub issues"""
    print("🔍 Searching for issues...")
    processed_issues = get_processed_issues()
    headers = {'Authorization': f'token {CONFIG.GITHUB_TOKEN}', 'Accept': 'application/vnd.github.v3+json'}
    url = f"https://api.github.com/search/issues?q={CONFIG.SEARCH_QUERY}&sort=created&order=desc&per_page=30"
    
    try:
        response = requests.get(url, headers=headers, timeout=30)
        if response.status_code != 200:
            print(f"❌ Error searching for issues: {response.status_code}")
            return None
        
        items = response.json().get('items', [])
        for issue in items:
            issue_url = issue.get('html_url')
            
            if issue_url in processed_issues:
                continue
                
            if is_good_issue_candidate(issue):
                print(f"✅ Found issue: {issue_url}")
                return issue
                
    except Exception as e:
        print(f"❌ Exception during issue search: {e}")
        Metrics.update("errors")
        
    print("🤷 No suitable issues found.")
    return None

# --- Better Repository Analysis ---
def get_repo_structure(temp_dir: str) -> str:
    """Get a string representation of the repository structure."""
    structure_lines = []
    try:
        for root, dirs, files in os.walk(temp_dir):
            # Filter directories
            dirs[:] = [d for d in dirs if d not in CONFIG.BLACKLISTED_DIRS and not d.startswith('.')]
            
            level = root.replace(temp_dir, '').count(os.sep)
            indent = ' ' * 2 * level
            rel_path = os.path.relpath(root, temp_dir)
            
            if rel_path != '.':
                structure_lines.append(f"{indent}{os.path.basename(root)}/")
            
            sub_indent = ' ' * 2 * (level + 1)
            for f in files[:10]:  # Limit files per directory
                if not f.startswith('.'):
                    structure_lines.append(f"{sub_indent}{f}")
            
            if len(structure_lines) > 150:
                break
    except Exception as e:
        print(f"⚠️ Error building file structure: {e}")

    return '\n'.join(structure_lines)

def get_repo_context(temp_dir: str, relevant_files: List[str]) -> Dict:
    """Gather context from a list of relevant files."""
    print("📚 Analyzing repository...")
    context = {"files": {}}
    total_size = 0

    if not relevant_files:
        print("🤷 No relevant files selected.")
        return context

    for rel_path in relevant_files:
        try:
            full_path = Path(temp_dir) / rel_path
            
            # Basic safety check
            if not is_safe_to_modify(rel_path) or not full_path.is_file():
                print(f"⚠️ Skipping unsafe or non-existent file: {rel_path}")
                continue

            content = full_path.read_text(encoding='utf-8', errors='ignore')

            if len(content) > CONFIG.MAX_FILE_SIZE:
                content = content[:CONFIG.MAX_FILE_SIZE] + "\n... [TRUNCATED] ..."

            context["files"][rel_path] = content
            total_size += len(content)

            if total_size > CONFIG.MAX_CONTEXT_SIZE:
                print("⚠️ Exceeded max context size.")
                break
        except Exception as e:
            print(f"⚠️ Error reading file {rel_path}: {e}")
            continue

    print(f"📁 Found {len(context['files'])} relevant files")
    return context

def fork_repository(repo_full_name: str, headers: Dict) -> bool:
    """Fork repository"""
    print(f"🍴 Forking {repo_full_name}...")
    fork_url = f"https://api.github.com/repos/{repo_full_name}/forks"
    
    try:
        response = requests.post(fork_url, headers=headers, timeout=30)
        if response.status_code in [200, 201, 202]:
            print("✅ Fork created")
            time.sleep(10)  # Shorter wait
            return True
        else:
            print(f"❌ Failed to fork: {response.status_code}")
            return False
    except Exception as e:
        print(f"❌ Fork error: {e}")
        Metrics.update("errors")
        return False

# --- AI Agents ---
def select_relevant_files(issue: Dict, file_structure: str) -> List[str]:
    """Select relevant files using AI"""
    print("\n--- 🧠 Selecting Relevant Files ---")

    prompt = f"""
    Based on the following GitHub issue and the repository's file structure, please identify the most relevant files to modify.

    **Issue Title:** {issue.get('title')}
    **Issue Body:**
    {issue.get('body', '')[:1500]}

    **File Structure:**
    {file_structure}

    Respond with a JSON-formatted list of the file paths you believe are most relevant to solving this issue. For example: ["src/main.py", "lib/utils.js"].
    Return a maximum of 5 files. If no files seem relevant, return an empty list.
    """

    try:
        response_text = call_gemini_with_limits(model_flash, prompt, "flash")

        # Find the JSON list in the response
        json_match = re.search(r'\[(.*?)\]', response_text, re.DOTALL)
        if json_match:
            # Reconstruct the list string to be valid JSON
            files_str = f"[{json_match.group(1)}]"
            selected_files = json.loads(files_str)

            # Ensure it's a list of strings
            if isinstance(selected_files, list) and all(isinstance(f, str) for f in selected_files):
                print(f"✅ Selected files: {selected_files[:5]}")
                return selected_files[:5] # Return max 5 files

        print("⚠️ Could not parse relevant files from response.")
        return []

    except Exception as e:
        print(f"❌ Error selecting relevant files: {e}")
        return []

def classify_task(issue: Dict) -> str:
    """Simple task classification"""
    print("\n--- 🤔 Task Classification ---")
    
    classifier_prompt = f"""Classify this GitHub issue:

TITLE: {issue.get('title')}
DESCRIPTION: {issue.get('body', '')[:1000]}

Options: BUGFIX, DOCUMENTATION, FEATURE, REFACTOR, UNSUPPORTED
Respond with one word only."""

    try:
        response = call_gemini_with_limits(model_flash, classifier_prompt, "flash")
        task_type = response.strip().upper()
        
        if task_type in {"BUGFIX", "DOCUMENTATION", "FEATURE", "REFACTOR"}:
            print(f"✅ Task: {task_type}")
            return task_type
        else:
            print(f"❌ Unsupported: {task_type}")
            return "UNSUPPORTED"
            
    except Exception as e:
        print(f"⚠️ Classification failed: {e}")
        return "UNSUPPORTED"

def create_implementation_plan(issue: Dict, repo_context: Dict, task_type: str) -> Optional[Dict]:
    """Create a detailed, step-by-step implementation plan."""
    print("\n--- 🧠 Planning ---")
    
    available_files = list(repo_context['files'].keys())
    if not available_files:
        print("❌ No files available for analysis")
        return None
    
    planner_prompt = f"""
    You are an expert software engineer. Your task is to create a detailed, step-by-step implementation plan to resolve a GitHub issue.

    **Task Type:** {task_type}
    **Issue Title:** {issue.get('title')}
    **Issue Description:**
    {issue.get('body', '')[:2000]}

    **Available Files:**
    {available_files}

    **Instructions:**
    1.  **Analyze the Goal:** Carefully understand the user's request in the issue.
    2.  **Identify Key Files:** From the available files, identify the 1-3 most relevant files to modify.
    3.  **Formulate a Plan:** Create a clear, logical sequence of steps to implement the solution. The steps should be specific and actionable.
    4.  **Think Step-by-Step:** Explain your reasoning for choosing these files and steps.

    **Output Format:**
    Respond with a clean JSON object. Do not include any markdown formatting or other text outside the JSON.

    ```json
    {{
      "reasoning": "A brief explanation of why you chose these files and this approach.",
      "files": [
        {{
          "path": "path/to/file1.py",
          "change_type": "REWRITE",
          "reason": "Explain why this specific file needs to be rewritten to solve the issue."
        }},
        {{
          "path": "path/to/file2.js",
          "change_type": "APPEND",
          "reason": "Explain what new functionality needs to be appended to this file and why."
        }}
      ],
      "steps": [
        "First, I will modify the `User` class in `file1.py` to include a new `email` field.",
        "Next, I will add a new validation function to `file2.js` to ensure the email format is correct.",
        "Finally, I will update the documentation to reflect these changes."
      ]
    }}
    ```
    """

    try:
        response = call_gemini_with_limits(model_flash, planner_prompt, "flash")
        
        # Extract JSON
        json_match = re.search(r'\{.*\}', response, re.DOTALL)
        if not json_match:
            print("❌ Could not parse plan")
            return None
            
        plan = json.loads(json_match.group())
        
        if not plan.get("files"):
            print("❌ No files in plan")
            return None
            
        # Validate files exist in context
        valid_files = []
        for file_plan in plan["files"]:
            file_path = file_plan["path"]
            if file_path in repo_context["files"]:
                valid_files.append(file_plan)
            else:
                print(f"⚠️ Planned file not found: {file_path}")
        
        if not valid_files:
            print("❌ No valid files in plan")
            return None
            
        plan["files"] = valid_files[:2]  # Max 2 files
        print(f"📝 Plan: {len(plan['files'])} files")
        return plan
        
    except Exception as e:
        print(f"❌ Planning failed: {e}")
        return None

def implement_changes(plan: Dict, repo_context: Dict) -> Dict:
    """Implement changes based on the plan."""
    print("\n--- ⚙️ Implementation ---")
    
    implementations = {}
    
    for file_plan in plan["files"]:
        file_path = file_plan["path"]
        change_type = file_plan.get("change_type", "REWRITE")
        
        print(f"🛠️ Implementing: {file_path}")
        
        original_content = repo_context["files"].get(file_path, "")
        
        prompt = f"""
        You are an expert software engineer. Your task is to write clean, professional code to implement a planned change for a file.

        **File to Modify:** `{file_path}`
        **Implementation Plan:**
        ```json
        {json.dumps(plan, indent=2)}
        ```

        **Original File Content:**
        ```
        {original_content[:12000]}
        ```

        **Instructions:**
        1.  **Follow the Plan:** Adhere strictly to the steps outlined in the implementation plan.
        2.  **Write High-Quality Code:** Produce clean, well-commented, and efficient code.
        3.  **Maintain Existing Style:** Match the coding style and conventions of the original file.
        4.  **Provide Only Code:** Your response should contain *only* the raw code for the new file content (for 'REWRITE') or the code to be added (for 'APPEND'). Do not include any explanations, markdown formatting, or introductory text.

        **Your Response:**
        """

        try:
            response = call_gemini_with_limits(model_flash, prompt, "flash")
            implementations[file_path] = {
                "content": response.strip(),
                "change_type": change_type
            }
            print(f"✅ Implemented: {file_path}")
            
        except Exception as e:
            print(f"❌ Implementation failed: {file_path} - {e}")
    
    return implementations

def critique_and_refine(plan: Dict, implementations: Dict, repo_context: Dict) -> Dict:
    """Critique and refine the implemented changes, returning a structured review."""
    print("\n--- 🧐 Critique & Refine ---")
    
    reviews = {}
    
    for file_path, implementation in implementations.items():
        original_content = repo_context["files"].get(file_path, "")
        draft_content = implementation["content"]
        
        prompt = f"""
        You are a senior software engineer reviewing a code change.

        **File:** `{file_path}`
        **Plan:** {json.dumps(plan['steps'], indent=2)}
        **Original Code:**
        ```
        {original_content[:6000]}
        ```
        **Proposed Code:**
        ```
        {draft_content[:8000]}
        ```

        **Instructions:**
        1.  **Review:** Critique the proposed code for correctness, bugs, style, and adherence to the plan.
        2.  **Decide:** If the code is perfect, set `requires_re_implementation` to `false`. If it has significant flaws that need a rewrite, set it to `true`.
        3.  **Refine:** Rewrite the code to fix all identified issues.

        **Output:** Respond *only* with a JSON object in the following format:
        ```json
        {{
          "requires_re_implementation": boolean,
          "review_summary": "Your brief critique of the code.",
          "refined_code": "The complete, corrected code."
        }}
        ```
        """

        try:
            response_text = call_gemini_with_limits(model_pro, prompt, "pro")
            
            json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
            if json_match:
                review = json.loads(json_match.group())
                # Ensure refined code is clean
                code_blocks = re.findall(r"```(?:\w+)?\n(.*?)```", review.get("refined_code", ""), re.DOTALL)
                if code_blocks:
                    review["refined_code"] = code_blocks[-1].strip()
                
                reviews[file_path] = review
                print(f"✅ Reviewed: {file_path}")
            else:
                raise ValueError("No JSON object found in critique response.")

        except Exception as e:
            print(f"❌ Critique failed for {file_path}: {e}")
            reviews[file_path] = {
                "requires_re_implementation": False,
                "review_summary": "Critique failed, using original implementation.",
                "refined_code": draft_content
            }
            
    return reviews

def apply_changes(temp_dir: str, implementations: Dict):
    """Apply changes to files"""
    print("\n--- 💾 Applying Changes ---")
    
    for file_path, implementation in implementations.items():
        full_path = Path(temp_dir) / file_path
        change_type = implementation["change_type"]
        
        try:
            if change_type == "APPEND":
                with full_path.open("a", encoding="utf-8") as f:
                    f.write("\n\n" + implementation["content"])
                print(f"✅ Appended: {file_path}")
            else:
                full_path.write_text(implementation["content"], encoding="utf-8")
                print(f"✅ Rewrote: {file_path}")
        except Exception as e:
            print(f"❌ Failed to write {file_path}: {e}")
            raise

def validate_changes(temp_dir: str, implementations: Dict) -> bool:
    """Basic validation"""
    print("\n--- 🔍 Validation ---")
    
    for file_path in implementations:
        full_path = Path(temp_dir) / file_path
        
        if not full_path.exists():
            print(f"❌ File missing: {file_path}")
            return False
            
        try:
            # Quick syntax check for common file types
            if file_path.endswith('.py'):
                content = full_path.read_text()
                compile(content, file_path, 'exec')
            elif file_path.endswith('.json'):
                content = full_path.read_text()
                json.loads(content)
        except Exception as e:
            print(f"⚠️ Validation warning for {file_path}: {e}")
            return False
    
    print("✅ Changes validated")
    return True

# --- Main Processing ---
def process_issue(issue):
    """Process an issue"""
    issue_url = issue.get('html_url')
    original_repo_full_name = issue['repository_url'].replace('https://api.github.com/repos/', '')
    headers = {'Authorization': f'token {CONFIG.GITHUB_TOKEN}', 'Accept': 'application/vnd.github.v3+json'}
    
    print(f"\n🎯 Processing: {issue_url}")
    
    # Classify task
    task_type = classify_task(issue)
    if task_type == "UNSUPPORTED":
        print("❌ Task not supported")
        return
    
    # Fork repo
    if not fork_repository(original_repo_full_name, headers):
        return
    
    # Clone repo
    forked_repo_full_name = f"{CONFIG.GITHUB_USERNAME}/{original_repo_full_name.split('/')[1]}"
    temp_dir = f"temp_repo_{int(time.time())}"
    
    try:
        print(f"📥 Cloning: {forked_repo_full_name}")
        subprocess.run([
            'git', 'clone', '--depth', '1', 
            f"https://github.com/{forked_repo_full_name}.git", temp_dir
        ], check=True, capture_output=True, timeout=120)
        
        # Sync with upstream
        original_repo_url = f"https://github.com/{original_repo_full_name}.git"
        subprocess.run(['git', 'remote', 'add', 'upstream', original_repo_url], cwd=temp_dir, check=True)
        subprocess.run(['git', 'fetch', 'upstream', '--depth', '1'], cwd=temp_dir, check=True)
        
        default_branch = subprocess.check_output(
            ['git', 'rev-parse', '--abbrev-ref', 'HEAD'], cwd=temp_dir, text=True
        ).strip()
        
        subprocess.run(['git', 'merge', f'upstream/{default_branch}'], cwd=temp_dir, check=True)
        print("✅ Repository ready")
        
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"❌ Git setup failed: {e}")
        shutil.rmtree(temp_dir, ignore_errors=True)
        return
    
    # Analyze repo
    repo_structure = get_repo_structure(temp_dir)
    relevant_files = select_relevant_files(issue, repo_structure)
    repo_context = get_repo_context(temp_dir, relevant_files)

    if not repo_context["files"]:
        print("❌ No files to work with")
        shutil.rmtree(temp_dir, ignore_errors=True)
        return
    
    # Create and execute plan
    plan = create_implementation_plan(issue, repo_context, task_type)
    if not plan:
        shutil.rmtree(temp_dir, ignore_errors=True)
        return
    
    implementations = implement_changes(plan, repo_context)
    if not implementations:
        print("❌ No implementations generated")
        shutil.rmtree(temp_dir, ignore_errors=True)
        return

    reviews = critique_and_refine(plan, implementations, repo_context)
    
    # Check if any file needs re-implementation
    needs_re_implementation = any(r.get("requires_re_implementation") for r in reviews.values())
    
    if needs_re_implementation:
        print("\n--- 🔄 Re-implementing based on feedback ---")
        # Update context with feedback for the next attempt
        feedback_context = {f: r["review_summary"] for f, r in reviews.items()}

        # Create a new plan or modify the old one with feedback
        plan["feedback"] = feedback_context

        implementations = implement_changes(plan, repo_context)
        reviews = critique_and_refine(plan, implementations, repo_context)

    # Final implementation from refined code
    final_implementations = {}
    for file_path, review in reviews.items():
        change_type = next((f["change_type"] for f in plan["files"] if f["path"] == file_path), "REWRITE")
        final_implementations[file_path] = {
            "content": review["refined_code"],
            "change_type": change_type
        }

    # Apply changes
    apply_changes(temp_dir, final_implementations)
    
    # Validate
    if not validate_changes(temp_dir, final_implementations):
        print("❌ Validation failed")
        shutil.rmtree(temp_dir, ignore_errors=True)
        return
    
    # Git operations
    new_branch = f"fix-{issue['number']}"
    print(f"\n--- 🚀 Pushing: {new_branch} ---")
    
    try:
        subprocess.run(['git', 'config', 'user.email', f'{CONFIG.GITHUB_USERNAME}@users.noreply.github.com'], cwd=temp_dir, check=True)
        subprocess.run(['git', 'config', 'user.name', CONFIG.GITHUB_USERNAME], cwd=temp_dir, check=True)
        subprocess.run(['git', 'checkout', '-b', new_branch], cwd=temp_dir, check=True)
        subprocess.run(['git', 'add', '.'], cwd=temp_dir, check=True)
        
        # Check for changes
        status = subprocess.run(['git', 'status', '--porcelain'], cwd=temp_dir, capture_output=True, text=True)
        if not status.stdout.strip():
            print("🤷 No changes to commit")
            shutil.rmtree(temp_dir, ignore_errors=True)
            return
        
        commit_msg = f"fix: Resolve #{issue['number']} - {issue['title']}"
        subprocess.run(['git', 'commit', '-m', commit_msg], cwd=temp_dir, check=True)
        
        # Push
        forked_repo_url_with_auth = f"https://{CONFIG.GITHUB_USERNAME}:{CONFIG.GITHUB_TOKEN}@github.com/{forked_repo_full_name}.git"
        subprocess.run(['git', 'remote', 'set-url', 'origin', forked_repo_url_with_auth], cwd=temp_dir, check=True)
        subprocess.run(['git', 'push', '-u', 'origin', new_branch], cwd=temp_dir, check=True)
        print("✅ Code pushed")
        
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"❌ Git operations failed: {e}")
        shutil.rmtree(temp_dir, ignore_errors=True)
        return
    
    # Create PR
    print("\n--- 📬 Creating PR ---")
    
    try:
        repo_info = requests.get(f"https://api.github.com/repos/{original_repo_full_name}", headers=headers).json()
        base_branch = repo_info.get('default_branch', 'main')
        
        modified_files = list(refined_implementations.keys())
        pr_body = f"""### Fix for Issue #{issue['number']}

**Issue:** {issue['title']}



**Changes:**
- Modified: {', '.join(f'`{f}`' for f in modified_files)}

*Please review carefully before merging.*"""

        pr_data = {
            'title': f"fix: {issue['title']}",
            'body': pr_body,
            'head': f"{CONFIG.GITHUB_USERNAME}:{new_branch}",
            'base': base_branch
        }
        
        pr_url = f"https://api.github.com/repos/{original_repo_full_name}/pulls"
        pr_response = requests.post(pr_url, headers=headers, json=pr_data)
        
        if pr_response.status_code in [200, 201]:
            pr_html_url = pr_response.json()['html_url']
            print(f"🎉 PR created: {pr_html_url}")
            Metrics.update("prs_created")
        else:
            print(f"❌ PR failed: {pr_response.status_code}")
            print(f"Response: {pr_response.text[:300]}")
            
    except Exception as e:
        print(f"❌ PR creation error: {e}")
    
    # Cleanup
    shutil.rmtree(temp_dir, ignore_errors=True)
    Metrics.update("issues_processed")

if __name__ == "__main__":
    if not CONFIG.GITHUB_TOKEN:
        print("❌ GH_PAT not set!")
        exit(1)
    
    if not CONFIG.GEMINI_API_KEY:
        print("❌ GEMINI_API_KEY not set!")
        exit(1)
    
    print("\n" + "="*50)
    print("🤖 AI Assistant - RELAXED Version")
    print("="*50)
    
    issue_to_process = find_github_issues()
    if issue_to_process:
        try:
            process_issue(issue_to_process)
        except Exception as e:
            print(f"❌ Critical error: {e}")
            Metrics.update("errors")
        finally:
            add_issue_to_processed(issue_to_process.get('html_url'))
            print(f"✅ Added to processed: {issue_to_process.get('html_url')}")
    else:
        print("🏁 No issues found")
    
    if os.path.exists(METRICS_FILE):
        with open(METRICS_FILE, 'r') as f:
            metrics = json.load(f)
    else:
        metrics = {}

    print(f"\n📊 Stats: {metrics.get('issues_processed', 0)} processed, "
          f"{metrics.get('prs_created', 0)} PRs, {metrics.get('errors', 0)} errors")
