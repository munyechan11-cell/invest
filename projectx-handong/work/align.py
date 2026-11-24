"""Word-level timestamps for each narration file (Whisper medium), saved as JSON.
Subtitle chunk boundaries are later placed by matching cumulative Hangul syllable
counts of the script against these measured word timings (no guessed timing)."""
import sys, json, subprocess, numpy as np, re
from faster_whisper import WhisperModel
m = WhisperModel('medium', device='cpu', compute_type='int8')
def load(f):
    raw = subprocess.run(['ffmpeg','-v','error','-i',f,'-f','s16le','-ac','1','-ar','16000','-'],capture_output=True).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32)/32768
out = {}
for f in sys.argv[1:]:
    segs,_ = m.transcribe(load(f), language='ko', word_timestamps=True, vad_filter=False)
    words=[{'w':w.word.strip(),'s':round(w.start,3),'e':round(w.end,3)} for s in segs for w in (s.words or [])]
    key=f.split('/')[-1].rsplit('.',1)[0]
    out[key]=words
    print(key, words[0]['s'] if words else None, words[-1]['e'] if words else None, ''.join(w['w'] for w in words))
json.dump(out, open('align.json','w'), ensure_ascii=False, indent=0)
