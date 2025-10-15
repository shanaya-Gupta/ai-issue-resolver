import os
import requests
import subprocess
import shutil
import time
import google.generativeai as genai
import re
import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# --- CONFIGURATION ---
GITHUB_TOKEN = os.getenv('GH_PAT')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
GITHUB_USERNAME = "shanaya-Gupta"
SEARCH_QUERY = 'is:issue is:open label:"good first issue"'
PROCESSED_ISSUES_FILE = "processed_issues.txt"

# Configure Gemini AI Models
genai.configure(api_key=GEMINI_API_KEY)
model_flash = genai.GenerativeModel('gemini-flash-latest')
model_pro = genai.GenerativeModel('gemini-2.5-flash')

# --- HELPER FUNCTIONS ---

def get_processed_issues():
    """Load set of already processed issue URLs"""
    if not os.path.exists(PROCESSED_ISSUES_FILE): 
        return set()
    try:
        with open(PROCESSED_ISSUES_FILE, 'r') as f: 
            return set(line.strip() for line in f if line.strip())
    except Exception as e:
        print(f"⚠️  Could not read processed issues file: {e}")
        return set()

def add_issue_to_processed(issue_url):
    """Add issue URL to processed list"""
    try:
        with open(PROCESSED_ISSUES_FILE, 'a') as f: 
            f.write(issue_url + '\n')
    except Exception as e:
        print(f"⚠️  Could not write to processed issues file: {e}")

def safe_get_string(data: dict, key: str, default: str = '') -> str:
    """Safely get string value from dict, handling None"""
    value = data.get(key, default)
    return value if value is not None else default

def find_github_issues():
    """Search for new issues with enhanced filtering"""
    print("🔍 Searching for a new issue...")
    
    try:
        processed_issues = get_processed_issues()
    except Exception as e:
        print(f"❌ Error loading processed issues: {e}")
        return None
    
    headers = {
        'Authorization': f'token {GITHUB_TOKEN}', 
        'Accept': 'application/vnd.github.v3+json'
    }
    
    try:
        url = f"https://api.github.com/search/issues?q={SEARCH_QUERY}&sort=created&order=desc&per_page=100"
        response = requests.get(url, headers=headers, timeout=30)
        
        if response.status_code != 200:
            print(f"❌ Error searching for issues: {response.status_code}")
            return None
        
        items = response.json().get('items', [])
        
        if not items:
            print("⚠️  No issues found in search results")
            return None
        
        # Filter out issues that are too complex or too simple
        for issue in items:
            try:
                issue_url = safe_get_string(issue, 'html_url')
                
                if not issue_url:
                    continue
                
                if issue_url in processed_issues:
                    continue
                
                # Skip if too many comments (likely complex or abandoned)
                comments_count = issue.get('comments', 0)
                if comments_count > 15:
                    continue
                
                # Skip if issue body is too short (likely unclear) or None
                body = safe_get_string(issue, 'body')
                if len(body) < 50:
                    continue
                
                # Skip if no title
                title = safe_get_string(issue, 'title')
                if len(title) < 10:
                    continue
                
                print(f"✅ Found new issue: {issue_url}")
                return issue
                
            except Exception as e:
                print(f"⚠️  Error processing issue: {e}")
                continue
        
        print("⚠️  No new suitable issues found.")
        return None
        
    except Exception as e:
        print(f"❌ Error in find_github_issues: {e}")
        return None

def fork_repository(repo_full_name, headers):
    """Fork repository with better error handling"""
    print(f"🍴 Forking {repo_full_name}...")
    
    try:
        fork_url = f"https://api.github.com/repos/{repo_full_name}/forks"
        response = requests.post(fork_url, headers=headers, timeout=30)
        
        if response.status_code in [200, 201, 202]:
            print("✅ Fork created or already exists")
            time.sleep(20)  # Wait for fork to be ready
            return True
        elif response.status_code == 403:
            print("⚠️  Rate limit or permissions issue. Waiting 60 seconds...")
            time.sleep(60)
            return False
        else:
            print(f"❌ Failed to fork: {response.status_code} - {response.text[:200]}")
            return False
    except Exception as e:
        print(f"❌ Exception during fork: {e}")
        return False

