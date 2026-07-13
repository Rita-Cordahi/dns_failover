import os
import sys
import subprocess
import requests

def run_setup_script(env):
    script_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "scripts",
        "cloudflare_failover_setup.py"
    )
    env = env.copy()
    env["http_proxy"] = ""
    env["https_proxy"] = ""
    env["HTTP_PROXY"] = ""
    env["HTTPS_PROXY"] = ""
    env["no_proxy"] = "*"
    env["NO_PROXY"] = "*"
    
    result = subprocess.run(
        [sys.executable, script_path],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    return result

def test_setup_first_run(reset_mock_server):
    env = os.environ.copy()
    env["CLOUDFLARE_API_TOKEN"] = "test-cf-token"
    env["CLOUDFLARE_ACCOUNT_ID"] = "test-account-id"
    env["CLOUDFLARE_ZONE_ID"] = "test-zone-id"
    env["CLOUDFLARE_API_URL"] = "http://127.0.0.1:8001/client/v4"
    env["CLOUDFLARE_DOMAIN"] = "test.example.com"
    env["PRIMARY_ORIGIN_ADDRESS"] = "primary.test.com"
    env["BACKUP_ORIGIN_ADDRESS"] = "backup.test.com"
    
    res = run_setup_script(env)
    assert res.returncode == 0
    output = res.stdout + res.stderr
    assert "applied successfully" in output
    
    # Retrieve mock server state
    history_resp = requests.get("http://127.0.0.1:8001/control/history").json()
    history = history_resp["history"]
    
    # Expect 4 POST requests corresponding to 1 monitor, 2 pools, 1 load balancer
    post_methods = [req["method"] for req in history if req["method"] == "POST"]
    assert len(post_methods) == 4
    
    # Verify mock server stored them in its state
    assert len(history_resp["monitors"]) == 1
    assert len(history_resp["pools"]) == 2
    assert len(history_resp["load_balancers"]) == 1

def test_setup_second_run_updates_existing(reset_mock_server):
    env = os.environ.copy()
    env["CLOUDFLARE_API_TOKEN"] = "test-cf-token"
    env["CLOUDFLARE_ACCOUNT_ID"] = "test-account-id"
    env["CLOUDFLARE_ZONE_ID"] = "test-zone-id"
    env["CLOUDFLARE_API_URL"] = "http://127.0.0.1:8001/client/v4"
    env["CLOUDFLARE_DOMAIN"] = "test.example.com"
    env["PRIMARY_ORIGIN_ADDRESS"] = "primary.test.com"
    env["BACKUP_ORIGIN_ADDRESS"] = "backup.test.com"
    
    # First run (creates elements)
    res1 = run_setup_script(env)
    print("res1 returncode:", res1.returncode)
    print("res1 stdout:", res1.stdout)
    print("res1 stderr:", res1.stderr)
    assert res1.returncode == 0
    
    # Second run (updates elements)
    res2 = run_setup_script(env)
    print("res2 returncode:", res2.returncode)
    print("res2 stdout:", res2.stdout)
    print("res2 stderr:", res2.stderr)
    assert res2.returncode == 0
    output = res2.stdout + res2.stderr
    assert "updated successfully" in output
    
    # Retrieve mock server state
    history_resp = requests.get("http://127.0.0.1:8001/control/history").json()
    history = history_resp["history"]
    print("History from mock server:", history_resp)
    
    # Expect at least 4 PUT requests corresponding to updates
    put_methods = [req["method"] for req in history if req["method"] == "PUT"]
    assert len(put_methods) >= 4

def test_setup_billing_error(reset_mock_server):
    env = os.environ.copy()
    env["CLOUDFLARE_API_TOKEN"] = "test-cf-token"
    env["CLOUDFLARE_ACCOUNT_ID"] = "test-account-id"
    env["CLOUDFLARE_ZONE_ID"] = "test-zone-id"
    env["CLOUDFLARE_API_URL"] = "http://127.0.0.1:8001/client/v4"
    env["CLOUDFLARE_DOMAIN"] = "test.example.com"
    env["PRIMARY_ORIGIN_ADDRESS"] = "primary.test.com"
    env["BACKUP_ORIGIN_ADDRESS"] = "backup.test.com"

    # Inject billing failure mode into mock server
    fail_resp = requests.post("http://127.0.0.1:8001/control/failures", json={
        "cloudflare": {
            "error_code": 1211,
            "message": "Subscription plan limit exceeded",
            "status_code": 403
        }
    })
    assert fail_resp.status_code == 200, "Failed to inject billing failure into mock server"

    # Verify the failure is registered before running the setup script
    import time
    time.sleep(0.1)

    res = run_setup_script(env)
    output = res.stdout + res.stderr
    assert res.returncode == 1, (
        f"Expected returncode 1 (billing error), got {res.returncode}.\n"
        f"stdout: {res.stdout}\nstderr: {res.stderr}"
    )
    assert "Billing/Subscription error" in output or "billing" in output.lower(), (
        f"Expected billing error message in output: {output}"
    )
