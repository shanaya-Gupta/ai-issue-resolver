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
    if not os.path.exists(PROCESSED_ISSUES_FILE): 
        return set()
    with open(PROCESSED_ISSUES_FILE, 'r') as f: 
        return set(line.strip() for line in f)

def add_issue_to_processed(issue_url):
    with open(PROCESSED_ISSUES_FILE, 'a') as f: 
        f.write(issue_url + '\n')

def find_github_issues():
    """Search for new issues with enhanced filtering"""
    print("🔍 Searching for a new issue...")
    processed_issues = get_processed_issues()
    headers = {
        'Authorization': f'token {GITHUB_TOKEN}', 
        'Accept': 'application/vnd.github.v3+json'
    }
    
    url = f"https://api.github.com/search/issues?q={SEARCH_QUERY}&sort=created&order=desc&per_page=100"
    response = requests.get(url, headers=headers)
    
    if response.status_code != 200:
        print(f"❌ Error searching for issues: {response.status_code}")
        return None
    
    items = response.json().get('items', [])
    
    # Filter out issues that are too complex or too simple
    for issue in items:
        if issue['html_url'] in processed_issues:
            continue
            
        # Skip if too many comments (likely complex or abandoned)
        if issue.get('comments', 0) > 15:
            continue
            
        # Skip if issue body is too short (likely unclear)
        if len(issue.get('body', '')) < 50:
            continue
            
        print(f"✅ Found new issue: {issue['html_url']}")
        return issue
    
    print("⚠️  No new suitable issues found.")
    return None

def fork_repository(repo_full_name, headers):
    """Fork repository with better error handling"""
    print(f"🍴 Forking {repo_full_name}...")
    fork_url = f"https://api.github.com/repos/{repo_full_name}/forks"
    response = requests.post(fork_url, headers=headers)
    
    if response.status_code in [200, 201, 202]:
        print("✅ Fork created or already exists")
        time.sleep(20)  # Wait for fork to be ready
        return True
    else:
        print(f"❌ Failed to fork: {response.status_code} - {response.text}")
        return False

def get_repo_structure(temp_dir: str) -> str:
    """Generate a tree-like structure of the repository"""
    structure = []
    for root, dirs, files in os.walk(temp_dir):
        # Skip hidden directories and common ignorable paths
        dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ['node_modules', '__pycache__', 'venv', 'env']]
        
        level = root.replace(temp_dir, '').count(os.sep)
        indent = ' ' * 2 * level
        rel_root = os.path.relpath(root, temp_dir)
        if rel_root != '.':
            structure.append(f"{indent}{os.path.basename(root)}/")
        
        sub_indent = ' ' * 2 * (level + 1)
        for file in files:
            if not file.startswith('.'):
                structure.append(f"{sub_indent}{file}")
    
    return '\n'.join(structure[:500])  # Limit lines

def get_relevant_files(temp_dir: str, max_size: int = 1000000) -> Dict[str, str]:
    """Read repository files intelligently"""
    files_content = {}
    extensions = (
        '.py', '.js', '.ts', '.jsx', '.tsx', '.java', '.go', '.rs', '.c', '.cpp', 
        '.h', '.hpp', '.cs', '.rb', '.php', '.swift', '.kt', '.scala',
        '.md', '.txt', '.yaml', '.yml', '.json', '.toml', '.ini', '.cfg',
        '.html', '.css', '.scss', '.less', '.vue', '.sql'
    )
    
    total_size = 0
    
    # Prioritize certain files
    priority_files = ['README.md', 'CONTRIBUTING.md', 'setup.py', 'package.json', 'requirements.txt']
    
    all_files = []
    for root, _, files in os.walk(temp_dir):
        if any(skip in root for skip in ['.git', 'node_modules', '__pycache__', 'venv', '.env']):
            continue
        for file in files:
            if file.endswith(extensions):
                full_path = os.path.join(root, file)
                rel_path = os.path.relpath(full_path, temp_dir)
                priority = 0 if file in priority_files else 1
                all_files.append((priority, rel_path, full_path))
    
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
    
    return files_content

