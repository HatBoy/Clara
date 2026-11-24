"""模块五：两份真实 WAL 文件库、独立连接与仅绑定回环地址的 DAV。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Barrier, Thread
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session

from database import Base, _ensure_updated_at_triggers
from models import Task, WorkflowRun
from schemas import TaskCreate, TaskUpdate
from services import sync_engine as se, tasks, workflows
from services.sync_webdav import WebDavClient, WebDavError


@pytest.fixture
def devices(tmp_path, monkeypatch):
    """独立引擎、连接池、快照目录；只在两台设备间串行切换同步入口。"""
    engines = {}
    for name in ('A', 'B'):
        folder = tmp_path / name
        folder.mkdir()
        engine = create_engine(f'sqlite:///{folder / "clara.db"}')
        engines[name] = engine

        @event.listens_for(engine, 'connect')
        def pragmas(conn, _):
            conn.execute('PRAGMA foreign_keys=ON')
            conn.execute('PRAGMA busy_timeout=5000')

        with engine.begin() as conn:
            assert conn.exec_driver_sql('PRAGMA journal_mode=WAL').scalar() == 'wal'
        Base.metadata.create_all(engine)
        _ensure_updated_at_triggers(engine)

    def sync(name, dav):
        monkeypatch.setattr(se, 'get_data_dir', lambda: tmp_path / name)
        with Session(engines[name]) as db:
            return se.run_sync(db, dav, SimpleNamespace(device_id=name))

    try:
        yield engines, sync
    finally:
        for engine in engines.values():
            engine.dispose()


@pytest.fixture
def loopback():
    files = {}
    state = SimpleNamespace(hook=lambda method, path: None, writes=[])

    class Dav(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def handle_request(self):
            payload = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            fault = state.hook(self.command, self.path)
            if fault == '断连':
                self.close_connection = True
                return
            if fault == '损坏':
                code, body = 200, b'{broken'
            elif self.command == 'MKCOL':
                code, body = 201, b''
            elif self.command == 'PUT':
                state.writes.append(self.path)
                files[self.path] = payload
                code, body = 201, b''
            else:
                code, body = (200, files[self.path]) if self.path in files else (404, b'')
            self.send_response(code)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_PUT = do_MKCOL = handle_request

    server = ThreadingHTTPServer(('127.0.0.1', 0), Dav)
    thread = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05})
    thread.start()
    dav = WebDavClient(f'http://127.0.0.1:{server.server_port}', None, None)
    try:
        yield dav, state
    finally:
        dav.close()
        server.shutdown()
        server.server_close()
        thread.join()


def _rows(engine):
    with Session(engine) as db:
        assert db.scalar(text('PRAGMA journal_mode')) == 'wal'
        assert db.scalar(text('PRAGMA foreign_keys')) == 1
        assert db.execute(text('PRAGMA integrity_check')).all() == [('ok',)]
        assert not db.execute(text('PRAGMA foreign_key_check')).all()
        return sorted((r.id, r.title) for r in db.query(Task))


@pytest.mark.parametrize('job_first', [False, True], ids=['edit-first', 'job-first'])
def test_stale_sync_preserves_user_and_job_commits(devices, loopback, job_first):
    engines, sync = devices
    dav, state = loopback
    with Session(engines['A']) as db:
        local = tasks.create_task(db, TaskCreate(title='本地待改')).id
    sync('A', dav)
    sync('B', dav)
    with Session(engines['B']) as db:
        remote = tasks.create_task(db, TaskCreate(title='远端新增')).id
    sync('B', dav)

    barrier = Barrier(2, timeout=15)

    def pause(method, path):
        if method == 'GET' and path.endswith('/tasks.json'):
            barrier.wait()  # 同步已读取本地行、尚未取得远端表
            barrier.wait()

    def edit():
        with Session(engines['A']) as db:
            assert tasks.update_task(db, local, TaskUpdate(title='用户已提交')).title == '用户已提交'

    def job():
        with Session(engines['A']) as db:
            assert workflows._claim_autobackup(db, datetime(2030, 1, 2, 8))

    state.hook = pause
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(sync, 'A', dav)
        barrier.wait()
        try:
            for actor in ((job, edit) if job_first else (edit, job)):
                actor()
        finally:
            barrier.wait()
        with pytest.raises(se.SyncAbort, match='规划期间本地 tasks 已变化'):
            pending.result(timeout=15)
    assert _rows(engines['A']) == [(local, '用户已提交')]
    state.hook = lambda *_: None
    sync('A', dav)
    sync('B', dav)
    writes = len(state.writes)
    for name in ('A', 'B'):
        stats = sync(name, dav)
        assert stats['tables_changed'] == stats['bytes_pushed'] == 0
        assert _rows(engines[name]) == sorted([(local, '用户已提交'), (remote, '远端新增')])
        with Session(engines[name]) as db:
            ledger = [(r.name, r.run_date.isoformat()) for r in db.query(WorkflowRun)]
            assert ledger == ([('autobackup', '2030-01-02')] if name == 'A' else [])
    assert len(state.writes) == writes


@pytest.mark.parametrize(('method', 'member', 'fault'), [
    ('GET', '/manifest.json', '断连'), ('GET', '/tasks.json', '损坏'),
    ('PUT', '/manifest.json', '断连'),
])
def test_dav_failure_retry_keeps_both_commits(devices, loopback, method, member, fault, tmp_path):
    engines, sync = devices
    dav, state = loopback
    sync('A', dav)
    sync('B', dav)
    expected = []
    for name in ('A', 'B'):
        with Session(engines[name]) as db:
            row = tasks.create_task(db, TaskCreate(title=name + '离线新增'))
            expected.append((row.id, row.title))
    sync('B', dav)
    base = tmp_path / 'A' / 'sync_snapshot'
    before = {p.name: p.read_bytes() for p in base.iterdir()}
    state.hook = lambda m, p: fault if m == method and p.endswith(member) else None
    state.writes.clear()
    with pytest.raises(WebDavError, match='损坏' if fault == '损坏' else '请求失败'):
        sync('A', dav)
    assert {p.name: p.read_bytes() for p in base.iterdir()} == before
    assert state.writes == (['/clara-sync/tables/tasks.json'] if method == 'PUT' else [])
    assert expected[0] in _rows(engines['A'])
    state.hook = lambda *_: None
    sync('A', dav)
    sync('B', dav)
    assert _rows(engines['A']) == _rows(engines['B']) == sorted(expected)


@pytest.mark.parametrize('same_name', [True, False], ids=['canonical-rekey', 'unrelated-replacement'])
def test_natural_identity_survives_total_pk_turnover(devices, loopback, same_name):
    from models import Project

    engines, sync = devices
    dav, _ = loopback
    with Session(engines['A']) as db:
        db.add(Project(id='z', name='原项目', color='#111111', updated_at=datetime(2030, 1, 1)))
        db.commit()
    if same_name:
        with Session(engines['B']) as db:
            db.add(Project(id='a', name='原项目', color='#222222', updated_at=datetime(2030, 1, 2)))
            db.commit()
    sync('A', dav)
    sync('B', dav)
    if not same_name:
        with Session(engines['B']) as db:
            db.delete(db.get(Project, 'z'))
            db.add(Project(id='a', name='无关项目', color='#222222'))
            db.commit()
        sync('B', dav)
        with pytest.raises(se.SyncAbort, match='已被清空'):
            sync('A', dav)
        with Session(engines['A']) as db:
            assert db.get(Project, 'z').name == '原项目'
        return
    sync('A', dav)
    for name in ('A', 'B'):
        assert sync(name, dav)['bytes_pushed'] == 0
        with Session(engines[name]) as db:
            project = db.query(Project).one()
            assert (project.id, project.name, project.color) == ('a', '原项目', '#222222')


def test_three_live_connections_account_for_every_commit(devices, loopback, tmp_path, monkeypatch, record_property):
    """三个工作线程各自持有物理连接；16 波同闸起跑，首波强制一次 SQLite 写锁争用。"""
    import json
    import sqlite3
    from datetime import timedelta
    from threading import Event

    from sqlalchemy.exc import OperationalError
    from services.sync_config import get_sync_config

    engines, sync = devices
    dav, _ = loopback
    rounds = 16
    start = datetime(2030, 1, 1, 8)
    with Session(engines['A']) as db:
        get_sync_config(db)
        ids = [tasks.create_task(db, TaskCreate(title=f'编辑前{i}')).id for i in range(rounds)]
    sync('A', dav)
    sync('B', dav)
    with Session(engines['B']) as db:
        remote = tasks.create_task(db, TaskCreate(title='远端已提交')).id
    sync('B', dav)
    monkeypatch.setattr(se, 'get_data_dir', lambda: tmp_path / 'A')
    gate = Barrier(3, timeout=15)
    held, contended, released = Event(), Event(), Event()

    def worker(role):
        completed, conflicts = [], []
        with engines['A'].connect() as conn, Session(bind=conn) as db:
            physical = id(conn.connection.driver_connection)
            for i in range(rounds):
                gate.wait()
                if role == '编辑':
                    if i == 0:
                        db.execute(text('BEGIN IMMEDIATE'))
                        held.set()
                        assert contended.wait(10)
                    row = tasks.update_task(db, ids[i], TaskUpdate(title=f'编辑后{i}'))
                    completed.append((row.id, row.title))
                    released.set()
                elif role == '认领':
                    day = start + timedelta(days=i)
                    if i == 0:
                        assert held.wait(10)
                        db.execute(text('PRAGMA busy_timeout=0'))
                        try:
                            with pytest.raises(OperationalError) as busy:
                                workflows._claim_autobackup(db, day)
                            assert busy.value.orig.sqlite_errorcode & 255 == sqlite3.SQLITE_BUSY
                            conflicts.append('SQLITE_BUSY')
                            db.rollback()
                        finally:
                            contended.set()
                        assert released.wait(10)
                        db.execute(text('PRAGMA busy_timeout=5000'))
                    assert workflows._claim_autobackup(db, day)
                    completed.append(('autobackup', day.date().isoformat()))
                else:
                    try:
                        se.run_sync(db, dav, SimpleNamespace(device_id='A'))
                        completed.append(i)
                    except se.SyncAbort as exc:
                        assert '规划期间本地 tasks 已变化' in str(exc)
                        db.rollback()
                        conflicts.append(i)
        return physical, completed, conflicts

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(worker, role) for role in ('编辑', '认领', '同步')]
        edit, job, run = [f.result(timeout=90) for f in futures]
    assert len({edit[0], job[0], run[0]}) == 3
    edited = [(tid, f'编辑后{i}') for i, tid in enumerate(ids)]
    ledger = [('autobackup', (start + timedelta(days=i)).date().isoformat()) for i in range(rounds)]
    assert edit[1:] == (edited, []) and job[1:] == (ledger, ['SQLITE_BUSY'])
    assert sorted(run[1] + run[2]) == list(range(rounds))
    record_property('wal_contention', json.dumps(dict(edits=len(edited), claims=len(ledger),
                    busy=len(job[2]), sync_ok=len(run[1]), stale_aborts=len(run[2]))))
    sync('A', dav)
    sync('B', dav)
    for name in ('A', 'B'):
        assert sync(name, dav)['bytes_pushed'] == 0
        assert _rows(engines[name]) == sorted(edited + [(remote, '远端已提交')])
        with Session(engines[name]) as db:
            assert sorted((r.name, r.run_date.isoformat()) for r in db.query(WorkflowRun)) == (
                ledger if name == 'A' else [])