def get_repo_structure(temp_dir: str) -> str:
    """Generate a tree-like structure of the repository"""
    structure = []
    try:
        for root, dirs, files in os.walk(temp_dir):
            # Skip hidden directories and common ignorable paths
            dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ['node_modules', '__pycache__', 'venv', 'env', 'dist', 'build']]
            
            level = root.replace(temp_dir, '').count(os.sep)
            indent = ' ' * 2 * level
            rel_root = os.path.relpath(root, temp_dir)
            if rel_root != '.':
                structure.append(f"{indent}{os.path.basename(root)}/")
            
            sub_indent = ' ' * 2 * (level + 1)
            for file in files[:50]:  # Limit files per directory
                if not file.startswith('.'):
                    structure.append(f"{sub_indent}{file}")
            
            if len(structure) > 500:  # Limit total lines
                break
    except Exception as e:
        print(f"⚠️  Error building repo structure: {e}")
        return "Could not build repository structure"
    
    return '\n'.join(structure)

def get_relevant_files(temp_dir: str, max_size: int = 800000) -> Dict[str, str]:
    """Read repository files intelligently"""
    files_content = {}
    extensions = (
        '.py', '.js', '.ts', '.jsx', '.tsx', '.java', '.go', '.rs', '.c', '.cpp', 
        '.h', '.hpp', '.cs', '.rb', '.php', '.swift', '.kt', '.scala',
        '.md', '.txt', '.yaml', '.yml', '.json', '.toml', '.ini', '.cfg',
        '.html', '.css', '.scss', '.less', '.vue', '.sql', '.sh', '.bash'
    )
    
    total_size = 0
    
    # Prioritize certain files
    priority_files = ['README.md', 'CONTRIBUTING.md', 'setup.py', 'package.json', 'requirements.txt', 'main.py', 'app.py', 'index.js']
    
    all_files = []
    try:
        for root, _, files in os.walk(temp_dir):
            if any(skip in root for skip in ['.git', 'node_modules', '__pycache__', 'venv', '.env', 'dist', 'build']):
                continue
            for file in files:
                if file.endswith(extensions):
                    full_path = os.path.join(root, file)
                    rel_path = os.path.relpath(full_path, temp_dir)
                    priority = 0 if file in priority_files else 1
                    all_files.append((priority, rel_path, full_path))
    except Exception as e:
        print(f"⚠️  Error walking directory: {e}")
        return files_content
    
    # Sort by priority
    all_files.sort(key=lambda x: x[0])
    
    for _, rel_path, full_path in all_files:
        if total_size >= max_size:
            break
        try:
            with open(full_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
                if len(content) > 50000:  # Skip very large files
                    content = content[:50000] + "\n... [FILE TRUNCATED] ..."
                files_content[rel_path] = content
                total_size += len(content)
        except Exception as e:
            print(f"⚠️  Could not read {rel_path}: {e}")
    
    print(f"📚 Loaded {len(files_content)} files ({total_size} chars)")
    return files_content

def fetch_issue_context(issue, headers):
    """Fetch additional context about the issue"""
    context = {
        'comments': [],
        'labels': [],
        'title': safe_get_string(issue, 'title', 'Untitled Issue'),
        'body': safe_get_string(issue, 'body', 'No description provided'),
        'number': issue.get('number', 0)
    }
    
    try:
        # Extract labels
        labels = issue.get('labels', [])
        if labels:
            context['labels'] = [label.get('name', '') for label in labels if isinstance(label, dict)]
        
        # Fetch comments
        comments_url = issue.get('comments_url')
        if comments_url and issue.get('comments', 0) > 0:
            response = requests.get(comments_url, headers=headers, timeout=30)
            if response.status_code == 200:
                comments = response.json()
                context['comments'] = [
                    {
                        'author': c.get('user', {}).get('login', 'unknown'),
                        'body': safe_get_string(c, 'body', '')
                    } 
                    for c in comments[:10] if isinstance(c, dict)  # Limit to first 10 comments
                ]
    except Exception as e:
        print(f"⚠️  Could not fetch full issue context: {e}")
    
    return context

def extract_code_from_response(response: str) -> str:
    """Robustly extract code from AI response"""
    if not response:
        return ""
    
    # Try to find code blocks
    code_blocks = re.findall(r"```(?:\w+)?\n(.*?)```", response, re.DOTALL)
    if code_blocks:
        return code_blocks[-1].strip()
    
    # Remove common markdown artifacts
    cleaned = re.sub(r'^#+\s+.*$', '', response, flags=re.MULTILINE)
    cleaned = re.sub(r'\*\*.*?\*\*', '', cleaned)
    cleaned = re.sub(r'^>\s+.*$', '', cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r'^\*\s+.*$', '', cleaned, flags=re.MULTILINE)
    
    return cleaned.strip()

def validate_code_syntax(file_path: str, code: str) -> Tuple[bool, str]:
    """Basic syntax validation for different languages"""
    if not code or not code.strip():
        return False, "Empty code"
    
    ext = Path(file_path).suffix.lower()
    
    if ext == '.py':
        try:
            compile(code, file_path, 'exec')
            return True, "Valid Python syntax"
        except SyntaxError as e:
            return False, f"Python syntax error: {e}"
    
    elif ext in ['.js', '.jsx', '.ts', '.tsx']:
        # Check for basic JS syntax issues
        if code.count('{') != code.count('}'):
            return False, "Mismatched braces"
        if code.count('(') != code.count(')'):
            return False, "Mismatched parentheses"
        if code.count('[') != code.count(']'):
            return False, "Mismatched brackets"
    
    elif ext == '.json':
        try:
            json.loads(code)
            return True, "Valid JSON"
        except json.JSONDecodeError as e:
            return False, f"JSON error: {e}"
    
    return True, "Basic validation passed"

def process_issue(issue):
    """Enhanced issue processing with multi-agent system"""
    issue_url = safe_get_string(issue, 'html_url')
    
    if not issue_url:
        print("❌ Invalid issue: no URL")
        return
    
    try:
        repo_url = safe_get_string(issue, 'repository_url')
        if not repo_url:
            print("❌ Invalid issue: no repository URL")
            return
        
        original_repo_full_name = repo_url.replace('https://api.github.com/repos/', '')
        
        if '/' not in original_repo_full_name:
            print("❌ Invalid repository name format")
            return
        
    except Exception as e:
        print(f"❌ Error parsing issue data: {e}")
        return
    
    headers = {
        'Authorization': f'token {GITHUB_TOKEN}', 
        'Accept': 'application/vnd.github.v3+json'
    }
    
    if not fork_repository(original_repo_full_name, headers):
        return
    
    forked_repo_full_name = f"{GITHUB_USERNAME}/{original_repo_full_name.split('/')[1]}"
    temp_dir = f"temp_repo_{int(time.time())}"
    
    try:
        # Setup repository
        print(f"📥 Cloning fork: {forked_repo_full_name}...")
        result = subprocess.run(
            ['git', 'clone', '--depth', '1', f"https://github.com/{forked_repo_full_name}.git", temp_dir],
            check=True, capture_output=True, text=True, timeout=120
        )
        
        print("🔄 Syncing with upstream...")
        original_repo_url = f"https://github.com/{original_repo_full_name}.git"
        subprocess.run(['git', 'remote', 'add', 'upstream', original_repo_url], cwd=temp_dir, check=True, timeout=30)
        subprocess.run(['git', 'fetch', 'upstream', '--depth', '1'], cwd=temp_dir, check=True, timeout=120)
        
        default_branch = subprocess.check_output(
            ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
            cwd=temp_dir, text=True, timeout=30
        ).strip()
        
        subprocess.run(['git', 'merge', f'upstream/{default_branch}'], cwd=temp_dir, check=True, timeout=60)
        print("✅ Repository synced")
        
    except subprocess.TimeoutExpired:
        print("❌ Git operation timed out")
        return
    except subprocess.CalledProcessError as e:
        print(f"❌ Git setup failed: {e}")
        if e.stderr:
            print(f"Error details: {e.stderr[:500]}")
        return
    except Exception as e:
        print(f"❌ Unexpected error during git setup: {e}")
        return
    
    # Gather repository context
    print("📚 Analyzing repository...")
    repo_structure = get_repo_structure(temp_dir)
    files_content = get_relevant_files(temp_dir)
    
    if not files_content:
        print("❌ No readable files found in repository")
        return
    
    issue_context = fetch_issue_context(issue, headers)
    
    # === STAGE 1: DEEP ISSUE ANALYSIS ===
    print("\n" + "="*60)
    print("🧠 STAGE 1: Deep Issue Analysis (Gemini Pro)")
    print("="*60)
    
    analysis_prompt = f"""You are an expert software architect analyzing a GitHub issue.

<ISSUE>
Title: {issue_context['title']}
Number: #{issue_context['number']}
Labels: {', '.join(issue_context['labels']) if issue_context['labels'] else 'None'}

Description:
{issue_context['body'][:2000]}

Comments:
{json.dumps(issue_context['comments'][:5], indent=2) if issue_context['comments'] else 'No comments'}
</ISSUE>

<REPOSITORY_STRUCTURE>
{repo_structure[:3000]}
</REPOSITORY_STRUCTURE>

Analyze this issue and provide:

1. PROBLEM_TYPE: (bug/feature/documentation/refactor/test)
2. CORE_ISSUE: One sentence describing the problem
3. ROOT_CAUSE: Technical reason
4. SOLUTION_APPROACH: High-level fix strategy
5. AFFECTED_AREAS: Which parts of codebase
6. RISK_LEVEL: (low/medium/high)

Respond in XML:
<ANALYSIS>
<PROBLEM_TYPE></PROBLEM_TYPE>
<CORE_ISSUE></CORE_ISSUE>
<ROOT_CAUSE></ROOT_CAUSE>
<SOLUTION_APPROACH></SOLUTION_APPROACH>
<AFFECTED_AREAS></AFFECTED_AREAS>
<RISK_LEVEL></RISK_LEVEL>
</ANALYSIS>"""

    try:
        time.sleep(15)  # Rate limit
        response = model_pro.generate_content(analysis_prompt)
        analysis = response.text
        print(f"📊 Analysis complete")
        
        # Check if risk is too high
        if "RISK_LEVEL>high" in analysis.lower():
            print("⚠️  Issue marked as high risk. Skipping for safety.")
            return
            
    except Exception as e:
        print(f"❌ Analysis failed: {e}")
        # Continue with empty analysis rather than failing
        analysis = "<ANALYSIS><PROBLEM_TYPE>unknown</PROBLEM_TYPE><CORE_ISSUE>Issue analysis failed</CORE_ISSUE><ROOT_CAUSE>Unknown</ROOT_CAUSE><SOLUTION_APPROACH>Proceed with caution</SOLUTION_APPROACH><AFFECTED_AREAS>Unknown</AFFECTED_AREAS><RISK_LEVEL>low</RISK_LEVEL></ANALYSIS>"
    
    # === STAGE 2: INTELLIGENT PLANNING ===
    print("\n" + "="*60)
    print("📋 STAGE 2: Creating Implementation Plan (Gemini Flash)")
    print("="*60)
    
    # Prepare file contents for planner
    files_context = ""
    for file_path, content in list(files_content.items())[:25]:  # Limit files
        files_context += f"\n{'='*40}\nFILE: {file_path}\n{'='*40}\n{content[:10000]}\n"
    
    planner_prompt = f"""You are a principal engineer creating an implementation plan.

<ANALYSIS>
{analysis[:2000]}
</ANALYSIS>

<ISSUE>
{issue_context['body'][:1500]}
</ISSUE>

<CODEBASE>
{files_context[:400000]}
</CODEBASE>

Create a concrete plan. You MUST:
1. Identify 1-3 files to modify (MUST exist in codebase)
2. For EACH file, specify exact changes needed
3. List files in order
4. Be specific about what to change

Respond in XML:
<PLAN>
<FILES_TO_MODIFY>
<FILE>
<PATH>exact/path/from/codebase.py</PATH>
<REASON>Why this file needs changes</REASON>
<CHANGES>
- Change 1
- Change 2
</CHANGES>
</FILE>
</FILES_TO_MODIFY>
<TESTING_STRATEGY>How to verify</TESTING_STRATEGY>
</PLAN>

CRITICAL: Only use file paths from the codebase. Do not invent paths."""

    try:
        response = model_flash.generate_content(planner_prompt)
        plan = response.text
        print(f"📝 Plan created")
        
        # Parse files to modify
        file_matches = re.findall(r"<FILE>.*?<PATH>(.*?)</PATH>.*?</FILE>", plan, re.DOTALL)
        if not file_matches:
            print("❌ No valid files identified in plan")
            return
            
        files_to_modify = [f.strip() for f in file_matches if f.strip() and f.strip().lower() != "n/a"]
        
        if not files_to_modify:
            print("❌ No actionable files in plan")
            return
        
        print(f"📂 Files to modify: {files_to_modify[:3]}")
        
    except Exception as e:
        print(f"❌ Planning failed: {e}")
        return
    
    # === STAGE 3: IMPLEMENTATION ===
    print("\n" + "="*60)
    print("⚙️  STAGE 3: Implementing Changes (Gemini Flash)")
    print("="*60)
    
    implemented_files = {}
    
    for file_path in files_to_modify[:3]:  # Limit to 3 files
        print(f"\n🔧 Implementing: {file_path}")
        
        # Read original file
        full_path = os.path.join(temp_dir, file_path)
        if not os.path.exists(full_path):
            print(f"⚠️  File not found, skipping: {file_path}")
            continue
        
        try:
            with open(full_path, 'r', encoding='utf-8', errors='ignore') as f:
                original_content = f.read()
        except Exception as e:
            print(f"⚠️  Could not read {file_path}: {e}")
            continue
        
        implementer_prompt = f"""You are a senior engineer implementing a code change.

<PLAN>
{plan[:5000]}
</PLAN>

<FILE_PATH>
{file_path}
</FILE_PATH>

<CURRENT_CONTENT>
{original_content[:30000]}
</CURRENT_CONTENT>

Implement the changes for this file according to the plan.

RULES:
1. Output ONLY the complete modified file
2. NO markdown, NO explanations
3. Keep existing functionality
4. Match coding style
5. Make minimal necessary changes

Output the complete file:"""

        try:
            response = model_flash.generate_content(implementer_prompt)
            draft_code = extract_code_from_response(response.text)
            
            if not draft_code:
                draft_code = response.text.strip()
            
            # Validate
            is_valid, msg = validate_code_syntax(file_path, draft_code)
            if not is_valid:
                print(f"⚠️  Validation failed: {msg}")
                # Try using raw response
                draft_code = response.text.strip()
                is_valid, msg = validate_code_syntax(file_path, draft_code)
                if not is_valid:
                    print(f"❌ Still invalid, skipping {file_path}")
                    continue
            
            implemented_files[file_path] = draft_code
            print(f"✅ Implemented {file_path}")
            
        except Exception as e:
            print(f"❌ Implementation failed for {file_path}: {e}")
            continue
    
    if not implemented_files:
        print("❌ No files were successfully implemented")
        return
    
    # === STAGE 4: REVIEW (OPTIONAL - Skip to save API calls) ===
    print("\n" + "="*60)
    print("💾 STAGE 4: Applying Changes (Skipping review to save API quota)")
    print("="*60)
    
    final_files = implemented_files  # Use draft directly
    
    # === STAGE 5: APPLY CHANGES ===
    for file_path, final_code in final_files.items():
        full_path = os.path.join(temp_dir, file_path)
        try:
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, 'w', encoding='utf-8') as f:
                f.write(final_code)
            print(f"✅ Applied: {file_path}")
        except Exception as e:
            print(f"❌ Failed to write {file_path}: {e}")
    
    # === STAGE 6: GIT OPERATIONS ===
    print("\n" + "="*60)
    print("📤 STAGE 5: Committing and Pushing")
    print("="*60)
    
    new_branch = f"fix/issue-{issue_context['number']}-ai"
    
    try:
        subprocess.run(['git', 'config', 'user.email', f'{GITHUB_USERNAME}@users.noreply.github.com'], 
                      cwd=temp_dir, check=True, timeout=30)
        subprocess.run(['git', 'config', 'user.name', GITHUB_USERNAME], 
                      cwd=temp_dir, check=True, timeout=30)
        subprocess.run(['git', 'checkout', '-b', new_branch], 
                      cwd=temp_dir, check=True, timeout=30)
        subprocess.run(['git', 'add', '.'], 
                      cwd=temp_dir, check=True, timeout=30)
        
        # Check for changes
        status = subprocess.run(['git', 'status', '--porcelain'], 
                               cwd=temp_dir, capture_output=True, text=True, timeout=30)
        if not status.stdout.strip():
            print("⚠️  No changes detected. Aborting.")
            return
        
        # Commit
        commit_msg = f"""fix: resolve issue #{issue_context['number']}

{issue_context['title'][:100]}

Changes:
{chr(10).join(f'- Modified {fp}' for fp in final_files.keys())}

Fixes #{issue_context['number']}"""
        
        subprocess.run(['git', 'commit', '-m', commit_msg], 
                      cwd=temp_dir, check=True, timeout=60)
        subprocess.run(['git', 'push', '-u', 'origin', new_branch, '--force'], 
                      cwd=temp_dir, check=True, timeout=120)
        print("✅ Changes pushed")
        
    except subprocess.TimeoutExpired:
        print("❌ Git operation timed out")
        return
    except subprocess.CalledProcessError as e:
        print(f"❌ Git operation failed: {e}")
        return
    
    # === STAGE 7: CREATE PULL REQUEST ===
    print("\n" + "="*60)
    print("🎯 STAGE 6: Creating Pull Request")
    print("="*60)
    
    try:
        repo_info = requests.get(f"https://api.github.com/repos/{original_repo_full_name}", 
                                headers=headers, timeout=30).json()
        base_branch = repo_info.get('default_branch', 'main')
        
        pr_body = f"""## 🤖 AI-Generated Fix for Issue #{issue_context['number']}

### Issue
{issue_context['title']}

### Changes Made
{chr(10).join(f'- `{fp}`: Modified to address the issue' for fp in final_files.keys())}

### Testing
Please review and test thoroughly before merging.

Closes #{issue_context['number']}

---
*AI-generated PR - Human review required*"""
        
        pr_data = {
            'title': f"[AI] Fix: {issue_context['title'][:60]}",
            'body': pr_body,
            'head': f"{GITHUB_USERNAME}:{new_branch}",
            'base': base_branch
        }
        
        pr_url = f"https://api.github.com/repos/{original_repo_full_name}/pulls"
        pr_response = requests.post(pr_url, headers=headers, json=pr_data, timeout=30)
        
        if pr_response.status_code in [200, 201]:
            pr_data_response = pr_response.json()
            print(f"\n🎉 SUCCESS! Pull Request Created!")
            print(f"🔗 {pr_data_response['html_url']}")
        else:
            print(f"\n❌ Failed to create PR")
            print(f"Status: {pr_response.status_code}")
            print(f"Response: {pr_response.text[:500]}")
    
    except Exception as e:
        print(f"❌ PR creation failed: {e}")
    
    finally:
        # Cleanup
        try:
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)
                print("🧹 Cleaned up temp directory")
        except Exception as e:
            print(f"⚠️  Could not clean up: {e}")


