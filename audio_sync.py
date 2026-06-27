"""
Dual-system sound sync.

The camera (.mov) records a low-quality scratch audio track. An external recorder
(.wav) captures the good audio. Both hear the same performance but start at
different times. We find that time offset by cross-correlation, so the clean WAV
can replace the camera audio.

Convention
----------
We return an offset `L` (seconds) meaning:

    a feature at WAV-time `u`  appears at  MOV-time `u + L`

i.e. mov_time = wav_time + L. Equivalently L = wav_start - mov_start.
A positive L means the camera started rolling *before* the recorder.

Method
------
1. Coarse: cross-correlate the two *onset-strength envelopes* (energy flux over
   time). Robust to different mics/EQ because it keys on transients (beats,
   claps), and cheap even for a 90-min show (~43 values/sec).
2. Refine: raw-waveform cross-correlation in a short high-energy window around
   the coarse estimate, for sample-accurate offset (< ~5 ms).
"""

from __future__ import annotations

from dataclasses import dataclass

import librosa
import numpy as np
import scipy.signal

EPS = 1e-9
DEFAULT_SR = 22050
DEFAULT_HOP = 512


@dataclass
class SyncResult:
    offset: float        # L, seconds (mov_time = wav_time + L)
    confidence: float    # 0..1 normalized correlation peak
    refined: bool

    @property
    def reliable(self) -> bool:
        return self.confidence >= 0.30


# ── building blocks (operate on numpy arrays; unit-testable) ─────────────────

def onset_env(y: np.ndarray, sr: int, hop: int = DEFAULT_HOP) -> np.ndarray:
    return librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)


def _normalize(x: np.ndarray) -> np.ndarray:
    x = x - x.mean()
    n = np.linalg.norm(x)
    return x / (n + EPS)


def coarse_offset(scratch: np.ndarray, wav: np.ndarray, sr: int,
                  hop: int = DEFAULT_HOP) -> tuple[float, float]:
    """
    Cross-correlate onset envelopes. Returns (offset_seconds, confidence).
    `scratch` = MOV scratch audio, `wav` = external recorder audio.
    """
    e_scratch = _normalize(onset_env(scratch, sr, hop))
    e_wav = _normalize(onset_env(wav, sr, hop))

    corr = scipy.signal.correlate(e_scratch, e_wav, mode="full", method="fft")
    lags = scipy.signal.correlation_lags(len(e_scratch), len(e_wav), mode="full")

    k = int(np.argmax(corr))
    lag_frames = int(lags[k])
    confidence = float(np.clip(corr[k], 0.0, 1.0))  # already normalized vectors
    offset_sec = lag_frames * hop / sr
    return offset_sec, confidence


