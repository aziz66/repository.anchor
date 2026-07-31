"""sync fan-out + the cross-service timestamp comparator.

The comparator matters because Trakt emits millisecond precision
("...29.000Z") and Simkl whole seconds ("...29Z"); a naive string compare would
rank the same instant differently and offer the wrong service's resume point.
"""

import pytest
from anchor import simkl, sync, trakt

CID = "tt1375666"


# --- _ts comparator --------------------------------------------------------

def test_same_second_across_precisions_compares_equal():
    assert sync._ts("2026-07-22T10:15:29.000Z") == sync._ts("2026-07-22T10:15:29Z")


def test_string_compare_would_have_been_wrong():
    # The bug this guards against: '.' (0x2E) sorts before 'Z' (0x5A).
    assert "2026-07-22T10:15:29.000Z" < "2026-07-22T10:15:29Z"
    assert not sync._ts("2026-07-22T10:15:29.000Z") < sync._ts("2026-07-22T10:15:29Z")


def test_a_genuinely_later_time_sorts_after():
    assert sync._ts("2026-07-22T10:15:30Z") > sync._ts("2026-07-22T10:15:29.000Z")


def test_unparseable_sorts_below_every_real_timestamp():
    assert sync._ts(None) < sync._ts("2000-01-01T00:00:00Z")
    assert sync._ts("") < sync._ts("2000-01-01T00:00:00Z")


# --- fan-out ---------------------------------------------------------------

@pytest.fixture
def both_connected(monkeypatch):
    monkeypatch.setattr(sync, "is_enabled", lambda name: True)
    monkeypatch.setattr(trakt, "is_authorized", lambda: True)
    monkeypatch.setattr(simkl, "is_authorized", lambda: True)
    # BYO creds: both backends must be "configured" (client id/secret entered)
    # for sync to consider them.
    monkeypatch.setattr(trakt, "_client_id", lambda: "cid")
    monkeypatch.setattr(trakt, "_client_secret", lambda: "sec")
    monkeypatch.setattr(simkl, "_client_id", lambda: "cid")


def test_a_backend_without_credentials_is_skipped(monkeypatch):
    monkeypatch.setattr(sync, "is_enabled", lambda name: True)
    monkeypatch.setattr(trakt, "is_authorized", lambda: True)
    monkeypatch.setattr(simkl, "is_authorized", lambda: True)
    monkeypatch.setattr(trakt, "_client_id", lambda: "cid")
    monkeypatch.setattr(trakt, "_client_secret", lambda: "sec")
    monkeypatch.setattr(simkl, "_client_id", lambda: "")   # no Simkl creds entered
    names = [n for n, _m, _d in sync.backends()]
    assert names == ["trakt"]   # Simkl is not configured -> excluded


def test_scrobble_fans_out_to_every_connected_backend(both_connected, monkeypatch):
    calls = []
    monkeypatch.setattr(trakt, "scrobble", lambda *a: calls.append(("trakt", a)) or 201)
    monkeypatch.setattr(simkl, "scrobble", lambda *a: calls.append(("simkl", a)) or 201)
    out = sync.scrobble("start", "movie", CID, 5.0)
    assert set(out) == {"trakt", "simkl"}
    assert {c[0] for c in calls} == {"trakt", "simkl"}


def test_one_backend_failing_does_not_stop_the_other(both_connected, monkeypatch):
    def boom(*a):
        raise RuntimeError("trakt down")
    monkeypatch.setattr(trakt, "scrobble", boom)
    monkeypatch.setattr(simkl, "scrobble", lambda *a: 201)
    out = sync.scrobble("start", "movie", CID, 5.0)
    assert out["trakt"] == 0 and out["simkl"] == 201   # best-effort, isolated


def test_resume_picks_the_most_recently_paused_service(both_connected, monkeypatch):
    monkeypatch.setattr(trakt, "playback_progress",
                        lambda ct, cid: {"progress": 30, "id": 11,
                                         "paused_at": "2026-07-22T10:00:00.000Z"})
    monkeypatch.setattr(simkl, "playback_progress",
                        lambda ct, cid: {"progress": 61, "id": 22,
                                         "paused_at": "2026-07-22T11:00:00Z"})
    pct, entries, ok = sync.resume("movie", CID)
    assert ok is True and pct == 61                     # the newer (Simkl) wins
    assert sorted(entries) == [("simkl", 22), ("trakt", 11)]  # both cleared on "start over"


def test_resume_reports_failure_only_when_it_has_nothing_to_offer(both_connected, monkeypatch):
    def down(ct, cid):
        raise trakt.TraktUnavailable("boom")
    monkeypatch.setattr(trakt, "playback_progress", down)
    monkeypatch.setattr(simkl, "playback_progress",
                        lambda ct, cid: {"progress": 40, "id": 9,
                                         "paused_at": "2026-07-22T11:00:00Z"})
    pct, _e, ok = sync.resume("movie", CID)
    assert pct == 40 and ok is True   # a healthy backend's position still offered
