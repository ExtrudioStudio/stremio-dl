from __future__ import annotations

from stremio_dl.config import Config
from stremio_dl.streams import (
    choose,
    merge,
    normalize_stream,
    parse_languages,
    parse_size,
    rank,
    rejection_reason,
    usable,
)


def _all(streams):
    return [c for s in streams if (c := normalize_stream(s, "Torrentio"))]


def test_normalizes_the_recorded_torrentio_response(friends_streams):
    cands = _all(friends_streams)
    assert len(cands) == len(friends_streams) == 58
    first = cands[0]
    assert first.info_hash == "cf65fec8f421173f1a36e75725066ade65658456"  # anonymized
    assert first.file_idx == 31
    assert first.resolution == "2160p"
    assert first.seeders == 21
    assert abs(first.size - 6.55 * 1024**3) < 1024**2
    assert first.source == "Rutracker"
    assert first.languages == ("en", "ru")
    assert first.is_pack
    assert first.hdr == ("DV", "HDR")
    assert first.filename == "S01E10.The.One.with.the.Monkey.2160p.REMUX-002p013.mkv"
    assert "udp://tracker.opentrackr.org:1337/announce" in first.trackers


def test_labels_without_a_resolution_fall_back_to_the_file_name(friends_streams):
    cands = {c.label: c for c in _all(friends_streams)}
    # "BDRip"/"DVDRip"/"WEBRip" labels carry no resolution; with nothing in the file name
    # either, the release counts as SD ("unknown").
    assert cands["DVDRip"].resolution == "unknown"
    by_res = {c.resolution for c in _all(friends_streams)}
    assert {"2160p", "1080p", "720p", "unknown"} <= by_res


def test_foreign_only_releases_are_rejected_by_default(friends_streams):
    cfg = Config()
    reasons = [rejection_reason(c, cfg) for c in _all(friends_streams)]
    assert reasons.count("audio language fr") == 3
    assert reasons.count("audio language ru") == 3
    # Multi-audio releases that include English stay.
    assert all(r is None for c, r in zip(_all(friends_streams), reasons, strict=True)
               if "en" in c.languages)  # fmt: skip


def test_language_filter_can_be_disabled(friends_streams):
    cfg = Config(languages=["any"])
    assert len(usable(_all(friends_streams), cfg)) == 58


def test_highest_seeded_1080p_english_release_wins(friends_streams):
    cands = usable(_all(friends_streams), Config())
    ranked = rank(cands, "1080p")
    assert ranked, "expected 1080p candidates"
    winner = ranked[0]
    assert winner.resolution == "1080p"
    assert winner.seeders == max(c.seeders for c in cands if c.resolution == "1080p")
    assert [c.seeders for c in ranked] == sorted((c.seeders for c in ranked), reverse=True)
    assert not winner.languages or "en" in winner.languages


def test_best_prefers_resolution_then_seeders(friends_streams):
    ranked = rank(usable(_all(friends_streams), Config()), "best")
    assert ranked[0].resolution == "2160p"


def test_choose_falls_back_to_lower_resolution_only_when_asked(friends_streams):
    cands = [c for c in usable(_all(friends_streams), Config()) if c.resolution != "1080p"]
    assert choose(cands, "1080p", "skip", 5) == ([], None)
    picked, used = choose(cands, "1080p", "lower", 5)
    assert used == "720p" and len(picked) == 5


def _stream(**kw):
    base = {
        "name": "Torrentio\n1080p",
        "title": "Show.S01E01.1080p.WEB\n👤 10 💾 1.2 GB ⚙️ 1337x",
        "infoHash": "a" * 40,
        "behaviorHints": {"filename": "Show.S01E01.1080p.WEB.mkv"},
    }
    base.update(kw)
    return base


def test_executables_and_cams_are_rejected():
    cfg = Config()
    exe = normalize_stream(_stream(behaviorHints={"filename": "Show.S01E01.1080p.exe"}))
    assert rejection_reason(exe, cfg) == "not a video file (.exe)"
    cam = normalize_stream(_stream(title="Movie.2026.HDCAM.x264\n👤 99 💾 1 GB ⚙️ x"))
    cam = cam.__class__(**{**cam.to_dict(), "filename": "Movie.2026.HDCAM.x264.mkv"})
    assert rejection_reason(cam, cfg) == "cam/screener release"
    ok = normalize_stream(_stream())
    assert rejection_reason(ok, cfg) is None


def test_non_torrent_streams_are_ignored():
    assert normalize_stream({"name": "Debrid", "url": "https://example.com/x.mkv"}) is None


def test_merge_keeps_best_seeded_duplicate():
    a = normalize_stream(_stream())
    b = a.__class__(**{**a.to_dict(), "seeders": 50, "addon": "Other"})
    assert merge([a, b]) == [b]


def test_size_and_language_parsing():
    assert parse_size("💾 700 MB") == 700 * 1024**2
    assert parse_size("💾 1,024.5 MB") == int(1024.5 * 1024**2)
    assert parse_size("no size") == 0
    assert parse_languages("🇬🇧 / 🇮🇹") == ("en", "it")
    assert parse_languages("🇮🇱") == ("he",)
    assert parse_languages("🌐") == ("multi",)
