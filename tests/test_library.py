from __future__ import annotations

import json
from dataclasses import asdict, fields, replace

import pytest

from livrosound import library, paths
from livrosound.library import BookRecord, Library, Settings, audio_signature
from livrosound.speech import SpeechOptions


def record(book_id: str, last_opened: str = "", **kw) -> BookRecord:
    return BookRecord(book_id, f"Title {book_id}", "Author", f"/books/{book_id}.epub", "epub", last_opened=last_opened, **kw)


# ---------- Settings ----------


def test_settings_defaults_when_no_file():
    assert not paths.settings_path().exists()
    assert Settings.load() == Settings()


def test_settings_round_trip(isolated_cache):
    s = Settings(theme="dark", reflow_font_size=24, page_zoom=1.5, highlight=False, playback_rate=1.25,
                 paragraph_pause=1.2, read_headings=False, read_footnotes=True, validate_audio=True,
                 voice="Ana")
    s.save()
    assert paths.settings_path() == isolated_cache / "settings.json"
    assert Settings.load() == s


def test_settings_save_leaves_no_temp_files(isolated_cache):
    Settings().save()
    Settings(theme="calm").save()
    assert sorted(p.name for p in isolated_cache.iterdir()) == ["settings.json"]


def test_settings_paths_resolved_at_call_time(tmp_path, monkeypatch):
    other = tmp_path / "elsewhere"
    monkeypatch.setenv("LIVROSOUND_CACHE", str(other))
    Settings(voice="Matthew").save()
    assert (other / "settings.json").is_file()
    assert Settings.load().voice == "Matthew"


def test_settings_tolerant_read():
    paths.settings_path().parent.mkdir(parents=True)
    paths.settings_path().write_text(
        json.dumps(
            {
                "theme": "quiet",
                "from_a_newer_version": [1, 2],  # unknown: ignored
                "reflow_font_size": "big",  # wrong type: default
                "page_zoom": 2,  # int for a float: fine
                "highlight": 0,  # int for a bool: default
                "playback_rate": False,  # bool for a float: default
                "paragraph_pause": None,  # null: default
                "voice": 7,  # number for a str: default
                # engine, read_headings… missing: defaults
            }
        )
    )
    s = Settings.load()
    assert s == Settings(theme="quiet", page_zoom=2.0)
    assert isinstance(s.page_zoom, float)


def test_settings_whole_float_for_an_int():
    paths.settings_path().parent.mkdir(parents=True)
    paths.settings_path().write_text(json.dumps({"reflow_font_size": 22.0, "page_zoom": 1.25}))
    s = Settings.load()
    assert s.reflow_font_size == 22 and isinstance(s.reflow_font_size, int)
    paths.settings_path().write_text(json.dumps({"reflow_font_size": 22.5}))
    assert Settings.load().reflow_font_size == 20


@pytest.mark.parametrize("content", ["", "{not json", "[1, 2, 3]", "null", '"text"', "\xff\xfe garbage"])
def test_settings_unreadable_file_gives_defaults(content):
    paths.settings_path().parent.mkdir(parents=True)
    paths.settings_path().write_text(content, encoding="latin-1")
    assert Settings.load() == Settings()


def test_settings_non_finite_float_gives_default():
    paths.settings_path().parent.mkdir(parents=True)
    paths.settings_path().write_text('{"playback_rate": NaN, "page_zoom": Infinity}')
    s = Settings.load()
    assert s.playback_rate == 1.0 and s.page_zoom == 1.0


def test_speech_options_from_settings():
    assert Settings().speech_options() == SpeechOptions(paragraph_pause_ms=600, read_headings=True, read_footnotes=False)
    s = Settings(paragraph_pause=1.25, read_headings=False, read_footnotes=True)
    assert s.speech_options() == SpeechOptions(paragraph_pause_ms=1250, read_headings=False, read_footnotes=True)
    assert Settings(paragraph_pause=0.7).speech_options().paragraph_pause_ms == 700  # 0.7 * 1000 = 700.0000000000001
    assert Settings(paragraph_pause=0.2).speech_options().paragraph_pause_ms == 200


# ---------- Library ----------


def test_empty_library():
    lib = Library.load()
    assert lib.recent() == []
    assert lib.get("nope") is None


def test_library_round_trip(isolated_cache):
    lib = Library()
    a = record("aaa", "2026-10-20T10:00:00", layout="original", engine="Pocket-TTS", voice="Ana",
               audio_signature="sig", position_block=42, position_ms=1234, generated_fraction=0.37,
               generation_start=40)
    b = record("bbb", "2026-10-21T09:00:00")
    lib.upsert(a)
    lib.upsert(b)
    lib.save()
    assert paths.library_path() == isolated_cache / "library.json"
    loaded = Library.load()
    assert loaded.get("aaa") == a
    assert loaded.get("bbb") == b
    assert sorted(p.name for p in isolated_cache.iterdir()) == ["library.json"]


