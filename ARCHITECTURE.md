# Architecture

Anchor is a Kodi **playback companion**. A separate app resolves a stream and
casts it to Kodi; Anchor plays it, identifies it, and reports playback to Trakt
and/or Simkl. It never browses, scrapes or resolves anything itself.

## Add-ons

| Add-on | Role |
|---|---|
| `plugin.video.anchor` | Plugin (`default.py`) + resident service (`service.py`). |
| `script.module.anchor` | Shared library, importable as `anchor`: Trakt/Simkl clients, the scrobble gate, IntroDB, the disk cache. |
| `repository.anchor` | The Kodi repository users install from. |

## Two processes and the identity hand-off

`default.py` runs per invocation and exits; `service.py` is resident. They share
state **only** through `xbmcgui.Window(10000)` properties under the `anchor.`
prefix:

```
default.py  --writes-->  anchor.playing_id / playing_type / playing_file / playing_ts
service.py  --reads--->  _fresh_stash():  120s freshness window + current-file match
```

If the id-less branch fires (see below), `default.py` *clears* those properties
instead. See [`AGENTS.md`](AGENTS.md) for the process mechanics.

## Scrobble flow

`service.py` (an `xbmc.Player`) turns player callbacks into scrobble events and
calls `anchor.sync`, which fans out to every connected backend **sequentially**
and **independently** (one failing never blocks the other). `_gate.ScrobbleGate`
enforces the shared rules (dedupe, 1 POST/sec floor, the open-session latch, the
sub-1% clamp); the resume prompt reads the most-recently-paused position across
services, comparing timestamps by parsed value (Trakt sends millisecond
precision, Simkl whole seconds). `start` is deferred until after the resume
lookup because `start` deletes stored progress on both services.

---

# The cast contract

**This is the single source of truth for the `play_url` interface.** The
companion app and `plugin.video.anchor` must agree on it. Do not restate the
parameter list anywhere else — `README.md` links here.

## Invocation

```
plugin://plugin.video.anchor/?action=play_url&url=<enc>&imdb=tt…&type=…&season=N&episode=N&…
```

Handled by `default.py:play_url`. The add-on plays the resolved `url` carrying
the supplied identity/metadata, then `setResolvedUrl` succeeds. If `url` is
missing, it resolves failure and does nothing else.

## Parameters

| Param | Required | Meaning |
|---|---|---|
| `action` | ✅ | Must be `play_url`. |
| `url` | ✅ | The already-resolved, playable stream URL (percent-encoded). |
| `type` | – | `tv` / `series` / `episode` → **series**; anything else (or absent) → **movie**. |
| `imdb` | – | IMDb id. Bare `tt1234567`, **or** the packed `tt1234567:season:episode`. |
| `season` | – | Season number (may instead arrive packed inside `imdb`). |
| `episode` | – | Episode number (may instead arrive packed inside `imdb`). |
| `title` | – | Display title (also the ListItem label; falls back to `url`). |
| `year` | – | Release year (integer). |
| `plot` | – | Synopsis. |
| `poster` | – | Poster/thumb image URL. |
| `fanart` | – | Fanart/background image URL. |
| `genre` | – | Comma-separated genres. |
| `show` | – | Series title (episodes only). |

Anything omitted stays blank — Anchor performs **no** metadata lookups.

## Identity resolution

- **Content id.** For a movie it is the bare `imdb`. For an episode it is
  `imdb:season:episode`, assembled from either the packed `imdb` form or the
  separate `season`/`episode` params (both accepted; separate params win only
  when the packed form is absent — non-numeric season/episode are ignored).
- **Id-less cast (e.g. Live TV).** When `imdb` is absent or not a `tt…` id,
  `default.py` **clears** the identity stash rather than leaving the previous
  item's identity live — otherwise the service would keep scrobbling the last
  movie against the new stream. Nothing is scrobbled for an id-less cast.
- **Release name.** The stash records the release filename parsed from the tail
  of `url` (debrid URLs end with it) when it looks more specific than `title`;
  kept for diagnostics/release-aware tooling.
