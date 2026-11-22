"""Remote control for Apple HomeView."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
import logging
from typing import Any

from pyatv.const import InputAction

from homeassistant.components.remote import (
    ATTR_DELAY_SECS,
    ATTR_HOLD_SECS,
    ATTR_NUM_REPEATS,
    DEFAULT_DELAY_SECS,
    DEFAULT_HOLD_SECS,
    RemoteEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import HomeViewConfigEntry
from .const import DOMAIN
from .entity import HomeViewEntity

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

# Commands that live outside pyatv's remote_control interface.
SPECIAL_COMMANDS: dict[str, tuple[str, str]] = {
    "wakeup": ("power", "turn_on"),
    "turn_on": ("power", "turn_on"),
    "suspend": ("power", "turn_off"),
    "turn_off": ("power", "turn_off"),
    "volume_up": ("audio", "volume_up"),
    "volume_down": ("audio", "volume_down"),
}

REMOTE_COMMANDS = {
    "up",
    "down",
    "left",
    "right",
    "select",
    "menu",
    "home",
    "home_hold",
    "top_menu",
    "play",
    "pause",
    "play_pause",
    "stop",
    "next",
    "previous",
}

HOLDABLE = {"up", "down", "left", "right", "select", "menu", "home"}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HomeViewConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the HomeView remote."""
    async_add_entities([HomeViewRemote(entry.runtime_data)])


class HomeViewRemote(HomeViewEntity, RemoteEntity):
    """Send navigation and transport commands to a HomeView."""

    _attr_translation_key = "remote"

    def __init__(self, manager) -> None:
        """Initialize the remote."""
        super().__init__(manager, "remote")

    @property
    def is_on(self) -> bool:
        """The remote is on while connected."""
        return self.atv is not None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Reconnect to the HomeView."""
        self.manager.async_start()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Remote cannot be turned off independently; no-op."""

    async def async_send_command(self, command: Iterable[str], **kwargs: Any) -> None:
        """Send one or more commands."""
        if self.atv is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="not_connected"
            )
        repeats: int = kwargs.get(ATTR_NUM_REPEATS, 1)
        delay: float = kwargs.get(ATTR_DELAY_SECS, DEFAULT_DELAY_SECS)
        hold: float = kwargs.get(ATTR_HOLD_SECS, DEFAULT_HOLD_SECS)

        commands = list(command)
        for name in commands:
            if name not in SPECIAL_COMMANDS and name not in REMOTE_COMMANDS:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="unknown_command",
                    translation_placeholders={
                        "command": name,
                        "valid": ", ".join(
                            sorted(REMOTE_COMMANDS | set(SPECIAL_COMMANDS))
                        ),
                    },
                )

        for _ in range(repeats):
            for name in commands:
                if name in SPECIAL_COMMANDS:
                    group, method = SPECIAL_COMMANDS[name]
                    func = getattr(getattr(self.atv, group), method)
                    await func()
                else:
                    func = getattr(self.atv.remote_control, name)
                    if hold >= 1 and name in HOLDABLE:
                        await func(action=InputAction.Hold)
                    else:
                        await func()
                _LOGGER.debug("Sent %s to %s", name, self.manager.name)
                await asyncio.sleep(delay)
