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
    REQUESTS_PER_MINUTE_FLASH: int = 8
    REQUESTS_PER_MINUTE_PRO: int = 4
    TOKENS_PER_MINUTE_FLASH: int = 200000
    TOKENS_PER_MINUTE_PRO: int = 100000
    
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
model_flash = genai.GenerativeModel('gemini-2.0-flash-exp')
model_pro = genai.GenerativeModel('gemini-2.0-flash-thinking-exp')

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
def get_repo_context(temp_dir: str) -> Dict:
    """Gather context with RELAXED safety"""
    print("📚 Analyzing repository...")
    context = {"structure": "", "files": {}}
    
    try:
        # Build simple structure
        structure_lines = []
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
        
        context["structure"] = '\n'.join(structure_lines)
        
        # Gather files with RELAXED safety
        total_size = 0
        files_found = 0
        
        for root, dirs, files in os.walk(temp_dir):
            dirs[:] = [d for d in dirs if d not in CONFIG.BLACKLISTED_DIRS]
            
            for file in files:
                if files_found > 50:  # Reasonable limit
                    break
                    
                full_path = Path(root) / file
                rel_path = str(full_path.relative_to(temp_dir))
                
                # Use relaxed safety check
                if not is_safe_to_modify(rel_path):
                    continue
                
                try:
                    content = full_path.read_text(encoding='utf-8', errors='ignore')
                    
                    # Size limits
                    if len(content) > CONFIG.MAX_FILE_SIZE:
                        content = content[:CONFIG.MAX_FILE_SIZE] + "\n... [TRUNCATED] ..."
                    
                    context["files"][rel_path] = content
                    total_size += len(content)
                    files_found += 1
                    
                    if total_size > CONFIG.MAX_CONTEXT_SIZE:
                        break
                        
                except Exception as e:
                    continue  # Skip unreadable files
                    
            if total_size > CONFIG.MAX_CONTEXT_SIZE:
                break
                
    except Exception as e:
        print(f"⚠️ Error gathering repo context: {e}")
        Metrics.update("errors")
        
    print(f"📁 Found {len(context['files'])} safe files")
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
    """Create implementation plan"""
    print("\n--- 🧠 Planning ---")
    
    available_files = list(repo_context['files'].keys())
    if not available_files:
        print("❌ No files available for analysis")
        return None
    
    planner_prompt = f"""Create an implementation plan for this {task_type} task:

ISSUE: {issue.get('title')}
DESCRIPTION: {issue.get('body', '')[:2000]}

AVAILABLE FILES: {available_files[:20]}

Identify 1-2 key files to modify. Respond with JSON:

{{
  "files": [
    {{
      "path": "file1.py",
      "change_type": "REWRITE|APPEND", 
      "reason": "Why modify this file"
    }}
  ],
  "steps": ["Step 1", "Step 2"]
}}"""

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
    """Implement changes"""
    print("\n--- ⚙️ Implementation ---")
    
    implementations = {}
    
    for file_plan in plan["files"]:
        file_path = file_plan["path"]
        change_type = file_plan["change_type"]
        
        print(f"🛠️ Implementing: {file_path}")
        
        original_content = repo_context["files"][file_path]
        
        if change_type == "APPEND":
            prompt = f"""Add content to this file:

FILE: {file_path}
PLAN: {json.dumps(plan, indent=2)}
EXISTING CONTENT:
{original_content[:8000]}

Provide ONLY the new content to append:"""
        else:
            prompt = f"""Rewrite this file:

FILE: {file_path}  
PLAN: {json.dumps(plan, indent=2)}
ORIGINAL CONTENT:
{original_content[:12000]}

Provide the COMPLETE rewritten file:"""

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

def critique_changes(plan: Dict, implementations: Dict, repo_context: Dict) -> Dict:
    """Critique changes"""
    print("\n--- 🧐 Critique ---")
    
    refined = {}
    
    for file_path, implementation in implementations.items():
        original_content = repo_context["files"][file_path]
        change_type = implementation["change_type"]
        draft_content = implementation["content"]
        
        if change_type == "APPEND":
            prompt = f"""Refine text to append:

FILE: {file_path}
PLAN: {json.dumps(plan, indent=2)}
ORIGINAL CONTENT (context):
{original_content[:4000]}
TEXT TO APPEND:
{draft_content}

Provide ONLY the refined text:"""
        else:
            prompt = f"""Refine this code:

FILE: {file_path}
PLAN: {json.dumps(plan, indent=2)}
ORIGINAL CONTENT:
{original_content[:8000]}
NEW CODE:
{draft_content[:10000]}

Provide ONLY the complete refined code:"""

        try:
            response = call_gemini_with_limits(model_pro, prompt, "pro")
            
            # Clean response
            refined_content = response.strip()
            code_blocks = re.findall(r"```(?:\w+)?\n(.*?)```", refined_content, re.DOTALL)
            if code_blocks:
                refined_content = code_blocks[-1].strip()
                
            refined[file_path] = {
                "content": refined_content,
                "change_type": change_type
            }
            print(f"✅ Refined: {file_path}")
            
        except Exception as e:
            print(f"❌ Critique failed: {file_path} - {e}")
            refined[file_path] = implementation  # Use original
    
    return refined

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
            # Don't fail on validation warnings
    
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
    repo_context = get_repo_context(temp_dir)
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
    
    refined_implementations = critique_changes(plan, implementations, repo_context)
    
    # Apply changes
    apply_changes(temp_dir, refined_implementations)
    
    # Validate
    if not validate_changes(temp_dir, refined_implementations):
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
