#!/bin/bash
# usage: make_loops.sh name=url ...   -> downloads raw 2K clip, writes 720p silent web loop to app/assets/loop/<name>.mp4
set -e
D=/home/user/invest/projectx-tv/work/loops; O=/home/user/invest/projectx-tv/app/assets/loop
for kv in "$@"; do n=${kv%%=*}; u=${kv#*=}
  curl -sS -o "$D/$n.raw.mp4" "$u"
  ffmpeg -v error -y -i "$D/$n.raw.mp4" -an -vf "scale=1280:720:flags=lanczos,fps=24,format=yuv420p" -c:v libx264 -preset slow -crf 27 -movflags +faststart "$O/$n.mp4"
  printf "%-10s %s %s\n" "$n" "$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$O/$n.mp4")" "$(du -h "$O/$n.mp4" | cut -f1)"
done
