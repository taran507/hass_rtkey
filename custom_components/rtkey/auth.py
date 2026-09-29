import asyncio
import functools
import json
import time
from datetime import datetime

import requests
from homeassistant.core import HomeAssistant

TOKEN_REFRESH_BUFFER = 300


class RTKeyAuth:
    def __init__(self, hass: HomeAssistant, login: str | None, password: str | None, device_id: str) -> None:
        self.hass = hass
        self.login = login
        self.password = password
        self.device_id = device_id
        self.token = None
        self.token_expires_at = 0
        self.lock = asyncio.Lock()

    async def get_token(self) -> tuple[str, str]:
        async with self.lock:
            if not self.login or not self.password:
                raise ValueError("RTKey login and password are required")
            if time.time() >= self.token_expires_at - TOKEN_REFRESH_BUFFER:
                response = await self.hass.async_add_executor_job(
                    functools.partial(
                        requests.post,
                        "https://keyapis.key.rt.ru/identity/api/v1/authorization/login_by_password",
                        headers={
                            "X-Device-Id": self.device_id,
                            "Content-Type": "text/plain",
                        },
                        data=json.dumps({"phoneNumber": self.login, "password": self.password}),
                        timeout=15,
                    )
                )
                response.raise_for_status()
                data = response.json()["data"]
                expires_at = datetime.fromisoformat(data["expiredAt"].replace("Z", "+00:00"))
                self.token = data["accessToken"]
                self.token_expires_at = expires_at.timestamp()
            return "Bearer", self.token