def test_changes_stay_in_memory_until_save():
    lib = Library()
    lib.upsert(record("aaa"))
    assert Library.load().get("aaa") is None
    lib.save()
    assert Library.load().get("aaa") is not None


def test_upsert_replaces_by_id():
    lib = Library()
    lib.upsert(record("aaa", position_block=1))
    lib.upsert(record("aaa", position_block=9))
    assert lib.get("aaa").position_block == 9
    assert len(lib.recent()) == 1


def test_recent_newest_first_never_opened_last():
    lib = Library([
        record("old", "2026-01-01T08:00:00"),
        record("never"),
        record("new", "2026-10-23T12:30:00"),
        record("mid", "2026-05-05T05:05:05"),
    ])
    assert [r.book_id for r in lib.recent()] == ["new", "mid", "old", "never"]


def test_library_tolerant_read():
    paths.library_path().parent.mkdir(parents=True)
    good = asdict(record("aaa", "2026-10-01T00:00:00"))
    paths.library_path().write_text(
        json.dumps(
            {
                "books": [
                    {**good, "future_field": {"x": 1}},  # unknown key: ignored
                    {"book_id": "bbb", "title": "Only a title", "position_block": "3"},  # missing / wrong type
                    {"title": "No id"},  # no id: dropped
                    {"book_id": ""},  # empty id: dropped
                    "not a record",  # dropped
                ],
                "future_top_level": True,
            }
        )
    )
    lib = Library.load()
    assert lib.get("aaa") == record("aaa", "2026-10-01T00:00:00")
    assert lib.get("bbb") == BookRecord("bbb", "Only a title", "", "", "")
    assert {r.book_id for r in lib.recent()} == {"aaa", "bbb"}


@pytest.mark.parametrize("content", ["", "{oops", "[]", '{"books": {"a": 1}}', '{"books": null}'])
def test_unreadable_library_is_empty(content):
    paths.library_path().parent.mkdir(parents=True)
    paths.library_path().write_text(content)
    assert Library.load().recent() == []


def test_book_record_defaults_match_contract():
    r = BookRecord("id", "t", "a", "/p", "pdf")
    assert (r.layout, r.engine, r.voice, r.audio_signature) == ("livrosound", "", "", "")
    assert (r.position_block, r.position_ms, r.last_opened) == (0, 0, "")
    assert (r.generated_fraction, r.generation_start) == (0.0, -1)
    assert not hasattr(r, "ocr")  # scanned books (OCR) aren't supported


def test_a_library_from_the_ocr_days_still_loads():
    """library.json written while scanned books were offered: the OCR keys are ignored."""
    paths.library_path().parent.mkdir(parents=True)
    old = {**asdict(record("aaa")), "scanned": True, "ocr": "always", "ocr_quality": "best"}
    paths.library_path().write_text(json.dumps({"books": [old]}))
    assert Library.load().get("aaa") == record("aaa")


# ---------- remove ----------


def make_book_dir(book_id: str):
    folder = paths.book_dir(book_id)
    (folder / "ocr").mkdir(parents=True)
    (folder / "book.json").write_text("{}")
    (folder / "audio.sqlite").write_bytes(b"\x00" * 10)
    return folder


def test_remove_deletes_the_book_folder():
    lib = Library([record("aaa"), record("bbb")])
    folder = make_book_dir("aaa")
    other = make_book_dir("bbb")
    lib.remove("aaa")
    assert lib.get("aaa") is None
    assert not folder.exists()
    assert other.is_dir()
    assert lib.get("bbb") is not None


def test_remove_can_keep_files():
    lib = Library([record("aaa")])
    folder = make_book_dir("aaa")
    lib.remove("aaa", delete_files=False)
    assert lib.get("aaa") is None
    assert folder.is_dir()


def test_remove_without_a_folder_or_record_is_harmless():
    lib = Library([record("aaa")])
    lib.remove("aaa")
    lib.remove("never-added")
    assert lib.recent() == []


def test_remove_deletes_a_cached_folder_without_a_record():
    folder = make_book_dir("orphan")
    Library().remove("orphan")
    assert not folder.exists()


