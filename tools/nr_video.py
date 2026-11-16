"""Run DLSSNR-AMD neural rendering over a video via raw RGBA8 pipes.

    python3 tools/nr_video.py samples/input.mp4 -o outputs/video.mp4
    python3 tools/nr_video.py samples/input.mp4 -o outputs/video.mp4 --no-temporal
    python3 tools/nr_video.py samples/input.mp4 -o outputs/video.mp4 --codec h264_amf

Requires ffmpeg/ffprobe on PATH and build/nr_stream.exe (tools/build_stream.py).
Stderr from the stream host goes to <output>.nr_stream.log so the three-process
pipe cannot deadlock on a full log pipe.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys

import _proc  # hidden-window spawns (see _proc.py)

try:  # frozen exe: the project root is next to the exe, not the bundle
    from _paths import project_root as _project_root
    ROOT = _project_root()
except Exception:
    ROOT = pathlib.Path(__file__).resolve().parents[1]
try:  # packaged builds ship ffmpeg next to the exe instead of on PATH
    from _tools import tool as _tool
except Exception:  # pragma: no cover - source checkout without tools/ on sys.path
    def _tool(name: str) -> str:
        return name

STREAM = ROOT / "build/nr_stream.exe"
FFMPEG = _tool("ffmpeg")
FFPROBE = _tool("ffprobe")


def _ffprobe(args: list[str], path: str) -> dict:
    """ffprobe -> parsed JSON, with a message a user can act on.

    Letting CalledProcessError escape printed a full Python traceback as the
    job's error text, which is useless in the GUI's note column.
    """
    r = _proc.run([FFPROBE, "-v", "error", *args, "-of", "json", path],
                  capture_output=True, text=True, errors="replace")
    if r.returncode != 0:
        lines = [ln.strip() for ln in (r.stderr or "").splitlines() if ln.strip()]
        detail = lines[-1] if lines else f"ffprobe exit code {r.returncode}"
        raise RuntimeError(f"cannot read media info (missing or corrupt file): {path} -- {detail}")
    try:
        return json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        raise RuntimeError(f"cannot parse media info: {path}")


def probe(path: str) -> dict:
    data = _ffprobe(["-select_streams", "v:0",
                     "-show_entries",
                     "stream=width,height,r_frame_rate,avg_frame_rate,nb_frames,pix_fmt,duration"], path)
    streams = data.get("streams") or []
    if not streams:
        raise RuntimeError(f"no video stream in {path}")
    return streams[0]


def ratio(text: str | None) -> float:
    """"24/1" -> 24.0, "528/35" -> 15.0857; 0.0 when unusable."""
    try:
        num, _, den = str(text).partition("/")
        d = float(den) if den else 1.0
        return float(num) / d if d else 0.0
    except (TypeError, ValueError):
        return 0.0


def media_duration(path: str) -> float:
    """Container duration; falls back to the video stream's own duration."""
    fmt = _ffprobe(["-show_entries", "format=duration"], path).get("format", {})
    try:
        return float(fmt.get("duration"))
    except (TypeError, ValueError):
        return 0.0


