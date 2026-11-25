"""Assemble the opening video: 10 generated 10-second blocks + narration + burned Korean subtitles.

Timing rules
- Each block is exactly BLOCK seconds; one narration line lives inside its own block,
  centred on the measured speech span (Whisper word timestamps in align.json).
- Subtitle chunk boundaries come from the script's line breaks; each boundary time is the
  end time of the Whisper word at which the cumulative Hangul-syllable share of the
  transcript reaches the chunk's share of the script. No time-stretching anywhere.
Outputs: out/intro_1080.mp4 (master), out/intro_720.mp4 (<= ~13 MB for the web app), out/intro.srt
"""
import json, re, subprocess, os, sys
KEY, TITLE_TXT, TITLE_IMG, OUTNAME, MUSIC = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]

W = os.path.dirname(os.path.abspath(__file__))
BLOCK = 10.0
TITLE = 4.0
script = json.load(open(f'{W}/script.json'))[KEY]
align = {k.split('_',1)[1]:v for k,v in json.load(open(f'{W}/align.json')).items() if k.startswith(KEY+'_')}
lines = {l['id']: l for l in script['lines']}
# script text overrides that changed after the first TTS pass
ids = ['b01', 'b02']
os.makedirs(f'{W}/out', exist_ok=True)

syl = lambda s: len(re.findall(r'[가-힣A-Za-z0-9]', s))

def chunk_times(words, chunks):
    """Return [(start,end)] per chunk using cumulative syllable share."""
    wl = [(syl(w['w']), w['s'], w['e']) for w in words]
    total_w = sum(n for n, _, _ in wl) or 1
    total_c = sum(syl(c) for c in chunks) or 1
    sp_start, sp_end = words[0]['s'], words[-1]['e']
    bounds = [sp_start]
    acc_c = 0
    for c in chunks[:-1]:
        acc_c += syl(c)
        target = acc_c / total_c * total_w
        acc_w = 0
        t = sp_end
        for n, s, e in wl:
            acc_w += n
            if acc_w >= target - 0.5:
                t = e
                break
        bounds.append(t)
    bounds.append(sp_end)
    return [(bounds[i], bounds[i + 1]) for i in range(len(chunks))]

