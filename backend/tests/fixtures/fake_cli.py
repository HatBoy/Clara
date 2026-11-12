"""假 CLI 测试替身：按第一个参数分支，模拟真实 CLI 的 JSON 契约与失败形态。

由 test_cli_tools / test_data_ingest 注册为 fake 适配器（launchers 指向
(sys.executable, 本文件)）——真实适配器的采集命令待真机实测，管道行为全走本替身
（同 mcp_session 假 client 替身惯例）。环境变量注入（FAKE_*）控制授权态，无需改脚本。

子命令：version / auth-status（FAKE_AUTHED=1 授权态，否则 stderr 报未授权退 1）/
auth-refresh（lark 真机形态：tokenStatus=needs_refresh——access token 过期待刷、
refresh token 仍在，授权事实成立）/ auth-start（stdout 印授权 URL）/ fetch·fetch2
（窗内窗外条目各若干，fetch2 为第二采集域供多域合并用例；FAKE_NOW 注入固定基准
时间）/ slow（30s 超时用）/ fail（stderr 报错退 1）/ big（3MB 输出钳制用）/ gbk
（GBK 编码输出）。
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "version":
        print(json.dumps({"version": "1.0.0-fake"}))
    elif cmd == "auth-status":
        if os.environ.get("FAKE_AUTHED") != "1":
            print("not authenticated: token missing or expired", file=sys.stderr)
            sys.exit(1)
        print(json.dumps({"ok": True}))
    elif cmd == "auth-refresh":
        print(json.dumps([{"appId": "cli_fake", "tokenStatus": "needs_refresh"}]))
    elif cmd == "auth-start":
        print("请在浏览器完成授权：https://example.com/auth?code=abc123")
    elif cmd in ("fetch", "fetch2"):
        # FAKE_NOW 注入固定基准时间：跨两次采集时间戳/指纹稳定（dedup 断言前提）；
        # 缺省每次 now——偏移 30/45 分钟避开增量游标 1h 下限的边界抖动
        raw_now = os.environ.get("FAKE_NOW")
        now = datetime.fromisoformat(raw_now) if raw_now else datetime.now()
        # 条目形态对齐 _extract_items 启发式（title/dueDate 强信号键）——窗外条目
        # 供时间窗过滤断言
        if cmd == "fetch":
            items = [
                {"id": "f1", "title": "窗口内待办", "body": "fake cli item",
                 "dueDate": (now - timedelta(minutes=30)).isoformat()},
                {"id": "f2", "title": "窗口内日程", "body": "fake cli event",
                 "startDate": (now - timedelta(minutes=45)).isoformat()},
                {"id": "old", "title": "窗口外条目", "body": "out of window",
                 "dueDate": (now - timedelta(hours=200)).isoformat()},
            ]
        else:  # fetch2：第二采集域（一源多域 fetches 合并用例）
            items = [
                {"id": "g1", "title": "第二域条目", "body": "second domain",
                 "dueDate": (now - timedelta(minutes=20)).isoformat()},
            ]
        print(json.dumps({"items": items}, ensure_ascii=False))
    elif cmd == "slow":
        time.sleep(30)
        print("{}")
    elif cmd == "fail":
        print("boom: something went wrong", file=sys.stderr)
        sys.exit(1)
    elif cmd == "big":
        print('{"padding": "' + "x" * (3 * 1024 * 1024) + '"}')
    elif cmd == "gbk":
        sys.stdout.buffer.write('{"标题": "中文GBK内容"}'.encode("gbk"))
        sys.stdout.buffer.flush()
    else:
        print(json.dumps({"ok": True}))


if __name__ == "__main__":
    main()
