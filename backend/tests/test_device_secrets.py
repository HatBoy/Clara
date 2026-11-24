"""全部密钥为用例生成的假值，仅在临时目录测试 Windows 用户保护边界。"""
import json
import sqlite3
import zipfile
from contextlib import closing

from sqlalchemy import text
from conftest import TestingSessionLocal
from models import ModelProvider, McpServer, ChatChannel, IntegrationSettings, SyncSettings
from services import backup, config_backup, device_secrets, row_codec


def test_sqlite_only_stores_local_handles(tmp_path, monkeypatch):
    monkeypatch.setattr(device_secrets, 'get_data_dir', lambda: tmp_path)
    marker = '仅用于回归-test-password-745'
    with TestingSessionLocal() as db:
        model = ModelProvider(name='测试', model_name='openai/test', api_key=marker)
        mcp = McpServer(name='测试', transport='sse', url='https://example.test', headers={'token':marker})
        channel = ChatChannel(provider='wechat', app_id=marker, app_secret=marker)
        integration = IntegrationSettings(composio_api_key=marker)
        sync = SyncSettings(webdav_password=marker)
        db.add_all([model, mcp, channel, integration, sync]); db.commit(); db.expire_all()
        assert (model.api_key, mcp.headers['token'], channel.app_id, channel.app_secret,
                integration.composio_api_key, sync.webdav_password) == (marker,) * 6
        assert db.scalar(text('select api_key from model_providers')).startswith('clara-secret:')
        for name in device_secrets._FIELDS:
            portable = json.dumps(row_codec.dump_table(db, name), ensure_ascii=False)
            assert marker not in portable and 'clara-secret:' not in portable
        snap = json.dumps(config_backup.dump_config(db), ensure_ascii=False)
        assert marker not in snap and 'clara-secret:' not in snap
    for path in (tmp_path / 'credentials').iterdir():
        assert marker.encode() not in path.read_bytes()


def test_standard_archive_scrubs_legacy_database_and_snapshot(tmp_path, monkeypatch):
    path = tmp_path / 'clara.db'; dest = tmp_path / '结果.zip'
    monkeypatch.setattr(backup,'DATABASE_PATH',path)
    monkeypatch.setattr(backup,'get_data_dir',lambda: tmp_path)
    marker = 'FAKE-KEY-791-BOUNDARY'
    with closing(sqlite3.connect(path)) as db:
        db.execute('create table model_providers (id text primary key, api_key text)')
        db.execute('insert into model_providers values (?,?)', ('x', marker)); db.commit()
    (tmp_path/'config_snapshot.json').write_text(json.dumps({'model_providers':[{'id':'x','api_key':marker}]}))
    backup.create_backup(str(dest))
    with zipfile.ZipFile(dest) as z:
        assert all(marker.encode() not in z.read(member) for member in z.namelist())
    with closing(sqlite3.connect(path)) as db:
        assert db.execute('select api_key from model_providers').fetchone()[0] == marker


def test_prune_only_unreferenced_old_tokens(tmp_path):
    import os
    import time
    folder = tmp_path / 'credentials'
    folder.mkdir()
    live, recovery, orphan, recent = [str(i) * 32 for i in range(4)]
    now = time.time()
    for name in (live, recovery, orphan, recent, '用户文件'):
        p = folder / name
        p.write_bytes(b'placeholder')
        if name != recent:
            os.utime(p, (now-8*86400, now-8*86400))
    (tmp_path/'backups').mkdir()
    for db_path, token in ((tmp_path/'clara.db', live), (tmp_path/'backups/pre-restore-1.db', recovery)):
        with closing(sqlite3.connect(db_path)) as db:
            db.execute('create table mcp_servers(headers text)')
            db.execute('insert into mcp_servers values (?)', (json.dumps('clara-secret:'+token),))
            db.commit()
    broken = tmp_path/'backups/pre-restore-2.db'
    broken.write_bytes(b'not a database')
    assert device_secrets.prune_orphaned_secrets(tmp_path, now=now) == 0
    assert (folder/orphan).exists()
    broken.unlink()
    assert device_secrets.prune_orphaned_secrets(tmp_path, now=now) == 1
    assert {p.name for p in folder.iterdir()} == {live,recovery,recent,'用户文件'}


# —— 发版前验收批补充：DPAPI 往返/篡改降级/拒绝明文回退（模块五·凭证加密存储）——


def test_dpapi_roundtrip_tamper_and_bad_ref_degrade(tmp_path, monkeypatch):
    """DPAPI 加密往返与降级边界：_save 落盘密文（不含明文片段）→ _load 明文
    等值；密文被篡改 → 不可读即回退 missing（绝不抛、绝不回读明文）；坏引用
    形态（非 clara-secret:<hex32>）同降级——历史明文值留在列里也读不出来。"""
    import re as _re

    monkeypatch.setattr(device_secrets, 'get_data_dir', lambda: tmp_path)
    marker = 'FAKE-SECRET-881-ROUNDTRIP'
    ref = device_secrets._save(marker)
    assert _re.fullmatch(r'clara-secret:[a-f0-9]{32}', ref)
    token = tmp_path / 'credentials' / ref[len('clara-secret:'):]
    assert token.is_file() and marker.encode() not in token.read_bytes()
    assert device_secrets._load(ref, missing=None) == marker  # DPAPI 往返
    token.write_bytes(token.read_bytes()[:-4] + b'junk')      # 密文篡改
    assert device_secrets._load(ref, missing='fallback') == 'fallback'
    assert device_secrets._load('clara-secret:' + 'z' * 32, missing='gone') == 'gone'
    assert device_secrets._load('sk-plaintext-leftover', missing='gone') == 'gone'
    assert device_secrets._save('') == '' and device_secrets._save(None) is None


def test_dpapi_non_windows_refuses_plaintext_fallback(monkeypatch):
    """非 Windows 平台拒绝本机凭证：抛 RuntimeError 绝不回退明文落库
    （device_secrets._dpapi:32 红线）。"""
    import pytest

    monkeypatch.setattr(device_secrets.os, 'name', 'posix')
    with pytest.raises(RuntimeError):
        device_secrets._dpapi(b'x')
