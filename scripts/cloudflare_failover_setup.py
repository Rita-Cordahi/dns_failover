#!/usr/bin/env python3
import os
import sys
import json
import logging
import urllib.request
import urllib.error

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)

class CloudflareBillingError(Exception):
    """Raised when there is a billing or subscription issue with Cloudflare."""
    pass

class CloudflareAPIError(Exception):
    """Raised for general Cloudflare API errors."""
    pass

class CloudflareFailoverManager:
    def __init__(self):
        self.api_token = os.getenv("CLOUDFLARE_API_TOKEN")
        self.account_id = os.getenv("CLOUDFLARE_ACCOUNT_ID")
        self.zone_id = os.getenv("CLOUDFLARE_ZONE_ID")
        
        # Validation
        missing = []
        if not self.api_token:
            missing.append("CLOUDFLARE_API_TOKEN")
        if not self.account_id:
            missing.append("CLOUDFLARE_ACCOUNT_ID")
        if not self.zone_id:
            missing.append("CLOUDFLARE_ZONE_ID")
            
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")
            
        self.api_url = os.getenv("CLOUDFLARE_API_URL", "https://api.cloudflare.com/client/v4")
        
    def request(self, method, path, data=None):
        url = f"{self.api_url}{path}"
        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json"
        }
        
        req_data = None
        if data is not None:
            req_data = json.dumps(data).encode("utf-8")
            
        req = urllib.request.Request(url, data=req_data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                res_body = response.read().decode("utf-8")
                res_json = json.loads(res_body)
                if not res_json.get("success", False):
                    self._handle_errors(res_json.get("errors", []))
                return res_json.get("result")
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8")
            try:
                res_json = json.loads(err_body)
                self._handle_errors(res_json.get("errors", []))
            except (json.JSONDecodeError, KeyError):
                pass
            raise CloudflareAPIError(f"HTTP Error {e.code}: {e.reason} - {err_body}")
        except urllib.error.URLError as e:
            raise CloudflareAPIError(f"Network error or timeout: {e.reason}")

    def _handle_errors(self, errors):
        for err in errors:
            code = err.get("code")
            message = err.get("message", "")
            # Identify billing/subscription errors
            billing_keywords = ["subscription", "billing", "upgrade", "plan", "pay", "payment", "entitlement", "tier"]
            if any(kw in message.lower() for kw in billing_keywords) or code in [1211, 1205, 81057]:
                logging.error(f"Billing/Subscription Error [{code}]: {message}")
                raise CloudflareBillingError(f"Cloudflare Billing/Subscription error: {message} (code: {code})")
            
        err_msgs = [f"[{e.get('code')}]: {e.get('message')}" for e in errors]
        raise CloudflareAPIError("; ".join(err_msgs))

    def get_monitors(self):
        logging.info("Fetching existing Monitors...")
        return self.request("GET", f"/accounts/{self.account_id}/load_balancers/monitors") or []

    def get_pools(self):
        logging.info("Fetching existing Pools...")
        return self.request("GET", f"/accounts/{self.account_id}/load_balancers/pools") or []

    def get_load_balancers(self):
        logging.info("Fetching existing Load Balancers...")
        return self.request("GET", f"/zones/{self.zone_id}/load_balancers") or []

    def setup_monitor(self):
        monitors = self.get_monitors()
        monitor_config = {
            "expected_body": "healthy",
            "expected_codes": "200",
            "method": "GET",
            "path": "/api/v1/health/failover",
            "type": "https",
            "port": 443,
            "interval": 60,
            "retries": 2,
            "timeout": 5,
            "description": "Failover Health Check Monitor"
        }
        
        existing = next((m for m in monitors if m.get("description") == monitor_config["description"]), None)
        
        if existing:
            monitor_id = existing["id"]
            logging.info(f"Monitor already exists (ID: {monitor_id}). Updating it...")
            result = self.request("PUT", f"/accounts/{self.account_id}/load_balancers/monitors/{monitor_id}", monitor_config)
            logging.info("Monitor updated successfully.")
            return result["id"]
        else:
            logging.info("Creating new Monitor...")
            result = self.request("POST", f"/accounts/{self.account_id}/load_balancers/monitors", monitor_config)
            logging.info(f"Monitor created successfully (ID: {result['id']}).")
            return result["id"]

    def setup_pool(self, name, description, address, monitor_id):
        pools = self.get_pools()
        pool_config = {
            "name": name,
            "description": description,
            "enabled": True,
            "monitor": monitor_id,
            "origins": [
                {
                    "name": f"{name}-origin",
                    "address": address,
                    "enabled": True,
                    "weight": 1
                }
            ]
        }
        
        existing = next((p for p in pools if p.get("name") == name), None)
        
        if existing:
            pool_id = existing["id"]
            logging.info(f"Pool '{name}' already exists (ID: {pool_id}). Updating it...")
            result = self.request("PUT", f"/accounts/{self.account_id}/load_balancers/pools/{pool_id}", pool_config)
            logging.info(f"Pool '{name}' updated successfully.")
            return result["id"]
        else:
            logging.info(f"Creating new Pool '{name}'...")
            result = self.request("POST", f"/accounts/{self.account_id}/load_balancers/pools", pool_config)
            logging.info(f"Pool '{name}' created successfully (ID: {result['id']}).")
            return result["id"]

    def setup_load_balancer(self, domain, default_pools, fallback_pool):
        lbs = self.get_load_balancers()
        lb_config = {
            "name": domain,
            "description": "Multi-region Failover Load Balancer",
            "proxied": True,
            "fallback_pool": fallback_pool,
            "default_pools": default_pools,
            "steering_policy": "geo"
        }
        
        existing = next((l for l in lbs if l.get("name") == domain), None)
        
        if existing:
            lb_id = existing["id"]
            logging.info(f"Load Balancer '{domain}' already exists (ID: {lb_id}). Updating it...")
            result = self.request("PUT", f"/zones/{self.zone_id}/load_balancers/{lb_id}", lb_config)
            logging.info(f"Load Balancer '{domain}' updated successfully.")
            return result["id"]
        else:
            logging.info(f"Creating new Load Balancer '{domain}'...")
            result = self.request("POST", f"/zones/{self.zone_id}/load_balancers", lb_config)
            logging.info(f"Load Balancer '{domain}' created successfully (ID: {result['id']}).")
            return result["id"]

    def run(self):
        domain = os.getenv("CLOUDFLARE_DOMAIN", "failover.example.com")
        primary_addr = os.getenv("PRIMARY_ORIGIN_ADDRESS", "primary.example.com")
        backup_addr = os.getenv("BACKUP_ORIGIN_ADDRESS", "backup.example.com")
        
        logging.info(f"Starting Cloudflare DNS Failover Setup for domain: {domain}")
        
        # 1. Setup Monitor
        monitor_id = self.setup_monitor()
        
        # 2. Setup Primary Pool
        primary_pool_id = self.setup_pool(
            name="primary-pool",
            description="Primary Region origin pool",
            address=primary_addr,
            monitor_id=monitor_id
        )
        
        # 3. Setup Backup Pool
        backup_pool_id = self.setup_pool(
            name="backup-pool",
            description="Backup Region origin pool (failover)",
            address=backup_addr,
            monitor_id=monitor_id
        )
        
        # 4. Setup Load Balancer
        lb_id = self.setup_load_balancer(
            domain=domain,
            default_pools=[primary_pool_id, backup_pool_id],
            fallback_pool=backup_pool_id
        )
        
        logging.info("Cloudflare DNS Failover configuration applied successfully.")
        return {
            "monitor_id": monitor_id,
            "primary_pool_id": primary_pool_id,
            "backup_pool_id": backup_pool_id,
            "load_balancer_id": lb_id
        }

if __name__ == "__main__":
    try:
        manager = CloudflareFailoverManager()
        manager.run()
    except Exception as e:
        logging.error(f"Setup failed: {e}")
        sys.exit(1)
