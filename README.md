# Apple HomeView for Home Assistant

A Home Assistant custom integration for the Apple HomeView home hub
(`HomeAccessory17,1` desk and `HomeAccessory17,2` wall models). Install it
through HACS. It is self-contained: everything lives in
`custom_components/apple_homeview`, and the only library it uses is `pyatv`,
which Home Assistant already ships. No add-ons, bridges or extra software.

Home Assistant's built-in [Apple TV integration](https://www.home-assistant.io/integrations/apple_tv/)
cannot set up a HomeView: the pairing PIN never appears on screen, and once
paired the remote control channel is never opened because the model is
unknown to pyatv. This integration fixes both.

> Not affiliated with or endorsed by Apple. HomeView, HomePod, AirPlay and
> Apple Music are trademarks of Apple Inc.

## What you get

Per HomeView:

- **Media player** (`media_player.<name>`)
  - Now playing for anything on the HomeView: the Music app, or audio an
    iPhone is AirPlaying to it. Title, artist, album, genre, position,
    duration, artwork and app name.
  - Play, pause, stop, next, previous, seek
  - Volume set and step
  - Sleep and wake (`turn_off` / `turn_on`)
  - Play audio from Home Assistant: TTS, the media browser (local media,
    radio) and audio URLs/files, streamed over AirPlay audio
  - **Play video on the HomeView screen:** MP4, MOV and HLS (`.m3u8`) URLs
    and video from the media browser, with play, pause, seek, stop,
    position and duration. Shows on the HomeView as playing from
    "Home Assistant".
