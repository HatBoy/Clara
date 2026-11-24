"""设备授权：SQLite 只留随机引用，本机文件由当前 Windows 用户 DPAPI 加密。"""
import ctypes
import json
import os
import re
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

from sqlalchemy import JSON, String
from sqlalchemy.types import TypeDecorator

from config import get_data_dir

_PREFIX = 'clara-secret:'
_FIELDS = {
    'model_providers': ('api_key',), 'integration_settings': ('composio_api_key',),
    'sync_settings': ('webdav_password',), 'chat_channels': ('app_id', 'app_secret'),
    'mcp_servers': ('env', 'headers'),
}
_DEST = {'model_providers': ('base_url', 'model_name'), 'mcp_servers': ('transport', 'command', 'args', 'url'),
         'chat_channels': ('provider',), 'sync_settings': ('webdav_url', 'webdav_username')}


class _Blob(ctypes.Structure):
    _fields_ = [('size', ctypes.c_uint32), ('data', ctypes.c_void_p)]


def _dpapi(data: bytes, *, decrypt=False) -> bytes:
    if os.name != 'nt':
        raise RuntimeError('本机凭证需要 Windows 用户凭据保护，不会回退为明文')
    buf = ctypes.create_string_buffer(data)
    source, dest = _Blob(len(data), ctypes.cast(buf, ctypes.c_void_p)), _Blob()
    crypt = ctypes.windll.crypt32
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(dest)):
        raise OSError('当前用户无法访问本机凭证，请重新授权')
    try:
        return ctypes.string_at(dest.data, dest.size)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.c_void_p(dest.data))


def _save(value):
    if value is None or value == '':
        return value
    data = _dpapi(json.dumps(value, ensure_ascii=False).encode('utf8'))
    folder = get_data_dir() / 'credentials'
    folder.mkdir(exist_ok=True)
    token = uuid.uuid4().hex
    with (folder / token).open('xb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    return _PREFIX + token


def _load(ref, missing):
    if not ref:
        return ref
    if not isinstance(ref, str) or not re.fullmatch(_PREFIX + '[a-f0-9]{32}', ref):
        return missing
    try:
        data = (get_data_dir() / 'credentials' / ref[len(_PREFIX):]).read_bytes()
        return json.loads(_dpapi(data, decrypt=True))
    except (OSError, ValueError):
        return missing


class SecretText(TypeDecorator):
    impl = String
    cache_ok = True

    def __init__(self, length, *, missing=None):
        self.missing = missing
        super().__init__(length)

    def process_bind_param(self, value, dialect):
        return _save(value)

    def process_result_value(self, value, dialect):
        return _load(value, self.missing)


class SecretMap(TypeDecorator):
    impl = JSON(none_as_null=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return _save(value)

    def process_result_value(self, value, dialect):
        return _load(value, None)


def portable(name, row):
    """转换任何版本交换行；必填渠道字段保留空占位，其他秘密不发键。"""
    if name not in _FIELDS:
        return row
    out = dict(row)
    for field in _FIELDS[name]:
        if name == 'chat_channels':
            out[field] = ''
        else:
            out.pop(field, None)
    if name == "chat_channels":
        for key in ("conn_ok", "conn_at", "conn_error", "push_target"):
            if key in out:
                out[key] = None
    return out


def portable_update(name, incoming, local):
    """同身份更新不碰本机授权，换端点则清除。"""
    out = dict(incoming)
    changed = any(k in incoming and incoming[k] != local.get(k) for k in _DEST.get(name, ()))
    for k in _FIELDS.get(name, ()):
        if changed:
            out[k] = '' if name == 'chat_channels' else None
        else:
            out.pop(k, None)
    if name == "chat_channels" and not changed:
        for key in ("conn_ok", "conn_at", "conn_error", "push_target"):
            out.pop(key, None)
    return out


def strip_database(path: Path):
    """仅处理交换副本；重建文件，防止释放页残留旧明文或引用。"""
    with closing(sqlite3.connect(path)) as db:
        db.execute('PRAGMA journal_mode=DELETE')
        db.execute('PRAGMA secure_delete=ON')
        for name, fields in _FIELDS.items():
            # 副本净化不能因触发器把历史备份伪装成较新的业务编辑。
            db.execute(f'DROP TRIGGER IF EXISTS trg_{name}_sync_ua')
            cols = {r[1] for r in db.execute(f'PRAGMA table_info({name})')}
            for field in fields:
                if field in cols:
                    db.execute(f'UPDATE {name} SET {field}=? WHERE {field} IS NOT NULL',
                               ('' if name == 'chat_channels' else None,))
        db.commit()
        db.execute('VACUUM')


def migrate_local(engine):
    """升级开发阶段的历史明文列；失败回滚，不降级。"""
    with engine.begin() as conn:
        for name, fields in _FIELDS.items():
            for field in fields:
                for rowid, raw in conn.exec_driver_sql(f'SELECT rowid,{field} FROM {name} WHERE {field} IS NOT NULL').all():
                    val = json.loads(raw) if name == 'mcp_servers' else raw
                    if val is None or val == '' or (isinstance(val, str) and re.fullmatch(_PREFIX + '[a-f0-9]{32}', val)):
                        continue
                    token = _save(val)
                    conn.exec_driver_sql(f'UPDATE {name} SET {field}=? WHERE rowid=?',
                                         (json.dumps(token) if name == 'mcp_servers' else token, rowid))


def prune_orphaned_secrets(data_dir: Path, *, now=None) -> int:
    """启动时回收七天前的无主密文；任一恢复副本不可读即不删，不解密凭证。"""
    import logging
    import time

    folder = data_dir / 'credentials'
    if not folder.is_dir() or folder.is_symlink():
        return 0
    cutoff = (time.time() if now is None else now) - 7 * 86400
    try:
        candidates = {p.name for p in folder.iterdir() if re.fullmatch('[a-f0-9]{32}', p.name)
                      and not p.is_symlink() and p.is_file() and p.stat().st_mtime < cutoff}
        if not candidates:
            return 0
        live = data_dir / 'clara.db'
        backups = data_dir / 'backups'
        sources = [live, *backups.glob('pre-restore-*.db')]
        for path in sources:
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
                for table, fields in _FIELDS.items():
                    cols = {c[1] for c in db.execute(f'PRAGMA table_info({table})')}
                    for key in cols.intersection(fields):
                        for (value,) in db.execute(f'SELECT {key} FROM {table} WHERE {key} IS NOT NULL'):
                            if table == 'mcp_servers':
                                value = json.loads(value)
                            if isinstance(value, str) and value.startswith(_PREFIX):
                                candidates.discard(value[len(_PREFIX):])
        for token in candidates:
            (folder / token).unlink(missing_ok=True)
        return len(candidates)
    except (OSError, sqlite3.Error, ValueError) as exc:
        logging.getLogger(__name__).warning('凭证维护未完成（%s），保留未清理文件', type(exc).__name__)
        return 0
