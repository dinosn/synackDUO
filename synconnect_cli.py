import base64
import json
import subprocess
import sys
import time
from urllib.parse import parse_qs, urlparse
import requests
from bs4 import BeautifulSoup

# Constants
EMAIL = ""
PASSWORD = ""
DUO_POLL_INTERVAL = 2  # seconds
DUO_DEVICE_HEALTH_WAIT = 60
DUO_DEVICE_HEALTH_POLL_INTERVAL = 5
MAX_RETRIES = 3
PRIMARY_PKEY = "DPxxxx" # Refer README for more info
FALLBACK_PKEY = "DPxxxx" # Refer README for more info
DUO_APPROVER_CMD = ["python3", "main.py"]
file_path = '/tmp/synacktoken'

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


def wait_for_pre_authn_factors(session, url, params, headers):
    deadline = time.time() + DUO_DEVICE_HEALTH_WAIT
    last_payload = None
    while True:
        response = session.get(url, params=params, headers=headers)
        try:
            payload = response.json()
        except ValueError:
            raise RuntimeError(
                f"pre_authn/evaluation returned non-JSON: "
                f"{response.status_code} {response.text[:200]}"
            )
        pkeys = collect_push_pkeys(payload)
        if pkeys:
            return payload, set(pkeys)

        action = duo_action(payload)
        if action != 'remediation':
            return payload, set()
        last_payload = payload

        if time.time() >= deadline:
            raise RuntimeError(
                "pre_authn/evaluation remained in Duo remediation after "
                f"{DUO_DEVICE_HEALTH_WAIT}s; no Duo Push transaction will be "
                "created until the endpoint/device policy is satisfied. "
                f"Details: {duo_gate_summary(last_payload)}"
            )

        print("Duo endpoint/device remediation is still active; waiting for "
              "device health collection to finish.")
        time.sleep(DUO_DEVICE_HEALTH_POLL_INTERVAL)


