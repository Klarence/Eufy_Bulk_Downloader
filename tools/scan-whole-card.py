#!/usr/bin/env python3
"""
Walk an entire eufy microSD card and report every video file on it.

The extraction only ever looked under /Camera00/event. This walks from the card
root so nothing can hide elsewhere (other camera folders, continue/, /video, and
so on), and cross-references what it finds against the clips already recovered so
any newly-discovered footage is obvious.

Read-only: uses debugfs `ls`, never writes to the card.

  sudo python3 tools/scan-whole-card.py --device /dev/rdisk8s1
"""
import argparse
import csv
import os
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict

DEBUGFS = "/opt/homebrew/opt/e2fsprogs/sbin/debugfs"
VIDEO_EXTS = {".zxvideo", ".dat", ".mp4", ".h265", ".h264", ".264", ".video", ".ts", ".m4v"}
LS_RE = re.compile(
    r"^\s*(\d+)\s+(\d+)\s*\(\d+\)\s+(\d+)\s+(\d+)\s+(\d+)\s+"
    r"(\d{1,2}-\w{3}-\d{4})\s+(\d{2}:\d{2})\s+(.+?)\s*$"
)
DATE_RE = re.compile(r"(20\d{2})(\d{2})(\d{2})")


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def dbg(device, command):
    return subprocess.run([DEBUGFS, "-R", command, device],
                          capture_output=True, text=True).stdout


def list_dir(device, path):
    """[(name, is_dir, size)] - tries unquoted then quoted, as debugfs -R does
    not do shell-style quote stripping."""
    for cmd in (f"ls -l {path}", f'ls -l "{path}"'):
        entries = []
        for line in dbg(device, cmd).splitlines():
            m = LS_RE.match(line)
            if not m:
                continue
            name = m.group(8)
            if name in (".", ".."):
                continue
            entries.append((name, m.group(2).startswith("4"), int(m.group(5))))
        if entries:
            return entries
    return []


def walk(device, root="/"):
    stack = [(root, 0)]
    files = []
    dirs = 0
    empty = []
    while stack:
        d, depth = stack.pop()
        entries = list_dir(device, d)
        dirs += 1
        if not entries:
            empty.append(d)
        if dirs % 50 == 0:
            log(f"  ...{dirs} dirs, {len(files)} files (depth {depth})")
        sub = []
        for name, is_dir, size in entries:
            p = f"{d.rstrip('/')}/{name}"
            if is_dir:
                sub.append(p)
            else:
                files.append((p, size))
        stack.extend((p, depth + 1) for p in sub)
    return files, dirs, empty


def stamp(path):
    m = DATE_RE.search(os.path.basename(path))
    return m.group(0) if m else None


def top(path):
    parts = path.strip("/").split("/")
    return "/".join(parts[:2]) if len(parts) > 1 else "/"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="/dev/rdisk8s1")
    ap.add_argument("--root", default="/")
    ap.add_argument("--recovered", default="/Volumes/Samsung T5/eufy-footage/MANIFEST.csv")
    ap.add_argument("--report", default="/Volumes/Samsung T5/eufy-footage/CARD-SCAN.csv")
    args = ap.parse_args()

    if os.geteuid() != 0:
        sys.exit("needs root to read the raw card: sudo " + " ".join(sys.argv))
    if not os.path.exists(DEBUGFS):
        sys.exit(f"debugfs not found at {DEBUGFS}")

    log(f"walking {args.device} from {args.root} ...")
    files, dirs, empty = walk(args.device, args.root)
    log(f"walk complete: {dirs} directories, {len(files)} files")

    videos = [(p, s) for p, s in files
              if os.path.splitext(p)[1].lower() in VIDEO_EXTS]
    log(f"video files found: {len(videos)}")

    # Anything dated before 2026 is the thing we actually care about.
    years = Counter()
    old = []
    for p, s in videos:
        st = stamp(p)
        y = st[:4] if st else "????"
        years[y] += 1
        if st and st < "20260101":
            old.append((p, s, st))
    log(f"video date years: {dict(sorted(years.items()))}")
    if old:
        log(f"*** {len(old)} videos predate 2026 ***")
        for p, s, st in old[:30]:
            log(f"    {st}  {s/1e6:8.1f} MB  {p}")

    by_area = defaultdict(lambda: [0, 0])
    for p, s in videos:
        a = top(p)
        by_area[a][0] += 1
        by_area[a][1] += s
    log("\nvideos by area:")
    for a in sorted(by_area, key=lambda x: -by_area[x][1]):
        n, b = by_area[a]
        log(f"  {a:24} {n:6d} clips  {b/1e9:7.1f} GB")

    non_event = [(p, s) for p, s in videos if not p.startswith("/Camera00/event/")]
    log(f"\nvideos OUTSIDE /Camera00/event/: {len(non_event)} "
        f"({sum(s for _, s in non_event)/1e9:.1f} GB)")
    for p, s in non_event[:20]:
        log(f"    {s/1e6:8.1f} MB  {p}")

    # Cross-reference against what we already recovered.
    have = set()
    if os.path.exists(args.recovered):
        with open(args.recovered) as fh:
            for row in csv.DictReader(fh):
                have.add(row["filename"])
    oncard = {os.path.basename(p) for p, _ in videos}
    missing = sorted(oncard - have)
    log(f"\nalready recovered: {len(have & oncard)} matching names")
    log(f"on card but NOT recovered: {len(missing)}")
    for m in missing[:25]:
        log(f"    {m}")
    if len(missing) > 25:
        log(f"    ... and {len(missing)-25} more (see {args.report})")

    os.makedirs(os.path.dirname(args.report), exist_ok=True)
    with open(args.report, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["path", "bytes", "date", "area", "recovered"])
        for p, s in sorted(videos):
            base = os.path.basename(p)
            w.writerow([p, s, stamp(p) or "", top(p), "yes" if base in have else "no"])
    log(f"\nfull listing written to {args.report}")

    log("=" * 60)
    if not old and not non_event:
        log("RESULT: nothing older or outside event/ - the recovered set is complete")
    else:
        log("RESULT: see items flagged above; this card holds more than we extracted")


if __name__ == "__main__":
    main()