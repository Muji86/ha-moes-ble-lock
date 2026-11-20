"""Config flow: pick the lock, get its keys (Tuya developer account or by hand), verify them.

Cloud path: log in to the Tuya IoT platform once, find the lock whose uuid matches the one in
the discovered lock's advert, take its local key and check code, then verify with a real
Bluetooth login (never unlocks). The cloud is not used after setup.
"""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_ACCESS_ID,
    CONF_ACCESS_SECRET,
    CONF_CHECK_CODE,
    CONF_DEVICE_ID,
    CONF_IDLE_TIMEOUT,
    CONF_LOCAL_KEY,
    CONF_POLL_INTERVAL,
    CONF_UUID,
    DEFAULT_IDLE_TIMEOUT,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
)
from .device import CONNECT_ERRORS, LockUnavailable, make_connector
from .lib import protocol as p
from .lib.client import TuyaBleLock
from .lib.cloud import CloudAuthError, CloudError, CloudLock, find_locks

_LOGGER = logging.getLogger(__name__)

CRED_KEYS = (CONF_LOCAL_KEY, CONF_UUID, CONF_DEVICE_ID, CONF_CHECK_CODE)
CONF_LOCK = "lock"


def _cred_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    d = defaults or {}
    return vol.Schema({vol.Required(k, default=d.get(k, vol.UNDEFINED)): str for k in CRED_KEYS})


