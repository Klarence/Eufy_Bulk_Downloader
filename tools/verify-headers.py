#!/usr/bin/env python3
"""
Prove a candidate HEVC parameter-set file actually decodes a real card clip.

Structural checks are not enough here: a wrong SPS still yields a well-formed
MP4 with a correct duration and a scrubbing timeline while every frame renders
green or grey. This decodes a clip, reports decoder errors, and writes a PNG so
the picture can be eyeballed. Run this before trusting a batch conversion.

  python3 tools/verify-headers.py <headers.bin> <clip.zxvideo> [--png out.png]
"""
import os
import subprocess
import sys

IRAP = set(range(16, 24))


def annexb(buf):
    nals, j = [], 0
    while j < len(buf) - 4:
        k = buf.find(b"\x00\x00\x01", j)
        if k == -1:
            break
        nals.append((k - 1 if (k > 0 and buf[k - 1] == 0) else k, (buf[k + 3] >> 1) & 0x3F))
        j = k + 3
    return nals


def run(cmd):
    return subprocess.run(cmd, capture_output=True)


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    headers_path, clip = sys.argv[1], sys.argv[2]
    png = None
    if "--png" in sys.argv:
        png = sys.argv[sys.argv.index("--png") + 1]

    headers = open(headers_path, "rb").read()
    data = open(clip, "rb").read()
    nals = annexb(data[:4 * 1024 * 1024])
    first_irap = next((o for o, t in nals if t in IRAP), None)
    if first_irap is None:
        sys.exit("no IRAP NAL found in clip")

    ts = headers + data[first_irap:]
    tmp_h = clip + ".probe.h265"
    open(tmp_h, "wb").write(ts)

    mp4 = clip + ".probe.mp4"
    r = run(["ffmpeg", "-y", "-v", "error", "-f", "hevc", "-i", tmp_h,
             "-c", "copy", "-tag:v", "hvc1", mp4])
    if r.returncode != 0:
        print("CONVERT FAILED:", r.stderr.decode()[:400])

    info = run(["ffprobe", "-v", "error", "-show_entries",
                "stream=width,height,nb_frames,duration", "-of", "csv=p=0", mp4])
    print(f"headers : {headers_path} ({len(headers)} bytes)")
    print(f"clip    : {os.path.basename(clip)} (IRAP at {first_irap})")
    print(f"decoded : {info.stdout.decode().strip() or 'nothing'}")

    dec = run(["ffmpeg", "-v", "error", "-i", tmp_h, "-f", "null", "-"])
    errs = [l for l in dec.stderr.decode().splitlines()
            if "ref lists" in l or "RPS" in l or "cu_qp_delta" in l or "CABAC" in l]
    print(f"errors  : {len(errs)}")
    for l in errs[:5]:
        print("   ", l.split("]")[-1].strip())

    # Coverage matters more than variance: a real picture fills the frame, a
    # corrupt one leaves large flat regions. Sample across the clip.
    worst = None
    dur = 22.0
    for t in (1, dur * 0.35, dur * 0.6, dur * 0.85):
        raw = run(["ffmpeg", "-v", "error", "-ss", f"{t:.1f}", "-i", mp4,
                   "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"]).stdout
        if not raw:
            print(f"  t={t:5.1f}s  NO FRAME")
            worst = worst if worst is not None else 0.0
            continue
        n = len(raw)
        mean = sum(raw) / n
        var = sum((b - mean) ** 2 for b in raw) / n
        # fraction of bytes within 3 of the mean => flat area
        flat = sum(1 for b in raw if abs(b - mean) <= 3) / n
        print(f"  t={t:5.1f}s  mean={mean:6.1f} var={var:8.1f} flat={flat*100:5.1f}%")
        score = var if flat < 0.6 else 0.0
        worst = score if worst is None else min(worst, score)

    if png:
        run(["ffmpeg", "-y", "-v", "error", "-ss", "5", "-i", mp4,
             "-frames:v", "1", "-vf", "scale=960:-2", png])
        print(f"frame   : {png}")

    print()
    verdict = (len(errs) == 0 and worst is not None and worst > 40)
    print("VERDICT:", "PASS - decodes cleanly" if verdict
          else "FAIL - still corrupt; do NOT run a batch with these headers")
    os.unlink(tmp_h)
    if not verdict:
        os.unlink(mp4)
    sys.exit(0 if verdict else 1)


if __name__ == "__main__":
    main()
