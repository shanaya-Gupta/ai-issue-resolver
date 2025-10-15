import os
import requests
import subprocess
import shutil
import time
import google.generativeai as genai
import re
import json
from pathlib import Path

# --- CONFIGURATION ---
GITHUB_TOKEN = os.getenv('GH_PAT')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
GITHUB_USERNAME = "shanaya-Gupta" # I've set this for you, but double-check!
SEARCH_QUERY = 'is:issue is:open label:"good first issue"'
PROCESSED_ISSUES_FILE = "processed_issues.txt"

# --- Configure the Gemini AI Models for our Hybrid Team ---
genai.configure(api_key=GEMINI_API_KEY)
model_flash = genai.GenerativeModel('gemini-2.5-flash')
model_pro = genai.GenerativeModel('gemini-2.5-flash')

# --- HELPER FUNCTIONS ---

def get_processed_issues():
    if not os.path.exists(PROCESSED_ISSUES_FILE): return set()
    with open(PROCESSED_ISSUES_FILE, 'r') as f: return set(line.strip() for line in f)

def add_issue_to_processed(issue_url):
    with open(PROCESSED_ISSUES_FILE, 'a') as f: f.write(issue_url + '\n')

def find_github_issues():
    print("🔍 Searching for a new, high-quality issue...")
    processed_issues = get_processed_issues()
    headers = {'Authorization': f'token {GITHUB_TOKEN}', 'Accept': 'application/vnd.github.v3+json'}
    url = f"https://api.github.com/search/issues?q={SEARCH_QUERY}&sort=created&order=desc&per_page=100"
    try:
        response = requests.get(url, headers=headers, timeout=30)
        if response.status_code != 200:
            print(f"❌ Error searching for issues: {response.status_code}"); return None
        items = response.json().get('items', [])
        for issue in items:
            issue_url = issue.get('html_url')
            if issue_url in processed_issues: continue
            if issue.get('comments', 0) > 10: continue
            body = issue.get('body') or ""
            if len(body) < 100: continue
            print(f"✅ Found new issue: {issue_url}"); return issue
    except Exception as e:
        print(f"❌ Exception during issue search: {e}"); return None
    print("🤷 No new suitable issues found."); return None

def fork_repository(repo_full_name, headers):
    print(f"🍴 Forking {repo_full_name}...")
    fork_url = f"https://api.github.com/repos/{repo_full_name}/forks"
    try:
        response = requests.post(fork_url, headers=headers, timeout=30)
        if response.status_code in [200, 201, 202]:
            print("✅ Fork request sent or fork already exists."); time.sleep(20); return True
        else:
            print(f"❌ Failed to fork: {response.status_code} - {response.text[:100]}"); return False
    except Exception as e:
        print(f"❌ Exception during fork: {e}"); return False

def get_repo_context(temp_dir: str):
    """Gathers intelligent context (structure and key files) from the repo."""
    print("📚 Analyzing repository structure and key files...")
    context = {"structure": "Could not generate repository structure.", "files": {}}
    try:
        structure = []
        for root, dirs, files in os.walk(temp_dir):
            dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ['node_modules', '__pycache__', 'venv', 'dist', 'build']]
            level = root.replace(temp_dir, '').count(os.sep)
            indent = ' ' * 2 * level
            if os.path.basename(root) != temp_dir:
                structure.append(f"{indent}{os.path.basename(root)}/")
            sub_indent = ' ' * 2 * (level + 1)
            for f in files[:20]:
                if not f.startswith('.'): structure.append(f"{sub_indent}{f}")
            if len(structure) > 300: break
        context["structure"] = '\n'.join(structure)

        extensions = ('.py', '.js', '.ts', '.md', '.txt', '.html', '.css', '.yaml', '.yml', '.json', '.go', '.rs', '.java')
        priority_files = ['README.md', 'CONTRIBUTING.md', 'package.json', 'requirements.txt']
        total_size = 0
        all_files = list(Path(temp_dir).rglob("*.*"))
        all_files.sort(key=lambda x: 0 if x.name in priority_files else 1)
        for full_path in all_files:
            if any(part.startswith('.') for part in full_path.parts): continue
            if full_path.suffix in extensions:
                rel_path = str(full_path.relative_to(temp_dir))
                try:
                    content = full_path.read_text(encoding='utf-8', errors='ignore')
                    if len(content) > 30000: content = content[:30000] + "\n... [TRUNCATED] ..."
                    context["files"][rel_path] = content
                    total_size += len(content)
                    if total_size > 500000: return context
                except Exception: pass
    except Exception as e:
        print(f"⚠️ Error gathering repo context: {e}")
    return context

