"""Close generated acceptance identities; preserve all recordings and evidence."""
from datetime import datetime,timezone
import json
import sqlite3

from app.core import DATA,audit,db,now

groups={'TEST 集成验收','TEST 界面验收','LOAD 负载验收','FAULT 故障验收'}
with db() as c:
    ids=[r['id'] for r in c.execute('SELECT id,group_name FROM contestants')
         if r['group_name'] in groups and r['id'].startswith(('TEST-','LOAD-','FAULT-'))]
    active=[r['contestant_id'] for r in c.execute('SELECT contestant_id FROM live_sessions WHERE ended IS NULL') if r['contestant_id'] in ids]
    if active:raise RuntimeError('Acceptance streams are still running; refusing to close identities')
snapshot=DATA/'metadata-backups'/('acceptance-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'.sqlite3')
with db() as source,sqlite3.connect(snapshot) as target:
    source.backup(target)
    assert target.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
snapshot.chmod(0o600)
with db(immediate=True) as c:
    for cid in ids:
        c.execute('UPDATE contestants SET disabled=1,stream_key_hash=NULL WHERE id=?',(cid,))
        usernames=[r[0] for r in c.execute("SELECT username FROM users WHERE contestant_id=? AND role='student'",(cid,))]
        for username in usernames:
            c.execute('DELETE FROM auth_sessions WHERE username=?',(username,))
            c.execute('UPDATE users SET disabled=1 WHERE username=?',(username,))
        c.execute('UPDATE alerts SET resolved=? WHERE contestant_id=? AND resolved IS NULL',(now(),cid))
    audit(c,'acceptance','acceptance.identities.closed',detail=json.dumps({'count':len(ids),'snapshot':snapshot.name}))
print(json.dumps({'closed_test_contestants':len(ids),'snapshot':snapshot.name,'recordings_preserved':True}))
