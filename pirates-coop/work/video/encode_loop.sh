#!/bin/bash
# encode_loop.sh name  -> app/assets/loop/name.mp4  (720p, no audio, web-friendly, ~6.6 s seamless loop)
set -e
N=$1; D=/home/user/invest/pirates-coop
ffmpeg -v error -y -i $D/work/video/raw/$N.mp4 -an -vf "scale=1280:720:flags=lanczos,fps=24" -c:v libx264 -preset medium -crf 27 -maxrate 1400k -bufsize 2800k -pix_fmt yuv420p -movflags +faststart $D/app/assets/loop/$N.mp4
printf "%-8s %s KB\n" $N $(( $(stat -c%s $D/app/assets/loop/$N.mp4)/1024 ))
