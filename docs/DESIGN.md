# LivroSound: design and module contract

LivroSound is a **local, offline** macOS desktop app (PySide6/Qt) that reads books
aloud. The user uploads a PDF with digital text, an EPUB, a DOCX or a TXT, picks a
layout and a narrator voice, and listens with live sentence highlighting, auto-scroll
and click-to-seek. Audio is generated on-device with **Pocket-TTS**, can stream once 5%
of the book is ready, and exports to MP3 (with chapter markers) at 100%.

**Scope cut (2026-10-23, user decisions).** Scanned books: "OCR is too imperfect … hold
off on the scanned books capability for now and let's ship the app with just PDF
(already in digital text), EPUB, DOCX and TXT." OmniVoice: "just ship with pocket-tts
because omnivoice takes too long." A scanned PDF is refused at import ("Scanned books
aren't supported yet."). The APIs that went with them are marked **Removed** below;
the OCR- and OmniVoice-capable code is kept in
`~/livrosound-scanned-books-snapshot-2026-10-23.tar.gz` for a future return. Mistakes in
a user's file are not LivroSound's to fix: "if the mistake was with the PDF, meaning the
original file was the problem, then don't worry about that."

It is the book-reading sibling of **Script Radio** (`~/screenplay-reader-private`,
package `screenplay_reader`). Script Radio is the reference for look, feel, code style
and many building blocks; **never modify anything in `~/screenplay-reader-private`** —
copy and adapt into `~/livrosound` instead.

- Project root: `~/livrosound` (uv, Python 3.12, venv `.venv/`; the one extra,
  `validate` (Whisper), is installed). Run things with `uv run ...` from the project root.
- Run the app: `uv run main.py`. Tests: `uv run pytest` (fast tests must not load real
  models; mark real-model tests `@pytest.mark.slow`).
- Tests must set `LIVROSOUND_CACHE` to a temp dir (see `tests/conftest.py`) so they never
  touch the user's library. Never write into the user's iCloud folder; reading the
  sample books there is fine.
- No network access is needed or allowed at runtime (Pocket-TTS and Whisper load from
  `~/.cache/huggingface`).
- Don't test by generating a whole book (hours of CPU): the user reports the bugs they
  hear. Use the fake engine, `scripts/smoke_test.py --blocks N`, or a few real blocks.

## Code style

Match Script Radio: a module docstring that explains *why* the module exists and its
non-obvious rules, short docstrings on public functions, comments only where the reason
isn't obvious, dataclasses for data, `from __future__ import annotations`, type hints,
no dead code. UI text is plain and friendly (Apple-like). Fonts: UI = SF Pro via
`theme.ui_font()`; never put a `font-family` in a Qt stylesheet.

## Constants (shared)

| Name | Value | Where |
| --- | --- | --- |
| `SAMPLE_RATE` | 24000 (16-bit mono PCM everywhere after synthesis) | `dsp.py` |
| `STREAM_THRESHOLD` | 0.05 (Play enables once the unbroken audio from the position holds 5% of the book) | `generation.py` |
| `DEFAULT_VOICE` | `"Aaron"` | `voices.py` |
| `PREVIEW_LINE` | `"It was the best of times, it was the worst of times."` | `voice_preview.py` |
| `TARGET_LUFS` | -19.0 | `loudness.py` |
| `DECLICK_MS` | 8 | `dsp.py` |
| `MAX_CHUNK_WORDS` | 30 (Pocket-TTS splits internally above ~50 tokens; we keep every join under our control) | `speech.py` |
| `PLAYBACK_SPEEDS` | 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0 | `ui/main_window.py` |

## Data model (already written: `livrosound/book.py`, `livrosound/paths.py`)

Read `livrosound/book.py`. Key points: a `Book` is a flat list of `Block`s in reading
order plus `toc: list[TocEntry]`. Every position in the app is `(block index, ms into
that block's audio)`; every highlight/click span is a char range of `Block.text`.
PDF blocks carry word boxes (`Block.words`) for the original-layout page view;
EPUB/DOCX/TXT books carry `original_html` with `<a name="ls-b{i}"></a>` before block i.
Cache locations come from `paths.py` (`book_dir(book_id)` etc.). `PARSER_VERSION` = 2.
`Book.scanned` means an EPUB whose text came from a scan (Internet Archive), cleaned of
page debris. **Removed:** `Book.text_source`.

## Module contract

Each module below lists its public API. Signatures are binding: other modules are
written against them in parallel. Private helpers are up to the implementer.

### `textutil.py` — text repair shared by the ingesters (port ideas from `~/scan-to-book/scan2book/textfix.py`)

```python
TERMINAL: tuple[str, ...]              # sentence-final characters incl. closing quotes
HEADING_RE: re.Pattern                 # "CHAPTER IV", "Chapter 12", "Book the First", "PREFACE", lone roman numeral…
PAGE_NUMBER_RE: re.Pattern             # "14", "- 14 -", "xii", "[p. 3]"
def is_word(word: str, vocab: "Vocab | None" = None) -> bool   # /usr/share/dict + inflections + the book's own vocab
class Vocab:                            # word counts of the document itself
    @classmethod
    def from_texts(cls, texts: Iterable[str]) -> "Vocab": ...
    def count(self, word: str) -> int: ...
def join_mode(prev: str, nxt: str, vocab: Vocab | None = None) -> str
    # How line `nxt` attaches to the paragraph so far `prev`:
    # "space" (normal), "drop-hyphen" ("independ-" + "ent" -> "independent"),
    # "join" (no space, keep chars: "friendship—" + "what", soft hyphen handled by caller).
def normalize_chars(text: str) -> str   # ligatures ﬁ→fi, nbsp→space, soft hyphen removed, odd spaces, zero-width chars
def fix_spaced_letters(line: str) -> str  # "A R T F U L" -> "ARTFUL"
# split_run_together (OCR word fusions: "isright" -> "is right") — **Removed** with scanned
# books; it is in the 2026-10-23 snapshot.
def roman_to_int(token: str) -> int | None
def title_case(text: str) -> str        # "SHORT SENTENCES" -> "Short Sentences", small words lower except first
def looks_like_boilerplate(line: str) -> bool  # "Digitized by the Internet Archive…", bare URLs, scanner notices
```

### `sentences.py` — port of Script Radio's `sentences.py` (same API), with a longer
abbreviation list for books (chap, vol, ed, eds, trans, fig, cf, viz, p, pp, ibid, op,
cit, ch, no, nos, esp, approx…). `split_sentences(text, start=0, end=None) -> list[tuple[int,int]]`.

### `speech.py` — what the narrator says for each block

```python
SPEECH_VERSION = 1
MAX_CHUNK_WORDS = 30

@dataclass
class SpeechOptions:
    paragraph_pause_ms: int = 600
    read_headings: bool = True
    read_footnotes: bool = False

@dataclass
class Chunk:
    block: int
    start: int            # char span of Block.text this chunk voices (= what is highlighted)
    end: int
    text: str             # what the engine is asked to say (normalized, ASCII punctuation, one line, non-empty)
    pause_after_ms: int   # silence after it; the block's last chunk carries the block's trailing pause

def is_read(block: Block, options: SpeechOptions) -> bool        # will this block get audio? (separators: True, silence only)
def lead_in_ms(block: Block, options: SpeechOptions) -> int       # silence before the block (level-1 heading 1200, other headings 800, separator 1200, else 0)
def block_chunks(book: Book, index: int, options: SpeechOptions) -> list[Chunk]
def speakable(text: str) -> str                                    # normalization of one passage (below)
```

Chunking: sentences (`split_sentences`); a sentence over `MAX_CHUNK_WORDS` words is
split at the clause punctuation (`, ; : — –`) nearest its middle, recursively, else at a
word boundary. Verse: one chunk per line. Pauses: after a sentence 350 ms, after a clause
split 150 ms, after a verse line 300 ms, after a heading 900 ms, after a block's last
chunk `paragraph_pause_ms`. Chunks whose speakable text is empty are dropped.