def refine_offset(scratch: np.ndarray, wav: np.ndarray, sr: int,
                  coarse_L: float, win: float = 20.0,
                  max_resid: float = 1.0,
                  focus: tuple[float, float] | None = None) -> float:
    """
    Sample-accurate refinement. Picks a high-energy window of the WAV that the
    MOV also covers (given the coarse offset), raw-correlates, and corrects the
    residual lag (searched within ±max_resid seconds).

    focus : optional (lo_sec, hi_sec) WAV-time region to refine within — used for
            per-song drift correction so the window sits near that song.
    """
    # Short-term energy of the WAV to find a transient-rich window.
    frame = int(0.1 * sr)
    rms = np.sqrt(np.convolve(wav.astype(np.float64) ** 2,
                              np.ones(frame) / frame, mode="same"))

    win_n = int(win * sr)
    lo = max(0, int(-coarse_L * sr))                       # WAV idx where MOV starts
    hi = min(len(wav), len(scratch) - int(coarse_L * sr))  # WAV idx where MOV ends
    if focus:
        lo = max(lo, int(focus[0] * sr))
        hi = min(hi, int(focus[1] * sr))
    lo = max(lo, 0)
    hi = min(hi, len(wav) - win_n)
    if hi <= lo:
        return coarse_L  # no usable overlap; keep coarse

    # center the window on the most energetic usable instant
    center = lo + int(np.argmax(rms[lo:hi + win_n])) if hi + win_n <= len(rms) else lo
    u0 = int(np.clip(center - win_n // 2, lo, hi))

    wav_slice = wav[u0:u0 + win_n]
    m0 = u0 + int(round(coarse_L * sr))
    pad = int(max_resid * sr)
    a, b = max(0, m0 - pad), min(len(scratch), m0 + win_n + pad)
    scratch_slice = scratch[a:b]
    if len(scratch_slice) < win_n or len(wav_slice) < win_n:
        return coarse_L

    corr = scipy.signal.correlate(_normalize(scratch_slice), _normalize(wav_slice),
                                  mode="full", method="fft")
    lags = scipy.signal.correlation_lags(len(scratch_slice), len(wav_slice), mode="full")
    # residual lag relative to where we expected the slice to line up
    expected = m0 - a
    search = np.abs(lags - expected) <= pad
    corr_masked = np.where(search, corr, -np.inf)
    k = int(np.argmax(corr_masked))
    resid_frames = int(lags[k]) - expected
    return coarse_L + resid_frames / sr


# ── high-level entry point ───────────────────────────────────────────────────

def estimate_offset(scratch_wav_path: str, master_wav_path: str,
                    sr: int = DEFAULT_SR, refine: bool = True,
                    win: float = 20.0) -> SyncResult:
    """
    Load a MOV-derived scratch WAV and the master WAV, return the sync offset.
    (Caller extracts the scratch track from the .mov first via media.extract_audio.)
    """
    scratch, _ = librosa.load(scratch_wav_path, sr=sr, mono=True)
    master, _ = librosa.load(master_wav_path, sr=sr, mono=True)

    L, conf = coarse_offset(scratch, master, sr)
    refined = False
    if refine and conf >= 0.15:
        L = refine_offset(scratch, master, sr, L, win=win)
        refined = True
    return SyncResult(offset=L, confidence=conf, refined=refined)


# ── self-test (no real media needed) ─────────────────────────────────────────

def _self_test() -> bool:
    """
    Generate a synthetic 'recorder' signal and a delayed, noisy, differently-EQ'd
    'camera scratch' copy, then verify we recover the known delay. Proves the
    offset sign convention and accuracy.
    """
    rng = np.random.default_rng(0)
    sr = DEFAULT_SR
    dur = 60.0
    n = int(dur * sr)
    t = np.arange(n) / sr

    # master: tone bed + sharp transients (claps/beats)
    master = 0.2 * np.sin(2 * np.pi * 220 * t)
    for onset in rng.uniform(2, dur - 2, size=120):
        i = int(onset * sr)
        master[i:i + 200] += np.hanning(200) * rng.uniform(0.5, 1.0)
    master /= np.max(np.abs(master))

    true_delay = 1.234  # camera started 1.234 s BEFORE recorder -> L should be +1.234
    d = int(true_delay * sr)
    scratch = np.zeros_like(master)
    scratch[d:] = master[:n - d]                  # scratch(t) = master(t - delay)
    scratch = scipy.signal.lfilter([1, -0.7], [1], scratch)  # different EQ
    scratch += 0.05 * rng.standard_normal(n)       # mic noise
    scratch *= 0.4                                  # different gain

    L, conf = coarse_offset(scratch, master, sr)
    L = refine_offset(scratch, master, sr, L)
    err_ms = abs(L - true_delay) * 1000
    ok = err_ms < 20 and conf > 0.2
    print(f"[self-test] true={true_delay:.3f}s detected={L:.3f}s "
          f"err={err_ms:.1f}ms confidence={conf:.2f} -> {'PASS' if ok else 'FAIL'}")
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if _self_test() else 1)
