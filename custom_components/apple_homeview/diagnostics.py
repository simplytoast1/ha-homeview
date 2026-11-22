"""Diagnostics for Apple HomeView."""

from __future__ import annotations

from typing import Any

from pyatv.const import FeatureState

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import HomeViewConfigEntry
from .const import CONF_CREDENTIALS, CONF_IDENTIFIER, CONF_OUTPUT_ID

TO_REDACT = {CONF_CREDENTIALS, CONF_IDENTIFIER, CONF_OUTPUT_ID, "mac"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: HomeViewConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    manager = entry.runtime_data
    data: dict[str, Any] = {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "connected": manager.atv is not None,
    }
    if (atv := manager.atv) is not None:
        info = atv.device_info
        data["device"] = {
            "model": info.raw_model,
            "os": str(info.operating_system),
            "version": info.version,
            "build": info.build_number,
        }
        data["features"] = {
            feature.name: state.state.name
            for feature, state in atv.features.all_features().items()
            if state.state is not FeatureState.Unsupported
        }
    return data
