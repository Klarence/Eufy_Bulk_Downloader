#!/usr/bin/env python3
"""
Bulk-extract playable MP4s from an eufy camera's microSD card.

eufy cards are ext4, which macOS cannot mount. This reads the card through
e2fsprogs' `debugfs` (read-only, no mount, no kernel driver) and converts each
clip to a standalone MP4.

The container format is auto-detected per file rather than assumed, and every
output is validated with ffprobe before being kept, so a mis-detected file is
reported instead of silently producing a broken MP4.

Requires root, because raw block-device reads need it:
    sudo python3 tools/eufy-sd-extract.py --device /dev/rdisk8s1 --out /Volumes/T5/eufy
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time

DEBUGFS = "/opt/homebrew/opt/e2fsprogs/sbin/debugfs"
# VPS/SPS/PPS only. PREFIX_SEI (39) is metadata and must not be treated as a
# parameter set - doing so made the extractor believe the clip was self-contained
# and skip the reference headers it actually needed.
REAL_PARAM_NALS = {32, 33, 34}

# Each event clip is accompanied by .txt JSON metadata, encrypted .jpg
# thumbnails and .crop/.lst/.evt index files. Only these are worth converting;
# treating a sidecar as an "unknown video" just buries the real signal.
VIDEO_EXTS = {
    ".zxvideo",
    ".dat",
    ".mp4",
    ".h265",
    ".264",
    ".h264",
    ".video",
    ".ts",
    ".m4v",
}

# debugfs re-opens (and re-validates) the whole filesystem on every invocation.
# With 62M blocks and 15.6M inodes that costs ~1s per file, which at 100k+ clips
# is measured in days. e2cp holds the fs open across a whole batch instead.
DEFAULT_BATCH = 1500


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def dbg(device, command):
    """Run one debugfs command; return stdout."""
    r = run([DEBUGFS, "-R", command, device])
    # debugfs writes its version banner to stdout; harmless.
    return r.stdout


LS_RE = re.compile(
    r"^\s*(\d+)\s+(\d+)\s*\(\d+\)\s+(\d+)\s+(\d+)\s+(\d+)\s+"
    r"(\d{1,2}-\w{3}-\d{4})\s+(\d{2}:\d{2})\s+(.+?)\s*$"
)


def list_dir(device, path):
    """Return [(name, is_dir, size)] for a directory on the card.

    debugfs's -R parser splits on whitespace and does not do shell-style quote
    stripping, so a quoted path can silently resolve to nothing. Try unquoted
    first and fall back to quoted, so a path with an odd character still lists.
    """
    out = dbg(device, f"ls -l {path}")
    entries = _parse_ls(out)
    if not entries:
        entries = _parse_ls(dbg(device, f'ls -l "{path}"'))
    return entries


def _parse_ls(out):
    entries = []
    for line in out.splitlines():
        m = LS_RE.match(line)
        if not m:
            continue
        name = m.group(8)
        if name in (".", ".."):
            continue
        mode = m.group(2)
        # e2fsprogs mode: leading digit 4 = directory, 1 = regular file.
        entries.append((name, mode.startswith("4"), int(m.group(5))))
    return entries


def walk(device, root, limit=None, report=None):
    """Depth-first walk yielding (path, size) for regular files.

    Each directory costs a debugfs process spawn, so on a card with many
    thousands of inodes this takes minutes. Progress is reported as it goes:
    a silent walk is indistinguishable from a hung one.

    Subdirectories are pushed in reverse so they pop in natural order, which
    makes the walk chronological -- oldest footage first. That matters: eufy
    overwrites the oldest clips when the card fills, so the clips at greatest
    risk of deletion are exactly the ones we want off the card first.
    """
    stack = [(root, 0)]
    seen = 0
    last_report = 0
    while stack:
        d, _ = stack.pop()
        entries = list_dir(device, d)
        if not entries and report is not None:
            report["empty"].append(d)
        if report is not None:
            report["dirs"] += 1
            if report["dirs"] % 25 == 0 and report["dirs"] != last_report:
                last_report = report["dirs"]
                log(
                    f"  ...walking: {report['dirs']} dirs, {report['files']} files, "
                    f"{report['bytes'] / 1e9:.1f} GB (pending: {len(stack)})"
                )

        subdirs = []
        for name, is_dir, size in entries:
            p = f"{d.rstrip('/')}/{name}"
            if is_dir:
                subdirs.append(p)
            else:
                if report is not None:
                    report["files"] += 1
                    report["bytes"] += size
                yield p, size
                seen += 1
                if limit and seen >= limit:
                    return
        for p in reversed(subdirs):
            stack.append((p, 1))


def annexb_nals(buf, limit=None):
    """Yield (start_offset, nal_type) for Annex-B start codes.

    A 3- or 4-byte start code both put the NAL header byte at k+3 when the
    00 00 01 pattern is found at k, so one scan covers both.
    """
    j = 0
    n = len(buf)
    out = []
    while True:
        k = buf.find(b"\x00\x00\x01", j)
        if k == -1 or (limit and len(out) > limit):
            break
        if k + 3 >= n:
            break
        nal_type = (buf[k + 3] >> 1) & 0x3F
        start = k - 1 if (k > 0 and buf[k - 1] == 0) else k
        out.append((start, nal_type))
        j = k + 3
    return out


def is_plaintext_hevc(head):
    """True if `head` looks like a real Annex-B HEVC stream.

    The card holds a MIX of clips: some store plaintext video, some store
    eufy-AES ciphertext. Scanning ciphertext for 00 00 01 patterns finds
    plausible-looking but meaningless NAL headers - a uniform spread across
    types 0..63, including values above 40 which do not exist in HEVC, and an
    "IDR" hundreds of KB in. Those clips cannot be decoded without eufy's key,
    and treating the noise as a stream is what produced 8765 corrupt MP4s.

    Two robust discriminators, both verified against a known-good clip:
      * HEVC NAL types never exceed 40.
      * a real clip opens with its IDR within the first few hundred bytes.
    """
    nals, j = [], 0
    while j < len(head) - 4:
        k = head.find(b"\x00\x00\x01", j)
        if k == -1:
            break
        if k + 3 < len(head):
            nals.append(
                (
                    k - 1 if (k > 0 and head[k - 1] == 0) else k,
                    (head[k + 3] >> 1) & 0x3F,
                )
            )
        j = k + 3
    if len(nals) < 8:
        return False, "too few NALs to be a video stream"
    bad = sum(1 for _, t in nals if t > 40)
    if bad:
        return False, f"{bad}/{len(nals)} NAL types >40 - not HEVC (ciphertext)"
    first_irap = next((o for o, t in nals if 16 <= t <= 23), None)
    if first_irap is None:
        return False, "no IDR/CRA found"
    if first_irap > 65536:
        return False, f"first IDR at {first_irap} - not a stream start"
    return True, f"{len(nals)} NALs, first IDR at {first_irap}"


def probe_and_plan(path):
    """Return (kind, video_start, note) for a card file."""
    with open(path, "rb") as f:
        head = f.read(4 * 1024 * 1024)

    if head[4:8] == b"ftyp":
        return "mp4", 0, "already an MP4"
    if head[:4] != b"XZYH" and head[:3:4] != b"\x00\x00\x01":
        return "unknown", None, f"unrecognised magic {head[:8].hex()}"

    ok, why = is_plaintext_hevc(head)
    if not ok:
        return "encrypted", None, why

    kind = "zxvideo" if head[:4] == b"XZYH" else "hevc-raw"
    nals = annexb_nals(head)
    first_irap = next(o for o, t in nals if 16 <= t <= 23)
    # The 4K reference set is used unconditionally: it is verified against a
    # real clip, and a clip's own inline sets cannot be trusted to describe the
    # same geometry.
    return kind, first_irap, why


ADTS_RE = re.compile(rb"\xff[\xf0-\xff]")


def _adts_frame_len(data, i, n):
    """Validate an ADTS header at i; return (total_length, profile) or None.

    Checks the fields that a random 0xFFFx byte-pair inside HEVC data would
    almost never satisfy simultaneously: layer == 00, a real sample-rate index,
    a sane channel configuration, and exactly one raw data block.
    """
    if i + 7 > n:
        return None
    b1, b2, b3, b6 = data[i + 1], data[i + 2], data[i + 3], data[i + 6]
    if (b1 & 0x06) != 0:  # layer must be 00
        return None
    sf_index = (b2 & 0x3C) >> 2
    if sf_index > 12:  # 13-15 are reserved
        return None
    channel_config = ((b2 & 0x01) << 2) | ((b3 & 0xC0) >> 6)
    if channel_config < 1 or channel_config > 7:
        return None
    if (b6 & 0x03) != 0:  # multiple raw data blocks per frame: unexpected here
        return None
    length = ((b3 & 0x03) << 11) | (data[i + 4] << 3) | (data[i + 5] >> 5)
    crc = 0 if (b1 & 0x01) else 2  # protection_absent == 0 means CRC present
    if length < (7 + crc) or length > 2048:
        return None
    if i + length > n:
        return None
    profile = (sf_index, channel_config, b2 & 0xC0)
    return length, profile


def extract_adts(path):
    """Carve AAC ADTS frames out of an interleaved eufy stream.

    Audio is woven between video NALs rather than stored in one block, but ADTS
    frames are self-delimiting, so concatenating them in file order rebuilds a
    decodable AAC stream. Header validation alone still admits false positives,
    so a second pass keeps only frames sharing the dominant
    (sample-rate, channels, profile) triple -- real audio is homogeneous, stray
    matches are not.
    """
    with open(path, "rb") as f:
        data = f.read()
    n = len(data)

    candidates = []
    pos = 0
    while pos < n - 7:
        m = ADTS_RE.search(data, pos)
        if not m:
            break
        i = m.start()
        pos = i + 1
        got = _adts_frame_len(data, i, n)
        if got:
            candidates.append((i, got[0], got[1]))

    if not candidates:
        return b"", 0

    # Dominant profile wins; a lone oddball frame is almost certainly a false hit.
    counts = {}
    for _, _, prof in candidates:
        counts[prof] = counts.get(prof, 0) + 1
    dominant = max(counts.items(), key=lambda kv: kv[1])[0]
    if counts[dominant] < 30:
        return b"", 0

    out = bytearray()
    frames = 0
    for i, length, prof in candidates:
        if prof != dominant:
            continue
        out += data[i : i + length]
        frames += 1
    return bytes(out), frames


def convert(src, out_mp4, headers, payload_offset, audio=None, audio_offset=None):
    """Write src's HEVC payload (plus headers) into out_mp4 as a playable MP4."""
    tmp_h265 = out_mp4 + ".h265"
    with open(tmp_h265, "wb") as out:
        if headers:
            out.write(headers)
        if payload_offset:
            with open(src, "rb") as f:
                f.seek(payload_offset)
                shutil.copyfileobj(f, out, length=4 * 1024 * 1024)
        else:
            with open(src, "rb") as f:
                shutil.copyfileobj(f, out, length=4 * 1024 * 1024)

    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "hevc", "-i", tmp_h265]
    tmp_aac = None
    if audio:
        tmp_aac = out_mp4 + ".aac"
        with open(tmp_aac, "wb") as f:
            f.write(audio)
        if audio_offset:
            cmd += ["-itsoffset", str(audio_offset), "-f", "adts", "-i", tmp_aac]
        else:
            cmd += ["-f", "adts", "-i", tmp_aac]
        cmd += ["-map", "0:v:0", "-map", "1:a:0"]
    cmd += ["-c", "copy", "-tag:v", "hvc1", "-movflags", "+faststart", out_mp4]

    r = run(cmd)
    os.unlink(tmp_h265)
    if tmp_aac:
        os.unlink(tmp_aac)
    return (
        r.returncode == 0 and os.path.exists(out_mp4) and os.path.getsize(out_mp4) > 0
    )


