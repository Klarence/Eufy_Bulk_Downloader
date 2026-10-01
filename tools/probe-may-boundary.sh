#!/bin/bash
# Test the handful of clips from 27-29 May 2026 to find the exact date the
# eufy per-frame container replaced the plain 4K stream.
#
# We know 20260527154217 is recoverable. 20260528 and 20260529 are the two
# clips that straddle the boundary - if either decodes, the switch happened on
# 30 May and everything up to then is still extractable.
set -u
S=/dev/rdisk8s1
O="/Volumes/Samsung T5/may-probe"
DBG=/opt/homebrew/opt/e2fsprogs/sbin/debugfs
REPO=/Users/klarenceouyang/GitHub/Eufy_Bulk_Downloader

mkdir -p "$O" || exit 1
cd "$REPO" || exit 1

CLIPS=(
  /Camera00/event/202605/20260527/20260527153944
  /Camera00/event/202605/20260527/20260527210524
  /Camera00/event/202605/20260528/20260528205536
  /Camera00/event/202605/20260529/20260529205107
  /Camera00/event/202605/20260530/20260530064711
)

for base in "${CLIPS[@]}"; do
  stem=$(basename "$base")
  echo "=== $stem ==="
  sudo e2cp "$S:$base.txt" "$O/$stem.txt" 2>/dev/null && echo -n "" || echo "  (no sidecar)"
  sudo e2cp "$S:$base.zxvideo" "$O/$stem.zxvideo" 2>/dev/null || { echo "  COPY FAILED"; continue; }
  if [ -s "$O/$stem.txt" ]; then
    python3 - "$O/$stem.txt" <<'PY'
import json, sys
for line in open(sys.argv[1]):
    line = line.strip()
    if not line:
        continue
    p = json.loads(line).get("payload", {})
    if p.get("res_best_width"):
        print(f"  sidecar: {p['res_best_width']}x{p['res_best_height']} "
              f"frames={p.get('frame_num')} cipher={p.get('cipher_id')} "
              f"{p.get('start_time')} -> {p.get('end_time')}")
PY
  fi
  echo -n "  4K headers : "
  python3 tools/verify-headers.py tools/hevc_params_4k_from_cam.bin "$O/$stem.zxvideo" 2>/dev/null | grep -E "^VERDICT|^decoded" | tr '\n' ' '
  echo
  echo -n "  1080p hdrs : "
  python3 tools/verify-headers.py tools/cam_1080p_params.bin "$O/$stem.zxvideo" 2>/dev/null | grep -E "^VERDICT|^decoded" | tr '\n' ' '
  echo
  echo
done
