"""Minimal xbmcgui stub.

The Window stub backs its properties in a MODULE-LEVEL dict keyed by window id,
so a ``Window(10000)`` created in the plugin process and another in the service
process share state - exactly as Kodi's real home window does. That property
hand-off is the entire identity contract between the two Anchor processes, so
tests depend on it being modelled faithfully.
"""

NOTIFICATION_INFO = "info"
NOTIFICATION_WARNING = "warning"
NOTIFICATION_ERROR = "error"
DLG_YESNO_NO_BTN = 10
DLG_YESNO_YES_BTN = 11

windows = {}  # window id -> {property: value}


class Window:
    def __init__(self, wid=0):
        self._id = wid
        windows.setdefault(wid, {})

    def getProperty(self, key):
        return windows[self._id].get(key, "")

    def setProperty(self, key, value):
        windows[self._id][key] = value

    def clearProperty(self, key):
        windows[self._id].pop(key, None)


# Every InfoTag setter call, in order: {"setTitle": [("T",)], ...}. Cleared by
# `reset()`. Recording rather than swallowing lets a test assert on the metadata
# contract, which otherwise fails silently: a dropped field just looks like a
# slightly emptier info screen.
info_calls = {}


class _InfoTag:
    """Accepts any setX(...) call InfoTagVideo exposes, and records it."""
    def __getattr__(self, name):
        def record(*a, **k):
            info_calls.setdefault(name, []).append(a)
        return record


class ListItem:
    #: The most recently constructed item, so a test can read back its art.
    last = None

    def __init__(self, label="", label2="", path="", offscreen=False):
        ListItem.last = self
        self.label = label
        self._path = path
        self._props = {}
        self._art = {}

    def setPath(self, path):
        self._path = path

    def getPath(self):
        return self._path

    def setProperty(self, key, value):
        self._props[key] = value

    def getProperty(self, key):
        return self._props.get(key, "")

    def setContentLookup(self, enabled):
        pass

    def setArt(self, art):
        self._art = dict(art)

    def getArt(self, key=None):
        return self._art if key is None else self._art.get(key, "")

    def setInfo(self, *a, **k):
        pass

    def getVideoInfoTag(self):
        return _InfoTag()


class Dialog:
    def notification(self, *a, **k):
        pass

    def yesno(self, *a, **k):
        return False

    def ok(self, *a, **k):
        return True

    def select(self, *a, **k):
        return -1


class DialogProgress:
    def create(self, *a, **k):
        pass

    def update(self, *a, **k):
        pass

    def iscanceled(self):
        return False

    def close(self):
        pass


class WindowXMLDialog:
    def __init__(self, *args, **kwargs):
        pass

    def doModal(self):
        pass

    def close(self):
        pass

    def getControl(self, control_id):
        return _Control()


class _Control:
    def __getattr__(self, name):
        return lambda *a, **k: None


ControlButton = object
ControlImage = object
ControlLabel = object


def reset():
    """Forget recorded InfoTag calls and the last ListItem, between tests."""
    info_calls.clear()
    ListItem.last = None
