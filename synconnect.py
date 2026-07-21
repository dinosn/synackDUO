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
DUO_DEVICE_HEALTH_WAIT = 60
DUO_DEVICE_HEALTH_POLL_INTERVAL = 5
DUO_APPROVER_CMD = ["python3", "main.py"]

DUO_STATUS_SUCCESS = {
    'success', 'allow', 'allowed', 'approve', 'approved', 'authenticated',
    'complete', 'completed',
}
DUO_STATUS_PENDING = {
    'status', 'waiting', 'pending', 'pushed', 'sent', 'in_progress',
    'push_waiting',
}
DUO_STATUS_FAILURE = {
    'failure', 'failed', 'fail', 'deny', 'denied', 'reject', 'rejected',
    'timeout', 'timed_out', 'fraud', 'canceled', 'cancelled', 'locked_out',
    'push_failed',
}

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


def response_json(response, context, require_ok=True):
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"{context} returned non-JSON: {response.status_code} "
            f"{response.text[:300]}"
        ) from exc
    if require_ok and response.status_code != 200:
        raise RuntimeError(
            f"{context} failed: {response.status_code} "
            f"{json.dumps(redact_payload(payload))[:500]}"
        )
    return payload


def normalize_duo_status(value):
    if value is None:
        return ''
    return str(value).strip().lower().replace(' ', '_').replace('-', '_').strip('.')


def classify_duo_status_value(value):
    normalized = normalize_duo_status(value)
    if not normalized:
        return None
    if normalized in DUO_STATUS_SUCCESS or 'success' in normalized:
        return True
    if normalized in DUO_STATUS_PENDING:
        return 'pending'
    if normalized in DUO_STATUS_FAILURE:
        return False
    if 'waiting' in normalized or 'pending' in normalized or 'pushed' in normalized:
        return 'pending'
    if 'denied' in normalized or 'timeout' in normalized or 'failed' in normalized:
        return False
    return None


def parse_push_status(payload):
    response = payload.get('response', payload)
    if not isinstance(response, dict):
        raise RuntimeError(f"Unexpected Duo push/status response: {payload}")

    result = response.get('result')
    if isinstance(result, dict):
        for key in ('result', 'status', 'status_code', 'state', 'outcome'):
            classified = classify_duo_status_value(result.get(key))
            if classified is not None:
                return classified
    else:
        classified = classify_duo_status_value(result)
        if classified is not None:
            return classified

    for key in ('status', 'status_code', 'state', 'outcome', 'auth_status',
                'push_status', 'status_msg'):
        classified = classify_duo_status_value(response.get(key))
        if classified is not None:
            return classified

    for key in ('success', 'approved', 'authenticated'):
        if response.get(key) is True:
            return True

    raise RuntimeError(
        "Unrecognized Duo push/status response: "
        f"{json.dumps(redact_payload(payload), sort_keys=True)[:1000]}"
    )


def redact_payload(value):
    secret_keys = {
        'akey', 'authkey', 'authorization', 'cookie', 'csrf', 'csrf_token',
        'grant_token', 'key', 'password', 'pkey', 'secret', 'session', 'sid',
        'token', 'txid', 'push_txid', 'urgid',
    }
    secret_suffixes = (
        '_key', '_secret', '_sid', '_token', 'password',
    )
    if isinstance(value, dict):
        redacted = {}
        for key, child in value.items():
            key_text = str(key).lower()
            if key_text in secret_keys or key_text.endswith(secret_suffixes):
                redacted[key] = '<redacted>'
            else:
                redacted[key] = redact_payload(child)
        return redacted
    if isinstance(value, list):
        return [redact_payload(child) for child in value[:5]]
    return value


