# Narrator voices

These are your own cloned voices, and the only ones LivroSound ever offers
(Pocket-TTS's built-in voices are never listed). Each voice is two files with
the same name, plus a line in `voices.json`:

| File | What it is |
| --- | --- |
| `NAME.wav` | 5–20 seconds of one person speaking: a clean, mono recording in a quiet room, no music |
| `NAME.wav.txt` | the exact words spoken in `NAME.wav`, as plain text. Pocket-TTS clones from the audio alone, but keep the transcript with it: engines that clone from text too need it, and a changed transcript makes a fresh ▶ preview |
| `voices.json` | `"NAME": {"gender": "female", "age": "adult"}` — groups the voice under **Women** or **Men** in Choose a Voice, ordered young → adult → mature |

The file name (minus `.wav`) is the voice's name everywhere in the app, so use
a first name: `Maria.wav` + `Maria.wav.txt` shows up as **Maria**.

## Adding a voice

1. Drop `NAME.wav` and `NAME.wav.txt` into this folder.
2. Add a line to `voices.json`, for example
   `"Maria": {"gender": "female", "age": "mature"}`.
   `gender` is `female` or `male`; `age` is `young`, `adult` or `mature`.
   A voice without a line still works: it's listed under **Other**.
3. Restart LivroSound. The ▶ preview is made the first time you open Choose a
   Voice, or all at once with `uv run python scripts/make_voice_previews.py`.

Don't worry about the recording's volume: every paragraph LivroSound makes is
leveled to the same loudness (−19 LUFS), so a quiet sample and a loud one
sound alike. Pocket-TTS copies the room and microphone along with the voice,
though, so a noisy sample gives a noisy narrator. Line breaks in the
transcript don't matter (they're read as spaces). Re-recording a voice
(replacing its `.wav`) makes its preview again automatically.

## Where these voices came from

They are the voices you cloned for Script Radio, renamed to first names. The
`source` field in `voices.json` keeps each one's old Script Radio file name:

| Voice | Was | | Voice | Was |
| --- | --- | --- | --- | --- |
| **Aaron** (default) | Default_Narrator_Male | | **Johnny** | Johnny_Male_Adult_Southern |
| **Ana** | Default_Narrator_Female | | **Kyle** | Kyle_Male_Young_Rebellious |
| **Anne** | Anne_Female_Casual_Conversational | | **Liz** | Liz_Female_Young_Aloof_Bratty |
| **Connor** | Connor_Male_Adult_Aussie | | **Matthew** | Matthew_Male_Southern_Pensive |
| **Danny** | Danny_Male_Young_Anxious | | **Ronald** | Ronald_Male_Mature_Imposing |
| **Emily** | Emily_Female_Young_Stoic | | **Ruth** | Ruth_female_young_insecure |
| **Ethan** | Ethan_Male_Manly_Rugged | | **Susan** | Susan_Female_voice |
| **Gertrude** | Gertrude_Female_Mature_Wise | | **Therese** | Therese_Female_Teacher_explainer_Patient |
| **Gigi** | Gigi_Female_Young_Valley_Girl | | | |

These recordings are committed with the code, so only add voices you have the
right to use.
