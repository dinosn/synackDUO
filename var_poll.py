import requests
import json
import urllib3
import sqlite3
import time
from datetime import datetime

# Suppress only the single InsecureRequestWarning from urllib3 needed for this use case.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Path to the token file
token_file_path = '/tmp/synacktoken'

def read_token():
    with open(token_file_path, 'r') as file:
        return file.read().strip()

# Initial read of the authorization token
auth_token = read_token()

# Base URL and headers for the request
base_url = "https://platform.synack.com/api/targets"
headers = {
    "Sec-Ch-Ua": '"Chromium";v="125", "Not.A/Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Authorization": f"Bearer {auth_token}",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.6422.112 Safari/537.36",
    "Sec-Ch-Ua-Platform": '"Linux"',
    "Accept": "*/*",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
    "Referer": "https://platform.synack.com/targets",
    "Accept-Encoding": "gzip, deflate, br",
    "Accept-Language": "en-US,en;q=0.9",
    "Priority": "u=1, i",
    "Connection": "keep-alive"
}

# Parameters for the request
params = {
    "filter[primary]": "registered",
    "filter[secondary]": "all",
    "filter[category]": "all",
    "filter[industry]": "all",
    "filter[payout_status]": "all",
    "sorting[field]": "dynamic_payment_percentage",
    "sorting[direction]": "asc",
    "pagination[page]": 1
}

# Proxies dictionary to use the localhost proxy
proxies = {
    "http": "",
    "https": ""
}

# Slack webhook URL
#slack_webhook_url = 'https://hooks.slack.com/services/something'


# Connect to the SQLite database (or create it if it doesn't exist)
conn = sqlite3.connect('synack_data.db')
c = conn.cursor()

# Create table for storing the data
c.execute('''CREATE TABLE IF NOT EXISTS targets (
                id TEXT PRIMARY KEY,
                data TEXT
            )''')
conn.commit()

def fetch_data():
    global auth_token
    all_data = []
    params["pagination[page]"] = 1  # Reset page to 1 for each poll
    while True:
        headers["Authorization"] = f"Bearer {auth_token}"
        response = requests.get(base_url, headers=headers, params=params, proxies=proxies, verify=False)
        if response.status_code == 401:
            auth_token = read_token()
            headers["Authorization"] = f"Bearer {auth_token}"
            response = requests.get(base_url, headers=headers, params=params, proxies=proxies, verify=False)
        
        if response.status_code == 200 and response.json():
            content = response.json()
            all_data.extend(content)
            params["pagination[page]"] += 1
        else:
            break
    return {item['slug']: {key: item[key] for key in ['codename', 'averagePayout', 'dynamic_payment_percentage', 'lastSubmitted']} 
            for item in all_data if 'slug' in item}

def convert_unix_to_datetime(unix_time):
    if isinstance(unix_time, int):
        return datetime.utcfromtimestamp(unix_time).strftime('%Y-%m-%d %H:%M:%S')
    return unix_time

def format_percentage(value):
    if isinstance(value, str) and value.endswith('%'):
        return value
    return f"{float(value) * 100}%"

def send_to_slack(message):
    payload = {
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": message
                }
            }
        ]
    }
    requests.post(slack_webhook_url, json=payload, verify=False)

def format_item(item):
    formatted_item = f"*Codename:* {item['codename']}\n"
    formatted_item += f"*Average Payout:* {item['averagePayout']}\n"
    formatted_item += f"*Dynamic Payment Percentage:* {item['dynamic_payment_percentage']}\n"
    formatted_item += f"*Last Submitted:* {item['lastSubmitted']}\n"
    return formatted_item

def print_and_send_differences(old_data, new_data):
    old_ids = set(old_data.keys())
    new_ids = set(new_data.keys())

    added_ids = new_ids - old_ids
    updated_ids = {i for i in new_ids & old_ids if old_data[i] != new_data[i]}

    slack_message = ""

    if added_ids:
        message = "*Added items:*\n"
        print("Added items:")
        for i in added_ids:
            item = new_data[i]
            item['lastSubmitted'] = convert_unix_to_datetime(item['lastSubmitted'])
            item['dynamic_payment_percentage'] = format_percentage(item['dynamic_payment_percentage'])
            formatted_item = format_item(item)
            print(formatted_item)
            message += f"{formatted_item}\n"
        slack_message += message
    
    if updated_ids:
        message = "*Updated items:*\n"
        print("Updated items:")
        for i in updated_ids:
            old_item = old_data[i]
            new_item = new_data[i]

            # Convert datetime and format percentage for display only
            old_display = old_item.copy()
            new_display = new_item.copy()
            old_display['lastSubmitted'] = convert_unix_to_datetime(old_item['lastSubmitted'])
            old_display['dynamic_payment_percentage'] = format_percentage(old_item['dynamic_payment_percentage'])
            new_display['lastSubmitted'] = convert_unix_to_datetime(new_item['lastSubmitted'])
            new_display['dynamic_payment_percentage'] = format_percentage(new_item['dynamic_payment_percentage'])

            if old_display != new_display:
                formatted_old_item = format_item(old_display)
                formatted_new_item = format_item(new_display)
                print(f"Old:\n{formatted_old_item}")
                print(f"New:\n{formatted_new_item}")
                message += f"*Old:*\n{formatted_old_item}\n*New:*\n{formatted_new_item}\n"
        slack_message += message

    if slack_message:
        send_to_slack(slack_message)

while True:
    try:
        # Print the current date and time
        print(f"\nPolling at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

        # Fetch new data
        new_data = fetch_data()

        # Fetch old data from the database
        c.execute("SELECT id, data FROM targets")
        old_data = {row[0]: json.loads(row[1]) for row in c.fetchall()}

        # Print and send differences
        print_and_send_differences(old_data, new_data)

        # Store new and updated data in the database
        for target_id, data in new_data.items():
            c.execute("INSERT OR REPLACE INTO targets (id, data) VALUES (?, ?)", (target_id, json.dumps(data)))
        conn.commit()

        # Wait for 10 minutes before the next poll
        time.sleep(600)
    except requests.exceptions.RequestException as e:
        print(f"Connection error: {e}")
        print("Waiting for 10 minutes before retrying...")
        time.sleep(600)

# Close the database connection
conn.close()
