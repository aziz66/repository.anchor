# AGENTS.md

Mechanics an agent will otherwise get wrong. The source comments cover *what*
each piece does; this covers the things that aren't visible in any single file.

## Two processes, one add-on

`plugin.video.anchor` is both a **plugin** and a **resident service**:

- **`default.py`** runs once per `plugin://` invocation and exits. It resolves
  the cast (`action=play_url`), builds the ListItem, writes the identity stash,
  and calls `setResolvedUrl`.
- **`service.py`** is the long-lived `xbmc.Player` subclass. It scrobbles,
  prompts for resume, and skips intros.

They share **no memory**. Their only channel is **`xbmcgui.Window(10000)`
properties under the `anchor.` prefix** (`PROP = "anchor."`). `default.py`
writes `anchor.playing_id` / `playing_type` / `playing_file` / `playing_ts`;
`service.py` reads them in `_fresh_stash()`, gated by a **120-second freshness
window** *and* a **current-file match** (so a stash written for one stream can
never mislabel another). That property store is where playback identity lives
between the two processes — nothing else crosses the boundary.

## Non-obvious seams

- **`sync.is_enabled` is monkeypatched at `service.py`** (`sync.is_enabled = lambda name: ADDON.getSetting(name + "_scrobble") != "false"`).
  Read in the library alone it looks like dead code returning `True`. It is the
  deliberate policy seam: the library ships a default, the add-on injects the
  real per-service toggle. The same pattern sets `client.CACHE_ADDON` and
  `store.DATA_ADDON`.
- **The scrobble `start` is deferred to the service loop on purpose.** `start`
  DELETES stored progress on both Trakt and Simkl, so sending it eagerly (in the
  player callback, before the resume position has been read) destroys the resume
  point it is meant to protect. `_pending_start` carries it until after the
  resume lookup.
- **The watched threshold lives in one place.** `_gate.WATCHED_PCT = 80.0`;
  `service.py` and `trakt.py` both derive theirs from it, so they cannot drift.

## Diagnosis

There is no debug mode or verbose toggle. Diagnosis is the Kodi log:

```
grep '\[anchor' ~/.kodi/temp/kodi.log      # or the platform's kodi.log path
```

Library modules log under `[anchor.trakt]` / `[anchor.simkl]` / `[anchor.sync]`
/ `[anchor.introdb]` / `[anchor.client]`; the plugin logs under `[anchor]` and
`[anchorlite]`-free tags. **Two logging conventions coexist:** the library
passes magic ints (`log(msg, 2)` = warning, `3` = error); the plugin uses the
named `xbmc.LOGWARNING` / `xbmc.LOGERROR` constants. Both reach the same log.

## Tests

The library was written to run outside Kodi (every Kodi import has an
`except ImportError` fallback). Run from the repo root, no Kodi needed:

```
python3 -m pip install -r requirements-dev.txt
python3 -m pytest          # 36 tests, ~0.1s
python3 -m ruff check .
```

`tests/stubs/` provides `xbmc*` modules injected on `sys.path` by
`tests/conftest.py` **before** any add-on import (the plugin reads `sys.argv`
and imports `xbmc*` at module top, so the bootstrap order matters). The
`xbmcgui.Window` stub models shared property get/set — that is what lets a test
exercise the plugin→service identity hand-off.

## Build, version, publish

- **Build the repo:** `python3 build_pages.py` regenerates `docs/` (the GitHub
  Pages Kodi repository: `addons.xml`, `.md5`, `zips/`, per-dir `index.html`).
  **Never hand-edit `docs/`** — it is generated. Commit it and push.
- **Version bumps are paired.** Bump `script.module.anchor/addon.xml`'s
  `version` **and** the matching `<import addon="script.module.anchor"
  version="…"/>` in `plugin.video.anchor/addon.xml`. Nothing validates the pair
  except CI's docs-staleness job (a version bump without a `docs/` rebuild fails
  CI). After any version change: re-run `build_pages.py` and commit `docs/`.
- **CI** (`.github/workflows/ci.yml`) runs ruff, pytest, and the
  `build_pages.py` + `git diff --exit-code docs/` staleness guard on every push.

## The cast contract

The `plugin://plugin.video.anchor/?action=play_url&…` parameter set is specified
once, in [`ARCHITECTURE.md`](ARCHITECTURE.md). The companion app and this add-on
must agree on it; do not restate it elsewhere.
