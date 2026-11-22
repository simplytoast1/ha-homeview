"""Media player for Apple HomeView."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from datetime import datetime
from functools import wraps
import logging
from typing import Any, Concatenate

from pyatv import exceptions
from pyatv.const import (
    DeviceState,
    FeatureName,
    FeatureState,
    MediaType as AppleMediaType,
    PowerState,
    RepeatState,
    ShuffleState,
)
from pyatv.helpers import is_streamable
from pyatv.interface import (
    AppleTV as AppleTVInterface,
    AudioListener,
    OutputDevice,
    Playing,
    PowerListener,
    PushListener,
    PushUpdater,
)
from yarl import URL

from homeassistant.components import media_source
from homeassistant.components.media_player import (
    ATTR_MEDIA_EXTRA,
    BrowseMedia,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
    RepeatMode,
    async_process_play_media_url,
)
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import HomeViewConfigEntry
from .const import CONF_CREDENTIALS, CONF_MODEL, CONF_OUTPUT_ID, DOMAIN
from .entity import HomeViewEntity
from .speakers import get_directory
from .video import HomeViewVideoSession, cmtime_seconds

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

# How long play_media waits for a stream to fail before returning to the caller.
STREAM_START_GRACE = 3.0

SUPPORT_BASE = MediaPlayerEntityFeature.TURN_ON | MediaPlayerEntityFeature.TURN_OFF

AUDIO_TYPES = {"music", "audio", "podcast", "track", "playlist", "channel"}
VIDEO_TYPES = {"video", "movie", "tvshow", "episode"}
VIDEO_EXTENSIONS = (".mp4", ".m4v", ".mov", ".m3u8")
VIDEO_POLL_INTERVAL = 5.0
VIDEO_STATES = {
    "loading": MediaPlayerState.BUFFERING,
    "playing": MediaPlayerState.PLAYING,
    "paused": MediaPlayerState.PAUSED,
}
# Notifications that mean the video finished or failed.
VIDEO_END_NOTIFICATIONS = {
    "didPlayToEndTime",
    "itemDidPlayToEndTime",
    "failedToPlayToEndTime",
    "itemFailedToPlayToEndTime",
}

FEATURE_MAP: dict[FeatureName, MediaPlayerEntityFeature] = {
    FeatureName.StreamFile: MediaPlayerEntityFeature.PLAY_MEDIA
    | MediaPlayerEntityFeature.BROWSE_MEDIA,
    FeatureName.Play: MediaPlayerEntityFeature.PLAY,
    FeatureName.Pause: MediaPlayerEntityFeature.PAUSE,
    FeatureName.Stop: MediaPlayerEntityFeature.STOP,
    FeatureName.Next: MediaPlayerEntityFeature.NEXT_TRACK,
    FeatureName.Previous: MediaPlayerEntityFeature.PREVIOUS_TRACK,
    FeatureName.SetPosition: MediaPlayerEntityFeature.SEEK,
    FeatureName.SetVolume: MediaPlayerEntityFeature.VOLUME_SET,
    FeatureName.VolumeUp: MediaPlayerEntityFeature.VOLUME_STEP,
    FeatureName.VolumeDown: MediaPlayerEntityFeature.VOLUME_STEP,
    FeatureName.SetRepeat: MediaPlayerEntityFeature.REPEAT_SET,
    FeatureName.SetShuffle: MediaPlayerEntityFeature.SHUFFLE_SET,
    FeatureName.SetOutputDevices: MediaPlayerEntityFeature.GROUPING,
}

COMMAND_ERRORS = (
    TimeoutError,
    exceptions.BlockedStateError,
    exceptions.ConnectionLostError,
    exceptions.InvalidStateError,
    exceptions.NotSupportedError,
    exceptions.OperationTimeoutError,
    exceptions.ProtocolError,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HomeViewConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the HomeView media player."""
    async_add_entities([HomeViewMediaPlayer(entry.runtime_data)])


