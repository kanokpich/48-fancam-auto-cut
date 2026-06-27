# auto-cut — Project Requirements & Roadmap

Planning artifact for the idol-event video automation pipeline. Captures what's
built (Phase 1), why, and where it goes next — including a path to a real app.

Owner: John (037M2). Stack today: Python 3 + ffmpeg, macOS / Apple Silicon.

---

## Vision

Turn a raw idol-event shoot — long continuous audio from an external recorder +
camera clips that start/stop — into clean, per-song, watermarked, ready-to-post
videos with as little manual work as possible. Eventually a GUI app a
photographer can run without touching a terminal.

---

## Phase 1 — Delivered (CLI)

Status legend: ✅ done · 🟡 partial · ⬜ not started

### Core pipeline
| # | Requirement | Status | Where |
|---|-------------|--------|-------|
| 1 | Sync camera `.mov` to external `.wav` (WAV is master) | ✅ | `audio_sync.py` |
| 2 | Robust sync: onset-envelope coarse + raw-waveform refine (<5 ms) | ✅ | `audio_sync.py` |
| 3 | Per-song drift re-refinement over a long show | ✅ | `audio_sync.py`, `render.py` |
| 4 | **Multi-camera**: many clips, each its own offset; song→clip routing | ✅ | `syncmap.py` |
| 5 | Split show into songs (skip MC / waiting) — energy + beat-clarity | ✅ | `song_splitter.py` |
| 6 | Song boundaries are an **editable `songs.json`** (human-in-the-loop) | ✅ | `song_splitter.py` |
| 7 | Times in `songs.json` as `M:SS` / `H:MM:SS` (+ raw seconds) | ✅ | `song_splitter.py` |
| 8 | PNG watermark — badge (corner) **and** full-frame, auto-detected | ✅ | `watermark.py` |
| 9 | Cross-dissolve: fade in/out (video + audio) head/tail, to any color | ✅ | `render.py` |
| 10 | **Hardware encoding** (VideoToolbox, ~5× faster, like FCPX) | ✅ | `render.py` |
| 11 | Output modes: deliverable (H.264) · software (x264) · lossless (copy) | ✅ | `render.py` |
| 11a | **Combine** all songs → `full_performance.mp4` (dip-to-color joins) | ✅ | `render.py` |

---

## Phase 2 — Delivered (shooting modes)

| # | Requirement | Status | Where |
|---|-------------|--------|-------|
| P2-1 | `--mode overall` — per-song + `full_show.mp4` (whole take, MC kept) | ✅ | `render.py`, `idol_cut.py` |
| P2-2 | `--mode focus` — per-song + `full_performance.mp4` (songs only, joined) | ✅ | `render.py`, `idol_cut.py` |
| P2-3 | `render_full`: entrance→exit, MC gaps included | ✅ | `render.py` |
| P2-4 | Multi-file stitching (4GB/30-min card splits) — greedy coverage walk | ✅ | `render.py` |
| P2-5 | Trim full show: `--full-start` / `--full-end` (timecode) | ✅ | `idol_cut.py` |
| P2-6 | Fade sides per segment: entrance/exit dip, seam joins invisible | ✅ | `render.py` |
| P2-7 | `full_show.mp4` > `full_performance.mp4` (MC gap present) — tested | ✅ | `test_pipeline.py` |

### Workflow & UX
| # | Requirement | Status | Where |
|---|-------------|--------|-------|
| 12 | Sources on external SSD / SD card (`/Volumes`), friendly errors | ✅ | `media.py` |
| 13 | Optional local cache for slow cards (`--cache-local`) | ✅ | `media.py` |
| 14 | Interactive file pickers (macOS dialog + terminal menu fallback) | ✅ | `pick.py` |
| 15 | Multi-select clips; **choose output folder at runtime** | ✅ | `pick.py` |
| 16 | Live progress bars: sync / detect / render (real ffmpeg %) | ✅ | `idol_cut.py`, `render.py` |
| 17 | `manifest.json` per run (clip used, offset, status) | ✅ | `render.py` |
| 18 | Standalone single-file cutter (beat/onset/silence/highlight) | ✅ | `auto_cut.py` |

### Quality
| # | Requirement | Status | Where |
|---|-------------|--------|-------|
| 19 | Sync self-test (synthetic, known offset) | ✅ | `audio_sync.py` |
| 20 | End-to-end synthetic test (sync→detect→route→render, both encoders) | ✅ | `test_pipeline.py` |
| 21 | Research filed (hardware encoding) for reuse | ✅ | wiki: *FFmpeg Hardware Encoding (Apple Silicon)* |

---

## Architecture (current)

```
                 ┌─────────────┐
  .wav (master) ─┤ song_splitter├─→ songs.json (editable, timecode)
                 └─────────────┘
  .mov × N ──┐   ┌─────────────┐
             ├──→│  syncmap    │──→ sync.json (per-clip offset + coverage)
  .wav ──────┘   │ (audio_sync)│
                 └─────────────┘
                                   ┌──────────┐
  sync.json + songs.json + logo ──→│  render  │──→ output/*.mp4 + manifest.json
                                   │  (ffmpeg)│
                                   └──────────┘
```

