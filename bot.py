import os
import requests
import subprocess
import shutil
import time
import google.generativeai as genai

# --- CONFIGURATION ---
# Load your secret keys from environment variables.
# This is a secure way to handle secrets, especially in GitHub Actions.
GITHUB_TOKEN = os.getenv('GH_PAT')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')

# Your GitHub username. The bot will commit and create PRs as you.
GITHUB_USERNAME = "shanaya-Gupta" # <--- IMPORTANT: CHANGE THIS

# What kind of issues are we looking for?
# You can change the language and labels.
SEARCH_QUERY = 'is:issue is:open label:"good first issue" language:python'

# --- Configure the Gemini AI Model ---
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-2.5-pro')

# --- HELPER FUNCTIONS ---

def find_github_issues():
    """Finds GitHub issues based on the SEARCH_QUERY."""
    print("Searching for issues...")
    headers = {
        'Authorization': f'token {GITHUB_TOKEN}',
        'Accept': 'application/vnd.github.v3+json'
    }
    url = f"https://api.github.com/search/issues?q={SEARCH_QUERY}&sort=created&order=desc"
    response = requests.get(url, headers=headers)
    
    if response.status_code != 200:
        print(f"Error searching for issues: {response.status_code} - {response.text}")
        return []
        
    # We only want to work on one issue per run to be safe.
    items = response.json().get('items', [])
    if not items:
        print("No issues found.")
        return None
    
    # Return the first issue found
    return items[0]


def process_issue(issue):
    """Clones the repo, analyzes the code, generates a fix, and creates a PR."""
    issue_url = issue['html_url']
    
    # --- THIS IS THE FIXED LINE ---
    repo_full_name = issue['repository_url'].replace('https://api.github.com/', '')
    
    repo_url = f"https://{GITHUB_USERNAME}:{GITHUB_TOKEN}@github.com/{repo_full_name}.git"
    
    # Create a temporary directory to clone the repo
    temp_dir = f"temp_repo_{repo_full_name.replace('/', '_')}_{int(time.time())}"
    print(f"Processing issue: {issue_url}")
    print(f"Cloning {repo_full_name} into {temp_dir}...")

    # 1. Clone the repository
    try:
        subprocess.run(['git', 'clone', repo_url, temp_dir], check=True)
    except subprocess.CalledProcessError as e:
        print(f"Failed to clone repo: {e}")
        return

    # 2. Gather context (read relevant files)
    context = ""
    for root, _, files in os.walk(temp_dir):
        for file in files:
            # Simple filter for text-based files
            if file.endswith(('.py', '.js', '.ts', '.md', '.txt', '.html', '.css')):
                try:
                    with open(os.path.join(root, file), 'r', encoding='utf-8') as f:
                        # Add file path and content to the context
                        context += f"\n--- FILE: {os.path.join(root, file).replace(temp_dir, '')} ---\n"
                        context += f.read()
                except Exception:
                    pass # Ignore files that can't be read

    # 3. Prompt Gemini to generate a fix
    print("Asking Gemini for a fix...")
    prompt = f"""
    You are an autonomous AI software engineer. Your task is to fix a GitHub issue.
    Analyze the following issue and the provided source code, then generate a fix.
    
    **ISSUE DETAILS:**
    Title: {issue['title']}
    URL: {issue_url}
    Body:
    {issue['body']}
    
    **SOURCE CODE CONTEXT:**
    {context[:300000]}  # Limit context size to be safe

    **INSTRUCTIONS:**
    1. Understand the issue from the details provided.
    2. Analyze the source code to identify where the fix is needed.
    3. Generate the code changes required to resolve the issue.
    4. Provide the fix ONLY as a unified diff patch. Do not include any other text, explanations, or markdown formatting. Just the raw diff.
    
    Example of a valid response:
    ```diff
    --- a/src/main.py
    +++ b/src/main.py
    @@ -1,5 +1,5 @@
     def hello():
    -    print("Hello World")
    +    print("Hello, Universe")
     
     if __name__ == "__main__":
         hello()
    ```
    
    Now, generate the patch for the issue above.
    """

    try:
        response = model.generate_content(prompt)
        patch = response.text
        
        # Clean the patch from markdown code blocks
        if patch.startswith("```diff"):
            patch = patch[7:]
        if patch.endswith("```"):
            patch = patch[:-3]
        patch = patch.strip()

        if not patch:
            print("Gemini did not return a patch. Aborting.")
            shutil.rmtree(temp_dir)
            return

    except Exception as e:
        print(f"Error calling Gemini API: {e}")
        shutil.rmtree(temp_dir)
        return

    # 4. Apply the patch
    print("Applying patch...")
    patch_file = os.path.join(temp_dir, 'fix.patch')
    with open(patch_file, 'w') as f:
        f.write(patch)
    
    # We use 'git apply' to apply the changes
    result = subprocess.run(['git', 'apply', 'fix.patch'], cwd=temp_dir, capture_output=True, text=True)
    
    if result.returncode != 0:
        print(f"Failed to apply patch: {result.stderr}")
        shutil.rmtree(temp_dir)
        return
        
    print("Patch applied successfully.")

    # 5. Create a new branch, commit, and push
    branch_name = f"ai-fix-{issue['number']}-{int(time.time())}"
    print(f"Creating new branch: {branch_name}")
    
    try:
        subprocess.run(['git', 'checkout', '-b', branch_name], cwd=temp_dir, check=True)
        subprocess.run(['git', 'config', 'user.email', 'bot@example.com'], cwd=temp_dir, check=True)
        subprocess.run(['git', 'config', 'user.name', GITHUB_USERNAME], cwd=temp_dir, check=True)
        subprocess.run(['git', 'add', '.'], cwd=temp_dir, check=True)
        subprocess.run(['git', 'commit', '-m', f"feat: AI-generated fix for issue #{issue['number']}"], cwd=temp_dir, check=True)
        subprocess.run(['git', 'push', '-u', 'origin', branch_name], cwd=temp_dir, check=True)
        print("Changes pushed to new branch.")
    except subprocess.CalledProcessError as e:
        print(f"A Git command failed: {e.stderr}")
        shutil.rmtree(temp_dir)
        return

    # 6. Create a Pull Request
    print("Creating Pull Request...")
    pr_title = f"AI Fix for Issue #{issue['number']}: {issue['title']}"
    pr_body = f"This is an AI-generated pull request to fix issue #{issue['number']}.\n\nCloses #{issue['number']}."
    
    pr_headers = {
        'Authorization': f'token {GITHUB_TOKEN}',
        'Accept': 'application/vnd.github.v3+json'
    }
    pr_data = {
        'title': pr_title,
        'body': pr_body,
        'head': branch_name,
        'base': 'main' # Or 'master', we can try to detect this later
    }
    pr_url = f"https://api.github.com/{repo_full_name}/pulls"
    
    pr_response = requests.post(pr_url, headers=pr_headers, json=pr_data)

    if pr_response.status_code == 226 or pr_response.status_code == 201:
        print(f"Successfully created PR: {pr_response.json()['html_url']}")
    else:
        print(f"Failed to create PR: {pr_response.status_code} - {pr_response.text}")

    # 7. Clean up the temporary directory
    print(f"Cleaning up {temp_dir}...")
    shutil.rmtree(temp_dir)


# --- MAIN EXECUTION ---
if __name__ == "__main__":
    print("Starting AI GitHub Issue Resolver Bot...")
    issue = find_github_issues()
    if issue:
        process_issue(issue)
    else:
        print("No suitable issues to process. Exiting.")
    print("Bot run finished.")
