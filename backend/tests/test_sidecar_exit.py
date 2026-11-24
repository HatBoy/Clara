"""用宿主实际的标准输入协议退出源码边车；远端只是回环内存替身。"""
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest


def test_managed_exit_flushes_new_task(tmp_path):
    files = {}
    class Dav(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def reply(self, code, body=b''):
            self.send_response(code)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def do_MKCOL(self): self.reply(201)
        def do_GET(self): self.reply(200 if self.path in files else 404, files.get(self.path, b''))
        def do_PUT(self):
            files[self.path] = self.rfile.read(int(self.headers['Content-Length']))
            self.reply(201)
    with ThreadingHTTPServer(('127.0.0.1', 0), Dav) as dav, socket.socket() as port_probe:
        threading.Thread(target=dav.serve_forever, daemon=True).start()
        port_probe.bind(('127.0.0.1', 0))
        port = port_probe.getsockname()[1]
        port_probe.close()
        env = {**os.environ, 'CLARA_DATA_DIR': str(tmp_path / '实例'), 'PYTHONUTF8':'1'}
        with (tmp_path / '边车日志').open('wb') as log:
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve().parents[1] / 'main.py'),
                                        '--port', str(port), '--host-managed'], env=env,
                                        stdin=subprocess.PIPE, stdout=log, stderr=log)
            try:
                with httpx.Client(base_url=f'http://127.0.0.1:{port}', trust_env=False, timeout=2) as client:
                    until = time.monotonic() + 45
                    while True:
                        try:
                            if client.get('/api/health').is_success: break
                        except httpx.TransportError: pass
                        assert process.poll() is None and time.monotonic() < until
                        threading.Event().wait(.1)
                    client.patch('/api/sync', json={'webdav_url':f'http://127.0.0.1:{dav.server_port}',
                                                    'webdav_enabled':True}).raise_for_status()
                    client.post('/api/tasks', json={'title':'退出前最后一笔'}).raise_for_status()
                    process.stdin.write(b'clara:quit\n')
                    process.stdin.flush()
                assert process.wait(timeout=24) == 0
                rows = json.loads(files['/clara-sync/tables/tasks.json'])['rows']
                assert any(row['title'] == '退出前最后一笔' for row in rows)
                assert '/clara-sync/manifest.json' in files
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                process.stdin.close()
                dav.shutdown()


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows 控制台事件')
def test_reload_ctrl_c_with_open_subscriptions(tmp_path):
    """不靠测试客户端关流或第二次 Ctrl+C 解围；父子进程必须正常收尾。"""
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    # 独立控制台；显式恢复 Ctrl+C（CI/终端工具可能继承了忽略标志）。
    launch = ('import ctypes; assert ctypes.windll.kernel32.SetConsoleCtrlHandler(None,0); '
              'from uvicorn.main import main; main()')
    interrupt = ('import ctypes,sys,time; k=ctypes.windll.kernel32; k.FreeConsole(); '
                 'assert k.AttachConsole(int(sys.argv[1])); assert k.SetConsoleCtrlHandler(None,1); '
                 'assert k.GenerateConsoleCtrlEvent(0,0); time.sleep(.3)')
    log_path = tmp_path / 'reload.log'
    with log_path.open('wb') as log:
        proc = subprocess.Popen([sys.executable, '-c', launch, 'main:app', '--reload', '--port', str(port)],
                                cwd=Path(__file__).resolve().parents[1], stdout=log, stderr=log,
                                creationflags=subprocess.CREATE_NEW_CONSOLE,
                                env={**os.environ, 'CLARA_DATA_DIR': str(tmp_path), 'PYTHONUTF8': '1'})
        try:
            with httpx.Client(base_url=f'http://127.0.0.1:{port}', trust_env=False) as client:
                until = time.monotonic() + 45
                while True:
                    try:
                        if client.get('/api/health').is_success: break
                    except httpx.TransportError: pass
                    assert proc.poll() is None and time.monotonic() < until
                    time.sleep(.1)
                with client.stream('GET', '/api/reminders/stream') as reminders, \
                     client.stream('GET', '/api/ai/stream') as workflows:
                    streams = [reminders.iter_lines(), workflows.iter_lines()]
                    for lines in streams:
                        assert next(lines) == ': connected'
                    subprocess.run([sys.executable, '-c', interrupt, str(proc.pid)], check=True, timeout=5)
                    assert proc.wait(timeout=12) == 0
            output = log_path.read_text('utf-8', errors='replace')
            assert 'Application shutdown complete' in output and 'Stopping reloader process' in output
        finally:
            if proc.poll() is None:
                subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'], capture_output=True)
                proc.wait(timeout=10)
