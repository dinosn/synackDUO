import requests
import time
import urllib3
import random
from threading import Thread
import argparse

# Suppress only the single InsecureRequestWarning from urllib3 needed for `verify=False`.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DEBUG = False  # Default: debugging is off

def debug_log(msg):
    if DEBUG:
        print(f"[DEBUG] {msg}")

def get_task(token, proxies):
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
    debug_log(f"GET tasks: {url}")
    response = requests.get(url, headers=headers, params=params, proxies=proxies, verify=False)
    debug_log(f"Response: {response.status_code}")
    return response

def mark_target_as_read(token, listing_uid, proxies):
    """Marks a target as read using a GET request."""
    url = f"https://platform.synack.com/api/resource_reads?resource_type=target&resource_id={listing_uid}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    debug_log(f"GET mark as read: {url}")
    response = requests.get(url, headers=headers, proxies=proxies, verify=False)
    if response.status_code != 204:
        debug_log(f"Failed to mark target as read: {response.status_code} {response.text}")
    else:
        debug_log(f"Target {listing_uid} marked as read.")

def post_claim_task(token, task_info, proxies):
    mark_target_as_read(token, task_info['listingUid'], proxies)

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
    debug_log(f"POST claim task: {url} | Payload: {payload}")
    response = requests.post(url, json=payload, headers=headers, proxies=proxies, verify=False)
    debug_log(f"Response: {response.status_code} {response.text}")
    return response

def poll_unregistered_targets(token, proxies, known_slugs):
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
        debug_log("Polling unregistered targets...")
        response = requests.get(url, headers=headers, proxies=proxies, verify=False)
        
        if response.status_code == 200:
            targets = response.json()
            for target in targets:
                slug = target['slug']
                if slug not in known_slugs:
                    debug_log(f"New target found: {slug}")
                    known_slugs.add(slug)
                    signup_target(token, slug, proxies, target.get("listingUid"))
        
        elif response.status_code == 401:
            token = refresh_token()
        
        elif response.status_code == 429:
            delay = random.randint(10, 20)
            debug_log(f"429 detected in target poll. Sleeping {delay}s.")
            time.sleep(delay)
        
        else:
            debug_log(f"Unexpected status code: {response.status_code}")
        time.sleep(300)

def signup_target(token, slug, proxies, listing_uid=None):
    url = f"https://platform.synack.com/api/targets/{slug}/signup"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    payload = {"ResearcherListing": {"terms": 1}}
    debug_log(f"POST signup target: {url}")
    response = requests.post(url, json=payload, headers=headers, proxies=proxies, verify=False)
    
    if response.status_code == 200:
        print(f"Signed up for target {slug} successfully.")
        if listing_uid:
            mark_target_as_read(token, listing_uid, proxies)
    else:
        print(f"Failed to sign up for target {slug}. Status: {response.status_code}, Response: {response.text}")

def refresh_token():
    print("Token expired. Please enter a new token:")
    return input("New Token: ")

def main(token):
    proxies = {}
    known_slugs = set()

    target_thread = Thread(target=poll_unregistered_targets, args=(token, proxies, known_slugs))
    target_thread.daemon = True
    target_thread.start()

    while True:
        get_response = get_task(token, proxies)

        if get_response.status_code == 401:
            token = refresh_token()
            continue
        elif get_response.status_code == 429:
            delay = random.randint(10, 20)
            debug_log(f"429 detected in task poll. Sleeping {delay}s.")
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
                    delay = random.randint(10, 20)
                    debug_log(f"429 detected in task claim. Sleeping {delay}s.")
                    time.sleep(delay)
                    continue
                elif post_response.status_code == 201:
                    print("Mission claimed successfully.")
                elif post_response.status_code == 412:
                    print("Mission cannot be claimed anymore.")
                    break

                time.sleep(11)
        else:
            debug_log(f"Failed to retrieve tasks. Status code: {get_response.status_code}")
        time.sleep(30)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Poll and claim tasks and targets on Synack platform.")
    parser.add_argument("token", help="JWT token for authentication")
    parser.add_argument("--debug", action="store_true", help="Enable debug output")
    args = parser.parse_args()

    DEBUG = args.debug
    main(args.token)
