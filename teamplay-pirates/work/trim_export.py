"""Export game-broadcast audio for the app: trim leading/trailing near-silence
(envelope below -36 dBFS over 50 ms windows), loudness-normalize, short fades."""
import subprocess, numpy as np, glob, os, sys
def pcm(f):
    raw=subprocess.run(['ffmpeg','-v','error','-i',f,'-f','s16le','-ac','1','-ar','16000','-'],capture_output=True).stdout
    return np.frombuffer(raw,np.int16).astype(np.float32)/32768
for f in sorted(glob.glob('tts_final/g_*.mp3')):
    a=pcm(f); w=800  # 50 ms
    rms=np.sqrt(np.convolve(a*a,np.ones(w)/w,'same'))+1e-9
    db=20*np.log10(rms); idx=np.where(db>-36)[0]
    s=max(0,idx[0]/16000-0.05); e=min(len(a)/16000,idx[-1]/16000+0.25)
    out='../app/assets/audio/'+os.path.basename(f)
    subprocess.run(['ffmpeg','-v','error','-y','-ss',f'{s:.3f}','-t',f'{e-s:.3f}','-i',f,'-af',
      f'afade=t=in:d=0.02,afade=t=out:st={e-s-0.15:.3f}:d=0.15,loudnorm=I=-16:TP=-1.5:LRA=11,adelay=120',
      '-ac','1','-ar','44100','-b:a','96k',out],check=True)
    d=float(subprocess.run(['ffprobe','-v','error','-show_entries','format=duration','-of','csv=p=0',out],capture_output=True,text=True).stdout)
    print(f'{os.path.basename(f):20s} src {len(a)/16000:5.2f}s  speech {s:5.2f}-{e:5.2f}  out {d:5.2f}s')
