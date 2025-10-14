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
GITHUB_USERNAME = "shanaya-Gupta" # <--- I've set this for you, but double-check!
SEARCH_QUERY = 'is:issue is:open label:"good first issue" language:python'

# --- Configure the Gemini AI Model ---
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-2.0-flash')

# --- HELPER FUNCTIONS ---

def find_github_issues():
    """Finds GitHub issues based on the SEARCH_QUERY."""
    print("Searching for issues...")
    headers = {'Authorization': f'token {GITHUB_TOKEN}', 'Accept': 'application/vnd.github.v3+json'}
    url = f"https://api.github.com/search/issues?q={SEARCH_QUERY}&sort=created&order=desc"
    response = requests.get(url, headers=headers)
    if response.status_code != 200:
        print(f"Error searching for issues: {response.status_code}")
        return None
    items = response.json().get('items', [])
    if not items:
        print("No issues found matching criteria.")
        return None
    return items[0]

def fork_repository(repo_full_name, headers):
    """Forks the repository to the authenticated user's account."""
    print(f"Forking {repo_full_name}...")
    fork_url = f"https://api.github.com/repos/{repo_full_name}/forks"
    response = requests.post(fork_url, headers=headers)
    if response.status_code in [200, 201, 202]:
        print("Fork request sent or fork already exists.")
        time.sleep(15)
        return True
    else:
        print(f"Failed to fork repository: {response.status_code} - {response.text}")
        return False

def process_issue(issue):
    """Forks, clones, fixes, and creates a PR for an issue using the full-file replacement strategy."""
    issue_url = issue['html_url']
    original_repo_full_name = issue['repository_url'].replace('https://api.github.com/repos/', '')
    repo_owner, repo_name = original_repo_full_name.split('/')
    
    headers = {'Authorization': f'token {GITHUB_TOKEN}', 'Accept': 'application/vnd.github.v3+json'}
    
    if not fork_repository(original_repo_full_name, headers):
        return

    forked_repo_full_name = f"{GITHUB_USERNAME}/{repo_name}"
    forked_repo_url_with_auth = f"https://{GITHUB_USERNAME}:{GITHUB_TOKEN}@github.com/{forked_repo_full_name}.git"
    temp_dir = f"temp_repo_{int(time.time())}"
    
    print(f"Cloning our fork: {forked_repo_full_name}...")
    try:
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
        shutil.rmtree(temp_dir)
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

    print("Asking Gemini to rewrite the file...")
    prompt = f"""
    You are an expert AI programmer. Your task is to fix a GitHub issue by rewriting a single file.

    ISSUE: {issue['title']}
    DESCRIPTION: {issue['body']}

    CODEBASE CONTEXT:
    {context[:500000]}

    INSTRUCTIONS:
    1.  Identify the single file that needs to be modified to fix the issue.
    2.  Provide the full, complete content of this file with the fix applied.
    3.  Your response MUST be in the following format, and nothing else:

    FILE_PATH: [path/to/your/file.py]

    ```python
    # Full content of the fixed file goes here
    # ...
    ```
    """

    try:
        response = model.generate_content(prompt)
        raw_response = response.text

        # --- NEW: Parse the file path and the new code content ---
        match = re.search(r"FILE_PATH: (.*?)\n\n```(?:\w+)?\n(.*)```", raw_response, re.DOTALL)
        if not match:
            print("Could not parse Gemini's response. The format was incorrect.")
            print("--- RAW GEMINI OUTPUT ---")
            print(raw_response[:500])
            print("-------------------------")
            shutil.rmtree(temp_dir)
            return

        file_to_change = match.group(1).strip()
        new_code_content = match.group(2).strip()

    except Exception as e:
        print(f"Error from Gemini: {e}")
        shutil.rmtree(temp_dir)
        return

    # --- NEW: Overwrite the file with the new content ---
    print(f"Applying fix by overwriting file: {file_to_change}")
    full_path_to_file = os.path.join(temp_dir, file_to_change)

    if not os.path.exists(os.path.dirname(full_path_to_file)):
        os.makedirs(os.path.dirname(full_path_to_file))
        
    try:
        with open(full_path_to_file, 'w', encoding='utf-8') as f:
            f.write(new_code_content)
        print("File overwritten successfully.")
    except FileNotFoundError:
        print(f"Error: Gemini specified a file path that does not exist: {file_to_change}")
        shutil.rmtree(temp_dir)
        return

    # --- The rest is the same: commit, push, and PR ---
    new_branch = f"fix/ai-{issue['number']}"
    print(f"Pushing to our fork's branch: {new_branch}")
    
    try:
        subprocess.run(['git', 'config', 'user.email', f'{GITHUB_USERNAME}@users.noreply.github.com'], cwd=temp_dir, check=True)
        subprocess.run(['git', 'config', 'user.name', GITHUB_USERNAME], cwd=temp_dir, check=True)
        subprocess.run(['git', 'checkout', '-b', new_branch], cwd=temp_dir, check=True)
        subprocess.run(['git', 'add', '.'], cwd=temp_dir, check=True)
        status = subprocess.run(['git', 'status', '--porcelain'], cwd=temp_dir, capture_output=True, text=True)
        if not status.stdout.strip():
            print("AI rewrite resulted in no changes. Aborting.")
            shutil.rmtree(temp_dir)
            return
        subprocess.run(['git', 'commit', '-m', f"fix: Resolve issue #{issue['number']}"], cwd=temp_dir, check=True)
        subprocess.run(['git', 'remote', 'set-url', 'origin', forked_repo_url_with_auth], cwd=temp_dir, check=True)
        subprocess.run(['git', 'push', '-u', 'origin', new_branch, '--force'], cwd=temp_dir, check=True)
        print("Code pushed to our fork on GitHub.")
    except subprocess.CalledProcessError as e:
        print(f"Git command failed: {e.cmd}\nStderr: {e.stderr}")
        shutil.rmtree(temp_dir)
        return

    print("Creating Pull Request...")
    repo_info = requests.get(f"https://api.github.com/repos/{original_repo_full_name}", headers=headers).json()
    base_branch = repo_info.get('default_branch', 'main')

    pr_data = { 'title': f"AI Fix for: {issue['title']}", 'body': f"Resolves #{issue['number']}.", 'head': f"{GITHUB_USERNAME}:{new_branch}", 'base': base_branch }
    pr_url = f"https://api.github.com/repos/{original_repo_full_name}/pulls"
    pr_response = requests.post(pr_url, headers=headers, json=pr_data)

    if pr_response.status_code in [200, 201]:
        print(f"SUCCESS! PR created: {pr_response.json()['html_url']}")
    elif pr_response.status_code == 422 and 'A pull request already exists' in pr_response.text:
        print("A PR already exists for this branch.")
    else:
        print(f"Failed to create PR. Status: {pr_response.status_code}\nResponse: {pr_response.text}")

    print("Cleaning up...")
    shutil.rmtree(temp_dir)


if __name__ == "__main__":
    if GITHUB_USERNAME == "YOUR_GITHUB_USERNAME":
        print("ERROR: You must change GITHUB_USERNAME in bot.py!")
        exit(1)
    print("Bot starting...")
    issue = find_github_issues()
    if issue:
        process_issue(issue)
    else:
        print("Done.")
