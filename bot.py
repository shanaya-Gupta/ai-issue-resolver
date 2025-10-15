import os
import requests
import subprocess
import shutil
import time
import google.generativeai as genai
import re

# --- CONFIGURATION ---
GITHUB_TOKEN = os.getenv('GH_PAT')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
GITHUB_USERNAME = "shanaya-Gupta" # I've set this for you, but double-check!
SEARCH_QUERY = 'is:issue is:open label:"good first issue" language:python'
PROCESSED_ISSUES_FILE = "processed_issues.txt"

# --- Configure the Gemini AI Models for our Hybrid Team ---
genai.configure(api_key=GEMINI_API_KEY)
model_flash = genai.GenerativeModel('gemini-flash-latest')
model_pro = genai.GenerativeModel('gemini-2.5-pro')

# --- HELPER FUNCTIONS ---

def get_processed_issues():
    if not os.path.exists(PROCESSED_ISSUES_FILE): return set()
    with open(PROCESSED_ISSUES_FILE, 'r') as f: return set(line.strip() for line in f)

def add_issue_to_processed(issue_url):
    with open(PROCESSED_ISSUES_FILE, 'a') as f: f.write(issue_url + '\n')

def find_github_issues():
    print("Searching for a new issue...")
    processed_issues = get_processed_issues()
    headers = {'Authorization': f'token {GITHUB_TOKEN}', 'Accept': 'application/vnd.github.v3+json'}
    url = f"https://api.github.com/search/issues?q={SEARCH_QUERY}&sort=created&order=desc&per_page=50"
    response = requests.get(url, headers=headers)
    if response.status_code != 200:
        print(f"Error searching for issues: {response.status_code}"); return None
    items = response.json().get('items', [])
    for issue in items:
        if issue['html_url'] not in processed_issues:
            print(f"Found new issue to process: {issue['html_url']}"); return issue
    print("No new issues found to process in this run."); return None

def fork_repository(repo_full_name, headers):
    print(f"Forking {repo_full_name}...")
    fork_url = f"https://api.github.com/repos/{repo_full_name}/forks"
    response = requests.post(fork_url, headers=headers)
    if response.status_code in [200, 201, 202]:
        print("Fork request sent or fork already exists."); time.sleep(20); return True
    else:
        print(f"Failed to fork repository: {response.status_code} - {response.text}"); return False

