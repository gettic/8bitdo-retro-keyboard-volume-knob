# Pipe protocol (program <-> native-messaging host)

Protocol version: **2** (v1 never shipped; the program only accepts 2)

## The pipe

- Name: `\\.\pipe\8bitdo-volume-knob`. If the environment variable `VOLUME_KNOB_PIPE` is set, both sides use its value as the full pipe name instead (tests use this, e.g. `\\.\pipe\8bitdo-volume-knob-test-<random>`).
- The **program** owns the pipe (server). The **host** is the client (`CreateFile` on the name, read/write).
- The server creates it with `FILE_FLAG_FIRST_PIPE_INSTANCE` (if another process already owns the name, the program refuses to start the pipe), `PIPE_REJECT_REMOTE_CLIENTS`, byte mode, overlapped I/O, and a DACL that allows only the current user (`D:P(A;;GA;;;<current user SID>)`).
- Up to 4 hosts can be connected at the same time (for example one per Brave profile).
- If the pipe does not exist (program not running) or the connection drops, the host keeps working on its own and **retries connecting within 2 s** (the program's start-up failsafe waits 20 s for hosts).

## Framing

- UTF-8 JSON objects, one per line, each line ends with `\n`. No line may be longer than 64 KB.
- Both sides **ignore unknown message types and unknown fields**. Bad JSON lines are ignored; the program drops a client after 3 bad lines in a row.
- Tab titles, URLs or anything else about what the user watches are never sent.

## Handshake

On every connect, the host sends first:

```json
{"type":"hello","protocol":2,"host_version":"1.0.0","features":["boost","set_volume"]}
```

- `features`: what this host/extension can do. The program sends a `max` above 1.0 only to hosts with `"boost"`, and `set_volume` only to hosts with `"set_volume"`. Missing = none.

The program answers:

```json
{"type":"hello","protocol":2,"version":"1.0.0","features":["boost","set_volume"]}
```

If `protocol` is not 2, the program logs it and ignores that client (no commands are sent to it).

## Host -> program

```json
{"type":"state","tabs":[{"tab":123,"site":"youtube","playing":true,"volume":1.2,"muted":false,"max_volume":1.5,"boost":"on"}]}
```

- Sent on every change and at least every 2 s while connected. It always holds the **full** list of the host's YouTube / YouTube Music tabs (an empty list = no tabs).
- **Sent immediately** (not on the 2 s tick) when a tab appears or its player loads, and again when its volume first becomes known. The failsafe answers within a few hundred ms of that state.
- `tab`: integer tab id. It must stay the same while Brave stays open (also across host reconnects). The program keys tabs by connection + tab, and the failsafe remembers (site, tab) for 30 s after it disappears.
- `site`: `"youtube"` or `"ytmusic"`.
- `playing`: true while the tab's player is actually playing (not paused, not ended).
- `volume`: the tab's **effective** volume, **0.0-1.5** = player slider (0.0-1.0) x boost gain (1.0-1.5, only while the slider is at 1.0). `null` (or missing) = not known yet (player not loaded).
- `muted`: the player's own mute state.
- `max_volume`: the highest volume this tab can reach right now: 1.5 when boost is possible, 1.0 when blocked / unavailable / not tried yet. Missing = 1.0.
- `boost`: `"off"` (gain 1.0), `"on"` (gain above 1.0), `"blocked"` (browser refused to start audio processing; the extension retries on the next boost step), `"unavailable"` (the media element is taken by something else; boost impossible for this tab), `"error"` (processing stopped; the tab may be silent until reloaded). Missing = `"off"`.
- After a page reload the extension resets the boost: the tab reports `volume` 1.0 (or its slider value) and `boost:"off"`. That is normal.
- The program treats a host's state as stale after 5 s without a `state` message.

## Program -> host

All commands target either `tabs` (list of tab ids of this host) or, when `tabs` is absent, `site`: `"youtube"`, `"ytmusic"` or `"all"`.

Change volume (knob):

```json
{"type":"volume","tabs":[123,456],"delta":0.04,"max":1.5}
```

- The host computes `new = old + delta` on the effective volume.
- **Crossing 100 %:** if `old < 1.0 < new` or `old > 1.0 > new`, then `new = 1.0` (the knob always stops on 100 % once).
- Then the host clamps `new` to `[0, max]` and to what the tab can do (`max_volume`; a blocked tab retries the boost here). Missing `max` = 1.0 (no boost).
- Applying a delta unmutes the tab.
- The program sends `max` = its `boost_max` setting (1.0-1.5) to hosts with `"boost"`, except for tabs whose `boost` is `"unavailable"` (those get 1.0). Hosts without `"boost"` always get 1.0.

Set volume (failsafe):

```json
{"type":"set_volume","tabs":[123],"volume":0.5}
```

- Absolute effective volume, clamped to `[0, 1.5]` and the tab's ability. At 1.0 or below the gain goes back to 1.0 first, then the slider moves. **Mute is not changed.**
- The program sends it only when a tab is **over 1.0** at program start or when it opens while nothing else is open (see the program's failsafe), and only to hosts with `"set_volume"`.

Mute / unmute:

```json
{"type":"mute","tabs":[123],"muted":true}
{"type":"mute","site":"all","muted":false}
```

- `muted` is explicit (not a toggle): the program decides one mute state for apps and tabs together, so they stay in step.

The program sends nothing other than `hello`, `volume`, `set_volume` and `mute`.
