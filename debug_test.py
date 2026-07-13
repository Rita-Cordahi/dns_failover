import os
import sys
import subprocess
import requests
import json
import time

server_path = os.path.abspath("tests/mock_server.py")
print("Starting mock server from:", server_path)

# Let's run uvicorn in a way that we can see stdout/stderr
proc = subprocess.Popen(
    [sys.executable, server_path],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True
)

# Wait and poll
success = False
url = "http://127.0.0.1:8001/control/reset"
for i in range(50):
    time.sleep(0.1)
    # Check if process is still running
    poll = proc.poll()
    if poll is not None:
        print(f"Mock server process terminated with code {poll}")
        out, err = proc.communicate()
        print("Mock server stdout:", out)
        print("Mock server stderr:", err)
        sys.exit(1)
        
    try:
        resp = requests.post(url, timeout=0.5)
        if resp.status_code == 200:
            success = True
            print("Mock server successfully started!")
            break
    except requests.exceptions.ConnectionError:
        pass

if not success:
    print("Mock server failed to respond in 5 seconds.")
    proc.terminate()
    out, err = proc.communicate()
    print("Mock server stdout:", out)
    print("Mock server stderr:", err)
    sys.exit(1)

try:
    # Prepare env
    env = os.environ.copy()
    env["CLOUDFLARE_API_TOKEN"] = "test-cf-token"
    env["CLOUDFLARE_ACCOUNT_ID"] = "test-account-id"
    env["CLOUDFLARE_ZONE_ID"] = "test-zone-id"
    env["CLOUDFLARE_API_URL"] = "http://127.0.0.1:8001/client/v4"
    env["CLOUDFLARE_DOMAIN"] = "test.example.com"
    env["PRIMARY_ORIGIN_ADDRESS"] = "primary.test.com"
    env["BACKUP_ORIGIN_ADDRESS"] = "backup.test.com"
    
    script_path = os.path.abspath("scripts/cloudflare_failover_setup.py")
    print("Running setup script...")
    res_script = subprocess.run(
        [sys.executable, script_path],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    print("Script returncode:", res_script.returncode)
    print("Script stdout:", res_script.stdout)
    print("Script stderr:", res_script.stderr)

    # Get history
    history_resp = requests.get("http://127.0.0.1:8001/control/history").json()
    print("Mock Server History:")
    print(json.dumps(history_resp, indent=2))

finally:
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except:
        proc.kill()