def ts_ass(t):
    h = int(t // 3600); m = int(t % 3600 // 60); s = t % 60
    return f'{h}:{m:02d}:{s:05.2f}'

def ts_srt(t):
    ms = int(round(t * 1000)); h, ms = divmod(ms, 3600000); m, ms = divmod(ms, 60000); s, ms = divmod(ms, 1000)
    return f'{h:02d}:{m:02d}:{s:02d},{ms:03d}'

events, srt, offsets = [], [], {}
for i, bid in enumerate(ids):
    words = align[bid]
    sp_end = words[-1]['e']
    off = min(1.0, max(0.2, (BLOCK - sp_end) / 2))
    offsets[bid] = off
    base = i * BLOCK + off
    chunks = [c for c in lines[bid]['sub'].split('\n') if c.strip()]
    style = 'Narr'
    for k, ((s, e), text) in enumerate(zip(chunk_times(words, chunks), chunks)):
        start = base + s - (0.08 if k == 0 else 0.0)
        end = base + (e + 0.25 if k == len(chunks) - 1 else e)
        events.append(f'Dialogue: 0,{ts_ass(start)},{ts_ass(end)},{style},,0,0,0,,{text}')
        srt.append((start, end, text))

title_t0 = len(ids) * BLOCK
events.append(f'Dialogue: 1,{ts_ass(title_t0+0.4)},{ts_ass(title_t0+TITLE)},Title,,0,0,0,,{{\\fad(500,400)}}'+TITLE_TXT+'')
events.append(f'Dialogue: 1,{ts_ass(title_t0+1.1)},{ts_ass(title_t0+TITLE)},TitleSub,,0,0,0,,{{\\an8\\pos(960,640)\\fad(400,400)}}PROJECT X · 한동 팀플')

ass = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Narr,Noto Sans KR,66,&H00FFFFFF,&H00FFFFFF,&H00101010,&H96000000,-1,0,0,0,100,100,0,0,1,4,1.5,2,120,120,64,1
Style: Villain,Noto Sans KR,66,&H00A9C8FF,&H00FFFFFF,&H00081040,&H96000000,-1,0,0,0,100,100,0,0,1,4,1.5,2,120,120,64,1
Style: Title,Black Han Sans,170,&H00FFFFFF,&H00FFFFFF,&H00101010,&H96000000,0,0,0,0,100,100,2,0,1,6,3,5,80,80,40,1
Style: TitleSub,Noto Sans KR,58,&H00FFFFFF,&H00FFFFFF,&H00101010,&H96000000,-1,0,0,0,100,100,4,0,1,3,2,2,80,80,250,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
""" + '\n'.join(events) + '\n'
open(f'{W}/out/{KEY}.ass', 'w').write(ass)
with open(f'{W}/out/{KEY}.srt', 'w') as f:
    for n, (s, e, t) in enumerate(srt, 1):
        f.write(f'{n}\n{ts_srt(s)} --> {ts_srt(e)}\n{t}\n\n')

# ---------------- ffmpeg graph ----------------
inputs, fc = [], []
for i, bid in enumerate(ids):
    inputs += ['-i', f'{W}/vid/{KEY}_{bid}.mp4']
for bid in ids:
    inputs += ['-i', f'{W}/tts/{KEY}_{bid}.mp3']
inputs += ['-loop', '1', '-t', str(TITLE), '-i', TITLE_IMG]
inputs += ['-i', MUSIC]
n = len(ids)
for i in range(n):
    fc.append(f'[{i}:v]trim=0:{BLOCK},setpts=PTS-STARTPTS,scale=1920:1080:flags=lanczos,fps=24,format=yuv420p[v{i}]')
    fc.append(f'[{i}:a]atrim=0:{BLOCK},asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo,'
              f'afade=t=in:d=0.15,afade=t=out:st={BLOCK-0.2}:d=0.2,volume=0.0[ba{i}]')
ti = 2 * n
fc.append(f'[{ti}:v]scale=2400:-1,zoompan=z=\'min(zoom+0.0009,1.12)\':x=\'iw/2-(iw/zoom/2)\':y=\'ih/2-(ih/zoom/2)\':d={int(TITLE*24)}:s=1920x1080:fps=24,'
          f'eq=brightness=-0.06,format=yuv420p,trim=0:{TITLE},setpts=PTS-STARTPTS,fade=t=out:st={TITLE-0.6}:d=0.6[vt]')
fc.append(f'anoisesrc=color=pink:amplitude=0.08:d={TITLE}:r=48000,lowpass=f=900,aformat=channel_layouts=stereo,'
          f'afade=t=in:d=1,afade=t=out:st={TITLE-1.5}:d=1.5,volume=0.0[bat]')
fc.append(''.join(f'[v{i}]' for i in range(n)) + '[vt]' + f'concat=n={n+1}:v=1:a=0[vcat]')
fc.append(''.join(f'[ba{i}]' for i in range(n)) + '[bat]' + f'concat=n={n+1}:v=0:a=1[bed]')
narr = []
for i, bid in enumerate(ids):
    d = int(round((i * BLOCK + offsets[bid]) * 1000))
    fc.append(f'[{n+i}:a]aresample=48000,aformat=channel_layouts=stereo,loudnorm=I=-16:TP=-1.5:LRA=11,adelay={d}|{d}[n{i}]')
    narr.append(f'[n{i}]')
fc.append(''.join(narr) + f'amix=inputs={n}:normalize=0:duration=longest[narr]')
fc.append('[narr]asplit=2[narr1][narrsc]')
TOT = n*BLOCK + TITLE
fc.append(f"[{2*n+1}:a]aresample=48000,aformat=channel_layouts=stereo,atrim=0:{TOT},asetpts=PTS-STARTPTS,afade=t=in:d=0.8,afade=t=out:st={TOT-2.0}:d=2.0,volume=0.6[mus]")
fc.append('[bed][mus]amix=inputs=2:normalize=0:duration=longest[bed2]')
fc.append('[bed2][narrsc]sidechaincompress=threshold=0.03:ratio=5:attack=20:release=500[bedd]')
fc.append('[bedd][narr1]amix=inputs=2:normalize=0:duration=first,loudnorm=I=-16:TP=-1.5:LRA=11[aout]')
fc.append(f"[vcat]subtitles={W}/out/{KEY}.ass:fontsdir=/home/user/invest/teamplay-pirates/work/fonts[vout]")
graph = ';\n'.join(fc)
open(f'{W}/out/graph.txt', 'w').write(graph)
master = f'{W}/out/{OUTNAME}_music.mp4'
cmd = ['ffmpeg', '-v', 'error', '-y'] + inputs + ['-filter_complex', graph, '-map', '[vout]', '-map', '[aout]',
       '-c:v', 'libx264', '-preset', 'slow', '-crf', '24', '-pix_fmt', 'yuv420p', '-r', '24',
       '-c:a', 'aac', '-b:a', '160k', '-ar', '48000', '-movflags', '+faststart', master]
subprocess.run(cmd, check=True)
print('offsets', offsets)
