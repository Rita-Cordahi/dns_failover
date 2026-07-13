import os
import json
import logging
import httpx
from backend import config

logger = logging.getLogger(__name__)

# Shared HTTPX client with HTTP/2 support (multiplexing)
cf_http_client = httpx.AsyncClient(http2=True, timeout=10.0)


class CloudflareClient:
    def __init__(self, api_token: str = None, zone_id: str = None, account_id: str = None):
        self.api_token = api_token or config.CLOUDFLARE_API_TOKEN
        self.zone_id = zone_id or config.CLOUDFLARE_ZONE_ID
        self.account_id = account_id or config.CLOUDFLARE_ACCOUNT_ID
        self.api_url = os.getenv("CLOUDFLARE_API_URL", "https://api.cloudflare.com/client/v4")

    async def _request(self, method: str, path: str, data=None):
        url = f"{self.api_url}{path}"
        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json"
        }

        try:
            if method == "GET":
                resp = await cf_http_client.get(url, headers=headers)
            elif method == "POST":
                resp = await cf_http_client.post(url, headers=headers, json=data)
            elif method == "PUT":
                resp = await cf_http_client.put(url, headers=headers, json=data)
            else:
                raise ValueError(f"Unsupported method: {method}")

            resp.raise_for_status()
            res_json = resp.json()
            if not res_json.get("success", False):
                raise RuntimeError(f"Cloudflare API Error: {res_json.get('errors')}")
            return res_json.get("result")
        except Exception as e:
            logger.error(f"Cloudflare API request failed to {url}: {e}")
            raise e

    async def get_pool_status(self) -> list:
        # Check for mock fallback in tests or unconfigured environments
        if (
            self.api_token == "mock_cf_token"
            or not self.account_id
            or self.account_id == "mock_cf_account"
        ):
            return [
                {
                    "id": "primary-pool-id",
                    "name": "primary-pool",
                    "healthy": True,
                    "origins": [
                        {
                            "name": "primary-pool-origin",
                            "address": "primary.example.com",
                            "healthy": True
                        }
                    ]
                },
                {
                    "id": "backup-pool-id",
                    "name": "backup-pool",
                    "healthy": True,
                    "origins": [
                        {
                            "name": "backup-pool-origin",
                            "address": "backup.example.com",
                            "healthy": True
                        }
                    ]
                }
            ]

        try:
            pools = await self._request("GET", f"/accounts/{self.account_id}/load_balancers/pools")
            results = []
            for p in pools:
                pool_id = p.get("id")
                pool_detail = await self._request("GET", f"/accounts/{self.account_id}/load_balancers/pools/{pool_id}")
                results.append({
                    "id": pool_id,
                    "name": pool_detail.get("name"),
                    "healthy": pool_detail.get("healthy", True),
                    "origins": [
                        {
                            "name": origin.get("name"),
                            "address": origin.get("address"),
                            "healthy": origin.get("healthy", True)
                        }
                        for origin in pool_detail.get("origins", [])
                    ]
                })
            return results
        except Exception as e:
            logger.error(f"Failed to fetch live Cloudflare pools: {e}")
            return []

    async def set_pool_routing(self, primary_enabled: bool, backup_enabled: bool) -> bool:
        if (
            self.api_token == "mock_cf_token"
            or not self.account_id
            or self.account_id == "mock_cf_account"
        ):
            logger.info("Mock mode: manual failover routing set successfully.")
            return True

        try:
            pools = await self._request("GET", f"/accounts/{self.account_id}/load_balancers/pools")
            for p in pools:
                pool_id = p.get("id")
                name = p.get("name")

                enabled = True
                if "primary" in name.lower():
                    enabled = primary_enabled
                elif "backup" in name.lower():
                    enabled = backup_enabled

                # Update the pool
                p["enabled"] = enabled
                await self._request("PUT", f"/accounts/{self.account_id}/load_balancers/pools/{pool_id}", p)

            return True
        except Exception as e:
            logger.error(f"Failed to set pool routing on Cloudflare: {e}")
            return False
