# auto-cut

Audio-driven video automation for idol-event footage.

Two tools:

| Tool | Purpose |
|------|---------|
| **`idol_cut.py`** | Dual-system pipeline: sync one or many camera clips to the external-recorder `.wav`, split into one clip per song, watermark, hardware-encode. |
| `auto_cut.py` | Single-file beat/onset/silence/highlight cutter (one video, its own audio). |

**Multi-camera** — the WAV runs the whole show; cameras start/stop, so you give
several `.mov` files and each is synced independently. Every song is routed to
whichever clip covers it. **Hardware-encoded** by default (Apple VideoToolbox —
the Media Engine Final Cut Pro uses), ~5× faster than software libx264 on real
4K footage.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt          # needs ffmpeg + ffprobe on PATH
python audio_sync.py                      # self-test the sync math
python test_pipeline.py                   # full synthetic end-to-end test
```

---

## idol_cut.py — the dual-system pipeline

Your camera records video + a rough scratch audio. An external recorder captures
the good audio (`.wav`). They start at different times. This pipeline aligns
them (WAV is master), splits the show into songs (skipping MC / waiting), and
watermarks each clip.

Source files usually live on an **external SSD / SD card** under `/Volumes/...`.
That's handled — it reads in place and tells you clearly if the drive isn't
plugged in.

### Picking files at runtime

Every command takes file paths as flags **or** lets you pick them when you run
it — leave a flag off and a macOS Finder dialog opens (at your SSD by default).
Over SSH / no GUI it falls back to a numbered terminal menu (`--no-gui` forces it).

```bash
python idol_cut.py render -o output/show     # pops pickers for mov, wav, songs, watermark
python idol_cut.py detect                     # pops a picker for the wav
```

For the watermark, **Cancel = no watermark**. Pass `--no-watermark` to skip it
without being asked. If you omit `-o`, a **folder picker** opens at render time to
choose where the clips go (Cancel → `output/<wav filename>`).

### 1. Find your drive

```bash
python idol_cut.py volumes
```

### 2. Detect songs → editable list

```bash
python idol_cut.py detect "/Volumes/Johndyr SSD/show.wav" -o songs.json
```

Song detection is a **heuristic** (energy + beat-clarity). It will not be
perfect on a live recording. Open `songs.json`, fix `start`/`end`, delete
anything that isn't a song. Times can be written as **`M:SS`** or **`H:MM:SS`**
(e.g. `18:45` or `1:02:05`) — raw seconds still work too. Tuning knobs:

```bash
--min-song 60      # ignore music runs shorter than this
--merge-gap 12     # don't split a song across a short quiet bridge
--pulse-thresh 0.30   # lower = catches quieter/ballad songs (more false positives)
--energy-floor 25  # dB below the loudest moment that still counts as music
--features-csv feats.csv   # dump per-window features to tune against
```

### 3. Sync the cameras → sync.json

The WAV is continuous; cameras start/stop. Sync each clip to the WAV once:

```bash
python idol_cut.py sync --wav "/Volumes/Johndyr SSD/show.wav" \
  --mov "/Volumes/Johndyr SSD/camA1.mov" \
  --mov "/Volumes/Johndyr SSD/camA2.mov" \
  -o sync.json
```

This prints a coverage timeline (which clip covers which part of the show) and a
confidence per clip. (Leave `--mov` off to multi-select in a Finder dialog.)

### 4. Render one clip per song

```bash
python idol_cut.py render \
  --sync sync.json \
  --songs songs.json \
  --watermark logo.png \
  --prefix 2026-06-27_ \
  -o output/show
```

Each song is routed to the camera clip covering it, cut with the master WAV
audio + watermark, and **hardware-encoded** (H.264/AAC, web-ready). A
`manifest.json` records every clip, which camera it came from, and the offset used.

A live progress bar shows the real ffmpeg encode percentage per song (parsed from
ffmpeg `-progress`), plus an overall song count. `sync` and `detect` show bars too.

For a single camera you can skip `sync.json` and pass files directly:
`render --mov show.mov --wav show.wav --songs songs.json -o output/show`.

### Watermark sizing

- **`--wm-mode auto`** (default) — detects the watermark type from its dimensions.
- **fullframe** — your PNG is the same size as the video (logo placed inside a
  full-frame transparent canvas). Overlaid 1:1, not shrunk.
- **badge** — your PNG is just the logo. Scaled to `--wm-scale` (default 12% of
  video width) and dropped in `--wm-position` (tl/tr/bl/br/center).

Force it with `--wm-mode fullframe` / `--wm-mode badge` if auto guesses wrong.

### Encoding speed / quality

- **`--encoder hardware`** (default) — `h264_videotoolbox`, ~5× faster, like FCPX.
  Tune with `--quality 0..100` (default 62; higher = better/larger).
- **`--encoder software`** — `libx264 -crf 18`, slower but smaller files; use for
  an archival master.
- **`--lossless`** — stream-copy the original video (no re-encode, no watermark)
  for color grading in DaVinci Resolve. Watermark on export instead.

### Head/tail fade (cross-dissolve)

Each clip gets a 1 s fade in/out on video **and** audio by default, so songs don't
start/end abruptly. Tune with `--fade 0.5`, change the color with
`--fade-color white` (any ffmpeg color / `0xRRGGBB`), or turn off with `--no-fade`.
(Fades need re-encoding, so they're skipped in `--lossless` mode.)

### Two shooting modes

Fancam teams shoot the same show two ways. Pass `--mode` to handle each:

| Mode | Camera style | Extra output | Command |
|------|-------------|--------------|---------|
| **`overall`** | Wide, all members, rolls start→end incl. MC | `full_show.mp4` — whole take, nothing cut | `--mode overall` |
| **`focus`** | One member, stops between songs | `full_performance.mp4` — songs only, joined | `--mode focus` |
| **`songs`** (default) | Any | Per-song clips only | (omit `--mode`) |

Both modes still produce individual per-song clips. The extra file is additional.

#### overall → full_show.mp4

```bash
python idol_cut.py render --sync sync.json --songs songs.json \
  --watermark logo.png --mode overall -o output/overall
