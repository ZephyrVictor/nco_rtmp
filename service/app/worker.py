import concurrent.futures
import json
import logging
import os
from pathlib import Path
import signal
import sqlite3
import threading
import time

import httpx

from .core import DATA, alert, audit, db, get_state, initialize, now, queue_media, resolve, set_state, storage
from .media import validate

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logging.getLogger('httpx').setLevel(logging.WARNING)
stop = threading.Event()
SRS = os.environ.get('SRS_API_URL', 'http://srs:1985/api/v1')


def read_streams(client):
    response=client.get(SRS+'/streams/',params={'start':0,'count':2048}).json()
    if response.get('code')!=0:
        raise ValueError('SRS returned error')
    return {s['name']:s for s in response.get('streams',[]) if s.get('publish',{}).get('active')}


def backup():
    destination = DATA / 'metadata-backups' / time.strftime('%Y-%m-%d.sqlite3', time.gmtime())
    if not destination.exists():
        temporary = destination.with_suffix('.tmp')
        with db() as source, sqlite3.connect(temporary) as target:
            source.backup(target)
            if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('Backup integrity check failed')
        temporary.chmod(0o600)
        temporary.replace(destination)
    with db() as c:
        state=get_state(c,'backup',{})
        state.update({'metadata_at': destination.stat().st_mtime,'metadata_file': destination.name})
        state.setdefault('configured',False)
        state.setdefault('independent_backup',False)
        set_state(c, 'backup', state)
        if not state.get('independent_backup') or now()-state.get('independent_at',0)>86400:
            alert(c, 'backup:independent', '独立存储备份缺失或超过 24 小时未更新', '同一 RAID 的元数据快照不能视为独立容灾副本')
        else:
            resolve(c,'backup:independent')


def reconcile_uploads():
    with db(immediate=True) as c:
        for upload in c.execute("SELECT * FROM uploads WHERE state='uploading'").fetchall():
            path = DATA / 'uploads' / upload['id']
            info = path.with_name(path.name + '.info')
            if not info.exists() or not path.is_file() or path.stat().st_size != upload['size']:
                continue
            meta = json.loads(info.read_text())
            if meta.get('Offset') != upload['size']:
                continue
            queue_media(c, contestant_id=upload['contestant_id'], round_id=upload['round_id'],
                        kind='original', source_uid='upload:' + upload['id'],
                        path=str(path.relative_to(DATA)), filename=upload['filename'])
            c.execute("UPDATE uploads SET state='received' WHERE id=?", (upload['id'],))


def monitor():
    client = httpx.Client(timeout=3)
    previous_files = {}
    cycle = 0
    while not stop.is_set():
        try:
            checked = now()
            online, srs_ok = {}, False
            try:
                online = read_streams(client)
                srs_ok = True
            except Exception:
                logging.warning('SRS API unavailable')
            with db(immediate=True) as c:
                set_state(c, 'worker_at', checked)
                set_state(c, 'monitor', {'checked_at': checked, 'srs_ok': srs_ok, 'streams': len(online)})
                (resolve(c, 'service:srs') if srs_ok else alert(c, 'service:srs', '直播服务不可用', severity='critical'))
                for live in c.execute('SELECT * FROM live_sessions WHERE ended IS NULL').fetchall():
                    cid = live['contestant_id']
                    stream = online.get(cid)
                    if stream:
                        c.execute('UPDATE live_sessions SET last_seen=?,stats_json=? WHERE id=?',
                                  (checked, json.dumps(stream), live['id']))
                        resolve(c, 'offline:' + cid)
                        files = list((DATA / 'dvr' / cid).rglob('*'))
                        latest = max((f for f in files if f.is_file()), key=lambda f: f.stat().st_mtime, default=None)
                        if latest and checked - latest.stat().st_mtime < 45:
                            resolve(c, 'dvr:' + cid)
                        elif checked - live['started'] > 60:
                            alert(c, 'dvr:' + cid, '服务器录像未持续写入', contestant_id=cid, severity='critical')
                        received = stream.get('recv_bytes', 0)
                        before = get_state(c, 'recv:' + cid, {'bytes': -1, 'at': checked})
                        if received != before['bytes']:
                            set_state(c, 'recv:' + cid, {'bytes': received, 'at': checked})
                            resolve(c, 'stalled:' + cid)
                        elif checked - before['at'] > 30:
                            alert(c, 'stalled:' + cid, '推流数据停滞超过 30 秒', contestant_id=cid)
                    elif srs_ok and checked - (live['last_seen'] or live['started']) > 15:
                        c.execute('UPDATE live_sessions SET ended=? WHERE id=?', (checked, live['id']))
                        alert(c, 'offline:' + cid, '推流中断，需立即通知志愿者', contestant_id=cid)
                for round_ in c.execute('SELECT * FROM rounds WHERE enabled=1 AND start_at<=? AND end_at>?', (checked - 30, checked)):
                    for candidate in c.execute('SELECT id FROM contestants WHERE disabled=0').fetchall():
                        if candidate['id'] not in online:
                            alert(c, 'offline:' + candidate['id'], '比赛期间未连接直播，需立即核查', contestant_id=candidate['id'])
                disk = storage(c)
                for level in (80, 85, 90):
                    if disk['used_fraction'] >= level / 100:
                        alert(c, 'disk:' + str(level), f'共享磁盘使用率达到 {level}%', severity='critical' if level >= 85 else 'warning')
                    else:
                        resolve(c, 'disk:' + str(level))
                if disk['committed_bytes'] >= disk['budget_bytes'] * .9:
                    alert(c, 'budget', '录像项目空间预算接近上限', severity='critical')
                else:
                    resolve(c, 'budget')
                c.execute("UPDATE jobs SET lease_until=? WHERE state='running'", (checked + 120,))
            # Reconcile closed files and retain orphaned fragments after a receiver crash.
            files = {}
            for path in (DATA / 'dvr').rglob('*'):
                if not path.is_file() or path.suffix not in ('.mp4','.flv','.tmp'):
                    continue
                relative = str(path.relative_to(DATA))
                stat = path.stat()
                files[relative] = stat.st_size
                if previous_files.get(relative) != stat.st_size or checked - stat.st_mtime < 15:
                    continue
                cid = path.relative_to(DATA / 'dvr').parts[0]
                orphan = path.suffix=='.tmp'
                if orphan and (not srs_ok or cid in online or checked-stat.st_mtime<60):
                    continue
                with db(immediate=True) as c:
                    if c.execute('SELECT 1 FROM media WHERE path=?', (relative,)).fetchone():
                        continue
                    live = c.execute('SELECT * FROM live_sessions WHERE contestant_id=? AND started<=? ORDER BY started DESC LIMIT 1', (cid, stat.st_mtime)).fetchone()
                    if live and (live['ended'] is not None or cid not in online):
                        queue_media(c, contestant_id=cid, round_id=live['round_id'], kind='dvr',
                                    source_uid='dvr:' + relative, path=relative, filename=path.name)
                        if orphan:
                            alert(c,'orphan:'+relative,'接收服务重启后留下未封口的辅助片段',
                                  '原片已保留并送检；不能据此认定比赛录制连续',cid)
            previous_files = files
            if cycle % 12 == 0:
                reconcile_uploads()
                backup()
            cycle += 1
        except Exception:
            logging.exception('Monitor cycle failed')
        stop.wait(5)
    client.close()


