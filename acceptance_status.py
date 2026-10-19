"""Collect read-only acceptance evidence without exporting credentials."""
import json
from pathlib import Path
import subprocess

REMOTE = r'''
import json,os,sqlite3,subprocess,time,urllib.request
from pathlib import Path
root=Path('/data/2027nco')
v=os.statvfs(root)
used=(v.f_blocks-v.f_bfree)*v.f_frsize
available=v.f_bavail*v.f_frsize
result={'observed_at_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),
 'host':{'cpu_count':os.cpu_count(),'load':os.getloadavg(),
         'memory':{line.split(':')[0]:line.split(':')[1].strip() for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith(('MemTotal:','MemAvailable:'))}},
 'storage':{'available_bytes':available,'used_fraction':used/(used+available)}}
result['host']['network']={p.name:int(p.read_text()) for p in Path('/sys/class/net/eno5/statistics').iterdir() if p.name in ('rx_bytes','tx_bytes','rx_dropped','rx_errors','rx_missed_errors','tx_dropped','tx_errors')}
names=subprocess.check_output(['docker','ps','-a','--filter','label=com.docker.compose.project=nco-proctor','--format','{{.Names}}'],text=True).splitlines()
result['containers']=[]
for name in names:
 item=json.loads(subprocess.check_output(['docker','inspect',name],text=True))[0]
 if item['Config']['Labels'].get('com.docker.compose.oneoff')=='True':continue
 result['containers'].append({'name':name,'state':item['State']['Status'],
  'health':item['State'].get('Health',{}).get('Status'),
  'oom_killed':item['State']['OOMKilled'],'restart_count':item['RestartCount'],
  'ports':item['NetworkSettings']['Ports']})
result['docker_stats']=[json.loads(line) for line in subprocess.check_output(['docker','stats','--no-stream','--format','{{json .}}',*names],text=True).splitlines()]
with sqlite3.connect('file:'+str(root/'metadata/proctor.sqlite3')+'?mode=ro',uri=True) as c:
 c.row_factory=sqlite3.Row
 result['jobs']=[dict(r) for r in c.execute('SELECT state,COUNT(*) count FROM jobs GROUP BY state')]
 result['media']=[dict(r) for r in c.execute('SELECT kind,state,COUNT(*) count FROM media GROUP BY kind,state')]
 result['failed_media']=[{'contestant_id':r['contestant_id'],'kind':r['kind'],'filename':r['filename'],
                        'errors':json.loads(r['report_json']).get('errors')} for r in c.execute("SELECT contestant_id,kind,filename,report_json FROM media WHERE state='failed'")]
 result['recovered_orphan_flv']=[{'contestant_id':r['contestant_id'],'filename':r['filename'],'state':r['state'],
                               'decode_ok':json.loads(r['report_json']).get('decode_ok')} for r in c.execute("SELECT contestant_id,filename,state,report_json FROM media WHERE filename LIKE '%.flv.tmp'")]
 result['active_sessions']=c.execute('SELECT COUNT(*) FROM live_sessions WHERE ended IS NULL').fetchone()[0]
 result['enabled_contestants']=c.execute('SELECT COUNT(*) FROM contestants WHERE disabled=0').fetchone()[0]
 result['unresolved_alerts']=[dict(r) for r in c.execute('SELECT fingerprint,title,severity FROM alerts WHERE resolved IS NULL')]
try:
 data=json.load(urllib.request.urlopen('http://172.17.0.1:9090/api/v1/targets',timeout=8))
 result['prometheus']=[{'job':t['labels'].get('job'),'health':t['health'],'error':t['lastError']} for t in data['data']['activeTargets']]
except Exception as e:result['prometheus_error']=str(e)
result['other_containers']=subprocess.check_output(['docker','ps','--format','{{.Names}} {{.Status}}'],text=True).splitlines()
print(json.dumps(result,ensure_ascii=False,indent=2))
'''
output = subprocess.check_output(['ssh','ncoai@202.112.51.208','python3 -'],input=REMOTE,text=True)
result=json.loads(output)
destination=Path(__file__).parent/'test-results/final-health.json'
destination.write_text(json.dumps(result,ensure_ascii=False,indent=2))
print(json.dumps({'saved':str(destination),'time':result['observed_at_utc'],
                  'storage':result['storage'],'containers':result['containers'],
                  'jobs':result['jobs'],'active_sessions':result['active_sessions'],
                  'enabled_contestants':result['enabled_contestants'],
                  'unresolved_alert_count':len(result['unresolved_alerts']),
                  'prometheus':result.get('prometheus')},ensure_ascii=False,indent=2))