`speakable`: drop `sup` style runs (note marks) and bracketed/asterisk note refs
(`[1]`, `*`, `†`); typographic punctuation → ASCII (Pocket-TTS's tokenizer mangles curly
quotes — see Script Radio `speech.py`); em dash between words → ", "; number ranges
`1914–1918` → "1914 to 1918"; numbers → words with num2words (years 1100–2099 as years,
ordinals 1st/2nd, decimals, percents, currency $ £ €, thousands separators); Roman
numerals after Chapter/Book/Part/Volume/Section/Act → words; regnal "Henry VIII" →
"Henry the Eighth"; ALL-CAPS words of 4+ letters (or any all-caps line) → normal case,
but initialisms (no vowel, or ≤3 letters like TV, FBI, USA) stay; "e.g." → "for
example", "i.e." → "that is", "etc." → "et cetera", "&" → "and", "vs." → "versus",
"cf." → "compare", "pp." → "pages", "p. 12" → "page twelve"; URLs and e-mail addresses
dropped; whitespace collapsed (no newline ever reaches an engine).

### `dsp.py` + `loudness.py` — audio helpers (numpy, float32 in [-1, 1])

```python
SAMPLE_RATE = 24000
DECLICK_MS = 8
def resample(x: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray
def trim_speech(x: np.ndarray, sr: int) -> np.ndarray
    # Port of yt-dub's trim (~/Claude/Projects/code/my-scripts/yt_dub.py): trims leading and
    # trailing silence (30 ms pad) and DROPS a short cut-off sound (<= 120 ms) that runs into the
    # clip's last sample after a >= 30 ms gap — Pocket-TTS's end-of-clip "tick". Never drops a
    # short sound at the clip's start (those are real words: "A", "No").
def declick(x: np.ndarray, sr: int, ms: float = DECLICK_MS) -> np.ndarray   # linear ramp to zero at both edges
def silence(ms: int, sr: int = SAMPLE_RATE) -> np.ndarray
def to_pcm16(x: np.ndarray) -> bytes
def from_pcm16(data: bytes) -> np.ndarray
def ms_of(n_samples: int, sr: int = SAMPLE_RATE) -> int          # rounded
def samples_of(ms: float, sr: int = SAMPLE_RATE) -> int
def time_stretch(pcm: bytes, rate: float, sr: int = SAMPLE_RATE) -> bytes
    # Pitch-preserving speed change with ffmpeg atempo (chain filters past 2.0). rate == 1 → pcm unchanged.
# loudness.py: port Script Radio's BS.1770 code to numpy in/out:
def loudness(x: np.ndarray, sr: int) -> float
def level(x: np.ndarray, sr: int, target_lufs: float = TARGET_LUFS) -> np.ndarray   # gain + look-ahead peak limiter at -1 dBFS, max +40 dB
```

### `engines/` + `voices.py` + `voice_preview.py` + `validate.py`

```python
# voices.py — the cloned narrator voices in assets/voices/ (NAME.wav + NAME.wav.txt, metadata in voices.json)
DEFAULT_VOICE = "Aaron"
@dataclass
class Voice:
    name: str; gender: str; age: str; wav: Path; transcript: str
def catalog() -> list[Voice]                       # every NAME.wav in VOICES_DIR (metadata may be missing → gender/age "")
def voice_named(name: str) -> Voice | None
def grouped() -> list[tuple[str, list[Voice]]]    # [("Women", …), ("Men", …), ("Other", …) if any]; each sorted young→adult→mature, then name

# engines/base.py — kept as an interface so another local engine can be added later
class TTSEngine(ABC):
    name: str                 # "Pocket-TTS"
    install_hint: str         # "Install with: uv sync"
    def available(self) -> tuple[bool, str]: ...  # False on an Intel Mac (no torch>=2.5 wheels)
    def voices(self) -> list[str]: ...           # catalog names only — never Pocket-TTS's built-in voices
    def synthesize(self, voice: str, text: str) -> tuple[np.ndarray, int]: ...   # float32 mono [-1,1], sample rate
# engines/pocket.py: PocketTTSEngine (lazy model load, voice states cached, threading.Lock, clones from Voice.wav;
#   raises if the gated cloning weights can't load instead of reading with a non-cloning model)
# engines/omnivoice.py — Removed (2026-10-23), with the `omnivoice` extra.
# engines/__init__.py
def load_engines() -> list[TTSEngine]            # [PocketTTSEngine()] — one shared instance for the app
def engine_named(name: str) -> TTSEngine | None

# voice_preview.py — audition clips cached in paths.voice_previews_dir()
PREVIEW_LINE = "It was the best of times, it was the worst of times."
def preview_path(engine_name: str, voice: str) -> Path   # fingerprint includes the sample's size+mtime
def make_preview(engine: TTSEngine, voice: str) -> Path  # renders (trim, declick, level) a 24 kHz WAV if missing

# validate.py — port Script Radio's Whisper check (mlx-whisper on Apple Silicon), numpy based
def local_model(repo_id: str) -> str              # the cached Hugging Face snapshot folder (no network check)
MAX_ATTEMPTS = 3
@dataclass
class Verdict: ok: bool; error_rate: float; transcript: str
@dataclass
class Issue: block: int; text: str; transcript: str; error_rate: float
class Transcriber:
    def available(self) -> tuple[bool, str]: ...
    def check(self, x: np.ndarray, sr: int, text: str) -> Verdict: ...
```

### `audio_store.py` + `timeline.py` + `library.py`

```python
# audio_store.py — one SQLite file per book: book_dir(id)/"audio.sqlite" (WAL, a connection per thread)
@dataclass
class Cue:
    start_ms: int; end_ms: int; start_char: int; end_char: int   # a chunk inside a block's audio
@dataclass
class BlockAudio:
    block: int; duration_ms: int; cues: list[Cue]
class AudioStore:
    def __init__(self, path: Path): ...
    @property
    def signature(self) -> str: ...                    # "" for a new store
    def reset(self, signature: str) -> None: ...       # drop all audio, set the signature
    def put(self, block: int, pcm: bytes, cues: list[Cue]) -> None: ...   # 16-bit mono 24 kHz; replaces
    def has(self, block: int) -> bool: ...
    def generated(self) -> set[int]: ...               # a copy; kept in memory, thread-safe
    def pcm(self, block: int) -> bytes | None: ...
    def info(self, block: int) -> BlockAudio | None: ...
    def durations(self) -> dict[int, int]: ...
    def close(self) -> None: ...
def cue_at(cues: list[Cue], ms: int) -> Cue | None    # last cue starting at or before ms (None before the first)

# timeline.py — pure position arithmetic over the blocks that get audio
class Timeline:
    def __init__(self, order: list[int], durations: dict[int, int]): ...   # order: blocks with audio, reading order
    def set_duration(self, block: int, duration_ms: int) -> None: ...      # a block just got generated
    def ready(self, block: int) -> bool: ...
    def first_at_or_after(self, block_index: int) -> int | None: ...      # any block index → the first ordered block ≥ it
    def next_block(self, block: int) -> int | None: ...                    # next/previous in order, generated or not
    def prev_block(self, block: int) -> int | None: ...
    def skip(self, block: int, ms: int, delta_ms: int) -> tuple[int, int]: ...
        # ±10 s: walk across ready neighbours; stop (clamp) at the edge of the contiguous ready audio
    def start_ms_of(self, block: int) -> int | None: ...                   # offset from the first ordered block, if every block before it is ready (export, chapter times)

# library.py — settings and the library of books, JSON under cache_dir() (atomic writes, tolerant reads like Script Radio's settings.py)
@dataclass
class Settings:
    theme: str = "original"; reflow_font_size: int = 20; page_zoom: float = 1.0
    highlight: bool = True; playback_rate: float = 1.0
    paragraph_pause: float = 0.6; read_headings: bool = True; read_footnotes: bool = False
    validate_audio: bool = False; voice: str = "Aaron"     # Removed: engine (Pocket-TTS is the only one)
    @classmethod
    def load(cls) -> "Settings": ...
    def save(self) -> None: ...
    def speech_options(self) -> SpeechOptions: ...
@dataclass
class BookRecord:
    book_id: str; title: str; author: str; source_path: str; source_kind: str
    layout: str = "livrosound"          # "original" | "livrosound"
    engine: str = ""; voice: str = ""; audio_signature: str = ""
    position_block: int = 0; position_ms: int = 0
    last_opened: str = ""                # ISO timestamp
    generated_fraction: float = 0.0
    generation_start: int = -1           # block an unfinished generation run started from; -1 = none
    speech_options: dict = {}            # asdict(SpeechOptions) the book's audio is made with ({} = none yet)
    read_fraction: float = -1.0          # spoken characters before the saved position ("34% read"); -1 = unknown
    def audio_options(self) -> SpeechOptions | None: ...
    # engine: which engine made the audio (always "Pocket-TTS" now), kept for audio_signature.
    # Removed: scanned, ocr, ocr_quality. Old JSON keys are ignored on load.
class Library:
    @classmethod
    def load(cls) -> "Library": ...
    def save(self) -> None: ...
    def get(self, book_id: str) -> BookRecord | None: ...
    def upsert(self, record: BookRecord) -> None: ...
    def remove(self, book_id: str, *, delete_files: bool = True) -> None: ...   # deletes book_dir too
    def recent(self) -> list[BookRecord]: ...      # newest last_opened first
def audio_signature(engine: str, voice: str, options: SpeechOptions) -> str   # includes SPEECH_VERSION
```