def _clean(user_input: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    data = {k: str(user_input[k]) for k in CRED_KEYS}
    # local_key may legitimately contain spaces/symbols; only trim the others
    for k in (CONF_UUID, CONF_DEVICE_ID, CONF_CHECK_CODE):
        data[k] = data[k].strip()
    errors: dict[str, str] = {}
    if len(data[CONF_LOCAL_KEY]) < 6:
        errors[CONF_LOCAL_KEY] = "invalid_local_key"
    if len(data[CONF_CHECK_CODE]) != 8:
        errors[CONF_CHECK_CODE] = "invalid_check_code"
    return data, errors


def _lock_data(lock: CloudLock) -> dict[str, str]:
    return {
        CONF_LOCAL_KEY: lock.local_key,
        CONF_UUID: lock.uuid,
        CONF_DEVICE_ID: lock.device_id,
        CONF_CHECK_CODE: lock.check_code or "",
    }


class MoesBleLockConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._address: str | None = None
        self._discovered: dict[str, str] = {}
        self._cloud: dict[str, str] = {}
        self._cloud_locks: list[CloudLock] = []
        self._prefill: dict[str, str] = {}
        self._title: str | None = None

    # ------------------------------------------------------------------ helpers
    async def _validate(self, address: str, data: dict[str, str]) -> str | None:
        """Connect + authenticate once (never unlocks). Returns an error key or None."""
        creds = p.Credentials(data[CONF_LOCAL_KEY], data[CONF_UUID], data[CONF_DEVICE_ID],
                              data[CONF_CHECK_CODE])
        lock = TuyaBleLock(address, creds)
        try:
            await lock.connect(connector=make_connector(self.hass, address, "MOES lock setup"))
        except p.AuthError:
            return "invalid_auth"
        except LockUnavailable:
            return "not_advertising"
        except CONNECT_ERRORS as err:
            _LOGGER.debug("validation connect failed: %r", err)
            return "cannot_connect"
        except Exception:  # noqa: BLE001
            _LOGGER.exception("unexpected error validating lock")
            return "unknown"
        finally:
            await lock.disconnect()
        return None

    def _advert_uuid(self, address: str) -> str | None:
        info = bluetooth.async_last_service_info(self.hass, address, connectable=True)
        if info is None:
            return None
        sd = info.service_data.get(p.SERVICE_UUID)
        md = info.manufacturer_data.get(p.TUYA_COMPANY_ID)
        return p.uuid_from_advertisement(sd, md) if sd and md else None

    def _saved_cloud(self) -> dict[str, str]:
        """Developer-account credentials from a lock that is already set up, if any."""
        for entry in self._async_current_entries(include_ignore=False):
            if entry.data.get(CONF_ACCESS_ID):
                return {CONF_ACCESS_ID: entry.data[CONF_ACCESS_ID],
                        CONF_ACCESS_SECRET: entry.data[CONF_ACCESS_SECRET]}
        return {}

    async def _fetch(self, cloud: dict[str, str]) -> tuple[str | None, dict[str, str]]:
        """Fetch locks from the cloud. Returns (error key, description placeholders)."""
        try:
            _endpoint, locks = await find_locks(
                async_get_clientsession(self.hass), cloud[CONF_ACCESS_ID],
                cloud[CONF_ACCESS_SECRET])
        except CloudAuthError:
            return "invalid_cloud_auth", {}
        except (CloudError, aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.warning("Tuya cloud request failed: %s", err)
            return "cloud_error", {"error": str(err)}
        if not locks:
            return "no_cloud_locks", {}
        self._cloud, self._cloud_locks = cloud, locks
        return None, {}

    async def _finish_with(self, lock: CloudLock) -> ConfigFlowResult:
        """Verify the fetched keys on the real lock and create the entry."""
        data = _lock_data(lock)
        self._title = lock.name
        if not lock.check_code:
            # Lock never unlocked over Bluetooth yet: ask for the code, rest pre-filled.
            self._prefill = data
            return await self.async_step_credentials()
        if err := await self._validate(self._address, data):
            self._prefill = data
            return await self.async_step_credentials(error=err)
        return self.async_create_entry(
            title=lock.name, data={CONF_ADDRESS: self._address, **data, **self._cloud})

    # ------------------------------------------------------------------ discovery / start
    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._address = discovery_info.address
        name = discovery_info.name if discovery_info.name not in (None, "", "TY") else "Tuya lock"
        self.context["title_placeholders"] = {"name": f"{name} {discovery_info.address}"}
        return await self.async_step_method()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            self._address = address
            return await self.async_step_method()

        current = self._async_current_ids()
        for info in bluetooth.async_discovered_service_info(self.hass, connectable=True):
            if p.SERVICE_UUID in info.service_uuids and info.address not in current:
                self._discovered[info.address] = f"{info.name or 'Tuya BLE'} ({info.address})"
        if not self._discovered:
            return self.async_abort(reason="no_devices_found")
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_ADDRESS): vol.In(self._discovered)}),
        )

    async def async_step_method(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        # A developer account is already saved from another lock: use it straight away.
        if (saved := self._saved_cloud()) and not (await self._fetch(saved))[0]:
            return await self._match()
        return self.async_show_menu(
            step_id="method",
            menu_options=["cloud", "credentials"],
            description_placeholders={"address": self._address},
        )

    # ------------------------------------------------------------------ cloud
    async def async_step_cloud(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {"error": ""}
        if user_input is not None:
            cloud = {CONF_ACCESS_ID: user_input[CONF_ACCESS_ID].strip(),
                     CONF_ACCESS_SECRET: user_input[CONF_ACCESS_SECRET].strip()}
            err, extra = await self._fetch(cloud)
            if err is None:
                return await self._match()
            errors["base"] = err
            placeholders.update(extra)
        d = user_input or self._saved_cloud()
        return self.async_show_form(
            step_id="cloud",
            data_schema=vol.Schema({
                vol.Required(CONF_ACCESS_ID, default=d.get(CONF_ACCESS_ID, vol.UNDEFINED)): str,
                vol.Required(CONF_ACCESS_SECRET,
                             default=d.get(CONF_ACCESS_SECRET, vol.UNDEFINED)): str,
            }),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def _match(self) -> ConfigFlowResult:
        """Pick the cloud lock that is this discovered lock (by the uuid in its advert)."""
        if uuid := self._advert_uuid(self._address):
            for lock in self._cloud_locks:
                if lock.uuid == uuid:
                    return await self._finish_with(lock)
            return self.async_abort(reason="not_in_account")
        # No advert data cached (e.g. the lock is busy): let the user pick.
        return await self.async_step_cloud_pick()

    async def async_step_cloud_pick(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        configured = {e.data.get(CONF_DEVICE_ID) for e in self._async_current_entries()}
        choices = {lk.device_id: lk.name for lk in self._cloud_locks
                   if lk.device_id not in configured}
        if user_input is not None:
            lock = next(lk for lk in self._cloud_locks if lk.device_id == user_input[CONF_LOCK])
            return await self._finish_with(lock)
        if not choices:
            return self.async_abort(reason="no_cloud_locks")
        return self.async_show_form(
            step_id="cloud_pick",
            data_schema=vol.Schema({vol.Required(CONF_LOCK): vol.In(choices)}),
            description_placeholders={"address": self._address},
        )

    # ------------------------------------------------------------------ manual
    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None, error: str | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {"base": error} if error else {}
        if user_input is not None:
            data, errors = _clean(user_input)
            if not errors and (err := await self._validate(self._address, data)):
                errors["base"] = err
            if not errors:
                return self.async_create_entry(
                    title=self._title or f"MOES lock {self._address[-5:]}",
                    data={CONF_ADDRESS: self._address, **data, **self._cloud},
                )
        return self.async_show_form(
            step_id="credentials",
            data_schema=_cred_schema(user_input or self._prefill),
            errors=errors,
            description_placeholders={"address": self._address},
        )

    # ------------------------------------------------------------------ reauth
    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        # Keys changed (lock re-paired in Smart Life): re-fetch them if we have the account.
        if entry.data.get(CONF_ACCESS_ID):
            cloud = {CONF_ACCESS_ID: entry.data[CONF_ACCESS_ID],
                     CONF_ACCESS_SECRET: entry.data[CONF_ACCESS_SECRET]}
            if not (await self._fetch(cloud))[0]:
                lock = next((lk for lk in self._cloud_locks
                             if lk.device_id == entry.data[CONF_DEVICE_ID]), None)
                if lock and lock.check_code:
                    data = _lock_data(lock)
                    if not await self._validate(entry.data[CONF_ADDRESS], data):
                        return self.async_update_reload_and_abort(entry, data_updates=data)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = _clean(user_input)
            if not errors and (err := await self._validate(entry.data[CONF_ADDRESS], data)):
                errors["base"] = err
            if not errors:
                return self.async_update_reload_and_abort(entry, data_updates=data)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_cred_schema(user_input),
            errors=errors,
            description_placeholders={"address": entry.data[CONF_ADDRESS]},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return MoesOptionsFlow()


class MoesOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        o = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(CONF_IDLE_TIMEOUT, default=o.get(CONF_IDLE_TIMEOUT,
                                                              DEFAULT_IDLE_TIMEOUT)):
                    vol.All(vol.Coerce(int), vol.Range(min=0, max=3600)),
                vol.Required(CONF_POLL_INTERVAL, default=o.get(CONF_POLL_INTERVAL,
                                                               DEFAULT_POLL_INTERVAL)):
                    vol.All(vol.Coerce(int), vol.Range(min=5, max=1440)),
            }),
        )
