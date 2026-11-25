"""Build intro.mp4 and the three ending videos for the cooperative pirate version.

Inputs
- v1 pirate opening blocks b01..b10 (10 s each, native audio is dropped: it contains hallucinated speech)
- narration: v1 b01..b08 (unchanged) + new b09/b10 (tts/b09.mp3, tts/b10.mp3) + endings (tts/e1..e3.mp3)
- loops in app/assets/loop, music in app/assets/music
- Whisper word times (align_new.json for new lines, v1 align.json for b01..b08)

Usage: python3 build_media.py intro|e1|e2|e3
Timing rules: subtitle chunk edges come from measured word times (cumulative Hangul-syllable share),
no time stretching of speech. Music is ducked under narration (sidechain), final loudnorm.
"""
import json, re, subprocess, sys, os

W = os.path.dirname(os.path.abspath(__file__))
A = os.path.abspath(f'{W}/../../app/assets')
V1 = '/home/user/invest/teamplay-pirates/work'
FONTS = f'{V1}/fonts'
align_v1 = json.load(open(f'{V1}/align.json'))
align_new = json.load(open(f'{W}/align_new.json'))

syl = lambda s: len(re.findall(r'[가-힣A-Za-z0-9]', s))

INTRO_LINES = {
    'b01': ('v1', f'{V1}/tts_final/b01.mp3', "이번 학기 마지막 과제.\n거친 팀플의 바다 위로, 세 척의 배가 동시에 출항했다.\n목적지는 '종강항'.", 'Narr'),
    'b02': ('new', f'{W}/tts/408.mp3', "배의 이름은 '팀플호'.\n네 번의 도전을 모두 함께 넘어야,\n'종강항'에 도착할 수 있다.", 'Narr'),
    'b03': ('v1', f'{V1}/tts_final/b03.mp3', "그런데 출항 전날 밤,\n불 꺼진 강의실에 수상한 그림자가 나타났다.\n학점을 노리는 해적, 캡틴 F.", 'Narr'),
    'b04': ('v1', f'{V1}/tts_final/b04.mp3', "상대평가라는 게 말이야…\n남이 망하면, 네가 올라가는 거거든. 크크크.", 'Villain'),
    'b05': ('v1', f'{V1}/tts_final/b05.mp3', "캡틴 F는 각 팀에서 딱 한 명씩을 몰래 불러냈다.\n\"팀플을 망쳐라. 그러면 A+를 주마.\"", 'Narr'),
    'b06': ('v1', f'{V1}/tts_final/b06.mp3', "그렇게 세 척의 배에 한 명씩,\n아무도 모르게 해적이 올라탔다.\n우리는 그들을 'X맨'이라 부른다.", 'Narr'),
    'b07': ('v1', f'{V1}/tts_final/b07.mp3', "겉으로는 성실한 팀원.\n자료를 찾는 척, 회의에 나오는 척.\n하지만 결정적인 순간, 몰래 배에 구멍을 낸다.", 'Narr'),
    'b08': ('new', f'{W}/tts/407.mp3', "항해는 네 번의 도전으로 이루어진다.\n자료조사, 갑판 집합, 돛 수리, 그리고 입항 보고.", 'Narr'),
    'b09': ('new', f'{W}/tts/401.mp3', "구멍이 난 배는 가라앉는다. 하지만 이번에는 달라.\n각자의 배마다 숨어든 X맨 한 명을,\n모두 함께 힘을 모아 이겨내는 거야.", 'Narr'),
    'b10': ('new', f'{W}/tts/406.mp3', "선원들이여, 협동하라!\n자료조사, 갑판 집합, 돛 수리, 입항 보고.\n마지막에 X맨을 지목하라.\n팀플호, 출항합니다!", 'Narr'),
}
END_LINES = {
    'e1': (f'{W}/tts/403.mp3', "크하하하!\n아무도 내 X맨을 알아채지 못했군!\n이번 학기는 우리 해적단의 승리다!", 'Villain', 'tense', ['night', 'sink']),
    'e2': (f'{W}/tts/404.mp3', "X맨은 일부만 들켰다.\n배들은 간신히 항구에 도착했지만,\n아직 해적의 그림자가 남아 있다.", 'Narr', 'calm', ['sea', 'deck']),
    'e0': (f'{W}/tts/409.mp3', "X맨은 끝까지 숨었고,\n항해마저 무너졌다.\n세 척의 배는 안개 속으로 가라앉았다.\n바다에는 해적 깃발만 남았다.", 'Narr', 'tense', ['sea', 'sink']),
    'e2a': (f'{W}/tts/410.mp3', "X맨 한 명만 들켰다.\n한 척만 겨우 항구에 닿았고,\n나머지 배는 아직 바다 위에서\n해적과 싸우고 있다.", 'Narr', 'calm', ['night', 'arrive']),
    'e4': (f'{W}/tts/411.mp3', "X맨이 모두 들켰고,\n항해마저 완벽했다.\n세 척은 나란히 종강항에 입항했다.\n이 바다의 전설이 될 선원들이다.", 'Narr', 'play', ['harbor', 'arrive']),
    'e3': (f'{W}/tts/405.mp3', "세 명의 X맨이 모두 들켰다.\n선원들은 힘을 모아 배를 지켜냈고,\n팀플호는 종강항에 무사히 도착했다.", 'Narr', 'play', ['deck', 'arrive']),
}

