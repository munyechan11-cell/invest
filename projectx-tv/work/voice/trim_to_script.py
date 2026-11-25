"""Cut each TTS file right after the scripted text ends (minimax sometimes appends babble),
then write Whisper word timings of the cut file to align.json for subtitle placement."""
import json, re, subprocess, numpy as np, glob, os
from faster_whisper import WhisperModel
m = WhisperModel('medium', device='cpu', compute_type='int8')
sc = json.load(open('script.json'))
syl = lambda s: len(re.findall(r'[가-힣A-Za-z0-9]', s))
def pcm(f):
    raw = subprocess.run(['ffmpeg','-v','error','-i',f,'-f','s16le','-ac','1','-ar','16000','-'],capture_output=True).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32)/32768
def words(f):
    segs,_ = m.transcribe(pcm(f), language='ko', word_timestamps=True)
    return [w for s in segs for w in (s.words or [])]
out = {}
for e, v in sc.items():
    for l in v['lines']:
        key = f"{e}_{l['id']}"; f = f'tts/{key}.mp3'
        if not os.path.exists(f'tts/{key}.raw.mp3'): os.rename(f, f'tts/{key}.raw.mp3')
        ws = words(f'tts/{key}.raw.mp3'); N = syl(l['tts']); acc = 0; end = ws[-1].end
        for w in ws:
            acc += syl(w.word)
            if acc >= N - 1: end = w.end; break
        subprocess.run(['ffmpeg','-v','error','-y','-i',f'tts/{key}.raw.mp3','-t',f'{end+0.3:.2f}',f],check=True)
        ws2 = words(f)
        out[key] = [{'w':w.word.strip(),'s':round(w.start,3),'e':round(w.end,3)} for w in ws2]
        print(f"{key}: cut {end:.2f}s of {ws[-1].end:.2f} | {''.join(w.word for w in ws2).strip()}")
json.dump(out, open('align.json','w'), ensure_ascii=False)
