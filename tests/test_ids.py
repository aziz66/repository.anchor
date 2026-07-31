from anchor import ids


def test_split_movie_id_has_no_season_episode():
    assert ids.split_series_id("tt1375666") == ("tt1375666", None, None)


def test_split_packed_episode_id():
    assert ids.split_series_id("tt0903747:1:2") == ("tt0903747", 1, 2)


def test_split_ignores_non_numeric_suffix():
    # kitsu/tmdb prefixed ids must not be mistaken for season:episode.
    assert ids.split_series_id("kitsu:1") == ("kitsu:1", None, None)
    assert ids.split_series_id("tmdb:603") == ("tmdb:603", None, None)


def test_base_id_strips_the_episode_suffix():
    assert ids.base_id("tt0903747:1:2") == "tt0903747"
    assert ids.base_id("tt1375666") == "tt1375666"
