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
GITHUB_USERNAME = "shanaya-Gupta" # <--- IMPORTANT: CHANGE THIS
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
    
    # 202 means fork is in progress; 200 or 201 means it's done or already exists
    if response.status_code in [200, 201, 202]:
        print("Fork created or already exists.")
        # Give GitHub a moment to complete the fork operation
        time.sleep(10) 
        return True
    else:
        print(f"Failed to fork repository: {response.status_code} - {response.text}")
        return False

def process_issue(issue):
    """Forks, clones, fixes, and creates a PR for an issue."""
    issue_url = issue['html_url']
    original_repo_full_name = issue['repository_url'].replace('https://api.github.com/repos/', '')
    repo_owner, repo_name = original_repo_full_name.split('/')
    
    headers = {'Authorization': f'token {GITHUB_TOKEN}', 'Accept': 'application/vnd.github.v3+json'}
    
    # --- STEP 1: FORK THE REPOSITORY ---
    if not fork_repository(original_repo_full_name, headers):
        return

    # Now we work with OUR fork
    forked_repo_full_name = f"{GITHUB_USERNAME}/{repo_name}"
    forked_repo_url_with_auth = f"https://{GITHUB_USERNAME}:{GITHUB_TOKEN}@github.com/{forked_repo_full_name}.git"

    temp_dir = f"temp_repo_{int(time.time())}"
    print(f"Processing issue: {issue_url} in our fork {forked_repo_full_name}")

    # --- STEP 2: CLONE OUR FORK ---
    print(f"Cloning our fork: {forked_repo_full_name}...")
    try:
        subprocess.run(['git', 'clone', '--depth', '1', f"https://github.com/{forked_repo_full_name}.git", temp_dir], check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        print(f"Failed to clone our fork: {e.stderr}")
        return

    # The rest of the logic is the same...
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

    print("Asking Gemini for a fix...")
    prompt = f"Fix this GitHub issue.\n\nISSUE: {issue['title']}\nDESCRIPTION: {issue['body']}\n\nCODEBASE:\n{context[:500000]}\n\nINSTRUCTIONS:\nProvide ONLY a unified diff patch inside a ```diff code block."

    try:
        response = model.generate_content(prompt)
        raw_response = response.text
        match = re.search(r"```diff\n(.*?)```", raw_response, re.DOTALL)
        if match:
            patch = match.group(1).strip().replace('\r\n', '\n').replace('\r', '\n')
            if not patch.endswith('\n'): patch += '\n'
        else:
            print("Could not find ```diff block in Gemini response.")
            shutil.rmtree(temp_dir)
            return
    except Exception as e:
        print(f"Error from Gemini: {e}")
        shutil.rmtree(temp_dir)
        return

    print("Applying patch...")
    patch_file = os.path.join(temp_dir, 'fix.patch')
    with open(patch_file, 'w', newline='\n') as f: f.write(patch)
    
    result = subprocess.run(['git', 'apply', '--ignore-space-change', '--ignore-whitespace', 'fix.patch'], cwd=temp_dir, capture_output=True, text=True)
    
    if result.returncode != 0:
        print(f"Failed to apply patch. Git output:\n{result.stderr}")
        shutil.rmtree(temp_dir)
        return
        
    print("Patch applied successfully!")

    # --- STEP 3: PUSH TO OUR FORK ---
    new_branch = f"fix/ai-{issue['number']}"
    print(f"Pushing to our fork's branch: {new_branch}")
    
    try:
        subprocess.run(['git', 'config', 'user.email', f'{GITHUB_USERNAME}@users.noreply.github.com'], cwd=temp_dir, check=True)
        subprocess.run(['git', 'config', 'user.name', GITHUB_USERNAME], cwd=temp_dir, check=True)
        subprocess.run(['git', 'checkout', '-b', new_branch], cwd=temp_dir, check=True)
        subprocess.run(['git', 'add', '.'], cwd=temp_dir, check=True)
        status = subprocess.run(['git', 'status', '--porcelain'], cwd=temp_dir, capture_output=True, text=True)
        if not status.stdout.strip():
            print("Patch resulted in no changes. Aborting.")
            shutil.rmtree(temp_dir)
            return
        subprocess.run(['git', 'commit', '-m', f"fix: Resolve issue #{issue['number']}"], cwd=temp_dir, check=True)
        # We need to set the remote URL to our fork with authentication
        subprocess.run(['git', 'remote', 'set-url', 'origin', forked_repo_url_with_auth], cwd=temp_dir, check=True)
        subprocess.run(['git', 'push', '-u', 'origin', new_branch], cwd=temp_dir, check=True)
        print("Code pushed to our fork on GitHub.")
    except subprocess.CalledProcessError as e:
        print(f"Git command failed: {e.cmd}\nStderr: {e.stderr}")
        shutil.rmtree(temp_dir)
        return

    # --- STEP 4: CREATE PULL REQUEST ---
    print("Creating Pull Request...")
    repo_info = requests.get(f"https://api.github.com/repos/{original_repo_full_name}", headers=headers).json()
    base_branch = repo_info.get('default_branch', 'main')

    pr_data = {
        'title': f"AI Fix for: {issue['title']}",
        'body': f"This is an AI-generated pull request that attempts to resolve issue #{issue['number']}.",
        'head': f"{GITHUB_USERNAME}:{new_branch}", # Your branch
        'base': base_branch                      # The original repo's branch
    }
    
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
