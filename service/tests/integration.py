"""Real gateway/tusd/SRS/worker tests, run in a temporary compose API container."""
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

import httpx

BASE=os.environ.get('API_BASE','https://gateway:8443')
STAMP=str(int(time.time()))
CID='TEST-'+STAMP
ROOT=Path('/data/exports')/('integration-'+STAMP)
ROOT.mkdir(mode=0o750)
results=[]


def check(name,condition,detail=None):
    assert condition, (name,detail)
    results.append({'name':name,'passed':True,'detail':detail})
    print('PASS',name,flush=True)


def login(username,password):
    client=httpx.Client(base_url=BASE,timeout=30)
    response=client.post('/api/auth/login',json={'username':username,'password':password})
    response.raise_for_status()
    client.headers['X-CSRF-Token']=response.json()['csrf']
    return client


def post(c,path,body):
    response=c.post('/api/'+path,json=body)
    response.raise_for_status()
    return response.json()


def fixture(path,good=True):
    args=['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','testsrc2=size=1280x832:rate=1',
          '-f','lavfi','-i','sine=frequency=440:sample_rate=44100','-t','20','-c:v','libx264',
          '-threads','1','-preset','veryfast','-profile:v','high','-pix_fmt','yuv420p','-g','2']
    args+=['-crf','24'] if good else ['-b:v','900k','-maxrate','900k','-bufsize','900k','-x264-params','nal-hrd=cbr']
    args+=['-c:a','aac','-ar','44100','-ac','1','-b:a','96k','-y',str(path)]
    subprocess.run(args,check=True,timeout=60)


def create_upload(c,path,checksum=None):
    data=path.read_bytes()
    meta={'filename':path.name,'contestant_id':CID,'round_id':'day-1',
          'sha256':checksum or hashlib.sha256(data).hexdigest()}
    headers={'Tus-Resumable':'1.0.0','Upload-Length':str(len(data)),
             'Upload-Metadata':','.join(k+' '+base64.b64encode(v.encode()).decode() for k,v in meta.items())}
    response=c.post('/uploads/',headers=headers)
    check('tus-create-'+path.name,response.status_code==201,response.text[:200])
    location='/uploads/'+response.headers['Location'].rstrip('/').split('/')[-1]
    return location,data


def wait_media(c,uid):
    for _ in range(120):
        items=c.get('/api/media?contestant='+CID).json()['items']
        item=next((m for m in items if m['source_uid']=='upload:'+uid),None)
        if item and item['state'] in ('checked','failed'):
            return item
        time.sleep(1)
    raise AssertionError('Validation timed out')