Module responsibilities:
- `media.py` — volume resolution, ffprobe, audio extraction. The only place that
  touches `/Volumes` and external media.
- `audio_sync.py` — pure offset math (numpy/scipy); unit-testable, UI-free.
- `syncmap.py` — multi-clip model: `ClipSync`, `build_sync_map`, `best_clip`.
- `song_splitter.py` — detection + `songs.json` I/O + timecode parse/format.
- `watermark.py` — ffmpeg overlay filtergraph (badge/fullframe).
- `render.py` — ffmpeg command assembly (sync clamp + watermark + fade + encode),
  UI-agnostic via callbacks (`on_song_begin/tick/progress`).
- `pick.py` — interactive selection (files/folder), macOS dialog + terminal menu.
- `idol_cut.py` — CLI: `volumes / sync / detect / render / auto`; owns rich UI.

Key design rules that should survive into an app:
- **Detection proposes, human disposes.** `songs.json` is the contract; never
  fully auto. A GUI should make editing it visual, not remove the review step.
- **`render.py` knows no UI.** It takes callbacks. Swap the CLI's rich bars for a
  GUI progress view without touching render logic.
- **Master audio is the single source of truth** for the timeline; video is hung
  off it via offsets. Everything maps to WAV time.

---

## Key decisions & rationale

- **Onset-envelope cross-correlation for sync** — robust across very different
  mics (camera vs recorder) because it keys on transients, and cheap on 90-min
  shows. Raw-waveform refine gives sample accuracy.
- **Hardware encode default** — measured ~5.4× faster than libx264-medium on real
  4K (0.25× → 1.3× realtime). Same Media Engine FCPX uses. Software/lossless kept
  for archival/grading. (See wiki page for the benchmark.)
- **Full-frame watermark auto-detect** — photographers export full-frame PNG
  overlays; shrinking them to a corner badge was wrong. Compare PNG dims to video.
- **Per-song clip routing by coverage** — cameras stop/start; the WAV is
  continuous; route each song to the clip that covers it.

---

## Known limitations (Phase 1)

- A song spanning a camera **gap** renders only the covered part (and warns).
- A song spanning a camera **boundary** is served by the single best-covering
  clip — no auto-splice across two files mid-song yet.
- Song detection is heuristic; ballads / long MC hype need manual `songs.json`
  edits or threshold tuning.
- Single audio source (one WAV). No multi-recorder merge.
- No reframing/aspect conversion (single output aspect = source).

---

## Phase 3+ — Roadmap toward an app

### Phase 3 — Power features (still CLI/script)
- ⬜ **Splice a song across camera files** — concatenate the covering clips with a
  crossfade at the seam (handles camera stop mid-song).
- ⬜ **Multi-aspect export presets** — one render → 16:9 (YouTube), 9:16 (Reels/
  Shorts), 1:1 (IG) with smart center/face-aware crop.
- ⬜ **Loudness normalization** — `-14 LUFS` (YouTube) / `-16` (IG) via `loudnorm`.
- ⬜ **Auto color** — apply a LUT / teal-orange grade per the color-grading wiki.
- ⬜ **Setlist import** — map song titles to `label`s (filenames become real names).
- ⬜ **Batch queue** — multiple shows / cameras, background render, resume.
- ⬜ **Title cards / lower-thirds** — song name + date intro per clip.

### Phase 4 — App (no terminal)
- ⬜ **GUI** — Mac-native (SwiftUI) or Electron/Tauri wrapping the Python core
  (or a port). The CLI's callback seams (`render.py`) make this a thin layer.
- ⬜ **Visual song editor** — waveform + thumbnails; drag song in/out points
  instead of editing `songs.json` by hand. This is the highest-value GUI piece.
- ⬜ **Multicam angle switcher** — beat-synced cuts between angles within a song.
- ⬜ **Project files** — save/reopen a show (sources + sync + songs + settings).
- ⬜ **One-click presets** — "037M2 YouTube", "037M2 Reels" bundling encoder +
  watermark + aspect + LUF + grade.

### Phase 5 — Scale / polish
- ⬜ ML music/speech segmentation (replace the energy+pulse heuristic).
- ⬜ Cloud / remote render for long shows.
- ⬜ Auto-upload (YouTube/Drive) with metadata from the setlist.

### Suggested next step
**Phase 3 "multi-aspect export presets"** — one render → 16:9 / 9:16 / 1:1 from
the same show, reusing the existing render path with a crop/scale stage. Highest
leverage for posting to multiple platforms (YouTube / Reels / IG Square) without
re-shooting.

---

## Porting notes (if the core is rewritten for an app)

- The sync math (`audio_sync.py`) and routing (`syncmap.py`) are pure and portable
  (numpy/scipy) — keep them as the engine; only the UI changes.
- All heavy lifting is shelling out to `ffmpeg`/`ffprobe`; any language can drive
  the same commands. The non-trivial IP is: the offset estimation, the song-→clip
  routing, the filtergraph assembly (watermark mode + fade + sync clamp), and the
  VideoToolbox flags.
- Keep `songs.json` / `sync.json` / `manifest.json` as the stable interchange
  formats — a GUI reads/writes the same files the CLI does.
