import base64
import hashlib
import json
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
import time

import httpx
import imageio_ffmpeg

ROOT=Path(__file__).resolve().parent
OUTPUT=ROOT/'test-results'
OUTPUT.mkdir(exist_ok=True)
REMOTE='/data/2027nco/service'
HOST='ncoai@202.112.51.208'
CID='FAULT-'+str(int(time.time()))
results=[]


def check(name,valid):
    assert valid,name
    results.append(name)
    print('PASS',name,flush=True)


def remote(*args):
    command=['docker','compose','-f',REMOTE+'/compose.yaml','--project-directory',REMOTE,*args]
    subprocess.run(['ssh',HOST,shlex.join(command)],check=True,stdout=subprocess.DEVNULL,timeout=90)


def wait_for(function,seconds=60):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        try:
            if function():return
        except (httpx.HTTPError,KeyError,ValueError):pass
        time.sleep(1)
    raise AssertionError('Wait timed out')


def main():
    admin=httpx.Client(base_url='https://127.0.0.1:18443',verify=str(ROOT/'nco-private-ca.crt'),trust_env=False,timeout=30)
    response=admin.post('/api/auth/login',json={'username':'admin','password':(ROOT/'.admin-password').read_text().strip()})
    response.raise_for_status()
    admin.headers['X-CSRF-Token']=response.json()['csrf']
    def post(path,body={}):
        r=admin.post('/api/'+path,json=body);r.raise_for_status();return r.json()
    post('contestants',{'id':CID,'name':'故障恢复验收','group_name':'FAULT 故障验收'})
    key=post('contestants/'+CID+'/key')
    samples=admin.get('/api/media?kind=original&limit=200').json()['items']
    sample=next(m for m in samples if m['state']=='checked' and m['report'].get('x264_options') and 'rc=crf' in m['report']['x264_options'])
    data=admin.get('/api/media/'+sample['id']+'/download').content
    fixture=OUTPUT/'fault-original.mkv'
    fixture.write_bytes(data)
    log=(OUTPUT/'fault-publish.log').open('wb')
    def publish():
        return subprocess.Popen([imageio_ffmpeg.get_ffmpeg_exe(),'-nostdin','-v','error',
                                 '-re','-f','lavfi','-i','testsrc2=size=1280x832:rate=1',
                                 '-re','-f','lavfi','-i','sine=frequency=440:sample_rate=44100',
                                 '-c:v','libx264','-threads','2','-preset','veryfast',
                                 '-profile:v','high','-tune','zerolatency','-b:v','900k','-maxrate','900k',
                                 '-bufsize','1800k','-g','2','-x264-params','nal-hrd=cbr',
                                 '-c:a','aac','-ac','1','-b:a','96k',
                                 '-f','flv','rtmp://127.0.0.1:11935/live/'+key['stream_key']],
                                stdout=subprocess.DEVNULL,stderr=log)
    def status():
        return next(c for c in admin.get('/api/contestants').json() if c['id']==CID)['status']
    publisher=publish()
    try:
        wait_for(lambda:status()=='online')
        check('mac-to-server-rtmp-through-ssh',publisher.poll() is None)
        metadata={'filename':fixture.name,'contestant_id':CID,'round_id':'day-1','sha256':hashlib.sha256(data).hexdigest()}
        response=admin.post('/uploads/',headers={'Tus-Resumable':'1.0.0','Upload-Length':str(len(data)),
                 'Upload-Metadata':','.join(k+' '+base64.b64encode(v.encode()).decode() for k,v in metadata.items())})
        check('upload-created',response.status_code==201)
        location='/uploads/'+response.headers['Location'].rstrip('/').split('/')[-1]
        headers={'Tus-Resumable':'1.0.0','Content-Type':'application/offset+octet-stream','Upload-Offset':'0'}
        check('upload-partial',admin.patch(location,headers=headers,content=data[:1024]).status_code==204)
        remote('restart','api')
        wait_for(lambda:admin.get('/api/auth/me').status_code==200)
        check('session-survives-api-restart',admin.get('/api/auth/me').status_code==200)
        check('gateway-dns-recovers-api',admin.get('/api/overview').status_code==200)
        check('upload-offset-survives-api-restart',admin.head(location,headers={'Tus-Resumable':'1.0.0'}).headers.get('Upload-Offset')=='1024')
        remote('stop','worker')
        time.sleep(47)
        check('worker-loss-detected',not admin.get('/api/overview').json()['worker_ok'])
        check('worker-loss-alert',any(a['fingerprint']=='service:worker' and not a['resolved'] for a in admin.get('/api/alerts').json()))
        check('monitor-loss-shows-unknown',status()=='unknown')
        check('publisher-survives-api-worker-outage',publisher.poll() is None)
        remote('start','worker')
        wait_for(lambda:admin.get('/api/overview').json()['worker_ok'] and status()=='online')
        check('worker-recovery',True)
        check('upload-resumes-after-outage',admin.patch(location,headers={**headers,'Upload-Offset':'1024'},content=data[1024:]).status_code==204)
        uid=location.split('/')[-1]
        def received():
            items=admin.get('/api/media?contestant='+CID).json()['items']
            return next((m for m in items if m['source_uid']=='upload:'+uid and m['state']=='checked'),None)
        wait_for(received)
        check('upload-validated-after-worker-recovery',received()['sha256']==hashlib.sha256(data).hexdigest())
        remote('restart','srs')
        wait_for(lambda:status()=='offline')
        check('receiver-restart-detected',True)
        if publisher.poll() is None:publisher.terminate()
        publisher.wait(timeout=20)
        publisher=publish()
        wait_for(lambda:status()=='online')
        check('publish-reconnect-after-receiver-restart',publisher.poll() is None)
        subprocess.run(['node','browser-live.mjs'],cwd=ROOT/'service/web',env={**os.environ,'FAULT_CID':CID},check=True,timeout=120)
        check('real-browser-live-playback',True)
        subprocess.run(['node','browser-upload.mjs'],cwd=ROOT/'service/web',env={**os.environ,'FAULT_CID':CID},check=True,timeout=120)
        check('real-browser-original-upload',True)
        remote('exec','-T','api','python','-c',
               "import sqlite3; s=sqlite3.connect('/data/metadata/proctor.sqlite3'); t=sqlite3.connect('/data/exports/fault-restore.sqlite3'); s.backup(t); t.close(); s.close()")
        snapshot=OUTPUT/'restore-check.sqlite3'
        subprocess.run(['scp','-q',HOST+':/data/2027nco/exports/fault-restore.sqlite3',str(snapshot)],check=True)
        snapshot.chmod(0o600)
        with sqlite3.connect(snapshot) as c:
            check('independent-metadata-copy-integrity',c.execute('PRAGMA integrity_check').fetchone()[0]=='ok')
            check('metadata-restore-contains-recent-receipt',c.execute('SELECT sha256 FROM media WHERE source_uid=?',('upload:'+uid,)).fetchone()[0]==hashlib.sha256(data).hexdigest())
    finally:
        remote('start','worker')
        if publisher.poll() is None:publisher.terminate()
        try:publisher.wait(timeout=10)
        except subprocess.TimeoutExpired:publisher.kill();publisher.wait()
        time.sleep(5)
        admin.put('/api/contestants/'+CID,json={'name':'故障恢复验收','group_name':'FAULT 故障验收','disabled':True})
        log.close()
    (OUTPUT/'fault-results.json').write_text(json.dumps({'cid':CID,'checks':results,'passed':len(results)},ensure_ascii=False,indent=2))
    print('RESULT',len(results),flush=True)


if __name__=='__main__':main()