def main():
    admin=login('admin',Path('/run/secrets/admin_password').read_text().strip())
    post(admin,'contestants',{'id':CID,'name':'集成验收样本','group_name':'TEST 集成验收'})
    post(admin,'contestants',{'id':CID+'-other','name':'权限隔离样本','group_name':'TEST 集成验收'})
    password=hashlib.sha256(os.urandom(32)).hexdigest()
    for suffix,cid in [('a',CID),('b',CID+'-other')]:
        post(admin,'users',{'username':CID+'-'+suffix,'password':password,'role':'student','contestant_id':cid})
    a=login(CID+'-a',password)
    b=login(CID+'-b',password)
    anonymous=httpx.Client(base_url=BASE,timeout=5)
    check('anonymous-api-denied',anonymous.get('/api/contestants').status_code==401)
    check('anonymous-live-denied',anonymous.get('/live/'+CID+'.flv').status_code==401)
    check('student-live-denied',a.get('/live/'+CID+'.flv').status_code==403)
    check('student-admin-denied',a.get('/api/users').status_code==403)
    check('internal-hooks-not-public',anonymous.post('/internal/srs',json={}).status_code==405)
    fixture(ROOT/'合规参数 短片.mkv')
    fixture(ROOT/'bad-cbr.mkv',False)
    (ROOT/'corrupt.mkv').write_bytes(b'not an MKV original\x00'*1000)
    for index,(name,checksum) in enumerate([('合规参数 短片.mkv',None),('bad-cbr.mkv',None),('corrupt.mkv',None),('合规参数 短片.mkv','0'*64)]):
        location,data=create_upload(a,ROOT/name,checksum)
        uid=location.split('/')[-1]
        check('other-owner-head-denied-'+str(index),b.head(location,headers={'Tus-Resumable':'1.0.0'}).status_code==403)
        check('missing-csrf-patch-denied-'+str(index),anonymous.patch(location,headers={'Tus-Resumable':'1.0.0','Content-Type':'application/offset+octet-stream','Upload-Offset':'0'},content=b'x').status_code==401)
        headers={'Tus-Resumable':'1.0.0','Content-Type':'application/offset+octet-stream','Upload-Offset':'0'}
        response=a.patch(location,headers=headers,content=data[:1024])
        check('partial-patch-'+str(index),response.status_code==204,response.text[:200])
        offset=a.head(location,headers={'Tus-Resumable':'1.0.0'})
        check('resumable-head-'+str(index),offset.status_code==200 and offset.headers.get('Upload-Offset')=='1024')
        response=a.patch(location,headers={**headers,'Upload-Offset':'1'},content=b'x')
        check('offset-conflict-'+str(index),response.status_code==409)
        response=a.patch(location,headers={**headers,'Upload-Offset':'1024'},content=data[1024:])
        check('resume-finish-'+str(index),response.status_code==204,response.text[:200])
        item=wait_media(admin,uid)
        check('completed-head-'+str(index),a.head(location,headers={'Tus-Resumable':'1.0.0'}).headers.get('Upload-Offset')==str(len(data)))
        check('completed-delete-denied-'+str(index),a.delete(location,headers={'Tus-Resumable':'1.0.0'}).status_code==403)
        check('other-owner-download-denied-'+str(index),b.get('/api/media/'+item['id']+'/download').status_code==404)
        content=a.get('/api/media/'+item['id']+'/download')
        check('preserved-original-sha-'+str(index),content.status_code==200 and hashlib.sha256(content.content).hexdigest()==hashlib.sha256(data).hexdigest())
        if index==0:
            check('good-parameters-detected',item['state']=='checked' and item['report']['decode_ok'] and item['report']['x264_options'] and 'crf=24.0' in item['report']['x264_options'],item['report']['errors'])
            check('short-test-not-compliant',not item['report']['parameters_pass'] and not item['report']['automatic_compliance'])
            check('false-acceptance-blocked',admin.post('/api/media/'+item['id']+'/review',json={'review':'accepted','note':'short sample'}).status_code==409)
            review=post(admin,'media/'+item['id']+'/review',{'review':'rejected','note':'短片仅作功能验收，未覆盖比赛全程'})
        if index==1:
            check('cbr-rejected',any('CRF 24' in e for e in item['report']['errors']))
        if index==2:
            check('corrupt-file-failed',item['state']=='failed')
        if index==3:
            check('hash-mismatch-failed',item['state']=='failed' and any('SHA-256' in e for e in item['report']['errors']))
    key=post(admin,'contestants/'+CID+'/key',{})
    private=ROOT/'stream-key.json'
    private.write_text(json.dumps(key))
    private.chmod(0o600)
    invalid=subprocess.run(['ffmpeg','-nostdin','-v','error','-i',str(ROOT/'bad-cbr.mkv'),'-t','2','-c','copy','-f','flv','rtmp://srs:1935/live/'+CID+'?token=bad&round=day-1'],capture_output=True,timeout=15)
    check('unauthorized-rtmp-publish-denied',invalid.returncode!=0)
    publisher=subprocess.Popen(['ffmpeg','-nostdin','-v','error','-re','-stream_loop','-1','-i',str(ROOT/'bad-cbr.mkv'),'-c','copy','-f','flv','rtmp://srs:1935/live/'+key['stream_key']],stdout=subprocess.DEVNULL,stderr=(ROOT/'publish.log').open('wb'))
    try:
        time.sleep(5)
        check('publisher-running',publisher.poll() is None)
        movie=ROOT/'gateway-playback.flv'
        start=time.monotonic()
        with admin.stream('GET','/live/'+CID+'.flv',timeout=20) as response,movie.open('wb') as target:
            check('authorized-flv-playback',response.status_code==200)
            for block in response.iter_bytes(65536):
                target.write(block)
                if time.monotonic()-start>8: break
        check('flv-header',movie.read_bytes().startswith(b'FLV'))
        decoded=subprocess.run(['ffmpeg','-nostdin','-v','error','-xerror','-i',str(movie),'-map','0:v','-f','null','-'],capture_output=True,timeout=30)
        check('gateway-video-decodes',decoded.returncode==0,decoded.stderr.decode()[-500:])
        time.sleep(5)
        active=next(c for c in admin.get('/api/contestants').json() if c['id']==CID)
        check('live-state-online',active['status']=='online')
        print('GATEWAY_LIVE_SMOKE_OK',flush=True)
    finally:
        publisher.terminate()
        try: publisher.wait(timeout=10)
        except subprocess.TimeoutExpired: publisher.kill();publisher.wait()
    for _ in range(30):
        media=admin.get('/api/media?contestant='+CID).json()['items']
        dvr=[m for m in media if m['kind']=='dvr' and m['state']=='checked']
        if dvr:break
        time.sleep(2)
    check('dvr-callback-and-full-decode',bool(dvr))
    time.sleep(6)
    active=next(c for c in admin.get('/api/contestants').json() if c['id']==CID)
    check('disconnect-state-offline',active['status']=='offline')
    check('disconnect-alert-visible',any(a['contestant_id']==CID and '断开' in a['title'] for a in admin.get('/api/alerts').json()))
    exported=admin.get('/api/manifest.csv')
    check('manifest-export',exported.status_code==200 and CID in exported.text)
    for cid in [CID,CID+'-other']:
        admin.put('/api/contestants/'+cid,json={'name':'集成验收样本','group_name':'TEST 集成验收','disabled':True}).raise_for_status()
    result={'id':STAMP,'checks':results,'passed':len(results),'fixture_directory':str(ROOT),'recordings':media}
    (ROOT/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print('RESULT',str(ROOT/'result.json'),flush=True)


if __name__=='__main__':main()