def validate(mp4):
    """Return (codec, width, height) for a playable video, else None.

    Uses JSON rather than csv: -of csv emits fields in ffprobe's internal
    order, not the order requested, so positional parsing silently misreads.
    """
    r = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,width,height",
            "-of",
            "json",
            mp4,
        ]
    )
    if r.returncode != 0:
        return None
    try:
        streams = json.loads(r.stdout).get("streams") or []
    except json.JSONDecodeError:
        return None
    if not streams:
        return None
    s = streams[0]
    try:
        w, h = int(s.get("width", 0)), int(s.get("height", 0))
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    return s.get("codec_name"), w, h


def looks_corrupt(mp4, samples=2):
    """True if the clip is black or badly corrupted; None if undeterminable.

    Checks flatness, not variance. A wrong-resolution header decodes a thin
    strip of correct picture over a huge uniform field, which still shows high
    variance - an earlier version of this function used variance and waved
    through every one of 3634 corrupt files. What actually identifies the
    failure is the fraction of each frame that is uniform, so several frames
    spread across the clip are sampled and the worst one decides.
    """
    dur = 22.0
    try:
        r = run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                mp4,
            ]
        )
        dur = float(json.loads(r.stdout)["format"]["duration"])
    except Exception:
        pass

    worst = None
    for i in range(samples):
        t = 1.0 + (dur - 2.0) * (i / max(samples - 1, 1))
        raw = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-ss",
                f"{t:.1f}",
                "-i",
                mp4,
                "-frames:v",
                "1",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "gray",
                "-",
            ],
            capture_output=True,
        ).stdout
        if not raw:
            return True  # could not decode a frame this far in = broken
        mean = sum(raw) / len(raw)
        flat = sum(1 for b in raw if abs(b - mean) <= 3) / len(raw)
        worst = flat if worst is None else max(worst, flat)
    return worst > 0.6 if worst is not None else None