def process_issue(issue):
    """Processes an issue using the enhanced multi-agent system."""
    issue_url = issue.get('html_url')
    original_repo_full_name = issue['repository_url'].replace('https://api.github.com/repos/', '')
    headers = {'Authorization': f'token {GITHUB_TOKEN}', 'Accept': 'application/vnd.github.v3+json'}
    
    if not fork_repository(original_repo_full_name, headers): return

    forked_repo_full_name = f"{GITHUB_USERNAME}/{original_repo_full_name.split('/')[1]}"
    temp_dir = f"temp_repo_{int(time.time())}"
    
    try:
        print(f"📥 Cloning our fork: {forked_repo_full_name}...")
        subprocess.run(['git', 'clone', '--depth', '1', f"https://github.com/{forked_repo_full_name}.git", temp_dir], check=True, capture_output=True, text=True, timeout=120)
        print("🔄 Syncing with upstream...")
        original_repo_url = f"https://github.com/{original_repo_full_name}.git"
        subprocess.run(['git', 'remote', 'add', 'upstream', original_repo_url], cwd=temp_dir, check=True, timeout=30)
        subprocess.run(['git', 'fetch', 'upstream', '--depth', '1'], cwd=temp_dir, check=True, timeout=120)
        default_branch = subprocess.check_output(['git', 'rev-parse', '--abbrev-ref', 'HEAD'], cwd=temp_dir, text=True, timeout=30).strip()
        subprocess.run(['git', 'merge', f'upstream/{default_branch}'], cwd=temp_dir, check=True, timeout=60)
        print("✅ Repository synced")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"❌ Git setup failed: {e}"); shutil.rmtree(temp_dir, ignore_errors=True); return
    
    repo_context = get_repo_context(temp_dir)

    # --- NEW: AGENT 0 - THE TASK CLASSIFIER (FLASH) ---
    print("\n--- 🤔 Stage 0: Task Classification (Gemini Flash) ---")
    classifier_prompt = f"""You are a task classifier. Analyze the issue title and description to determine the type of task.
    <ISSUE>
    <TITLE>{issue.get('title')}</TITLE>
    <DESCRIPTION>{issue.get('body', '')[:1000]}</DESCRIPTION>
    </ISSUE>
    What is the primary intent of this task? Respond with one of these exact words: REWRITE, APPEND, REFACTOR, UNKNOWN.
    - REWRITE: For fixing a bug in existing code.
    - APPEND: For adding new content to a documentation file (like README.md).
    - REFACTOR: For improving or cleaning up existing code without changing functionality.
    - UNKNOWN: If the intent is unclear.
    """
    try:
        response = model_flash.generate_content(classifier_prompt)
        task_type = response.text.strip().upper()
        if task_type not in ["REWRITE", "APPEND", "REFACTOR"]: task_type = "REWRITE" # Default to rewrite
        print(f"✅ Task classified as: {task_type}")
    except Exception as e:
        print(f"⚠️ Classifier failed: {e}. Defaulting to REWRITE mode."); task_type = "REWRITE"
        
    # --- AGENT 1: THE PLANNER (FLASH) ---
    print("\n--- 🧠 Stage 1: Planning (Gemini Flash) ---")
    planner_prompt = f"""You are a principal engineer. Analyze the issue and codebase to create a plan.
    <ISSUE><TITLE>{issue.get('title')}</TITLE><DESCRIPTION>{issue.get('body', '')[:2000]}</DESCRIPTION></ISSUE>
    <REPOSITORY_STRUCTURE>{repo_context['structure']}</REPOSITORY_STRUCTURE>
    Your task is to produce a plan.
    1. From the files in the structure, you MUST select the single most relevant file path to modify.
    2. Do NOT invent a file path. If no file seems relevant, respond with `<PRIMARY_FILE>N/A</PRIMARY_FILE>`.
    Respond in XML format: <PLAN><PRIMARY_FILE>path/to/file.ext</PRIMARY_FILE><STRATEGY>1. Step one...</STRATEGY></PLAN>"""
    try:
        response = model_flash.generate_content(planner_prompt)
        plan_text = response.text
        print(f"📝 Generated Plan:\n{plan_text}")
        primary_file_match = re.search(r"<PRIMARY_FILE>(.*?)</PRIMARY_FILE>", plan_text, re.DOTALL)
        if not primary_file_match:
            print("❌ Planner failed: Could not parse plan response."); shutil.rmtree(temp_dir, ignore_errors=True); return
        file_to_change = primary_file_match.group(1).strip()
        if file_to_change == "N/A":
            print("🤷 Planner could not identify a relevant file. Skipping."); shutil.rmtree(temp_dir, ignore_errors=True); return
    except Exception as e:
        print(f"❌ Planner failed: {e}"); shutil.rmtree(temp_dir, ignore_errors=True); return
        
    # --- AGENT 2: THE IMPLEMENTER (FLASH) ---
    print(f"\n--- ⚙️  Stage 2: Implementation for {file_to_change} (Gemini Flash) in {task_type} mode ---")
    original_file_content = repo_context['files'].get(file_to_change)
    if original_file_content is None:
        print(f"❌ Critical Error: Planner identified file '{file_to_change}' but it was not found in context."); shutil.rmtree(temp_dir, ignore_errors=True); return

    # --- NEW: Mode-based Implementation Prompt ---
    if task_type == "APPEND":
        implementer_prompt = f"""You are a technical writer. The task is to ADD content to the documentation file `{file_to_change}`.
        <PLAN>{plan_text}</PLAN>
        <EXISTING_FILE_CONTENT>{original_file_content}</EXISTING_FILE_CONTENT>
        Your task is to generate ONLY the new section/content that needs to be appended to the file. Do not repeat the existing content.
        Provide ONLY the raw new content to be added. Do not use markdown.
        """
    else: # REWRITE or REFACTOR
        implementer_prompt = f"""You are a senior engineer. Implement the code change for `{file_to_change}` based on the plan.
        <PLAN>{plan_text}</PLAN>
        <ORIGINAL_FILE_CONTENT>{original_file_content}</ORIGINAL_FILE_CONTENT>
        Provide ONLY the full, complete, rewritten content of the file. Do not add any explanation."""
        
    try:
        response = model_flash.generate_content(implementer_prompt)
        first_draft_code = response.text.strip()
    except Exception as e:
        print(f"❌ Implementer failed: {e}"); shutil.rmtree(temp_dir, ignore_errors=True); return
    
    # --- AGENT 3: THE CRITIC (PRO) ---
    print("\n--- 🧐 Stage 3: Critique (Gemini Pro) ---")
    print("⏳ Waiting 15s to respect API rate limits...")
    time.sleep(15)
    
    if task_type == "APPEND":
         critic_prompt = f"""You are a meticulous editor. Review the proposed text to be appended.
         <PLAN>{plan_text}</PLAN>
         <TEXT_TO_APPEND>{first_draft_code}</TEXT_TO_APPEND>
         Your task is to refine the text for clarity and accuracy.
         Your entire response MUST be only the raw, final text to be appended. DO NOT use markdown.
         """
    else:
        critic_prompt = f"""You are a meticulous staff engineer. Refine the provided code draft.
        <PLAN>{plan_text}</PLAN>
        <FIRST_DRAFT_CODE for `{file_to_change}`>{first_draft_code}</FIRST_DRAFT_CODE>
        Your entire response will be written directly to `{file_to_change}`. It MUST NOT contain anything other than the raw, final source code.
        DO NOT use markdown. DO NOT add explanations.
        """
    try:
        response = model_pro.generate_content(critic_prompt)
        final_code_response = response.text.strip()
        code_blocks = re.findall(r"```(?:\w+)?\n(.*?)```", final_code_response, re.DOTALL)
        final_code = code_blocks[-1].strip() if code_blocks else final_code_response
    except Exception as e:
        print(f"❌ Critic failed: {e}"); shutil.rmtree(temp_dir, ignore_errors=True); return
        
    # --- APPLY THE FINAL, CRITIQUED CODE ---
    full_path_to_file = Path(temp_dir) / file_to_change
    try:
        # --- NEW: Apply based on task type ---
        if task_type == "APPEND":
            print(f"✅ Applying fix by APPENDING to file: {file_to_change}")
            with full_path_to_file.open("a", encoding="utf-8") as f:
                f.write("\n\n" + final_code)
        else: # REWRITE or REFACTOR
            print(f"✅ Applying fix by REWRITING file: {file_to_change}")
            full_path_to_file.write_text(final_code, encoding='utf-8')
        print("💾 File updated successfully.")
    except Exception as e:
        print(f"❌ Failed to write file: {e}"); shutil.rmtree(temp_dir, ignore_errors=True); return

    # --- GIT OPERATIONS ---
    new_branch = f"fix/ai-{issue['number']}"
    print(f"\n--- 🚀 Stage 4: Pushing to branch: {new_branch} ---")
    forked_repo_url_with_auth = f"https://{GITHUB_USERNAME}:{GITHUB_TOKEN}@github.com/{forked_repo_full_name}.git"
    try:
        # Standard Git operations
        subprocess.run(['git', 'config', 'user.email', f'{GITHUB_USERNAME}@users.noreply.github.com'], cwd=temp_dir, check=True)
        subprocess.run(['git', 'config', 'user.name', GITHUB_USERNAME], cwd=temp_dir, check=True)
        subprocess.run(['git', 'checkout', '-b', new_branch], cwd=temp_dir, check=True)
        subprocess.run(['git', 'add', '.'], cwd=temp_dir, check=True)
        status = subprocess.run(['git', 'status', '--porcelain'], cwd=temp_dir, capture_output=True, text=True)
        if not status.stdout.strip():
            print("🤷 No changes detected after applying fix. Aborting."); shutil.rmtree(temp_dir, ignore_errors=True); return
        commit_msg = f"feat: Resolve issue #{issue['number']}\n\n{issue['title']}"
        subprocess.run(['git', 'commit', '-m', commit_msg], cwd=temp_dir, check=True)
        subprocess.run(['git', 'remote', 'set-url', 'origin', forked_repo_url_with_auth], cwd=temp_dir, check=True)
        subprocess.run(['git', 'push', '-u', 'origin', new_branch, '--force'], cwd=temp_dir, check=True)
        print("✅ Code pushed to our fork on GitHub.")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"❌ Git push failed: {e}"); shutil.rmtree(temp_dir, ignore_errors=True); return

    # --- CREATE PULL REQUEST ---
    print("\n--- 📬 Stage 5: Creating Pull Request ---")
    repo_info = requests.get(f"https://api.github.com/repos/{original_repo_full_name}", headers=headers).json()
    base_branch = repo_info.get('default_branch', 'main')
    pr_body = f"### Fix for Issue #{issue['number']}\n\n**Issue:** {issue['title']}\n\nThis PR attempts to resolve the issue by modifying the following file(s):\n- `{file_to_change}`\n\n* Please review thoroughly.*"
    pr_data = { 'title': f"[AI] feat: {issue['title']}", 'body': pr_body, 'head': f"{GITHUB_USERNAME}:{new_branch}", 'base': base_branch }
    pr_url = f"https://api.github.com/repos/{original_repo_full_name}/pulls"
    pr_response = requests.post(pr_url, headers=headers, json=pr_data)
    if pr_response.status_code in [200, 201]:
        print(f"🎉 SUCCESS! PR created: {pr_response.json()['html_url']}")
    else:
        print(f"❌ Failed to create PR: {pr_response.status_code}\n{pr_response.text[:200]}")
    
    shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    if GITHUB_USERNAME == "YOUR_GITHUB_USERNAME" or not GITHUB_USERNAME:
        print("❌ ERROR: GITHUB_USERNAME is not set correctly!"); exit(1)
    
    print("\n" + "="*50 + "\n🤖 AI Professional Agent Initialized\n" + "="*50)
    issue_to_process = find_github_issues()
    if issue_to_process:
        try:
            process_issue(issue_to_process)
        finally:
            print(f"📝 Adding {issue_to_process.get('html_url')} to processed list.")
            add_issue_to_processed(issue_to_process.get('html_url'))
            try:
                subprocess.run(['git', 'config', 'user.name', GITHUB_USERNAME])
                subprocess.run(['git', 'config', 'user.email', f'{GITHUB_USERNAME}@users.noreply.github.com'])
                subprocess.run(['git', 'add', PROCESSED_ISSUES_FILE])
                if subprocess.run(['git', 'status', '--porcelain'], capture_output=True, text=True).stdout:
                    subprocess.run(['git', 'commit', '-m', 'chore: update processed issues log [skip ci]'], check=True)
                    subprocess.run(['git', 'push'], check=True)
                print("✅ Processed issues log updated and pushed.")
            except Exception as e:
                print(f"⚠️ Could not commit the processed issues file: {e}")
    else:
        print("🏁 No new issues to process. Run finished.")