def process_issue(issue):
    """Processes an issue using the Plan, Implement, Critique chain with a Hybrid AI Team."""
    issue_url = issue['html_url']
    original_repo_full_name = issue['repository_url'].replace('https://api.github.com/repos/', '')
    headers = {'Authorization': f'token {GITHUB_TOKEN}', 'Accept': 'application/vnd.github.v3+json'}
    
    if not fork_repository(original_repo_full_name, headers): return

    forked_repo_full_name = f"{GITHUB_USERNAME}/{original_repo_full_name.split('/')[1]}"
    temp_dir = f"temp_repo_{int(time.time())}"
    
    try:
        print(f"Cloning our fork: {forked_repo_full_name}...")
        subprocess.run(['git', 'clone', f"https://github.com/{forked_repo_full_name}.git", temp_dir], check=True, capture_output=True, text=True)
        print("Syncing fork with the original repository...")
        original_repo_url = f"https://github.com/{original_repo_full_name}.git"
        subprocess.run(['git', 'remote', 'add', 'upstream', original_repo_url], cwd=temp_dir, check=True)
        subprocess.run(['git', 'fetch', 'upstream'], cwd=temp_dir, check=True)
        default_branch = subprocess.check_output(['git', 'rev-parse', '--abbrev-ref', 'HEAD'], cwd=temp_dir, text=True).strip()
        subprocess.run(['git', 'merge', f'upstream/{default_branch}'], cwd=temp_dir, check=True)
        print("Fork is now up-to-date.")
    except (subprocess.CalledProcessError, subprocess.SubprocessError) as e:
        print(f"Failed during git setup: {e}"); return

    print("Reading files for context...")
    context = ""
    for root, _, files in os.walk(temp_dir):
        if '.git' in root: continue
        for file in files:
            if file.endswith(('.py', '.js', '.ts', '.md', '.txt', '.html', '.css', '.yaml')):
                try:
                    with open(os.path.join(root, file), 'r', encoding='utf-8', errors='ignore') as f:
                        context += f"\n--- FILE: {os.path.relpath(os.path.join(root, file), temp_dir)} ---\n" + f.read()
                except Exception: pass

    # --- AGENT 1: THE PLANNER (using FLASH) ---
    print("\n--- Stage 1: Planning (using Gemini Flash) ---")
    # --- NEW: Stricter prompt to prevent hallucination ---
    planner_prompt = f"""
    You are a principal software engineer. Analyze the following issue and codebase to create a robust implementation plan.
    
    <ISSUE_INFO>
        <TITLE>{issue['title']}</TITLE>
        <DESCRIPTION>{issue['body']}</DESCRIPTION>
    </ISSUE_INFO>

    <CODEBASE>
    {context[:400000]} 
    </CODEBASE>

    Your task is to produce a plan.
    1.  From the files provided in the `<CODEBASE>`, you MUST select the single most relevant file path to modify.
    2.  Do NOT invent or hypothesize a file path. If you cannot identify a suitable file from the provided context, you MUST respond with `<PRIMARY_FILE>N/A</PRIMARY_FILE>`.
    3.  Create a step-by-step strategy for the fix.
    
    Respond in the following XML format:
    <PLAN>
        <PRIMARY_FILE>path/to/relevant/file.py</PRIMARY_FILE>
        <STRATEGY>1. Step one...</STRATEGY>
    </PLAN>
    """
    try:
        response = model_flash.generate_content(planner_prompt)
        plan_text = response.text
        print(f"Generated Plan:\n{plan_text}")
        primary_file_match = re.search(r"<PRIMARY_FILE>(.*?)</PRIMARY_FILE>", plan_text, re.DOTALL)
        if not primary_file_match:
            print("Planner failed: Could not parse plan response."); return
        
        file_to_change = primary_file_match.group(1).strip()
        # --- NEW: Handle hallucination gracefully ---
        if file_to_change == "N/A":
            print("Planner could not identify a relevant file. Skipping issue."); return
            
    except Exception as e:
        print(f"Planner failed: {e}"); return
        
    # --- AGENT 2: THE IMPLEMENTER (using FLASH) ---
    print(f"\n--- Stage 2: Implementation for {file_to_change} (using Gemini Flash) ---")
    original_file_content = ""
    try:
        with open(os.path.join(temp_dir, file_to_change), 'r', encoding='utf-8', errors='ignore') as f:
            original_file_content = f.read()
    except FileNotFoundError:
        print(f"Could not find the file '{file_to_change}' identified by the Planner. The AI may have still hallucinated."); return

    implementer_prompt = f"""You are a senior software engineer. Implement the code change for a single file based on the provided plan and the original file content. <PLAN>{plan_text}</PLAN><ORIGINAL_FILE_CONTENT for `{file_to_change}`>{original_file_content}</ORIGINAL_FILE_CONTENT>Provide ONLY the full, complete, rewritten content of the file `{file_to_change}`. Do not add any other text or explanation."""
    try:
        response = model_flash.generate_content(implementer_prompt)
        first_draft_code = response.text.strip()
        if first_draft_code.startswith("```"):
            first_draft_code = re.search(r"```(?:\w+)?\n(.*?)\n?```", first_draft_code, re.DOTALL).group(1).strip()
    except Exception as e:
        print(f"Implementer failed: {e}"); return
    
    # --- AGENT 3: THE CRITIC (using PRO) ---
    print("\n--- Stage 3: Self-Correction and Critique (using Gemini Pro) ---")
    critic_prompt = f"""You are a 40-year experienced staff engineer, known for your meticulous code reviews. Analyze the proposed code change. Critique the first draft and provide a final, improved, production-ready version. <PLAN>{plan_text}</PLAN><FIRST_DRAFT_CODE for `{file_to_change}`>{first_draft_code}</FIRST_DRAFT_CODE>Your task is to return the final, improved code. Think about edge cases, style, robustness, and potential side effects. Provide ONLY the final, complete, rewritten content of the file `{file_to_change}`."""
    try:
        response = model_pro.generate_content(critic_prompt)
        final_code = response.text.strip()
        if final_code.startswith("```"):
            final_code = re.search(r"```(?:\w+)?\n(.*?)\n?```", final_code, re.DOTALL).group(1).strip()
    except Exception as e:
        print(f"Critic failed: {e}"); return
    
    # --- APPLY THE FINAL, CRITIQUED CODE ---
    print(f"Applying final, reviewed fix to file: {file_to_change}")
    full_path_to_file = os.path.join(temp_dir, file_to_change)
    try:
        os.makedirs(os.path.dirname(full_path_to_file), exist_ok=True)
        with open(full_path_to_file, 'w', encoding='utf-8') as f: f.write(final_code)
        print("File overwritten successfully.")
    except FileNotFoundError:
        print(f"Error: AI specified a file path that does not exist: {file_to_change}"); return

    # --- GIT OPERATIONS ---
    new_branch = f"fix/ai-{issue['number']}"
    print(f"\nPushing to branch: {new_branch}")
    forked_repo_url_with_auth = f"https://{GITHUB_USERNAME}:{GITHUB_TOKEN}@github.com/{forked_repo_full_name}.git"
    try:
        subprocess.run(['git', 'config', 'user.email', f'{GITHUB_USERNAME}@users.noreply.github.com'], cwd=temp_dir, check=True)
        subprocess.run(['git', 'config', 'user.name', GITHUB_USERNAME], cwd=temp_dir, check=True)
        subprocess.run(['git', 'checkout', '-b', new_branch], cwd=temp_dir, check=True)
        subprocess.run(['git', 'add', '.'], cwd=temp_dir, check=True)
        status = subprocess.run(['git', 'status', '--porcelain'], cwd=temp_dir, capture_output=True, text=True)
        if not status.stdout.strip():
            print("AI rewrite resulted in no changes. Aborting."); return
        subprocess.run(['git', 'commit', '-m', f"fix: Resolve issue #{issue['number']}"], cwd=temp_dir, check=True)
        subprocess.run(['git', 'remote', 'set-url', 'origin', forked_repo_url_with_auth], cwd=temp_dir, check=True)
        subprocess.run(['git', 'push', '-u', 'origin', new_branch, '--force'], cwd=temp_dir, check=True)
        print("Code pushed to our fork on GitHub.")
    except subprocess.CalledProcessError as e:
        print(f"Git command failed: {e.cmd}\nStderr: {e.stderr}"); return

    # --- CREATE PULL REQUEST ---
    print("Creating Pull Request...")
    repo_info = requests.get(f"https://api.github.com/repos/{original_repo_full_name}", headers=headers).json()
    base_branch = repo_info.get('default_branch', 'main')
    pr_data = { 'title': f"AI Fix for: {issue['title']}", 'body': f"Resolves #{issue['number']}.", 'head': f"{GITHUB_USERNAME}:{new_branch}", 'base': base_branch }
    pr_url = f"https://api.github.com/repos/{original_repo_full_name}/pulls"
    pr_response = requests.post(pr_url, headers=headers, json=pr_data)
    if pr_response.status_code in [200, 201]:
        print(f"SUCCESS! PR created: {pr_response.json()['html_url']}")
    else:
        print(f"Failed to create PR. Status: {pr_response.status_code}\nResponse: {pr_response.text}")


if __name__ == "__main__":
    if GITHUB_USERNAME == "YOUR_GITHUB_USERNAME":
        print("ERROR: You must change GITHUB_USERNAME in bot.py!"); exit(1)
    
    print("Bot starting a new run...")
    issue_to_process = find_github_issues()
    
    if issue_to_process:
        try:
            process_issue(issue_to_process)
        finally:
            print(f"Adding {issue_to_process['html_url']} to processed list.")
            add_issue_to_processed(issue_to_process['html_url'])
            try:
                subprocess.run(['git', 'config', 'user.name', GITHUB_USERNAME])
                subprocess.run(['git', 'config', 'user.email', f'{GITHUB_USERNAME}@users.noreply.github.com'])
                subprocess.run(['git', 'add', PROCESSED_ISSUES_FILE])
                subprocess.run(['git', 'commit', '-m', 'Update processed issues log'])
                subprocess.run(['git', 'push'])
            except Exception as e:
                print(f"Could not commit the processed issues file. Error: {e}")
    else:
        print("No new issues to process. Run finished.")
