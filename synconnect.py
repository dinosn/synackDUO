import base64
import json
import subprocess
import sys
import time
from urllib.parse import parse_qs, urlparse

import requests
from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.proxy import Proxy, ProxyType
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


# Replace with your login credentials
username = 'username'
password = 'password'

# Specify the file path
file_path = '/tmp/synacktoken'
proxy_host = ''
proxy_port = ''

DUO_POLL_INTERVAL = 2
DUO_PUSH_TIMEOUT = 60

BROWSER_FEATURES = (
    '{"touch_supported":false,"platform_authenticator_status":"unavailable",'
    '"webauthn_supported":true,"screen_resolution_height":1112,'
    '"screen_resolution_width":1710,"screen_color_depth":30,'
    '"is_uvpa_available":false,"client_capabilities_uvpa":false}'
)
CLIENT_HINTS = base64.b64encode(json.dumps({
    "brands": [{"brand": "Not-A.Brand", "version": "24"},
               {"brand": "Chromium", "version": "146"}],
    "fullVersionList": [], "mobile": False,
    "platform": "macOS", "platformVersion": "", "uaFullVersion": "",
}).encode()).decode()

# Check if proxy settings are provided
if proxy_host and proxy_port:
    proxy = Proxy({
        'proxyType': ProxyType.MANUAL,
        'httpProxy': f'{proxy_host}:{proxy_port}',
        'ftpProxy': f'{proxy_host}:{proxy_port}',
        'sslProxy': f'{proxy_host}:{proxy_port}',
        'noProxy': ''
    })
    options = webdriver.FirefoxOptions()
    options.add_argument('--proxy-server=http://{}:{}'.format(proxy_host, proxy_port))
    requests_proxies = {
        'http': f'http://{proxy_host}:{proxy_port}',
        'https': f'http://{proxy_host}:{proxy_port}',
    }
else:
    options = webdriver.FirefoxOptions()
    requests_proxies = None

options.headless = True  # Set to True if you don't want to see the browser
driver = webdriver.Firefox(options=options)

try:
    # Step 1: Login via Selenium
    driver.get('https://login.synack.com/')
    driver.find_element(By.NAME, 'email').send_keys(username)
    driver.find_element(By.NAME, 'password').send_keys(password)
    driver.implicitly_wait(20)
    while True:
        try:
            button = driver.find_element(By.CLASS_NAME, 'btn-blue')
            button.click()
            driver.implicitly_wait(10)
        except NoSuchElementException:
            break

    # Step 2: Wait for the Duo frameless prompt URL to load
    WebDriverWait(driver, 30).until(
        lambda d: '/prompt/' in urlparse(d.current_url).path
    )
    parsed = urlparse(driver.current_url)
    duo_base = f"{parsed.scheme}://{parsed.netloc}"
    akey = parsed.path.split('/prompt/')[1].split('/')[0]
    qs = parse_qs(parsed.query)
    authkey = qs.get('authkey', [None])[0]
    trace_id = qs.get('req_trace_group', [''])[0]
    if not authkey:
        raise RuntimeError(f"authkey missing from Duo prompt URL: {driver.current_url}")

    # Step 3: Mirror Selenium's cookies + UA into a requests session
    session = requests.Session()
    if requests_proxies:
        session.proxies.update(requests_proxies)
        session.verify = False
    for cookie in driver.get_cookies():
        session.cookies.set(
            cookie['name'], cookie['value'],
            domain=cookie.get('domain'), path=cookie.get('path', '/'),
        )
    user_agent = driver.execute_script("return navigator.userAgent;")
    custom_headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                  "image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    duo_headers = {
        **custom_headers,
        "Origin": duo_base,
        "Referer": f"{duo_base}/prompt/{akey}?authkey={authkey}"
                   f"&req_trace_group={trace_id}",
        "X-Duo-Req-Trace-Group": trace_id,
    }

    # Step 4: Duo pre-auth (payload → initialization → evaluation)
    session.get(
        f"{duo_base}/prompt/{akey}/auth/payload",
        params={'authkey': authkey, 'browser_features': BROWSER_FEATURES},
        headers=duo_headers,
    )
    session.get(
        f"{duo_base}/prompt/{akey}/pre_authn/initialization",
        params={'authkey': authkey, 'is_ipad': 'false',
                'client_hints': CLIENT_HINTS},
        headers=duo_headers,
    )
    eval_resp = session.get(
        f"{duo_base}/prompt/{akey}/pre_authn/evaluation",
        params={'authkey': authkey, 'browser_features': BROWSER_FEATURES,
                'local_trust_choice': 'undecided'},
        headers=duo_headers,
    )
    factors = eval_resp.json()['response']['available_unified_auth_factors']['factors']
    pkeys = [f['device_info']['pkey'] for f in factors if f.get('factor_type') == 'push']
    if not pkeys:
        raise RuntimeError("No push-capable Duo devices enrolled")

    # Step 5: Trigger push on each enrolled device until one succeeds
    def trigger_and_poll(pkey):
        r = session.post(
            f"{duo_base}/prompt/{akey}/auth/factors/push/auth",
            json={'authkey': authkey, 'pkey': pkey},
            headers={**duo_headers, "Content-Type": "application/json"},
        )
        txid = r.json()['response']['push_txid']
        subprocess.run(["python3", "main.py"], check=True)
        deadline = time.time() + DUO_PUSH_TIMEOUT
        while time.time() < deadline:
            r = session.get(
                f"{duo_base}/prompt/{akey}/auth/factors/push/status",
                params={'authkey': authkey, 'push_txid': txid,
                        'saw_good_news': 'false'},
                headers=duo_headers,
            )
            result = r.json()['response']['result']['result']
            if result == 'SUCCESS':
                return True
            if result == 'STATUS':
                time.sleep(DUO_POLL_INTERVAL)
                continue
            return False
        return False

    approved = False
    for pkey in pkeys:
        if trigger_and_poll(pkey):
            approved = True
            break
        print(f"Push to device {pkey} was not approved; trying next enrolled device.")
    if not approved:
        print("All Duo push attempts failed or timed out.")
        sys.exit(1)

    # Step 6: Finalize the Duo auth and hand control back to Selenium
    finalize = session.get(
        f"{duo_base}/prompt/{akey}/auth/finalize_auth",
        params={'authkey': authkey}, headers=duo_headers,
    )
    exit_url = finalize.json()['response']['url']

    driver.get(exit_url)
    WebDriverWait(driver, 50).until(EC.title_contains("Platform"))

    key_to_retrieve = "shared-session-com.synack.accessToken"
    stored_value = driver.execute_script(
        f"return sessionStorage.getItem('{key_to_retrieve}');"
    )

    print(f"Value from session storage for key '{key_to_retrieve}': {stored_value[:10]}")
    with open(file_path, 'w') as file:
        file.write(stored_value)

finally:
    try:
        if 'driver' in locals() and driver is not None:
            driver.quit()
    except WebDriverException as e:
        print(f"Error closing the browser: {str(e)}")
