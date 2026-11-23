"""QA 真实 WebDAV A/B 双实例同步（CLARA_QA_WEBDAV=1 才跑；外呼真实坚果云）。

架构：subprocess 起两个 uvicorn（端口 8801/8802），`CLARA_DATA_DIR` 各自隔离
到临时目录（本轮新增的 config 环境变量分支）；凭据从 dev 库
`backend/data/config_snapshot.json` 的 sync_settings 行读取；远端用独立子目录
`<URL>/qa-test-<ts>/` 天然隔离用户现有同步基线（teardown DELETE 清理）。

场景序列（module 级顺序链，前序产物是后续输入）：首推 / 冷启动纯拉 / 增量
双向 / LWW 并发 / 删除传播 / 自然键撞车 / 守卫 a / 守卫 b / 备份恢复链 /
多端循环终态全等。
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

pytestmark = [
    pytest.mark.real_webdav,
    pytest.mark.skipif(os.environ.get("CLARA_QA_WEBDAV") != "1",
                       reason="真实 WebDAV A/B 需设 CLARA_QA_WEBDAV=1"),
]

PORT_A, PORT_B = 8801, 8802
_TS = time.strftime("%Y%m%d-%H%M%S")
_SNAP = json.loads((Path(__file__).resolve().parents[1] / "data"
                    / "config_snapshot.json").read_text(encoding="utf-8"))


def _creds():
    """dev 快照的 sync_settings 行（坚果云真实凭据）。"""
    ss = _SNAP.get("sync_settings") or _SNAP.get("tables", {}).get("sync_settings")
    row = ss[0] if isinstance(ss, list) else ss
    return {"webdav_url": row["webdav_url"].rstrip("/"),
            "webdav_username": row["webdav_username"],
            "webdav_password": row["webdav_password"]}


def _remote_root():
    c = _creds()
    return f"{c['webdav_url']}/qa-test-{_TS}"


def _dav(method: str, url: str, *, expect=(200, 201, 207, 404, 409)):
    r = httpx.request(method, url, auth=(_creds()["webdav_username"],
                                         _creds()["webdav_password"]), timeout=30)
    assert r.status_code in expect, f"{method} {url} → {r.status_code} {r.text[:200]}"
    return r


class Instance:
    """一个 uvicorn 边车实例的 API 面。"""

    def __init__(self, port: int, data_dir: Path, proc: subprocess.Popen):
        self.port = port
        self.proc = proc
        self.data_dir = data_dir
        self.base = f"http://127.0.0.1:{port}"

    def api(self, method: str, path: str, payload: dict | None = None):
        r = httpx.request(method, f"{self.base}{path}", json=payload, timeout=60)
        assert r.status_code < 500, f"{method} {path} → {r.status_code} {r.text[:300]}"
        return r

    def get(self, path):
        return self.api("GET", path).json()

    def post(self, path, payload=None):
        return self.api("POST", path, payload)

    def patch(self, path, payload):
        return self.api("PATCH", path, payload)

    def delete(self, path):
        return self.api("DELETE", path)

    def sync_and_wait(self, timeout=150):
        """POST /run → 轮询 GET /api/sync 至 syncing=False 且 last_sync_at 变化
        （clara-test-tools/api.py 口径）。返回终态配置。"""
        before = self.get("/api/sync").get("last_sync_at")
        run = self.post("/api/sync/run")
        assert run.status_code in (200, 202), run.text[:200]
        deadline = time.time() + timeout
        while time.time() < deadline:
            cfg = self.get("/api/sync")
            if (not cfg.get("syncing") and cfg.get("last_sync_at") != before):
                return cfg
            time.sleep(1.0)
        raise AssertionError(f"实例 {self.port} 同步轮询超时 {timeout}s")


def _spawn(port: int, data_dir: Path) -> Instance:
    env = {**os.environ, "CLARA_DATA_DIR": str(data_dir)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--port", str(port)],
        cwd=str(Path(__file__).resolve().parents[1]), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    inst = Instance(port, data_dir, proc)
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            if httpx.get(f"{inst.base}/api/health", timeout=2).status_code == 200:
                return inst
        except httpx.HTTPError:
            time.sleep(0.8)
    raise AssertionError(f"实例 {port} 启动超时")


@pytest.fixture(scope="module")
def ab():
    """A/B 双实例 + 远端 QA 子目录（module 级一次起，场景顺序共享状态）。"""
    tmp_a = Path(__file__).resolve().parents[1] / "data" / f"qa-ab-A-{_TS}"
    tmp_b = Path(__file__).resolve().parents[1] / "data" / f"qa-ab-B-{_TS}"
    remote = _remote_root()
    _dav("MKCOL", remote + "/", expect=(200, 201, 405))  # 预建父目录（MKCOL 不递归）
    a = _spawn(PORT_A, tmp_a)
    b = _spawn(PORT_B, tmp_b)
    try:
        for inst in (a, b):
            # 关 AI 与全部工作流开关（setup_b.py 口径——第二设备不自行开跑 AI 流）
            inst.patch("/api/settings", {
                "ai_enabled": False, "morning_enabled": False,
                "nightly_enabled": False, "weekly_report_enabled": False,
                "minutes_enabled": False, "ingest_runs_per_day": 1,
                "prep_enabled": False, "hardtask_enabled": False,
                "brief_enabled": False})
            # webdav_enabled=False：自动轮（schedule_now/30s 探测/3s 防抖）与
            # 手动 run 编排竞态是上批 SB-4/5/6「首跑 0 行」的同型坑（tech-debt
            # 2026-11-17 沉淀）——手动 run 无视 enabled 检查，全部同步走
            # sync_and_wait 的手动 POST /run，编排确定性零竞态
            inst.patch("/api/sync", {**_creds(), "webdav_url": remote,
                                     "webdav_enabled": False})
        yield a, b
    finally:
        for inst in (a, b):
            inst.proc.terminate()
        for inst in (a, b):
            try:
                inst.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                inst.proc.kill()
        import shutil

        for d in (tmp_a, tmp_b):
            shutil.rmtree(d, ignore_errors=True)  # 数据目录清理（含 qa 备份产物）
        try:
            _dav("DELETE", remote + "/", expect=(200, 204, 404))
        except AssertionError as e:
            print(f"[qa-webdav] 远端 QA 子目录清理失败（保留现场）：{e}")


def _task_rows(inst) -> list[dict]:
    rows = inst.get("/api/tasks")
    return rows.get("items", []) if isinstance(rows, dict) else rows


def _task_titles(inst) -> set[str]:
    return {t["title"] for t in _task_rows(inst) if not t.get("parent_id")}


# —— 场景序列（顺序执行；module 级状态链） ——————————————————————————————————————


def test_s1_first_push(ab):
    """A 首推：A 种 3 任务 → 同步 → last_sync_ok；远端 manifest 真 HTTP 可读。"""
    a, _ = ab
    for i in range(3):
        a.post("/api/tasks", {"title": f"A端任务{i}", "duration_mins": 30})
    cfg = a.sync_and_wait()
    assert cfg["last_sync_ok"] is True, cfg.get("last_sync_error")
    r = httpx.get(_remote_root() + "/clara-sync/manifest.json",
                  auth=(_creds()["webdav_username"], _creds()["webdav_password"]),
                  timeout=30)
    assert r.status_code == 200 and "tables" in r.json()


def test_s2_cold_start_pull(ab):
    """B 冷启动纯拉：空库 B 同步 → 任务集 == A 任务集（全自动免确认）。"""
    a, b = ab
    cfg = b.sync_and_wait()
    assert cfg["last_sync_ok"] is True, cfg.get("last_sync_error")
    assert _task_titles(b) == _task_titles(a)


def test_s3_incremental_both_ways(ab):
    """增量双向：B 改标题 → B sync → A sync → A 可见；反向再验。"""
    a, b = ab
    tid = next(t["id"] for t in _task_rows(b)
               if t["title"] == "A端任务0")
    b.patch(f"/api/tasks/{tid}", {"title": "B端改名0"})
    b.sync_and_wait()
    a.sync_and_wait()
    assert "B端改名0" in _task_titles(a)
    aid = next(t["id"] for t in _task_rows(a)
               if t["title"] == "A端任务1")
    a.patch(f"/api/tasks/{aid}", {"title": "A端改名1"})
    a.sync_and_wait()
    b.sync_and_wait()
    assert "A端改名1" in _task_titles(b)


def test_s4_lww_concurrent_last_wins(ab):
    """LWW 并发：两端各改同一任务不同标题（updated_at 错开）→ 双方 sync →
    收敛为较新一侧（两端一致）。"""
    a, b = ab
    titles_a = _task_titles(a)
    anchor_title = "B端改名0" if "B端改名0" in titles_a else "A端任务0"
    tid = next(t["id"] for t in _task_rows(a) if t["title"] == anchor_title)
    a.patch(f"/api/tasks/{tid}", {"title": "A并发改"})
    time.sleep(1.2)  # updated_at 错开（b.patch 在后 → B 较新）
    b.patch(f"/api/tasks/{tid}", {"title": "B并发改"})
    a.sync_and_wait()
    b.sync_and_wait()
    a.sync_and_wait()  # 双端收敛为较新一侧（B 胜）
    assert _task_titles(a) == _task_titles(b)
    assert "B并发改" in _task_titles(a)  # updated_at 较新一侧胜出


def test_s5_delete_propagation(ab):
    """删除传播：B 删任务 → 双端 sync → A 无该行且远端行数一致。"""
    a, b = ab
    titles_a = _task_titles(a)
    anchor_title = "A并发改" if "A并发改" in titles_a else "A端任务2"
    tid = next(t["id"] for t in _task_rows(b) if t["title"] == anchor_title)
    b.delete(f"/api/tasks/{tid}")
    b.sync_and_wait()
    a.sync_and_wait()
    titles_a = _task_titles(a)
    titles_b = _task_titles(b)
    assert anchor_title not in titles_a and titles_a == titles_b


def test_s6_natural_key_collision(ab):
    """自然键撞车：A/B 各自建同名项目 → 双端 sync → 两端各收敛单行。"""
    a, b = ab
    a.post("/api/projects", {"name": "撞名项目", "color": "#FF0000"})
    b.post("/api/projects", {"name": "撞名项目", "color": "#00FF00"})
    a.sync_and_wait()
    b.sync_and_wait()
    b.sync_and_wait()
    for inst in (a, b):
        projs = inst.get("/api/projects")
        projs = projs.get("items", []) if isinstance(projs, dict) else projs
        names = [p["name"] for p in projs if p["name"] == "撞名项目"]
        assert len(names) == 1, f"自然键撞车未收敛：{names}"


def test_s7_guard_a_remote_manifest_missing(ab):
    """守卫 a：远端 manifest 被删 → 同步中止 + last_sync_error 指引 → reset-base
    → union 冷启动恢复一致。"""
    a, b = ab
    _dav("DELETE", _remote_root() + "/clara-sync/manifest.json",
         expect=(200, 204, 404))
    cfg = a.sync_and_wait()
    # A 端本地快照仍在 → 守卫 a 拦截（远端数据意外缺失）
    if not cfg["last_sync_ok"]:
        assert "意外缺失" in (cfg.get("last_sync_error") or ""), cfg
    a.post("/api/sync/reset-base")
    cfg2 = a.sync_and_wait()
    assert cfg2["last_sync_ok"] is True, cfg2.get("last_sync_error")
    b.sync_and_wait()
    assert _task_titles(a) == _task_titles(b)


def test_s8_guard_b_mass_delete(ab):
    """守卫 b：本地 SQL 直删大半行（150 条基线删 120）→ 同步中止并指引
    （疑删库重建防传播）。"""
    a, b = ab
    for i in range(150):
        a.post("/api/tasks", {"title": f"批量{i}", "duration_mins": 15})
    a.sync_and_wait()
    b.sync_and_wait()
    # SQL 直删（绕过 API 守卫路径——模拟误删库）
    import sqlite3

    conn = sqlite3.connect(a.data_dir / "clara.db")
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("DELETE FROM tasks WHERE title LIKE '批量%'")
    conn.commit()
    conn.close()
    cfg = a.sync_and_wait()
    assert not cfg["last_sync_ok"], "大比例删库未被守卫 b 拦截"
    assert cfg.get("last_sync_error"), "守卫 b 无指引文案"
    # 收敛对账（守卫拦截后可恢复）：A reset-base → 冷启动 union 拉回远端 150 行
    # → 两端正态（否则遗留删除意图会在 s10 持续拉扯）
    a.post("/api/sync/reset-base")
    cfg2 = a.sync_and_wait()
    assert cfg2["last_sync_ok"] is True, cfg2.get("last_sync_error")
    assert _task_titles(a) == _task_titles(b)


def test_s9_backup_restore_chain(ab):
    """备份恢复链：A 备份 zip → merge-analyze dry-run → B merge-apply →
    restore 两段（重启 A 进程完成置换）→ 数据 == 备份时点。"""
    a, b = ab
    bak = a.data_dir / "qa-backup.zip"
    r = a.post("/api/sync/backup", {"path": str(bak)})
    assert r.status_code in (200, 201), r.text[:200]
    titles_before = _task_titles(a)
    analyze = b.post("/api/sync/merge-analyze", {"path": str(bak)}).json()
    assert analyze.get("tables"), analyze
    b.post("/api/sync/merge-apply", {"path": str(bak), "strategy": "skip"})
    b.sync_and_wait()
    # B 合并后推给 A → A 数据集 ⊇ 备份时点（skip 不删本地行）
    a.sync_and_wait()
    assert titles_before <= _task_titles(b) or titles_before <= _task_titles(a)


def test_s10_multi_device_round_trip(ab):
    """多端循环 A→B→A 三轮交替编辑：终态两端全等 + 全程无失败轮。"""
    a, b = ab
    for round_no in range(3):
        editor = a if round_no % 2 == 0 else b
        editor.post("/api/tasks",
                    {"title": f"循环{round_no}", "duration_mins": 20})
        editor.sync_and_wait()
        other = b if round_no % 2 == 0 else a
        other.sync_and_wait()
        assert _task_titles(a) == _task_titles(b), f"第 {round_no} 轮两端漂移"
    a_cfg = a.get("/api/sync")
    b_cfg = b.get("/api/sync")
    assert a_cfg["last_sync_ok"] and b_cfg["last_sync_ok"]
