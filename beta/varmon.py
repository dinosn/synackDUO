import argparse
import json
import os
import time
import requests
import urllib3
from datetime import datetime

# Suppress SSL warnings globally
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Configuration
VARPAY_CACHE = "varpay_cache.json"
SLACK_WEBHOOK_URL = "https://hooks.slack.com/services/XXXXXXXXX/XXXXXXXXX/XXXXXXXXXXXXXXXXXX"  # Replace
DEFAULT_PROXY = "http://127.0.0.1:8080"
POLL_INTERVAL = 600  # 10 minutes

def send_slack_alert(message, proxies=None, debug=False):
    payload = {"text": message}
    try:
        if debug:
            print(f"[DEBUG] Sending Slack alert: {payload}")
        r = requests.post(SLACK_WEBHOOK_URL, json=payload, proxies=proxies, verify=False, timeout=10)
        if debug:
            print(f"[DEBUG] Slack response: {r.status_code} - {r.text}")
        r.raise_for_status()
    except Exception as e:
        print(f"[!] Failed to send Slack alert: {e}")

def get_varpay(slug, token, proxies=None, debug=False):
    url = f"https://platform.synack.com/api/targets/{slug}/dynamic_payment_percentage"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json"
    }
    try:
        if debug:
            print(f"[DEBUG] Requesting URL: {url}")
        response = requests.get(url, headers=headers, proxies=proxies, verify=False, timeout=15)

        if response.status_code == 401:
            raise ValueError("Unauthorized: Token is invalid or expired.")

        if debug:
            print(f"[DEBUG] Response status: {response.status_code}")
            print(f"[DEBUG] Response body: {response.text}")

        response.raise_for_status()
        data = response.json()
        value = data.get("dynamic_payment_percentage")
        return float(value) if value is not None else None
    except ValueError as ve:
        raise ve
    except Exception as e:
        print(f"[!] Error fetching varpay for {slug}: {e}")
        return None

def load_cache():
    if os.path.exists(VARPAY_CACHE):
        with open(VARPAY_CACHE, "r") as f:
            return json.load(f)
    return {}

def save_cache(cache):
    with open(VARPAY_CACHE, "w") as f:
        json.dump(cache, f, indent=2)

def send_change_notification(slug, old_val, new_val, proxies=None, debug=False):
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    message = (
        f"*VarPay Change Detected*\n"
        f"Target: `{slug}`\nOld VarPay: `{old_val}`\nNew VarPay: `{new_val}`\nTimestamp: {timestamp}"
    )
    send_slack_alert(message, proxies=proxies, debug=debug)

def monitor_targets(slug_list, token, proxies=None, debug=False):
    cache = load_cache()
    while True:
        try:
            for slug in slug_list:
                current_varpay = get_varpay(slug, token, proxies=proxies, debug=debug)
                if current_varpay is None:
                    print(f"[!] Could not retrieve current varpay for {slug}")
                    continue

                cached_raw = cache.get(slug)
                try:
                    cached_varpay = float(cached_raw) if cached_raw is not None else None
                except ValueError:
                    cached_varpay = None

                if cached_varpay is None:
                    if debug:
                        print(f"[DEBUG] First run for {slug}. Caching value: {current_varpay}")
                    print(f"[+] First run for {slug}. VarPay cached as {current_varpay}. No Slack notification sent.")
                    cache[slug] = str(current_varpay)
                    save_cache(cache)
                    continue

                if cached_varpay != current_varpay:
                    print(f"[+] VarPay change detected for {slug}: {cached_varpay} → {current_varpay}")
                    send_change_notification(slug, cached_varpay, current_varpay, proxies=proxies, debug=debug)
                    cache[slug] = str(current_varpay)
                    save_cache(cache)
                else:
                    if debug:
                        print(f"[-] No change for {slug} (Current: {current_varpay})")

            if debug:
                print(f"[DEBUG] Sleeping for {POLL_INTERVAL} seconds...\n")
            time.sleep(POLL_INTERVAL)

        except ValueError as ve:
            print(f"[!] {ve}")
            send_slack_alert(f":warning: *Synack token is invalid or expired.* Monitoring stopped.", proxies=proxies, debug=debug)
            break

def main():
    parser = argparse.ArgumentParser(description="Monitor varpay change for one or more Synack targets.")
    parser.add_argument("slugs", help="Comma-separated list of target slug/listing_id")
    parser.add_argument("--token", required=True, help="Synack Bearer token")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("--no-proxy", action="store_true", help="Disable default proxy usage")
    args = parser.parse_args()

    proxies = None if args.no_proxy else {"http": DEFAULT_PROXY, "https": DEFAULT_PROXY}
    slug_list = [slug.strip() for slug in args.slugs.split(",") if slug.strip()]
    monitor_targets(slug_list, args.token, proxies=proxies, debug=args.debug)

if __name__ == "__main__":
    main()
