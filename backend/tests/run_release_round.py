"""独立运行一次源码候选验收：python tests/run_release_round.py <全新输出目录>。"""
import ctypes
import json
import os
import platform
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

BASE = Path(__file__).resolve().parents[1]


def rss_windows(process):
    """从自身启动的一个进程读取工作集；两项均为字节。"""
    from ctypes import wintypes
    class Counters(ctypes.Structure):
        _fields_ = [('cb', wintypes.DWORD), ('faults', wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in ('peak', 'rss', 'pp', 'p', 'np', 'n', 'page', 'peak_page')]
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    if not ctypes.windll.psapi.GetProcessMemoryInfo(wintypes.HANDLE(int(process._handle)),
                                                   ctypes.byref(counters), counters.cb):
        raise ctypes.WinError()
    return dict(rss=counters.rss, peak_rss=counters.peak)


def measure(out, idx):
    with tempfile.TemporaryDirectory(prefix='clara-release-') as data, socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
        probe.close()
        env = dict(os.environ, CLARA_DATA_DIR=data, PYTHONUTF8='1')
        with (out / f'sidecar-{idx}.log').open('wb') as log, httpx.Client(trust_env=False, timeout=1) as client:
            t = time.perf_counter()
            proc = subprocess.Popen([sys.executable, 'main.py', '--port', str(port), '--host-managed'],
                                    cwd=BASE, env=env, stdout=log, stderr=log, stdin=subprocess.PIPE)
            try:
                while time.perf_counter()-t < 60 and proc.poll() is None:
                    try:
                        if client.get(f'http://127.0.0.1:{port}/api/health').json()['status']=='ok':
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(.025)
                else:
                    raise RuntimeError('临时边车未就绪')
                sample = dict(start_ms=round((time.perf_counter()-t)*1000, 2))
                if sys.platform == 'win32':
                    sample.update(rss_windows(proc))
                if idx == 0:
                    subprocess.run(['node', 'tests/release_browser.mjs', str(port)], cwd=BASE, env=env,
                                   timeout=120, check=True)
                proc.stdin.write(b'clara:quit\n')
                proc.stdin.flush()
                assert proc.wait(timeout=30) == 0
                return sample
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                proc.stdin.close()


def main():
    out = Path(sys.argv[1]).resolve()
    out.mkdir(parents=True, exist_ok=False)
    samples = [measure(out, i) for i in range(5)]
    (out/'startup.json').write_text(json.dumps(dict(python=sys.version, system=platform.platform(), samples=samples), indent=2), encoding='utf-8')
    with (out/'pytest.log').open('wb') as log:
        return subprocess.call([sys.executable, '-m', 'pytest', '-q', '-o', 'junit_logging=system-out',
                                f'--junitxml={out / "results.xml"}'], cwd=BASE, stdout=log, stderr=log,
                               env=dict(os.environ, PYTHONUTF8='1', PYTHONHASHSEED='42'))


if __name__ == '__main__':
    sys.exit(main())
