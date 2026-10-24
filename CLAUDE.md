# LivroSound — project guide for Claude Code

A **local, offline** macOS desktop app (PySide6/Qt) that reads books aloud: a PDF with
digital text, an EPUB (also an unzipped `.epub` folder), a DOCX or a TXT → one narrator
voice cloned from the user's recordings, made by **Pocket-TTS** on the CPU → listen with
live sentence highlighting, auto-scroll and click-to-seek / click-to-generate; export
one MP3 with chapters. Everything runs on-device.

It's the book-reading sibling of **Script Radio** (`~/screenplay-reader-private`,
package `screenplay_reader`), the reference for look, feel and code style. That repo
is **read-only** from here: copy and adapt, never modify it.

`docs/DESIGN.md` is the binding contract: every module's public API, the
**User flows (binding)** and the **As built** notes. Update it when an API changes.

## Run / dev

- Run the app: `uv run main.py`
- Set up / repair the venv: `uv sync` (Python 3.12, uv, `.venv/`). The only extra is
  `--extra validate` (the Whisper check). Don't `uv add` / `pip install` without asking.
- Tests: `uv run pytest` — 883 fast tests, no models (`addopts = -m 'not slow'`).
  Real-model / real-book tests (25): `uv run pytest -m slow tests/<file> -q`.
- `tests/conftest.py` gives every test its own `LIVROSOUND_CACHE` (never the user's
  library) and offscreen Qt (`qapp` fixture). Fakes: `tests/fakes.py` `FakeEngine`;
  `tests/fake_output.py` `FakeOutputs`, `InlineExecutor`, `fake_stretch` (player without a
  sound card). `tests/test_main_window.py` drives every flow with scripted dialog answers.
- Smoke test: `uv run python scripts/smoke_test.py <book> [--engine fake|pocket]
  [--blocks N] [--voice NAME] [--out FILE]` — import, print title/author/TOC/skipped
  matter/first spoken text, generate N blocks from the start, check cues, export an MP3
  (+ chapters via ffprobe), timings. Uses a temp cache; MP3 → `cache/smoke_test.mp3`.
  `tests/test_scripts.py` runs it with the fake engine.
- Voice previews up front: `uv run python scripts/make_voice_previews.py`.
- **Don't run full-book generation to test** (hours of CPU): the user reports the bugs
  they hear. Use the fake engine, `--blocks N`, or a few blocks of Pocket-TTS.
- Offscreen screenshots: `QT_QPA_PLATFORM=offscreen QT_FONT_DPI=72` (add
  `QT_SCALE_FACTOR=2` for a Retina look).
- No network at runtime: Pocket-TTS (and Whisper) load from `~/.cache/huggingface`.
- Sample books (read only): `~/Library/Mobile Documents/com~apple~CloudDocs/Ebooks/`.
  `Artful_Sentences.pdf` there is a scan: it is refused now.

## Architecture (where things live)

Everything is `(block index, ms into that block's audio)` for positions and char
offsets into `Block.text` for highlight/click/cue spans.

Core (`livrosound/`, no Qt):
- `book.py` — `Book` = flat `blocks` in reading order + `toc` (+ PDF `pages`, `text_box`;
  `scanned` = an EPUB whose text came from a scan, e.g. Internet Archive). `Block.kind`
  (heading/paragraph/quote/verse/note/image/separator), `styles`, `words` (PDF word boxes
  + char spans), `role` (`SKIPPED_ROLES` = front/back matter shown but never read;
  `TITLE_ROLE` = the one "<title>, by <author>." block), `link` (a printed-contents line →
  target block). `PARSER_VERSION` = 2 (bump → cached books re-parsed, their audio dropped).
  `file_book_id` = sha1 of the file's bytes.
- `paths.py` — `cache_dir()` (`LIVROSOUND_CACHE` or `cache/`, read on every call),
  `book_dir(id)`, `settings_path()`, `library_path()`, `voice_previews_dir()`.
- `ingest/` — `load_book(path, *, progress, cancelled)`: the only entry point; runs off the
  UI thread; cache = `book_dir/book.json` + `original.html` (valid while `PARSER_VERSION`
  matches). A re-parse deletes the old audio only after it succeeds. `book_id_for(path)`
  (files and unzipped EPUB folders). `BookImportError` text is shown to the user; TOC
  entries on skipped blocks are dropped for every format. Parsers: `pdf.py` (PyMuPDF
  rawdict words → rows → blocks from geometry; drop caps, notes, running heads, roles,
  contents links; TOC = outline > printed contents > headings; `is_scanned(doc)` → refused),
  `epub.py` (zipfile + lxml; nav/NCX; epub:type, landmarks, `<guide>`, file names → roles;
  Internet Archive hocr-to-epub pages cleaned and cut into paragraph-sized blocks),
  `docx.py`, `txt.py` (Gutenberg wrapper, hard-wrap detection). Shared: `heads.py`
  (running heads + page numbers), `matter.py` (front/back matter roles, title + subtitle,
  `clean_title` strips series tags, contents-page links, `toc_from_headings`), `textmap.py`
  (char maps, so `original.html` loses exactly what the blocks lose).