### `generation.py` + `export.py`

```python
STREAM_THRESHOLD = 0.05
class GenerationCancelled(Exception): ...
class Generator:
    def __init__(self, book: Book, store: AudioStore, engine: TTSEngine, voice: str,
                 options: SpeechOptions, checker: Transcriber | None = None): ...
    def order(self) -> list[int]: ...              # blocks with audio (speech.is_read), reading order
    def weights(self) -> dict[int, int]: ...       # characters per ordered block (separators count 1)
    def queue_from(self, start_block: int) -> list[int]: ...
        # ordered blocks from the first ≥ start_block to the end, then wrapping from the start; generated ones skipped
    def render_block(self, index: int) -> tuple[bytes, list[Cue], list[Issue]]: ...
        # lead-in silence; per chunk: synthesize → resample to 24 kHz → trim_speech → declick → cue → pause;
        # whole block leveled (loudness.level), → pcm16. Separator: 1200 ms silence, no cues.
    def run(self, start_block: int, *, on_block: Callable[[int, int], None] = …,       # (block, duration_ms) after store.put
            on_status: Callable[[str], None] = …, cancelled: Callable[[], bool] = …) -> list[Issue]: ...
    def fraction_done(self) -> float: ...          # generated characters / all characters (0..1)
def stream_ready(order: list[int], generated: set[int], start_block: int,
                 weights: dict[int, int], threshold: float = STREAM_THRESHOLD) -> bool
    # True once the contiguous generated run starting at the first ordered block ≥ start_block covers
    # threshold of the whole book's weight, or reaches the end of the book.

# export.py
def export_mp3(book: Book, store: AudioStore, order: list[int], path: Path, *, bitrate: str = "96k",
               progress: Callable[[float], None] = …, cancelled: Callable[[], bool] = …) -> None
    # ffmpeg (libmp3lame, mono) fed PCM over stdin in order; ID3 title/artist/album and one CHAP per
    # TOC entry (start = first ordered block ≥ entry.block; entries on skipped front/back matter get
    # none; the first chapter starts at 0:00, so the title announcement belongs to it); writes
    # path.partial then renames; ValueError if any ordered block lacks audio.
```

### `ingest/` — every format → `Book`

```python
# ingest/__init__.py
SUPPORTED_SUFFIXES = (".pdf", ".epub", ".docx", ".txt")
class BookImportError(Exception): ...          # str(error) is the message the user sees
class ImportCancelled(Exception): ...
def load_book(path: str | Path, *, progress: Callable[[int, int, str], None] = …,
              cancelled: Callable[[], bool] = …) -> Book
    # Off the UI thread. Cache: book_dir(book_id_for(path)) / book.json + original.html, valid while
    # PARSER_VERSION matches (a moved file updates source_path). A re-parse deletes the book's audio
    # only after it has succeeded (a cancelled or failed one keeps the old book and audio).
    # Book.toc never lists a skipped block (entries on front/back matter are dropped, every format).
def book_id_for(path: str | Path) -> str       # file_book_id, or for an unzipped EPUB folder a fingerprint
                                               # of its files' names and bytes; OSError when unreadable
# Removed (2026-10-23): ImportOptions (scanned / ocr / ocr_quality / key()), load_book's options
# argument, import.json.

# ingest/pdf.py
def parse_pdf(path: Path, book_id: str, progress=…, cancelled=…, *, pages: range | None = None) -> Book
    # BookImportError for a damaged, password-protected or scanned PDF.
def is_scanned(doc: pymupdf.Document) -> bool
    # ≥ 60% of up to 24 sampled pages are one image covering ≥ 85% of the page with < 30 visible
    # characters: an image-only PDF, or a scan under an old OCR layer's invisible text.
# ingest/ocr.py — Removed (2026-10-23): PdfProbe, probe_pdf, ocr_available, text_quality,
# PaddlePageReader, OcrLine, SECONDS_PER_PAGE, the `ocr` extra (PaddleOCR), book_dir/ocr/.
# ingest/epub.py, ingest/docx.py, ingest/txt.py
def parse_epub(path: Path, book_id: str, progress=…, cancelled=…) -> Book   # images/fonts → book_dir/res/
def parse_docx(path: Path, book_id: str) -> Book
def parse_txt(path: Path, book_id: str) -> Book
```