def main():
    """Main entry point"""
    if not GITHUB_TOKEN:
        print("❌ ERROR: GH_PAT environment variable not set!")
        exit(1)
    
    if not GEMINI_API_KEY:
        print("❌ ERROR: GEMINI_API_KEY environment variable not set!")
        exit(1)
    
    if GITHUB_USERNAME == "YOUR_GITHUB_USERNAME":
        print("❌ ERROR: You must set GITHUB_USERNAME!")
        exit(1)
    
    print("\n" + "="*60)
    print("🤖 AI ISSUE RESOLVER - Enhanced Edition")
    print("="*60 + "\n")
    
    issue_to_process = find_github_issues()
    
    if issue_to_process:
        try:
            process_issue(issue_to_process)
        except Exception as e:
            print(f"\n❌ Critical error: {e}")
            import traceback
            traceback.print_exc()
        finally:
            print(f"\n📝 Marking issue as processed")
            add_issue_to_processed(safe_get_string(issue_to_process, 'html_url'))
            
            # Commit processed issues log
            try:
                subprocess.run(['git', 'config', 'user.name', GITHUB_USERNAME], timeout=30)
                subprocess.run(['git', 'config', 'user.email', 
                              f'{GITHUB_USERNAME}@users.noreply.github.com'], timeout=30)
                subprocess.run(['git', 'add', PROCESSED_ISSUES_FILE], timeout=30)
                subprocess.run(['git', 'commit', '-m', 'chore: update processed issues log [skip ci]'], timeout=30)
                subprocess.run(['git', 'push'], timeout=60)
                print("✅ Processed issues log updated")
            except Exception as e:
                print(f"⚠️  Could not update log: {e}")
    else:
        print("✨ No new issues to process. Exiting.")
    
    print("\n" + "="*60)
    print("🏁 Run Complete")
    print("="*60)


if __name__ == "__main__":
    main()
