"""Read-only Tuya IoT platform (developer account) client, used once at setup to fetch lock keys.

Only GET requests besides the token request. Uses the account's Access ID / Access Secret
(Tuya IoT platform → Cloud → project → Overview). Works with any aiohttp ClientSession.
"""
from __future__ import annotations

import hashlib
import hmac
import time
import uuid as _uuid
from dataclasses import dataclass
from typing import Any

import aiohttp

from .protocol import check_code_from_status

# Data centres, most likely first for European users; setup tries each until one has devices.
ENDPOINTS: dict[str, str] = {
    "eu": "https://openapi.tuyaeu.com",
    "eu_west": "https://openapi-weaz.tuyaeu.com",
    "us": "https://openapi.tuyaus.com",
    "us_east": "https://openapi-ueaz.tuyaus.com",
    "cn": "https://openapi.tuyacn.com",
    "in": "https://openapi.tuyain.com",
}

LOCK_CATEGORIES = {"jtmspro", "ms", "jtmsbh", "bxx", "gyms", "videolock", "photolock"}


class CloudError(Exception):
    """The Tuya cloud refused a request."""


class CloudAuthError(CloudError):
    """Access ID / Secret rejected."""


@dataclass(frozen=True)
class CloudLock:
    device_id: str
    name: str
    uuid: str
    local_key: str
    check_code: str | None
    category: str
    product_id: str
    product_name: str


def _sign(secret: str, client_id: str, token: str, t: str, nonce: str,
          method: str, path: str, body: bytes = b"") -> str:
    content_hash = hashlib.sha256(body).hexdigest()
    string_to_sign = f"{method}\n{content_hash}\n\n{path}"
    msg = client_id + token + t + nonce + string_to_sign
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest().upper()


class TuyaCloud:
    def __init__(self, session: aiohttp.ClientSession, access_id: str, access_secret: str,
                 endpoint: str) -> None:
        self._session = session
        self._id = access_id.strip()
        self._secret = access_secret.strip()
        self.endpoint = endpoint
        self._token = ""

    async def _request(self, path: str, token: str) -> dict[str, Any]:
        t = str(int(time.time() * 1000))
        nonce = _uuid.uuid4().hex
        headers = {
            "client_id": self._id,
            "sign": _sign(self._secret, self._id, token, t, nonce, "GET", path),
            "sign_method": "HMAC-SHA256",
            "t": t,
            "nonce": nonce,
        }
        if token:
            headers["access_token"] = token
        async with self._session.get(self.endpoint + path, headers=headers,
                                     timeout=aiohttp.ClientTimeout(total=15)) as resp:
            data = await resp.json(content_type=None)
        if not data.get("success"):
            code, msg = data.get("code"), data.get("msg", "")
            # 1004 sign invalid, 1106 permission/no such client, 2009 client id invalid
            if code in (1004, 1106, 2009, 1010, 1011):
                raise CloudAuthError(f"{code}: {msg}")
            raise CloudError(f"{code}: {msg}")
        return data

    async def login(self) -> None:
        data = await self._request("/v1.0/token?grant_type=1", "")
        self._token = data["result"]["access_token"]

    async def get(self, path: str) -> Any:
        return (await self._request(path, self._token))["result"]

    async def devices(self) -> list[dict[str, Any]]:
        """All devices of the app accounts linked to the cloud project."""
        out: list[dict[str, Any]] = []
        last = ""
        for _ in range(50):  # paging guard
            q = "/v1.0/iot-01/associated-users/devices?size=100"
            if last:
                q += f"&last_row_key={last}"
            res = await self.get(q)
            out.extend(res.get("devices", []))
            if not res.get("has_more"):
                break
            last = res.get("last_row_key", "")
        return out

    async def locks(self) -> list[CloudLock]:
        locks = []
        for d in await self.devices():
            if d.get("category") not in LOCK_CATEGORIES or not d.get("uuid"):
                continue
            status = {s.get("code"): s.get("value") for s in d.get("status", [])}
            # The v1.0 list's local_key is NOT the Bluetooth key for these locks (verified:
            # the lock rejects it). The v2.0 thing endpoint returns the key the lock was paired with.
            thing = await self.get(f"/v2.0/cloud/thing/{d['id']}")
            locks.append(CloudLock(
                device_id=d["id"], name=d.get("name") or d["id"], uuid=d["uuid"],
                local_key=thing.get("local_key", ""), check_code=check_code_from_status(status),
                category=d.get("category", ""), product_id=d.get("product_id", ""),
                product_name=d.get("product_name", "")))
        return locks


async def find_locks(session: aiohttp.ClientSession, access_id: str,
                     access_secret: str) -> tuple[str, list[CloudLock]]:
    """Try each data centre; return (endpoint, locks) for the first that knows the account."""
    auth_failures = 0
    last_err: Exception | None = None
    for endpoint in ENDPOINTS.values():
        cloud = TuyaCloud(session, access_id, access_secret, endpoint)
        try:
            await cloud.login()
            locks = await cloud.locks()
        except CloudAuthError as err:
            auth_failures += 1
            last_err = err
            continue
        except (CloudError, aiohttp.ClientError, TimeoutError) as err:
            last_err = err
            continue
        if locks:
            return endpoint, locks
    if auth_failures == len(ENDPOINTS):
        raise CloudAuthError(str(last_err))
    if last_err and not isinstance(last_err, CloudAuthError):
        raise CloudError(str(last_err))
    return "", []