@pytest.mark.parametrize("book_id", ["", ".", "..", "../outside", "../../", "nested/inner", "/tmp"])
def test_remove_never_deletes_outside_books_dir(book_id, isolated_cache):
    # Things an odd id could point at: the books folder, the cache, a sibling, a nested folder.
    make_book_dir("keep")
    (isolated_cache / "outside").mkdir(parents=True)
    (isolated_cache / "outside" / "precious.txt").write_text("x")
    (isolated_cache / "settings.json").write_text("{}")
    (paths.books_dir() / "nested" / "inner").mkdir(parents=True)
    lib = Library([record(book_id)])
    lib.remove(book_id)
    assert lib.get(book_id) is None
    assert (paths.book_dir("keep") / "book.json").is_file()
    assert (isolated_cache / "outside" / "precious.txt").is_file()
    assert (isolated_cache / "settings.json").is_file()
    assert (paths.books_dir() / "nested" / "inner").is_dir()


def test_remove_never_follows_a_symlinked_book_folder(isolated_cache):
    target = make_book_dir("real")
    outside = isolated_cache / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("x")
    (paths.books_dir() / "link-out").symlink_to(outside, target_is_directory=True)
    (paths.books_dir() / "link-in").symlink_to(target, target_is_directory=True)
    lib = Library()
    lib.remove("link-out")
    lib.remove("link-in")
    assert (outside / "precious.txt").is_file()
    assert (target / "book.json").is_file()


# ---------- audio_signature ----------


def test_audio_signature_is_stable():
    opts = SpeechOptions()
    assert audio_signature("Pocket-TTS", "Aaron", opts) == audio_signature("Pocket-TTS", "Aaron", SpeechOptions())
    assert len(audio_signature("Pocket-TTS", "Aaron", opts)) == 40


def test_audio_signature_of_existing_audio_never_changes():
    """Audio made earlier stays valid: the signature of the default narration is pinned."""
    assert audio_signature("Pocket-TTS", "Aaron", SpeechOptions()) == "b4641a6a9bf9959e0cffdf11d975adc59389c4c7"


def test_settings_from_the_two_engine_days_still_load():
    """settings.json written while OmniVoice was offered: its engine key is ignored."""
    paths.settings_path().parent.mkdir(parents=True)
    paths.settings_path().write_text(json.dumps({"engine": "OmniVoice", "voice": "Ana"}))
    assert Settings.load() == Settings(voice="Ana")


def test_audio_signature_changes_with_engine_and_voice():
    opts = SpeechOptions()
    base = audio_signature("Pocket-TTS", "Aaron", opts)
    assert audio_signature("Another-TTS", "Aaron", opts) != base
    assert audio_signature("Pocket-TTS", "Ana", opts) != base


def test_audio_signature_covers_every_speech_option():
    base_opts = SpeechOptions()
    base = audio_signature("Pocket-TTS", "Aaron", base_opts)
    for f in fields(SpeechOptions):
        value = getattr(base_opts, f.name)
        changed = (not value) if isinstance(value, bool) else value + 1
        assert audio_signature("Pocket-TTS", "Aaron", replace(base_opts, **{f.name: changed})) != base, f.name


def test_audio_signature_includes_speech_version(monkeypatch):
    base = audio_signature("Pocket-TTS", "Aaron", SpeechOptions())
    monkeypatch.setattr(library, "SPEECH_VERSION", library.SPEECH_VERSION + 1)
    assert audio_signature("Pocket-TTS", "Aaron", SpeechOptions()) != base


# ---------- added for the main window: speech options and reading progress ----------


def test_book_record_new_fields_default_and_round_trip():
    r = BookRecord("id", "t", "a", "/p", "pdf")
    assert r.speech_options == {} and r.read_fraction == -1.0
    assert r.audio_options() is None
    options = SpeechOptions(paragraph_pause_ms=900, read_headings=False, read_footnotes=True)
    lib = Library([record("aaa", speech_options=asdict(options), read_fraction=0.25)])
    lib.save()
    loaded = Library.load().get("aaa")
    assert loaded.audio_options() == options
    assert loaded.read_fraction == 0.25
    assert BookRecord("x", "t", "a", "/p", "pdf").speech_options is not r.speech_options  # no shared default


def test_book_record_new_fields_tolerate_old_and_odd_files():
    paths.library_path().parent.mkdir(parents=True)
    old = asdict(record("old"))
    del old["speech_options"], old["read_fraction"]  # a library.json from before these fields
    odd = {**asdict(record("odd")), "speech_options": "fast", "read_fraction": "half"}
    partial = {**asdict(record("partial")), "speech_options": {"read_headings": False, "nonsense": 1}}
    paths.library_path().write_text(json.dumps({"books": [old, odd, partial]}))
    lib = Library.load()
    assert lib.get("old") == record("old")
    assert (lib.get("odd").speech_options, lib.get("odd").read_fraction) == ({}, -1.0)
    assert lib.get("partial").audio_options() == SpeechOptions(read_headings=False)