def fetch_issue_context(issue, headers):
    """Fetch additional context about the issue"""
    context = {
        'comments': [],
        'labels': [label['name'] for label in issue.get('labels', [])],
        'title': issue['title'],
        'body': issue.get('body', ''),
        'number': issue['number']
    }
    
    # Fetch comments
    comments_url = issue.get('comments_url')
    if comments_url and issue.get('comments', 0) > 0:
        response = requests.get(comments_url, headers=headers)
        if response.status_code == 200:
            comments = response.json()
            context['comments'] = [
                {'author': c['user']['login'], 'body': c['body']} 
                for c in comments[:10]  # Limit to first 10 comments
            ]
    
    return context

def extract_code_from_response(response: str) -> str:
    """Robustly extract code from AI response"""
    # Try to find code blocks
    code_blocks = re.findall(r"```(?:\w+)?\n(.*?)```", response, re.DOTALL)
    if code_blocks:
        return code_blocks[-1].strip()
    
    # Remove common markdown artifacts
    cleaned = re.sub(r'^#+\s+.*$', '', response, flags=re.MULTILINE)
    cleaned = re.sub(r'\*\*.*?\*\*', '', cleaned)
    cleaned = re.sub(r'^>\s+.*$', '', cleaned, flags=re.MULTILINE)
    
    return cleaned.strip()

def validate_code_syntax(file_path: str, code: str) -> Tuple[bool, str]:
    """Basic syntax validation for different languages"""
    ext = Path(file_path).suffix
    
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
    
    # Basic checks for all files
    if not code.strip():
        return False, "Empty file"
    
    return True, "Basic validation passed"

def process_issue(issue):
    """Enhanced issue processing with multi-agent system"""
    issue_url = issue['html_url']
    original_repo_full_name = issue['repository_url'].replace('https://api.github.com/repos/', '')
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
        subprocess.run(
            ['git', 'clone', f"https://github.com/{forked_repo_full_name}.git", temp_dir],
            check=True, capture_output=True, text=True
        )
        
        print("🔄 Syncing with upstream...")
        original_repo_url = f"https://github.com/{original_repo_full_name}.git"
        subprocess.run(['git', 'remote', 'add', 'upstream', original_repo_url], cwd=temp_dir, check=True)
        subprocess.run(['git', 'fetch', 'upstream'], cwd=temp_dir, check=True)
        
        default_branch = subprocess.check_output(
            ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
            cwd=temp_dir, text=True
        ).strip()
        
        subprocess.run(['git', 'merge', f'upstream/{default_branch}'], cwd=temp_dir, check=True)
        print("✅ Repository synced")
        
    except subprocess.CalledProcessError as e:
        print(f"❌ Git setup failed: {e}")
        return
    
    # Gather repository context
    print("📚 Analyzing repository...")
    repo_structure = get_repo_structure(temp_dir)
    files_content = get_relevant_files(temp_dir)
    issue_context = fetch_issue_context(issue, headers)
    
    # === STAGE 1: DEEP ISSUE ANALYSIS ===
    print("\n" + "="*60)
    print("🧠 STAGE 1: Deep Issue Analysis (Gemini Pro)")
    print("="*60)
    
    analysis_prompt = f"""You are an expert software architect analyzing a GitHub issue.

<ISSUE>
Title: {issue_context['title']}
Number: #{issue_context['number']}
Labels: {', '.join(issue_context['labels'])}

Description:
{issue_context['body']}

Comments:
{json.dumps(issue_context['comments'], indent=2)}
</ISSUE>

<REPOSITORY_STRUCTURE>
{repo_structure}
</REPOSITORY_STRUCTURE>

Analyze this issue deeply and provide:

1. PROBLEM_TYPE: (bug/feature/documentation/refactor/test)
2. CORE_ISSUE: One clear sentence describing the actual problem
3. ROOT_CAUSE: Technical reason for the issue
4. SOLUTION_APPROACH: High-level strategy to fix it
5. AFFECTED_AREAS: Which parts of the codebase are involved
6. RISK_LEVEL: (low/medium/high) - complexity and potential for breaking changes

Respond in XML format:
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
        print(f"📊 Analysis:\n{analysis}")
        
        # Check if risk is too high
        if "RISK_LEVEL>high" in analysis:
            print("⚠️  Issue marked as high risk. Skipping for safety.")
            return
            
    except Exception as e:
        print(f"❌ Analysis failed: {e}")
        return
    
    # === STAGE 2: INTELLIGENT PLANNING ===
    print("\n" + "="*60)
    print("📋 STAGE 2: Creating Implementation Plan (Gemini Flash)")
    print("="*60)
    
    # Prepare file contents for planner
    files_context = ""
    for file_path, content in list(files_content.items())[:30]:  # Limit files
        files_context += f"\n{'='*60}\nFILE: {file_path}\n{'='*60}\n{content}\n"
    
    planner_prompt = f"""You are a principal engineer creating a detailed implementation plan.

