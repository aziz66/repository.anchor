"""ScrobbleGate - the shared dedupe / rate / watched-latch rules both backends
depend on. This is the single most behaviour-critical unit in the library."""

from anchor import _gate

CID = "tt1375666"


def gate(**kw):
    return _gate.ScrobbleGate("test", **kw)


def _post(g, action, cid, pct, now, success=True):
    """Run the check->record cycle as a real backend would."""
    ok, pct = g.check(action, cid, pct, now)
    if ok:
        g.record(action, cid, pct, now, success=success)
    return ok, pct


def test_start_seeds_the_latch_and_a_duplicate_start_is_suppressed():
    g = gate()
    assert _post(g, "start", CID, 5.0, 0.0)[0] is True
    # A redundant start would wipe the stored resume point - it must be dropped.
    assert _post(g, "start", CID, 6.0, 0.1)[0] is False
    assert g.open_cid == CID


def test_a_failed_start_does_not_latch_so_the_retry_still_goes_out():
    g = gate()
    ok, _ = g.check("start", CID, 5.0, 0.0)
    g.record("start", CID, 5.0, 0.0, success=False)   # e.g. Simkl 404 id_err
    assert ok is True
    assert g.open_cid is None
    # A later start with a distinct position is allowed (nothing latched).
    assert _post(g, "start", CID, 25.0, 2.0)[0] is True


def test_stop_clears_the_latch():
    g = gate()
    _post(g, "start", CID, 5.0, 0.0)
    _post(g, "stop", CID, 50.0, 2.0)
    assert g.open_cid is None


def test_sub_one_percent_stop_is_clamped_to_one_when_a_session_is_open():
    g = gate()
    _post(g, "start", CID, 5.0, 0.0)
    ok, pct = g.check("stop", CID, 0.3, 2.0)
    assert ok is True and pct == 1.0   # else the service is left open forever


def test_sub_one_percent_stop_is_dropped_when_no_session_is_open():
    g = gate()
    assert g.check("stop", CID, 0.3, 0.0)[0] is False


def test_pause_at_watched_threshold_is_suppressed_only_for_trakt_style():
    trakt_like = gate(suppress_pause_at_watched=True)
    _post(trakt_like, "start", CID, 5.0, 0.0)
    assert trakt_like.check("pause", CID, 90.0, 2.0)[0] is False   # Trakt 422s it

    simkl_like = gate(suppress_pause_at_watched=False)
    _post(simkl_like, "start", CID, 5.0, 0.0)
    assert simkl_like.check("pause", CID, 90.0, 2.0)[0] is True    # Simkl stores it


def test_identical_reemission_is_deduped_within_the_window():
    g = gate(dedupe_window=10.0)
    _post(g, "start", CID, 5.0, 0.0)
    _post(g, "pause", CID, 40.0, 2.0)
    assert g.check("pause", CID, 40.0, 5.0)[0] is False   # same pos, inside window
    assert g.check("pause", CID, 40.0, 13.0)[0] is True   # window elapsed


def test_rate_floor_applies_to_non_stop_but_stop_is_exempt():
    g = gate(min_post_interval=1.2)
    _post(g, "start", CID, 5.0, 100.0)
    # a second non-stop inside the floor is dropped...
    assert g.check("pause", CID, 40.0, 100.3)[0] is False
    # ...but a stop inside the floor must still go out (a dead link dies fast).
    assert g.check("stop", CID, 41.0, 100.3)[0] is True


def test_reset_clears_all_state():
    g = gate()
    _post(g, "start", CID, 5.0, 0.0)
    g.reset()
    assert g.open_cid is None and g.last is None
