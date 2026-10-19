"""Add one scrape job without changing existing monitoring jobs."""
import copy
from pathlib import Path
import subprocess
import time

import yaml

path=Path('/data/gzctf-backup/prometheus/prometheus.yml')
original=path.read_text()
config=yaml.safe_load(original)
updated=copy.deepcopy(config)
job={'job_name':'nco-proctor','scrape_interval':'15s','scrape_timeout':'5s',
     'metrics_path':'/metrics','static_configs':[{'targets':['172.17.0.1:19185']}],
     'authorization':{'type':'Bearer','credentials':Path(__file__).parent.joinpath('secrets/metrics_token').read_text().strip()}}
updated['scrape_configs']=[j for j in updated.get('scrape_configs',[]) if j['job_name']!='nco-proctor']+[job]
if config==updated:
    print('Monitoring already configured')
else:
    backup=Path(__file__).parent/('prometheus.pre-nco-'+str(int(time.time()))+'.yml')
    backup.write_text(original)
    backup.chmod(0o600)
    def write_config(content):
        subprocess.run(['docker','run','--rm','-i','--user','0:0','--entrypoint','python',
                        '-v',str(path)+':/config.yml','nco-proctor:2026.10','-c',
                        'import sys; from pathlib import Path; Path("/config.yml").write_text(sys.stdin.read())'],
                       input=content,text=True,check=True)
    # Preserve the inode of the file bind-mounted into Prometheus.
    try:
        write_config(yaml.safe_dump(updated,allow_unicode=True,sort_keys=False))
        subprocess.run(['docker','exec','prometheus','promtool','check','config','/etc/prometheus/prometheus.yml'],
                       check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        subprocess.run(['docker','kill','--signal=HUP','prometheus'],check=True,stdout=subprocess.DEVNULL)
    except BaseException:
        write_config(original)
        raise
    print('Added nco-proctor scrape job; previous jobs retained. Backup:',backup)