<ANALYSIS>
{analysis}
</ANALYSIS>

<ISSUE>
{issue_context['body']}
</ISSUE>

<CODEBASE>
{files_context}
</CODEBASE>

Create a concrete implementation plan. You MUST:

1. Identify ALL files that need to be modified (not just one)
2. For EACH file, specify EXACTLY what changes are needed
3. List files in ORDER of modification (dependencies first)
4. Only suggest files that ACTUALLY EXIST in the codebase
5. Be specific about function names, variables, and line changes

Respond in XML format:
<PLAN>
<FILES_TO_MODIFY>
<FILE>
<PATH>exact/path/from/codebase.py</PATH>
<REASON>Why this file needs changes</REASON>
<CHANGES>
- Specific change 1
- Specific change 2
</CHANGES>
</FILE>
<!-- Repeat FILE tags for each file -->
</FILES_TO_MODIFY>
<TESTING_STRATEGY>How to verify the fix works</TESTING_STRATEGY>
<POTENTIAL_SIDE_EFFECTS>What could break</POTENTIAL_SIDE_EFFECTS>
</PLAN>

CRITICAL: Only use file paths that exist in the codebase provided. Do not invent paths."""

    try:
        response = model_flash.generate_content(planner_prompt)
        plan = response.text
        print(f"📝 Plan:\n{plan}")
        
        # Parse files to modify
        file_matches = re.findall(r"<FILE>.*?<PATH>(.*?)</PATH>.*?</FILE>", plan, re.DOTALL)
        if not file_matches:
            print("❌ No valid files identified in plan")
            return
            
        files_to_modify = [f.strip() for f in file_matches if f.strip() != "N/A"]
        print(f"📂 Files to modify: {files_to_modify}")
        
    except Exception as e:
        print(f"❌ Planning failed: {e}")
        return
    
    # === STAGE 3: IMPLEMENTATION ===
    print("\n" + "="*60)
    print("⚙️  STAGE 3: Implementing Changes (Gemini Flash)")
    print("="*60)
    
    implemented_files = {}
    
    for file_path in files_to_modify[:5]:  # Limit to 5 files for safety
        print(f"\n🔧 Implementing changes for: {file_path}")
        
        # Read original file
        full_path = os.path.join(temp_dir, file_path)
        if not os.path.exists(full_path):
            print(f"⚠️  File not found: {file_path}, skipping")
            continue
        
        try:
            with open(full_path, 'r', encoding='utf-8', errors='ignore') as f:
                original_content = f.read()
        except Exception as e:
            print(f"⚠️  Could not read {file_path}: {e}")
            continue
        
        implementer_prompt = f"""You are a senior engineer implementing a specific code change.

<PLAN>
{plan}
</PLAN>

<CURRENT_FILE_PATH>
{file_path}
</CURRENT_FILE_PATH>

<CURRENT_FILE_CONTENT>
{original_content}
</CURRENT_FILE_CONTENT>

Implement the changes for THIS specific file according to the plan.

