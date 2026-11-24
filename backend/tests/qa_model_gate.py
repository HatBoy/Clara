"""真模型测试的限流转发器。SDK 只拿到回环地址，不接触供应商凭据。"""
import os
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import urlsplit

import httpx


# 三轮共享供应商尝试上限（2026-11-24 验收批 30→40：真实层 13 例 + SDK/空正文
# 重试余量；RC8 批 12 场景实测 30 触顶致记忆例外用例未跑完）
BUDGET_ATTEMPTS = 40


def reserve_attempt(directory):
    """三轮共用一个专属目录；先原子占位再发包，崩溃或失败也不退额度。"""
    for i in range(1, BUDGET_ATTEMPTS + 1):
        try:
            (directory / f'provider-{i:02}.used').touch(exist_ok=False)
        except FileExistsError:
            continue
        return True
    return False


def dedicated_config():
    model, key, url, ledger = (os.environ.get('CLARA_QA_MODEL' + suffix, '').strip()
                               for suffix in ('', '_KEY', '_URL', '_BUDGET_DIR'))
    if not (model.startswith('openai/') and model[7:] and key and url and ledger):
        return None
    parts = urlsplit(url)
    directory = Path(ledger)
    if (parts.scheme != 'https' or not parts.hostname or parts.username is not None
            or parts.query or parts.fragment or not directory.is_absolute() or not directory.is_dir()):
        return None
    return model, key, url.rstrip('/'), directory


@contextmanager
def bounded_relay(key, base_url, directory):
    # 每个 SDK/空正文重试都是独立的入站请求；上游不跟重定向、不读环境代理、无重试。
    from secrets import token_hex

    local_key = token_hex(24)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # 不记录模型输入或鉴权信息

        def do_POST(self):
            if self.path != '/v1/chat/completions' or self.headers.get('Authorization') != 'Bearer ' + local_key:
                self.send_error(403)
                return
            body = self.rfile.read(int(self.headers['Content-Length']))
            if not reserve_attempt(directory):
                server.blocked = True
                self.send_error(403, 'QA request budget exhausted')
                return
            server.sent = True
            with httpx.Client(trust_env=False, timeout=120) as client:
                with client.stream('POST', base_url + '/chat/completions', content=body,
                                   headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'}) as response:
                    if response.status_code != 200:
                        server.failed = True
                        self.send_error(502, 'QA provider failed')
                        return
                    self.send_response(200)
                    self.send_header('Content-Type', response.headers.get('Content-Type', 'application/json'))
                    self.end_headers()
                    for chunk in response.iter_bytes():
                        self.wfile.write(chunk)

    class RelayServer(ThreadingHTTPServer):
        def handle_error(self, request, client_address):
            self.failed = True  # 不打印含端点、请求或凭据的上游异常

    with RelayServer(('127.0.0.1', 0), Handler) as server:
        server.daemon_threads = False
        server.sent = server.blocked = server.failed = False
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f'http://127.0.0.1:{server.server_port}/v1', local_key, server
        finally:
            server.shutdown()
            thread.join()
