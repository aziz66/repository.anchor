"""Test bootstrap: put the Kodi stubs and both add-on source trees on sys.path
BEFORE any add-on module imports, and shape sys.argv the way Kodi would (the
plugin reads sys.argv[1]/[2] at import time).
"""

import os
import sys

import pytest

_TESTS = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_TESTS)

# Kodi stubs first so `import xbmc` resolves to ours.
for p in (
    os.path.join(_TESTS, "stubs"),
    os.path.join(_ROOT, "script.module.anchor", "lib"),   # the `anchor` package
    os.path.join(_ROOT, "plugin.video.anchor"),           # default/service/popup
):
    if p not in sys.path:
        sys.path.insert(0, p)

# default.py: HANDLE = int(sys.argv[1]); BASE_URL = sys.argv[0].
sys.argv = ["plugin://plugin.video.anchor/", "1", "?action="]


@pytest.fixture
def home():
    """A fresh, empty home window (clears the shared property store)."""
    import xbmcgui
    xbmcgui.windows.clear()
    return xbmcgui.Window(10000)


@pytest.fixture
def settings():
    """Empty, isolated add-on settings dict for the test."""
    import xbmcaddon
    xbmcaddon.settings.clear()
    yield xbmcaddon.settings
    xbmcaddon.settings.clear()


@pytest.fixture
def stub_http(monkeypatch):
    """Replace the shared HTTP transport with a recorder + scripted responses.

    Returns a controller: ``.responses`` maps a URL substring -> (status, body);
    ``.calls`` is the list of (method, url, headers, body) actually sent.
    """
    from anchor import _net

    class _Ctl:
        def __init__(self):
            self.responses = {}
            self.calls = []

        def __call__(self, method, url, headers, body=None, user_agent=None, timeout=None):
            self.calls.append((method, url, headers, body))
            for frag, resp in self.responses.items():
                if frag in url:
                    return resp
            return 200, ""

    ctl = _Ctl()
    monkeypatch.setattr(_net, "http_json", ctl)
    # trakt/simkl call _net.http_json indirectly via their own _http helpers,
    # which reference the module attribute at call time, so patching _net is
    # enough. Return the controller for scripting.
    return ctl