def main() -> int:
    """Entry point for both direct runs and the exe's self-dispatch.

    Media errors become one readable line; letting the exception escape printed
    a traceback that ended up as the job's error text in the GUI.
    """
    try:
        return run()
    except (RuntimeError, OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def run() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("--root", default=str(ROOT / "runtime"))
    ap.add_argument("--model", default="small", choices=["small", "original", "auto"])
    ap.add_argument("--intensity", type=float, default=1.0)
    ap.add_argument("--style", type=int, default=0)
    ap.add_argument("--passes", type=int, default=1)
    ap.add_argument("--local-tone", type=float, default=1.0)
    ap.add_argument("--local-structure", type=float, default=1.0)
    ap.add_argument("--skin-structure", type=float, default=None)
    ap.add_argument("--no-automask", action="store_true")
    ap.add_argument("--no-temporal", action="store_true")
    ap.add_argument("--noise-every-frame", action="store_true",
                    help="advance the NR noise seed per frame (the DLL's video behaviour)")
    ap.add_argument("--max-frames", type=int, default=0)
    ap.add_argument("--max-seconds", type=float, default=0.0,
                    help="only encode the first N seconds of the clip (0 = whole clip)")
    ap.add_argument("--no-pipeline", action="store_true",
                    help="use the serial I/O loop (A/B switch for the reader/writer threads)")
    ap.add_argument("--progress-every", type=int, default=0,
                    help="write a progress line every N frames (0 = about 200 per clip); "
                         "the GUI's progress bar reads them from the stream log")
    ap.add_argument("--codec", default="libx264",
                    help="libx264 | h264_amf | hevc_amf | av1_amf | auto "
                         "(auto = AMF at 4K and above, libx264 below)")
    ap.add_argument("--crf", type=int, default=18)
    ap.add_argument("--no-audio", action="store_true")
    ap.add_argument("--aco", action="store_true",
                    help="use the package's precompiled ACO machine code instead of LLPC")
    ap.add_argument("--batch", type=int, default=1,
                    help="frames recorded per submit (1 = one submit per frame)")
    ap.add_argument("--rgba8", action="store_true",
                    help="feed RGBA8 straight to the runtime (4x less staging traffic, fewer CPU conversions)")
    ap.add_argument("--vfr", choices=["cfr", "as-is"], default="cfr",
                    help="variable-frame-rate input: 'cfr' resamples to r_frame_rate so duration and "
                         "audio stay correct (default); 'as-is' keeps frames but collapses timing to r_frame_rate")
    a = ap.parse_args()

    if not STREAM.exists():
        print("missing build/nr_stream.exe; run: python3 tools/build_core.py && python3 tools/build_stream.py")
        return 2
    info = probe(a.input)
    w, h = int(info["width"]), int(info["height"])
    if w % 2 or h % 2:
        print(f"error: {w}x{h} has an odd side; yuv420p H.264 needs even dimensions.")
        print(f"  pad first: ffmpeg -i <in> -vf \"pad=ceil(iw/2)*2:ceil(ih/2)*2\" -c:v libx264 <even.mp4>")
        return 2
    rate = info.get("r_frame_rate", "24/1")
    # A seconds limit is turned into both a decoder -t (stops reading early, so a
    # long source is not decoded for nothing) and an explicit frame count.
    if a.max_seconds and a.max_seconds > 0:
        nominal_rate = ratio(rate) or 24.0
        a.max_frames = max(1, int(round(a.max_seconds * nominal_rate)))

    frames = info.get("nb_frames", "?")
    print(f"input: {a.input}  {w}x{h}  {rate} fps  {frames} frames")

    # Variable frame rate: the rawvideo pipe carries pixels only, so the encoder
    # has no timestamps to work with. Encoding at r_frame_rate would silently
    # shorten the clip (frames / nominal_rate != real duration), so resample to
    # a constant rate first and keep the original duration and audio alignment.
    nominal = ratio(info.get("r_frame_rate"))
    average = ratio(info.get("avg_frame_rate"))
    if not average:
        dur = float(info.get("duration") or 0)
        average = (float(frames) / dur) if (dur and str(frames).isdigit()) else nominal
    in_duration = media_duration(a.input)
    vfr = nominal > 0 and average > 0 and abs(average - nominal) / nominal > 0.01
    resample = vfr and a.vfr == "cfr"
    if vfr:
        print(f"variable frame rate detected: nominal {nominal:.3f} fps, average {average:.3f} fps "
              f"({in_duration:.3f}s)")
        if resample:
            print(f"  -> resampling to a constant {nominal:.3f} fps (duration and audio preserved); "
                  f"pass --vfr as-is to keep the original frames instead")
        else:
            print(f"  -> --vfr as-is: frames are kept but timing is forced to {nominal:.3f} fps; "
                  f"duration will differ from the source")

    # AMF is much faster at 4K (36% end to end) but its fixed-QP rate control
    # bloats the file at lower resolutions, where libx264 is barely slower and
    # far more compact. "auto" picks per-frame-size so the default is sane at
    # both ends instead of one.
    codec = a.codec
    if codec == "auto":
        codec = "h264_amf" if w * h >= 3840 * 2160 else "libx264"
        print(f"codec auto -> {codec} ({w}x{h})")

    # The stream host prints a status line every N frames; upstream's 30 is far
    # too coarse for a short clip (48 frames == four updates), so ask for roughly
    # 200 updates and let the GUI's progress bar follow the log.
    stream_duration = float(info.get("duration") or 0)
    clip_duration = stream_duration if stream_duration > 0 else in_duration
    frames_hint = a.max_frames or int(round(clip_duration * nominal))
    progress_every = a.progress_every or max(1, frames_hint // 200)

    out = pathlib.Path(a.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    log_path = out.with_suffix(out.suffix + ".nr_stream.log")

    decode = [FFMPEG, "-v", "error"]
    if a.max_seconds and a.max_seconds > 0:
        decode += ["-t", f"{a.max_seconds:.3f}"]
    decode += ["-i", a.input]
    if resample:
        # Pin the frame count to round(video_duration * rate): the fps filter
        # fills up to the end of the stream's time base and can emit a few
        # extra frames, which would lengthen the clip just as badly.
        video_duration = float(info.get("duration") or 0) or in_duration
        want = max(1, round(video_duration * nominal))
        decode += ["-vf", f"fps={rate}", "-fps_mode", "cfr", "-frames:v", str(want)]
        print(f"  -> {want} frames at {nominal:.3f} fps = {want / nominal:.3f}s")
    else:
        decode += ["-fps_mode", "passthrough"]
    decode += ["-f", "rawvideo", "-pix_fmt", "rgba", "-"]
    stream = [str(STREAM), "--width", str(w), "--height", str(h), "--root", a.root,
              "--model", a.model, "--intensity", str(a.intensity), "--style", str(a.style),
              "--passes", str(a.passes), "--local-tone", str(a.local_tone),
              "--local-structure", str(a.local_structure)]
    if a.skin_structure is not None:
        stream += ["--skin-structure", str(a.skin_structure)]
    stream += ["--progress-every", str(progress_every)]
    if a.no_pipeline:
        stream.append("--no-pipeline")
    if a.no_automask:
        stream.append("--no-automask")
    if a.no_temporal:
        stream.append("--no-temporal")
    if a.noise_every_frame:
        stream.append("--noise-every-frame")
    if a.aco:
        stream.append("--aco")
    if a.batch > 1:
        stream += ["--batch", str(a.batch)]
    else:
        stream += ["--batch", "1"]
    if a.rgba8:
        stream.append("--rgba8")
    if a.max_frames:
        stream += ["--max-frames", str(a.max_frames)]
    # The rawvideo pipe carries no audio, so map it from the original file as a
    # second input; -map 1:a? keeps clips without an audio stream working.
    encode = [FFMPEG, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgba",
              "-s", f"{w}x{h}", "-r", rate, "-i", "-"]
    if not a.no_audio:
        encode += ["-i", a.input]
    encode += ["-map", "0:v:0"]
    if not a.no_audio:
        encode += ["-map", "1:a?", "-c:a", "copy"]
        # A full clip must keep every video frame: the audio track is often a
        # few milliseconds shorter and -shortest would silently trim frames.
        # Only trim when the clip was deliberately shortened.
        if a.max_frames or (a.max_seconds and a.max_seconds > 0):
            encode.append("-shortest")
    encode += ["-c:v", codec]
    if codec == "libx264":
        encode += ["-crf", str(a.crf)]
    elif codec.endswith("_amf"):
        # The AMF encoders ignore -crf and would fall back to a default bitrate
        # (visibly soft at 4K), so map the quality knob onto constant QP.
        qp = max(0, min(51, a.crf))
        encode += ["-rc", "cqp", "-qp_i", str(qp), "-qp_p", str(qp), "-qp_b", str(qp),
                   "-quality", "quality"]
    pix_fmt = "nv12" if codec.endswith("_amf") else "yuv420p"
    encode += ["-pix_fmt", pix_fmt, str(out)]

    print(f"temporal: {'off' if a.no_temporal else 'on'}   "
          f"noise-every-frame: {'on' if a.noise_every_frame else 'off'}   "
          f"codec: {codec} (qp/crf {a.crf})")
    with open(log_path, "wb") as log:
        # Decoder stderr goes to the same log: on Windows its pipe-cut notices
        # are expected noise with --max-frames and should not hit the console.
        p1 = _proc.popen(decode, stdout=subprocess.PIPE, stderr=log,
                         stdin=subprocess.DEVNULL)
        p2 = _proc.popen(stream, stdin=p1.stdout, stdout=subprocess.PIPE, stderr=log)
        p1.stdout.close()
        p3 = _proc.popen(encode, stdin=p2.stdout)
        p2.stdout.close()
        rc3 = p3.wait()
        rc2 = p2.wait()
        rc1 = p1.wait()
    print(f"exit: decode={rc1} stream={rc2} encode={rc3}")
    print(f"stream log: {log_path}")
    # With --max-frames the stream host exits first and closes the pipe, so the
    # decoder reports EPIPE after finishing useful work. That is expected, not a
    # failure: 32 is EPIPE, and Windows surfaces it unsigned as 4294967264.
    decode_pipe_cut = a.max_frames and rc1 in (32, -32, 4294967264)
    if decode_pipe_cut:
        rc1 = 0
    if rc1 or rc2 or rc3:
        print(log_path.read_text(encoding="utf-8", errors="replace")[-2500:])
        return 1
    print(f"output: {out} ({out.stat().st_size} bytes)")

    # A frame-count check alone cannot catch a timing bug: collapsing VFR to the
    # nominal rate keeps every frame and still shortens the clip. Compare the
    # video stream's own duration, because a longer audio track hides the
    # shortening in the container duration.
    out_info = probe(str(out))
    src_video = float(info.get("duration") or 0) or in_duration
    # --max-frames deliberately shortens the clip; that is the expected length.
    expected = (a.max_frames / nominal) if (a.max_frames and nominal > 0) else src_video
    out_video = float(out_info.get("duration") or 0)
    if not out_video and str(out_info.get("nb_frames", "")).isdigit():
        out_video = float(out_info["nb_frames"]) / max(nominal, 1e-6)
    # Per-frame cost, taken from the renderer's own timing line: the same number
    # the GUI shows in its 延迟 column.
    try:
        timing = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        timing = ""
    m = re.search(r"avg_gpu=([\d.]+)ms", timing)
    if m:
        per_frame = float(m.group(1))
        line = f"per-frame: {per_frame:.2f} ms GPU ({1000.0 / per_frame:.0f} fps of compute)"
        wall = re.search(r"\[wall\] per-frame ([\d.]+)ms", timing)
        if wall:
            # Whole-loop cost per frame: the three stages run back to back, so this
            # is what actually limits throughput.
            w = float(wall.group(1))
            line += f", {w:.2f} ms wall ({1000.0 / w:.0f} fps end to end inside the streamer)"
        print(line)

    if expected > 0 and out_video > 0:
        drift = abs(out_video - expected) / expected
        verdict = "ok" if drift <= 0.01 else "MISMATCH"
        label = "expected (trimmed)" if a.max_frames else "source"
        print(f"video timing: {label} {expected:.3f}s -> output {out_video:.3f}s "
              f"({drift * 100:.2f}% drift, {verdict})")
        if drift > 0.01:
            print("  warning: the video track no longer matches the expected length, so playback "
                  "speed and audio sync are wrong. Variable-frame-rate input needs --vfr cfr.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