def claim():
    with db(immediate=True) as c:
        row = c.execute("""SELECT j.id AS job_id,j.attempts,m.* FROM jobs j JOIN media m ON m.id=j.media_id
                          WHERE j.state='queued' OR (j.state='running' AND j.lease_until<?)
                          ORDER BY j.created LIMIT 1""", (now(),)).fetchone()
        if not row:
            return None
        if row['attempts']>=3:
            c.execute("UPDATE jobs SET state='failed',error='Validation interrupted three times',lease_until=NULL WHERE id=?",(row['job_id'],))
            c.execute("UPDATE media SET state='failed',report_json=? WHERE id=?",(json.dumps({'errors':['校验被中断三次，需人工检查资源和原件'],'automatic_compliance':False}),row['id']))
            alert(c,'media:'+row['id'],'录像校验被反复中断，需人工处理',contestant_id=row['contestant_id'])
            return None
        c.execute("UPDATE jobs SET state='running',attempts=attempts+1,lease_until=? WHERE id=?", (now() + 120, row['job_id']))
        c.execute("UPDATE media SET state='checking' WHERE id=?", (row['id'],))
        return dict(row)


def process(item):
    try:
        with db() as c:
            upload = c.execute('SELECT * FROM uploads WHERE id=?', (item['source_uid'].removeprefix('upload:'),)).fetchone()
        report = validate(item, dict(upload) if upload else None)
        integrity = report['decode_ok'] and (not upload or report['sha256'] == upload['expected_sha256'])
        with db(immediate=True) as c:
            c.execute('UPDATE media SET state=?,sha256=?,report_json=?,path=?,finished=? WHERE id=?',
                      ('checked' if integrity else 'failed', report['sha256'], json.dumps(report, ensure_ascii=False), report['path'], now(), item['id']))
            c.execute("UPDATE jobs SET state=?,lease_until=NULL,error=? WHERE id=?",
                      ('done' if integrity else 'failed', None if integrity else '; '.join(report['errors']), item['job_id']))
            if report['errors']:
                alert(c, 'media:' + item['id'], '录像校验发现问题', '; '.join(report['errors']), item['contestant_id'])
            else:
                resolve(c, 'media:' + item['id'])
            audit(c, 'worker', 'media.checked', item['id'], 'parameters_pass=' + str(report['parameters_pass']))
    except Exception as error:
        logging.exception('Media validation failed %s', item['id'])
        with db() as c:
            c.execute("UPDATE media SET state='failed',report_json=?,finished=? WHERE id=?",
                      (json.dumps({'errors': [str(error)[:2000]], 'decode_ok': False, 'automatic_compliance': False}), now(), item['id']))
            c.execute("UPDATE jobs SET state='failed',lease_until=NULL,error=? WHERE id=?", (str(error)[:2000], item['job_id']))
            alert(c, 'media:' + item['id'], '录像校验任务失败，原件已保留', str(error)[:1000], item['contestant_id'])


def main():
    initialize()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    # One supervisor owns leases; recover work immediately after its container restarts.
    with db() as c:
        c.execute("UPDATE jobs SET state='queued',lease_until=NULL WHERE state='running'")
    thread = threading.Thread(target=monitor, daemon=True)
    thread.start()
    concurrency=min(8,max(1,int(os.environ.get('VALIDATION_CONCURRENCY','6'))))
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        pending = set()
        while not stop.is_set():
            pending = {f for f in pending if not f.done()}
            if len(pending) < concurrency:
                item = claim()
                if item:
                    pending.add(pool.submit(process, item))
                    continue
            stop.wait(1)
    thread.join(timeout=10)


if __name__ == '__main__':
    main()