**PDF rules** (digital text only, from PyMuPDF's rawdict word boxes): a scan
(`is_scanned`) is refused with BookImportError ("“<title>” is a scanned book. LivroSound
reads PDFs with digital text; scanned books aren't supported yet. …"), which the main
window shows under the title "Scanned books aren't supported yet."; damaged and
password-protected PDFs are refused too. Otherwise: furniture removal (running
heads/page numbers near the top/bottom of pages, e.g. `14 ARTFUL SENTENCES`,
`SHORT SENTENCES 19`); drop caps (a 1-letter glyph ≥2.5× body height joins the next
word: `A` + `NTHONY` → `ANTHONY`); paragraphs from first-line indents, vertical gaps,
and font-size changes; a line that stops mid-sentence and the one that goes on with it
stay in one block even when one is centred or set in; headings (≥1.25× body size, or
short centered lines, or `HEADING_RE`); block quotes / epigraphs (indented both sides
and/or smaller type → `quote`); footnotes (smaller type at the page foot starting with a
marker → `note`); paragraphs continued across page breaks merged (notes and furniture
don't interrupt them); line-end hyphens via `textutil.join_mode`; TOC from the PDF
outline (a coarse outline gains the book's own "Chapter N" headings one level below),
else the printed contents, else detected headings (consecutive heading blocks such as
`CHAPTER ONE` + `Short Sentences` make one entry "Chapter One: Short Sentences").
Everything is measured in the page's own em and line pitch. `Book.text_box` = robust
union of body-text boxes.

**Running heads and page numbers are never part of the book (user requirement, all
formats).** A printed page's header/footer — `10 ARTFUL SENTENCES` ([page number][book
title]) on even pages, `SHORT SENTENCES 19` ([chapter title][page number]) on odd pages,
or a lone page number — must not appear in any Block: it is never spoken and the
highlighter never passes over it. Text layers and ebooks made from scans pick
these up, often in small or letter-spaced caps (`ARTFUL SENTENCES`, `A R T F U L`), so
match them loosely: digits/roman numerals at either end, the rest compared with spaces,
case and punctuation ignored, against the book title, chapter titles, and any line text
that repeats near the top/bottom on several pages. **But a chapter heading is not a
running head** (user correction): on a chapter's opening page, `CHAPTER ONE` (often small
caps at the very top, beside an ornament) followed by the chapter title `Short Sentences`
is a heading — read aloud and highlighted. A top/bottom line counts as a running head
only if it carries a page number (arabic, or roman in front matter) at either end, is a
lone page number, or its text recurs at the top/bottom of several pages; a line matching
`HEADING_RE` (Chapter/Part/Book + a number word or numeral, "CHAPTER IV") that occurs once
is always a heading — its trailing numeral is not a page number. EPUBs and TXTs made from scans
(Internet Archive) carry the same heads as stray paragraphs or glued *inside* a paragraph
("…the walls 24 ARTFUL SENTENCES are white…"); remove those too (from the blocks and from
original_html). Highlighting is built only from a sentence's own characters: in the page
view a sentence across a page break gets one band per line on each page, never one over
the head; in the reflow views a highlight range is split wherever the mapped character
positions jump over text that is not part of the block. Highlight bands always cover
whole words (word boxes), never cut a word in half.

**Only the book itself is read (user requirement, all formats).** Books open with
pages nobody wants narrated: the cover, a blurb / back-cover copy /
"Praise for…" page, a half-title and title page (often several copies of the title), blank
pages, the "Digitized by the Internet Archive" page, the copyright page, acknowledgements,
the printed table of contents; at the back an index (and back-cover copy). Ingest keeps
these blocks (they are shown, muted, in the reflow layout and are on the page images) but
marks them with `Block.role` in `book.SKIPPED_ROLES`, so they are never spoken or
highlighted and "Generate audio from the beginning" starts after them. Exactly one block —
the cleanest title-page copy of the title, with its subtitle in the same block (or the
cover's title when there is no title page) — gets `role = TITLE_ROLE` and is read once
as "<title>, by <author>."; every other copy of the title is "frontmatter". Metadata
titles lose series and publisher tags (`matter.clean_title`: "Blood Meridian (Picador
Collection)" → "Blood Meridian"). Front pages that belong to the book stay readable:
dedications ("Dedicated to…", "For Lucien", "To my wife…", even on the title page's
file), epigraphs, prefaces, forewords, introductions, prologues. Signals:
- PDF: page-level, for the pages before the first chapter-like heading (Preface, Foreword,
  Introduction, Prologue, Chapter/Part One…, or the first outline/TOC target) and the last
  few pages: an image page with little text = cover; text pages before the title page or a
  long italic-heavy descriptive page = blurb; a page mostly made of title/subtitle/author/
  publisher = frontmatter (title copies); `looks_like_boilerplate` = boilerplate;
  "Copyright ©", "All rights reserved", "ISBN", "Library of Congress", "Published by",
  "Printed in" = copyright; a section headed Acknowledg(e)ments = acknowledgments; a section
  headed Contents / Table of Contents or a page whose lines mostly end in page numbers =
  contents; a section headed Index at the back = index (a skipped section runs until the
  next heading of the same or higher level).
- EPUB: EPUB3 `epub:type` (cover, titlepage, halftitlepage, copyright-page, toc,
  acknowledgments, index, frontmatter…) on body/sections, the landmarks nav / EPUB2
  `<guide>` (cover, title-page, toc, copyright-page, text/bodymatter = where the book
  starts), file names (cover.xhtml, titlepage.xhtml, copyright.xhtml, toc.xhtml, nav.xhtml),
  and the same heading cues.
- DOCX/TXT: the heading cues and copyright markers; a title at the top becomes TITLE_ROLE.
The table of contents is still parsed: it feeds `Book.toc` (the Contents popover) when
the file has no better outline, and each of its lines that names a chapter gets
`Block.link` = that chapter's first block, so clicking a line in the book's own contents
page jumps there like a hyperlink (EPUB contents pages: their `<a href>` targets).
`Book.toc` itself never lists front/back matter (Acknowledgments, the Index):
`load_book` drops those entries for every format, while `Block.link` still points
wherever the printed line says.

**EPUB rules**: container.xml → OPF (title, creator, manifest, spine), EPUB3 nav or EPUB2
NCX for the TOC (map `href#frag` via `Block.anchors`); walk spine XHTML: h1–h6 →
heading (level), p/div/li → paragraph, blockquote → quote, p with ≥2 `<br>` or pre →
verse, img/svg image → image block (file copied to res/), hr → separator; em/i/cite →
"i", strong/b → "b", sup / noteref links → "sup", small-caps classes → "sc". The original
HTML: spine bodies concatenated with their CSS, image src rewritten to the res/ files,
anchors `<a name="ls-b{i}"></a>` inserted before each block's element.
Internet Archive EPUBs (hocr-to-epub, one file per scanned page; `Book.scanned`): the
per-page "estimated to be only N% accurate" notices removed, pages under 50% are stray
marks (boilerplate), running heads and bracketed page numbers cut (`heads.py`),
paragraphs split by a page break joined ("gov" + "ernment"), and since the source has
one `<p>` per page, the text cut into paragraph-sized blocks at sentence ends.
**DOCX**: python-docx paragraphs (+ table cells) in order; Title/Heading N styles →
headings; Quote/Intense Quote → quote; run italic/bold → styles; alignment; core
properties for title/author; original HTML with the document's fonts, sizes and alignment.
**TXT**: utf-8 (BOM) / cp1252 / latin-1; Project Gutenberg header/footer stripped;
paragraphs split on blank lines, hard-wrapped lines rejoined, headings via `HEADING_RE`
or short all-caps lines between blank lines; original HTML = the text in a monospace
`<pre>`-like layout with block anchors.

EPUB, DOCX and TXT share `matter.number_headings`: headings whose number is set apart, as
calibre and many publishers print them, become one heading ("PART" + "One" → "PART One";
"1" + "Are You Smart…" → one heading); a lone numeral repeating "Chapter 3" is dropped;
lone numerals opening long stretches in sequence ("1", "2"… under Maurice's parts) are
the chapter headings, a level below.

### `ui/` — Qt (PySide6 6.11)

`ui/theme.py`: port Script Radio's `theme.py` (four themes Original/Quiet/Calm/Dark with
color-wheel highlighters and frame colors, ACCENT fern `#4F7942`, CANCEL_RED,
PROGRESS_BLUES, EXPORT_BLUE, `ui_font`, `stylesheet()`, `tooltip_stylesheet()`) plus
`READING_FAMILIES = ["Iowan Old Style", "New York", "Charter", "Georgia"]` and
`reading_font(size)`. Port `tests/test_theme.py` (contrast rules).

`ui/widgets.py`: port Popover, ThemesPopover ("Aa": theme cards, A−/A+ size, plus a
Override book layout switch, page view and highlighter controls, More Settings…), VoicePicker +
VoiceMenu (one narrator; menu grouped under "Women" / "Men" headers), VoicePreviews,
ContentsPopover (from ScenesPopover: TOC entries indented by level, each row is a link;
a small fern dot marks chapters whose audio is ready), PlayerCapsule (−10 sec, ⏮, ▶, ⏭,
+10 sec), GradientProgressBar, `confirm(parent, title, text, yes="Yes", no="No") -> bool`
(No left / Yes right), `inform(parent, title, text)`, ToggleSwitch, `aa_icon`, `list_icon`.

`ui/landing.py`: `LivroSoundLanding(QWidget)` — sleek, modern, minimalist: the open
hardcover book icon (`assets/icons/livrosound.svg`), the wordmark **LivroSound**, a quiet
tagline, and two buttons: **Upload New Book** (primary, fern) and **Go to Library**
(secondary). Signals `upload_clicked`, `library_clicked`.

`ui/dialogs.py`: `ImportProgressDialog` (**Removed 2026-11-06:** `LayoutDialog`), `ChooseVoiceDialog` (window title
"LivroSound | Choose a Voice", title "Choose a Voice", one Narrator VoicePicker with ▶
preview by Pocket-TTS; `ask(parent, engine, voice, …) -> voice | None`; Continue off with the
install hint when the engine can't run), `CustomizeAudioDialog` (Highlight
sentences, Read chapter titles, Read footnotes, Whisper check, Pause between paragraphs
0.2–2.0 s; Go Back / Generate Audio), `BookDetailsDialog` (a book's facts; edits its tags and
collections), `SettingsDialog`, `ChapterExportDialog` (see "Additions" below).
**Removed (2026-11-05):** `LibraryDialog` (replaced by the library page).

`ui/reader_view.py` (the shared interface), `ui/reflow_view.py`, `ui/page_view.py`:

```python
class ReaderView(QWidget-like):          # both views provide exactly this
    clicked: Signal(int, int)            # (block, char) the user clicked
    user_scrolled: Signal()              # wheel/drag/keys: suspends auto-follow
    def set_book(self, book: Book, mode: str) -> None      # "livrosound" | "original"
    def set_theme(self, theme: Theme) -> None
    def set_scale(self, value: float) -> None              # reflow: font pt; page view: zoom factor
    def show_highlight(self, block: int, start: int, end: int) -> None
    def clear_highlight(self) -> None
    def scroll_to(self, block: int, char: int = 0, *, animate: bool = True) -> None   # center it
    def keep_in_view(self, block: int, start: int, end: int) -> None   # re-center if it drifts out of the middle 70%
    def current_block(self) -> int                         # first block visible near the top
```

- `ReflowView` (QTextEdit, read-only): **LivroSound layout** = Apple Books look: Iowan Old
  Style (fallbacks New York, Charter, Georgia), centered column ≤ ~700 px, line height
  ~150%, first-line indents, centered chapter headings, quotes indented, verse lines kept,
  separators "⁂", images scaled to the column, inline italic/bold/small caps; built from
  blocks with exact char maps. **Original** (EPUB/DOCX/TXT) = `original_html` rendered by
  Qt, block starts found from the `ls-b{i}` anchors, characters aligned within each
  block's range ignoring whitespace. Highlight = ExtraSelections line by line (Script
  Radio `_show_highlight`); auto-scroll with an eased animation (Script Radio
  `_scroll_to_range`). Must stay responsive on a 350k-word EPUB (Anna Karenina).
- `PageView` (QAbstractScrollArea): **Original layout for PDFs** (digital text only) =
  continuous vertical scroll, each page cropped to `Book.text_box` (+ margin) and scaled to
  fit the width × zoom (like Preview zoomed in, continuous scroll — see the user's
  screenshots), pages rendered lazily off the UI thread with PyMuPDF (own Document per
  thread; where the view is heading first, a quick preview then the sharp page, so a long
  jump never lands on blank pages), LRU-cached; in Quiet/Calm/Dark white pages are tinted
  to the theme (pages with real color keep it); PDF annotations are never drawn;
  highlight = one band per printed line straight from `Block.words`, whole words only,
  padding never past the middle of the gap to the next word, blended onto the page;
  click → nearest word → (block, char).

`ui/player.py` — `BookPlayer(QObject)`: plays the book from the AudioStore across blocks
with no gaps, through a QAudioSink (24 kHz mono Int16, push mode pumped by a timer, as
Script Radio's live stream). Speed via `dsp.time_stretch` per block (prefetched off the
UI thread). An injectable output so tests run without a sound card.

```python
class BookPlayer(QObject):
    position_changed: Signal(int, int)   # block, ms into its audio (source time, not stretched)
    playing_changed: Signal(bool)
    needs_audio: Signal(int)             # playback reached a block with no audio yet
    finished: Signal()                   # end of the book
    def set_book(self, store: AudioStore, timeline: Timeline) -> None
    def play(self) -> None; def pause(self) -> None; def is_playing(self) -> bool
    def seek(self, block: int, ms: int = 0) -> None
    def position(self) -> tuple[int, int]
    def skip(self, delta_ms: int) -> None            # ±10 s across blocks (Timeline.skip)
    def next_block(self) -> None; def prev_block(self) -> None   # one paragraph forward/back
    def set_rate(self, rate: float) -> None
    def block_generated(self, block: int) -> None    # wake up if waiting for this block
    def set_waiting_allowed(self, allowed: Callable[[int], bool]) -> None   # main window: will generation reach this block soon?
```

`ui/main_window.py` — `MainWindow(engine: TTSEngine | None = None, output_factory=None)`
(default engine `load_engines()[0]`, Pocket-TTS; tests inject FakeEngine and FakeOutputs),
orchestrating the flows below. `main.py` quiets library logging (`quiet_libraries()`),
calls `ensure_reading_fonts()`, sets the app name "LivroSound", Fusion style,
`ui_font(13)`, the window/Dock icon (`livrosound-1024.png`), opens MainWindow.

## User flows (binding)

1. **Launch** → landing page. Resume is enabled when the library has books. Menu bar:
   File (Upload New Book… ⌘O, Go to Library ⇧⌘L, Settings… ⌘,), Audio (Audio-Generation
   Only… ⌥⌘G, Audio Player Only ⌥⌘P, Export Audio… ⇧⌘E), Edit (Find… ⌘F, Find Next ⌘G, Find Previous ⇧⌘G), View (Larger / Smaller
   Text ⌘+ / ⌘−), Help (About LivroSound, Credits).
2. **Upload New Book** → file dialog (PDF with digital text, EPUB — also an unzipped
   .epub folder — DOCX, TXT). The file's fingerprint is taken off the UI thread; a book
   already in the library asks "You've opened this book before. Pick up where you left
   off?" (Yes → flow 9). Import runs off the UI thread with `ImportProgressDialog`
   (shown after 300 ms, so a cached book opens without it; Cancel). A scanned PDF is
   turned away: "Scanned books aren't supported yet." with ingest's explanation; any
   other failure: "This book couldn't be opened."
3. (2026-11-06) No layout question: a book opens as its file made it (PDF pages, the EPUB/DOCX's
   own HTML; a TXT in the reading layout), unless Override book layout is on (see Additions).
4. `ChooseVoiceDialog` (the narrator, ▶ previews; Pocket-TTS is the only engine) →
   `CustomizeAudioDialog` (Go Back returns to Choose a Voice). Cancel anywhere → the book
   is shown without audio; clicking its text later opens Choose a Voice first.
5. The reader opens, then asks **"Generate audio from the beginning?"** Yes → generation
   starts at the first block that is read (usually the title announcement). No → an alert
   "Choose where to start": "Click any part of the text, or choose a chapter from
   Contents, to start generating audio from there." (also left in the status bar). The
   next click/Contents pick asks "Start generating audio from this point?".
6. While generating: blue gradient progress bar bottom-right ("Generating 37%" of the
   whole book), **Cancel Generation** floating above it (asks "Stop generating audio?";
   keeps what's made; the bar then becomes a fern **Resume Generating** button). Where
   the position has no audio yet, Play waits until the unbroken audio from the position
   reaches 5% of the book (or the end), then enables with the ready "ding" (only for
   audio asked for in this session) and "Ready to play — generation continues in the
   background". Where audio already exists, Play is enabled at once. At 100% the bar is
   replaced by **Export Audio** (see Additions, 2026-11-06).
7. **Playback**: the sentence being read is highlighted (unless Highlight is off) and
   auto-scrolled into view; through a heading's lead-in silence the last sentence stays
   lit until the heading is heard. Scrolling by hand pauses auto-follow until the next play/seek. −10 sec /
   +10 sec jump 10 s (audio, highlight and scroll follow); ⏮ / ⏭ jump one paragraph
   back/forward; speed menu (0.5×–2×) beside the capsule. Space = play/pause, ← / → =
   −10/+10 s.
8. **Clicking text / a Contents entry / a line of the book's own contents page**:
   generated → play from that sentence. Not generated → **"Audio not ready yet. Start
   generating audio from this point?"**; Yes → playback stops, generation restarts from
   that block (audio already made is kept), the 5% rule applies again. A click on
   something that is never read (front matter, an image) goes to the next block that is.
   Playback that runs into a block without audio waits ("Waiting for audio…") if the
   running generation makes it within `WAIT_AHEAD_CHARS` (2000) characters, otherwise
   pauses and asks the same question.
9. **Go to Library** → the library page → clicking a cover opens the book at its saved position
   (sentence highlighted and centered), audio ready where generated; if generation was
   unfinished: "Continue generating audio?" (continues from the first missing block at/after
   the position). No ding on resume. A PDF opened from a new folder is read from there;
   one whose file is gone opens in the LivroSound layout with "The PDF isn't where it
   was." Removing the open book from Your Books closes it first.
10. **Re-Generate Audio** (toolbar; disabled while generating) → Choose a Voice →
    Customize Audio → if audio exists: "Replace the audio made so far?" (Replace / Keep It)
    → flow 5.
11. Toolbar: **New Book**, **Contents** (TOC popover), **Aa** (themes, size, layout,
    highlight), **Re-Generate Audio**, the book title centered (reading serif).
12. Quit (or opening another book) saves the position; a running generation is cancelled
    cleanly (finished blocks are kept). If a thread can't stop at once, the window hides
    and the app quits when every thread has.

## As built

Notes on how the modules turned out, where they go beyond the contract above.
- `pytest` deselects slow tests by default (`addopts = -m 'not slow'`): 883 fast tests, 25 slow ones (real models, the user's sample books) with `uv run pytest -m slow tests/<file> -q`. `tests/test_main_window.py` drives every flow offscreen with scripted dialog answers, a FakeEngine that can be held mid-sentence and FakeOutputs. `tests/test_scripts.py` runs `scripts/smoke_test.py` (fake engine) and `scripts/make_voice_previews.py`.
- book.py gained `Block.role` (`book.SKIPPED_ROLES` = front/back matter shown but never read or highlighted; `book.TITLE_ROLE` = the single title announcement, read as "<title>, by <author>.") and `Block.link` (a line of the book's own contents page → target block index, -1 = none). `Block.skipped` / `Block.spoken` honor roles. speech.is_read already excludes skipped blocks and always reads the TITLE_ROLE block. See DESIGN.md "Only the book itself is read" and "Running heads and page numbers are never part of the book".
- speech.py: besides MAX_CHUNK_WORDS=30 there is MAX_SPOKEN_CHARS=140 per chunk (Pocket-TTS's tokenizer ~1.5 tokens/word); speakable flattens accents to ASCII; block_chunks ≈0.2 ms per chunk (call per block in the generation thread, not for the whole book on the UI thread); is_read ≈35 µs per block (cache results).
- dsp.py: `dsp.ffmpeg_binary()` finds ffmpeg even when launched from Finder (use it everywhere instead of a bare "ffmpeg"); time_stretch ≈30–40 ms per 2 s block; trim_speech uses a relative floor; level() only multiplies, so declicked zero edges stay zero.
- engines: `engines.load_engines()` returns `[PocketTTSEngine]`, a shared singleton (previews and generation share one model); `engine_named(name)`; output 24 kHz; Pocket-TTS ≈4.3x realtime (first call ≈7 s import+load). `voice_preview.make_preview(engine, voice)`; `validate.Transcriber().check(x, sr, text)` (≈1.45 s per 5 s clip); `validate.local_model` (moved from engines/omnivoice.py).
- voices.grouped() returns Voice objects; widgets take names: `[(g, [v.name for v in vs]) for g, vs in voices.grouped()]`.
- audio_store: `AudioStore(book_dir/"audio.sqlite")`; generated()/has()/durations() are in-memory and cheap; pcm()/info() hit SQLite on the calling thread's own connection; stop worker threads before close(); reset() frees disk space.
- timeline: `Timeline(order, durations)` requires an ascending order; prev/next step strictly by paragraph; start_ms_of cached.
- library: keep ONE Library instance per window and save() after changes; timestamps `datetime.now().isoformat(timespec="seconds")`; `Settings.speech_options()`.
- ui/widgets.py: `confirm(parent, title, text, yes="Yes", no="No", *, destructive=False)` (destructive → red Yes; use it for Cancel Generation / Replace audio / Remove from Library), `inform(parent, title, text)`, both built on `Alert(QDialog)` (tests patch `Alert.exec`); `PlayerCapsule(parent)` owns its five buttons with signals play_clicked(), skip_clicked(int ms), paragraph_clicked(int ±1) and methods set_playing, set_play_enabled, set_navigation_enabled; `ThemesPopover(parent, current_key, *, layout, highlight)` signals theme_chosen, text_size_step, layout_chosen, highlight_toggled; `ContentsPopover(parent, entries[(title, level, ready)], current=None)` signal picked(int), popup_under(button); `VoicePreviews(render, parent)` with set_render/toggle/warm/stop/shutdown/before_play; `VoicePicker(groups, current, previews, parent)`. Theme object names styled by theme.stylesheet(): readerView, pageView, readerStage, transportWidget, bookTitle, pillButton, speedCombo, cancelButton, exportButton, resumeButton, generateAudioButton, goBackButton, castingTitle, castingSubtitle, hint, caption, primaryButton, secondaryButton, dangerButton, card. On theme change: themes.set_current(t) before setStyleSheet.
- Icons: assets/icons/livrosound.svg (book art), assets/icons/livrosound-1024.png (window/Dock icon). Ready ding: assets/sounds/ready.wav.
- library: `BookRecord.speech_options` (asdict of the SpeechOptions the book's audio is made with; `record.audio_options()` → SpeechOptions | None) and `BookRecord.read_fraction` (spoken characters before the saved position, -1 = unknown; `dialogs.read_fraction` uses it before falling back to book.json). Old library.json files load unchanged (`_from_dict` honors default factories).
- ui/widgets.py: `ContentsPopover.set_ready(list[bool])` updates the fern dots while the popover is open.
- library: no OCR fields on BookRecord and no `Settings.engine` any more (old JSON keys are ignored); `BookRecord.engine` still records which engine made a book's audio (always "Pocket-TTS" now), so `audio_signature` of existing audio is unchanged.
- ui/main_window.py: `MainWindow(engine=None, output_factory=None)` (default `load_engines()[0]`, Pocket-TTS; tests inject FakeEngine and FakeOutputs). One `GenerationWorker(QThread)` at a time (signals block_done, status, finished_ok, failed, cancelled; slots get the worker passed explicitly); a restart cancels it and starts the next run from its `finished`. Play is gated by `stream_ready` from the reader's position after every (re)start (`_gate`; the ding only for audio asked for in this session, never for "Continue generating audio?"). The player may wait only for blocks the running worker makes within `WAIT_AHEAD_CHARS` (2000) characters, else it asks. Clicks, Contents picks and contents-page links all go through `_go_to(block, char)`; questions are asked from `QTimer.singleShot(0, self, …)`, never inside the view's or player's own handlers. A closed book's worker "retires": its store closes when the thread ends, and reopening/re-importing that book waits for it (`_when_released`). Quit hides the window if a thread (a chunk, an import, an export, a voice preview) can't stop within 2 s, closes once all have, then quits the app itself (quitOnLastWindowClosed is off while hidden: a dialog closing then would quit under the running threads, and the hidden window's own close doesn't count as the last one); nothing a Task or a stopped worker hands back after Quit is acted on. The 5% gate applies only where the position has no audio yet. Removing the open book from the library closes it first; its files go once no worker or export uses them. An export keeps its book's store open through book switches. Work that would switch books waits while a modal dialog is up (`_when_no_dialog`), and code after a dialog checks the same book is still open (`_still`). A moved PDF is read from its new place; a missing one opens in the LivroSound layout with an explanation. LayoutDialog previews: the PDF's opening page via PyMuPDF (annotations off), an off-screen ReflowView grab for the LivroSound layout and (HTML under 400k chars) the original. Menu bar: File (Upload New Book… ⌘O, Resume a Previous Book… ⇧⌘O), Help (About LivroSound, Credits). `main.py` `quiet_libraries()` silences Pocket-TTS's INFO chatter and torch's weight_norm warning (errors still shown).
- ingest (2026-10-23): `load_book(path, *, progress, cancelled)`; `book_id_for(path)` is public (the main window fingerprints a file off the UI thread before importing, to ask "You've opened this book before."); `PARSER_VERSION` = 2; the old audio is deleted only after a re-parse succeeds; `Book.toc` entries on skipped blocks are dropped for every format (`_without_skipped_toc`), and export skips them too. PDF: scanned PDFs refused (`pdf.is_scanned`); title + subtitle on the title page form one TITLE_ROLE block; a mid-sentence line break never splits a block. EPUB: series/publisher tags stripped from the OPF title (`matter.clean_title`); dedications stay readable even on the title page's file; Internet Archive EPUBs (`Book.scanned`) cut into paragraph-sized blocks. `matter.number_headings` (EPUB/DOCX/TXT) merges "PART" + "One" and finds numeral-only chapter headings.
- export: no chapter for a TOC entry on a skipped block; the first chapter starts at 0:00 (players show "no chapter" for audio before the first mark).
- ui/page_view.py: digital PDFs only, so bands come straight from the text layer's word boxes (the old ink-offset/snap fitting for OCR layers is gone); padding capped at the middle of the gap to the next word; pages tinted by brightness in Quiet/Calm/Dark; after a long jump the destination renders first (preview, then sharp) while the scroll animates.
- ui/main_window.py (later): "No" to "Generate audio from the beginning?" shows an alert "Choose where to start" with the hint, and leaves it in the status bar; clicking the text of a book without a voice opens Choose a Voice → Customize Audio and generates from there; the highlight keeps the last sentence lit through a block's lead-in silence; a scanned PDF's import error is titled "Scanned books aren't supported yet."; unzipped EPUB folders open like files.
- scripts: `smoke_test.py <book> [--engine fake|pocket] [--blocks N] [--voice NAME] [--out FILE]` (temp cache; MP3 → `cache/smoke_test.mp3`); `make_voice_previews.py [--engine Pocket-TTS]`.
- **Removed (2026-10-23, user decisions; code kept in `~/livrosound-scanned-books-snapshot-2026-10-23.tar.gz`)**: scanned books — `ingest/ocr.py` (PaddleOCR PP-OCRv6: `PdfProbe`, `probe_pdf`, `ocr_available`, `text_quality`, `PaddlePageReader`, `OcrLine`, `SECONDS_PER_PAGE`), `ImportOptions` and `load_book`'s options argument, `import.json`, `book_dir/ocr/`, `ImportDialog`, `Book.text_source`, `BookRecord.scanned` / `ocr` / `ocr_quality`, the `ocr` extra; OmniVoice — `engines/omnivoice.py`, the engine choice in Choose a Voice, `Settings.engine`, the `omnivoice` extra.

## Additions (2026-11-05, user requests)

The user asked for Readest's settings and customizations, a library, and new audio
modes ("add all the features in readest features.pdf"; the PDF shows Readest's ☰ menu).
Decisions the user made when asked: Look Up uses the Mac's offline dictionary (New
Oxford American) with Merriam-Webster and Oxford as web links (the only optional
network use, on a click); audio-generation only mode is built even though it saves
little CPU (the display costs little next to Pocket-TTS); single/double page applies to
every layout; highlights get five colors, notes and a list; Export Chapters makes only
the chosen chapters and saves each as soon as it's ready. Hyphenation (in Readest) is
not done: Qt doesn't hyphenate and soft hyphens would break the char maps.

### New modules

```python
# shelf.py — the library's ordering, grouping and search (pure)
SORTS: dict[str, str]   # title author format pages series date_read date_added date_published progress time_remaining
GROUPS: dict[str, str]  # none authors tags subjects collections series status
def sort_records(records, key, descending=False, then="", then_descending=False) -> list[BookRecord]   # missing values last
def group_records(records, group) -> list[tuple[str, list[BookRecord]]]   # a book can be in several groups
def matches(record, query) -> bool; def seconds_remaining(record, rate=1.0) -> float | None
def status(record) -> "Reading" | "Not Started" | "Finished"; def all_values(records, "tags"|"collections")

# bookinfo.py — read straight from the source file (no PARSER_VERSION bump: audio is kept)
INFO_VERSION = 1
@dataclass class BookInfo: subjects, series, series_index, published, pages, cover (bytes)
def read_info(path, kind, *, words=0) -> BookInfo     # EPUB OPF (dc:subject, dc:date, calibre:series,
                                                      # belongs-to-collection, cover-image), PDF metadata
                                                      # + first page as the cover, DOCX core properties
def save_cover(book_id, data) -> Path | None          # book_dir/cover.jpg, 600 px high
def cover_path(book_id) -> Path

# search.py — Find in the book
class FoldedBook: find(query, limit=MAX_MATCHES) -> list[Match(block, start, end)]   # case/accent/quote-insensitive
def nearest(matches, block) -> int

# annotations.py — the reader's highlights and notes, book_dir/annotations.json
COLORS = {yellow, green, blue, pink, purple}
@dataclass class Annotation: start_block, start_char, end_block, end_char, text, color, note, created, id
class Annotations(book_id): all() add(book, start, end, color, note) get(id) update(id, color=, note=) remove(id) at(block, char)

# dictionary.py — DictionaryServices (DCSCopyTextDefinition) via ctypes on macOS
def define(word) -> str | None; dictionary_app_url / merriam_webster_url / oxford_url(word) -> str

# estimate.py — generation time: RateMeter (first block left out), rates(saved_normal, saved_only,
#   live, live_is_generation_only) -> (normal, only) chars/s, DEFAULT_RATE 60, GENERATION_ONLY_GAIN 1.08,
#   blend(), duration_text()
# power.py — KeepAwake: NSProcessInfo beginActivityWithOptions (no App Nap / idle sleep) while generating
# ui/fonts.py — add_font(path) copies into paths.fonts_dir() and registers; load_user_fonts(); families()
# ui/library_page.py — LibraryPage(library, settings): BookShelf (painted grid/list, sections, Select),
#   view_menu() / app_menu() / context_menu(id); signals open_requested, upload_requested,
#   home_requested, back_to_book_requested, remove_requested(list), details_requested, reveal_requested,
#   settings_requested, about_requested, option_toggled(field, on), changed
# ui/audio_pages.py — GenerationPage (audio-generation only), PlayerPage (audio player only), ChapterBar
```

Extended APIs:
- `Settings`: library_view/columns/group/sort/descending/then/then_descending; open_last_book,
  auto_import, always_show_status_bar, auto_mode, light_theme, dark_theme; font_family,
  font_weight, line_spacing, paragraph_spacing, word_spacing, letter_spacing, justify,
  margin_left/right (-1 = centered), max_column, page_view ("scroll"|"single"|"double"),
  show_page_numbers, show_read_percent, custom_page/custom_text; highlight_unit
  ("word"|"sentence"|"paragraph"), read_aloud, export_format ("mp3"|"m4b"), rate_normal,
  rate_generation_only.
- `BookRecord`: date_added, subjects, series, series_index, published, pages, spoken_chars,
  info_version, tags, collections.
- `ReaderView`: `set_page_mode(mode)`, `set_style(ReadingStyle)`, `page_info() -> (first, last,
  total) | None`, `spread_step()`, `page_step(±1)`, `turn_to(value)`, `set_marks([(block, start,
  end, color)])`, `selected_range()`, `clear_selection()`; signals `selection_made(QPoint)`,
  `context_requested(block, char, QPoint)`. `ReflowView.count_pages()` lays the book out to count.
- `Generator.run_blocks(blocks, ...)`; `GenerationWorker(generator, start, blocks=None)`.
- `export.export_audio(book, store, order, path, *, fmt, bitrate, cover, progress, cancelled)`
  (MP3, or M4B = AAC in MP4 via ffmpeg's ipod muxer: QuickTime + Nero chapters, cover as attached
  art); `export_chapter(book, store, blocks, path, *, title, track, fmt, cover, ...)`;
  `chapter_blocks(book, order)` (top-level chapters, the opening goes with the first);
  `chapter_file_name(n, title, fmt)`. `export_mp3` is kept.
- `voices.grouped()` starts with ("Recommended", [Aaron, Ana]) (user: "Aaron and Ana must be at
  top of the list").
- `theme.custom_theme(page, text)`, `set_custom_colors()`, `theme_for("custom")`,
  `selection_color(theme)` (mouse selection is never the reading highlight's color),
  `reading_font(size, weight, family)`.

### New flows (binding)

13. **Library page** (Go to Library, ⇧⌘L, the toolbar's Library): covers (grid) or rows (list),
    search, Columns Auto/±, Group by, Sort by + Then by, each Ascending/Descending (all kept in
    Settings). Select → Open / Collection / Tag / Details / Remove; right-click a cover for the
    same plus Show in Finder. Book info and covers missing from older records are read in the
    background while the page is open. The open book stays open (Back to Book); Remove asks and
    closes it first.
14. **☰ options**: Open Last Book on Start; Auto Import on File Open (Finder FileOpen events and
    drag-and-drop import at once, else "Add “x” to your library?"); Always Show Status Bar (the
    "Page 12 of 340 · 34%" line under the book; else it fades after 3 s); Auto Mode (light_theme
    in macOS Light, dark_theme in Dark; choosing a theme sets the one for the current appearance).
15. **Settings** (⌘, or Aa ▸ More Settings…): Font, Layout, Color, Reading, General, Shortcuts;
    changes apply live. Page view continuous / single / double for every layout (paged: Qt's
    pagination, no line cut by a page edge; double page paints two pages; PDF: whole pages
    fitted). Highlighter word (estimated within each cue by syllables) / sentence / paragraph.
16. **Select text** (drag or double-click; a click waits doubleClickInterval so a double click
    never starts playback): Highlight (5 colors), Add Note…, Copy, Look Up, Find in Book.
    Right-click a highlight: Color, Edit Note…, Copy, Remove; right-click text: Look Up, Read
    Aloud from Here. Notes (toolbar) lists them; click jumps.
17. **Find** ⌘F: matches in reading order from the page on screen; the current one marked orange.
18. **Read books aloud off** (Settings ▸ Reading): an e-reader — no player, no voice or audio
    questions, clicks only follow contents links, ←/→/Space turn pages; the saved position is the
    page on screen (also whenever a book has no audio yet).
19. **Export**: Export MP3 / Export M4B (the status-bar button follows the last format; More…
    opens the Audio menu); **Export Chapters…** picks chapters, makes only the missing blocks of
    those (`run_blocks`) and saves each as "NN - Title.ext" in the chosen folder as soon as it's
    complete, one export at a time.
20. **Audio-generation only** (⌥⌘G): the generation page (cover, progress, time left, "When it's
    done, save: Nothing / MP3 / M4B / Chapters" into ~/Music without asking); the views draw
    nothing and the PDF render thread stops; Back to Reading returns (generation continues).
21. **Time estimates**: beside the progress bar, "About X left · about Y in audio-generation
    only mode", from the rate measured in this run (else the Mac's saved average for that mode,
    else 60 chars/s); each run's rate is blended into Settings when it ends.
22. **Audio player only** (⌥⌘P, toolbar Player): the player page — cover, chapter title, "7h 15m
    left in book", a bar across the chapter (drag to move; un-made audio asks to make it), the
    whole book as chapter segments (click to go), speed, Chapters, the player capsule, Show Text.
23. While any generation runs, KeepAwake holds off App Nap and idle sleep.

## Revisions (2026-11-06, user's first test on the Mac)

User decisions: "the whole Livrosound layout versus original is confusing... scrap that to make
one customizable layout... first time the user imports a pdf, the app should keep whatever
font, settings, etc the file came with but under settings, make an option 'Override book
layout'... Below it, include an option that asks 'Set as Default for all books?' if the user
says no, then the customization is just for that one book". Themes still tint pages with the
override off; theme and page view stay app-wide.

- **One layout.** `LayoutDialog`, its previews and Aa's Original | LivroSound control are gone.
  `BookRecord.layout`: "" = follow `Settings.override_layout` (False by default), "original" /
  "livrosound" = this book's own choice (old records keep theirs). The views' modes are
  unchanged underneath ("original" = the file's layout, "livrosound" = LivroSound's typesetting).
- **Override book layout** (Settings ▸ Font / Layout, and Aa) with **Set as default for all
  books** under it. `library.StyleStore(settings, record)` reads/writes the per-book fields
  (`BOOK_STYLE_FIELDS`: font family/weight/size, spacing, justify, margins, text width) into
  `BookRecord.style`, or, with the box ticked (or no book open), into Settings; ticking it moves
  the book's choices into Settings and clears its own. `SettingsDialog(..., styles=StyleStore)`
  announces `changed("override")`; the style controls are off until the override is on.
- **Text width** = `Settings.text_width` (% of the window, 85 by default) instead of a fixed
  700 px column (`max_column` removed; `ReadingStyle.width`). Margins set by hand still win.
- **Single / double page look like pages** (user's Readest screenshots): `ui/paper.py` —
  `sheet_rects(width, height, pages)` (a 0.74-aspect sheet as tall as the view; two meet at the
  spine), a seamless 1/f-noise paper texture tinted light or dark, `paint_sheet`, `paint_spine`,
  `paint_labels` (the chapter at the top left, "24 / 254" at the bottom right; the corner pill
  then shows only the % read). ReflowView paints the sheets and maps mouse events onto them
  (`_flow_point`); PageView shows each printed page whole (uncropped) when paged.
- **Generate audio from the beginning?** is `GenerateAudioDialog`: Export the audio when it's done
  (on), Audio file format M4B (default) / MP3, Save in [Choose Folder…]. Yes/No as before; the
  plan goes to `BookRecord.auto_export` and is carried out at 100% without asking
  (`_export_when_done`). The generation-only page's "When it's done, save:" edits the same plan.
- **Export Audio** (status-bar button, Audio ▸ Export Audio… ⇧⌘E) is `ExportAudioDialog`: format
  (M4B default), Whole Book or Chapter by Chapter (tick chapters), Save in folder. A whole book not
  finished yet is made first and saved by itself; chosen chapters are made (only those) and saved
  one by one into `<folder>/<Book title>/`. `Settings.export_format` / `export_folder` remember the
  last choice. Export Chapters (menu item and dialog) is gone.

