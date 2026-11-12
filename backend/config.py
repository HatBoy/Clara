"""应用配置。

核心职责：给出数据库文件的可写路径、解析服务监听端口。后端有两种运行形态，
路径规则不同：

- 开发形态（uvicorn 源码运行）：数据落 backend/data/ 下，随源码可见、便于调试
- 打包形态（PyInstaller onefile）：sys._MEIPASS 是临时解压目录、进程退出即焚，
  且工作目录不确定（取决于 Tauri 从哪里 spawn），必须落系统用户数据目录
"""

import os
import sys
from pathlib import Path

APP_NAME = "clara"

# 监听地址（固定本地回环；Rust 宿主按此探测/连接）
HOST = "127.0.0.1"


def _resolve_port(argv: list[str]) -> int:
    """解析命令行 "--port N"（打包形态由 Tauri 宿主 spawn 边车时传入，见
    src-tauri/src/lib.rs 的端口配置；端口真源在 Tauri 侧配置文件，不进 SQLite）。
    无参 / 坏值 / 越界一律回退 8000——dev 手动 uvicorn 不传参，即保持现状。"""
    for i, arg in enumerate(argv):
        if arg == "--port" and i + 1 < len(argv):
            try:
                p = int(argv[i + 1])
            except ValueError:
                break
            if 1 <= p <= 65535:
                return p
            break
    return 8000


PORT = _resolve_port(sys.argv)


def _is_frozen() -> bool:
    """是否运行在 PyInstaller 打包形态（打包后 sys.frozen 被置为 True）。"""
    return getattr(sys, "frozen", False)


# 公开形态常量（模块级求值一次）：服务层按此分流 dev 专属行为
# （如 seed 的测试建议卡重灌——测试数据不得反复灌给正式用户）
IS_PACKAGED = _is_frozen()


def get_data_dir() -> Path:
    """返回可写的数据目录（存放 SQLite 数据库文件），不存在则创建。"""
    if _is_frozen():
        if sys.platform == "win32":
            base = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
        else:
            base = Path.home() / ".local" / "share" / APP_NAME
    else:
        base = Path(__file__).resolve().parent / "data"
    base.mkdir(parents=True, exist_ok=True)
    return base


DATABASE_PATH = get_data_dir() / "clara.db"
DATABASE_URL = f"sqlite:///{DATABASE_PATH}"
