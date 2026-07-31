"""Fan-out facade over the scrobbling backends (Trakt, Simkl).

The user may connect either service, both, or neither. With both connected
every playback event goes to both, and the resume prompt offers whichever
service holds the most recent position.

Calls are deliberately **sequential**: Simkl's API rules only sanction parallel
requests against their Cloudflare-cached catalog endpoints, and everything here
is per-user data. They are also **independent** - one service being down,
unauthorized or rate-limited must never stop the other from recording playback.

Which services are active is decided by the add-on, not the library::

    from anchor import sync
    sync.is_enabled = lambda name: ADDON.getSetting(name + "_scrobble") != "false"
"""

from __future__ import annotations

import re

from . import simkl, trakt

try:
    import xbmc
    def log(msg, level=1):
        xbmc.log("[anchor.sync] " + msg, level)
except ImportError:  # outside Kodi (tests)
    def log(msg, level=1):
        print("[sync] " + msg)


# (setting key / internal name, module, display name)
BACKENDS = (
    ("trakt", trakt, "Trakt"),
    ("simkl", simkl, "Simkl"),
)


def is_enabled(name):        # overridden by the add-on; default: all on
    return True


def _usable(name, mod):
    if not is_enabled(name):
        return False
    if hasattr(mod, "configured") and not mod.configured():
        return False         # e.g. Simkl without a real client_id in this build
    return True


def backends(authorized_only=True):
    """The active backends as ``(name, module, display)`` triples."""
    out = []
    for name, mod, display in BACKENDS:
        if not _usable(name, mod):
            continue
        if authorized_only and not mod.is_authorized():
            continue
        out.append((name, mod, display))
    return out


def any_authorized():
    return bool(backends())


def reauth_names():
    """Display names of connected services whose sign-in has lapsed."""
    out = []
    for name, mod, display in BACKENDS:
        if not _usable(name, mod):
            continue
        try:
            if mod.reauth_needed():
                out.append(display)
        except Exception:  # noqa: BLE001
            pass
    return out


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------
# Both services emit ISO-8601 UTC, but NOT to the same precision: Trakt sends
# "2014-10-15T22:21:29.000Z" and Simkl "2015-03-15T15:30:11Z". Comparing those
# as plain strings is wrong - "." (0x2E) sorts before "Z" (0x5A), so at the same
# second the Trakt row reads as OLDER and we would offer the wrong service's
# resume point. Parse to a comparable tuple instead. Second granularity is
# plenty for "which did the user pause more recently", so fractional seconds
# are simply ignored and no timezone handling is needed (both are UTC-Z).
_TS_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})")


def _ts(value):
    """ISO-8601 UTC string -> comparable tuple; ``()`` when unparseable (which
    sorts below every real timestamp, so a row with no usable time never wins
    against one that has one)."""
    if not isinstance(value, str):
        return ()
    m = _TS_RE.match(value)
    return tuple(int(g) for g in m.groups()) if m else ()


# ---------------------------------------------------------------------------
# Playback events
# ---------------------------------------------------------------------------

def scrobble(action, ctype, content_id, progress):
    """Report a playback event to every connected service.

    Returns ``{name: status}``. Best-effort: a failure in one backend is logged
    and never propagated, so it cannot stop the others or break playback.
    """
    out = {}
    for name, mod, _display in backends():
        try:
            out[name] = mod.scrobble(action, ctype, content_id, progress)
        except Exception as exc:  # noqa: BLE001 - never break playback for a scrobble
            log("%s scrobble/%s failed: %r" % (name, action, exc), 3)
            out[name] = 0
    return out


def resume(ctype, content_id):
    """Best saved position for this item across all connected services.

    Returns ``(pct, entries, ok)``:

    * ``pct``     - the most recently paused position, or None.
    * ``entries`` - ``[(name, item_id), ...]`` for EVERY service holding a
      position for this item. "Start over" must clear all of them, or the one
      left behind re-prompts on the next play.
    * ``ok``      - False when a lookup failed and we have nothing to offer, so
      the caller can retry rather than concluding "nothing to resume".
    """
    best = None          # (ts_key, name, entry)
    entries = []
    failed = False
    for name, mod, _display in backends():
        try:
            entry = mod.playback_progress(ctype, content_id)
        except Exception as exc:  # noqa: BLE001 - includes {Trakt,Simkl}Unavailable
            failed = True
            log("%s resume lookup unavailable: %s" % (name, exc), 2)
            continue
        if not entry:
            continue
        if entry.get("id"):
            entries.append((name, entry["id"]))
        pct = entry.get("progress")
        if not pct:
            continue
        key = _ts(entry.get("paused_at"))
        if best is None or key > best[0]:
            best = (key, name, entry)
    if best is not None:
        log("resume %s: using %s @%.0f%%" % (content_id, best[1],
                                             best[2].get("progress") or 0))
    # A position from a healthy service is worth offering even if the other one
    # was unreachable; only report failure when we have nothing to show for it.
    return ((best[2].get("progress") if best else None), entries,
            (best is not None) or not failed)


def clear_playback(entries):
    """Drop the stored resume point on every service that had one."""
    by_name = {name: mod for name, mod, _d in BACKENDS}
    for name, item_id in entries or []:
        mod = by_name.get(name)
        if not mod:
            continue
        try:
            mod.clear_playback(item_id)
        except Exception as exc:  # noqa: BLE001
            log("%s clear playback failed: %r" % (name, exc), 2)


def maintenance():
    """Periodic upkeep for backends that need it.

    Only Trakt does: its access token expires and the refresh chain has to be
    kept rotated even across idle stretches. Simkl tokens never expire, and its
    API rules forbid background timers - so it is deliberately a no-op here.
    """
    for name, mod, _display in backends():
        keep = getattr(mod, "keepalive", None)
        if keep is None:
            continue
        try:
            keep()
        except Exception as exc:  # noqa: BLE001
            log("%s keepalive failed: %r" % (name, exc), 2)
