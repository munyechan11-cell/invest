#!/bin/bash
# make_loop.sh in.mp3 out.mp3 [crossfade_sec]
# Seamless loop: body = a[X:L-X] + (tail a[L-X:L] fading out  mixed with  head a[0:X] fading in).
# When the file wraps, the end (blend) continues straight into a[X:...], which is exactly what followed a[0:X].
set -e
IN=$1; OUT=$2; X=${3:-4}; CUT=${4:-0}
L=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$IN")
if [ "$CUT" != "0" ]; then L=$CUT; fi
python3 - "$IN" "$OUT" "$X" "$L" <<'PY'
import subprocess, sys
i,o,X,L = sys.argv[1], sys.argv[2], float(sys.argv[3]), float(sys.argv[4])
fc = (f"[0:a]atrim={X}:{L-X},asetpts=PTS-STARTPTS[body];"
      f"[0:a]atrim={L-X}:{L},asetpts=PTS-STARTPTS,afade=t=out:d={X}:curve=qsin[tail];"
      f"[0:a]atrim=0:{X},asetpts=PTS-STARTPTS,afade=t=in:d={X}:curve=qsin[head];"
      f"[tail][head]amix=inputs=2:duration=longest:normalize=0[blend];"
      f"[body][blend]concat=n=2:v=0:a=1,loudnorm=I=-22:TP=-3:LRA=7[out]")
subprocess.run(['ffmpeg','-v','error','-y','-t',str(L),'-i',i,'-filter_complex',fc,'-map','[out]','-ac','2','-ar','44100','-b:a','112k',o],check=True)
PY
printf "%-20s -> %-18s %ss\n" "$IN" "$OUT" "$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$OUT")"