# -> output/overall/song01.mp4 ... + output/overall/full_show.mp4
```

`full_show.mp4` is the **whole take**: entrance → songs → MC → songs → exit.
Nothing is cut. The MC gaps are preserved.

Trim the head/tail (e.g. cut your pre-show chat off the top):
```bash
--full-start 0:30   --full-end 1:45:00   # timecode or raw seconds
```

Or use `--full` as a standalone flag without `--mode`.

**Multi-file (4GB/30-min card splits):** pass multiple `--mov` files to `sync`.
`render_full` stitches them greedily — at each point it picks the clip reaching
furthest, producing a seamless show. Splits inside the same camera are stream-
copied (instant); different cameras are scaled if resolutions differ.

Fades: only the **true entrance and exit** dip to color. Internal stitch seams
have no fade, so card-split joins are invisible.

#### focus → full_performance.mp4

```bash
python idol_cut.py render --sync sync.json --songs songs.json \
  --watermark logo.png --mode focus -o output/focus
# -> output/focus/song01.mp4 ... + output/focus/full_performance.mp4
```

`full_performance.mp4` is every song **joined in order** — MC gaps dropped, dip-
to-color transitions between songs (fade defaults to 1.5 s with `--mode focus`).
Uniform clips are stream-copied (instant).

Or use `--combine` as a standalone flag without `--mode`.

### Endscreen

Append a branded endscreen to `full_show.mp4` and `full_performance.mp4`. Works with both images (PNG/JPG → looped as video) and video clips:

```bash
# pick interactively when asked
python idol_cut.py render --mode overall --songs songs.json --mov cam.mov --wav show.wav -o out
# -> dialog: "เลือก endscreen" (Cancel = ไม่ใส่)

# or pass it directly
python idol_cut.py render --mode overall --endscreen endscreen.png --endscreen-duration 8 ...

# skip without being asked
python idol_cut.py render --mode focus --no-endscreen ...
```

The endscreen fades in from `--fade-color` (default black), continuing the tail-fade of the last clip seamlessly. Image endscreens default to 10 s; override with `--endscreen-duration`.

### One-shot

```bash
python idol_cut.py auto --wav show.wav --mov camA1.mov --mov camA2.mov \
  --watermark logo.png -o output/show
# syncs + detects songs, pauses for you to review songs.json, prints the render command
# add --yes to skip review and render straight away
```

## How sync works

1. **Coarse** — cross-correlate the two onset-strength envelopes (transients line
   up even across very different mics). Cheap on a 90-min show.
2. **Refine** — raw-waveform correlation in a loud window for < 5 ms accuracy.
3. **Per-song re-refine** (default) — re-estimates the offset around each song,
   cancelling camera/recorder **clock drift** over a long show. Disable with
   `--no-refine`.

Offset convention: `mov_time = wav_time + L`. Positive `L` = camera rolled first.

## Notes / limits

- Multiple cameras supported (each its own `.mov`). A song that spans a camera
  **gap** is rendered only for the part a camera covers (and warns).
- A song that spans a camera **boundary** is served by the single clip with the
  most coverage — it won't auto-splice across two files mid-song.
- Stereo WAV is preserved in the deliverable; only the analysis path downmixes.
- Slow SD card + many iterations? Add `--cache-local` to copy sources to local
  scratch first.

## Files

```
idol_cut.py       CLI: volumes / sync / detect / render / auto
media.py          external-volume resolution, ffprobe, audio extraction
audio_sync.py     dual-system offset estimation (+ self-test)
syncmap.py        multi-clip sync map + song->clip routing
song_splitter.py  song-boundary detection, editable songs.json
pick.py           interactive file selection (macOS dialog + terminal menu)
watermark.py      PNG overlay filtergraph (badge / fullframe)
render.py         per-song ffmpeg render (sync + cut + watermark + hw encode)
test_pipeline.py  synthetic end-to-end test
auto_cut.py       standalone single-file cutter
```

## Roadmap

Full requirements list (Phase 1, done) and the plan toward a GUI app are in
[ROADMAP.md](ROADMAP.md).

## Wiki

The hardware-encoding research that informed the speed fix is filed in the
sibling vault: `claude-obsidian/wiki/concepts/FFmpeg Hardware Encoding (Apple Silicon).md`.