def _command[T: "HomeViewMediaPlayer", **P](
    func: Callable[Concatenate[T, P], Coroutine[Any, Any, None]],
) -> Callable[Concatenate[T, P], Coroutine[Any, Any, None]]:
    """Turn pyatv failures into a Home Assistant error the UI can show."""

    @wraps(func)
    async def wrapper(self: T, *args: P.args, **kwargs: P.kwargs) -> None:
        if self.atv is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="not_connected"
            )
        try:
            await func(self, *args, **kwargs)
        except COMMAND_ERRORS as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"error": str(err) or type(err).__name__},
            ) from err

    return wrapper


class HomeViewMediaPlayer(
    HomeViewEntity, MediaPlayerEntity, PowerListener, AudioListener, PushListener
):
    """Media player for a HomeView."""

    _attr_name = None
    _attr_media_image_remotely_accessible = False

    def __init__(self, manager) -> None:
        """Initialize the media player."""
        super().__init__(manager)
        self._attr_supported_features = SUPPORT_BASE
        self._playing: Playing | None = None
        self._playing_updated: datetime | None = None
        self._stream_task: asyncio.Task[None] | None = None
        # AirPlay video playback (see video.py)
        self._video: HomeViewVideoSession | None = None
        self._video_poll: asyncio.Task[None] | None = None
        self._video_title: str | None = None
        self._video_url: str | None = None
        self._video_state: MediaPlayerState | None = None
        self._video_position: float | None = None
        self._video_duration: float | None = None
        self._video_updated: datetime | None = None

    # Connection lifecycle

    @callback
    def async_device_connected(self, atv: AppleTVInterface) -> None:
        """Set up listeners and work out what this HomeView supports."""
        features = atv.features.all_features()
        supported = SUPPORT_BASE
        for feature, flag in FEATURE_MAP.items():
            info = features.get(feature)
            if info is not None and info.state is not FeatureState.Unsupported:
                supported |= flag
        self._attr_supported_features = supported

        atv.power.listener = self
        atv.audio.listener = self
        if atv.features.in_state(FeatureState.Available, FeatureName.PushUpdates):
            atv.push_updater.listener = self
            atv.push_updater.start()

        if supported & MediaPlayerEntityFeature.GROUPING:
            self.manager.entry.async_create_background_task(
                self.hass,
                self._async_refresh_speakers(),
                name=f"{DOMAIN} speaker scan {self.manager.name}",
            )

    async def _async_refresh_speakers(self) -> None:
        """Find AirPlay speakers so they can be listed and grouped."""
        await get_directory(self.hass).async_refresh()
        if self.hass and self.atv is not None:
            self.async_write_ha_state()

    @callback
    def async_device_disconnected(self) -> None:
        """Forget state when the connection drops."""
        self._playing = None
        self._playing_updated = None
        self._async_close_video()

    async def async_will_remove_from_hass(self) -> None:
        """Stop any running stream."""
        if self._stream_task is not None:
            self._stream_task.cancel()
        self._async_close_video()

    # AirPlay video

    @property
    def _video_active(self) -> bool:
        return self._video is not None and self._video.is_open

    async def _async_play_video(self, url: str, title: str | None) -> None:
        """Open a video session and play a URL on the HomeView screen."""
        await self._async_stop_video()
        if self._stream_task is not None and not self._stream_task.done():
            self._stream_task.cancel()
        data = self.manager.entry.data
        session = HomeViewVideoSession(
            data[CONF_ADDRESS],
            self.manager.airplay_port,
            data[CONF_CREDENTIALS],
            data.get(CONF_OUTPUT_ID) or "",
            self.manager.name,
            data.get(CONF_MODEL) or "HomeAccessory17,1",
            self._async_video_event,
        )
        self._video = session
        self._video_title = title
        self._video_url = url
        self._video_state = MediaPlayerState.BUFFERING
        self._video_position = 0.0
        self._video_duration = None
        self._video_updated = dt_util.utcnow()
        try:
            await session.start()
            await session.play(url, title=title)
        except Exception as err:
            self._async_close_video()
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="video_failed",
                translation_placeholders={"error": str(err) or type(err).__name__},
            ) from err
        self._video_poll = self.manager.entry.async_create_background_task(
            self.hass, self._async_poll_video(session), name=f"{DOMAIN} video status"
        )
        self.async_write_ha_state()

    async def _async_poll_video(self, session: HomeViewVideoSession) -> None:
        """Ask the HomeView for position and duration while a video plays."""
        while session.is_open and session is self._video:
            try:
                await session.request_playback_info()
            except Exception:
                return
            await asyncio.sleep(VIDEO_POLL_INTERVAL)

    @callback
    def _async_video_event(self, message: dict[str, Any]) -> None:
        """Update state from video player messages."""
        kind = message.get("type")
        if kind == "sessionEnded":
            self._async_close_video()
        elif kind == "playbackState":
            self._video_state = VIDEO_STATES.get(
                message.get("name", ""), self._video_state
            )
        elif kind == "playbackInfo":
            info = message.get("info") or {}
            if (position := cmtime_seconds(info.get("position"))) is not None:
                self._video_position = position
                self._video_updated = dt_util.utcnow()
            if (duration := cmtime_seconds(info.get("duration"))) is not None:
                self._video_duration = duration
            rate = info.get("rate")
            if isinstance(rate, (int, float)) and info.get("readyToPlay"):
                self._video_state = (
                    MediaPlayerState.PLAYING if rate > 0 else MediaPlayerState.PAUSED
                )
        elif kind == "notification" and message.get("name") in VIDEO_END_NOTIFICATIONS:
            self._async_close_video()
        else:
            return
        self.async_write_ha_state()

    async def _async_stop_video(self) -> None:
        if self._video is not None:
            session = self._video
            self._async_close_video()
            await session.stop()

    @callback
    def _async_close_video(self) -> None:
        if self._video_poll is not None:
            self._video_poll.cancel()
            self._video_poll = None
        if self._video is not None:
            session, self._video = self._video, None
            session.close()
            self._video_state = None
            if self.hass is not None and self.entity_id:
                self.async_write_ha_state()

    # pyatv listener callbacks

    @callback
    def playstatus_update(self, updater: PushUpdater, playstatus: Playing) -> None:
        """Handle a now playing update."""
        self._playing = playstatus
        self._playing_updated = dt_util.utcnow()
        self.async_write_ha_state()

    @callback
    def playstatus_error(self, updater: PushUpdater, exception: Exception) -> None:
        """Handle a push update error; pyatv restarts updates by itself."""
        _LOGGER.debug(
            "Now playing update error from %s: %s", self.manager.name, exception
        )
        self._playing = None
        self.async_write_ha_state()

    @callback
    def powerstate_update(self, old_state: PowerState, new_state: PowerState) -> None:
        """Handle a power state change."""
        self.async_write_ha_state()

    @callback
    def volume_update(self, old_level: float, new_level: float) -> None:
        """Handle a volume change."""
        self.async_write_ha_state()

    @callback
    def volume_device_update(
        self, output_device: OutputDevice, old_level: float, new_level: float
    ) -> None:
        """Handle a volume change on a grouped speaker."""

    @callback
    def outputdevices_update(
        self, old_devices: list[OutputDevice], new_devices: list[OutputDevice]
    ) -> None:
        """Handle speakers joining or leaving the group."""
        self.async_write_ha_state()

    # State

    @property
    def state(self) -> MediaPlayerState | None:
        """Return the player state."""
        if self.atv is None:
            return None
        if self._video_active:
            return self._video_state or MediaPlayerState.BUFFERING
        if (
            self._is_available(FeatureName.PowerState)
            and self.atv.power.power_state is PowerState.Off
        ):
            return MediaPlayerState.OFF
        if self._playing is None:
            return MediaPlayerState.IDLE
        state = self._playing.device_state
        if state is DeviceState.Playing:
            return MediaPlayerState.PLAYING
        if state in (DeviceState.Paused, DeviceState.Seeking, DeviceState.Stopped):
            return MediaPlayerState.PAUSED
        return MediaPlayerState.IDLE

    @property
    def _has_media(self) -> bool:
        return self._playing is not None and self.state in (
            MediaPlayerState.PLAYING,
            MediaPlayerState.PAUSED,
        )

    @property
    def app_id(self) -> str | None:
        """Bundle id of the app that is playing."""
        if self._video_active:
            return None
        if self._is_available(FeatureName.App) and (app := self.atv.metadata.app):
            return app.identifier
        return None

    @property
    def app_name(self) -> str | None:
        """Name of the app that is playing."""
        if self._video_active:
            return "AirPlay"
        if self._is_available(FeatureName.App) and (app := self.atv.metadata.app):
            return app.name
        return None

    @property
    def media_content_type(self) -> MediaType | None:
        """Content type of the current media."""
        if self._video_active:
            return MediaType.VIDEO
        if not self._playing:
            return None
        return {
            AppleMediaType.Music: MediaType.MUSIC,
            AppleMediaType.Video: MediaType.VIDEO,
            AppleMediaType.TV: MediaType.TVSHOW,
        }.get(self._playing.media_type)

    @property
    def media_content_id(self) -> str | None:
        """Content id of the current media."""
        if self._video_active:
            return self._video_url
        if self._playing and self._is_available(FeatureName.ContentIdentifier):
            return self._playing.content_identifier
        if self._playing and self._is_available(FeatureName.iTunesStoreIdentifier):
            store_id = self._playing.itunes_store_identifier
            return str(store_id) if store_id else None
        return None

    @property
    def media_title(self) -> str | None:
        """Title of the current media."""
        if self._video_active:
            return self._video_title
        return self._playing.title if self._playing else None

    @property
    def media_artist(self) -> str | None:
        """Artist of the current media."""
        if self._playing and self._is_available(FeatureName.Artist):
            return self._playing.artist
        return None

    @property
    def media_album_name(self) -> str | None:
        """Album of the current media."""
        if self._playing and self._is_available(FeatureName.Album):
            return self._playing.album
        return None

    @property
    def media_series_title(self) -> str | None:
        """Series of the current media."""
        if self._playing and self._is_available(FeatureName.SeriesName):
            return self._playing.series_name
        return None

    @property
    def media_season(self) -> str | None:
        """Season of the current media."""
        if self._playing and self._is_available(FeatureName.SeasonNumber):
            return str(self._playing.season_number)
        return None

    @property
    def media_episode(self) -> str | None:
        """Episode of the current media."""
        if self._playing and self._is_available(FeatureName.EpisodeNumber):
            return str(self._playing.episode_number)
        return None

    @property
    def media_duration(self) -> float | None:
        """Duration of the current media in seconds."""
        if self._video_active:
            return self._video_duration
        return self._playing.total_time if self._playing else None

    @property
    def media_position(self) -> float | None:
        """Position of the current media in seconds."""
        if self._video_active:
            return self._video_position
        return self._playing.position if self._playing else None

    @property
    def media_position_updated_at(self) -> datetime | None:
        """When the position was last reported."""
        if self._video_active:
            return self._video_updated
        return self._playing_updated if self._has_media else None

    @property
    def volume_level(self) -> float | None:
        """Volume, 0..1."""
        if self._is_available(FeatureName.Volume):
            return self.atv.audio.volume / 100.0
        return None

    @property
    def repeat(self) -> RepeatMode | None:
        """Repeat mode."""
        if self._playing and self._is_available(FeatureName.Repeat):
            return {
                RepeatState.Track: RepeatMode.ONE,
                RepeatState.All: RepeatMode.ALL,
            }.get(self._playing.repeat, RepeatMode.OFF)
        return None

    @property
    def shuffle(self) -> bool | None:
        """Shuffle mode."""
        if self._playing and self._is_available(FeatureName.Shuffle):
            return self._playing.shuffle is not ShuffleState.Off
        return None

    @property
    def media_image_hash(self) -> str | None:
        """Hash of the current artwork."""
        if self._video_active:
            return None
        if self._has_media and self._is_available(FeatureName.Artwork):
            return self.atv.metadata.artwork_id
        return None

    async def async_get_media_image(self) -> tuple[bytes | None, str | None]:
        """Fetch the current artwork."""
        if self.atv is not None and self._has_media:
            try:
                artwork = await self.atv.metadata.artwork(width=512, height=512)
            except COMMAND_ERRORS as err:
                _LOGGER.debug("Could not fetch artwork: %s", err)
                return None, None
            if artwork:
                return artwork.bytes, artwork.mimetype or "image/jpeg"
        return None, None

    # AirPlay speaker group (HomeViews, HomePods and other AirPlay 2 speakers)
    #
    # The HomeView is the group leader: whatever it plays (Music app, AirPlay
    # from an iPhone) also comes out of every speaker added here, exactly like
    # picking several speakers in the iPhone AirPlay menu.

    @property
    def _own_output_id(self) -> str | None:
        oid = self.manager.entry.data.get(CONF_OUTPUT_ID)
        return oid.upper() if oid else None

    @property
    def group_members(self) -> list[str] | None:
        """Entity ids of the speakers in this HomeView's AirPlay group."""
        if not self._is_available(FeatureName.OutputDevices):
            return None
        devices = self.atv.audio.output_devices
        if len(devices) < 2:
            return []
        directory = get_directory(self.hass)
        members = [
            entity_id
            for device in devices
            if (entity_id := directory.entity_for(device.identifier, device.name))
        ]
        # Home Assistant expects the group leader first.
        if self.entity_id in members:
            members.remove(self.entity_id)
        return [self.entity_id, *members]

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose the AirPlay speakers in the group and those available."""
        if not self._is_available(FeatureName.OutputDevices):
            return None
        in_group = {d.identifier.upper() for d in self.atv.audio.output_devices}
        if self._own_output_id:
            in_group.add(self._own_output_id)
        return {
            "airplay_speakers": [
                device.name or device.identifier
                for device in self.atv.audio.output_devices
            ],
            "available_airplay_speakers": [
                speaker.name
                for speaker in get_directory(self.hass).speakers
                if speaker.output_id not in in_group
            ],
        }

    async def _async_output_ids(self, keys: list[str]) -> list[str]:
        """Resolve speaker names, ids or entity ids to AirPlay group ids."""
        directory = get_directory(self.hass)
        ids: list[str] = []
        for key in keys:
            if key == self.entity_id:
                continue
            speaker = await directory.async_resolve(key)
            if speaker is None:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="unknown_speaker",
                    translation_placeholders={
                        "speaker": key,
                        "available": ", ".join(s.name for s in directory.speakers),
                    },
                )
            if speaker.output_id != self._own_output_id:
                ids.append(speaker.output_id)
        return ids

    @_command
    async def async_join_players(self, group_members: list[str]) -> None:
        """Add speakers (by entity id) to this HomeView's AirPlay group."""
        if ids := await self._async_output_ids(group_members):
            await self.atv.audio.add_output_devices(*ids)

    @_command
    async def async_unjoin_player(self) -> None:
        """Leave any group this HomeView is in."""
        own_id = self._own_output_id
        # If this HomeView leads a group, play only on itself again.
        if own_id and len(self.atv.audio.output_devices) > 1:
            await self.atv.audio.set_output_devices(own_id)
            return
        # Otherwise ask whichever HomeView leads our group to drop us.
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            other = getattr(entry, "runtime_data", None)
            if other is None or other is self.manager or other.atv is None:
                continue
            ids = [d.identifier.upper() for d in other.atv.audio.output_devices]
            if own_id and own_id in ids:
                await other.atv.audio.remove_output_devices(own_id)
                return

    @_command
    async def async_add_speakers(self, speakers: list[str]) -> None:
        """Add AirPlay speakers to the group by name, id or entity id."""
        if ids := await self._async_output_ids(speakers):
            await self.atv.audio.add_output_devices(*ids)

    @_command
    async def async_remove_speakers(self, speakers: list[str]) -> None:
        """Remove AirPlay speakers from the group."""
        if ids := await self._async_output_ids(speakers):
            await self.atv.audio.remove_output_devices(*ids)

    @_command
    async def async_set_speakers(self, speakers: list[str]) -> None:
        """Play on exactly these speakers (this HomeView is always included)."""
        ids = await self._async_output_ids(speakers)
        own = [self._own_output_id] if self._own_output_id else []
        await self.atv.audio.set_output_devices(*own, *ids)

    @_command
    async def async_set_speaker_volume(self, speaker: str, volume_level: float) -> None:
        """Set the volume of one speaker in the group (0..1)."""
        ids = await self._async_output_ids([speaker])
        target_id = ids[0] if ids else self._own_output_id
        device = next(
            (
                d
                for d in self.atv.audio.output_devices
                if d.identifier.upper() == (target_id or "")
            ),
            None,
        )
        if device is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="speaker_not_in_group",
                translation_placeholders={"speaker": speaker},
            )
        await self.atv.audio.set_volume(volume_level * 100.0, output_device=device)

    # Commands

    async def async_turn_on(self) -> None:
        """Wake the HomeView."""
        if self.atv is None:
            self.manager.async_start()
            return
        if self._is_available(FeatureName.TurnOn):
            await self.atv.power.turn_on()

    async def async_turn_off(self) -> None:
        """Put the HomeView to sleep."""
        await self._async_stop_video()
        if self._is_available(FeatureName.TurnOff):
            await self.atv.power.turn_off()

    @_command
    async def async_media_play(self) -> None:
        """Play."""
        if self._video_active:
            await self._video.set_rate(1.0)
            return
        await self.atv.remote_control.play()

    @_command
    async def async_media_pause(self) -> None:
        """Pause."""
        if self._video_active:
            await self._video.set_rate(0.0)
            return
        await self.atv.remote_control.pause()

    @_command
    async def async_media_play_pause(self) -> None:
        """Toggle play and pause."""
        if self._video_active:
            playing = self._video_state is MediaPlayerState.PLAYING
            await self._video.set_rate(0.0 if playing else 1.0)
            return
        await self.atv.remote_control.play_pause()

    @_command
    async def async_media_stop(self) -> None:
        """Stop playback."""
        if self._video_active:
            await self._async_stop_video()
            return
        if self._stream_task is not None and not self._stream_task.done():
            self._stream_task.cancel()
        await self.atv.remote_control.stop()

    @_command
    async def async_media_next_track(self) -> None:
        """Next track."""
        await self.atv.remote_control.next()

    @_command
    async def async_media_previous_track(self) -> None:
        """Previous track."""
        await self.atv.remote_control.previous()

    @_command
    async def async_media_seek(self, position: float) -> None:
        """Seek to a position in seconds."""
        if self._video_active:
            await self._video.seek(position)
            self._video_position = position
            self._video_updated = dt_util.utcnow()
            self.async_write_ha_state()
            return
        await self.atv.remote_control.set_position(round(position))

    @_command
    async def async_set_volume_level(self, volume: float) -> None:
        """Set volume, 0..1."""
        await self.atv.audio.set_volume(volume * 100.0)

    @_command
    async def async_volume_up(self) -> None:
        """Volume up."""
        await self.atv.audio.volume_up()

    @_command
    async def async_volume_down(self) -> None:
        """Volume down."""
        await self.atv.audio.volume_down()

    @_command
    async def async_set_repeat(self, repeat: RepeatMode) -> None:
        """Set repeat mode."""
        await self.atv.remote_control.set_repeat(
            {RepeatMode.ONE: RepeatState.Track, RepeatMode.ALL: RepeatState.All}.get(
                repeat, RepeatState.Off
            )
        )

    @_command
    async def async_set_shuffle(self, shuffle: bool) -> None:
        """Set shuffle."""
        await self.atv.remote_control.set_shuffle(
            ShuffleState.Songs if shuffle else ShuffleState.Off
        )

    # Playing media

    async def async_browse_media(
        self,
        media_content_type: MediaType | str | None = None,
        media_content_id: str | None = None,
    ) -> BrowseMedia:
        """Browse Home Assistant media sources (audio and video)."""
        return await media_source.async_browse_media(
            self.hass,
            media_content_id,
            content_filter=lambda item: item.media_content_type.startswith(
                ("audio/", "video/")
            ),
        )

    @_command
    async def async_play_media(
        self, media_type: MediaType | str, media_id: str, **kwargs: Any
    ) -> None:
        """Play a URL, a local file or a media source item.

        Video (and anything the HomeView should fetch itself, such as HLS)
        is handed to the HomeView's AirPlay video player, which shows it on
        screen. Plain audio, including TTS, is streamed over AirPlay audio.
        """
        mime_type: str | None = None
        title = (kwargs.get(ATTR_MEDIA_EXTRA) or {}).get("title")
        local_path: str | None = None
        if media_source.is_media_source_id(media_id):
            item = await media_source.async_resolve_media(
                self.hass, media_id, self.entity_id
            )
            mime_type = item.mime_type
            local_path = str(item.path) if item.path else None
            media_id = async_process_play_media_url(self.hass, item.url)

        media_type = str(media_type).lower()
        mime_type = (mime_type or "").lower()
        url = URL(media_id)
        is_url = url.is_absolute() and url.scheme in ("http", "https")
        is_video = (
            mime_type.startswith("video/")
            or media_type in VIDEO_TYPES
            or url.path.lower().endswith(VIDEO_EXTENSIONS)
        )
        if is_video:
            if not is_url:
                raise ServiceValidationError(
                    translation_domain=DOMAIN, translation_key="video_needs_url"
                )
            await self._async_play_video(
                media_id, title or url.name or self.manager.name
            )
            return

        if not self._is_available(FeatureName.StreamFile):
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="cannot_play"
            )
        source = local_path or media_id
        is_audio = (
            mime_type.startswith("audio/")
            or media_type in AUDIO_TYPES
            or media_type.startswith("audio/")
        )
        if is_audio or await is_streamable(source):
            await self._async_stop_video()
            await self._async_start_stream(source)
            return
        if is_url:
            # Let the HomeView's player try formats AirPlay audio cannot
            # stream, such as HLS radio.
            await self._async_play_video(
                media_id, title or url.name or self.manager.name
            )
            return
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="cannot_play"
        )

    async def _async_start_stream(self, media_id: str) -> None:
        """Stream audio in the background so the service call returns quickly."""
        if self._stream_task is not None and not self._stream_task.done():
            self._stream_task.cancel()

        assert self.atv is not None
        stream: Awaitable[None] = self.atv.stream.stream_file(media_id)
        task = self.manager.entry.async_create_background_task(
            self.hass, stream, name=f"{DOMAIN} stream {self.manager.name}"
        )
        self._stream_task = task
        done, _ = await asyncio.wait({task}, timeout=STREAM_START_GRACE)
        if done:
            if not task.cancelled() and (err := task.exception()) is not None:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="command_failed",
                    translation_placeholders={"error": str(err) or type(err).__name__},
                ) from err
            return
        task.add_done_callback(self._log_stream_result)

    def _log_stream_result(self, task: asyncio.Task[None]) -> None:
        """Log a stream that failed after play_media already returned."""
        if task.cancelled():
            return
        if (err := task.exception()) is not None:
            _LOGGER.warning("Streaming to %s stopped: %s", self.manager.name, err)
