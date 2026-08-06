"""Anchor - Kodi video plugin entry point.

Receives an ALREADY-resolved stream cast in by a companion app at
``?action=play_url&url=<encoded>&imdb=tt..`` and plays it carrying its own
identity, so Kodi shows real metadata and the resident service can scrobble,
resume and skip intros.

There is deliberately no browsing, scraping or metadata layer here: the app
resolves the stream and supplies the metadata. This file is only the Kodi
presentation layer (routing, ListItems, dialogs).
"""

from __future__ import annotations

import json
import os
import sys
from urllib.parse import parse_qsl, unquote, urlencode, urlparse

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin
from anchor import client, simkl, store, trakt

# Point the shared library's cache and token storage at THIS add-on's own
# addon_data, so "Clear cache" and the stored tokens stay isolated to us.
client.CACHE_ADDON = "plugin.video.anchor"
store.DATA_ADDON = "plugin.video.anchor"

ADDON = xbmcaddon.Addon()
HANDLE = int(sys.argv[1])
BASE_URL = sys.argv[0]

# Bring-your-own app credentials: bind the library's Trakt/Simkl client id +
# secret to this add-on's settings, read live so entering them takes effect
# without a restart. Nothing is embedded in the library.
trakt.set_credentials(lambda: ADDON.getSetting("trakt_client_id").strip(),
                      lambda: ADDON.getSetting("trakt_client_secret").strip())
simkl.set_credentials(lambda: ADDON.getSetting("simkl_client_id").strip())

# Window used to hand the playing item's identity to the resident service,
# under our own "anchor.*" property namespace.
HOME = xbmcgui.Window(10000)
PROP = "anchor."
_STASH_PROPS = ("playing_id", "playing_type", "playing_filename",
                "playing_videosize", "playing_file", "playing_ts")


def build_url(**kwargs):
    return BASE_URL + "?" + urlencode({k: v for k, v in kwargs.items()
                                       if v is not None})


def notify(msg, heading="Anchor"):
    xbmcgui.Dialog().notification(heading, msg, xbmcgui.NOTIFICATION_INFO, 4000)


# ---------------------------------------------------------------------------
# Identity stash (read by service.py)
# ---------------------------------------------------------------------------

def _stash_id(content_id, ctype, filename="", videosize=""):
    """Record what we're about to play for the scrobbler service."""
    import time
    HOME.setProperty(PROP + "playing_id", content_id)
    HOME.setProperty(PROP + "playing_type", ctype)
    HOME.setProperty(PROP + "playing_filename", filename or "")
    HOME.setProperty(PROP + "playing_videosize", str(videosize or ""))
    HOME.setProperty(PROP + "playing_file", "")  # bound below once known
    HOME.setProperty(PROP + "playing_ts", str(time.time()))  # freshness gate


def _clear_stash():
    for p in _STASH_PROPS:
        HOME.clearProperty(PROP + p)


# ---------------------------------------------------------------------------
# Playback - the one entry point that matters
# ---------------------------------------------------------------------------

def _parse_meta(raw):
    """Decode the `meta` JSON param. Malformed input is ignored, never fatal:
    a cast must still play when the metadata is broken."""
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except (ValueError, TypeError):
        xbmc.log("[anchor] meta param was not valid JSON, ignoring",
                 xbmc.LOGWARNING)
        return {}


def _actors(entries):
    """[{name, role, thumbnail, order}] -> [xbmc.Actor]. Kodi 20+ only."""
    out = []
    for i, e in enumerate(entries or []):
        if isinstance(e, str):
            e = {"name": e}
        if not isinstance(e, dict) or not e.get("name"):
            continue
        try:
            out.append(xbmc.Actor(e["name"], e.get("role", "") or "",
                                  int(e.get("order", i)), e.get("thumbnail", "") or ""))
        except (AttributeError, TypeError, ValueError):
            return []          # older Kodi without xbmc.Actor: skip cast entirely
    return out


