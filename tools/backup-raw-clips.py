#!/usr/bin/env python3
"""
Back up the raw .zxvideo clips we cannot yet decode.

The recovered MP4s are fine, but the ~15.8k clips from 30 May onward are in
eufy's per-frame format which we have not reverse-engineered yet. Those raw
files are currently the ONLY copy of that footage: the card is 94% full and
deletes oldest-first, so every day it sits in the camera risks losing more.

This copies the raw clips and their .txt sidecars off the card onto the T5
before any more experimenting happens. Nothing is converted or modified - the
bytes are preserved exactly so a future decoder can work on them.

Resumable: files already present are skipped, so it is safe to re-run after an
interruption.

  sudo python3 tools/backup-raw-clips.py --device /dev/rdisk8s1
"""
import argparse
import csv
import os
import subprocess
import sys
import time
from collections import defaultdict


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="/dev/rdisk8s1")
    ap.add_argument("--scan", default="/Volumes/Samsung T5/eufy-footage/CARD-SCAN.csv")
    ap.add_argument("--manifest",
                    default="/Volumes/Samsung T5/eufy-footage/MANIFEST.csv")
    ap.add_argument("--out", default="/Volumes/Samsung T5/eufy-raw")
    ap.add_argument("--batch", type=int, default=1200)
    ap.add_argument("--skip-sidecars", action="store_true")
    args = ap.parse_args()

    if os.geteuid() != 0:
        sys.exit("needs root to read the raw card: sudo " + " ".join(sys.argv))
    if not os.path.exists(args.scan):
        sys.exit(f"scan file not found: {args.scan}\nrun tools/scan-whole-card.py first")

    have = set()
    if os.path.exists(args.manifest):
        with open(args.manifest) as fh:
            for r in csv.DictReader(fh):
                have.add(os.path.splitext(r["filename"])[0])

    rows = [r for r in csv.DictReader(open(args.scan)) if r["date"]]
    todo = [r for r in rows
            if os.path.splitext(os.path.basename(r["path"]))[0] not in have]
    total_bytes = sum(int(r["bytes"]) for r in todo)
    log(f"{len(rows)} clips on card, {len(have)} already recovered as MP4")
    log(f"to back up: {len(todo)} raw clips, {total_bytes/1e9:.1f} GB")

    by_month = defaultdict(list)
    for r in todo:
        by_month[r["date"][:6]].append(r["path"])

    os.makedirs(args.out, exist_ok=True)
    t0 = time.time()
    done = 0
    done_bytes = 0
    failed = []

    for month in sorted(by_month):
        paths = by_month[month]
        dest = os.path.join(args.out, month)
        os.makedirs(dest, exist_ok=True)
        missing = []
        for p in paths:
            stem = os.path.basename(p).rsplit(".", 1)[0]
            if os.path.exists(os.path.join(dest, stem + ".zxvideo")):
                continue
            missing.append(p)
        if not missing:
            log(f"{month}: already complete ({len(paths)} clips)")
            done += len(paths)
            continue

        log(f"{month}: {len(missing)} clips to copy -> {dest}")
        for i in range(0, len(missing), args.batch):
            batch = missing[i:i + args.batch]
            want = []
            for p in batch:
                want.append(p)
                if not args.skip_sidecars:
                    want.append(p.rsplit(".", 1)[0] + ".txt")
            r = subprocess.run(
                ["e2cp", "-s", f"{args.device}:/", "-d", dest],
                input="".join(x + "\n" for x in want),
                capture_output=True, text=True)
            if r.returncode != 0:
                log(f"  batch error: {r.stderr.strip()[:300]}")
                failed.extend(batch)
            done += len(batch)
            done_bytes += sum(int(x["bytes"]) for x in rows if x["path"] in set(batch))
            rate = done_bytes / max(time.time() - t0, 1e-9) / 1e6
            eta = (total_bytes - done_bytes) / max(rate * 1e6, 1) / 60
            log(f"  {done}/{len(todo)} clips, {done_bytes/1e9:.1f}/{total_bytes/1e9:.1f} GB, "
                f"{rate:.1f} MB/s, ETA {eta/60:.1f}h")

    log("=" * 60)
    log(f"copied {done} clips, {done_bytes/1e9:.1f} GB in {(time.time()-t0)/60:.1f} min")
    if failed:
        log(f"{len(failed)} failed - re-run this script to retry them")
        for p in failed[:10]:
            log(f"    {p}")
    log(f"raw archive: {args.out}")


if __name__ == "__main__":
    main()