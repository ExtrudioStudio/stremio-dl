from __future__ import annotations

import pytest

from stremio_dl.models import EpisodeRef, Title
from stremio_dl.quality import lower_than, matches, parse_quality
from stremio_dl.selection import (
    SelectionError,
    normalize,
    parse_selection,
    split_spec,
    summarize,
)


def refs(*codes: tuple[int, int]) -> list[EpisodeRef]:
    return [EpisodeRef(s, e) for s, e in codes]


def test_friends_metadata_excludes_specials(friends: Title):
    seasons = friends.seasons()
    assert sorted(seasons) == list(range(1, 11))
    assert [len(seasons[s]) for s in range(1, 11)] == [24, 24, 25, 24, 24, 25, 24, 24, 24, 18]
    assert len(friends.episodes) == 236


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("s1 eps10 - s2 eps 3", "s1e10-s2e3"),
        ("Season 1 Episode 10 to Season 2 Episode 3", "s1e10-s2e3"),
        ("S01E10–S02E03", "s01e10-s02e03"),
        ("1x05", "s1e05"),
        ("s2 e1-5, s4 ep2", "s2e1-5,s4e2"),
        ("s1 s3", "s1,s3"),
        ("s1 and s3", "s1,s3"),
    ],
)
def test_normalize(text, expected):
    assert normalize(text) == expected


def test_the_example_from_the_brief(friends: Title):
    sel = parse_selection("s1 eps10 - s2 eps 3 /full hd", friends)
    assert sel.quality == "1080p"
    assert len(sel.refs) == 18
    assert sel.refs[0] == EpisodeRef(1, 10)
    assert sel.refs[14] == EpisodeRef(1, 24)
    assert sel.refs[15] == EpisodeRef(2, 1)
    assert sel.refs[-1] == EpisodeRef(2, 3)
    assert summarize(sel.refs) == "S01E10–S01E24, S02E01–S02E03"


@pytest.mark.parametrize(
    ("text", "count", "first", "last", "quality"),
    [
        ("s3", 25, (3, 1), (3, 25), None),
        ("s1-s3 /4k", 73, (1, 1), (3, 25), "2160p"),
        ("s1-3 /4k", 73, (1, 1), (3, 25), "2160p"),
        ("s2e1-5, s4e2 /720", 6, (2, 1), (4, 2), "720p"),
        ("all /best", 236, (1, 1), (10, 18), "best"),
        ("1x05", 1, (1, 5), (1, 5), None),
        ("s10e17 - s10", 2, (10, 17), (10, 18), None),
        ("s1e1, 3", 2, (1, 1), (1, 3), None),
        ("s1e2-e4 @ hd", 3, (1, 2), (1, 4), "720p"),
        ("s5 1080p", 24, (5, 1), (5, 24), "1080p"),
        ("s2e3 full hd", 1, (2, 3), (2, 3), "1080p"),
    ],
)
def test_selection_forms(friends, text, count, first, last, quality):
    sel = parse_selection(text, friends)
    assert len(sel.refs) == count
    assert sel.refs[0] == EpisodeRef(*first)
    assert sel.refs[-1] == EpisodeRef(*last)
    assert sel.quality == quality


def test_default_quality_is_used_when_none_given(friends):
    assert parse_selection("s1e1", friends, default_quality="720p").quality == "720p"


def test_duplicates_collapse_and_order_is_kept(friends):
    sel = parse_selection("s2e3, s1e1-2, s2e3", friends)
    assert sel.refs == refs((1, 1), (1, 2), (2, 3))


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("s12", "Season 12 doesn't exist"),
        ("s1e30", "S01E30 doesn't exist: season 1 has 24"),
        ("s2e3 - s1e5", "runs backwards"),
        ("e5", "needs a season"),
        ("s1-", "Can't understand"),
        ("banana", "Can't understand"),
        ("all - s2", "'all' can't be part of a range"),
        ("", "Say which episodes"),
    ],
)
def test_selection_errors(friends, text, message):
    with pytest.raises(SelectionError, match=message):
        parse_selection(text, friends)


def test_unknown_quality_is_an_error(friends):
    with pytest.raises(ValueError, match="Unknown quality"):
        parse_selection("s1 /8k", friends)


def test_movie_selection_is_quality_only():
    movie = Title("tt1375666", "movie", "Inception", 2010)
    assert parse_selection("4k", movie).quality == "2160p"
    assert parse_selection("/full hd", movie).quality == "1080p"
    assert parse_selection("", movie, "720p").quality == "720p"


def test_split_spec():
    assert split_spec("s1 /full hd") == ("s1", "full hd")
    assert split_spec("s1@4k") == ("s1", "4k")
    assert split_spec("s1-s2") == ("s1-s2", None)


@pytest.mark.parametrize(
    ("text", "res"),
    [
        ("4K", "2160p"),
        ("uhd", "2160p"),
        ("Full HD", "1080p"),
        ("full-hd", "1080p"),
        ("fhd", "1080p"),
        ("1080", "1080p"),
        ("HD", "720p"),
        ("720p", "720p"),
        ("sd", "480p"),
        ("Best", "best"),
    ],
)
def test_quality_aliases(text, res):
    assert parse_quality(text) == res


def test_quality_helpers():
    assert matches("480p", "unknown")
    assert not matches("1080p", "720p")
    assert matches("best", "360p")
    assert lower_than("1080p") == ["720p", "480p", "360p"]
