import requests
import time
import urllib3
from threading import Thread
import argparse

# Suppress only the single InsecureRequestWarning from urllib3 needed for `verify=False`.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def get_task(token, proxies):
    """Performs GET request to retrieve tasks."""
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
    return response

def post_claim_task(token, task_info, proxies):
    """Performs POST request to claim a specific task."""
    url = f"https://platform.synack.com/api/tasks/v1/organizations/{task_info['organizationUid']}/listings/{task_info['listingUid']}/campaigns/{task_info['campaignUid']}/tasks/{task_info['id']}/transitions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}"
    }
    payload = {"type": "CLAIM"}
    response = requests.post(url, json=payload, headers=headers, proxies=proxies, verify=False)
    return response

def check_response_for_auth_failure(response):
    """Checks if the response indicates an authentication failure such as token expiration."""
    if response.status_code == 401:
        return True
    return False

def refresh_token():
    """Prompts the user to enter a new token."""
    print("Token expired. Please enter a new token:")
    return input("New Token: ")

def main(token):
    proxies = {}
    known_slugs = set()

    while True:
        get_response = get_task(token, proxies)
        if check_response_for_auth_failure(get_response):
            token = refresh_token()  # Update the token if it's expired
            continue  # Retry the last operation with the new token

        if get_response.status_code == 200:
            tasks = get_response.json()
            for task in tasks:
                post_response = post_claim_task(token, task, proxies)
                if check_response_for_auth_failure(post_response):
                    token = refresh_token()  # Update the token if it's expired
                    continue  # Retry the last operation with the new token
                
                if post_response.status_code == 201:
                    print("Mission claimed successfully.")
                elif post_response.status_code == 412:
                    print("Mission cannot be claimed anymore.")
                    break
                time.sleep(5)
        else:
            print(f"Failed to retrieve tasks. Status code: {get_response.status_code}")
        time.sleep(30)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Poll and claim tasks and targets on Synack platform.")
    parser.add_argument("token", help="JWT token for authentication")
    args = parser.parse_args()
    main(args.token)
