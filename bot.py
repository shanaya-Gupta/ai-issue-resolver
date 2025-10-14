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

# --- Configure the Gemini AI Model ---
# We need the smartest model to get high-quality code. Our new parser can handle its chattiness.
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-2.5-pro')

# --- HELPER FUNCTIONS ---

def get_processed_issues():
    """Reads the list of processed issue URLs from the memory file."""
    if not os.path.exists(PROCESSED_ISSUES_FILE):
        return set()
    with open(PROCESSED_ISSUES_FILE, 'r') as f:
        return set(line.strip() for line in f)

def add_issue_to_processed(issue_url):
    """Adds a new issue URL to the memory file."""
    with open(PROCESSED_ISSUES_FILE, 'a') as f:
        f.write(issue_url + '\n')

def find_github_issues():
    """Finds a new, unprocessed GitHub issue."""
    print("Searching for a new issue...")
    processed_issues = get_processed_issues()
    print(f"Loaded {len(processed_issues)} previously processed issues.")
    
    headers = {'Authorization': f'token {GITHUB_TOKEN}', 'Accept': 'application/vnd.github.v3+json'}
    url = f"https://api.github.com/search/issues?q={SEARCH_QUERY}&sort=created&order=desc&per_page=50"
    
    response = requests.get(url, headers=headers)
    if response.status_code != 200:
        print(f"Error searching for issues: {response.status_code}")
        return None
        
    items = response.json().get('items', [])
    for issue in items:
        if issue['html_url'] not in processed_issues:
            print(f"Found new issue to process: {issue['html_url']}")
            return issue
            
    print("No new issues found to process in this run.")
    return None

def fork_repository(repo_full_name, headers):
    """Forks the repository to the authenticated user's account."""
    print(f"Forking {repo_full_name}...")
    fork_url = f"https://api.github.com/repos/{repo_full_name}/forks"
    response = requests.post(fork_url, headers=headers)
    if response.status_code in [200, 201, 202]:
        print("Fork request sent or fork already exists.")
        time.sleep(20) # Give GitHub more time to complete the fork
        return True
    else:
        print(f"Failed to fork repository: {response.status_code} - {response.text}")
        return False

