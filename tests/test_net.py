"""_net.http_json is the designated safety boundary: it must NEVER raise (it
runs on Kodi's player-callback thread) and reports transport failure as status 0.
"""

from anchor import _net


def test_transport_failure_returns_zero_and_never_raises():
    # Connection refused on a closed local port - exercises the active branch
    # (requests or urllib) without any external network dependency.
    status, body = _net.http_json("GET", "http://127.0.0.1:1/nope", {}, timeout=1)
    assert status == 0
    assert body == ""


def test_returns_a_status_body_tuple():
    out = _net.http_json("GET", "http://127.0.0.1:1/nope", {}, timeout=1)
    assert isinstance(out, tuple) and len(out) == 2
