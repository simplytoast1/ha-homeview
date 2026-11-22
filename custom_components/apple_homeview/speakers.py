"""Directory of AirPlay speakers a HomeView can add to its group."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import time

from pyatv.const import Protocol

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import CONF_OUTPUT_ID, DOMAIN
from .manager import async_scan_device

_LOGGER = logging.getLogger(__name__)

REFRESH_INTERVAL = 600  # seconds
SCAN_TIMEOUT = 3


@dataclass(frozen=True)
class AirPlaySpeaker:
    """One AirPlay receiver on the network (HomePod, HomeView, Apple TV...)."""

    name: str
    output_id: str
    device_id: str | None
    model: str | None


class SpeakerDirectory:
    """Find AirPlay speakers and map them to Home Assistant entities."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the directory."""
        self.hass = hass
        self._speakers: dict[str, AirPlaySpeaker] = {}
        # None means never scanned. A monotonic clock can be smaller than
        # REFRESH_INTERVAL right after the host boots, so 0.0 is not safe.
        self._updated: float | None = None
        self._lock = asyncio.Lock()

    @property
    def speakers(self) -> list[AirPlaySpeaker]:
        """Return known speakers sorted by name."""
        return sorted(self._speakers.values(), key=lambda s: s.name.casefold())

    async def async_refresh(self, force: bool = False) -> None:
        """Scan the network for AirPlay receivers if the cache is stale."""
        async with self._lock:
            if (
                not force
                and self._updated is not None
                and time.monotonic() - self._updated < REFRESH_INTERVAL
            ):
                return
            try:
                confs = await async_scan_device(self.hass, timeout=SCAN_TIMEOUT)
            except Exception:
                _LOGGER.debug("AirPlay speaker scan failed", exc_info=True)
                return
            found: dict[str, AirPlaySpeaker] = {}
            for conf in confs:
                service = conf.get_service(Protocol.AirPlay)
                if service is None:
                    continue
                props = service.properties
                output_id = props.get("psi") or props.get("pi")
                if not output_id:
                    continue
                speaker = AirPlaySpeaker(
                    name=conf.name,
                    output_id=output_id.upper(),
                    device_id=(props.get("deviceid") or "").upper() or None,
                    model=props.get("model"),
                )
                found[speaker.output_id] = speaker
            self._speakers = found
            self._updated = time.monotonic()
            _LOGGER.debug("Found %d AirPlay speakers", len(found))

    def by_output_id(self, output_id: str) -> AirPlaySpeaker | None:
        """Look up a speaker by its AirPlay group id."""
        return self._speakers.get(output_id.upper())

    def find(self, key: str) -> AirPlaySpeaker | None:
        """Find a speaker by name, AirPlay group id or device id (MAC)."""
        wanted = key.strip()
        if speaker := self.by_output_id(wanted):
            return speaker
        folded = wanted.casefold()
        for speaker in self._speakers.values():
            if speaker.name.casefold() == folded:
                return speaker
            if speaker.device_id and dr.format_mac(speaker.device_id) == dr.format_mac(
                wanted
            ):
                return speaker
        return None

    async def async_resolve(self, key: str) -> AirPlaySpeaker | None:
        """Resolve a speaker name, id or media_player entity id."""
        await self.async_refresh()
        speaker = (
            self._from_entity(key)
            if key.startswith("media_player.")
            else self.find(key)
        )
        if speaker is None:
            # Maybe it just came online; scan once more.
            await self.async_refresh(force=True)
            speaker = (
                self._from_entity(key)
                if key.startswith("media_player.")
                else self.find(key)
            )
        return speaker

    def _from_entity(self, entity_id: str) -> AirPlaySpeaker | None:
        """Map any media_player entity to an AirPlay speaker.

        HomeView entities use their stored group id. Other entities (for
        example HomePods from the Apple TV integration) are matched by their
        device's MAC address or name.
        """
        ent_reg = er.async_get(self.hass)
        entry = ent_reg.async_get(entity_id)
        candidates: list[str] = []
        if entry is not None:
            if entry.platform == DOMAIN and entry.config_entry_id:
                config_entry = self.hass.config_entries.async_get_entry(
                    entry.config_entry_id
                )
                if config_entry and (oid := config_entry.data.get(CONF_OUTPUT_ID)):
                    return self.by_output_id(oid) or AirPlaySpeaker(
                        name=config_entry.title,
                        output_id=oid.upper(),
                        device_id=None,
                        model=None,
                    )
            if entry.device_id and (
                device := dr.async_get(self.hass).async_get(entry.device_id)
            ):
                candidates.extend(
                    value
                    for kind, value in device.connections
                    if kind == dr.CONNECTION_NETWORK_MAC
                )
                candidates.extend(n for n in (device.name_by_user, device.name) if n)
        if (state := self.hass.states.get(entity_id)) and (
            name := state.attributes.get("friendly_name")
        ):
            candidates.append(name)
        for candidate in candidates:
            if speaker := self.find(candidate):
                return speaker
        return None

    def entity_for(self, output_id: str, name: str | None) -> str | None:
        """Return the media_player entity id for a group member, if any."""
        ent_reg = er.async_get(self.hass)
        for config_entry in self.hass.config_entries.async_entries(DOMAIN):
            oid = config_entry.data.get(CONF_OUTPUT_ID)
            if oid and oid.upper() == output_id.upper() and config_entry.unique_id:
                return ent_reg.async_get_entity_id(
                    "media_player", DOMAIN, config_entry.unique_id
                )
        if not name:
            return None
        folded = name.casefold()
        dev_reg = dr.async_get(self.hass)
        for entry in ent_reg.entities.values():
            if entry.domain != "media_player" or entry.disabled:
                continue
            device = dev_reg.async_get(entry.device_id) if entry.device_id else None
            names = [device.name_by_user, device.name] if device else []
            if any(n and n.casefold() == folded for n in names):
                return entry.entity_id
        return None


def get_directory(hass: HomeAssistant) -> SpeakerDirectory:
    """Return the shared speaker directory."""
    data = hass.data.setdefault(DOMAIN, {})
    if "speakers" not in data:
        data["speakers"] = SpeakerDirectory(hass)
    return data["speakers"]
