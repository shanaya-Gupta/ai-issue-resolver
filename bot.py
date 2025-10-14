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
GITHUB_USERNAME = "shanaya-Gupta" # <--- REMEMBER TO CHANGE THIS TO YOUR ACTUAL USERNAME
SEARCH_QUERY = 'is:issue is:open label:"good first issue" language:python'

# --- Configure the Gemini AI Model ---
genai.configure(api_key=GEMINI_API_KEY)
# Using Flash model as it's faster and has a generous rate limit
model = genai.GenerativeModel('gemini-2.0-flash') 

# --- HELPER FUNCTIONS ---

def find_github_issues():
    """Finds GitHub issues based on the SEARCH_QUERY."""
    print("Searching for issues...")
    headers = {'Authorization': f'token {GITHUB_TOKEN}', 'Accept': 'application/vnd.github.v3+json'}
    url = f"https://api.github.com/search/issues?q={SEARCH_QUERY}&sort=created&order=desc"
    response = requests.get(url, headers=headers)
    
    if response.status_code != 200:
        # Sometimes search API has rate limits, just return None to try later
        print(f"Error searching for issues (could be rate limit): {response.status_code}")
        return None
        
    items = response.json().get('items', [])
    if not items:
        print("No issues found matching criteria.")
        return None
    
    # To avoid stuck on same issue, ideally we'd pick random, but for now first is fine
    return items[0]


