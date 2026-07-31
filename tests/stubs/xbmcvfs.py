"""Minimal xbmcvfs stub. Maps special:// paths under a per-run temp dir so the
disk cache and token storage exercise their real Kodi code path (client._KODI
is True when this stub imports) without a Kodi install."""

import os
import tempfile

_root = tempfile.mkdtemp(prefix="anchor-test-")


def translatePath(path):
    if path.startswith("special://"):
        rel = path.split("://", 1)[1]
        return os.path.join(_root, *rel.split("/"))
    return path


def exists(path):
    return os.path.exists(path)


def mkdirs(path):
    try:
        os.makedirs(path, exist_ok=True)
        return True
    except OSError:
        return False


def delete(path):
    try:
        os.remove(path)
        return True
    except OSError:
        return False
