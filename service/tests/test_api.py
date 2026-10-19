import io
import json
import os
from pathlib import Path
import tempfile
import zipfile

os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='nco-unit-')
os.environ['ADMIN_PASSWORD'] = 'temporary-unit-admin-password'
os.environ['PLAY_SECRET'] = 'temporary-unit-play-secret'
os.environ['METRICS_TOKEN'] = 'temporary-unit-metrics-secret'

from fastapi.testclient import TestClient
import pytest

from app.main import app
from app.core import DATA, db, digest, initialize, now, queue_media, safe_path
from app.worker import claim, read_streams, reconcile_uploads


@pytest.fixture(autouse=True)
def database():
    initialize()
    with db() as c:
        for name in ['jobs','media','uploads','live_sessions','auth_sessions','login_attempts','events','alerts','contestants','users','rounds','system_state']:
            c.execute('DELETE FROM ' + name)
    initialize()


def client(role='admin', cid=None):
    c = TestClient(app)
    if role != 'admin':
        a = client()
        a.post('/api/users', json={'username': role, 'password': 'temporary-unit-user-password', 'role': role, 'contestant_id': cid})
    login = c.post('/api/auth/login', json={'username': role, 'password': 'temporary-unit-admin-password' if role == 'admin' else 'temporary-unit-user-password'})
    assert login.status_code == 200
    if role=='student':
        assert login.json()['contestant_id']==cid
    c.headers['X-CSRF-Token'] = login.json()['csrf']
    return c


def candidate(c, cid='test-01'):
    assert c.post('/api/contestants', json={'id': cid, 'name': 'Test'}).status_code == 200


def test_auth_csrf_rbac():
    anonymous = TestClient(app)
    assert anonymous.get('/api/contestants').status_code == 401
    a = client()
    candidate(a)
    csrf = a.headers.pop('X-CSRF-Token')
    assert a.post('/api/contestants', json={'id':'x','name':'x'}).status_code == 403
    a.headers['X-CSRF-Token'] = csrf
    assert a.post('/api/contestants', json={'id':'x','name':'x'}, headers={'Origin':'https://evil.example'}).status_code == 403
    student = client('student', 'test-01')
    assert student.post('/api/contestants', json={'id':'x','name':'x'}).status_code == 403
    assert len(student.get('/api/contestants').json()) == 1
    assert student.get('/api/users').status_code == 403
    assert student.get('/api/events').status_code == 403
    assert student.get('/internal/authorize', headers={'X-Original-URI':'/live/test-01.flv'}).status_code == 403


def test_publish_auth_and_duplicate():
    a = client()
    candidate(a)
    key = a.post('/api/contestants/test-01/key').json()['stream_key']
    good = {'action':'on_publish','app':'live','stream':'test-01','param':'?'+key.split('?',1)[1],'client_id':'c1'}
    assert a.post('/internal/srs',json={**good,'param':'?token=bad&round=day-1'}).json()['code'] == 403
    assert a.post('/internal/srs',json=good).json()['code'] == 0
    assert a.post('/internal/srs',json=good).json()['code'] == 409
    assert a.post('/api/contestants/test-01/key').status_code == 409
    a.post('/internal/srs',json={**good,'action':'on_unpublish'})
    assert a.post('/internal/srs',json={**good,'client_id':'c2'}).json()['code'] == 0
    assert a.post('/internal/srs',json={'action':'on_play','param':'?token=bad'}).json()['code'] == 403


