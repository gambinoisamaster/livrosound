# LivroSound

**Your books, read aloud.** LivroSound is a Mac app that turns a book (a PDF,
an EPUB, a Word document or a text file) into narrated audio in one of your
own cloned voices. You read along while it plays: the sentence being spoken is
highlighted and the page scrolls with it. When the whole book is done, export
it as one MP3 with chapters.

**Everything runs on your Mac.** Your books never leave it: no accounts, no
API keys, no cloud service that could keep your pages. The voices and the
optional Whisper check run locally.

LivroSound is the book-reading sibling of Script Radio (which reads
screenplays). It is for **personal, non-commercial use** (see
[Credits & licenses](#credits--licenses)).

## What it does

### Your books

- **PDF, EPUB, Word (.docx) and plain text (.txt).** LivroSound finds the
  paragraphs, chapter headings, quotations, poems, footnotes and scene breaks,
  and builds a table of contents. An unzipped EPUB (a folder Finder still
  shows as a book) works too.
- **PDFs need real text.** A PDF made from a word processor or a publisher's
  file reads perfectly. A **scanned** PDF (photos of printed pages) isn't
  supported yet: text recognition is still too imperfect to narrate, so
  LivroSound says *"Scanned books aren't supported yet."* and opens nothing.
  Try an EPUB, Word or text version of the book instead. (EPUBs that the
  Internet Archive made from scans do open: their page clutter is cleaned up.)
- **Only the book itself is read.** Running heads and page numbers
  (`10 ARTFUL SENTENCES`, `SHORT SENTENCES 19`, a lone `14`) are removed: never
  read, never highlighted. A chapter opening such as **CHAPTER ONE** /
  **Short Sentences** is a heading, and is read. The pages nobody wants
  narrated (the cover, blurbs and "Praise for…", extra title pages, scanner
  notices, the copyright page, acknowledgements, the printed table of
  contents, the index) stay on screen, muted in the LivroSound layout, but are
  never read or highlighted. The title is announced once, at the start:
  *"Blood Meridian, by Cormac McCarthy."* (series tags such as "(Picador
  Collection)" are left off). A dedication, an epigraph, a preface or an
  introduction is part of the book and is read.
- **Mistakes in the file stay in the file.** LivroSound reads what's in your
  book. If the file itself has a typo or a missing word, the narrator reads
  it that way.

### Two ways to see it

Pick one when you open a book; switch any time from **Aa**.

- **Keep Original Layout**: the book as it was designed. A PDF shows its own
  pages in one continuous scroll, zoomed in so the text column fills the
  window (like Preview with Continuous Scroll), with a soft highlight band over
  the words being read. An EPUB, Word or text file keeps its own fonts and
  formatting.
- **LivroSound Layout**: clean, comfortable text like Apple Books, in Iowan
  Old Style (or New York, Charter, Georgia), with a centered column, generous
  line spacing and centered chapter headings.

### Voices

Seventeen narrators, all cloned from your own recordings and named by first
name. **Aaron** (the former Default_Narrator_Male) is the default; **Ana** is
the former Default_Narrator_Female.

| Women | Men |
| --- | --- |
| Emily, Gigi, Liz, Ruth (young) | Danny, Kyle (young) |
| Ana, Anne, Susan, Therese (adult) | Aaron, Connor, Ethan, Johnny, Matthew (adult) |
| Gertrude (mature) | Ronald (mature) |

Press ▶ beside a voice to hear it say *"It was the best of times, it was the
worst of times."* Only these cloned voices are offered, never the built-in
voices that come with the speech engine. To add one, see
[assets/voices/README.md](assets/voices/README.md).

The voices are made by **Pocket-TTS**, Kyutai's small speech model, on your
Mac's processor. On an M1 it makes audio about **four times faster than it
plays**: a 10-hour book takes about 2½ hours, and you can start listening
after a few minutes.

**Clean joins.** A book is spoken a sentence at a time. Pocket-TTS sometimes
ends a clip with a tiny cut-off sound, heard as a "tick" where two sentences
meet; LivroSound trims it off, fades every clip's edges over 8 ms, and evens
out the volume of every paragraph (−19 LUFS), so there are no ticks or clicks
and no voice is louder than another.

### Listening

- **Start listening at 5%.** Audio is made in the background. As soon as 5% of
  the book (counted from where you are) is ready, Play turns on with a soft
  "ding" and *"Ready to play — generation continues in the background"*.
  Where audio already exists, you can play right away. Progress shows bottom
  right ("Generating 37%"), with **Cancel Generation** above it (it asks first
  and keeps everything already made; the bar then becomes **Resume
  Generating**).
- **Highlighting and auto-scroll.** The sentence being read is highlighted
  and kept in the middle of the window. Scroll by hand and the page stays
  where you put it until you next press Play or jump somewhere. Turn the
  highlight off in **Aa** if you prefer.
- **Controls.** **−10 sec** / **+10 sec** jump ten seconds; **⏮** / **⏭** jump
  one paragraph back or forward; the speed menu goes from 0.5× to 2× without
  changing the voice's pitch. Keyboard: Space plays and pauses, ← and → jump
  ten seconds. The highlight and the page follow every jump.
- **"Generate audio from the beginning?"** When a book opens without audio,
  LivroSound asks this first. Say No to start somewhere else: *"Click any part
  of the text, or choose a chapter from Contents, to start generating audio
  from there."*
- **Click to listen, click to generate.** Click any sentence: if its audio is
  ready, playback starts there. If not, LivroSound asks *"Audio not ready yet.
  Start generating audio from this point?"* Yes starts making audio from that
  paragraph (keeping everything already made), and the 5% rule applies again
  from there. If playback reaches a part that isn't made yet, it waits when
  that audio is about to arrive, and otherwise pauses and asks the same
  question. Clicking the text of a book that has no voice yet opens Choose a
  Voice first.
- **Contents.** The **Contents** button lists the book's chapters; every entry
  is a link. A green dot marks chapters whose audio is ready. The book's own
  printed table of contents works the same way: click a line and you're at
  that chapter.
- **Themes.** **Aa** sets the text size (or the zoom of a PDF's pages), the
  theme (Original, Quiet, Calm or Dark, each with a highlighter color picked
  from the color wheel to stand out on its page), the layout, and the
  highlight. In Quiet, Calm and Dark a PDF's white pages take the theme's
  colors too.

### Coming back

- **Go to Library** shows every book as its cover (or in a list), with how
  much you've read and how much audio is ready. Search, group them (authors,
  tags, subjects, collections, series, status) and sort them (title, author,
  format, pages, series, date read, date added, date published, progress, time
  remaining). Select books to open, tag, put into collections or remove; Details
  edits a book's tags and collections. A book reopens at the sentence where you
  stopped. If its audio wasn't finished, LivroSound asks *"Continue generating
  audio?"*.
- Opening a file you've opened before asks *"You've opened this book before.
  Pick up where you left off?"*
- If you've moved a PDF, open it from its new place and its pages come back
  (until then the book shows in the LivroSound layout).
- **Re-Generate Audio** (in the toolbar) lets you pick another voice or other
  options and make the audio again (it asks before replacing what's there).

### Export

When the audio reaches 100%, **Export MP3** (or **Export M4B**, an audiobook like Audible's) takes the progress bar's place; **Audio ▸ Export Chapters…** saves chosen chapters as files of their own, making only their audio first. It
saves the whole book as one MP3 with the title and author filled in and a
chapter marker for every chapter in the contents, so Apple Books, podcast
apps and car stereos can skip between chapters. It's fast (a 10-hour book in
under two minutes) and compact (96 kbps mono, about 43 MB per hour).

### Optional Whisper check

In **Customize Audio**, **Check every sentence with Whisper** has each
sentence transcribed on your Mac right after it's made and compared with the
book; a sentence that comes out wrong is made again (up to three tries).
It's slower: it more than doubles the time audio takes to make.

## Setup

You need [uv](https://docs.astral.sh/uv/) (it installs Python and everything
else) and [ffmpeg](https://ffmpeg.org) for playback speed and MP3 export:

```bash
brew install ffmpeg
cd ~/livrosound
uv sync                      # the app and Pocket-TTS
uv sync --extra validate     # also the Whisper check (optional)
```

The speech model downloads once, the first time, and is kept in
`~/.cache/huggingface`; after that LivroSound works offline. Pocket-TTS's
voice-cloning weights need a one-time step: accept the terms at
<https://huggingface.co/kyutai/pocket-tts> and sign in with
`uvx hf auth login` before the first run. The Whisper check downloads its
model (`whisper-large-v3-turbo`, about 1.5 GB) the first time it's used.

LivroSound needs a Mac with Apple Silicon (M1 or later) to make audio.
**Intel Macs can't run Pocket-TTS** (it needs a newer PyTorch than Intel Macs
get): the app opens and shows books, but Choose a Voice explains that no
audio can be made there.

## Run

```bash
uv run main.py
```

1. **Upload New Book** (or File ▸ Upload New Book…, ⌘O) and pick a PDF, EPUB,
   Word or text file.
2. **Choose a Layout**: Keep Original Layout or LivroSound Layout.
3. **Choose a Voice**: pick the narrator (▶ to listen first), then
   **Customize Audio**: highlight sentences, read chapter titles, read
   footnotes, the Whisper check, and the pause between paragraphs (0.2–2 s).
   **Generate Audio**. (Cancel at any step and the book opens without audio.)
4. Answer **"Generate audio from the beginning?"**, or say No and click where
   you want to start.
5. Press **Play** when it turns on. Use −10/+10 sec, ⏮/⏭, the speed menu,
   Contents, or just click the text.
6. At 100%, **Export MP3**.

Close the app whenever you like: your place is saved, and an unfinished
generation keeps everything it made.

## How it works

1. **Reading the file.** Each format is turned into one list of paragraphs
   ("blocks"): headings, paragraphs, quotes, verse, notes, images, scene
   breaks. A PDF is read from the position of every word on the page
   (indents, gaps and type sizes tell paragraphs and headings apart).
   Running heads, page numbers and scanner notices are removed, line-end
   hyphens are rejoined ("independ-" + "ent"), and front and back matter are
   labelled so they're never read.
2. **What the narrator says.** Each paragraph is split into sentences, long
   ones at their commas. Numbers, years, money and abbreviations become words
   ("1914" → "nineteen fourteen", "Chapter XIV" → "Chapter fourteen",
   "e.g." → "for example"); note marks and URLs are dropped.
3. **Making the audio.** Every sentence is spoken by Pocket-TTS with your
   cloned voice, trimmed of its end tick, faded at its edges, and joined with
   a natural pause; each paragraph is leveled and saved on its own. That's
   what lets you listen at 5%, start anywhere, cancel without losing anything,
   and resume later.
4. **Playing.** The player streams paragraph after paragraph with no gaps.
   Because each sentence was made separately, LivroSound knows exactly when
   it's heard, so the highlight follows the voice precisely.
5. **Export** pipes the paragraphs in order through ffmpeg into one MP3 with
   ID3 tags and chapters.

## Where your data lives

Everything LivroSound remembers is in the `cache/` folder inside the app
folder. Your book files themselves are never changed or moved.

| | |
| --- | --- |
| `cache/settings.json` | your preferences: theme, text size, voice, reading options |
| `cache/library.json` | your books: where each file is, its layout and voice, where you stopped, how much audio is made |
| `cache/books/<id>/` | one folder per book: the text as LivroSound read it (`book.json`, `original.html`), images and fonts from EPUBs (`res/`), and the audio (`audio.sqlite`) |
| `cache/voice_previews/` | the ▶ clips of each voice |

The audio takes about 170 MB per hour of book (the exported MP3 is about a
quarter of that). **Remove from Library** deletes that book's folder;
deleting `cache/` starts LivroSound fresh.

## For developers

```bash
uv run pytest                                   # fast tests (no models)
uv run pytest -m slow tests/test_engines.py -q  # tests that load real models
uv run python scripts/smoke_test.py book.epub                 # import + fake audio + MP3
uv run python scripts/smoke_test.py book.txt --engine pocket  # the same with Pocket-TTS
uv run python scripts/make_voice_previews.py    # every voice's ▶ clip, made up front
```

The smoke test prints the title, author, contents, the front and back matter
it skips, what the narrator says first, and timings; it works in a temporary
folder and leaves only the MP3 (`cache/smoke_test.mp3`). `docs/DESIGN.md` is
the design and module contract; `CLAUDE.md` is the map of the code.

## Credits & licenses

(In the app: **About LivroSound** and **Help ▸ Credits**.)

- **[Pocket-TTS](https://github.com/kyutai-labs/pocket-tts)** by Kyutai,
  MIT license: the text-to-speech and voice-cloning engine (on the CPU, with
  [PyTorch](https://pytorch.org), BSD).
- **[PyMuPDF](https://pymupdf.readthedocs.io)** by Artifex, **AGPL-3.0**:
  reads PDFs and draws their pages. Because of the AGPL, LivroSound is meant
  for personal, non-commercial use, like Script Radio; if it were ever
  distributed, the AGPL's terms would apply to the whole app.
- **[PySide6 / Qt for Python](https://doc.qt.io/qtforpython/)**, LGPL-3.0:
  the interface.
- **[mlx-whisper](https://github.com/ml-explore/mlx-examples)** (MIT) /
  **[faster-whisper](https://github.com/SYSTRAN/faster-whisper)** (MIT),
  running OpenAI's Whisper weights (MIT): the optional Whisper check.
- [Beautiful Soup](https://www.crummy.com/software/BeautifulSoup/) (MIT),
  [lxml](https://lxml.de) (BSD), [python-docx](https://github.com/python-openxml/python-docx)
  (MIT), [num2words](https://github.com/savoirfairelinux/num2words)
  (LGPL-2.1), [NumPy](https://numpy.org) (BSD), and
  [ffmpeg](https://ffmpeg.org) with LAME (LGPL/GPL), installed separately.
- Reading fonts (Iowan Old Style, New York, Charter, Georgia) and SF Pro
  ship with macOS; LivroSound uses them but doesn't include them.
- **The narrator voices** were recorded and cloned by the user, from Script
  Radio's voice library.

## Reading and listening, your way (Readest-style settings)

- **Settings** (⌘,): your font (or one you add), weight and size; line, paragraph,
  word and letter spacing; justified text; left and right margins and the widest
  column; continuous scroll, single page or double page; your own paper and text
  colors; Auto Mode (follows macOS Light/Dark); page numbers and % read.
- **Highlighter**: word by word, sentence by sentence or paragraph by paragraph.
- **Select text** to highlight it (five colors), add a note, copy it, look a word up
  (the Mac's own dictionary offline; Merriam-Webster and Oxford online) or find it.
  **Notes** lists your highlights. **⌘F** finds anything in the book.
- **Read books aloud** off (Settings ▸ Reading) makes LivroSound an e-reader only.
- **Audio ▸ Audio-Generation Only** makes the audio with nothing else on screen and
  can save the MP3, M4B or chapters by itself at the end; the time left is shown
  for both modes. **Audio ▸ Audio Player Only** shows just the cover, the chapter
  and the player.