def synack():
    def is_json(response):
        try:
            response.json()
            return True
        except ValueError:
            return False

    def exit_on_error(message):
        print(message)
        sys.exit(1)

    session = requests.Session()
    session.cookies = requests.cookies.RequestsCookieJar()

    custom_headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/146.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                  "image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://login.synack.com/",
    }

    # Step 1: GET request to login.synack.com to fetch CSRF token
    try:
        response = session.get('https://login.synack.com', headers=custom_headers)
        if response.status_code != 200:
            exit_on_error("Failed to fetch CSRF token")
        soup = BeautifulSoup(response.text, 'html.parser')
        csrf_token = soup.find('meta', {'name': 'csrf-token'})['content']
    except Exception as e:
        exit_on_error(f"Error during fetching CSRF token: {e}")

    # Step 2: POST request to /api/authenticate with credentials
    duo_auth_url = None
    for attempt in range(MAX_RETRIES):
        try:
            response = session.post(
                'https://login.synack.com/api/authenticate',
                json={"email": EMAIL, "password": PASSWORD},
                headers={'X-Csrf-Token': csrf_token},
            )
            if response.status_code == 200 and is_json(response):
                duo_auth_url = response.json().get('duo_auth_url')
                if duo_auth_url:
                    print(f"[!] Login successful on attempt {attempt + 1}")
                    break
                exit_on_error("Duo Auth URL missing in response")
            else:
                print(f"Login attempt {attempt + 1} failed, status code: "
                      f"{response.status_code}, retrying...")
                if attempt == MAX_RETRIES - 1:
                    exit_on_error("Login failed after maximum retries")
        except Exception as e:
            print(f"Login attempt {attempt + 1} failed with error: {e}")
            if attempt == MAX_RETRIES - 1:
                exit_on_error(f"Error during login after maximum retries: {e}")

    # Step 3: Follow OAuth → Duo prompt; extract akey / authkey / duo_base
    try:
        response = session.get(duo_auth_url, headers=custom_headers,
                               allow_redirects=True)
        if response.status_code != 200:
            exit_on_error(f"Duo redirect chain failed: {response.status_code}")
        parsed = urlparse(response.url)
        duo_base = f"{parsed.scheme}://{parsed.netloc}"
        if '/prompt/' not in parsed.path:
            exit_on_error(f"Unexpected Duo landing URL: {response.url}")
        akey = parsed.path.split('/prompt/')[1].split('/')[0]
        qs = parse_qs(parsed.query)
        authkey = qs.get('authkey', [None])[0]
        trace_id = qs.get('req_trace_group', [''])[0]
        if not authkey:
            exit_on_error("authkey missing from Duo prompt URL")
    except Exception as e:
        exit_on_error(f"Error during Duo redirect/extract: {e}")

    duo_headers = {
        **custom_headers,
        "Origin": duo_base,
        "Referer": f"{duo_base}/prompt/{akey}?authkey={authkey}"
                   f"&req_trace_group={trace_id}",
        "X-Duo-Req-Trace-Group": trace_id,
    }

    # Step 4: Pre-auth (payload → initialization → evaluation)
    try:
        response = session.get(
            f"{duo_base}/prompt/{akey}/auth/payload",
            params={'authkey': authkey, 'browser_features': BROWSER_FEATURES},
            headers=duo_headers,
        )
        if not is_json(response):
            exit_on_error("auth/payload returned non-JSON")
        payload = response.json()
        if duo_action(payload) == 'collect_device_health':
            print("Duo requested endpoint/device health collection before MFA "
                  "factors. Requests-only mode cannot complete that collection.")
        elif duo_action(payload):
            print(f"Duo auth/payload action: {duo_action(payload)}")
        raise_if_duo_remediation(payload, "auth/payload")

        response = session.get(
            f"{duo_base}/prompt/{akey}/pre_authn/initialization",
            params={'authkey': authkey, 'is_ipad': 'false',
                    'client_hints': CLIENT_HINTS},
            headers=duo_headers,
        )
        if response.status_code not in (200, 404) and not is_json(response):
            exit_on_error(
                f"pre_authn/initialization failed: {response.status_code} "
                f"{response.text[:200]}"
            )
        eval_payload, enrolled = wait_for_pre_authn_factors(
            session,
            f"{duo_base}/prompt/{akey}/pre_authn/evaluation",
            {'authkey': authkey, 'browser_features': BROWSER_FEATURES,
             'local_trust_choice': 'undecided'},
            duo_headers,
        )
        if not enrolled:
            exit_on_error(
                "No push-capable Duo devices found. "
                "pre_authn/evaluation payload: "
                f"{json.dumps(redact_payload(eval_payload), sort_keys=True)[:1000]}"
            )
    except Exception as e:
        exit_on_error(f"Error during Duo pre-auth: {e}")

    # Step 5: POST Duo push and poll, with fallback
    def trigger_push(pkey):
        r = session.post(
            f"{duo_base}/prompt/{akey}/auth/factors/push/auth",
            json={'authkey': authkey, 'pkey': pkey},
            headers={**duo_headers, "Content-Type": "application/json"},
        )
        if r.status_code != 200 or not is_json(r):
            try:
                body = json.dumps(redact_payload(r.json()))[:500]
            except ValueError:
                body = r.text[:200]
            exit_on_error(f"push/auth failed: {r.status_code} {body}")
        return r.json()['response']['push_txid']

    primary_pkey = (PRIMARY_PKEY if PRIMARY_PKEY in enrolled
                    else next(iter(enrolled)))
    fallback_pkey = (FALLBACK_PKEY
                     if FALLBACK_PKEY in enrolled
                     and FALLBACK_PKEY != primary_pkey
                     else None)

    def run_push_and_poll(pkey):
        txid = trigger_push(pkey)
        approver = subprocess.Popen(DUO_APPROVER_CMD)
        deadline = time.time() + 60
        try:
            while time.time() < deadline:
                r = session.get(
                    f"{duo_base}/prompt/{akey}/auth/factors/push/status",
                    params={'authkey': authkey, 'push_txid': txid,
                            'saw_good_news': 'false'},
                    headers=duo_headers,
                )
                payload = r.json() if is_json(r) else {}
                approver_rc = approver.poll()
                if r.status_code == 401:
                    if approver_rc == 0:
                        print("push/status returned 401 after local approval; "
                              "continuing to finalize auth.")
                        return True
                    if approver_rc is not None:
                        exit_on_error(
                            f"local approver exited with {approver_rc}, and "
                            "push/status failed: 401 "
                            f"{json.dumps(redact_payload(payload))[:500]}"
                        )
                    time.sleep(DUO_POLL_INTERVAL)
                    continue
                if approver_rc not in (None, 0):
                    exit_on_error(
                        f"local approver exited with {approver_rc} before "
                        "Duo push completed"
                    )
                if r.status_code != 200 or not payload:
                    exit_on_error(
                        f"push/status failed: {r.status_code} "
                        f"{json.dumps(redact_payload(payload))[:500] or r.text[:200]}"
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
            if approver.poll() is None:
                try:
                    approver.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    approver.terminate()
                    try:
                        approver.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        approver.kill()

    try:
        ok = run_push_and_poll(primary_pkey)
        if not ok and fallback_pkey:
            print("Primary device did not approve. Trying fallback Android.")
            ok = run_push_and_poll(fallback_pkey)
        if not ok:
            exit_on_error("All Duo push attempts failed/timed out.")
    except Exception as e:
        exit_on_error(f"Error during push/poll: {e}")

    # Step 6: Finalize auth → follow redirects → grant_token
    try:
        response = session.get(
            f"{duo_base}/prompt/{akey}/auth/finalize_auth",
            params={'authkey': authkey}, headers=duo_headers,
        )
        if not is_json(response):
            exit_on_error("finalize_auth returned non-JSON")
        exit_url = response.json()['response']['url']

        response = session.get(exit_url, headers=custom_headers,
                               allow_redirects=True)
        if 'grant_token=' not in response.url:
            exit_on_error(f"grant_token missing from final URL: {response.url}")
        grant_token = response.url.split('grant_token=')[1].split('&')[0]
    except Exception as e:
        exit_on_error(f"Error during Duo finalize/redirect: {e}")

    # Step 7: GET request to /token?grant_token= to receive access_token
    headers = {**custom_headers, 'X-Requested-With': 'XMLHttpRequest'}
    response = requests.get(
        f'https://platform.synack.com/token?grant_token={grant_token}',
        headers=headers,
    )
    access_token = response.json().get('access_token') if is_json(response) else None
    return access_token


def write_token_to_file(token, file_path):
    try:
        with open(file_path, 'w') as file:
            file.write(token)
    except Exception as e:
        print(f"Error writing to file: {e}")


auth = synack()

print("Access-Token:", auth)

write_token_to_file(auth, file_path)
