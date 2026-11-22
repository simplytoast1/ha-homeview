"""Config flow tests for Apple HomeView."""

from ipaddress import ip_address
from unittest.mock import AsyncMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.apple_homeview.const import (
    CONF_CREDENTIALS,
    CONF_IDENTIFIER,
    CONF_MODEL,
    CONF_OUTPUT_ID,
    DOMAIN,
)
from custom_components.apple_homeview.pairing import (
    InvalidPinError,
    PairingBackoffError,
)
from homeassistant import config_entries
from homeassistant.const import CONF_ADDRESS, CONF_NAME, CONF_PIN
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .conftest import ADDRESS, CREDENTIALS, DEVICE_ID, NAME, OUTPUT_ID, make_conf

ZEROCONF_INFO = ZeroconfServiceInfo(
    ip_address=ip_address(ADDRESS),
    ip_addresses=[ip_address(ADDRESS)],
    hostname="Living-Room-HomeView.local.",
    name=f"{NAME}._airplay._tcp.local.",
    port=7000,
    type="_airplay._tcp.local.",
    properties={
        "model": "HomeAccessory17,1",
        "deviceid": DEVICE_ID,
        "psi": OUTPUT_ID,
    },
)

EXPECTED_DATA = {
    CONF_ADDRESS: ADDRESS,
    CONF_NAME: NAME,
    CONF_IDENTIFIER: DEVICE_ID,
    CONF_CREDENTIALS: CREDENTIALS,
    CONF_MODEL: "HomeAccessory17,1",
    CONF_OUTPUT_ID: OUTPUT_ID,
}


def _no_setup():
    return patch(
        "custom_components.apple_homeview.async_setup_entry",
        AsyncMock(return_value=True),
    )


async def test_user_flow_pick_discovered(
    hass: HomeAssistant, mock_scan, mock_pairing
) -> None:
    """Pick a discovered HomeView and pair it."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["description_placeholders"] == {"count": "1"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"device": DEVICE_ID}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "pair"
    mock_pairing.begin.assert_awaited_once()

    with _no_setup():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PIN: "1234"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == NAME
    assert result["data"] == EXPECTED_DATA
    assert result["result"].unique_id == DEVICE_ID
    mock_pairing.finish.assert_awaited_once_with("1234")


async def test_user_flow_filters_non_homeview(
    hass: HomeAssistant, mock_scan, mock_pairing
) -> None:
    """HomePods and Apple TVs are not offered."""
    mock_scan.return_value = [
        make_conf(
            name="Bedroom HomePod",
            model="AudioAccessory6,1",
            device_id="02:AA:BB:CC:DD:EE",
        )
    ]
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["description_placeholders"] == {"count": "0"}


async def test_user_flow_manual_address_not_homeview(
    hass: HomeAssistant, mock_scan, mock_pairing
) -> None:
    """A manually entered non-HomeView address shows an error."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    mock_scan.return_value = [make_conf(model="AppleTV18,1")]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESS: "192.168.1.99"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "not_homeview"}


async def test_invalid_pin_restarts_pairing(
    hass: HomeAssistant, mock_scan, mock_pairing
) -> None:
    """A wrong PIN shows an error and asks the HomeView for a new PIN."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"device": DEVICE_ID}
    )
    mock_pairing.finish.side_effect = InvalidPinError("bad")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PIN: "0000"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_pin"}
    assert mock_pairing.begin.await_count == 2

    mock_pairing.finish.side_effect = None
    with _no_setup():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PIN: "4808"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_backoff_error(hass: HomeAssistant, mock_scan, mock_pairing) -> None:
    """The HomeView refusing to pair for a while is reported with a delay."""
    mock_pairing.begin.side_effect = PairingBackoffError(120)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"device": DEVICE_ID}
    )
    assert result["errors"] == {"base": "backoff"}
    assert result["description_placeholders"]["retry_after"] == "120"


async def test_zeroconf_flow(hass: HomeAssistant, mock_scan, mock_pairing) -> None:
    """A discovered HomeView is confirmed, then paired."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_ZEROCONF},
        data=ZEROCONF_INFO,
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "pair"

    with _no_setup():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PIN: "1234"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == EXPECTED_DATA


async def test_zeroconf_updates_address_of_existing(
    hass: HomeAssistant, mock_pairing
) -> None:
    """Rediscovery at a new address updates the entry and aborts."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DEVICE_ID,
        data={**EXPECTED_DATA, CONF_ADDRESS: "192.168.1.2"},
    )
    entry.add_to_hass(hass)
    with _no_setup():
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_ZEROCONF},
            data=ZEROCONF_INFO,
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_ADDRESS] == ADDRESS


async def test_zeroconf_rejects_other_models(hass: HomeAssistant) -> None:
    """Only HomeAccessory models start a flow."""
    info = ZeroconfServiceInfo(
        ip_address=ip_address(ADDRESS),
        ip_addresses=[ip_address(ADDRESS)],
        hostname="Bedroom.local.",
        name="Bedroom._airplay._tcp.local.",
        port=7000,
        type="_airplay._tcp.local.",
        properties={"model": "AudioAccessory6,1", "deviceid": "02:AA:BB:CC:DD:EE"},
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=info
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_homeview"


async def test_reauth(hass: HomeAssistant, mock_pairing) -> None:
    """Re-pairing replaces the stored credentials."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DEVICE_ID,
        data={**EXPECTED_DATA, CONF_CREDENTIALS: "old"},
    )
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "pair"

    with _no_setup():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PIN: "1234"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_CREDENTIALS] == CREDENTIALS
