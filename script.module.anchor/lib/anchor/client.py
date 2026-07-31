"""Cross-session on-disk cache (and logging) for the Anchor add-ons.

Pure-ish module: it imports ``xbmc`` only for logging and falls back to ``print``
when run outside Kodi (handy for tests).
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import time

try:
    import xbmcvfs
    _KODI = True
except ImportError:  # running outside Kodi (tests)
    _KODI = False


# ---------------------------------------------------------------------------
# Cross-session cache: a single on-disk store (0600). Each Kodi click is a fresh
# process so an in-memory cache never helps; the disk cache survives across
# navigations AND across Kodi restarts, keeps secret-bearing stream URLs out of
# the addon-readable Window properties, and is thread-safe (each key is its own
# file written atomically - no lock needed).
# ---------------------------------------------------------------------------

# Which add-on's addon_data holds the cache. The using add-on sets this to its
# own id at import time so its "clear cache" only touches its own files.
CACHE_ADDON = "plugin.video.anchor"


def _disk_dir():
    if _KODI:
        d = xbmcvfs.translatePath(
            "special://profile/addon_data/%s/cache/" % CACHE_ADDON)
    else:
        d = os.path.join(os.getcwd(), "spcache")
    try:
        os.makedirs(d, exist_ok=True)
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def _disk_path(key):
    return os.path.join(_disk_dir(),
                        hashlib.sha1(key.encode("utf-8")).hexdigest()[:16] + ".json")


def disk_get(key):
    """Return ``(hit, value)``; ``value`` may be None (a cached failure)."""
    try:
        with open(_disk_path(key), encoding="utf-8") as fh:
            exp, val = json.load(fh)
    except (OSError, ValueError):
        return False, None
    if exp < time.time():
        return False, None
    return True, val


def disk_set(key, val, ttl):
    path = _disk_path(key)
    tmp = "%s.%d.tmp" % (path, os.getpid())  # per-process tmp avoids cross-writes
    try:
        # 0600 from creation (not chmod-after) so a stream URL's debrid token is
        # never momentarily world-readable on a shared host.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump([time.time() + ttl, val], fh)
        os.replace(tmp, path)
    except OSError:
        return
    if random.random() < 0.05:  # amortise the listdir+sort (cheap on Android/SD)
        _disk_prune()


def _disk_prune(limit=400):
    """Keep the cache dir bounded; drop oldest files past the cap."""
    try:
        d = _disk_dir()
        files = [os.path.join(d, f) for f in os.listdir(d) if f.endswith(".json")]
        if len(files) <= limit:
            return
        files.sort(key=lambda p: os.path.getmtime(p))
        for p in files[:len(files) - limit]:
            try:
                os.remove(p)
            except OSError:
                pass
    except OSError:
        pass