def process_issue(issue):
    """Clones the repo, analyzes the code, generates a fix, and creates a PR."""
    issue_url = issue['html_url']
    repo_full_name = issue['repository_url'].replace('https://api.github.com/repos/', '')
    # Construct URL with auth for pushing later
    repo_url = f"https://{GITHUB_USERNAME}:{GITHUB_TOKEN}@github.com/{repo_full_name}.git"
    
    # Use a simpler temp dir name to avoid path length issues
    temp_dir = f"temp_repo_{int(time.time())}"
    print(f"-------------------------------------------------")
    print(f"Processing issue: {issue_url}")
    print(f"Repo: {repo_full_name}")
    print(f"-------------------------------------------------")

    # 1. Clone the repository
    print("Cloning repository...")
    try:
        # Clone with depth 1 to save bandwidth and time
        subprocess.run(['git', 'clone', '--depth', '1', f"https://github.com/{repo_full_name}.git", temp_dir], check=True, capture_output=True, text=True)
        
        # Set remote url with token for pushing
        subprocess.run(['git', 'remote', 'set-url', 'origin', repo_url], cwd=temp_dir, check=True)
    except subprocess.CalledProcessError as e:
        print(f"Failed to clone repo: {e.stderr}")
        return

    # 2. Gather context
    print("Reading files for context...")
    context = ""
    file_count = 0
    for root, _, files in os.walk(temp_dir):
        if '.git' in root: continue # Skip .git folder
        for file in files:
            if file.endswith(('.py', '.js', '.ts', '.md', '.txt', '.html', '.css', '.json', '.yaml', '.sh')):
                file_path = os.path.join(root, file)
                # Skip large files (>1MB) to save tokens
                if os.path.getsize(file_path) > 1024 * 1024: continue
                try:
                    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                        rel_path = os.path.relpath(file_path, temp_dir)
                        context += f"\n--- FILE: {rel_path} ---\n"
                        context += f.read()
                        file_count += 1
                except Exception:
                    pass
    print(f"Read {file_count} files.")

    # 3. Prompt Gemini to generate a fix
    print("Asking Gemini for a fix...")
    # Limit context to avoid token errors, Flash has large context but let's be safe
    safe_context = context[:500000] 
    
    prompt = f"""
    You are an expert developer. Fix this GitHub issue.
    
    ISSUE: {issue['title']}
    DESCRIPTION: {issue['body']}
    
    CODEBASE CONTEXT:
    {safe_context}

    INSTRUCTIONS:
    1. Generate a 'unified diff' patch to fix the issue.
    2. Output ONLY the patch inside a ```diff code block.
    3. Ensure context lines in the diff match the files exactly.
    4. Do not add any explanation.
    """

    try:
        response = model.generate_content(prompt)
        raw_response = response.text
        
        # Extract patch using regex
        match = re.search(r"```diff\n(.*?)```", raw_response, re.DOTALL)
        if match:
            patch = match.group(1).strip()
            # Normalize line endings
            patch = patch.replace('\r\n', '\n').replace('\r', '\n')
            # Ensure patch ends with a newline
            if not patch.endswith('\n'): patch += '\n'
        else:
            print("Could not find ```diff block in Gemini response.")
            print("Raw response preview:", raw_response[:200])
            shutil.rmtree(temp_dir)
            return

        if not patch:
            print("Extracted patch was empty.")
            shutil.rmtree(temp_dir)
            return

    except Exception as e:
        print(f"Error from Gemini: {e}")
        shutil.rmtree(temp_dir)
        return

    # 4. Apply the patch
    print("Applying patch...")
    patch_file = os.path.join(temp_dir, 'fix.patch')
    with open(patch_file, 'w', newline='\n') as f:
        f.write(patch)
    
    # --- THE FIX: Use forgiving flags for git apply ---
    # --ignore-space-change and --ignore-whitespace help when LLM messes up context spaces
    result = subprocess.run(
        ['git', 'apply', '--ignore-space-change', '--ignore-whitespace', '-v', 'fix.patch'],
        cwd=temp_dir, capture_output=True, text=True
    )
    
    if result.returncode != 0:
        print(f"Failed to apply patch. Git output:\n{result.stderr}")
        # Print patch for debugging
        print("--- FAILED PATCH ---")
        print(patch)
        print("--------------------")
        shutil.rmtree(temp_dir)
        return
        
    print("Patch applied successfully!")

    # 5. Create branch, commit, push
    new_branch = f"fix/issue-{issue['number']}"
    print(f"Pushing to branch: {new_branch}")
    
    try:
        # Configure git
        subprocess.run(['git', 'config', 'user.email', f'{GITHUB_USERNAME}@users.noreply.github.com'], cwd=temp_dir, check=True)
        subprocess.run(['git', 'config', 'user.name', GITHUB_USERNAME], cwd=temp_dir, check=True)
        
        # Checkout new branch
        subprocess.run(['git', 'checkout', '-b', new_branch], cwd=temp_dir, capture_output=True, check=True)
        
        # Stage all changes
        subprocess.run(['git', 'add', '.'], cwd=temp_dir, check=True)
        
        # Check if there is anything to commit
        status = subprocess.run(['git', 'status', '--porcelain'], cwd=temp_dir, capture_output=True, text=True)
        if not status.stdout.strip():
            print("Patch applied but resulted in no changes (maybe already fixed?). Aborting.")
            shutil.rmtree(temp_dir)
            return

        # Commit
        commit_msg = f"fix: Resolve issue #{issue['number']}\n\nGenerated by AI."
        subprocess.run(['git', 'commit', '-m', commit_msg], cwd=temp_dir, check=True)
        
        # Push
        subprocess.run(['git', 'push', '-u', 'origin', new_branch, '--force'], cwd=temp_dir, capture_output=True, check=True)
        print("Code pushed to GitHub.")
        
    except subprocess.CalledProcessError as e:
        print(f"Git command failed: {e.cmd}")
        print(f"Stderr: {e.stderr}")
        shutil.rmtree(temp_dir)
        return

    # 6. Create Pull Request via API
    print("Creating Pull Request...")
    
    # Try to find default branch
    repo_info = requests.get(f"https://api.github.com/repos/{repo_full_name}", headers=headers).json()
    base_branch = repo_info.get('default_branch', 'main')

    pr_data = {
        'title': f"Fix: {issue['title']}",
        'body': f"This PR fixes #{issue['number']}.\n\n*Generated automatically by an AI agent.*",
        'head': f"{GITHUB_USERNAME}:{new_branch}", # Format: username:branch
        'base': base_branch
    }
    
    pr_url = f"https://api.github.com/repos/{repo_full_name}/pulls"
    pr_response = requests.post(pr_url, headers=headers, json=pr_data)

    if pr_response.status_code in [200, 201]:
        print(f"SUCCESS! PR created: {pr_response.json()['html_url']}")
    elif pr_response.status_code == 422 and 'A pull request already exists' in pr_response.text:
        print("A PR already exists for this branch.")
    else:
        print(f"Failed to create PR. Status: {pr_response.status_code}")
        print(f"Response: {pr_response.text}")

    # 7. Cleanup
    print("Cleaning up...")
    shutil.rmtree(temp_dir)


# --- MAIN EXECUTION ---
if __name__ == "__main__":
    # Check username config
    if GITHUB_USERNAME == "YOUR_GITHUB_USERNAME":
        print("ERROR: You forgot to change GITHUB_USERNAME in bot.py!")
        exit(1)

    print("Bot starting...")
    issue = find_github_issues()
    if issue:
        process_issue(issue)
    else:
        print("Done.")
