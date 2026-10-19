import json
import os
from pathlib import Path
import shutil
import sqlite3

from .core import DATA, alert, audit, db, get_state, now, resolve, safe_path, set_state
from .media import sha256


def export(destination):
    destination=Path(destination).resolve()
    if not destination.is_dir():
        raise ValueError('Create and mount the independent backup destination first')
    if os.stat(destination).st_dev==os.stat(DATA).st_dev:
        raise ValueError('Destination must be a different filesystem, not the recording RAID')
    root=destination/'nco-proctor'
    root.mkdir(mode=0o750,exist_ok=True)
    with db() as c:
        files=[dict(r) for r in c.execute("SELECT id,path,size,sha256 FROM media WHERE kind='original' AND sha256 IS NOT NULL")]
    if shutil.disk_usage(root).free<sum(f['size'] for f in files)+1024**3:
        raise ValueError('Insufficient independent backup space')
    manifest=[]
    for item in files:
        source=safe_path(item['path'])
        target=root/item['path']
        target.parent.mkdir(parents=True,exist_ok=True)
        if not target.exists() or sha256(target)!=item['sha256']:
            temporary=target.with_suffix('.partial')
            shutil.copyfile(source,temporary)
            if sha256(temporary)!=item['sha256']:
                raise ValueError('Backup SHA-256 mismatch: '+item['id'])
            temporary.chmod(0o440)
            temporary.replace(target)
        manifest.append(item)
    stamp=str(int(now()))
    database=root/('metadata-'+stamp+'.sqlite3')
    with db() as source,sqlite3.connect(database) as target:
        source.backup(target)
        if target.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
            raise ValueError('Backup metadata integrity check failed')
    database.chmod(0o600)
    (root/('manifest-'+stamp+'.json')).write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
    with db() as c:
        state=get_state(c,'backup',{})
        state.update({'configured':True,'independent_backup':True,'independent_at':now(),
                      'independent_files':len(manifest),'independent_destination':str(root)})
        set_state(c,'backup',state)
        resolve(c,'backup:independent')
        audit(c,'backup','backup.independent',detail=str(len(manifest))+' verified originals')
    return {'files':len(manifest),'metadata':database.name}
