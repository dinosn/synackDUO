import requests
import time
import urllib3
import subprocess

# Suppress only the single InsecureRequestWarning from urllib3 needed for `verify=False`.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def read_token_from_file(file_path):
    """Reads the JWT token from the specified file."""
    with open(file_path, 'r') as file:
        return file.read().strip()

def get_tasks(token, proxies):
    """Performs a GET request to retrieve tasks."""
    url = "https://platform.synack.com/api/tasks/v2/tasks"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    params = {
        "perPage": 20,
        "viewed": "true",
        "page": 1,
        "status": "PUBLISHED",
        "sort": "CLAIMABLE",
        "sortDir": "DESC",
        "includeAssignedBySynackUser": "false"
    }
    
    response = requests.get(url, headers=headers, params=params, proxies=proxies, verify=False)
    if response.status_code == 200:
        tasks = response.json()
        if tasks:
            return tasks
    return None

def post_claim_task(token, task, proxies):
    """Attempts to claim a task with a POST request."""
    url = f"https://platform.synack.com/api/tasks/v1/organizations/{task['organizationUid']}/listings/{task['listingUid']}/campaigns/{task['campaignUid']}/tasks/{task['id']}/transitions"
    payload = {"type": "CLAIM"}
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}"
    }
    
    response = requests.post(url, json=payload, headers=headers, proxies=proxies, verify=False)
    return response.status_code

def main():
    token_file_path = '/tmp/synacktoken'
    token = read_token_from_file(token_file_path)
    proxies = {}

    while True:
        tasks = get_tasks(token, proxies)
        if tasks:
            for task in tasks:
                while True:
                    status_code = post_claim_task(token, task, proxies)
                    if status_code == 201:
                        print("Mission claimed successfully.")
                        break  # Break out of the inner loop on success
                    elif status_code == 412:
                        print("Precondition failed, stopping attempts to claim this task.")
                        break  # Stop if we hit the 412 condition
                    else:
                        print("Attempting to claim mission again in 5 seconds.")
                        time.sleep(5)  # Wait for 5 seconds before trying to claim the task again
                if status_code == 401:
                    subprocess.run(["python3", "synconnect_cli.py"])
                    token = read_token_from_file(token_file_path)
                    print("Token refreshed, continuing the loop.")  # If we hit 401 we exit the token needs to be refreshed
        time.sleep(30)  # Wait for 30 seconds before polling again

if __name__ == "__main__":
    main()
