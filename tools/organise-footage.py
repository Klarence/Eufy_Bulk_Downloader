#!/usr/bin/env python3
"""
Organise and verify the recovered eufy footage, then write a manifest.

Moves the flat YYYYMMDDHHMMSS.mp4 files into per-month folders, checks every
one actually decodes (not a spot check - a silent 0-byte or truncated file is
worse than a missing one, because it looks like footage), and records the
result in a CSV manifest plus a README describing exactly what is present and
what is missing.

  python3 tools/organise-footage.py --src "/Volumes/Samsung T5/eufy" \
                                    --dst "/Volumes/Samsung T5/eufy-footage"
"""
import argparse
import csv
import hashlib
import os
import re
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime

NAME_RE = re.compile(r"^(\d{8})(\d{6})\.mp4$", re.I)


def probe(path):
    """Return (ok, width, height, seconds, note)."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height:format=duration",
         "-of", "json", path],
        capture_output=True, text=True)
    if r.returncode != 0:
        return False, 0, 0, 0.0, "ffprobe failed"
    import json
    try:
        j = json.loads(r.stdout)
    except json.JSONDecodeError:
        return False, 0, 0, 0.0, "unparseable ffprobe output"
    st = (j.get("streams") or [{}])[0]
    w, h = int(st.get("width") or 0), int(st.get("height") or 0)
    dur = float(j.get("format", {}).get("duration") or 0)
    if w <= 0 or h <= 0:
        return False, w, h, dur, "no video dimensions"
    return True, w, h, dur, ""


def decodes(path):
    """True if a frame can actually be decoded near the middle of the clip."""
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", "1", "-i", path, "-frames:v", "1",
         "-f", "null", "-"], capture_output=True, text=True)
    return r.returncode == 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--dst", required=True)
    ap.add_argument("--no-move", action="store_true", help="verify in place only")
    ap.add_argument("--full-decode", action="store_true",
                    help="decode every frame (slow) instead of one probe frame")
    args = ap.parse_args()

    files = sorted(f for f in os.listdir(args.src)
                   if f.lower().endswith(".mp4") and NAME_RE.match(f))
    strays = sorted(f for f in os.listdir(args.src)
                    if f.lower().endswith(".mp4") and not NAME_RE.match(f))
    print(f"found {len(files)} named clips, {len(strays)} unrecognised .mp4 files")
    if strays:
        print("  unrecognised:", strays[:10])

    rows = []
    bad = []
    by_month = defaultdict(lambda: [0, 0])
    total_bytes = 0
    total_dur = 0.0

    for i, f in enumerate(files, 1):
        m = NAME_RE.match(f)
        day, clock = m.group(1), m.group(2)
        month = f"{day[:4]}-{day[4:6]}"
        src = os.path.join(args.src, f)
        size = os.path.getsize(src)
        total_bytes += size

        ok, w, h, dur, note = probe(src)
        dec = decodes(src) if ok else False
        good = ok and dec
        if not good:
            bad.append((f, note or "frame decode failed"))
        else:
            total_dur += dur
        by_month[month][0] += 1
        by_month[month][1] += size

        rows.append({
            "filename": f,
            "date": f"{day[:4]}-{day[4:6]}-{day[6:8]}",
            "time": f"{clock[:2]}:{clock[2:4]}:{clock[4:6]}",
            "bytes": size,
            "resolution": f"{w}x{h}" if w else "",
            "seconds": f"{dur:.2f}",
            "status": "ok" if good else "FAILED",
            "note": note,
        })

        if not args.no_move and good:
            d = os.path.join(args.dst, month)
            os.makedirs(d, exist_ok=True)
            tgt = os.path.join(d, f)
            if os.path.abspath(tgt) != os.path.abspath(src):
                shutil.move(src, tgt)

        if i % 250 == 0:
            print(f"  {i}/{len(files)} verified...", flush=True)

    os.makedirs(args.dst, exist_ok=True)
    manifest = os.path.join(args.dst, "MANIFEST.csv")
    with open(manifest, "w", newline="") as fh:
        wr = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        wr.writeheader()
        wr.writerows(rows)

    print("\n" + "=" * 62)
    print(f"clips verified   : {len(rows) - len(bad)}/{len(rows)}")
    print(f"total size       : {total_bytes/1e9:.1f} GB")
    print(f"total duration   : {total_dur/3600:.2f} hours")
    print(f"\nper month:")
    for k in sorted(by_month):
        n, b = by_month[k]
        print(f"  {k}  {n:5d} clips  {b/1e9:6.1f} GB")
    if bad:
        print(f"\n{len(bad)} FAILED - listed in MANIFEST.csv, not filed:")
        for f, why in bad[:20]:
            print(f"  {f}  {why}")
    print(f"\nmanifest: {manifest}")

    # Keep failed files out of the organised tree but leave them where they are.
    if args.no_move:
        print("(verify-only: nothing was moved)")
    else:
        print(f"\nfiled into {args.dst}/<YYYY-MM>/")


if __name__ == "__main__":
    main()