def e2cp_batch(device, listing, staging):
    """Copy a batch of card files into the staging dir; return how many landed.

    Staging is a hidden sibling of the output dir rather than the output dir
    itself: e2cp must materialise a whole batch before any of it can be
    converted, and dumping ~14GB of raw .zxvideo into the destination makes it
    look like the job is filling up with junk instead of making progress.
    """
    for f in os.listdir(staging):
        # Clear anything an interrupted previous run left behind.
        try:
            os.unlink(os.path.join(staging, f))
        except OSError:
            pass
    r = subprocess.run(
        ["e2cp", "-s", f"{device}:/", "-d", staging],
        input=listing,
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        log(f"  e2cp stderr: {r.stderr.strip()[:400]}")
    exts = tuple(VIDEO_EXTS)
    return sum(1 for e in os.scandir(staging) if e.name.lower().endswith(exts))


def save_cached_list(path, files):
    with open(path, "w") as f:
        for remote, size in files:
            f.write(f"{size}\t{remote}\n")


def load_cached_list(path):
    if not os.path.exists(path):
        return None
    out = []
    try:
        with open(path) as f:
            for line in f:
                size, _, remote = line.rstrip("\n").partition("\t")
                out.append((remote, int(size)))
    except (OSError, ValueError):
        return None
    return out or None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="/dev/rdisk8s1")
    ap.add_argument(
        "--out",
        default=None,
        help="output directory (use the T5, not internal); not needed for --list",
    )
    ap.add_argument(
        "--headers",
        default=os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "hevc_params_4k_from_cam.bin"
        ),
        help="HEVC VPS/SPS/PPS prefix (default: camera-derived 4K set)",
    )
    ap.add_argument("--root", default="/Camera00/event")
    ap.add_argument("--limit", type=int, default=None, help="stop after N files")
    ap.add_argument(
        "--with-audio",
        action="store_true",
        help="attempt ADTS AAC carve (off by default: the only reference "
        "clip had no valid AAC, and carving garbage is worse than none)",
    )
    ap.add_argument(
        "--audio-offset",
        default=None,
        help="seconds to shift audio by (e.g. -0.127) if it leads video",
    )
    ap.add_argument(
        "--list",
        action="store_true",
        help="print the card tree with per-directory file counts, then exit",
    )
    ap.add_argument(
        "--batch",
        type=int,
        default=DEFAULT_BATCH,
        help=f"clips per e2cp batch (default {DEFAULT_BATCH})",
    )
    ap.add_argument(
        "--since",
        default=None,
        help="only clips on/after this YYYYMMDD (e.g. 20260124)",
    )
    ap.add_argument("--until", default=None, help="only clips on/before this YYYYMMDD")
    ap.add_argument(
        "--walk",
        action="store_true",
        help="re-walk the card even if a cached file list exists",
    )
    ap.add_argument(
        "--reject-black",
        action="store_true",
        default=True,
        help="discard clips that decode to a black frame (default on)",
    )
    ap.add_argument(
        "--keep-black",
        dest="reject_black",
        action="store_false",
        help="keep clips even if they decode black",
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="list + probe only, write nothing"
    )
    args = ap.parse_args()

    if os.geteuid() != 0:
        sys.exit(
            "must run as root (debugfs needs raw device access): sudo "
            + " ".join(sys.argv)
        )
    if not os.path.exists(DEBUGFS):
        sys.exit(f"debugfs not found at {DEBUGFS} (brew install e2fsprogs)")
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            sys.exit(f"{tool} not on PATH (brew install ffmpeg)")

    headers = b""
    if args.headers and os.path.exists(args.headers):
        headers = open(args.headers, "rb").read()
        log(f"loaded {len(headers)} bytes of HEVC headers from {args.headers}")
    elif not args.list:
        log("WARNING: no --headers given; clips may fail to decode without VPS/SPS/PPS")

    if not args.list and not args.out:
        sys.exit("--out is required unless --list is given")

    report = {"dirs": 0, "files": 0, "bytes": 0, "empty": []}

    if args.list:
        # Stream rather than materialise: a card with ~100k inodes would
        # otherwise print nothing until the entire walk finished.
        log(f"streaming listing of {args.root} ...")
        for remote, size in walk(args.device, args.root, args.limit, report):
            log(f"  {size / 1e6:9.1f} MB  {remote}")
        log("=" * 60)
        log(
            f"TOTAL: {report['files']} files, {report['bytes'] / 1e9:.1f} GB, "
            f"{report['dirs']} dirs visited, {len(report['empty'])} empty"
        )
        return

    # Cache the walk: it costs a debugfs spawn per directory and the card does
    # not change while we read it, so re-walking on every resume is waste.
    listfile = os.path.join(args.out, ".filelist.tsv")
    cached = load_cached_list(listfile)
    if cached is not None and not args.walk:
        files = cached
        log(f"loaded {len(files)} files from cache {listfile}")
    else:
        files = list(walk(args.device, args.root, args.limit, report))
        total_bytes = sum(s for _, s in files)
        log(f"found {len(files)} files under {args.root} ({total_bytes / 1e9:.1f} GB)")
        log(
            f"  directories visited: {report['dirs']}, returning nothing: {len(report['empty'])}"
        )
        if cached is not None:
            log(f"  (ignoring cache, --walk given)")

    if not files:
        log("nothing to do")
        return

    if args.since or args.until:

        def day(p):
            b = os.path.basename(p)
            return b[:8] if len(b) >= 8 and b[:8].isdigit() else ""

        before = len(files)
        files = [
            (p, s)
            for p, s in files
            if (not args.since or day(p) >= args.since)
            and (not args.until or day(p) <= args.until)
        ]
        log(
            f"date filter {args.since or '-'}..{args.until or '-'}: "
            f"{before} -> {len(files)} files"
        )

    # Sidecars are not videos; drop them before they can pollute the stats.
    videos = [(p, s) for p, s in files if os.path.splitext(p)[1].lower() in VIDEO_EXTS]
    other = len(files) - len(videos)
    log(
        f"{len(videos)} video files ({sum(s for _, s in videos) / 1e9:.1f} GB), "
        f"{other} non-video sidecars skipped"
    )
    if not videos:
        log("no video files found; nothing to convert")
        return

    if not args.list and not args.walk:
        try:
            os.makedirs(args.out, exist_ok=True)
            save_cached_list(listfile, files)
        except OSError as e:
            log(f"could not write file-list cache: {e}")

    if args.list:
        for remote, size in videos:
            log(f"  {size / 1e6:9.1f} MB  {remote}")
        return

    if args.dry_run:
        for remote, _ in videos[: args.limit or 5]:
            log(f"  would convert {remote}")
        return

    os.makedirs(args.out, exist_ok=True)
    staging = os.path.join(args.out, ".staging")
    os.makedirs(staging, exist_ok=True)

    stats = {}
    failures = []
    unknown_magic = {}
    encrypted = []
    done = 0
    t0 = time.time()

    batches = [videos[i : i + args.batch] for i in range(0, len(videos), args.batch)]
    log(
        f"processing {len(videos)} clips in {len(batches)} batches "
        f"of up to {args.batch} (e2cp keeps the filesystem open per batch)"
    )

    for bi, batch in enumerate(batches, 1):
        todo = []
        for remote, _ in batch:
            base = os.path.basename(remote)
            out_mp4 = os.path.join(args.out, base.rsplit(".", 1)[0] + ".mp4")
            if os.path.exists(out_mp4):
                stats["skipped"] = stats.get("skipped", 0) + 1
            else:
                todo.append((remote, out_mp4))
        if not todo:
            continue

        batch_bytes = 0
        listing = "".join(f"{r}\n" for r, _ in todo)
        copied = e2cp_batch(args.device, listing, staging)
        if copied == 0:
            log(f"batch {bi}: e2cp copied nothing, skipping {len(todo)} clips")
            failures.extend((r, "e2cp batch failed") for r, _ in todo)
            stats["copy-failed"] = stats.get("copy-failed", 0) + len(todo)
            continue

        for remote, out_mp4 in todo:
            base = os.path.basename(remote)
            tmp = os.path.join(staging, base)
            done += 1
            if not os.path.exists(tmp):
                failures.append((remote, "missing after copy"))
                stats["missing"] = stats.get("missing", 0) + 1
                continue
            batch_bytes += os.path.getsize(tmp)

            kind, off, note = probe_and_plan(tmp)
            stats[kind] = stats.get(kind, 0) + 1
            if stats[kind] <= 3:
                log(f"  probe {base}: {note}")

            if kind == "mp4":
                shutil.move(tmp, out_mp4)
                ok = True
            elif kind in ("zxvideo", "hevc-raw"):
                if not headers:
                    stats["no-headers"] = stats.get("no-headers", 0) + 1
                adts = b""
                if args.with_audio:
                    adts, _ = extract_adts(tmp)
                ok = convert(tmp, out_mp4, headers, off, adts, args.audio_offset)
            elif kind == "encrypted":
                # Not eufy-AES ciphertext we can break without the camera key.
                # Record it so the user can retrieve these another way.
                encrypted.append((remote, os.path.getsize(tmp), note))
                os.unlink(tmp)
                continue
            else:
                # A video-extension file we cannot identify: worth seeing, but
                # only a sample -- logging all of them would bury everything else.
                sig = open(tmp, "rb").read(8).hex()
                unknown_magic.setdefault(sig, []).append(remote)
                if len(unknown_magic[sig]) > 3:
                    unknown_magic[sig] = unknown_magic[sig][:3] + ["..."]
                os.unlink(tmp)
                continue

            os.unlink(tmp)  # keep disk usage flat

            if ok:
                # looks_corrupt already proves ffmpeg decoded real frames, which is
                # a stronger check than parsing metadata -- so only fall back to
                # a separate ffprobe spawn when it could not decode anything.
                bad = looks_corrupt(out_mp4) if args.reject_black else False
                if bad:
                    v = validate(out_mp4)
                    os.unlink(out_mp4)
                    stats["corrupt"] = stats.get("corrupt", 0) + 1
                    dims = f"{v[1]}x{v[2]}" if v else "unknown"
                    failures.append(
                        (remote, f"corrupt/black at {dims} (header mismatch?)")
                    )
                elif bad is None:
                    v = validate(out_mp4)
                    if not v:
                        os.unlink(out_mp4)
                        failures.append((remote, "no decodable frame"))
                    else:
                        stats["converted"] = stats.get("converted", 0) + 1
                else:
                    stats["converted"] = stats.get("converted", 0) + 1
                    if stats["converted"] <= 3:
                        v = validate(out_mp4)
                        if v:
                            log(f"  OK {base} -> {v[1]}x{v[2]} {v[0]}")
            else:
                os.path.exists(out_mp4) and os.unlink(out_mp4)
                failures.append((remote, "ffmpeg convert failed"))

        rate = done / max(time.time() - t0, 1e-9) * 60
        eta = (len(videos) - done) / max(rate, 1e-9)
        log(
            f"batch {bi}/{len(batches)}: {done}/{len(videos)} clips, "
            f"{batch_bytes / 1e9:.1f} GB this batch, {rate:.0f}/min, ETA {eta / 60:.1f}h  {stats}"
        )

    log("=" * 60)
    log(f"done. {stats}")

    if encrypted:
        manifest = os.path.join(args.out, "ENCRYPTED-clips-not-downloaded.csv")
        with open(manifest, "w", newline="") as fh:
            fh.write("bytes,card_path,reason\n")
            for remote, size, why in encrypted:
                fh.write(f"{size},{remote},{why}\n")
        log(
            f"{len(encrypted)} encrypted clips could not be decoded "
            f"(need eufy's AES key). Listed in:"
        )
        log(f"  {manifest}")
        log(f"  total {sum(s for _, s, _ in encrypted) / 1e9:.1f} GB not downloaded")

    if unknown_magic:
        log(
            f"unrecognised video files, {len(unknown_magic)} distinct magic signatures:"
        )
        for sig, ex in unknown_magic.items():
            log(f"  magic {sig}: e.g. {', '.join(x for x in ex[:3] if x != '...')}")
    if failures:
        log(f"{len(failures)} failures; first 15:")
        for r, why in failures[:15]:
            log(f"  {why}: {r}")


if __name__ == "__main__":
    main()