- **AirPlay speakers (HomePods too).** Make the HomeView play on more
  AirPlay speakers at the same time, exactly like ticking several speakers in
  the iPhone AirPlay menu. Works with HomePods, other HomeViews, Apple TVs and
  AirPlay 2 speakers on your network. See [AirPlay speakers](#airplay-speakers).
- **Remote** (`remote.<name>_remote`) for on-screen navigation
- Automatic discovery, PIN pairing, re-pairing if the HomeView forgets Home
  Assistant, and a diagnostics download

State updates are pushed by the HomeView. While a video plays, its position
is refreshed every few seconds.

### Not supported (yet)

| Feature | Why |
| --- | --- |
| Launching apps / source list | HomeView refuses Companion protocol pairing, which app control needs. |
| Video that needs a login or DRM | The HomeView fetches the URL itself, so it must be reachable without cookies, and FairPlay-protected streams are not supported. |
| Shuffle and repeat | HomeView does not report them over AirPlay. |

## Install

### HACS (recommended)

1. HACS, then the three-dot menu, then **Custom repositories**.
2. Add this repository's URL with type **Integration**.
3. Search for **Apple HomeView**, download it, then restart Home Assistant.

### Manual

Copy `custom_components/apple_homeview` into your Home Assistant
`config/custom_components/` folder and restart.

Requires Home Assistant 2026.6 or newer.

## Set up

1. Go to **Settings > Devices & services**. Each HomeView shows up as
   *Discovered*. Or click **Add integration** and pick **Apple HomeView**.
2. Click **Add** on a HomeView, then stand where you can see its screen.
3. A 4-digit PIN appears on the HomeView. Type it into Home Assistant.
   If the screen is asleep, tap it.

Repeat for each HomeView. Each one needs its own PIN once; after that Home
Assistant reconnects on its own, including after restarts and IP changes.

Why a PIN when your iPhone needs none? Your iPhone is a member of your Apple
Home and is trusted through iCloud. Home Assistant is not, so the HomeView
asks for a one-time PIN, the same way an Apple TV does.

**Built-in Apple TV integration:** it also *discovers* HomeViews (as an
"Apple TV" device) but cannot pair them. Click **Ignore** on those Apple TV
discoveries so they stop showing up.

## AirPlay speakers

The HomeView leads the group: whatever it plays (Music app, or an iPhone
AirPlaying to it) also comes out of every speaker you add.

Speakers are named as they appear in the AirPlay menu (for example
`Study HomePod`). You can also pass a `media_player` entity id. Each HomeView
entity shows `airplay_speakers` (in the group now) and
`available_airplay_speakers` (found on the network) as attributes.

| Action | What it does |
| --- | --- |
| `apple_homeview.add_speakers` | Add speakers to the group |
| `apple_homeview.remove_speakers` | Take speakers out of the group |
| `apple_homeview.set_speakers` | Play on exactly these speakers plus the HomeView. An empty list means the HomeView only. |
| `apple_homeview.set_speaker_volume` | Set one grouped speaker's volume (0 to 1) |
| `media_player.join` / `unjoin` | Standard Home Assistant grouping with entity ids |

```yaml
action: apple_homeview.add_speakers
target:
  entity_id: media_player.living_room_homeview
data:
  speakers:
    - Living Room HomePod
    - Bedroom HomePod
```

```yaml
action: apple_homeview.set_speaker_volume
target:
  entity_id: media_player.living_room_homeview
data:
  speaker: Bedroom HomePod
  volume_level: 0.3
```

Audio that Home Assistant itself sends (TTS, radio) goes to one AirPlay
device per player. For HomePods on their own, use the built-in Apple TV
integration's HomePod players, which need no PIN.

## More examples

Play a video on the HomeView screen:

```yaml
action: media_player.play_media
target:
  entity_id: media_player.living_room_homeview
data:
  media_content_id: https://example.com/videos/clip.mp4
  media_content_type: video
  extra:
    title: Front door clip
```

The HomeView downloads the video itself, so the URL must be reachable from
the HomeView. Media from the Home Assistant media browser works as long as
the HomeView can reach your Home Assistant URL.

Announce something on a HomeView:

```yaml
action: tts.speak
target:
  entity_id: tts.home_assistant_cloud
data:
  media_player_entity_id: media_player.living_room_homeview
  message: "Dinner is ready"
```

Navigate the screen:

```yaml
action: remote.send_command
target:
  entity_id: remote.living_room_homeview_remote
data:
  command: [up, select]
```

Remote commands: `up`, `down`, `left`, `right`, `select`, `menu`, `home`,
`home_hold`, `top_menu`, `play`, `pause`, `play_pause`, `stop`, `next`,
`previous`, `volume_up`, `volume_down`, `turn_on`, `turn_off`, `wakeup`,
`suspend`. Use `hold_secs: 1` with a direction, `select`, `menu` or `home`
for a long press.

## Troubleshooting

- **No PIN on screen:** tap the screen to wake it and try again. Make sure
  Home Assistant and the HomeView are on the same network (or that mDNS and
  port 7000 cross your VLANs).
- **"Blocking pairing after too many attempts":** wait the number of seconds
  shown, or restart the HomeView from the Home app.
- **"Refused to pair":** in the Home app, open Home Settings, then Speakers
  and TV, and set *Allow Speaker and TV Access* to *Everyone* or *Anyone on
  the same network*.
- **Speaker not found:** the speaker list refreshes every 10 minutes and on
  demand. Check the `available_airplay_speakers` attribute for the exact name.
- **Entity unavailable:** the integration retries in the background with
  backoff. Enable debug logging for `custom_components.apple_homeview` and
  `pyatv` to see why.

## How it works

HomeView speaks the same AirPlay 2 protocol as an Apple TV:

1. **Pairing.** HomeView (and tvOS 27) only shows the pairing PIN when the
   pair-setup requests carry an `X-Apple-Client-Name` header. pyatv 0.18 does
   not send it, so this integration runs the HAP pair-setup exchange itself
   ([`pairing.py`](custom_components/apple_homeview/pairing.py)) using pyatv's
   SRP and TLV helpers. The finding is from
   [postlund/pyatv#2894](https://github.com/postlund/pyatv/pull/2894).
2. **Control.** pyatv only opens its remote control tunnel (MRP over AirPlay)
   for models it recognises. The integration forces the tunnel on for its own
   connections through pyatv's per-connection settings. pyatv itself is not
   patched, so Home Assistant's Apple TV integration is unaffected.
3. **Speakers.** Grouping uses the same MediaRemote output device commands an
   iPhone uses, sent through that tunnel. Speakers are identified by their
   AirPlay group id (`psi`), found with an mDNS scan.
4. **Video.** HomeView ignores the classic AirPlay `/play` request and
   rejects any request whose `User-Agent` is not an AirPlay client. Current
   Apple senders instead open a shared AirPlay session, describe themselves
   with MediaRemote messages (without these HomeView plays the sound but not
   the picture), open a remote control session stream plus a MediaRemote
   data channel (without it HomeView ends the session after about 40
   seconds), and then drive an AVPlayer-style queue (`insertPlayQueueItem`,
   `setRate`, `seek`) with `POST /command` and an `X-Apple-StreamID` header.
   [`video.py`](custom_components/apple_homeview/video.py) does the same with
   pyatv's building blocks. Playback status comes back on the AirPlay event
   channel. No FairPlay is needed for unprotected video.

## Development

```bash
python3.14 -m venv ../homeview-venv
../homeview-venv/bin/pip install -r requirements_test.txt
../homeview-venv/bin/python -m pytest
../homeview-venv/bin/ruff check custom_components tests
```

## Credits

Built on [pyatv](https://github.com/postlund/pyatv) by Pierre Ståhl and
contributors. The connection handling follows the patterns of Home
Assistant's Apple TV integration.

## License

Apache License 2.0. See [LICENSE](LICENSE).
