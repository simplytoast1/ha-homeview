"""Config flow for Apple HomeView."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

from pyatv.conf import AppleTV as AppleTVConfig
from pyatv.const import Protocol
import voluptuous as vol

from homeassistant.config_entries import SOURCE_REAUTH, ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS, CONF_NAME, CONF_PIN
from homeassistant.core import callback
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .const import (
    CLIENT_NAME,
    CONF_CREDENTIALS,
    CONF_IDENTIFIER,
    CONF_MODEL,
    CONF_OUTPUT_ID,
    DOMAIN,
    is_homeview_model,
    model_name,
)
from .manager import async_scan_device
from .pairing import (
    AIRPLAY_PORT,
    HomeViewPairing,
    InvalidPinError,
    PairingBackoffError,
    PairingError,
    PairingRejectedError,
)

_LOGGER = logging.getLogger(__name__)

CONF_DEVICE = "device"


def _airplay_properties(conf: AppleTVConfig) -> Mapping[str, str]:
    service = conf.get_service(Protocol.AirPlay)
    return service.properties if service else {}


def _is_homeview(conf: AppleTVConfig) -> bool:
    return is_homeview_model(_airplay_properties(conf).get("model"))


def _device_id(conf: AppleTVConfig) -> str | None:
    if device_id := _airplay_properties(conf).get("deviceid"):
        return device_id.upper()
    return None


class HomeViewConfigFlow(ConfigFlow, domain=DOMAIN):
    """Pair a HomeView with Home Assistant."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._address: str | None = None
        self._name: str | None = None
        self._port: int = AIRPLAY_PORT
        self._model: str | None = None
        self._output_id: str | None = None
        self._discovered: dict[str, AppleTVConfig] = {}
        self._pairing: HomeViewPairing | None = None
        self._retry_after: int | None = None

    @callback
    def async_remove(self) -> None:
        """Close any pairing session when the flow goes away."""
        if self._pairing is not None:
            self._pairing.close()
            self._pairing = None

    def _use_device(self, conf: AppleTVConfig) -> None:
        props = _airplay_properties(conf)
        service = conf.get_service(Protocol.AirPlay)
        self._address = str(conf.address)
        self._name = conf.name
        self._port = service.port if service and service.port else AIRPLAY_PORT
        self._model = props.get("model")
        self._output_id = props.get("psi")

    # Manual setup

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick a discovered HomeView or enter an address."""
        errors: dict[str, str] = {}

        if user_input is not None:
            conf: AppleTVConfig | None = None
            if address := user_input.get(CONF_ADDRESS, "").strip():
                confs = await async_scan_device(self.hass, address=address)
                conf = confs[0] if confs else None
                if conf is None:
                    errors["base"] = "cannot_connect"
                elif not _is_homeview(conf):
                    errors["base"] = "not_homeview"
            elif device := user_input.get(CONF_DEVICE):
                conf = self._discovered.get(device)
                if conf is None:
                    errors["base"] = "cannot_connect"
            else:
                errors["base"] = "no_selection"

            if not errors and conf is not None and (device_id := _device_id(conf)):
                await self.async_set_unique_id(device_id)
                self._abort_if_unique_id_configured(
                    updates={CONF_ADDRESS: str(conf.address)}
                )
                self._use_device(conf)
                return await self.async_step_pair()
            if not errors:
                errors["base"] = "cannot_connect"

        if user_input is None:
            configured = self._async_current_ids(include_ignore=False)
            self._discovered = {}
            for conf in await async_scan_device(self.hass):
                device_id = _device_id(conf)
                if _is_homeview(conf) and device_id and device_id not in configured:
                    self._discovered[device_id] = conf

        options = {
            device_id: f"{conf.name} ({conf.address})"
            for device_id, conf in sorted(
                self._discovered.items(), key=lambda item: item[1].name
            )
        }
        schema: dict[Any, Any] = {}
        if options:
            schema[vol.Optional(CONF_DEVICE)] = vol.In(options)
        schema[vol.Optional(CONF_ADDRESS)] = str
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(schema),
            errors=errors,
            description_placeholders={"count": str(len(options))},
        )

    # Discovery

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Handle a HomeView found on the network."""
        if discovery_info.ip_address.version == 6:
            return self.async_abort(reason="ipv6_not_supported")
        props = discovery_info.properties
        if not is_homeview_model(props.get("model")):
            return self.async_abort(reason="not_homeview")
        device_id = props.get("deviceid")
        if not device_id:
            return self.async_abort(reason="cannot_connect")

        await self.async_set_unique_id(device_id.upper())
        self._abort_if_unique_id_configured(updates={CONF_ADDRESS: discovery_info.host})

        self._address = discovery_info.host
        self._port = discovery_info.port or AIRPLAY_PORT
        self._name = discovery_info.name.removesuffix(f".{discovery_info.type}")
        self._model = props.get("model")
        self._output_id = props.get("psi")
        self.context["title_placeholders"] = {"name": self._name}
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm setup of a discovered HomeView."""
        if user_input is not None:
            return await self.async_step_pair()
        self._set_confirm_only()
        return self.async_show_form(
            step_id="confirm",
            description_placeholders={
                "name": self._name or "",
                "model": model_name(self._model),
            },
        )

    # Re-pairing

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Re-pair after the HomeView forgot Home Assistant."""
        self._address = entry_data[CONF_ADDRESS]
        self._name = entry_data[CONF_NAME]
        self._model = entry_data.get(CONF_MODEL)
        self._output_id = entry_data.get(CONF_OUTPUT_ID)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask before showing a new PIN."""
        if user_input is not None:
            return await self.async_step_pair()
        return self.async_show_form(
            step_id="reauth_confirm",
            description_placeholders={"name": self._name or ""},
        )

    # Pairing

    async def _async_begin_pairing(self) -> str | None:
        """Ask the HomeView to show a PIN. Return an error key on failure."""
        assert self._address is not None
        if self._pairing is not None:
            self._pairing.close()
        self._pairing = HomeViewPairing(self._address, self._port, CLIENT_NAME)
        try:
            await self._pairing.begin()
        except PairingBackoffError as err:
            self._retry_after = err.retry_after
            return "backoff"
        except PairingRejectedError:
            return "pairing_rejected"
        except PairingError:
            _LOGGER.debug("Could not start pairing", exc_info=True)
            return "cannot_connect"
        return None

    async def async_step_pair(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the PIN form and finish pairing."""
        errors: dict[str, str] = {}
        self._retry_after = None

        if user_input is None:
            if error := await self._async_begin_pairing():
                errors["base"] = error
        else:
            assert self._pairing is not None
            pin = str(user_input[CONF_PIN]).strip()
            try:
                credentials = await self._pairing.finish(pin)
            except InvalidPinError:
                errors["base"] = "invalid_pin"
            except PairingBackoffError as err:
                self._retry_after = err.retry_after
                errors["base"] = "backoff"
            except PairingRejectedError:
                errors["base"] = "pairing_rejected"
            except PairingError:
                _LOGGER.debug("Pairing failed", exc_info=True)
                errors["base"] = "pairing_failed"
            else:
                self._pairing.close()
                self._pairing = None
                return self._async_save(credentials)

            # A failed attempt ends the session on the device; start a new
            # one so a fresh PIN is on screen when the form comes back.
            if errors["base"] in ("invalid_pin", "pairing_failed") and (
                error := await self._async_begin_pairing()
            ):
                errors["base"] = error

        return self.async_show_form(
            step_id="pair",
            data_schema=vol.Schema({vol.Required(CONF_PIN): str}),
            errors=errors,
            description_placeholders={
                "name": self._name or "",
                "retry_after": str(self._retry_after or 60),
            },
        )

    @callback
    def _async_save(self, credentials: str) -> ConfigFlowResult:
        data = {
            CONF_ADDRESS: self._address,
            CONF_NAME: self._name,
            CONF_IDENTIFIER: self.unique_id,
            CONF_CREDENTIALS: credentials,
            CONF_MODEL: self._model,
            CONF_OUTPUT_ID: self._output_id,
        }
        if self.source == SOURCE_REAUTH:
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(), data_updates=data
            )
        return self.async_create_entry(title=self._name or "HomeView", data=data)
