"""AirPlay HAP pairing for Apple HomeView.

HomeView (like Apple TV on OS 27) generates a pairing PIN but only shows it
on screen when every pairing request carries an ``X-Apple-Client-Name``
header. pyatv 0.18 does not send that header, so the pair-setup exchange is
implemented here using pyatv's SRP and TLV building blocks. The resulting
credentials string is the same format pyatv uses, so it can be handed straight
to ``pyatv.connect``.
"""

from __future__ import annotations

import logging

from pyatv.auth.hap_srp import SRPAuthHandler
from pyatv.auth.hap_tlv8 import TlvValue, read_tlv, write_tlv
from pyatv.exceptions import HttpError
from pyatv.support.http import HttpConnection, http_connect

_LOGGER = logging.getLogger(__name__)

AIRPLAY_PORT = 7000

# HAP error codes returned in the TLV "Error" field.
_HAP_ERROR_AUTHENTICATION = 0x02
_HAP_ERROR_BACKOFF = 0x03
_HAP_ERROR_MAX_PEERS = 0x04
_HAP_ERROR_MAX_TRIES = 0x05
_HAP_ERROR_UNAVAILABLE = 0x06
_HAP_ERROR_BUSY = 0x07


class PairingError(Exception):
    """Generic pairing failure."""


class InvalidPinError(PairingError):
    """The PIN entered did not match the one shown on the HomeView."""


class PairingBackoffError(PairingError):
    """The HomeView refuses pairing for a while after failed attempts."""

    def __init__(self, retry_after: int | None) -> None:
        """Initialize with an optional retry delay in seconds."""
        super().__init__(f"pairing blocked, retry after {retry_after}s")
        self.retry_after = retry_after


class PairingRejectedError(PairingError):
    """The HomeView declined to start pairing (busy, too many peers, etc.)."""


def _check_tlv_error(data: dict[int, bytes]) -> None:
    """Raise a matching exception if a pair-setup response holds an error."""
    if TlvValue.Error not in data:
        return
    code = data[TlvValue.Error][0]
    if code == _HAP_ERROR_AUTHENTICATION:
        raise InvalidPinError("incorrect PIN")
    if code == _HAP_ERROR_BACKOFF:
        delay = data.get(TlvValue.BackOff)
        raise PairingBackoffError(int.from_bytes(delay, "little") if delay else None)
    if code in (_HAP_ERROR_MAX_PEERS, _HAP_ERROR_MAX_TRIES):
        raise PairingRejectedError("device does not accept more pairings")
    if code in (_HAP_ERROR_UNAVAILABLE, _HAP_ERROR_BUSY):
        raise PairingRejectedError("device is busy pairing with another client")
    raise PairingError(f"pairing failed with HAP error {code}")


class HomeViewPairing:
    """Run one AirPlay pair-setup exchange against a HomeView."""

    def __init__(self, address: str, port: int = AIRPLAY_PORT, name: str = "") -> None:
        """Initialize pairing for the device at ``address``."""
        self._address = address
        self._port = port
        self._name = name
        self._http: HttpConnection | None = None
        self._srp: SRPAuthHandler | None = None
        self._salt: bytes | None = None
        self._public_key: bytes | None = None

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": "AirPlay/320.20",
            "Connection": "keep-alive",
            "X-Apple-HKP": "3",
            "Content-Type": "application/octet-stream",
            "X-Apple-Client-Name": self._name,
        }

    async def _post(self, path: str, body: bytes | None = None) -> dict[int, bytes]:
        assert self._http is not None
        try:
            resp = await self._http.post(path, headers=self._headers, body=body)
        except HttpError as err:
            raise PairingRejectedError(str(err)) from err
        if not body:
            return {}
        if not isinstance(resp.body, bytes):
            raise PairingError(f"unexpected response: {resp.body!r}")
        data = read_tlv(resp.body)
        _check_tlv_error(data)
        return data

    async def begin(self) -> None:
        """Ask the HomeView to show a PIN and do the first SRP step."""
        self.close()
        self._srp = SRPAuthHandler()
        self._srp.initialize()
        try:
            self._http = await http_connect(self._address, self._port)
        except OSError as err:
            raise PairingError(f"cannot connect to {self._address}: {err}") from err

        await self._post("/pair-pin-start")
        data = await self._post(
            "/pair-setup",
            write_tlv({TlvValue.Method: b"\x00", TlvValue.SeqNo: b"\x01"}),
        )
        try:
            self._salt = data[TlvValue.Salt]
            self._public_key = data[TlvValue.PublicKey]
        except KeyError as err:
            raise PairingError("device did not return SRP parameters") from err
        _LOGGER.debug("Pairing started with %s, PIN should be on screen", self._address)

    async def finish(self, pin: str) -> str:
        """Complete pairing with the PIN and return a pyatv credentials string."""
        if self._srp is None or self._salt is None or self._public_key is None:
            raise PairingError("pairing was not started")

        self._srp.step1(pin)
        pub_key, proof = self._srp.step2(self._public_key, self._salt)
        await self._post(
            "/pair-setup",
            write_tlv(
                {
                    TlvValue.SeqNo: b"\x03",
                    TlvValue.PublicKey: pub_key,
                    TlvValue.Proof: proof,
                }
            ),
        )
        data = await self._post(
            "/pair-setup",
            write_tlv(
                {
                    TlvValue.SeqNo: b"\x05",
                    TlvValue.EncryptedData: self._srp.step3(name=self._name),
                }
            ),
        )
        try:
            credentials = self._srp.step4(data[TlvValue.EncryptedData])
        except KeyError as err:
            raise PairingError("device did not return pairing data") from err
        except Exception as err:  # pyatv raises a few types on bad data
            raise PairingError(f"could not verify device: {err}") from err
        return str(credentials)

    def close(self) -> None:
        """Drop the pairing connection, which also clears the PIN on screen."""
        if self._http is not None:
            self._http.close()
            self._http = None
