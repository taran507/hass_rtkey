import asyncio
import functools
import json
import logging
import time
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import jwt
import requests
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from transliterate import translit

from .auth import RTKeyAuth

DOMAIN = "rtkey"

PLATFORMS: list[str] = [Platform.IMAGE, Platform.CAMERA, Platform.LOCK]

CONF_NAME = "name"
CONF_LOGIN = "login"
CONF_PASSWORD = "password"
CONF_DEVICE_ID = "device_id"
CONF_CAMERA_IMAGE_REFRESH_INTERVAL = "camera_image_refresh_interval"

DATA_SCHEMA = {
    vol.Required(CONF_NAME, default="Flat1"): str,
}

OPTIONS_SCHEMA = {
    vol.Required(CONF_LOGIN): str,
    vol.Required(CONF_PASSWORD): str,
    vol.Required(CONF_DEVICE_ID): str,
    vol.Required(CONF_CAMERA_IMAGE_REFRESH_INTERVAL, default=2): int,
}

_LOGGER = logging.getLogger(DOMAIN)
_LOGGER.setLevel(logging.INFO)

TOKEN_REFRESH_BUFFER = 300
RATE_LIMIT_DELAY = 120  # each api query will repeated only after this delay


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    _LOGGER.info("Setting up RTKey entry %s", config_entry.entry_id)
    hass.data[config_entry.entry_id] = {
        "cameras_api": RTKeyCamerasApi(hass, config_entry)
    }
    await hass.config_entries.async_forward_entry_setups(config_entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    res = await hass.config_entries.async_unload_platforms(config_entry, PLATFORMS)
    if res:
        del hass.data[config_entry.entry_id]
    return res


class RTKeyCamerasApi:
    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        self.hass = hass
        self.auth = RTKeyAuth(
            hass,
            config_entry.options.get(CONF_LOGIN),
            config_entry.options.get(CONF_PASSWORD),
            config_entry.options.get(CONF_DEVICE_ID),
        )
        self.config_entry_name = config_entry.data[CONF_NAME]
        self.lock = asyncio.Lock()
        self.cached_cameras_info = None
        self.cached_cameras_info_timestamp = None
        self.cached_camera_images = {}
        self.cached_intercoms_info = None
        self.cached_intercoms_info_timestamp = None
        self.camera_image_locks = {}
        self.camera_image_tasks = {}
        self.camera_image_refresh_interval = config_entry.options[
            CONF_CAMERA_IMAGE_REFRESH_INTERVAL
        ]

    async def get_cameras_info(self) -> dict:
        async with self.lock:
            if self.cached_cameras_info:
                _LOGGER.info("Using cached cameras info")
                return self.cached_cameras_info

            token_type, token = await self.auth.get_token()
            r = await self.hass.async_add_executor_job(
                functools.partial(
                    requests.get,
                    "https://vc.key.rt.ru/api/v1/cameras?limit=100&offset=0",
                    headers={"Authorization": f"{token_type} {token}"},
                    allow_redirects=True,
                )
            )
            _LOGGER.info(r)
            _LOGGER.info(r.content)

            self.cached_cameras_info = json.loads(r.content)
            self.cached_cameras_info_timestamp = int(time.time())

            for camera_info in self.cached_cameras_info["data"]["items"]:
                decoded_screenshot_token = jwt.decode(
                    camera_info["screenshot_token"], options={"verify_signature": False}
                )
                camera_info["screenshot_token_exp"] = decoded_screenshot_token["exp"]

                decoded_streamer_token = jwt.decode(
                    camera_info["streamer_token"], options={"verify_signature": False}
                )
                camera_info["streamer_token_exp"] = decoded_streamer_token["exp"]

                camera_id = camera_info["id"]
                if camera_id not in self.cached_camera_images:
                    self.cached_camera_images[camera_id] = None
                if camera_id not in self.camera_image_locks:
                    self.camera_image_locks[camera_id] = asyncio.Lock()

            return self.cached_cameras_info

    async def clear_cached_cameras_info(self) -> None:
        async with self.lock:
            if self.cached_cameras_info:
                now = int(time.time())
                if (now - self.cached_cameras_info_timestamp) > RATE_LIMIT_DELAY:
                    self.cached_cameras_info = None
                    self.cached_cameras_info_timestamp = None

    async def get_camera_info(self, camera_id: str) -> dict | None:
        cameras_info = await self.get_cameras_info()
        for camera_info in cameras_info["data"]["items"]:
            if camera_info["id"] == camera_id:
                return camera_info
        return None

    async def get_camera_image(self, camera_id: str) -> bytes | None:
        camera_info = await self.get_camera_info(camera_id)

        now = int(time.time())
        if (
            camera_info
            and (camera_info["screenshot_token_exp"] - now) < TOKEN_REFRESH_BUFFER
        ):
            await self.clear_cached_cameras_info()
            camera_info = await self.get_camera_info(camera_id)

        if not camera_info:
            return None

        async with self.camera_image_locks[camera_id]:
            if self.cached_camera_images[camera_id]:
                _LOGGER.info("Using cached image for camera %s", camera_id)
                return self.cached_camera_images[camera_id]

            size = "large"
            url = camera_info["screenshot_url_template"].format(
                timestamp=now, size=size, cdn_token=camera_info["screenshot_token"]
            )
            _LOGGER.info("Fetching %s", url)
            r = await self.hass.async_add_executor_job(
                functools.partial(
                    requests.get,
                    url,
                    allow_redirects=True,
                    headers={"X-UTOKEN": camera_info["user_token"]},
                )
            )
            _LOGGER.info(r)

            self.cached_camera_images[camera_id] = r.content
            self.camera_image_tasks[camera_id] = asyncio.create_task(
                self.clear_cached_camera_image(
                    camera_id, self.camera_image_refresh_interval
                )
            )

            return r.content

    async def get_camera_stream_url(self, camera_id: str) -> str | None:
        camera_info = await self.get_camera_info(camera_id)

        now = int(time.time())
        if (
            camera_info
            and (camera_info["streamer_token_exp"] - now) < TOKEN_REFRESH_BUFFER
        ):
            await self.clear_cached_cameras_info()
            camera_info = await self.get_camera_info(camera_id)

        if not camera_info:
            return None

        stream_url = urlparse(camera_info["streamer_url"])
        query = dict(parse_qsl(stream_url.query))
        query.update(
            {
                "mp4-fragment-length": "0.5",
                "mp4-use-speed": "0",
                "mp4-afiller": "1",
                "token": camera_info["streamer_token"],
            }
        )
        return urlunparse(stream_url._replace(query=urlencode(query)))

    async def clear_cached_camera_image(self, camera_id: str, ttl: int) -> None:
        await asyncio.sleep(ttl)
        async with self.camera_image_locks[camera_id]:
            self.cached_camera_images[camera_id] = None
        _LOGGER.info("Deleted cached image for camera %s", camera_id)

    def build_device_name(self, device_title) -> str:
        device_name = device_title.lower()
        device_name = f"{self.config_entry_name} {device_name}"
        device_name = translit(device_name, "ru", reversed=True)
        return device_name.capitalize()

    async def get_intercoms_info(self) -> dict:
        async with self.lock:
            if self.cached_intercoms_info:
                _LOGGER.info("Using cached intercoms info")
                return self.cached_intercoms_info

            token_type, token = await self.auth.get_token()
            r = await self.hass.async_add_executor_job(
                functools.partial(
                    requests.get,
                    "https://household.key.rt.ru/api/v2/app/devices/intercom",
                    headers={"Authorization": f"{token_type} {token}"},
                    allow_redirects=True,
                )
            )
            _LOGGER.info(r)
            _LOGGER.info(r.content)

            self.cached_intercoms_info = json.loads(r.content)
            self.cached_intercoms_info_timestamp = int(time.time())

            return self.cached_intercoms_info

    async def open_intercom(self, intercom_id) -> None:
        async with self.lock:
            url = f"https://household.key.rt.ru/api/v2/app/devices/{intercom_id}/open"
            _LOGGER.info("Fetching %s", url)
            token_type, token = await self.auth.get_token()
            r = await self.hass.async_add_executor_job(
                functools.partial(
                    requests.post,
                    url,
                    allow_redirects=True,
                    headers={"Authorization": f"{token_type} {token}"},
                )
            )
            _LOGGER.info(r)
