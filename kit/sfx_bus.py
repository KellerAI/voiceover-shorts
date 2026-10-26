#!/usr/bin/env python3
"""SFX bus: normalise a voice stem, level SFX under it, mix, and gate with preflight/loudness.

Targets are the loudness rules in preflight/loudness.py: voice -14 LUFS +-1,
mix true peak <= -1.5 dBTP, SFX sample peak >= 12 dB under the voice sample peak.
Defaults leave headroom: voice true-peak ceiling -3.0 dBTP and SFX 14 dB under,
so a coincident voice + SFX peak still stays under -1.5 dBTP on the mix.

Every output is 48 kHz stereo pcm_s24le. HyperFrames turns a mono <audio> source
into dual-mono (+3 LU), so stems are stereo.

Usage:
  sfx_bus.py --voice VOICE --sfx SFX [--sfx SFX2 ...] --out-dir DIR
             [--target-lufs -14] [--voice-tp -3] [--sfx-under-db 14] [--json]
Writes DIR/voice_norm.wav, DIR/sfx_bus.wav, DIR/mix.wav, then runs
preflight.loudness.check(mix, voice_norm, sfx_bus) on them.
Exit: 0 when that check passes, 1 when it fails, 2 on a usage or IO error.

Python API:
  build(voice, sfx, out_dir, *, target_lufs=-14.0, voice_tp=-3.0, sfx_under_db=14.0) -> dict
  normalize_voice(src, dst, *, target_lufs, voice_tp) -> dict
  level_sfx(srcs, dst, *, voice_peak_db, sfx_under_db) -> dict
  mix(voice, sfx, dst) -> str
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from preflight import loudness  # noqa: E402

DEFAULT_TARGET_LUFS = -14.0
DEFAULT_VOICE_TP = -3.0
DEFAULT_SFX_UNDER_DB = 14.0
OUT_ARGS = ["-ar", "48000", "-ac", "2", "-c:a", "pcm_s24le"]
LIMITER = "aresample=192000,alimiter=limit={lim:.5f}:attack=5:release=50:level=disabled,aresample=48000"


class SfxBusError(Exception):
    pass


def _run(args: list[str]) -> None:
    try:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-y", *args],
                           capture_output=True, text=True, check=False, timeout=900)
    except FileNotFoundError as e:
        raise SfxBusError("ffmpeg not found") from e
    except subprocess.TimeoutExpired as e:
        raise SfxBusError("ffmpeg timed out") from e
    if r.returncode != 0:
        raise SfxBusError(f"ffmpeg failed: {r.stderr.strip()[-400:]}")


def _measure(path: str) -> dict:
    try:
        return loudness.measure(path)
    except loudness.LoudnessError as e:
        raise SfxBusError(str(e)) from e


def _peak(path: str) -> float:
    try:
        return loudness.sample_peak(path)
    except loudness.LoudnessError as e:
        raise SfxBusError(str(e)) from e


def _need_file(p: str) -> str:
    if not Path(p).is_file():
        raise SfxBusError(f"not a file: {p}")
    return str(p)


def normalize_voice(src: str, dst: str, *, target_lufs: float = DEFAULT_TARGET_LUFS,
                    voice_tp: float = DEFAULT_VOICE_TP) -> dict:
    """Linear gain to target_lufs when the projected true peak stays under voice_tp.
    Otherwise: gain, then an oversampled limiter at voice_tp - 0.5 dB (the kit README
    limiter stage), re-measure and re-trim the gain. Bounded loop (6 passes)."""
    _need_file(src)
    m = _measure(src)
    if not math.isfinite(m["lufs"]):
        raise SfxBusError(f"voice is silent: {src}")
    gain = target_lufs - m["lufs"]
    if m["true_peak_dbtp"] + gain <= voice_tp - 0.1:
        _run(["-i", src, "-af", f"volume={gain:.4f}dB", *OUT_ARGS, dst])
        out = _measure(dst)
        if abs(out["lufs"] - target_lufs) <= 0.3 and out["true_peak_dbtp"] <= voice_tp:
            return {"path": dst, "gain_db": gain, "limiter": None, **out}
    limit = 10 ** ((voice_tp - 0.5) / 20)
    out = m  # loop below always runs (range(6)) and overwrites this; init keeps it bound
    for _ in range(6):
        af = f"volume={gain:.4f}dB,{LIMITER.format(lim=limit)}"
        _run(["-i", src, "-af", af, *OUT_ARGS, dst])
        out = _measure(dst)
        if abs(out["lufs"] - target_lufs) <= 0.3 and out["true_peak_dbtp"] <= voice_tp:
            return {"path": dst, "gain_db": gain, "limiter": limit, **out}
        if out["true_peak_dbtp"] > voice_tp:
            limit *= 10 ** (-0.5 / 20)
        gain += target_lufs - out["lufs"]
    raise SfxBusError(f"could not reach {target_lufs} LUFS under {voice_tp} dBTP "
                      f"(last {out['lufs']:.2f} LUFS, {out['true_peak_dbtp']:.2f} dBTP)")


def level_sfx(srcs: list[str], dst: str, *, voice_peak_db: float,
              sfx_under_db: float = DEFAULT_SFX_UNDER_DB) -> dict:
    """Sum the SFX stems (unity, no amix normalisation) and scale so the bus sample
    peak sits sfx_under_db below voice_peak_db."""
    for s in srcs:
        _need_file(s)
    raw = str(Path(dst).with_suffix(".raw.wav"))
    ins = sum((["-i", s] for s in srcs), [])
    af = (f"amix=inputs={len(srcs)}:duration=longest:normalize=0" if len(srcs) > 1 else "anull")
    _run([*ins, "-af", af, "-ar", "48000", "-ac", "2", "-c:a", "pcm_f32le", raw])
    peak = _peak(raw)
    if not math.isfinite(peak):
        raise SfxBusError("sfx bus is silent")
    gain = (voice_peak_db - sfx_under_db) - peak
    _run(["-i", raw, "-af", f"volume={gain:.4f}dB", *OUT_ARGS, dst])
    return {"path": dst, "gain_db": gain, "peak_db": _peak(dst)}


def mix(voice: str, sfx: str, dst: str) -> str:
    """Sum voice + sfx at unity (amix normalize=0), length of the longer stem."""
    _run(["-i", voice, "-i", sfx, "-filter_complex",
          "amix=inputs=2:duration=longest:normalize=0", *OUT_ARGS, dst])
    return dst


def build(voice: str, sfx: str | list[str], out_dir: str, *,
          target_lufs: float = DEFAULT_TARGET_LUFS, voice_tp: float = DEFAULT_VOICE_TP,
          sfx_under_db: float = DEFAULT_SFX_UNDER_DB) -> dict:
    srcs = [sfx] if isinstance(sfx, str) else list(sfx)
    if not srcs:
        raise SfxBusError("need at least one sfx stem")
    od = Path(out_dir)
    od.mkdir(parents=True, exist_ok=True)
    v = normalize_voice(voice, str(od / "voice_norm.wav"), target_lufs=target_lufs, voice_tp=voice_tp)
    vp = _peak(v["path"])
    s = level_sfx(srcs, str(od / "sfx_bus.wav"), voice_peak_db=vp, sfx_under_db=sfx_under_db)
    m = mix(v["path"], s["path"], str(od / "mix.wav"))
    try:
        res = loudness.check(mix=m, voice=v["path"], sfx=s["path"])
    except loudness.LoudnessError as e:
        raise SfxBusError(str(e)) from e
    return {"passed": res["passed"], "voice": v["path"], "sfx": s["path"], "mix": m,
            "voice_gain_db": v["gain_db"], "voice_limiter": v["limiter"],
            "sfx_gain_db": s["gain_db"], "settings": {"target_lufs": target_lufs,
            "voice_tp": voice_tp, "sfx_under_db": sfx_under_db}, "loudness": res}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--voice", required=True)
    p.add_argument("--sfx", action="append", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--target-lufs", type=float, default=DEFAULT_TARGET_LUFS)
    p.add_argument("--voice-tp", type=float, default=DEFAULT_VOICE_TP)
    p.add_argument("--sfx-under-db", type=float, default=DEFAULT_SFX_UNDER_DB)
    p.add_argument("--json", action="store_true")
    try:
        a = p.parse_args(argv)
    except SystemExit as e:
        return 0 if e.code == 0 else 2
    try:
        res = build(a.voice, a.sfx, a.out_dir, target_lufs=a.target_lufs,
                    voice_tp=a.voice_tp, sfx_under_db=a.sfx_under_db)
    except SfxBusError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(res, indent=2))
    else:
        print(f"sfx_bus: {'PASS' if res['passed'] else 'FAIL'} mix={res['mix']}")
        for f in res["loudness"]["findings"]:
            print(f"  {f['kind']}: {f['detail']}")
    return 0 if res["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
