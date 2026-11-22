"""Setup and entity tests for Apple HomeView."""

from unittest.mock import AsyncMock, patch

from pyatv import exceptions
from pyatv.const import DeviceState, MediaType as AppleMediaType, PowerState
from pyatv.interface import OutputDevice
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.apple_homeview.const import DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from .conftest import DEVICE_ID, OUTPUT_ID, make_conf
from .test_config_flow import EXPECTED_DATA

ENTITY_ID = "media_player.living_room_homeview"


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DEVICE_ID, data=EXPECTED_DATA)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_setup_creates_entities(hass: HomeAssistant, mock_scan, mock_atv) -> None:
    """Setup connects and adds a media player and remote."""
    mock_atv.power.power_state = PowerState.On
    entry = await _setup(hass)
    assert entry.state is ConfigEntryState.LOADED

    state = hass.states.get(ENTITY_ID)
    assert state is not None
    assert state.state == "idle"
    assert state.attributes["volume_level"] == 0.4
    assert hass.states.get("remote.living_room_homeview_remote") is not None


async def test_now_playing_push(hass: HomeAssistant, mock_scan, mock_atv) -> None:
    """Push updates from the HomeView update the entity."""
    mock_atv.power.power_state = PowerState.On
    await _setup(hass)
    player = mock_atv.push_updater.listener
    playing = AsyncMock()
    playing.device_state = DeviceState.Playing
    playing.media_type = AppleMediaType.Music
    playing.title = "Test Song"
    playing.artist = "Test Artist"
    playing.position = 10
    playing.total_time = 193
    player.playstatus_update(mock_atv.push_updater, playing)
    await hass.async_block_till_done()

    state = hass.states.get(ENTITY_ID)
    assert state.state == "playing"
    assert state.attributes["media_title"] == "Test Song"
    assert state.attributes["media_artist"] == "Test Artist"
    assert state.attributes["media_duration"] == 193


async def test_volume_and_pause(hass: HomeAssistant, mock_scan, mock_atv) -> None:
    """Commands are forwarded to pyatv."""
    mock_atv.power.power_state = PowerState.On
    await _setup(hass)
    await hass.services.async_call(
        "media_player",
        "volume_set",
        {"entity_id": ENTITY_ID, "volume_level": 0.25},
        blocking=True,
    )
    mock_atv.audio.set_volume.assert_awaited_once_with(25.0)
    await hass.services.async_call(
        "media_player", "media_pause", {"entity_id": ENTITY_ID}, blocking=True
    )
    mock_atv.remote_control.pause.assert_awaited_once()


async def test_remote_command(hass: HomeAssistant, mock_scan, mock_atv) -> None:
    """Remote commands map to pyatv remote_control calls."""
    mock_atv.power.power_state = PowerState.On
    await _setup(hass)
    await hass.services.async_call(
        "remote",
        "send_command",
        {
            "entity_id": "remote.living_room_homeview_remote",
            "command": "up",
            "delay_secs": 0,
        },
        blocking=True,
    )
    mock_atv.remote_control.up.assert_awaited_once()


HOMEPOD_ID = "00000000-0000-4000-8000-000000000002"


def _with_homepod(mock_scan) -> None:
    mock_scan.return_value = [
        make_conf(),
        make_conf(
            name="Study HomePod",
            address="192.168.1.60",
            model="AudioAccessory6,1",
            device_id="02:66:77:88:99:AA",
            output_id=HOMEPOD_ID,
        ),
    ]


async def test_group_attributes(hass: HomeAssistant, mock_scan, mock_atv) -> None:
    """Grouped speakers and available speakers are exposed."""
    _with_homepod(mock_scan)
    mock_atv.power.power_state = PowerState.On
    mock_atv.audio.output_devices = [
        OutputDevice(name="Living Room HomeView", identifier=OUTPUT_ID),
        OutputDevice(
            name="Bedroom HomePod", identifier="00000000-0000-4000-8000-000000000003"
        ),
    ]
    await _setup(hass)
    state = hass.states.get(ENTITY_ID)
    assert state.attributes["group_members"] == [ENTITY_ID]
    assert state.attributes["airplay_speakers"] == [
        "Living Room HomeView",
        "Bedroom HomePod",
    ]
    assert state.attributes["available_airplay_speakers"] == ["Study HomePod"]


async def test_add_speakers_by_name(hass: HomeAssistant, mock_scan, mock_atv) -> None:
    """A HomePod is added to the group by its AirPlay name."""
    _with_homepod(mock_scan)
    mock_atv.power.power_state = PowerState.On
    await _setup(hass)
    await hass.services.async_call(
        DOMAIN,
        "add_speakers",
        {"entity_id": ENTITY_ID, "speakers": ["study homepod"]},
        blocking=True,
    )
    mock_atv.audio.add_output_devices.assert_awaited_once_with(HOMEPOD_ID)


async def test_set_speakers_keeps_homeview(
    hass: HomeAssistant, mock_scan, mock_atv
) -> None:
    """set_speakers always keeps the HomeView itself in the group."""
    _with_homepod(mock_scan)
    mock_atv.power.power_state = PowerState.On
    await _setup(hass)
    await hass.services.async_call(
        DOMAIN,
        "set_speakers",
        {"entity_id": ENTITY_ID, "speakers": ["Study HomePod"]},
        blocking=True,
    )
    mock_atv.audio.set_output_devices.assert_awaited_once_with(OUTPUT_ID, HOMEPOD_ID)