CRITICAL RULES:
1. Output ONLY the complete, modified file content
2. NO markdown, NO explanations, NO comments about what you changed
3. Maintain ALL existing functionality not related to the fix
4. Keep the exact same coding style as the original
5. Preserve all imports, comments, and structure
6. Make MINIMAL changes - only what's needed for the fix

Output the complete file content now:"""

        try:
            response = model_flash.generate_content(implementer_prompt)
            draft_code = extract_code_from_response(response.text)
            
            # Validate syntax
            is_valid, validation_msg = validate_code_syntax(file_path, draft_code)
            if not is_valid:
                print(f"⚠️  Validation failed for {file_path}: {validation_msg}")
                print("Trying alternative parsing...")
                draft_code = response.text.strip()
                is_valid, validation_msg = validate_code_syntax(file_path, draft_code)
                if not is_valid:
                    print(f"❌ Still invalid, skipping {file_path}")
                    continue
            
            implemented_files[file_path] = draft_code
            print(f"✅ Implementation complete for {file_path}")
            
        except Exception as e:
            print(f"❌ Implementation failed for {file_path}: {e}")
            continue
    
    if not implemented_files:
        print("❌ No files were successfully implemented")
        return
    
    # === STAGE 4: REVIEW AND REFINEMENT ===
    print("\n" + "="*60)
    print("🔍 STAGE 4: Code Review and Refinement (Gemini Pro)")
    print("="*60)
    
    time.sleep(15)  # Rate limit
    
    final_files = {}
    
    for file_path, draft_code in implemented_files.items():
        print(f"\n🔍 Reviewing: {file_path}")
        
        critic_prompt = f"""You are a meticulous staff engineer doing code review.

<ORIGINAL_PLAN>
{plan}
</ORIGINAL_PLAN>

<FILE_PATH>
{file_path}
</FILE_PATH>

<IMPLEMENTED_CODE>
{draft_code}
</IMPLEMENTED_CODE>

Review this implementation for:
1. Correctness - Does it solve the issue?
2. Bugs - Any logical errors or edge cases?
3. Style - Does it match the original codebase style?
4. Safety - Any potential breaking changes?
5. Completeness - Is anything missing?

If the code needs improvements, output the corrected version.
If it's already good, output it as-is.

CRITICAL: Output ONLY the final source code. NO markdown. NO explanations.

Final code:"""

        try:
            response = model_pro.generate_content(critic_prompt)
            final_code = extract_code_from_response(response.text)
            
            # Final validation
            is_valid, validation_msg = validate_code_syntax(file_path, final_code)
            if not is_valid:
                print(f"⚠️  Review produced invalid code: {validation_msg}")
                print("Using draft version instead")
                final_code = draft_code
            
            final_files[file_path] = final_code
            print(f"✅ Review complete for {file_path}")
            
        except Exception as e:
            print(f"⚠️  Review failed for {file_path}: {e}, using draft")
            final_files[file_path] = draft_code
    
    # === STAGE 5: APPLY CHANGES ===
    print("\n" + "="*60)
    print("💾 STAGE 5: Applying Changes")
    print("="*60)
    
    for file_path, final_code in final_files.items():
        full_path = os.path.join(temp_dir, file_path)
        try:
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, 'w', encoding='utf-8') as f:
                f.write(final_code)
            print(f"✅ Applied changes to {file_path}")
        except Exception as e:
            print(f"❌ Failed to write {file_path}: {e}")
    
    # === STAGE 6: GIT OPERATIONS ===
    print("\n" + "="*60)
    print("📤 STAGE 6: Committing and Pushing")
    print("="*60)
    
    new_branch = f"fix/issue-{issue['number']}-ai"
    forked_repo_url_with_auth = f"https://{GITHUB_USERNAME}:{GITHUB_TOKEN}@github.com/{forked_repo_full_name}.git"
    
    try:
        subprocess.run(['git', 'config', 'user.email', f'{GITHUB_USERNAME}@users.noreply.github.com'], cwd=temp_dir, check=True)
        subprocess.run(['git', 'config', 'user.name', GITHUB_USERNAME], cwd=temp_dir, check=True)
        subprocess.run(['git', 'checkout', '-b', new_branch], cwd=temp_dir, check=True)
        subprocess.run(['git', 'add', '.'], cwd=temp_dir, check=True)
        
        # Check if there are changes
        status = subprocess.run(['git', 'status', '--porcelain'], cwd=temp_dir, capture_output=True, text=True)
        if not status.stdout.strip():
            print("⚠️  No changes detected. Aborting.")
            return
        
        # Create detailed commit message
        commit_msg = f"""fix: resolve issue #{issue['number']}

