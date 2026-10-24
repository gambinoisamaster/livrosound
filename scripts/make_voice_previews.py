"""Render the Choose a Voice audition clip of every voice.

Usage: uv run python scripts/make_voice_previews.py [--engine Pocket-TTS]

Each clip is the voice reading voice_preview.PREVIEW_LINE ("It was the best of
times, it was the worst of times."), finished like the book's audio and cached
in cache/voice_previews/. Clips already made are skipped, so it's cheap to
re-run after adding a voice; a re-recorded voice gets a fresh clip. Optional:
the app makes any missing clip in the background when Choose a Voice opens.

Pocket-TTS loads in ~7 s, then is about 4x faster than real time (17 clips: ~35 s).
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from livrosound.engines import load_engines
from livrosound.voice_preview import PREVIEW_LINE, make_preview, preview_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Make every voice's audition clip.")
    parser.add_argument("--engine", help="only this engine (Pocket-TTS)")
    args = parser.parse_args(argv)

    engines = []
    for engine in load_engines():
        if args.engine and engine.name.lower() != args.engine.lower():
            continue
        ok, why = engine.available()
        if ok:
            engines.append(engine)
        else:
            print(f"{engine.name}: skipped. {why}")
    if not engines:
        sys.exit("No TTS engine is available." if not args.engine else f"{args.engine} isn't available.")

    todo = [(e, v) for e in engines for v in e.voices()]
    print(f'{len(todo)} clips of "{PREVIEW_LINE}"')
    made = failed = 0
    for i, (engine, voice) in enumerate(todo, 1):
        label = f"[{i}/{len(todo)}] {engine.name} · {voice}"
        if preview_path(engine.name, voice).is_file():
            print(f"{label}: already made")
            continue
        start = time.time()
        try:
            path = make_preview(engine, voice)
        except Exception as e:  # one bad sample shouldn't stop the others
            failed += 1
            print(f"{label}: failed: {e}", flush=True)
            continue
        made += 1
        print(f"{label}: {time.time() - start:.1f}s -> {path.name}", flush=True)
    print(f"Done: {made} new clip(s)" + (f", {failed} failed." if failed else "."))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
