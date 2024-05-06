# Synack DUO Automation Guide

Automate Synack DUO Push requests without needing a physical device and save the required token for further automation processes.

## Prerequisites
- Python3
- Libraries: pycryptodome, requests, beautifulsoup4

Install the necessary libraries:
```bash
pip install -r requirements.txt
```

## Setting up ruo
1. Execute `main.py`.
2. Enter the code from the QR code (use an alternative QR code scanner) or via the link provided in the email (accessible on a desktop).
  The proceess in screenshots is similar to this:
  
<img width="272" alt="Screenshot 2024-05-06 at 05 48 56" src="https://github.com/dinosn/synackDUO/assets/3851678/4e9435fa-8b9e-4800-9f68-75ffacf6e898"> ,
<img width="300" alt="Screenshot 2024-05-06 at 05 50 15" src="https://github.com/dinosn/synackDUO/assets/3851678/1f566215-f124-4c43-b701-8b5dc66613bb"> , 
<img width="260" alt="Screenshot 2024-05-06 at 05 50 47" src="https://github.com/dinosn/synackDUO/assets/3851678/27333bad-c874-47b2-ba33-78a50d2e1137"> , 
<img width="260" alt="Screenshot 2024-05-06 at 05 51 03" src="https://github.com/dinosn/synackDUO/assets/3851678/313ac430-3257-4bce-9075-2c191fc207c9"> , 
<img width="274" alt="Screenshot 2024-05-06 at 05 51 12" src="https://github.com/dinosn/synackDUO/assets/3851678/5146f0c3-b918-4cba-adcc-7a2ec04f54ae"> .

## Using synconnect (Selenium-based token generation)
### Preparation
1. Complete the ruo setup.
2. In `synconnect.py`, update your credentials at lines 12 and 13.
3. To run in headless mode (without opening a browser window), set `options.headless = True` on line 37.
4. (Optional) Customize Token Storage Location
   - To save the token in a different location, modify the `file_path` on line 16.
   - By default, the token is stored in `/tmp/synacktoken`.

### Running the Script
Execute the script using Python:
```bash
python3 synconnect.py
```

## Using synconnect_cli (Requests-based token generation)
### Initial Setup
1. After setting up ruo, capture the login process with Burp Suite.
2. Locate the `/frame/v4/auth/prompt/data` request and note down the index and key from its response.
3. Update `synconnect_cli.py` with your credentials on lines 12 and 13.
4. Set the `index` (e.g., `phone2`) on line 16 and `key` (e.g., `DPXXXXXXXXXX`) on line 17.
5. (Optional) Customize Token Storage Location
   - To save the token in a different location, modify the `file_path` on line 18.
   - By default, the token is stored in `/tmp/synacktoken`.

### Running the Script
Execute using Python:
```bash
python3 synconnect_cli.py
```

## Known Issue and Solution
For the automation to work correctly, the device set up for this script must be the primary device. If it's not, request to make it primary or do so manually by removing previous devices and re-adding them later.

Alternatively, use `synconnect_cli.py` with the correct configuration to circumvent this issue.

### Mission Bot
Execute using Python:
```bash
python3 mission.py
```
If you want to use it standalone as a script and provide your own token, comment out the following lines.  Token is always read from /tmp/synacktoken.
```
 71                   subprocess.run(["python3", "synconnect_cli.py"])
 72                   token = read_token_from_file(token_file_path)
```
