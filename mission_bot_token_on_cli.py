import requests
import time
import urllib3
import random
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
    url = (
        "https://platform.synack.com/api/tasks/v1/"
        f"organizations/{task_info['organizationUid']}/"
        f"listings/{task_info['listingUid']}/"
        f"campaigns/{task_info['campaignUid']}/"
        f"tasks/{task_info['id']}/transitions"
    )
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}"
    }
    payload = {"type": "CLAIM"}
    response = requests.post(url, json=payload, headers=headers, proxies=proxies, verify=False)
    return response

def poll_unregistered_targets(token, proxies, known_slugs):
    """
    Polls for unregistered targets every 5 minutes
    (300 seconds) and signs up for new ones.
    Handles 401, 429 as well.
    """
    while True:
        url = (
            "https://platform.synack.com/api/targets"
            "?filter%5Bprimary%5D=unregistered&filter%5Bsecondary%5D=all&filter%5Bcategory%5D=all"
            "&filter%5Bindustry%5D=all&filter%5Bpayout_status%5D=all"
            "&sorting%5Bfield%5D=onboardedAt&sorting%5Bdirection%5D=desc"
            "&pagination%5Bpage%5D=1&pagination%5Bper_page%5D=15"
        )
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        response = requests.get(url, headers=headers, proxies=proxies, verify=False)
        
        if response.status_code == 200:
            targets = response.json()
            for target in targets:
                slug = target['slug']
                if slug not in known_slugs:
                    known_slugs.add(slug)
                    signup_target(token, slug, proxies)
        
        elif response.status_code == 401:
            token = refresh_token()  # Handle token expiration
        
        elif response.status_code == 429:
            # Too Many Requests, random back-off
            delay = random.randint(10, 20)
            print(f"429 Too Many Requests detected. Sleeping for {delay} seconds before retrying poll_unregistered_targets.")
            time.sleep(delay)
        
        else:
            print(f"Unexpected status code in poll_unregistered_targets: {response.status_code}")

        # Poll every 5 minutes
        time.sleep(300)

def signup_target(token, slug, proxies):
    """Performs POST request to sign up for a target using its slug."""
    url = f"https://platform.synack.com/api/targets/{slug}/signup"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    payload = {"ResearcherListing": {"terms": 1}}
    response = requests.post(url, json=payload, headers=headers, proxies=proxies, verify=False)
    
    if response.status_code == 200:
        print(f"Signed up for target {slug} successfully.")
    else:
        print(
            f"Failed to sign up for target {slug}. "
            f"Status code: {response.status_code}, Response: {response.text}"
        )

def refresh_token():
    """Prompts the user to enter a new token."""
    print("Token expired. Please enter a new token:")
    return input("New Token: ")

def main(token):
    proxies = {}
    known_slugs = set()

    # Start the thread for polling unregistered targets
    target_thread = Thread(target=poll_unregistered_targets, args=(token, proxies, known_slugs))
    target_thread.daemon = True
    target_thread.start()

    while True:
        get_response = get_task(token, proxies)

        if get_response.status_code == 401:
            token = refresh_token()  # Handle token expiration
            continue
        
        elif get_response.status_code == 429:
            # Too Many Requests, random back-off
            delay = random.randint(10, 20)
            print(f"429 Too Many Requests detected. Sleeping for {delay} seconds before retrying get_task.")
            time.sleep(delay)
            continue
        
        elif get_response.status_code == 200:
            tasks = get_response.json()
            for task in tasks:
                post_response = post_claim_task(token, task, proxies)
                
                if post_response.status_code == 401:
                    token = refresh_token()
                    continue
                
                elif post_response.status_code == 429:
                    # Too Many Requests, random back-off
                    delay = random.randint(10, 20)
                    print(f"429 Too Many Requests detected. Sleeping for {delay} seconds before retrying post_claim_task.")
                    time.sleep(delay)
                    continue
                
                elif post_response.status_code == 201:
                    print("Mission claimed successfully.")
                
                elif post_response.status_code == 412:
                    print("Mission cannot be claimed anymore.")
                    # Break out of the for-loop for tasks
                    break

                # Changed from 5 seconds to 11 seconds
                time.sleep(11)
        
        else:
            print(f"Failed to retrieve tasks. Status code: {get_response.status_code}")

        # Sleep 30 seconds before checking again
        time.sleep(30)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Poll and claim tasks and targets on Synack platform.")
    parser.add_argument("token", help="JWT token for authentication")
    args = parser.parse_args()
    main(args.token)
