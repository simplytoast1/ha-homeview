"""Fixtures for Apple HomeView tests."""

from collections.abc import Generator
from ipaddress import IPv4Address
from unittest.mock import AsyncMock, MagicMock, patch

from pyatv.conf import AppleTV, ManualService
from pyatv.const import FeatureName, FeatureState, Protocol
import pytest

DEVICE_ID = "02:11:22:33:44:55"
OUTPUT_ID = "00000000-0000-4000-8000-000000000001"
ADDRESS = "192.168.1.50"
NAME = "Living Room HomeView"
CREDENTIALS = "aa:bb:cc:dd"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations, mock_async_zeroconf):
    """Enable loading custom integrations in every test."""
    return


def make_conf(
    name: str = NAME,
    address: str = ADDRESS,
    model: str = "HomeAccessory17,1",
    device_id: str = DEVICE_ID,
    output_id: str = OUTPUT_ID,
) -> AppleTV:
    """Build a pyatv config that looks like a scanned HomeView."""
    conf = AppleTV(IPv4Address(address), name)
    conf.add_service(
        ManualService(
            device_id,
            Protocol.AirPlay,
            7000,
            {"model": model, "deviceid": device_id, "psi": output_id},
        )
    )
    return conf


@pytest.fixture
def mock_scan() -> Generator[AsyncMock]:
    """Patch network scans in both the flow and the manager."""
    scan = AsyncMock(return_value=[make_conf()])
    with (
        patch("custom_components.apple_homeview.config_flow.async_scan_device", scan),
        patch("custom_components.apple_homeview.manager.async_scan_device", scan),
        patch("custom_components.apple_homeview.speakers.async_scan_device", scan),
    ):
        yield scan


@pytest.fixture
def mock_pairing() -> Generator[MagicMock]:
    """Patch the PIN pairing exchange."""
    with patch(
        "custom_components.apple_homeview.config_flow.HomeViewPairing", autospec=True
    ) as cls:
        pairing = cls.return_value
        pairing.begin = AsyncMock()
        pairing.finish = AsyncMock(return_value=CREDENTIALS)
        pairing.close = MagicMock()
        yield pairing


def make_atv() -> MagicMock:
    """Build a fake connected pyatv device."""
    atv = MagicMock()
    available = {
        FeatureName.Play,
        FeatureName.Pause,
        FeatureName.PlayUrl,
        FeatureName.StreamFile,
        FeatureName.SetVolume,
        FeatureName.Volume,
        FeatureName.PowerState,
        FeatureName.OutputDevices,
        FeatureName.SetOutputDevices,
        FeatureName.Artist,
        FeatureName.Title,
        FeatureName.PushUpdates,
        FeatureName.SetPosition,
        FeatureName.Stop,
    }
    features = {
        feature: MagicMock(
            state=FeatureState.Available
            if feature in available
            else FeatureState.Unsupported
        )
        for feature in FeatureName
    }
    atv.features.all_features.return_value = features
    atv.features.in_state.side_effect = lambda state, *names: all(
        features[name].state == state for name in names
    )
    atv.audio.volume = 40.0
    atv.audio.set_volume = AsyncMock()
    atv.audio.add_output_devices = AsyncMock()
    atv.audio.remove_output_devices = AsyncMock()
    atv.audio.set_output_devices = AsyncMock()
    atv.audio.output_devices = []
    atv.remote_control.play = AsyncMock()
    atv.remote_control.pause = AsyncMock()
    atv.remote_control.up = AsyncMock()
    atv.device_info.raw_model = "HomeAccessory17,1"
    atv.device_info.mac = "02:11:22:33:44:55"
    atv.device_info.version = "27.2.1"
    return atv


@pytest.fixture
def mock_atv() -> Generator[MagicMock]:
    """Patch the connection so setup gets a fake device."""
    atv = make_atv()
    with patch(
        "custom_components.apple_homeview.manager.async_connect_device",
        AsyncMock(return_value=atv),
    ):
        yield atv
