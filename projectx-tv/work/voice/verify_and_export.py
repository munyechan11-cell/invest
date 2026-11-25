"""For every narration line: cut right after the scripted text ends (TTS sometimes appends babble),
compare the Whisper transcript of the cut file to the script, then trim leading/trailing silence,
loudness-normalize and write the web mp3 to app/assets/audio/v_<id>.mp3."""
import json, re, subprocess, numpy as np, os
from faster_whisper import WhisperModel
m = WhisperModel('medium', device='cpu', compute_type='int8')
lines = json.load(open('script.json'))['v']['lines']
syl = lambda s: len(re.findall(r'[가-힣A-Za-z0-9]', s))
def pcm(f):
    raw = subprocess.run(['ffmpeg','-v','error','-i',f,'-f','s16le','-ac','1','-ar','16000','-'],capture_output=True).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32)/32768
def words(f):
    segs,_ = m.transcribe(pcm(f), language='ko', word_timestamps=True)
    return [w for s in segs for w in (s.words or [])]
OUT = '../../app/assets/audio'
report = []
for l in lines:
    k = l['id']; f = f'tts/{k}.mp3'; raw = f'tts/{k}.raw.mp3'
    if not os.path.exists(raw): os.rename(f, raw)
    ws = words(raw); N = syl(l['tts']); acc = 0; end = ws[-1].end if ws else 1.0
    for w in ws:
        acc += syl(w.word)
        if acc >= N - 1: end = w.end; break
    subprocess.run(['ffmpeg','-v','error','-y','-i',raw,'-t',f'{end+0.3:.2f}',f],check=True)
    heard = ''.join(w.word for w in words(f)).replace(' ','')
    a = pcm(f); w_ = 800; rms = np.sqrt(np.convolve(a*a, np.ones(w_)/w_, 'same'))+1e-9
    idx = np.where(20*np.log10(rms) > -38)[0]
    s0 = max(0, idx[0]/16000-0.05); e0 = min(len(a)/16000, idx[-1]/16000+0.2)
    out = f'{OUT}/v_{k}.mp3'
    subprocess.run(['ffmpeg','-v','error','-y','-ss',f'{s0:.3f}','-t',f'{e0-s0:.3f}','-i',f,'-af',
        f'afade=t=in:d=0.02,afade=t=out:st={max(0,e0-s0-0.12):.3f}:d=0.12,loudnorm=I=-16:TP=-1.5:LRA=11,adelay=100',
        '-ac','1','-ar','44100','-b:a','96k',out],check=True)
    dur = float(subprocess.run(['ffprobe','-v','error','-show_entries','format=duration','-of','csv=p=0',out],capture_output=True,text=True).stdout)
    want = re.sub(r'[^가-힣A-Za-z0-9]','',l['tts'])
    got = re.sub(r'[^가-힣A-Za-z0-9]','',heard)
    report.append((k, round(dur,1), got, want))
    print(f"{k:12s} {dur:5.1f}s | heard: {heard}")
json.dump(report, open('report.json','w'), ensure_ascii=False)
