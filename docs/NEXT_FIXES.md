# Fixes to do next (from the user's test on the Mac, 2026-11-05 evening)

The user tested branch `features-plus` and listed these. **Not done yet: wait for the user
to ask** ("tomorrow when I wake up, I can prompt you to fix it"). Their screenshots were in a
10-page PDF ("Screenshot 2026-11-05 at 9.22.56 PM"); what each page shows is noted below,
since the PDF itself isn't in the repo. Remember the user's standing rule: nothing on
GitHub or in the code may mention Claude (commits under `stevemacario`, no credit lines,
no "Claude" in branch names or messages).

## The list (the user's words first, then what it means in the code)

1. **Scroll by default.** "If the user opens a book for the first time (unless otherwise
   changed in settings) the display should appear in scroll mode." Today `page_view` is one
   app-wide setting, so after trying Single, every book opened in Single. → New books open
   in Scroll unless Settings says otherwise. *Ask:* should each book then remember its own
   page view, with Settings holding "page view for new books"?

2. **Turning pages by clicking the paper.** In single/double page, a click on the paper
   background left of the page(s) → previous page; right of them → next page.
   Their PDF p.7 shows Readest's Pagination settings for reference: Click to Paginate,
   Swipe to Paginate, Click Both Sides, Swap Click Sides, Disable Double Click, Show Page
   Navigation Buttons. *Ask* whether they want those toggles too (they didn't say).

3. **← / → turn pages** in single/double page ("make the app keyboard aware"). Today ← / →
   are −10 / +10 sec everywhere. *Ask:* in paged views, do the arrows turn pages only, and
   ±10 sec stays on the buttons?

4. **Font size in single/double page:** 15 pt by default, range 8–24 pt ("otherwise it looks
   strange"). PDF p.1 and p.3/p.5: Anna Karenina at 28 pt in Single page, far too big for
   the sheet.

5. **Paper background:** replace the generated texture (ui/paper.py `_texture`) with the
   user's image `/Users/stevemacario/Desktop/papyrus_page_background 2.png`. That file is on
   their Mac, not reachable from a cloud session: ask them to attach it in the chat or copy
   it into the repo (e.g. `assets/textures/papyrus.png`) and push. Tile or scale it to fill;
   tint it for the dark themes.

6. **Audio player only: one position line, not two.** "The player mode has two position
   lines (it should only have one)" (their pic 2; pic 3 = the reference look). Today
   PlayerPage has the chapter slider and the whole-book ChapterBar with its own marker.
   → Keep one bar. *Pics 2–3 weren't in the PDF:* ask for them, or ask which bar to keep
   (likely the single chapter bar of their first Audible-style screenshot, maybe with chapter
   ticks on it).

7. **Audio player only: a back arrow instead of "Show Text"**, at the top left. The **speed
   menu stays next to the player capsule** as in the reading window, not at the top right
   (their pics 4–5, also not in the PDF).

8. **Player page's "Chapters" button is barely visible** → style it like the reading
   window's toolbar buttons: the toolbar's blue background with white text.

9. **Reading toolbar** (PDF p.4: `Notes  Aa  Re-Generate Audio  Player`):
   - remove the **Notes** button for now;
   - **Player** → a headphones-only SVG icon, to the right of the book title;
   - **Aa** → to the right of the book title too (PDF p.8 shows the Aa button);
   - a **Settings** button on the toolbar, to the right of the title (opens the Settings
     pop-up);
   - rename **Contents** → **Chapters**.

10. **Markup mode** (a button like Apple's Markup icon, on the toolbar). Pressing it stops
    the audio, and the text stops being "click to play from here": the cursor is a normal
    text cursor for selecting, then highlight, **underline** (new), copy, add a note, etc.
    Pressing it again leaves Markup mode, and clicks guide the audio again. (Today selection
    already works in the normal mode, with a click delay so a double-click selects; in
    Markup mode no click should start audio. Underline needs a new annotation style.)

11. **Library page:**
    - remove the **Home** button: "once the user clicks Library, the Library page is Home
      Page." *Ask:* should the app then open on the Library (not the landing page) from the
      next launch on?
    - **☰ ▸ Settings… does nothing.** It must open the Settings pop-up (bug:
      `LibraryPage.settings_requested` isn't connected in `main_window._build_library_page`).

12. **Font size slider glitches.** Every change shows the spinning wait cursor (each step
    re-lays out and re-counts the whole book, e.g. Anna Karenina), and it only went from
    13 pt to 27 pt. → Apply the size when the slider is released (or after a short pause),
    count pages off the UI thread, and fix the range (with item 4's 8–24 pt in paged views).

13. **Point sizes look wrong.** The user compared with TextEdit (PDF p.10: Literata at 12, 13,
    14, 15, 18, 24, 36 and 48 pt). LivroSound's "28 pt" looks much bigger than TextEdit's
    28 pt. → Check how `theme.reading_font`'s point size and `_ReflowStyles.em` turn into
    pixels on a Retina Mac, so "N pt" matches TextEdit's N pt at 100% zoom.

## Also seen in the PDF (not in the user's list: mention, don't change unasked)

- PDF p.1 and p.9: **Cancel Generation** sits on top of the page number at the bottom right
  of single page ("4335", "3888" are half hidden behind it).
- PDF p.2: the Aa popover (Override book layout, Pages Scroll/Single/Double, Highlight while
  reading, Word/Sentence/Paragraph, More Settings…) works as built.
