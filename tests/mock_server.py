import json
import uuid
import threading
from fastapi import FastAPI, Request, Response, HTTPException

app = FastAPI(title="Mock External API Server")

# Thread-safe global state
lock = threading.Lock()
state = {
    "monitors": {},       # id -> dict
    "pools": {},          # id -> dict
    "load_balancers": {}, # id -> dict
    "pool_statuses": {},  # pool_id -> dict
    "history": [],        # list of logged requests
    "failures": {}        # service -> failure config
}

@app.middleware("http")
async def log_request_middleware(request: Request, call_next):
    path = request.url.path
    if not path.startswith("/control"):
        body = b""
        if request.method in ("POST", "PUT", "PATCH"):
            body = await request.body()
        body_str = body.decode("utf-8")
        try:
            body_json = json.loads(body_str) if body_str else None
        except Exception:
            body_json = body_str

        req_log = {
            "method": request.method,
            "path": path,
            "headers": dict(request.headers),
            "body": body_json
        }
        with lock:
            state["history"].append(req_log)

    response = await call_next(request)
    return response

def check_cloudflare_failure():
    with lock:
        cf_fail = state["failures"].get("cloudflare")
        if cf_fail:
            if cf_fail.get("timeout"):
                import time
                time.sleep(15)
                raise HTTPException(status_code=504, detail="Gateway Timeout")
            if cf_fail.get("error_code"):
                code = cf_fail["error_code"]
                msg = cf_fail.get("message", "Simulated Cloudflare error")
                status_code = cf_fail.get("status_code", 400)
                return Response(
                    status_code=status_code,
                    content=json.dumps({
                        "success": False,
                        "errors": [{"code": code, "message": msg}],
                        "messages": [],
                        "result": None
                    }),
                    media_type="application/json"
                )
    return None

# Cloudflare API Mock endpoints
@app.get("/client/v4/accounts/{account_id}/load_balancers/monitors")
async def get_monitors(account_id: str):
    fail_resp = check_cloudflare_failure()
    if fail_resp:
        return fail_resp
    with lock:
        res_list = list(state["monitors"].values())
    return {"success": True, "errors": [], "messages": [], "result": res_list}

@app.post("/client/v4/accounts/{account_id}/load_balancers/monitors")
async def create_monitor(account_id: str, request: Request):
    fail_resp = check_cloudflare_failure()
    if fail_resp:
        return fail_resp
    req_json = await request.json()
    with lock:
        m_id = f"monitor-{uuid.uuid4().hex}"
        monitor = {**req_json, "id": m_id}
        state["monitors"][m_id] = monitor
    return {"success": True, "errors": [], "messages": [], "result": monitor}

@app.put("/client/v4/accounts/{account_id}/load_balancers/monitors/{monitor_id}")
async def update_monitor(account_id: str, monitor_id: str, request: Request):
    fail_resp = check_cloudflare_failure()
    if fail_resp:
        return fail_resp
    req_json = await request.json()
    with lock:
        if monitor_id not in state["monitors"]:
            raise HTTPException(status_code=404, detail="Monitor not found")
        monitor = {**req_json, "id": monitor_id}
        state["monitors"][monitor_id] = monitor
    return {"success": True, "errors": [], "messages": [], "result": monitor}

@app.get("/client/v4/accounts/{account_id}/load_balancers/pools")
async def get_pools(account_id: str):
    fail_resp = check_cloudflare_failure()
    if fail_resp:
        return fail_resp
    with lock:
        res_list = list(state["pools"].values())
    return {"success": True, "errors": [], "messages": [], "result": res_list}

@app.post("/client/v4/accounts/{account_id}/load_balancers/pools")
async def create_pool(account_id: str, request: Request):
    fail_resp = check_cloudflare_failure()
    if fail_resp:
        return fail_resp
    req_json = await request.json()
    with lock:
        p_id = f"pool-{uuid.uuid4().hex}"
        pool = {**req_json, "id": p_id}
        state["pools"][p_id] = pool
    return {"success": True, "errors": [], "messages": [], "result": pool}

@app.put("/client/v4/accounts/{account_id}/load_balancers/pools/{pool_id}")
async def update_pool(account_id: str, pool_id: str, request: Request):
    fail_resp = check_cloudflare_failure()
    if fail_resp:
        return fail_resp
    req_json = await request.json()
    with lock:
        if pool_id not in state["pools"]:
            raise HTTPException(status_code=404, detail="Pool not found")
        pool = {**req_json, "id": pool_id}
        state["pools"][pool_id] = pool
    return {"success": True, "errors": [], "messages": [], "result": pool}