{issue['title']}

Changes:
{chr(10).join(f'- Modified {fp}' for fp in final_files.keys())}

Fixes #{issue['number']}"""
        
        subprocess.run(['git', 'commit', '-m', commit_msg], cwd=temp_dir, check=True)
        subprocess.run(['git', 'push', '-u', 'origin', new_branch, '--force'], cwd=temp_dir, check=True)
        print("✅ Changes pushed to fork")
        
    except subprocess.CalledProcessError as e:
        print(f"❌ Git operation failed: {e}")
        return
    
    # === STAGE 7: CREATE PULL REQUEST ===
    print("\n" + "="*60)
    print("🎯 STAGE 7: Creating Pull Request")
    print("="*60)
    
    repo_info = requests.get(f"https://api.github.com/repos/{original_repo_full_name}", headers=headers).json()
    base_branch = repo_info.get('default_branch', 'main')
    
    pr_body = f"""## 🤖 AI-Generated Fix for Issue #{issue['number']}

### Issue
{issue['title']}

### Changes Made
{chr(10).join(f'- `{fp}`: Modified to address the issue' for fp in final_files.keys())}

### Analysis
This PR was automatically generated by an AI assistant that:
1. Analyzed the issue and codebase structure
2. Created an implementation plan
3. Generated code changes
4. Reviewed and refined the solution

### Testing
Please review the changes carefully and test thoroughly before merging.

Closes #{issue['number']}

---
*This PR was generated automatically. Human review is required before merging.*"""
    
    pr_data = {
        'title': f"[AI] Fix: {issue['title'][:60]}...",
        'body': pr_body,
        'head': f"{GITHUB_USERNAME}:{new_branch}",
        'base': base_branch
    }
    
    pr_url = f"https://api.github.com/repos/{original_repo_full_name}/pulls"
    pr_response = requests.post(pr_url, headers=headers, json=pr_data)
    
    if pr_response.status_code in [200, 201]:
        pr_data_response = pr_response.json()
        print(f"\n🎉 SUCCESS! Pull Request Created!")
        print(f"🔗 {pr_data_response['html_url']}")
    else:
        print(f"\n❌ Failed to create PR")
        print(f"Status: {pr_response.status_code}")
        print(f"Response: {pr_response.text}")
    
    # Cleanup
    try:
        shutil.rmtree(temp_dir)
        print("🧹 Cleaned up temporary directory")
    except Exception as e:
        print(f"⚠️  Could not clean up temp directory: {e}")


if __name__ == "__main__":
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
            add_issue_to_processed(issue_to_process['html_url'])
            
            # Commit processed issues log
            try:
                subprocess.run(['git', 'config', 'user.name', GITHUB_USERNAME])
                subprocess.run(['git', 'config', 'user.email', f'{GITHUB_USERNAME}@users.noreply.github.com'])
                subprocess.run(['git', 'add', PROCESSED_ISSUES_FILE])
                subprocess.run(['git', 'commit', '-m', 'chore: update processed issues log [skip ci]'])
                subprocess.run(['git', 'push'])
                print("✅ Processed issues log updated")
            except Exception as e:
                print(f"⚠️  Could not update log: {e}")
    else:
        print("✨ No new issues to process. Exiting.")
    
    print("\n" + "="*60)
    print("🏁 Run Complete")
    print("="*60)
