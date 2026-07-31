"""Persistence location for the add-on's own state (Trakt/Simkl tokens).

Resolves the using add-on's profile dir. The token files written there embed
per-user secrets, so callers create them ``0600``.
"""

from __future__ import annotations

import os

# Which add-on's addon_data holds persistent state (Trakt/Simkl tokens). The
# using add-on sets this to its own id at import time - important because Kodi
# DELETES an add-on's addon_data when it is uninstalled, so state stored under
# another add-on's id would disappear with it.
DATA_ADDON = "plugin.video.anchor"


try:
    import xbmcvfs

    def _store_dir():
        d = xbmcvfs.translatePath(
            "special://profile/addon_data/%s/" % DATA_ADDON)
        if not xbmcvfs.exists(d):
            xbmcvfs.mkdirs(d)
        return d
except ImportError:  # outside Kodi (tests)
    def _store_dir():
        return os.getcwd()
