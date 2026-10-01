#!/bin/bash
# Fetch one encrypted/1080p card clip and test whether the camera's 1080p
# parameter sets decode it. Used to tell "different resolution" apart from
# "actually encrypted".
set -u
REPO=/Users/klarenceouyang/GitHub/Eufy_Bulk_Downloader
S=/dev/rdisk8s1
DIR=/Camera00/event/202605/20260530
STEM=20260530200842
O="/Volumes/Samsung T5/enc-test"
DBG=/opt/homebrew/opt/e2fsprogs/sbin/debugfs

mkdir -p "$O" || exit 1
cd "$REPO" || exit 1

echo "=== 1. is the clip still on the card? ==="
sudo "$DBG" -R "ls -l $DIR" "$S" 2>&1 | grep -v '^debugfs' | head -15

echo
echo "=== 2. sidecar + clip ==="
sudo e2cp -v "$S:$DIR/$STEM.txt" "$O/" 2>&1 | tail -4
sudo e2cp -v "$S:$DIR/$STEM.zxvideo" "$O/" 2>&1 | tail -4

echo
echo "=== 3. what landed ==="
ls -la "$O"

echo
if [ -s "$O/$STEM.zxvideo" ]; then
  echo "=== 4. sidecar facts ==="
  python3 - "$O/$STEM.txt" <<'PY'
import json, sys
for line in open(sys.argv[1]):
    line = line.strip()
    if not line:
        continue
    p = json.loads(line).get("payload", {})
    for k in ("res_best_width", "res_best_height", "cipher_id", "frame_num",
              "mic_status", "aes"):
        if k in p:
            print(f"  {k:18} = {p[k]!r}")
PY
  echo
  echo "=== 5. test with 1080p headers ==="
  python3 tools/verify-headers.py tools/cam_1080p_params.bin "$O/$STEM.zxvideo" \
    --png "$O/probe.png"
else
  echo "clip missing - the copy failed; see the e2cp output above"
fi