- `textutil.py` — `HEADING_RE`, `PAGE_NUMBER_RE`, `is_word`/`Vocab` (`/usr/share/dict` +
  the book's own words), `join_mode` (line-end hyphens),
  `fix_spaced_letters`, `title_case`, `looks_like_boilerplate`.
- `sentences.py` — `split_sentences` (Script Radio's, plus book abbreviations).
- `speech.py` — `is_read`, `lead_in_ms`, `block_chunks` (one chunk per sentence / clause /
  verse line, ≤ `MAX_CHUNK_WORDS` 30 and `MAX_SPOKEN_CHARS` 140), `speakable` (numbers,
  romans, abbreviations, ASCII quotes, accents flattened). `SPEECH_VERSION` is in the
  audio signature (bump → stored audio is stale).
- `dsp.py` — `SAMPLE_RATE` 24000 (16-bit mono everywhere after synthesis), `resample`,
  `trim_speech` (drops Pocket-TTS's end-of-clip tick), `declick` (8 ms), `time_stretch`
  (ffmpeg atempo), `ffmpeg_binary()`. `loudness.py` — BS.1770, every block to −19 LUFS.
- `voices.py` — `catalog()` from `assets/voices/NAME.wav` + `NAME.wav.txt` + `voices.json`
  (gender/age); `grouped()` → Women / Men / Other; `DEFAULT_VOICE = "Aaron"`.
- `engines/` — `TTSEngine` (`base.py`, kept so another local engine can be added);
  `pocket.py` `PocketTTSEngine` (≈4.3× real time after a ≈7 s load; gated cloning weights
  → explicit error). `load_engines()` → `[PocketTTSEngine]` (a shared singleton: previews
  and generation share one model); `engine_named()`.
- `voice_preview.py` — `PREVIEW_LINE`, `preview_path` (fingerprint of the sample),
  `make_preview`. `validate.py` — Whisper `Transcriber` (mlx-whisper / faster-whisper,
  its own `local_model`), `MAX_ATTEMPTS` 3.
- `audio_store.py` — one SQLite file per book (`book_dir/audio.sqlite`, WAL, a connection
  per thread): one row per block (PCM + `Cue`s), `signature`, `reset`. `generated()` /
  `has()` / `durations()` are in-memory.
- `timeline.py` — `Timeline(order, durations)`: ±10 s `skip` clamped to the contiguous
  ready audio, `next_block`/`prev_block` (paragraphs), `start_ms_of`.
- `generation.py` — `Generator.run(start)` renders missing blocks from `start` to the end,
  then wraps; `fraction_done`, `stream_ready` (`STREAM_THRESHOLD` 0.05), raises
  `GenerationCancelled` (finished blocks kept). Callbacks run on the worker thread.
- `export.py` — `export_mp3`: blocks piped to ffmpeg (libmp3lame 96k mono), ID3 tags, one
  CHAP per TOC entry that is read (the first starts at 0:00), `.partial` then rename.
- `library.py` — `Settings` (`settings.json`; no engine field), `BookRecord` / `Library`
  (`library.json`; `engine` kept for the audio signature, `speech_options`,
  `read_fraction`), `audio_signature(engine, voice, options)`. One `Library` per window;
  `save()` after changes. Old JSON keys are ignored.

UI (`livrosound/ui/`), entry point `main.py` (root: `quiet_libraries()`, app name,
Fusion, `ui_font(13)`, `ensure_reading_fonts()`, Dock icon, `MainWindow()`):
- `main_window.py` — `MainWindow(engine=None, output_factory=None)`: every flow of
  DESIGN.md. `GenerationWorker` (one QThread at a time), `Task` (imports, opening,
  export off the UI thread), `SpeedCombo`, `PLAYBACK_SPEEDS` 0.5–2.0, `WAIT_AHEAD_CHARS`.
  Positions go through `_go_to(block, char)`; Play is gated by `_gate` (5% rule).
- `theme.py` — four themes (Original / Quiet / Calm / Dark) with color-wheel highlighter,
  frame and title colors (`tests/test_theme.py` enforces contrast), `ACCENT` fern
  `#4F7942`, `CANCEL_RED`, `PROGRESS_BLUES`, `EXPORT_BLUE`, `ui_font`, `reading_font`,
  `stylesheet()`, `tooltip_stylesheet()`.
- `widgets.py` — `Popover`, `ThemesPopover` ("Aa": size, light/dark, themes, Layout,
  Highlight), `ContentsPopover` (fern dot = audio ready, `set_ready`), `VoicePicker` /
  `VoiceMenu`, `VoicePreviews`, `PlayerCapsule` (−10 sec, ⏮, ▶, ⏭, +10 sec),
  `GradientProgressBar`, `ToggleSwitch`, `Alert`, `confirm()`, `inform()`.
- `landing.py` — `LivroSoundLanding`: Upload New Book / Resume a Previous Book.
- `dialogs.py` — `ImportProgressDialog`, `LayoutDialog`, `ChooseVoiceDialog`
  (`ask(parent, engine, voice, …) -> voice | None`), `CustomizeAudioDialog`,
  `LibraryDialog` (`on_remove`); each has `ask(...)`.
- `reader_view.py` — the `ReaderView` mixin both views share (clicked, user_scrolled,
  auto-follow helpers, link cursor); `ensure_reading_fonts()`.
- `reflow_view.py` — `ReflowView` (QTextEdit): LivroSound layout of any book, and the
  original layout of EPUB/DOCX/TXT (`original_html`, blocks found by `ls-b{i}` anchors).
- `page_view.py` — `PageView`: a digital PDF's own pages, continuous scroll, cropped to
  `Book.text_box`, rendered by a worker thread (destination first after a long jump),
  tinted to the theme; highlight bands straight from word boxes.
- `player.py` — `BookPlayer`: streams block after block from the store through a
  `QAudioSink` (24 kHz mono), speed via `dsp.time_stretch`, waits for blocks generation
  is about to make (`set_waiting_allowed`), else emits `needs_audio`.

Assets: `assets/voices/` (17 voices + `voices.json` + README), `assets/icons/`
(`livrosound.svg` book art, `livrosound-app.svg` → `livrosound-1024.png` Dock icon),
`assets/sounds/ready.wav` (the ready ding).

`cache/` (gitignored): `settings.json`, `library.json`, `voice_previews/`,
`books/<book_id>/` (`book.json`, `original.html`, `res/`, `audio.sqlite`),
`smoke_test.mp3`.

## The user's rules (binding)

- **Formats:** "ship the app with just PDF (already in digital text), EPUB, DOCX and TXT".
  Scanned PDFs are refused ("Scanned books aren't supported yet.").
- **Engine/voices:** "just ship with pocket-tts" (OmniVoice was too slow). Only the user's
  cloned voices, renamed to first names (Aaron = former Default_Narrator_Male, Ana =
  former Default_Narrator_Female, …; old names in `voices.json` "source"). Never
  Pocket-TTS's built-in voices.
- **No ticks** between chunks: `dsp.trim_speech` + `declick` before every join, leveling
  per block (same fix as yt-dub).
- **Streaming** once 5% of the book is generated from the position (Script Radio: 10%).
- **Transport:** ±10 sec buttons, ⏮/⏭ = one paragraph; highlight and auto-scroll follow.
- **Before any audio:** "Generate audio from the beginning?" — No → "Choose where to
  start" (click text or a Contents chapter). Clicking ungenerated text: "Audio not ready
  yet. Start generating audio from this point?"
- **Contents** entries are links; lines of the book's printed contents page link to
  their chapters (`Block.link`).
- **Running heads and page numbers are never read or highlighted** (`10 ARTFUL
  SENTENCES`, `SHORT SENTENCES 19`), but a chapter opener (`CHAPTER ONE` +
  `Short Sentences`) is a heading and is read.
- **Front/back matter is never read** (cover, blurb, title copies, scanner notices,
  copyright, acknowledgements, printed TOC, index); the title is announced once.
  Dedications, epigraphs, prefaces are read.
- **Errors in the source file are not ours:** "if the mistake was with the PDF, meaning
  the original file was the problem, then don't worry about that."
- **Layouts:** Original (PDF pages like Preview's continuous scroll, zoomed in, with a
  highlight band; EPUB/DOCX/TXT with their own HTML) or LivroSound (Apple-Books-like,
  Iowan / New York).
- **Landing:** Upload New Book / Resume a Previous Book; resume restores the position.
- **Export MP3** with chapters at 100%.

## Conventions

- **Code style:** as Script Radio — a module docstring saying *why* the module exists and
  its non-obvious rules, short docstrings, comments only for non-obvious reasons,
  dataclasses, `from __future__ import annotations`, type hints, no dead code. UI text
  plain and friendly (Apple-like).
- **Fonts:** UI = SF Pro via `theme.ui_font(size, weight)`; the book = `theme.reading_font`
  (Iowan Old Style → New York → Charter → Georgia). Never put a `font-family` in a Qt
  stylesheet. `reader_view.ensure_reading_fonts()` right after creating the app.
- **Themes:** `theme.set_current(t)` *before* `setStyleSheet(theme.stylesheet(t))`, then
  `view.set_theme(t)`. Custom-painted widgets read `theme.current()`. Keep stylesheet
  `border-radius` ≤ half the widget's height (else Qt draws square corners).
- **Colors:** fern `ACCENT` for Play, Generate Audio, Resume Generating, ▶ previews and
  switches; Cancel Generation red; generation progress = blue gradient; Export MP3 blue.
- **Alerts:** `confirm(parent, title, text, …)` — No left / Yes right;
  `destructive=True` (red Yes) for Cancel Generation, Replace audio, Remove from Library.
  Always pass the main window as a dialog's parent (stylesheet + shared `VoicePreviews`).
- **Main window:** ask questions from `QTimer.singleShot(0, self, …)`, never inside a
  view's or the player's own handler; after any modal dialog check the same book is still
  open (`_still(book)`); work that switches books waits for no modal dialog
  (`_when_no_dialog`); a closed book's worker retires and its store closes when the thread
  ends (`_when_released` before reopening/re-importing it).
- **Threads:** ingest, generation, export, preview rendering, page rendering and time
  stretching run off the UI thread; their callbacks fire on the worker → emit Qt
  signals. `BookPlayer` lives on the UI thread. One SQLite connection per thread; stop
  worker threads (or `set_book`/`pause` the player) before `AudioStore.close()`.
- **ffmpeg:** always `dsp.ffmpeg_binary()` (a Finder-launched app has no Homebrew PATH).
- **Big books:** `speech.is_read` ≈35 µs/block and `block_chunks` per block — never for
  the whole book on the UI thread; `Generator.order()`/`weights()` are cached copies.
  Anna Karenina (7,941 blocks): LivroSound layout ≈0.3 s, original ≈2 s.
- Git: don't commit unless the user asks.

## Gotchas

- `QTest.qWait` holds the GIL and starves worker threads: wait with a `QEventLoop` +
  `QTimer.singleShot` (`_wait()` in `tests/test_player.py`).
- Qt drops an empty `<a name>` between block elements: `original_html` anchors wrap the
  block's first character. Qt positions count UTF-16 units.
- A re-parse (new `PARSER_VERSION`) deletes the book's audio once it succeeds; then
  `AudioStore.signature == ""` → reset that record's `generated_fraction` /
  `audio_signature`. Close the book's `AudioStore` before re-importing.
- PageView never draws PDF annotations (a reader's own Preview highlight would look like
  a stuck reading highlight).
- Pocket-TTS's cloning weights are gated on Hugging Face; without them pocket_tts silently
  loads a non-cloning model — `PocketTTSEngine` raises instead. Intel Macs have no engine
  (torch ≥ 2.5 has no Intel-Mac wheels): Choose a Voice says why and Continue stays off.
- `BookPlayer.set_waiting_allowed(fn)` is polled every 20 ms (keep it cheap); `pause()`
  before cancelling generation, or the player asks `needs_audio`.
- First `QAudioSink` takes 120–250 ms: the window warms `QMediaDevices.defaultAudioOutput()`.
- Quit hides the window while a thread (a chunk, an import, an export, a preview) is still
  stopping, and quits once all have (`quitOnLastWindowClosed` is off meanwhile).
- If git complains about `.git/index.lock`: `rm -f .git/index.lock`. Don't commit large
  binary art; voices are the exception (they ship with the code).

## Current state & ideas (2026-10-23)

- Feature-complete for PDF (digital), EPUB, DOCX, TXT with Pocket-TTS; 883 fast tests
  green. Nothing is committed yet (`master` has no commits).
- **Scanned books and OmniVoice were removed** (user decisions). The OCR-capable code
  (`ingest/ocr.py` with PaddleOCR PP-OCRv6, `ImportDialog`, `ImportOptions`, the
  `ocr`/`omnivoice` extras, `engines/omnivoice.py`) is saved in
  `~/livrosound-scanned-books-snapshot-2026-10-23.tar.gz` for a future return.
- Ideas: Qwen3-TTS / Chatterbox as a better (MLX) engine behind `TTSEngine`, as for
  Script Radio.