def words_for(key, src):
    return (align_v1[key] if src == 'v1' else align_new[key])

def chunk_times(words, chunks):
    wl = [(syl(w['w']), w['s'], w['e']) for w in words]
    total_w = sum(n for n, _, _ in wl) or 1
    total_c = sum(syl(c) for c in chunks) or 1
    sp_start, sp_end = words[0]['s'], words[-1]['e']
    bounds, acc_c = [sp_start], 0
    for c in chunks[:-1]:
        acc_c += syl(c)
        target = acc_c / total_c * total_w
        acc_w, t = 0, sp_end
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

ASS_HEAD = """[Script Info]
ScriptType: v4.00+
PlayResX: 1280
PlayResY: 720
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Narr,Noto Sans KR,44,&H00FFFFFF,&H00FFFFFF,&H00101010,&H96000000,-1,0,0,0,100,100,0,0,1,3,1,2,80,80,44,1
Style: Villain,Noto Sans KR,44,&H00A9C8FF,&H00FFFFFF,&H00081040,&H96000000,-1,0,0,0,100,100,0,0,1,3,1,2,80,80,44,1
Style: Title,Black Han Sans,118,&H00FFFFFF,&H00FFFFFF,&H00101010,&H96000000,0,0,0,0,100,100,2,0,1,5,2,5,60,60,30,1
Style: TitleSub,Noto Sans KR,40,&H00FFFFFF,&H00FFFFFF,&H00101010,&H96000000,-1,0,0,0,100,100,4,0,1,3,1,2,60,60,150,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

def sub_events(words, text, style, base):
    chunks = [c for c in text.split('\n') if c.strip()]
    ev = []
    for k, ((s, e), c) in enumerate(zip(chunk_times(words, chunks), chunks)):
        start = base + s - (0.08 if k == 0 else 0)
        end = base + (e + 0.3 if k == len(chunks) - 1 else e)
        ev.append(f'Dialogue: 0,{ts_ass(start)},{ts_ass(end)},{style},,0,0,0,,{c}')
    return ev

def dur(f):
    return float(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', f]))

def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        print(r.stderr[-2500:]); sys.exit(1)

def mix_and_burn(video_in, narr, music, total, ass_path, out):
    """narr: list of (file, start_sec). Video in is already total-length. Music loops, ducked under narration, fades out."""
    inputs = ['-i', video_in, '-stream_loop', '-1', '-i', music]
    for f, _ in narr:
        inputs += ['-i', f]
    parts = []
    for i, (f, t) in enumerate(narr):
        ms = int(t * 1000)
        parts.append(f'[{i+2}:a]aformat=sample_rates=44100:channel_layouts=stereo,adelay={ms}|{ms},apad[n{i}]')
    n = len(narr)
    parts.append(''.join(f'[n{i}]' for i in range(n)) + f'amix=inputs={n}:duration=longest:normalize=0,atrim=0:{total},asetpts=PTS-STARTPTS[narr]')
    parts.append('[narr]asplit=2[n1][n2]')
    parts.append(f'[1:a]aformat=sample_rates=44100:channel_layouts=stereo,atrim=0:{total},asetpts=PTS-STARTPTS,volume=0.9[m]')
    parts.append('[m][n1]sidechaincompress=threshold=0.02:ratio=9:attack=30:release=500[duck]')
    parts.append(f'[duck][n2]amix=inputs=2:duration=first:normalize=0,afade=t=in:d=0.8,afade=t=out:st={total-1.6}:d=1.6,loudnorm=I=-16:TP=-1.5:LRA=9[aout]')
    fc = ';'.join(parts)
    vf = f"ass={ass_path}:fontsdir={FONTS},fade=t=in:d=0.5,fade=t=out:st={total-1.0}:d=1.0"
    run(['ffmpeg', '-v', 'error', '-y', *inputs, '-filter_complex', fc, '-vf', vf, '-map', '0:v', '-map', '[aout]',
         '-t', f'{total}', '-c:v', 'libx264', '-preset', 'medium', '-crf', '25', '-maxrate', '1500k', '-bufsize', '3000k',
         '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '128k', '-movflags', '+faststart', out])

def build_intro():
    LEAD = 0.35
    seg_files, narr, events, t0 = [], [], [], 0.0
    os.makedirs(f'{W}/tmp', exist_ok=True)
    for bid, (src, f, text, style) in INTRO_LINES.items():
        words = words_for(bid if src == 'v1' else {'b02': '408', 'b08': '407', 'b09': '401', 'b10': '406'}[bid], src)
        sp_end = words[-1]['e']
        need = LEAD + sp_end + 0.9
        blk = f'{V1}/vid/{bid}.mp4'
        seg = f'{W}/tmp/{bid}.mp4'
        if need <= 10.0:
            seg_dur = 10.0
            vf = 'scale=1280:720:flags=lanczos,fps=24'
        elif bid == 'b09':
            seg_dur = round(need, 2)
            vf = f'setpts={seg_dur/10.0:.4f}*PTS,scale=1280:720:flags=lanczos,fps=24'
        else:  # b10: 10 s block + harbor loop tail so the last line is not cut
            seg_dur = round(need, 2)
            vf = 'scale=1280:720:flags=lanczos,fps=24'
        run(['ffmpeg', '-v', 'error', '-y', '-i', blk, '-an', '-vf', vf, '-t', str(seg_dur), '-c:v', 'libx264', '-preset', 'fast', '-crf', '20', '-pix_fmt', 'yuv420p', seg])
        if bid == 'b10':
            tail_len = seg_dur - 10.0 + 0.6
            tail = f'{W}/tmp/b10_tail.mp4'
            run(['ffmpeg', '-v', 'error', '-y', '-stream_loop', '-1', '-i', f'{A}/loop/harbor.mp4', '-an', '-t', f'{tail_len:.2f}',
                 '-vf', 'fps=24', '-c:v', 'libx264', '-preset', 'fast', '-crf', '20', '-pix_fmt', 'yuv420p', tail])
            joined = f'{W}/tmp/b10_full.mp4'
            run(['ffmpeg', '-v', 'error', '-y', '-i', seg, '-i', tail, '-filter_complex',
                 f'[0:v][1:v]xfade=transition=fade:duration=0.6:offset=9.4[v]', '-map', '[v]', '-c:v', 'libx264', '-preset', 'fast', '-crf', '20', '-pix_fmt', 'yuv420p', joined])
            seg = joined
            seg_dur = dur(joined)
        seg_files.append(seg)
        narr.append((f, t0 + LEAD))
        events += sub_events(words, text, style, t0 + LEAD)
        t0 += seg_dur
    total = t0
    # title card over the harbor tail
    events.append(f'Dialogue: 1,{ts_ass(total-3.6)},{ts_ass(total-0.6)},Title,,0,0,0,,{{\\fad(500,400)}}팀플호에 해적이 탔다')
    events.append(f'Dialogue: 1,{ts_ass(total-3.0)},{ts_ass(total-0.6)},TitleSub,,0,0,0,,{{\\fad(400,400)}}X맨을 찾아라 · 협동 항해판')
    ass = f'{W}/tmp/intro.ass'
    open(ass, 'w', encoding='utf-8').write(ASS_HEAD + '\n'.join(events) + '\n')
    lst = f'{W}/tmp/intro_list.txt'
    open(lst, 'w').write(''.join(f"file '{s}'\n" for s in seg_files))
    joined_all = f'{W}/tmp/intro_video.mp4'
    run(['ffmpeg', '-v', 'error', '-y', '-f', 'concat', '-safe', '0', '-i', lst, '-c', 'copy', joined_all])
    total = dur(joined_all)
    mix_and_burn(joined_all, narr, f'{A}/music/calm.mp3', total, ass, f'{A}/intro.mp4')
    print('intro', round(total, 1), 's', os.path.getsize(f'{A}/intro.mp4') // 1024, 'KB')

def build_end(key):
    f, text, style, music, loops = END_LINES[key]
    num = {'e1': '403', 'e2': '404', 'e3': '405', 'e0': '409', 'e2a': '410', 'e4': '411'}[key]
    words = align_new[num]
    LEAD = 0.5
    total = round(LEAD + words[-1]['e'] + 1.6, 2)
    # two loops cross-faded, long enough for total
    d = [dur(f'{A}/loop/{n}.mp4') for n in loops]
    XF = 0.8 if d[0] + d[1] - 0.8 >= total - 0.3 else 0.4   # 내레이션이 길면 이음 구간을 줄여 영상 길이를 확보
    off = d[0] - XF
    joined = f'{W}/tmp/{key}_video.mp4'
    run(['ffmpeg', '-v', 'error', '-y', '-i', f'{A}/loop/{loops[0]}.mp4', '-i', f'{A}/loop/{loops[1]}.mp3'.replace('.mp3', '.mp4'),
         '-filter_complex', f'[0:v][1:v]xfade=transition=fade:duration={XF}:offset={off:.2f}[v]', '-map', '[v]', '-t', f'{total}',
         '-c:v', 'libx264', '-preset', 'fast', '-crf', '22', '-pix_fmt', 'yuv420p', joined])
    assert dur(joined) >= total - 0.3, (dur(joined), total)
    out_name = {'e1': 'e1_pirate_win', 'e2': 'e2_partial', 'e3': 'e3_perfect', 'e0': 'e0_sunk', 'e2a': 'e2a_one', 'e4': 'e4_legend'}[key]
    ass = f'{W}/tmp/{key}.ass'
    open(ass, 'w', encoding='utf-8').write(ASS_HEAD + '\n'.join(sub_events(words, text, style, LEAD)) + '\n')
    os.makedirs(f'{A}/end', exist_ok=True)
    mix_and_burn(joined, [(f, LEAD)], f'{A}/music/{music}.mp3', total, ass, f'{A}/end/{out_name}.mp4')
    print(out_name, total, 's', os.path.getsize(f'{A}/end/{out_name}.mp4') // 1024, 'KB')

if __name__ == '__main__':
    what = sys.argv[1]
    if what == 'intro':
        build_intro()
    else:
        build_end(what)