async def test_unknown_speaker(hass: HomeAssistant, mock_scan, mock_atv) -> None:
    """An unknown speaker name gives a helpful error."""
    _with_homepod(mock_scan)
    mock_atv.power.power_state = PowerState.On
    await _setup(hass)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "add_speakers",
            {"entity_id": ENTITY_ID, "speakers": ["Garage Speaker"]},
            blocking=True,
        )
    mock_atv.audio.add_output_devices.assert_not_awaited()


async def test_speaker_volume(hass: HomeAssistant, mock_scan, mock_atv) -> None:
    """Per-speaker volume targets the matching group member."""
    _with_homepod(mock_scan)
    mock_atv.power.power_state = PowerState.On
    homepod = OutputDevice(name="Study HomePod", identifier=HOMEPOD_ID)
    mock_atv.audio.output_devices = [
        OutputDevice(name="Living Room HomeView", identifier=OUTPUT_ID),
        homepod,
    ]
    await _setup(hass)
    await hass.services.async_call(
        DOMAIN,
        "set_speaker_volume",
        {"entity_id": ENTITY_ID, "speaker": "Study HomePod", "volume_level": 0.3},
        blocking=True,
    )
    mock_atv.audio.set_volume.assert_awaited_once_with(30.0, output_device=homepod)


async def test_setup_retries_when_unreachable(hass: HomeAssistant, mock_scan) -> None:
    """No answer from the HomeView means setup is retried later."""
    mock_scan.return_value = []
    entry = await _setup(hass)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_auth_failure_starts_reauth(hass: HomeAssistant, mock_scan) -> None:
    """Rejected credentials start a re-pair flow."""
    with patch(
        "custom_components.apple_homeview.manager.async_connect_device",
        AsyncMock(side_effect=exceptions.AuthenticationError("nope")),
    ):
        entry = await _setup(hass)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert flows and flows[0]["context"]["source"] == "reauth"


async def test_play_media_streams_audio(
    hass: HomeAssistant, mock_scan, mock_atv
) -> None:
    """Audio is streamed to the HomeView over AirPlay audio."""
    mock_atv.power.power_state = PowerState.On
    mock_atv.stream.stream_file = AsyncMock()
    await _setup(hass)
    await hass.services.async_call(
        "media_player",
        "play_media",
        {
            "entity_id": ENTITY_ID,
            "media_content_id": "http://example.invalid/chime.mp3",
            "media_content_type": "music",
        },
        blocking=True,
    )
    mock_atv.stream.stream_file.assert_awaited_once_with(
        "http://example.invalid/chime.mp3"
    )


async def test_play_media_video(hass: HomeAssistant, mock_scan, mock_atv) -> None:
    """Video URLs go to the HomeView's AirPlay video player."""
    mock_atv.power.power_state = PowerState.On
    mock_atv.stream.stream_file = AsyncMock()
    await _setup(hass)
    with patch(
        "custom_components.apple_homeview.media_player.HomeViewVideoSession"
    ) as session_cls:
        session = session_cls.return_value
        session.is_open = True
        session.start = AsyncMock()
        session.play = AsyncMock()
        session.set_rate = AsyncMock()
        session.seek = AsyncMock()
        session.stop = AsyncMock()
        session.request_playback_info = AsyncMock()
        await hass.services.async_call(
            "media_player",
            "play_media",
            {
                "entity_id": ENTITY_ID,
                "media_content_id": "http://example.invalid/movie.mp4",
                "media_content_type": "video",
            },
            blocking=True,
        )
        session.start.assert_awaited_once()
        session.play.assert_awaited_once()
        assert session.play.await_args.args[0] == "http://example.invalid/movie.mp4"
        mock_atv.stream.stream_file.assert_not_awaited()

        # Status from the HomeView's player drives the entity.
        on_event = session_cls.call_args.args[6]
        on_event({"type": "playbackState", "name": "playing"})
        on_event(
            {
                "type": "playbackInfo",
                "info": {
                    "readyToPlay": True,
                    "rate": 1.0,
                    "position": {"value": 30, "timescale": 1, "flags": 1, "epoch": 0},
                    "duration": {"value": 600, "timescale": 1, "flags": 1, "epoch": 0},
                },
            }
        )
        await hass.async_block_till_done()
        state = hass.states.get(ENTITY_ID)
        assert state.state == "playing"
        assert state.attributes["media_content_type"] == "video"
        assert state.attributes["media_position"] == 30
        assert state.attributes["media_duration"] == 600
        assert state.attributes["media_title"] == "movie.mp4"

        # Transport controls are routed to the video session.
        await hass.services.async_call(
            "media_player", "media_pause", {"entity_id": ENTITY_ID}, blocking=True
        )
        session.set_rate.assert_awaited_with(0.0)
        await hass.services.async_call(
            "media_player",
            "media_seek",
            {"entity_id": ENTITY_ID, "seek_position": 120},
            blocking=True,
        )
        session.seek.assert_awaited_once_with(120)
        await hass.services.async_call(
            "media_player", "media_stop", {"entity_id": ENTITY_ID}, blocking=True
        )
        session.stop.assert_awaited_once()
        assert (
            hass.states.get(ENTITY_ID).attributes.get("media_content_type") != "video"
        )


async def test_play_media_video_needs_url(
    hass: HomeAssistant, mock_scan, mock_atv
) -> None:
    """A local video path cannot be fetched by the HomeView."""
    mock_atv.power.power_state = PowerState.On
    await _setup(hass)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "media_player",
            "play_media",
            {
                "entity_id": ENTITY_ID,
                "media_content_id": "/media/movie.mp4",
                "media_content_type": "video",
            },
            blocking=True,
        )