def _apply_info(tag, info):
    """Map Kodi-vocabulary keys onto InfoTagVideo setters.

    One table instead of a hand-written if-ladder: adding a field upstream in the
    app becomes a no-op here once its key is listed, and anything unrecognised is
    ignored rather than raising. Every setter is wrapped because InfoTag setters
    differ across Kodi versions and one missing method must not stop playback.
    """
    def _f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def _list(v):
        if isinstance(v, (list, tuple)):
            return [str(x).strip() for x in v if str(x).strip()]
        return [p.strip() for p in str(v).split(",") if p.strip()]

    setters = {
        "title": lambda v: tag.setTitle(str(v)),
        "originaltitle": lambda v: tag.setOriginalTitle(str(v)),
        "tvshowtitle": lambda v: tag.setTvShowTitle(str(v)),
        "plot": lambda v: tag.setPlot(str(v)),
        "plotoutline": lambda v: tag.setPlotOutline(str(v)),
        "tagline": lambda v: tag.setTagLine(str(v)),
        "year": lambda v: tag.setYear(int(v)),
        "season": lambda v: tag.setSeason(int(v)),
        "episode": lambda v: tag.setEpisode(int(v)),
        "duration": lambda v: tag.setDuration(int(v)),          # SECONDS
        "mpaa": lambda v: tag.setMpaa(str(v)),
        "premiered": lambda v: tag.setPremiered(str(v)[:10]),   # YYYY-MM-DD
        "genre": lambda v: tag.setGenres(_list(v)),
        "studio": lambda v: tag.setStudios(_list(v)),
        "country": lambda v: tag.setCountries(_list(v)),
        "director": lambda v: tag.setDirectors(_list(v)),
        "writer": lambda v: tag.setWriters(_list(v)),
        "rating": lambda v: tag.setRating(_f(v), 0, "imdb", True),
        "userrating": lambda v: tag.setUserRating(int(v)),
        "trailer": lambda v: tag.setTrailer(str(v)),
        "cast": lambda v: tag.setCast(_actors(v)),
    }
    for key, value in info.items():
        if value in (None, "", []):
            continue
        fn = setters.get(key)
        if not fn:
            continue                                  # forward compatibility
        try:
            fn(value)
        except Exception as exc:                      # noqa: BLE001
            xbmc.log("[anchor] could not set %s: %s" % (key, exc),
                     xbmc.LOGWARNING)