def iter_dicts(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_dicts(child)


def collect_push_pkeys(payload):
    response = payload.get('response', payload)
    pkeys = []
    seen = set()
    for item in iter_dicts(response):
        pkey = item.get('pkey')
        device_info = item.get('device_info')
        if not pkey and isinstance(device_info, dict):
            pkey = device_info.get('pkey')
        if not isinstance(pkey, str) or not pkey:
            continue

        searchable = json.dumps(item, default=str).lower()
        if 'push' not in searchable and not pkey.startswith('DP'):
            continue
        if pkey in seen:
            continue
        seen.add(pkey)
        pkeys.append(pkey)
    return pkeys


def duo_response(payload):
    response = payload.get('response', payload) if isinstance(payload, dict) else {}
    return response if isinstance(response, dict) else {}


def duo_action(payload):
    return duo_response(payload).get('action')


def duo_gate_summary(payload):
    response = duo_response(payload)
    authz_evaluation = response.get('authz_evaluation')
    summary = {
        'action': response.get('action'),
    }
    if isinstance(authz_evaluation, dict):
        summary['authz_evaluation'] = {
            'blocks': authz_evaluation.get('blocks'),
            'device': authz_evaluation.get('device'),
        }
    if isinstance(response.get('device_health'), dict):
        device_health = response['device_health']
        summary['device_health'] = {
            'os': device_health.get('os'),
            'browser': device_health.get('browser'),
            'is_collection_needed': device_health.get('is_collection_needed'),
            'is_collection_silent': device_health.get('is_collection_silent'),
            'is_unknown_browser': device_health.get('is_unknown_browser'),
            'has_passport_session': device_health.get('has_passport_session'),
        }
    return json.dumps(redact_payload(summary), sort_keys=True)


def raise_if_duo_remediation(payload, context):
    if duo_action(payload) != 'remediation':
        return
    raise RuntimeError(
        f"{context} returned Duo remediation instead of MFA factors; "
        "no Duo Push transaction will be created until the endpoint/device "
        f"policy is satisfied. Details: {duo_gate_summary(payload)}"
    )


def browser_text_summary(browser):
    script = r"""
    const text = document.body ? document.body.innerText : "";
    return text.replace(/\s+/g, " ").trim().slice(0, 600);
    """
    try:
        browser.switch_to.default_content()
        text = browser.execute_script(script)
    except WebDriverException:
        return ''
    return text or ''


def wait_for_pre_authn_factors(session, url, params, headers, browser):
    deadline = time.time() + DUO_DEVICE_HEALTH_WAIT
    last_payload = None
    while True:
        response = session.get(url, params=params, headers=headers)
        payload = response_json(response, "pre_authn/evaluation")
        pkeys = collect_push_pkeys(payload)
        if pkeys:
            return payload, pkeys

        action = duo_action(payload)
        if action != 'remediation':
            return payload, []
        last_payload = payload

        if time.time() >= deadline:
            details = duo_gate_summary(last_payload)
            visible_text = browser_text_summary(browser)
            if visible_text:
                details = f"{details}; browser_text={visible_text!r}"
            raise RuntimeError(
                "pre_authn/evaluation remained in Duo remediation after "
                f"{DUO_DEVICE_HEALTH_WAIT}s; no Duo Push transaction will be "
                "created until the endpoint/device policy is satisfied. "
                f"Details: {details}"
            )

        print("Duo endpoint/device remediation is still active; waiting for "
              "device health collection to finish.")
        time.sleep(DUO_DEVICE_HEALTH_POLL_INTERVAL)


DUO_PUSH_CLICK_SCRIPT = r"""
const terms = ["send me a push", "send push", "duo push", "push"];
const negativeTerms = [
  "passcode", "sms", "text message", "phone call", "call me",
  "security key", "webauthn"
];

function isVisible(el) {
  const style = window.getComputedStyle(el);
  const rect = el.getBoundingClientRect();
  return style.display !== "none" && style.visibility !== "hidden" &&
    rect.width > 0 && rect.height > 0;
}

function labelledByText(el) {
  const ids = (el.getAttribute("aria-labelledby") || "").split(/\s+/);
  return ids.map((id) => {
    const labelled = document.getElementById(id);
    return labelled ? labelled.textContent : "";
  }).join(" ");
}

function textOf(el) {
  return [
    el.innerText,
    el.value,
    el.getAttribute("aria-label"),
    el.getAttribute("title"),
    el.getAttribute("data-testid"),
    el.getAttribute("name"),
    labelledByText(el),
  ].filter(Boolean).join(" ").replace(/\s+/g, " ").toLowerCase();
}

function score(text) {
  let value = 0;
  if (text.includes("send me a push")) value += 40;
  if (text.includes("send push")) value += 30;
  if (text.includes("duo push")) value += 25;
  if (text.includes("push")) value += 10;
  if (text.includes("recommended")) value += 3;
  return value;
}

function walk(root, out) {
  const nodes = root.querySelectorAll ? Array.from(root.querySelectorAll("*")) : [];
  for (const el of nodes) {
    out.push(el);
    if (el.shadowRoot) walk(el.shadowRoot, out);
  }
}

const nodes = [];
walk(document, nodes);
const candidates = [];
for (const el of nodes) {
  const tag = (el.tagName || "").toLowerCase();
  const role = (el.getAttribute("role") || "").toLowerCase();
  const actionable = tag === "button" || tag === "a" || tag === "input" ||
    role === "button" || role === "menuitem" || el.tabIndex >= 0;
  if (!actionable || !isVisible(el)) continue;

  const text = textOf(el);
  if (!terms.some((term) => text.includes(term))) continue;
  if (negativeTerms.some((term) => text.includes(term))) continue;
  candidates.push({el, text, score: score(text)});
}

candidates.sort((left, right) => right.score - left.score);
if (!candidates.length) return null;
candidates[0].el.click();
return candidates[0].text.slice(0, 160);
"""


def stop_approver(approver):
    if approver.poll() is None:
        try:
            approver.wait(timeout=1)
        except subprocess.TimeoutExpired:
            approver.terminate()
            try:
                approver.wait(timeout=2)
            except subprocess.TimeoutExpired:
                approver.kill()


def click_duo_push_control(browser):
    deadline = time.time() + 20
    last_error = None
    while time.time() < deadline:
        frames = [None]
        try:
            browser.switch_to.default_content()
            frames.extend(browser.find_elements(By.CSS_SELECTOR, "iframe"))
        except WebDriverException as exc:
            last_error = str(exc).splitlines()[0]

        for frame in frames:
            try:
                browser.switch_to.default_content()
                if frame is not None:
                    browser.switch_to.frame(frame)
                clicked = browser.execute_script(DUO_PUSH_CLICK_SCRIPT)
                if clicked:
                    browser.switch_to.default_content()
                    return clicked
            except WebDriverException as exc:
                last_error = str(exc).splitlines()[0]
        time.sleep(0.5)

    raise RuntimeError(
        "Unable to find a Duo Push control in the browser prompt"
        + (f": {last_error}" if last_error else "")
    )


def wait_for_duo_browser_completion(browser, duo_netloc):
    def completed(driver):
        current = urlparse(driver.current_url)
        return (
            'Platform' in driver.title or
            'grant_token=' in driver.current_url or
            current.netloc != duo_netloc or
            '/prompt/' not in current.path
        )

    WebDriverWait(browser, DUO_PUSH_TIMEOUT).until(completed)


def approve_duo_with_browser_prompt(browser, duo_netloc):
    approver = subprocess.Popen(DUO_APPROVER_CMD)
    try:
        clicked = click_duo_push_control(browser)
        print(f"Clicked Duo Push control in browser: {clicked}")
        wait_for_duo_browser_completion(browser, duo_netloc)
        return True
    finally:
        stop_approver(approver)

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
    payload_resp = session.get(
        f"{duo_base}/prompt/{akey}/auth/payload",
        params={'authkey': authkey, 'browser_features': BROWSER_FEATURES},
        headers=duo_headers,
    )
    payload = response_json(payload_resp, "auth/payload")
    if duo_action(payload) == 'collect_device_health':
        print("Duo requested endpoint/device health collection before MFA "
              "factors. Waiting briefly for the browser prompt to process it.")
        time.sleep(5)
    elif duo_action(payload):
        print(f"Duo auth/payload action: {duo_action(payload)}")
    raise_if_duo_remediation(payload, "auth/payload")

    init_resp = session.get(
        f"{duo_base}/prompt/{akey}/pre_authn/initialization",
        params={'authkey': authkey, 'is_ipad': 'false',
                'client_hints': CLIENT_HINTS},
        headers=duo_headers,
    )
    if init_resp.status_code not in (200, 404):
        response_json(init_resp, "pre_authn/initialization")
    eval_payload, pkeys = wait_for_pre_authn_factors(
        session,
        f"{duo_base}/prompt/{akey}/pre_authn/evaluation",
        {'authkey': authkey, 'browser_features': BROWSER_FEATURES,
         'local_trust_choice': 'undecided'},
        duo_headers,
        driver,
    )
    if not pkeys:
        print("Duo evaluation did not include web push factors; falling back "
              "to the browser prompt UI.")

    # Step 5: Trigger push on each enrolled device until one succeeds
    def trigger_and_poll(pkey):
        r = session.post(
            f"{duo_base}/prompt/{akey}/auth/factors/push/auth",
            json={'authkey': authkey, 'pkey': pkey},
            headers={**duo_headers, "Content-Type": "application/json"},
        )
        txid = response_json(r, "push/auth")['response']['push_txid']
        approver = subprocess.Popen(DUO_APPROVER_CMD)
        deadline = time.time() + DUO_PUSH_TIMEOUT
        try:
            while time.time() < deadline:
                r = session.get(
                    f"{duo_base}/prompt/{akey}/auth/factors/push/status",
                    params={'authkey': authkey, 'push_txid': txid,
                            'saw_good_news': 'false'},
                    headers=duo_headers,
                )
                payload = response_json(r, "push/status", require_ok=False)
                approver_rc = approver.poll()
                if r.status_code == 401:
                    if approver_rc == 0:
                        print("push/status returned 401 after local approval; "
                              "continuing to finalize auth.")
                        return True
                    if approver_rc is not None:
                        raise RuntimeError(
                            f"local approver exited with {approver_rc}, and "
                            "push/status failed: 401 "
                            f"{json.dumps(redact_payload(payload))[:500]}"
                        )
                    time.sleep(DUO_POLL_INTERVAL)
                    continue
                if approver_rc not in (None, 0):
                    raise RuntimeError(
                        f"local approver exited with {approver_rc} before "
                        "Duo push completed"
                    )
                if r.status_code != 200:
                    raise RuntimeError(
                        f"push/status failed: {r.status_code} "
                        f"{json.dumps(redact_payload(payload))[:500]}"
                    )
                push_status = parse_push_status(payload)
                if push_status is True:
                    return True
                if push_status == 'pending':
                    time.sleep(DUO_POLL_INTERVAL)
                    continue
                return False
            return False
        finally:
            stop_approver(approver)

    approved = False
    browser_completed_duo = False
    last_push_error = None
    for pkey in pkeys:
        try:
            if trigger_and_poll(pkey):
                approved = True
                break
        except RuntimeError as exc:
            last_push_error = exc
            print(f"Duo prompt API push failed: {exc}")
            break
        print(f"Push to device {pkey} was not approved; trying next enrolled device.")
    if not approved:
        if last_push_error:
            print("Falling back to the Duo browser prompt UI.")
        approved = approve_duo_with_browser_prompt(driver, parsed.netloc)
        browser_completed_duo = True
    if not approved:
        print("All Duo push attempts failed or timed out.")
        sys.exit(1)

    # Step 6: Finalize the Duo auth and hand control back to Selenium
    if not browser_completed_duo:
        finalize = session.get(
            f"{duo_base}/prompt/{akey}/auth/finalize_auth",
            params={'authkey': authkey}, headers=duo_headers,
        )
        exit_url = response_json(finalize, "finalize_auth")['response']['url']
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
