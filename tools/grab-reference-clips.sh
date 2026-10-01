#!/bin/bash
# Pull a raw clip + sidecar for comparison against an app-exported decode.
#
# Do this BEFORE returning the card to the camera - while the card is in the
# Mac it is readable; once it is back in the camera the Mac cannot read it at
# all. We pick recent footage (26-27 Sep) because the card is 94% full and eufy
# deletes oldest-first, so the newest clips are the safest to have on it.
set -u
S=/dev/rdisk8s1
O="/Volumes/Samsung T5/ref-test"
mkdir -p "$O" || exit 1

# (day_dir, stem) pairs - deliberately a mid-size and a larger clip
PAIRS=(
  "/Camera00/event/202609/20260926:20260926091116"
  "/Camera00/event/202609/20260927:20260927092310"
  "/Camera00/event/202609/20260926:20260926091027"
)

for pair in "${PAIRS[@]}"; do
  dir="${pair%%:*}"
  stem="${pair##*:}"
  echo "=== $stem ==="
  sudo e2cp "$S:$dir/$stem.txt"     "$O/$stem.txt"     && echo "  got sidecar"
  sudo e2cp "$S:$dir/$stem.zxvideo" "$O/$stem.zxvideo" && echo "  got clip"
done

echo
echo "=== on disk ==="
ls -la "$O"
echo
echo "=== sidecar facts ==="
for f in "$O"/*.txt; do
  [ -s "$f" ] || continue
  python3 - "$f" <<'PY'
import json, sys, os
for line in open(sys.argv[1]):
    line = line.strip()
    if not line:
        continue
    p = json.loads(line).get("payload", {})
    if p.get("res_best_width"):
        print(f"  {os.path.basename(sys.argv[1])[:-4]}: "
              f"{p['res_best_width']}x{p['res_best_height']} frames={p.get('frame_num')} "
              f"cipher={p.get('cipher_id')} mic={p.get('mic_status')} "
              f"{p.get('start_time')} -> {p.get('end_time')}")
PY
done

cat <<'NOTE'

------------------------------------------------------------
Now put the card back in the camera, then in the eufy app:

  1. Open the SD / local storage event list
  2. Find the clip matching one of the stems above
     (27 Sep around 09:23 is the easiest - it is the larger one)
  3. Play it. NOTE whether it plays at all.
  4. Use Share / Export / Save to phone to save it.

Then tell me the exported filename and we compare it against the raw bytes.
------------------------------------------------------------
NOTE