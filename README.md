# Anchor

A lightweight **Kodi playback companion**. Anchor does not browse, scrape,
search or resolve anything — a companion app hands it an already-resolved stream,
and Anchor plays it while adding the things stock external players lack:

- **Correct identity & metadata** — the cast carries the IMDb id, so Kodi shows the
  right title, year and artwork instead of guessing from a filename.
- **Scrobbling to Trakt and/or Simkl** — connect either or both; play/pause/stop are
  reported to each, building watch history and cross-device resume points.
- **Resume prompt** — "Resume from N%?" using whichever connected service you paused
  most recently.
- **Skip intro / recap** — crowdsourced timestamps from [IntroDB](https://introdb.app).
- **Died-stream guard** — a stream that stalls or dies mid-play is never mis-recorded
  as fully watched, so your resume point survives.

It **ships no media, catalogs or stream sources**. It only plays a stream URL handed
to it by a companion app via a plugin call:

```
plugin://plugin.video.anchor/?action=play_url&url=<encoded>&imdb=tt..&type=movie|tv&season=N&episode=N
```

The full parameter set is specified once in [`ARCHITECTURE.md`](ARCHITECTURE.md#the-cast-contract).

The suite is two add-ons:

| Add-on | Role |
|---|---|
| `plugin.video.anchor` | The playback companion: scrobbling, resume, skip-intro |
| `script.module.anchor` | Shared library (Trakt/Simkl clients, scrobble gate, IntroDB) |

---

## Install (Kodi)

1. **Settings → System → Add-ons →** enable **Unknown sources**.
2. Get the repository zip — either download it directly:
   **<https://aziz66.github.io/repository.anchor/repository.anchor-1.0.2.zip>**,
   or **Settings → File manager → Add source →**
   `https://aziz66.github.io/repository.anchor/` (**keep the trailing slash**) → name it `anchor`.
3. **Add-ons → Install from zip file →** the downloaded zip (or the `anchor` source → `repository.anchor-*.zip`).
4. **Add-ons → Install from repository → Anchor Repository →** install **Anchor**. It auto-updates from here afterwards.

## Connect a scrobbling service (bring your own app)

Anchor uses **your own** free Trakt/Simkl developer apps — nothing is embedded
or shared, so your usage can never affect anyone else's. One-time setup, about
two minutes each. Do just the service(s) you want.

**Trakt**
1. Open **[app.trakt.tv/oauth/applications](https://app.trakt.tv/oauth/applications) → New Application** (sign in first).
2. Name it anything; set **Redirect URI** to `urn:ietf:wg:oauth:2.0:oob`; save.
3. Paste the **Client ID** and **Client Secret** into **Anchor → Settings → Trakt**.
4. **Authorize Trakt** → enter the code at the shown URL.

**Simkl**
1. Open **[simkl.com/settings/developer](https://simkl.com/settings/developer/) → Create a new app**.
2. Set **Redirect URI** to `urn:ietf:wg:oauth:2.0:oob`; save.
3. Paste the **Client ID** into **Anchor → Settings → Simkl** (no secret needed).
4. **Authorize Simkl** → enter the PIN at `simkl.com/pin`.

Connect one or both. Resume needs at least one connected.

> **Language:** the interface is **English only**. The bundled `strings.po`
> localises the settings screen labels; all on-screen prompts and notifications
> are hardcoded English.

---

## Data & privacy

Anchor talks only to the services you enable, and stores nothing off-device
beyond what those services already hold:

- **Trakt / Simkl** — when scrobbling is on, playback events (title, id, progress) are
  sent to authorize and scrobble. Access tokens are stored locally, `0600`, under the
  add-on's own data directory.
- **IntroDB** — when *Skip intro / recap* is on, the show's IMDb id + season/episode
  number are sent to `api.introdb.app` to look up timestamps.
- No telemetry, analytics, or other external calls. The stream URL you cast is played
  by Kodi's own HTTP stack; Anchor does not transmit it anywhere.

---

## Building the repo (maintainers)

The installable Kodi repo under `docs/` is generated from the add-on sources:

```
python3 build_pages.py     # writes docs/ (addons.xml, .md5, zips/, index.html)
```

GitHub Pages serves `docs/` at the URL above. Bump an add-on's `addon.xml` version,
re-run `build_pages.py`, commit `docs/`, and push.

> **Trakt/Simkl app credentials** are embedded in the client. Trakt's device-code flow
> requires the client secret to ship client-side (as it does in every Trakt-enabled Kodi
> add-on), so it is public by necessity; keep it a dedicated app not reused elsewhere.

---

## License

GPL-3.0-or-later. See [`LICENSE`](LICENSE).

Anchor is an independent project and is **not affiliated with, endorsed by, or associated
with** the Kodi/XBMC Foundation, Trakt, Simkl or IntroDB. It ships no media, content or
stream sources. Do not use it for piracy or to access content you are not authorised to.
