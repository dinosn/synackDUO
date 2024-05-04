import requests
import time
import urllib3
from threading import Thread

# Suppress only the single InsecureRequestWarning from urllib3 needed for `verify=False`.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def read_token_from_file(file_path):
    """Reads the JWT token from the specified file."""
    with open(file_path, 'r') as file:
        return file.read().strip()

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

def poll_unregistered_targets(token, proxies, known_slugs):
    """Polls for unregistered targets every 5 minutes and signs up for new ones."""
    while True:
        url = "https://platform.synack.com/api/targets?filter%5Bprimary%5D=unregistered&filter%5Bsecondary%5D=all&filter%5Bcategory%5D=all&filter%5Bindustry%5D=all&filter%5Bpayout_status%5D=all&sorting%5Bfield%5D=onboardedAt&sorting%5Bdirection%5D=desc&pagination%5Bpage%5D=1&pagination%5Bper_page%5D=15"
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
        else:
            print(f"Failed to retrieve unregistered targets. Status code: {response.status_code}")
        time.sleep(300)  # Poll every 5 minutes

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
        print(f"Failed to sign up for target {slug}. Status code: {response.status_code}, Response: {response.text}")

def main():
    token_file_path = '/tmp/synacktoken'
    token = read_token_from_file(token_file_path)
    proxies = {
        # "http": "http://yourproxyaddress:port",
        # "https": "http://yourproxyaddress:port",
    }
    known_slugs = set()  # To track known slugs and avoid duplicate sign-ups if ever.

    # Start the thread for polling unregistered targets
    target_thread = Thread(target=poll_unregistered_targets, args=(token, proxies, known_slugs))
    target_thread.start()

    while True:
        get_response = get_task(token, proxies)
        if get_response.status_code == 200:
            tasks = get_response.json()
            for task in tasks:
                post_response = post_claim_task(token, task, proxies)
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
    main()
