"""Add Whisper (medium) word timestamps for more narration files into align_new.json (merge, not overwrite)."""
import sys, json, subprocess, numpy as np, os
from faster_whisper import WhisperModel
P = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'align_new.json')
out = json.load(open(P))
m = WhisperModel('medium', device='cpu', compute_type='int8')
def load(f):
    raw = subprocess.run(['ffmpeg','-v','error','-i',f,'-f','s16le','-ac','1','-ar','16000','-'],capture_output=True).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32)/32768
for f in sys.argv[1:]:
    segs,_ = m.transcribe(load(f), language='ko', word_timestamps=True, vad_filter=False)
    words=[{'w':w.word.strip(),'s':round(w.start,3),'e':round(w.end,3)} for s in segs for w in (s.words or [])]
    key=f.split('/')[-1].rsplit('.',1)[0]
    out[key]=words
    print(key, words[0]['s'] if words else None, words[-1]['e'] if words else None, ''.join(w['w'] for w in words), flush=True)
    json.dump(out, open(P,'w'), ensure_ascii=False, indent=0)
