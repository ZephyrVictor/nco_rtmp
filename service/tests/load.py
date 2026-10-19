"""Bounded SRS ingest, gateway playback and DVR test on the Docker network."""
import asyncio
import json
import os
from pathlib import Path
import statistics
import subprocess
import time

import httpx

from integration import BASE, fixture, login, post

STAMP=str(int(time.time()))
PREFIX='LOAD-'+STAMP+'-'
ROOT=Path('/data/exports')/('load-'+STAMP)
ROOT.mkdir(mode=0o750)


async def stage(admin,keys,n,viewers,duration):
    processes=[]
    failures=[]
    received=[0]*viewers
    gaps=[0.0]*viewers
    samples=[]
    async with httpx.AsyncClient(base_url=BASE,cookies=admin.cookies,timeout=httpx.Timeout(20,read=20),
                                 limits=httpx.Limits(max_connections=400,max_keepalive_connections=20)) as web:
        async with httpx.AsyncClient(timeout=5) as internal:
            try:
                for offset in range(0,n,10):
                    outputs='|'.join('[f=flv:onfail=ignore]rtmp://srs:1935/live/'+k['stream_key'] for k in keys[offset:min(n,offset+10)])
                    log=(ROOT/f'{n}-{offset}.log').open('wb')
                    processes.append(subprocess.Popen(['ffmpeg','-nostdin','-v','error','-re','-stream_loop','-1',
                        '-i',str(ROOT/'load-source.mkv'),'-map','0:v','-map','0:a','-c','copy','-f','tee',outputs],stdout=subprocess.DEVNULL,stderr=log))
                    log.close()
                for attempt in range(75):
                    streams=(await internal.get('http://srs:1985/api/v1/streams/?start=0&count=2048')).json()['streams']
                    active=[s for s in streams if s.get('publish',{}).get('active') and s['name'].startswith(PREFIX)]
                    if len(active)==n:break
                    if attempt % 10 == 0:
                        print('WARMUP',n,len(active),'process_status',[p.poll() for p in processes],flush=True)
                    await asyncio.sleep(1)
                assert len(active)==n, ('publish count',len(active),n)
                start=time.monotonic()
                async def playback(index):
                    last=time.monotonic()
                    try:
                        async with web.stream('GET','/live/'+PREFIX+f'{index%n:03d}'+'.flv') as response:
                            assert response.status_code==200, response.status_code
                            first=True
                            async for block in response.aiter_bytes(32768):
                                current=time.monotonic()
                                gaps[index]=max(gaps[index],current-last)
                                last=current
                                if first:
                                    assert block.startswith(b'FLV'), 'Invalid FLV header'
                                    first=False
                                received[index]+=len(block)
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        failures.append({'viewer':index,'error':str(error)})
                tasks=[asyncio.create_task(playback(i)) for i in range(viewers)]
                previous_bytes=sum(s.get('recv_bytes',0) for s in active)
                previous_at=start
                while time.monotonic()-start<duration:
                    await asyncio.sleep(5)
                    tick=time.monotonic()
                    streams=(await internal.get('http://srs:1985/api/v1/streams/?start=0&count=2048')).json()['streams']
                    active=[s for s in streams if s.get('publish',{}).get('active') and s['name'].startswith(PREFIX)]
                    total=sum(s.get('recv_bytes',0) for s in active)
                    health_start=time.monotonic()
                    health=await web.get('/api/overview')
                    health.raise_for_status()
                    summary=(await internal.get('http://srs:1985/api/v1/summaries')).json().get('data',{})
                    samples.append({'elapsed':round(tick-start,2),'streams':len(active),
                        'ingress_mbps':(total-previous_bytes)*8/(tick-previous_at)/1e6,
                        'api_ms':(time.monotonic()-health_start)*1000,
                        'srs_self':summary.get('self'),'disk_available':health.json()['storage']['available_bytes']})
                    previous_bytes,previous_at=total,tick
                    assert len(active)==n, ('dropped publisher',n,len(active))
                    assert not failures, failures
                    assert all(p.poll() is None for p in processes), 'Publisher process exited'
                for task in tasks:task.cancel()
                await asyncio.gather(*tasks,return_exceptions=True)
                assert all(v>duration*50000 for v in received), ('Playback throughput too low',min(received))
                assert max(gaps)<15, ('Playback stalled',max(gaps))
                result={'publishers':n,'viewers':viewers,'seconds':round(time.monotonic()-start,2),
                        'passed':True,'viewer_errors':failures,'received_bytes':sum(received),
                        'viewer_min_bytes':min(received),'max_inter_chunk_seconds':max(gaps),
                        'api_p95_ms':sorted(s['api_ms'] for s in samples)[int((len(samples)-1)*.95)],
                        'samples':samples}
                (ROOT/f'stage-{n}.json').write_text(json.dumps(result,indent=2))
                print('STAGE_PASS',n,viewers,'Mbps',round(statistics.mean(s['ingress_mbps'] for s in samples),2),
                      'API_p95_ms',round(result['api_p95_ms'],2),'max_gap',round(max(gaps),2),flush=True)
                return result
            finally:
                for process in processes:process.terminate()
                for process in processes:
                    try:process.wait(timeout=15)
                    except subprocess.TimeoutExpired:process.kill();process.wait()


async def main():
    admin=login('admin',Path('/run/secrets/admin_password').read_text().strip())
    fixture(ROOT/'load-source.mkv',False)
    stages=json.loads(os.environ.get('LOAD_STAGES','[[10,4,30],[50,16,45],[200,64,120],[260,84,90]]'))
    maximum=max(s[0] for s in stages)
    keys=[]
    for index in range(maximum):
        cid=PREFIX+f'{index:03d}'
        post(admin,'contestants',{'id':cid,'name':'并发验收 '+str(index+1),'group_name':'LOAD 负载验收'})
        keys.append(post(admin,'contestants/'+cid+'/key',{}))
    results=[]
    try:
        for n,viewers,seconds in stages:
            results.append(await stage(admin,keys,n,viewers,seconds))
            await asyncio.sleep(8)
        for _ in range(120):
            jobs=admin.get('/api/overview').json()['jobs']
            if not any(j['state'] in ('running','queued') and j['n'] for j in jobs):break
            await asyncio.sleep(5)
        from app.core import db
        with db() as c:
            counts=[dict(r) for r in c.execute("SELECT state,COUNT(*) count FROM media WHERE contestant_id LIKE ? AND kind='dvr' GROUP BY state",(PREFIX+'%',))]
            indexed=c.execute('SELECT COUNT(DISTINCT contestant_id) FROM media WHERE contestant_id LIKE ? AND kind=\'dvr\'',(PREFIX+'%',)).fetchone()[0]
        assert indexed==maximum, ('Missing DVR contestants',indexed)
        assert all(r['state']=='checked' for r in counts), counts
        result={'prefix':PREFIX,'stages':results,'dvr_counts':counts,'dvr_contestants':indexed,
                'scope':'Docker-network synthetic source; not a venue/public-network or seven-hour test'}
        (ROOT/'result.json').write_text(json.dumps(result,indent=2))
        print('RESULT',ROOT/'result.json',flush=True)
    finally:
        for index in range(maximum):
            admin.put('/api/contestants/'+PREFIX+f'{index:03d}',json={'name':'并发验收 '+str(index+1),'group_name':'LOAD 负载验收','disabled':True})


if __name__=='__main__':asyncio.run(main())
