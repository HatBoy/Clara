"""config._resolve_port 纯函数直测（argv 解析，不启动服务、不碰库）。"""

from config import _resolve_port


def test_no_args_defaults_8000():
    # dev 手动 uvicorn 不传 --port → 固定 8000（现状口径）
    assert _resolve_port([]) == 8000
    assert _resolve_port(["main.py"]) == 8000


def test_port_flag_parses():
    # 打包形态：Tauri 宿主 spawn 边车时传 --port N
    assert _resolve_port(["clara-backend.exe", "--port", "8086"]) == 8086
    assert _resolve_port(["main.py", "--port", "9000"]) == 9000


def test_port_bad_value_falls_back():
    assert _resolve_port(["main.py", "--port", "abc"]) == 8000


def test_port_out_of_range_falls_back():
    assert _resolve_port(["main.py", "--port", "0"]) == 8000
    assert _resolve_port(["main.py", "--port", "65536"]) == 8000


def test_port_missing_tail_falls_back():
    # "--port" 是最后一个参数（缺尾值）→ 回退
    assert _resolve_port(["main.py", "--port"]) == 8000
