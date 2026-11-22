"""Constants for the Apple HomeView integration."""

from typing import Final

DOMAIN: Final = "apple_homeview"

CONF_CREDENTIALS: Final = "credentials"
CONF_IDENTIFIER: Final = "identifier"
CONF_MODEL: Final = "model"
CONF_OUTPUT_ID: Final = "output_id"

# HomeView advertises itself over AirPlay with model identifiers like
# "HomeAccessory17,1" (desk) and "HomeAccessory17,2" (wall mount).
HOMEVIEW_MODEL_PREFIX: Final = "HomeAccessory"

MODEL_NAMES: Final = {
    "HomeAccessory17,1": "HomeView",
    "HomeAccessory17,2": "HomeView (wall mount)",
}

# Name shown on the HomeView pairing screen and in its list of paired devices.
CLIENT_NAME: Final = "Home Assistant"

SIGNAL_CONNECTED: Final = f"{DOMAIN}_connected"
SIGNAL_DISCONNECTED: Final = f"{DOMAIN}_disconnected"


def is_homeview_model(model: str | None) -> bool:
    """Return True if an AirPlay model string belongs to a HomeView."""
    return bool(model) and model.lower().startswith(HOMEVIEW_MODEL_PREFIX.lower())


def model_name(model: str | None) -> str:
    """Return a friendly model name."""
    if model and model in MODEL_NAMES:
        return MODEL_NAMES[model]
    return model or "HomeView"
