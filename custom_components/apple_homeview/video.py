"""AirPlay video playback for Apple HomeView.

HomeView (OS 27) ignores the classic AirPlay ``/play`` request from
third-party senders. Current Apple senders instead:

1. open a shared AirPlay session that starts as remote-control-only and is
   then upgraded to a media session with timing,
2. describe themselves with MediaRemote ("MR") messages, which HomeView
   needs before it will show video,
3. open a remote control session stream and a MediaRemote data channel,
4. drive an AVPlayer-style queue (``insertPlayQueueItem``, ``setRate``)
   through ``POST /command`` with an ``X-Apple-StreamID`` header pointing at
   that stream.

Playback status comes back as ``/command`` messages on the event channel.
This module reproduces that flow with pyatv's building blocks; nothing in
pyatv is patched.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging
import plistlib
import random
from typing import Any
import uuid

from pyatv.auth.hap_channel import setup_channel
from pyatv.auth.hap_pairing import parse_credentials
from pyatv.protocols.airplay.ap2_session import (
    DATASTREAM_INPUT_INFO,
    DATASTREAM_OUTPUT_INFO,
    DATASTREAM_SALT,
)
from pyatv.protocols.airplay.auth import verify_connection
from pyatv.protocols.airplay.channels import (
    BaseEventChannel,
    DataStreamChannel,
    DataStreamListener,
)
from pyatv.protocols.mrp import messages as mrp_messages
from pyatv.protocols.mrp.protobuf import DeviceInfoMessage_pb2, NowPlayingClient_pb2
from pyatv.protocols.raop.protocols import TimingServer
from pyatv.protocols.raop.protocols.airplayv2 import (
    EVENTS_READ_INFO,
    EVENTS_SALT,
    EVENTS_WRITE_INFO,
)
from pyatv.settings import InfoSettings
from pyatv.support.http import HttpConnection, HttpResponse, http_connect
from pyatv.support.rtsp import RtspSession

from .const import CLIENT_NAME

_LOGGER = logging.getLogger(__name__)

# How Home Assistant presents itself to the HomeView. HomeView shows the
# sender's name with a speaker icon for this identity. The bundle id is never
# displayed; it is this project's own reverse-DNS namespace (its GitHub repo).
SENDER_BUNDLE = "io.github.simplytoast1.ha-homeview"
SENDER_MODEL = "Mac16,10"
SENDER_OS_NAME = "macOS"
SENDER_OS_VERSION = "27.0"
SENDER_BUILD = "25A100"
SENDER_SOURCE_VERSION = "1015.8.1"
SENDER_DEVICE_CLASS = 9  # MediaRemote DeviceClass.Mac
SENDER_MAC = "02:00:00:00:00:01"  # locally administered, not a real device

RCS_CLIENT_TYPE = "A6B27562-B43A-4F2D-B75F-82391E250194"
MRP_CLIENT_TYPE = "1910A70F-DBC0-4242-AF95-115DB30604E1"
STREAM_TYPE_CONTROL = 130
# MediaRemote commands advertised by the sender (play, pause, seek...).
SUPPORTED_COMMANDS = (0, 1, 2, 9, 11)
KEEP_ALIVE_INTERVAL = 2.0

EventCallback = Callable[[dict[str, Any]], None]


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def cmtime(seconds: float) -> dict[str, int]:
    """Return a CMTime dictionary for a number of seconds."""
    return {
        "epoch": 0,
        "flags": 1,
        "timescale": 1_000_000_000,
        "value": int(seconds * 1_000_000_000),
    }


def cmtime_seconds(value: Any) -> float | None:
    """Return seconds from a CMTime dictionary, or None."""
    if not isinstance(value, dict) or not value.get("timescale"):
        return None
    if not value.get("flags", 1) & 1:  # not a valid time
        return None
    return value["value"] / value["timescale"]


def decode_command(body: Any) -> dict[str, Any] | None:
    """Decode a ``/command`` body, unwrapping ``params.data`` when present."""
    if not isinstance(body, (bytes, bytearray)) or not body:
        return None
    try:
        message = plistlib.loads(body)
    except Exception:
        return None
    if not isinstance(message, dict):
        return None
    params = message.get("params")
    if isinstance(params, dict) and isinstance(params.get("data"), bytes):
        try:
            inner = plistlib.loads(params["data"])
        except Exception:
            return None
        return inner if isinstance(inner, dict) else None
    return message


class _EventChannel(BaseEventChannel):
    """Event channel that acknowledges the HomeView and forwards messages."""

    callback: EventCallback | None = None

    def handle_received(self) -> None:
        """Parse buffered requests, reply 200 and forward them."""
        while self.buffer:
            request, _, self.buffer = self.parse_request(self.buffer)
            if request is None:
                break
            headers = {"Content-Length": "0", "Audio-Latency": "0"}
            if "CSeq" in request.headers:
                headers["CSeq"] = request.headers["CSeq"]
            self.send(
                self.format_response(
                    HttpResponse(
                        request.protocol, request.version, 200, "OK", headers, b""
                    )
                )
            )
            message = decode_command(request.body)
            if message is not None and self.callback is not None:
                try:
                    self.callback(message)
                except Exception:
                    _LOGGER.exception("Error handling HomeView video event")


class _DataListener(DataStreamListener):
    """Keep the MediaRemote data channel open and notice when it closes."""

    def __init__(self, on_lost: Callable[[], None]) -> None:
        self._on_lost = on_lost

    def handle_protobuf(self, message: Any) -> None:
        """Playback status comes from the event channel instead."""

    def handle_connection_lost(self, exc: Exception | None) -> None:
        """The HomeView closed the session."""
        self._on_lost()


class HomeViewVideoSession:
    """One AirPlay video session to a HomeView.

    ``on_event`` receives decoded messages from the HomeView, for example
    ``{"type": "playbackState", "name": "playing"}``, ``playbackInfo``
    responses with position and duration, and ``{"type": "sessionEnded"}``
    when the connection closes.
    """

    def __init__(
        self,
        address: str,
        port: int,
        credentials: str,
        receiver_id: str,
        receiver_name: str,
        receiver_model: str,
        on_event: EventCallback | None = None,
    ) -> None:
        """Initialize the session; nothing is sent until ``start``."""
        self._address = address
        self._port = port
        self._credentials = parse_credentials(credentials)
        self._receiver_id = receiver_id
        self._receiver_name = receiver_name
        self._receiver_model = receiver_model
        self._on_event = on_event
        self._connection: HttpConnection | None = None
        self._rtsp: RtspSession | None = None
        self._stream_id: int | None = None
        self._transports: list[asyncio.BaseTransport] = []
        self._keep_alive_task: asyncio.Task[None] | None = None
        self._message_id = 0
        self.item_id: str | None = None

    @property
    def is_open(self) -> bool:
        """Return True while connected."""
        return self._connection is not None

    async def start(self) -> None:
        """Connect and set up everything needed before ``play``."""
        try:
            await self._start()
        except BaseException:
            self.close()
            raise

    async def _start(self) -> None:
        loop = asyncio.get_running_loop()
        self._connection = await http_connect(self._address, self._port)
        verifier = await verify_connection(self._credentials, self._connection)
        self._rtsp = RtspSession(self._connection)

        correlation = str(uuid.uuid4()).upper()
        group = str(uuid.uuid4()).upper()
        sender = {
            "deviceID": SENDER_MAC,
            "macAddress": SENDER_MAC,
            "name": CLIENT_NAME,
            "model": SENDER_MODEL,
            "osName": SENDER_OS_NAME,
            "osVersion": SENDER_OS_VERSION,
            "osBuildVersion": SENDER_BUILD,
            "sourceVersion": SENDER_SOURCE_VERSION,
            "sessionUUID": str(uuid.uuid4()).upper(),
            "sessionCorrelationUUID": correlation,
            "internalBuild": False,
            "diagnosticsAndUsage": False,
            "statsCollectionEnabled": False,
            "isSharedConnection": True,
            "combinedGetInfoWithControlSetup": True,
            "updateSessionRequest": False,
        }

        # 1. Remote-control-only session with an event channel.
        resp = await self._rtsp.setup(
            body={**sender, "isRemoteControlOnly": True, "timingProtocol": "None"}
        )
        transport, events = await setup_channel(
            _EventChannel,
            verifier,
            self._address,
            plistlib.loads(resp.body)["eventPort"],
            EVENTS_SALT,
            EVENTS_READ_INFO,
            EVENTS_WRITE_INFO,
        )
        events.callback = self._handle_event
        self._transports.append(transport)

        # 2. Upgrade to a media session; pyatv's NTP timing server answers
        # the HomeView's clock queries.
        timing_transport, timing = await loop.create_datagram_endpoint(
            TimingServer, local_addr=(self._connection.local_ip, 0)
        )
        self._transports.append(timing_transport)
        await self._rtsp.setup(
            body={
                **sender,
                "isMultiSelectAirPlay": True,
                "groupContainsGroupLeader": False,
                "groupUUID": group,
                "senderSupportsRelay": False,
                "supportsGroupCohesion": True,
                "timingProtocol": "NTP",
                "timingPort": timing.port,
            }
        )

        # 3. Describe the sender; HomeView shows no picture without this.
        client = NowPlayingClient_pb2.NowPlayingClient()
        client.processIdentifier = 4242
        client.bundleIdentifier = SENDER_BUNDLE
        client.processUserIdentifier = 501
        client.nowPlayingVisibility = 3
        client.displayName = CLIENT_NAME
        for message in (
            {
                "type": "updateMRNowPlayingInfo",
                "params": {"type": "npi-text", "params": {}, "mergePolicy": "replace"},
            },
            {
                "type": "updateMRNowPlayingClient",
                "params": {"mrNowPlayingClient": client.SerializeToString()},
            },
            {
                "type": "updateMRSupportedCommands",
                "params": {
                    "mrSupportedCommandsFromSender": [
                        plistlib.dumps(
                            {
                                "kCommandInfoCommandKey": command,
                                "kCommandInfoEnabledKey": True,
                            },
                            fmt=plistlib.FMT_BINARY,
                        )
                        for command in SUPPORTED_COMMANDS
                    ]
                },
            },
            {
                "type": "setMRInfo",
                "params": {"mrInfo": self._mr_info(group, correlation)},
            },
            {"type": "updateMRPlaybackState", "params": {"mrPlaybackState": 1}},
        ):
            await self._post(message)

        # 4. Remote control session stream: play commands are routed here.
        resp = await self._rtsp.setup(
            body={
                "streams": [
                    {
                        "clientUUID": str(uuid.uuid4()).upper(),
                        "clientTypeUUID": RCS_CLIENT_TYPE,
                        "channelID": f"{self._receiver_id}-RCS-1",
                        "controlType": 1,
                        "type": STREAM_TYPE_CONTROL,
                    }
                ]
            }
        )
        self._stream_id = plistlib.loads(resp.body)["streams"][0]["streamID"]

        # 5. MediaRemote data channel. Without a connected MediaRemote client
        # the HomeView ends the session after about 40 seconds.
        seed = random.randint(0, 2**63)
        resp = await self._rtsp.setup(
            body={
                "streams": [
                    {
                        "controlType": 2,
                        "channelID": str(uuid.uuid4()).upper(),
                        "seed": seed,
                        "clientUUID": str(uuid.uuid4()).upper(),
                        "type": STREAM_TYPE_CONTROL,
                        "wantsDedicatedSocket": True,
                        "clientTypeUUID": MRP_CLIENT_TYPE,
                    }
                ]
            }
        )
        transport, data = await setup_channel(
            DataStreamChannel,
            verifier,
            self._address,
            plistlib.loads(resp.body)["streams"][0]["dataPort"],
            DATASTREAM_SALT + str(seed),
            DATASTREAM_OUTPUT_INFO,
            DATASTREAM_INPUT_INFO,
        )
        data.listener = _DataListener(self._handle_lost)
        self._transports.append(transport)
        info = InfoSettings(name=CLIENT_NAME, os_build=SENDER_BUILD)
        data.send_protobuf(
            mrp_messages.device_information(info, str(uuid.uuid4()).upper())
        )
        data.send_protobuf(mrp_messages.set_connection_state())
        data.send_protobuf(mrp_messages.client_updates_config(now_playing=False))

        self._keep_alive_task = loop.create_task(self._keep_alive())
        _LOGGER.debug("Video session to %s ready", self._receiver_name)

    def _mr_info(self, group: str, correlation: str) -> bytes:
        info = DeviceInfoMessage_pb2.DeviceInfoMessage()
        info.uniqueIdentifier = str(uuid.uuid4()).upper()
        info.name = CLIENT_NAME
        info.computerName = CLIENT_NAME
        info.localizedModelName = "Mac"
        info.modelID = SENDER_MODEL
        info.deviceClass = SENDER_DEVICE_CLASS
        info.systemBuildVersion = SENDER_BUILD
        info.applicationBundleIdentifier = "com.apple.mediaremoted"
        info.protocolVersion = 1
        info.lastSupportedMessageType = 139
        info.supportsSystemPairing = True
        info.allowsPairing = True
        info.supportsACL = True
        info.supportsSharedQueue = True
        info.sharedQueueVersion = 3
        info.logicalDeviceCount = 1
        info.isProxyGroupPlayer = True
        info.isGroupLeader = True
        info.isAirplayActive = False
        info.isClusterAware = True
        info.groupUID = group
        info.airPlayGroupID = group
        info.senderDefaultGroupUID = correlation
        info.routingContextID = str(uuid.uuid4()).upper()
        info.parentGroupContainsDiscoverableGroupLeader = 1
        info.groupContainsDiscoverableGroupLeader = 1
        grouped = info.groupedDevices.add()
        grouped.name = self._receiver_name
        grouped.localizedModelName = self._receiver_model
        grouped.modelID = self._receiver_model
        grouped.deviceUID = self._receiver_id
        grouped.groupUID = group
        grouped.airPlayGroupID = group
        grouped.parentGroupContainsDiscoverableGroupLeader = 1
        grouped.groupContainsDiscoverableGroupLeader = 1
        inner = info.SerializeToString()
        return b"\x0a" + _varint(len(inner)) + inner

    async def _post(self, message: dict[str, Any]) -> HttpResponse:
        """Send a session-level command."""
        assert self._rtsp is not None
        return await self._rtsp.exchange(
            "POST", uri="/command", body=message, allow_error=True
        )

    async def _player(self, message: dict[str, Any]) -> HttpResponse:
        """Send a command to the HomeView's video player."""
        if self._rtsp is None or self._stream_id is None:
            raise ConnectionError("video session is not open")
        return await self._rtsp.exchange(
            "POST",
            uri="/command",
            headers={"X-Apple-StreamID": str(self._stream_id)},
            body={"params": {"data": plistlib.dumps(message, fmt=plistlib.FMT_BINARY)}},
            allow_error=True,
        )

    async def _keep_alive(self) -> None:
        while self._rtsp is not None:
            await asyncio.sleep(KEEP_ALIVE_INTERVAL)
            try:
                await self._rtsp.feedback(allow_error=True)
            except Exception:
                self._handle_lost()
                return

    def _handle_lost(self) -> None:
        was_open = self._connection is not None
        self.close()
        if was_open and self._on_event is not None:
            self._on_event({"type": "sessionEnded"})

    def _handle_event(self, message: dict[str, Any]) -> None:
        if self._on_event is not None:
            self._on_event(message)

    async def play(
        self, url: str, position: float = 0.0, title: str | None = None
    ) -> None:
        """Queue a URL and start playing it."""
        self.item_id = str(uuid.uuid4()).upper()
        item = {"uuid": self.item_id}
        no_limit = {"epoch": 0, "flags": 0, "timescale": 0, "value": 0}
        await self._player(
            {
                "type": "insertPlayQueueItem",
                "item": {
                    "Content-Location": url,
                    "Start-Position": cmtime(position),
                    "mediaType": "file",
                    "uuid": self.item_id,
                    "playbackRestrictions": 0,
                    "referenceRestrictions": 3,
                    "clientBundleID": SENDER_BUNDLE,
                    "clientProcName": SENDER_BUNDLE,
                    "audioMode": "default",
                    "allowedCommonMediaClientDataKeys": 0,
                    "snapTimeToPausePlayback": False,
                    "supportsIntegratedTimeline": False,
                },
            }
        )
        for message in (
            {
                "type": "setProperty",
                "property": "isInterestedInDateRange",
                "item": item,
                "value": True,
            },
            {
                "type": "setProperty",
                "property": "selectedMediaArray",
                "item": item,
                "value": [{"MediaSelectionGroupID": 100}],
            },
            {
                "type": "setProperty",
                "property": "metadata",
                "value": {
                    "Title": title or "",
                    "Artist": "",
                    "Album": "",
                    "TotalTrackCount": "",
                    "TrackNumber": "",
                },
            },
            {"type": "setProperty", "property": "actionAtItemEnd", "value": 1},
            {
                "type": "setProperty",
                "property": "forwardEndTime",
                "item": item,
                "value": no_limit,
            },
            {
                "type": "setProperty",
                "property": "reverseEndTime",
                "item": item,
                "value": no_limit,
            },
        ):
            await self._player(message)
        await self.set_rate(1.0)

    async def set_rate(self, rate: float) -> None:
        """Play (1.0) or pause (0.0)."""
        await self._player({"type": "setRate", "rate": rate})

    async def seek(self, seconds: float) -> None:
        """Jump to a position in the current item."""
        await self._player(
            {"type": "seek", "item": {"uuid": self.item_id}, "time": cmtime(seconds)}
        )

    async def request_playback_info(self) -> None:
        """Ask for position and duration; the answer arrives as an event."""
        self._message_id += 1
        await self._player(
            {"type": "playbackInfo", "kind": "request", "messageID": self._message_id}
        )

    async def stop(self) -> None:
        """Stop playback and close the session."""
        if self._rtsp is not None and self._stream_id is not None:
            try:
                await self.set_rate(0.0)
            except Exception:
                _LOGGER.debug("Could not pause before closing", exc_info=True)
        self.close()

    def close(self) -> None:
        """Drop all connections."""
        if self._keep_alive_task is not None:
            self._keep_alive_task.cancel()
            self._keep_alive_task = None
        for transport in self._transports:
            transport.close()
        self._transports.clear()
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        self._rtsp = None
        self._stream_id = None