def test_tus_owner_and_completion(monkeypatch):
    import app.main as main
    monkeypatch.setattr(main, 'storage', lambda _: {'available_bytes':10**15,'budget_remaining_bytes':10**15,'used_fraction':.1})
    a = client()
    candidate(a)
    candidate(a,'test-02')
    s = client('student','test-01')
    event = {'Type':'pre-create','Event':{'Upload':{'Size':100,'MetaData':{'contestant_id':'test-01','round_id':'day-1','sha256':'a'*64,'filename':'original.mkv'}}}}
    result = s.post('/internal/tus',json=event).json()
    uid = result['ChangeFileInfo']['ID']
    assert len(uid) == 32
    auth = {'X-Original-URI':'/uploads/'+uid,'X-Original-Method':'HEAD'}
    assert s.get('/internal/authorize', headers=auth).status_code == 204
    with db() as c:
        c.execute("UPDATE users SET contestant_id='test-02' WHERE username='student'")
    event['Event']['Upload']['MetaData']['contestant_id']='test-01'
    assert s.post('/internal/tus',json=event).json()['RejectUpload']
    # Ownership is the initiating account, independently of contestant metadata.
    with db() as c:
        c.execute("UPDATE uploads SET owner='admin',state='received' WHERE id=?", (uid,))
    assert s.get('/internal/authorize',headers=auth).status_code == 403
    assert a.get('/internal/authorize',headers={**auth,'X-Original-Method':'DELETE'}).status_code == 403


def test_safe_paths_and_review():
    a=client()
    candidate(a)
    with pytest.raises(ValueError):
        safe_path('../etc/passwd')
    f=DATA/'uploads'/'unit.mkv'
    f.write_bytes(b'example')
    with db() as c:
        mid=queue_media(c,contestant_id='test-01',round_id='day-1',kind='original',source_uid='unit:test',path='uploads/unit.mkv',filename='unit.mkv')
        assert mid==queue_media(c,contestant_id='test-01',round_id='day-1',kind='original',source_uid='unit:test',path='uploads/unit.mkv',filename='unit.mkv')
        c.execute("UPDATE media SET state='checked',report_json=? WHERE id=?", (json.dumps({'parameters_pass':False}),mid))
    assert a.post(f'/api/media/{mid}/review',json={'review':'accepted','note':'test'}).status_code==409
    assert a.post(f'/api/media/{mid}/review',json={'review':'rejected','note':'test'}).status_code==200
    job=claim()
    assert job['id']==mid
    assert claim() is None


def test_profile_and_round_rules():
    a=client()
    z=zipfile.ZipFile(io.BytesIO(a.get('/api/obs-profile.zip?width=2560&height=1664').content))
    ini=z.read('ICO2026/basic.ini').decode()
    assert 'OutputCX=1280' in ini and 'OutputCY=832' in ini
    assert 'FPSInt=1' in ini and 'SampleRate=44100' in ini and 'ChannelSetup=Mono' in ini
    assert json.loads(z.read('ICO2026/recordEncoder.json'))['crf']==24
    assert a.get('/api/obs-profile.zip?width=800&height=600').status_code==422
    assert a.put('/api/rounds/day-1',json={'name':'Test','start_at':1000}).status_code==200
    round_=a.get('/api/rounds').json()[0]
    assert round_['end_at']-round_['start_at']==25200


def test_disabled_sessions_revoked_and_metrics():
    a=client()
    p=client('proctor')
    assert a.put('/api/users/proctor',json={'disabled':True}).status_code==200
    assert p.get('/api/auth/me').status_code==401
    assert a.get('/api/metrics').status_code==401
    assert 'nco_storage_available_bytes' in a.get('/api/metrics',headers={'Authorization':'Bearer temporary-unit-metrics-secret'}).text
    assert a.post('/api/contestants/import',json={'csv':'id,name,group\nx,Test,G\nx,Test,G'}).status_code==422
    assert a.post('/api/contestants/import',json={'csv':'id,name,group\nx'}).status_code==422


def test_srs_pagination_and_worker_heartbeat():
    import httpx
    from app.core import set_state
    streams=[{'name':str(i),'publish':{'active':True}} for i in range(50)]
    def srs(request):
        count=int(request.url.params.get('count',10))
        return httpx.Response(200,json={'code':0,'streams':streams[:count]})
    with httpx.Client(transport=httpx.MockTransport(srs)) as mock:
        assert set(read_streams(mock))=={str(i) for i in range(50)}
    a=client()
    with db() as c:
        set_state(c,'worker_at',now()-100)
    assert not a.get('/api/overview').json()['worker_ok']
    assert any(item['fingerprint']=='service:worker' and not item['resolved'] for item in a.get('/api/alerts').json())
    with db() as c:
        set_state(c,'worker_at',now())
    assert a.get('/api/overview').json()['worker_ok']