@app.get("/client/v4/accounts/{account_id}/load_balancers/pools/{pool_id}")
async def get_pool(account_id: str, pool_id: str):
    fail_resp = check_cloudflare_failure()
    if fail_resp:
        return fail_resp
    with lock:
        if pool_id not in state["pools"]:
            raise HTTPException(status_code=404, detail="Pool not found")
        pool = state["pools"][pool_id]
        status_info = state["pool_statuses"].get(pool_id, {"healthy": True})
        pool_data = {**pool, **status_info}
    return {"success": True, "errors": [], "messages": [], "result": pool_data}

@app.get("/client/v4/accounts/{account_id}/load_balancers/pools/{pool_id}/health")
async def get_pool_health(account_id: str, pool_id: str):
    fail_resp = check_cloudflare_failure()
    if fail_resp:
        return fail_resp
    with lock:
        status_info = state["pool_statuses"].get(pool_id, {"healthy": True})
    return {"success": True, "errors": [], "messages": [], "result": status_info}

@app.get("/client/v4/zones/{zone_id}/load_balancers")
async def get_load_balancers(zone_id: str):
    fail_resp = check_cloudflare_failure()
    if fail_resp:
        return fail_resp
    with lock:
        res_list = list(state["load_balancers"].values())
    return {"success": True, "errors": [], "messages": [], "result": res_list}

@app.post("/client/v4/zones/{zone_id}/load_balancers")
async def create_load_balancer(zone_id: str, request: Request):
    fail_resp = check_cloudflare_failure()
    if fail_resp:
        return fail_resp
    req_json = await request.json()
    with lock:
        lb_id = f"lb-{uuid.uuid4().hex}"
        lb = {**req_json, "id": lb_id}
        state["load_balancers"][lb_id] = lb
    return {"success": True, "errors": [], "messages": [], "result": lb}

@app.put("/client/v4/zones/{zone_id}/load_balancers/{lb_id}")
async def update_load_balancer(zone_id: str, lb_id: str, request: Request):
    fail_resp = check_cloudflare_failure()
    if fail_resp:
        return fail_resp
    req_json = await request.json()
    with lock:
        if lb_id not in state["load_balancers"]:
            raise HTTPException(status_code=404, detail="Load balancer not found")
        lb = {**req_json, "id": lb_id}
        state["load_balancers"][lb_id] = lb
    return {"success": True, "errors": [], "messages": [], "result": lb}


# Discord Webhook Mock
@app.post("/discord/{path:path}")
async def discord_webhook(path: str, request: Request):
    return Response(status_code=204)


# Brevo SMTP Email API Mock
@app.post("/brevo/v3/smtp/email")
@app.post("/v3/smtp/email")
async def brevo_email(request: Request):
    return Response(
        status_code=201,
        content=json.dumps({"messageId": f"mock-msg-{uuid.uuid4().hex}"}),
        media_type="application/json"
    )


# Control Interface
@app.post("/control/reset")
async def control_reset():
    with lock:
        state["monitors"].clear()
        state["pools"].clear()
        state["load_balancers"].clear()
        state["pool_statuses"].clear()
        state["history"].clear()
        state["failures"].clear()
    return {"status": "ok"}

@app.post("/control/failures")
async def control_failures(request: Request):
    req_json = await request.json()
    with lock:
        state["failures"].update(req_json)
    return {"status": "ok"}

@app.get("/control/history")
async def control_history():
    with lock:
        history_copy = list(state["history"])
        monitors_copy = dict(state["monitors"])
        pools_copy = dict(state["pools"])
        load_balancers_copy = dict(state["load_balancers"])
        pool_statuses_copy = dict(state["pool_statuses"])
    return {
        "history": history_copy,
        "monitors": monitors_copy,
        "pools": pools_copy,
        "load_balancers": load_balancers_copy,
        "pool_statuses": pool_statuses_copy
    }

@app.post("/control/cloudflare/pools/{pool_id}/status")
async def set_pool_status(pool_id: str, request: Request):
    req_json = await request.json()
    with lock:
        state["pool_statuses"][pool_id] = req_json
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    import os
    port = int(os.getenv("MOCK_SERVER_PORT", "8001"))
    uvicorn.run(app, host="127.0.0.1", port=port)
