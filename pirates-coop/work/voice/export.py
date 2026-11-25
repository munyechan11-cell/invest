"""Export TTS raws -> app/assets/audio/<id>.mp3 (trim to speech, loudnorm -16, adelay 100, mono 44.1k 96k).
usage: export.py id=src[:cut_end_sec] ...   (cut_end = last spoken word end in RAW seconds; omitted -> last voiced group)"""
import sys, subprocess, numpy as np, os
ROOT='/home/user/invest/pirates-coop'; OUT=f'{ROOT}/app/assets/audio'
def load(f, sr=16000):
    raw=subprocess.run(['ffmpeg','-v','error','-i',f,'-f','s16le','-ac','1','-ar',str(sr),'-'],capture_output=True).stdout
    return np.frombuffer(raw,np.int16).astype(np.float32)/32768
def groups(x, gap, thr=-34, sr=16000):
    w=sr//100; n=len(x)//w
    db=20*np.log10(np.sqrt((x[:n*w].reshape(n,w)**2).mean(1))+1e-9)
    g=[]; s=None; last=None
    for i,v in enumerate(db>thr):
        t=i*0.01
        if v:
            if s is None: s=t
            elif t-last>gap: g.append((s,last+0.01)); s=t
            last=t
    if s is not None: g.append((s,last+0.01))
    return g
def export(i, src, cut=None):
    x=load(src); g=groups(x,0.4)
    start=max(0,g[0][0]-0.05)
    end=(cut if cut else g[-1][1])+0.22
    end=min(end,len(x)/16000)
    d=end-start
    af=f"afade=t=in:d=0.02,afade=t=out:st={d-0.12:.3f}:d=0.12,loudnorm=I=-16:TP=-1.5:LRA=11,adelay=100:all=1"
    o=f'{OUT}/{i}.mp3'
    subprocess.run(['ffmpeg','-y','-v','error','-ss',f'{start:.3f}','-t',f'{d:.3f}','-i',src,'-af',af,'-ac','1','-ar','44100','-b:a','96k',o],check=True)
    dur=float(subprocess.run(['ffprobe','-v','error','-show_entries','format=duration','-of','csv=p=0',o],capture_output=True,text=True).stdout)
    print(f'{i}: raw {len(x)/16000:.1f}s -> {dur:.2f}s (start {start:.2f}, end {end:.2f})')
if __name__=='__main__':
    for a in sys.argv[1:]:
        i,r=a.split('=',1); p=r.split(':'); export(i,p[0],float(p[1]) if len(p)>1 else None)
