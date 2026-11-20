"""Config flow: pick the lock, enter its Tuya keys, verify with a real handshake."""
from __future__ import annotations

import logging
from typing import Any

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

from .const import (
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

_LOGGER = logging.getLogger(__name__)

CRED_KEYS = (CONF_LOCAL_KEY, CONF_UUID, CONF_DEVICE_ID, CONF_CHECK_CODE)


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


class MoesBleLockConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._address: str | None = None
        self._discovered: dict[str, str] = {}

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

    # ------------------------------------------------------------------ discovery
    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._address = discovery_info.address
        self.context["title_placeholders"] = {"name": f"Tuya BLE {discovery_info.address}"}
        return await self.async_step_credentials()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            self._address = address
            return await self.async_step_credentials()

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

    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = _clean(user_input)
            if not errors and (err := await self._validate(self._address, data)):
                errors["base"] = err
            if not errors:
                return self.async_create_entry(
                    title=f"MOES lock {self._address[-5:]}",
                    data={CONF_ADDRESS: self._address, **data},
                )
        return self.async_show_form(
            step_id="credentials",
            data_schema=_cred_schema(user_input),
            errors=errors,
            description_placeholders={"address": self._address},
        )

    # ------------------------------------------------------------------ reauth
    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
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