def play_url(params):
    """Play an ALREADY-resolved external stream that carries its own identity.

    The app hands us the resolved stream ``url`` plus the identity
    (``imdb`` / ``type`` / ``season`` / ``episode``) and display metadata.
    Whatever the app omits stays blank - Anchor performs no metadata lookups.

    Display metadata arrives two ways, and both are honoured:

    * **Flat params** - the original contract
      (``title``/``year``/``plot``/``genre``/``show``/``poster``/``fanart``).
    * **``meta``** - one percent-encoded JSON object keyed by KODI's own
      vocabulary (``rating``, ``duration``, ``mpaa``, ``premiered``, ``studio``,
      ``director``, ``writer``, ``cast``, ``art{...}``, ...). Unknown keys are
      ignored, so the app can send a field before Anchor learns it, and an older
      Anchor keeps working against a newer app.

    ``meta`` wins where the two overlap: it is the newer, richer channel.
    """
    url = params.get("url")
    if not url:
        xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
        return

    ctype = ("series" if (params.get("type") or "movie").lower()
             in ("tv", "series", "episode") else "movie")

    # Identity: `imdb` may be a bare tt.. or the full tt..:season:episode.
    bits = (params.get("imdb") or "").strip().split(":")
    imdb = bits[0]
    season = params.get("season") or (bits[1] if len(bits) > 2 else None)
    episode = params.get("episode") or (bits[2] if len(bits) > 2 else None)

    def _i(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    content_id = imdb
    if ctype == "series" and imdb and _i(season) is not None \
            and _i(episode) is not None:
        content_id = "%s:%d:%d" % (imdb, _i(season), _i(episode))

    li = xbmcgui.ListItem(label=params.get("title") or url, offscreen=True)
    li.setPath(url)
    li.setProperty("IsPlayable", "true")
    li.setContentLookup(False)  # skip Kodi's HEAD probe (breaks some servers)

    # The flat params are the base layer; `meta` (JSON) overlays it. Building one
    # dict first means the two channels can never disagree about what was set.
    meta = _parse_meta(params.get("meta"))
    info = {
        "title": params.get("title"),
        "plot": params.get("plot"),
        "year": params.get("year"),
        "genre": params.get("genre"),
    }
    if ctype == "series":
        info["tvshowtitle"] = params.get("show")
        info["season"] = season
        info["episode"] = episode
    info.update({k: v for k, v in meta.items() if k != "art" and v not in (None, "", [])})

    art = {}
    if params.get("poster"):
        art["poster"] = params["poster"]
    if params.get("fanart"):
        art["fanart"] = params["fanart"]
    # `thumb` used to be aliased to the poster unconditionally, which silently
    # discarded an episode still the app supplied. Alias only as a FALLBACK.
    if params.get("thumb"):
        art["thumb"] = params["thumb"]
    for k, v in (meta.get("art") or {}).items():
        if isinstance(v, str) and v:
            art[k] = v
    if "thumb" not in art and art.get("poster"):
        art["thumb"] = art["poster"]

    tag = li.getVideoInfoTag()
    tag.setMediaType("episode" if ctype == "series" else "movie")
    _apply_info(tag, info)
    if imdb.startswith("tt"):
        tag.setUniqueIDs({"imdb": imdb}, "imdb")
        tag.setIMDBNumber(imdb)
    if art:
        li.setArt(art)

    if imdb.startswith("tt"):
        # Stash the EXACT id so the service identifies this playback with zero
        # guessing, then bind it to this URL. The stashed "filename" is the
        # RELEASE name from the cast URL (debrid URLs end with it) rather than
        # the display title - kept for diagnostics and release-aware tooling.
        base = unquote(urlparse(url).path.rsplit("/", 1)[-1])
        looks_like_release = "." in base and len(base) > len(
            params.get("title") or "")
        _stash_id(content_id, ctype,
                  base if looks_like_release else (params.get("title") or ""),
                  "")
        HOME.setProperty(PROP + "playing_file", url)
    else:
        # Id-less cast (e.g. Live TV): CLEAR the stash rather than leaving the
        # previous item's identity live, or the service would keep scrobbling
        # the last movie against this stream.
        _clear_stash()

    xbmcplugin.setResolvedUrl(HANDLE, True, li)


# ---------------------------------------------------------------------------
# Utility menu
# ---------------------------------------------------------------------------

def view_root():
    """Anchor has no catalogs - just the handful of things worth a button."""
    from anchor import sync
    rows = []
    for name, mod, display in sync.BACKENDS:
        if hasattr(mod, "configured") and not mod.configured():
            continue          # no usable app credentials in this build
        if mod.is_authorized():
            rows.append(("%s: [COLOR lime]signed in[/COLOR]" % display,
                         name + "_auth"))
            rows.append(("Sign out of %s" % display, name + "_logout"))
        else:
            rows.append(("[COLOR gold]Authorize %s[/COLOR]" % display,
                         name + "_auth"))
        if mod.reauth_needed():
            rows.insert(0, ("[COLOR gold][B]%s sign-in expired - "
                            "re-authorize[/B][/COLOR]" % display,
                            name + "_auth"))
    rows.append(("Clear cache", "clear_cache"))
    for label, action in rows:
        li = xbmcgui.ListItem(label=label)
        xbmcplugin.addDirectoryItem(HANDLE, build_url(action=action), li, False)
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def _auth_flow(mod, display, where, after=None):
    """Shared code-entry authorization dialog.

    Trakt (device code) and Simkl (PIN) are the same interaction: get a short
    code, show it with a URL, poll until the user enters it.
    """
    if hasattr(mod, "configured") and not mod.configured():
        notify("Enter your %s app credentials in Settings first" % display)
        return
    if mod.is_authorized():
        if not xbmcgui.Dialog().yesno(display, "Already authorized. Re-authorize?"):
            return
    dev = mod.device_code()
    if not dev:
        notify("%s: couldn't start auth" % display)
        return
    dlg = xbmcgui.DialogProgress()
    dlg.create("Authorize %s" % display,
               "On any device go to:\n[B]%s[/B]\nand enter code:  [B]%s[/B]"
               % (dev.get("verification_url") or where,
                  dev.get("user_code", "")))
    ok = mod.poll_token(dev, should_cancel=dlg.iscanceled)
    dlg.close()
    if ok and after:
        try:
            after()
        except Exception:  # noqa: BLE001 - a cosmetic confirmation must never fail auth
            pass
    notify("%s authorized" % display if ok
           else "%s authorization failed/cancelled" % display)


def action_trakt_auth():
    """Authorize Trakt via the device-code flow (watch history + resume)."""
    _auth_flow(trakt, "Trakt", "auth.trakt.tv/activate")


def action_trakt_logout():
    trakt.logout()
    notify("Trakt signed out")


def action_simkl_auth():
    """Authorize Simkl via the PIN flow (watch history + resume)."""
    # One POST /users/settings after success confirms the token really works,
    # so a broken sign-in surfaces here rather than as silent no-op scrobbles.
    _auth_flow(simkl, "Simkl", "simkl.com/pin", after=simkl.account_name)


def action_simkl_logout():
    simkl.logout()
    notify("Simkl signed out")


def action_clear_cache():
    """Wipe the on-disk result cache (IntroDB segments, playback lists)."""
    cache_dir = client._disk_dir()
    removed = 0
    try:
        for fn in os.listdir(cache_dir):
            if fn.endswith(".json"):
                try:
                    os.remove(os.path.join(cache_dir, fn))
                    removed += 1
                except OSError:
                    pass
    except OSError:
        pass
    notify("Cleared cache (%d item%s)" % (removed, "" if removed == 1 else "s"))


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def _terminate(action):
    """Make sure every action closes its handle so Kodi never hangs."""
    if action == "play_url":
        xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
    elif action is None:
        try:
            xbmcplugin.endOfDirectory(HANDLE, succeeded=False)
        except Exception:  # noqa: BLE001
            pass


def router_dispatch():
    params = dict(parse_qsl(sys.argv[2][1:]))
    action = params.get("action")
    try:
        if not action:
            view_root()
        elif action == "play_url":
            play_url(params)
        elif action == "trakt_auth":
            action_trakt_auth()
        elif action == "trakt_logout":
            action_trakt_logout()
        elif action == "simkl_auth":
            action_simkl_auth()
        elif action == "simkl_logout":
            action_simkl_logout()
        elif action == "clear_cache":
            action_clear_cache()
        else:
            # Unknown action (e.g. a stale favourite/widget). Show the menu
            # rather than leaving the handle open, which hangs Kodi on
            # "Working...".
            view_root()
    except Exception as exc:  # noqa: BLE001 - never leave Kodi spinning on a handle
        xbmc.log("[anchor] action %r failed: %r" % (action, exc),
                 xbmc.LOGERROR)
        _terminate(action)
        raise


if __name__ == "__main__":
    router_dispatch()