def process_issue(issue):
    """The main logic for processing a single issue."""
    issue_url = issue['html_url']
    original_repo_full_name = issue['repository_url'].replace('https://api.github.com/repos/', '')
    
    headers = {'Authorization': f'token {GITHUB_TOKEN}', 'Accept': 'application/vnd.github.v3+json'}
    
    if not fork_repository(original_repo_full_name, headers):
        return

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
        print(f"Failed during git setup: {e}")
        return

    print("Reading files for context...")
    context = ""
    for root, _, files in os.walk(temp_dir):
        if '.git' in root: continue
        for file in files:
            if file.endswith(('.py', '.js', '.ts', '.md', '.txt', '.html', '.css')):
                try:
                    with open(os.path.join(root, file), 'r', encoding='utf-8', errors='ignore') as f:
                        context += f"\n--- FILE: {os.path.relpath(os.path.join(root, file), temp_dir)} ---\n" + f.read()
                except Exception: pass

    # --- NEW: Using the Ironclad Prompt ---
    prompt = f"""
    You are a laconic, expert AI programmer. Your task is to fix a GitHub issue by rewriting a single file.

    <ISSUE_INFO>
        <TITLE>{issue['title']}</TITLE>
        <DESCRIPTION>{issue['body']}</DESCRIPTION>
    </ISSUE_INFO>

    <CODEBASE>
    {context[:500000]}
    </CODEBASE>

    <INSTRUCTIONS>
    1.  Analyze the issue and codebase to understand the required change.
    2.  Identify the single file that needs to be modified.
    3.  Rewrite the complete, full content of this file with the fix applied.
    4.  Your solution MUST be high-quality and production-ready. Do NOT remove major functionality. Do NOT introduce breaking changes like circular imports or NameErrors.
    5.  Your response MUST contain ONLY the file path and the code. DO NOT add any explanation, apology, or any other text.
    </INSTRUCTIONS>

    <OUTPUT_FORMAT>
    <FILE_PATH>path/to/your/file.py</FILE_PATH>
    <CODE>
    ```python
    # Full content of the fixed file goes here
    ```
    </CODE>
    </OUTPUT_FORMAT>
    """

    print("Asking Gemini Pro for a high-quality fix...")
    try:
        response = model.generate_content(prompt)
        raw_response = response.text

        # --- NEW: Using the Flexible Parser ---
        match = re.search(r"<FILE_PATH>(.*?)</FILE_PATH>.*?<CODE>\n?```(?:\w+)?\n(.*?)\n?```\n?</CODE>", raw_response, re.DOTALL)
        
        if not match:
            print("Could not parse Gemini's response. The format was incorrect or the AI was too chatty.")
            print("--- RAW GEMINI OUTPUT (PREVIEW) ---")
            print(raw_response[:500])
            print("---------------------------------")
            return

        file_to_change = match.group(1).strip()
        new_code_content = match.group(2).strip()

    except Exception as e:
        print(f"Error from Gemini: {e}")
        return

    print(f"Applying fix by overwriting file: {file_to_change}")
    full_path_to_file = os.path.join(temp_dir, file_to_change)
    try:
        os.makedirs(os.path.dirname(full_path_to_file), exist_ok=True)
        with open(full_path_to_file, 'w', encoding='utf-f8') as f: f.write(new_code_content)
        print("File overwritten successfully.")
    except FileNotFoundError:
        print(f"Error: Gemini specified a file path that does not exist: {file_to_change}")
        return

    # --- The rest is the same: commit, push, and PR ---
    new_branch = f"fix/ai-{issue['number']}"
    print(f"Pushing to our fork's branch: {new_branch}")
    forked_repo_url_with_auth = f"https://{GITHUB_USERNAME}:{GITHUB_TOKEN}@github.com/{forked_repo_full_name}.git"
    
    try:
        # Standard Git operations
        subprocess.run(['git', 'config', 'user.email', f'{GITHUB_USERNAME}@users.noreply.github.com'], cwd=temp_dir, check=True)
        subprocess.run(['git', 'config', 'user.name', GITHUB_USERNAME], cwd=temp_dir, check=True)
        subprocess.run(['git', 'checkout', '-b', new_branch], cwd=temp_dir, check=True)
        subprocess.run(['git', 'add', '.'], cwd=temp_dir, check=True)
        status = subprocess.run(['git', 'status', '--porcelain'], cwd=temp_dir, capture_output=True, text=True)
        if not status.stdout.strip():
            print("AI rewrite resulted in no changes. Aborting.")
            return
        subprocess.run(['git', 'commit', '-m', f"fix: Resolve issue #{issue['number']}"], cwd=temp_dir, check=True)
        subprocess.run(['git', 'remote', 'set-url', 'origin', forked_repo_url_with_auth], cwd=temp_dir, check=True)
        subprocess.run(['git', 'push', '-u', 'origin', new_branch, '--force'], cwd=temp_dir, check=True)
        print("Code pushed to our fork on GitHub.")
    except subprocess.CalledProcessError as e:
        print(f"Git command failed: {e.cmd}\nStderr: {e.stderr}")
        return

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
        print("ERROR: You must change GITHUB_USERNAME in bot.py!")
        exit(1)
    
    # We will add the memory part back later. Let's focus on quality first.
    print("Bot starting a new run...")
    issue_to_process = find_github_issues()
    
    if issue_to_process:
        try:
            process_issue(issue_to_process)
        finally:
            print(f"Finished processing {issue_to_process['html_url']}.")
            # We'll re-add the memory logic after we confirm this works.
    else:
        print("No new issues to process. Run finished.")
