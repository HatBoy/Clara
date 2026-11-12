import os
import sys
import json
import subprocess
import platform
import shutil
from pathlib import Path

def get_target_triple():
    system = platform.system().lower()
    machine = platform.machine().lower()
    
    if system == "windows":
        return "x86_64-pc-windows-msvc" if machine in ["amd64", "x86_64"] else "i686-pc-windows-msvc"
    elif system == "darwin": # macOS
        return "aarch64-apple-darwin" if machine == "arm64" else "x86_64-apple-darwin"
    elif system == "linux":
        return "x86_64-unknown-linux-gnu" if machine == "x86_64" else "aarch64-unknown-linux-gnu"
    else:
        raise Exception(f"Unsupported platform: {system} {machine}")


# —— litellm 打包裁剪（2026-11-11 用户拍板，方案探查全部实测验证）——
#
# 已验事实：
# - litellm 1.101 惰性加载：import litellm 实测急切拉入 ~950 个模块（llms 下
#   ~45 个顶层目录 + proxy 下 db/spend_tracking/common_utils 三子包 +
#   rust_bridge 的 .py 桥接层），这些是启动硬依赖（__init__.py 模块级
#   from .llms.X import XConfig 引用，含 ai21/watsonx 等小众）——exclude 即
#   启动崩溃，不裁；其余 llms 目录（~90 家小众适配器）与 proxy 子包（49/51，
#   含独占 20.9MB 的 _experimental）纯惰性——仅用户配置该前缀模型实际调用时
#   经 PEP 562 __getattr__ → importlib 动态 import，可直接 exclude 零风险
# - rust_bridge/_native.pyd 是 11.6MB Rust 扩展，loader.get_native_bridge()
#   对 ImportError 有 try/except → None 优雅回退（Clara 用自写 estimate_tokens，
#   不依赖 rust 计数）——只排除 _native 本体，.py 桥接层是急切须留
# - dashscope 实测不在急切集合，但它是 qwen 前缀的宿主目录（qwencloud.py/
#   qwen_ai_platform.py）——漏登保留清单即 qwen/ 前缀被裁，教训级条目
# - PyInstaller 6.22 CLI 无 --exclude-data-files（实测 --help 无此项），
#   proxy 残留数据文件仅 ~900KB，不值得为此迁 spec 流程——放弃数据剔除
# - 排除项全是惰性目录，PyInstaller 静态分析本就收不到它们，exclude 实际
#   只对冲 --collect-submodules=litellm 的全树枚举（6.22 exclude 优先级更高）
#
# 安全性方向论证：急切探测在 dev 环境（依赖齐全）执行，dev 急切集 ⊇ 冻结态
# 急切集——探测只用于决定「不裁谁」，偏差方向只会多保留，不会误裁。

_LITELLM_KEEP_LAZY_PROVIDERS = frozenset({
    # 惰性但保留的供应商适配器（急切集合天然在包，不在此列）：
    # 国内：moonshot（Kimi）/ zai（智谱 GLM）/ minimax / volcengine（火山豆包）
    #       / dashscope（qwen 前缀宿主，实测惰性，2026-11-11 教训补登）
    # 聚合商：openrouter / modelscope（魔搭）
    # 本地部署工具栈（用户要求常用工具全覆盖）：lm_studio / xinference /
    #       llamafile / docker_model_runner / infinity（ollama、vllm 在急切集）
    "moonshot", "zai", "minimax", "volcengine", "dashscope",
    "openrouter", "modelscope",
    "lm_studio", "xinference", "llamafile", "docker_model_runner", "infinity",
    # litellm_proxy：非供应商——get_llm_provider 对每个请求最先经
    # litellm.LiteLLMProxyChatConfig 判断「是否默认走 LiteLLM Proxy 网关」
    # （公共路由层动态引用，2026-11-11 首次打包冒烟实证：误裁致全部
    # completion 被 GetLLMProvider Exception 包装成 400）
    "litellm_proxy",
})


def detect_litellm_import_profile() -> tuple[set[str], Path]:
    """子进程干净 import litellm → (急切模块集, litellm 包目录)。

    急/惰集合随 litellm 版本浮动（__init__ 模块级 import 清单与 PEP 562 登记
    表每版都变），写死目录清单必漂移——build 时实测探测是唯一自适配口径。
    子进程单行 JSON 回传（litellm.__file__ + 全部 litellm.* 模块名），构建
    进程自身不 import litellm，包目录与急切集单一真源：
    - -I 隔离：去 cwd 进 sys.path（backend/ 的 config.py/models.py 会遮蔽
      三方顶层 import）
    - -X utf8：防 import 期非 ASCII 输出在 Windows 控制台编码崩
    - 必传 LITELLM_LOCAL_MODEL_COST_MAP=True：否则顶层先试拉 GitHub 价格表
      （国内网络三次重试白等 ~30 秒）
    - check=True：探测失败 fail-fast，不带病静默回退全量打包
    - 取 stdout 末行解析：litellm import 期可能有噪声输出，只认 JSON 行
    """
    code = ("import json,sys;import litellm;"
            "print(json.dumps([litellm.__file__]+[m for m in sys.modules"
            " if m=='litellm' or m.startswith('litellm.')]))")
    proc = subprocess.run(
        [sys.executable, "-I", "-X", "utf8", "-c", code],
        capture_output=True, text=True, check=True,
        env={**os.environ, "LITELLM_LOCAL_MODEL_COST_MAP": "True"})
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    return set(payload[1:]), Path(payload[0]).parent


# proxy 侧惰性但保留的子包（非急切但被核心层运行时引用）：
# - openai_files_endpoints：litellm_logging / prompt_templates 等核心层
#   函数内按需 import 其 common_utils（completion 链路可触达，2026-11-11
#   首次打包冒烟实证）
_LITELLM_KEEP_LAZY_PROXY = frozenset({
    "openai_files_endpoints",
})


def build_litellm_excludes(eager_modules: set[str], pkg_dir: Path) -> list[str]:
    """急切集 + 保留清单 → --exclude-module 参数列表。三段：

    ① llms 一级子项（文件系统枚举目录 + 顶层 .py）− 急切 − 保留清单。
       陷阱（实测）：不能用 pkgutil.iter_modules 枚举——provider 目录多为无
       __init__.py 的 namespace 目录，iter_modules 漏报（只报 48/135）；必须
       文件系统扫描（急切集来自 sys.modules 实测，namespace 目录照样入
       sys.modules）。急切基建文件（base.py/custom_llm.py/__init__.py）由
       急切判定天然保留；惰性单文件供应商（maritalk.py）一并裁
    ② proxy 一级子项同口径 − _LITELLM_KEEP_LAZY_PROXY（急切实测仅
       db/common_utils/spend_tracking 三子包与 _types.py/types_utils.py/
       proxy_cli.py 三文件；惰性大文件 proxy_server.py 800KB / utils.py
       345KB 等一并裁）
    ③ litellm.rust_bridge._native 固定项（11.6MB，见文件头注释）

    「惰性 ≠ 无人引用」教训（2026-11-11 首次打包冒烟）：核心层会在运行时
    按需 import 惰性目录（get_llm_provider 每请求先查 litellm.llms.
    litellm_proxy 的 LiteLLMProxyChatConfig；completion 链路触达 litellm.
    proxy.openai_files_endpoints）——纯惰性目录也可能被公共路径引用，
    新增裁剪后必须跑「frozen 模拟」+ exe 冒烟两道验证（见 docs/
    model-providers.md 打包裁剪节）
    """
    def lazy_subpackages(sub: str, keep: frozenset[str]) -> list[str]:
        out = []
        for child in sorted((pkg_dir / sub).iterdir()):
            if child.is_dir():
                if child.name == "__pycache__":
                    continue
                name = child.name
            elif child.suffix == ".py" and child.name != "__init__.py":
                name = child.stem
            else:
                continue
            prefix = f"litellm.{sub}.{name}"
            is_eager = any(m == prefix or m.startswith(prefix + ".")
                           for m in eager_modules)
            if not is_eager and name not in keep:
                out.append(prefix)
        return out

    return (lazy_subpackages("llms", _LITELLM_KEEP_LAZY_PROVIDERS)
            + lazy_subpackages("proxy", _LITELLM_KEEP_LAZY_PROXY)
            + ["litellm.rust_bridge._native"])


def build_and_move():
    print("🚀 开始使用 PyInstaller 编译 Python 后端...")
    
    # 动态获取绝对路径，避免由于执行目录不同导致路径错乱
    backend_dir = Path(__file__).parent.resolve()
    project_root = backend_dir.parent
    main_py = backend_dir / "main.py"
    
    # 1. 运行 PyInstaller，并指定工作目录 cwd
    #    经 sys.executable -m 调用而非裸 pyinstaller 命令——绑定当前 Python 环境
    #    （conda 未 activate 时 PATH 里的 pyinstaller 可能来自别的环境，打进残缺依赖）
    #
    #    依赖减肥与数据文件修复（2026-11-05 实测结论，152MB → ~127MB）：
    #    - exclude sympy：onnxruntime 声明的依赖（源码态 73MB），magika 判文件
    #      类型运行时不用（onnxruntime 官方 hook 只收动态库，不强制 sympy）
    #    - exclude boto3/botocore/s3transfer：litellm 的 AWS Bedrock 适配拖进来的
    #      （源码态 27MB+），不用 AWS；将来要接 Bedrock 时删掉这三行即可
    #    - litellm 根级 *.json 必须 add-data：PyInstaller 不自动收包内数据文件，
    #      litellm/__init__ 顶层读 model_prices_and_context_window_backup.json，
    #      缺了启动即崩——2026-10-11 litellm 引入起就存在的存量打包 bug（此前
    #      打包版从未冒烟过 AI import 链），2026-11-05 减肥冒烟首次暴露
    #    - lark_oapi 无法精收：lark_oapi/__init__ 顶层 `from .api import *` 且
    #      api/__init__ 顶层逐域 import——逐域 exclude 会在运行时 ImportError，
    #      全 api 树（99MB 源码态）只能整收（代码只用 im 域 + ws）
    #    - litellm.llms 厂商适配器：2026-11-05 曾有意全保留（模型选择空间），
    #      2026-11-11 用户拍板裁剪——只留国内外常见供应商/聚合商/本地部署
    #      工具栈 + openai/ 前缀 Base URL 通用通道，惰性小众目录动态排除
    #      （探测与计算见下方 _LITELLM_KEEP_LAZY_PROVIDERS 及两个函数）
    #
    #    --collect-submodules=composio/composio_client/mcp/langchain_mcp_adapters/
    #    markitdown：均为运行时动态 import 子模块（延迟 import + 动态发现），
    #    PyInstaller 静态分析收不全会打包残缺；--collect-data=magika 收 ONNX
    #    识别模型数据文件（markitdown 依赖 magika 判文件类型，模型缺失即崩）
    #    langgraph/langchain_openai（阶段二 AI）：graph 编译与 chat model 装载
    #    同为运行时动态发现，静态分析收不全；langchain_litellm（ChatLiteLLM 引擎，
    #    services/llm.py 的 init_llm）与 langchain_core 同理（消息 coercion 惰性加载）
    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--onefile",
        "--name", "clara-backend",
        "--collect-submodules=composio",
        "--collect-submodules=composio_client",
        "--collect-submodules=mcp",
        "--collect-submodules=langchain_mcp_adapters",
        "--collect-submodules=markitdown",
        "--collect-submodules=langgraph",
        "--collect-submodules=langchain_openai",
        "--collect-submodules=langchain_litellm",
        # litellm 三件套（import 链散布动态引用与数据文件读取，漏一样启动即崩——
        # 2026-11-05 逐层冒烟剥出：根级 model_prices json → litellm_core_utils.
        # tokenizers 子模块 → tiktoken 编码注册表 → containers/endpoints.json）：
        # ① 全树子模块 + 单包补扫 litellm_core_utils（collect 对 litellm 全树扫描
        #    有疏漏，实测 687 个子模块仍漏 core_utils 深层，单包扫 65 个全）
        # ② collect-data=litellm 收包内全部数据文件（各层 json / tiktoken 编码
        #    blob / endpoints.json 一次收全，不再逐个 add-data 追）
        # ③ tiktoken_ext 命名空间插件（openai_public 编码注册表）动态发现须全收
        "--collect-submodules=litellm",
        "--collect-submodules=litellm.litellm_core_utils",
        "--collect-data=litellm",
        "--collect-all=tiktoken_ext",
        "--collect-submodules=lark_oapi",
        "--collect-submodules=dingtalk_stream",
        "--collect-submodules=aibot",
        "--collect-submodules=langchain_core",
        # ics 导入（2026-11-11）：icalendar 的时区后端（icalendar.timezone）运行时
        # 动态选择，静态分析可能收残 → 全收；tzdata 是数据-only 包（Windows 无系统
        # IANA 库，冻结态 zoneinfo 的唯一数据源，约 200KB），icalendar 内部惰性
        # import 可能漏收 → 显式收最稳
        "--collect-all=icalendar",
        "--collect-all=tzdata",
        "--exclude-module=sympy",
        "--exclude-module=boto3",
        "--exclude-module=botocore",
        "--exclude-module=s3transfer",
        "--collect-data=magika",
        str(main_py)
    ]
    # PyInstaller 输出（workpath/distpath）挪到 backend/ 之外：uvicorn --reload
    # 递归监视 backend/ 整树，打包产物几千个文件的写入会触发 reload 风暴直接
    # 打死开发后端（2026-11-05 实证：连续打包期间 dev 后端进程死亡，前端一切
    # 写操作失效）——产物隔离到项目根 .pybuild/ 后打包与 dev 服务彻底解耦，
    # uvicorn 启动命令零改动
    pybuild_dir = project_root / ".pybuild"
    args += [
        "--workpath", str(pybuild_dir / "work"),
        "--distpath", str(pybuild_dir / "dist"),
    ]
    # litellm 裁剪（2026-11-11）：动态探测急切集合 → 只排除「确定惰性且不在
    # 保留清单」的目录（llms 小众适配器 + proxy 惰性子包 + rust_bridge 本体），
    # 机制与教训见文件头注释块
    eager_modules, litellm_pkg_dir = detect_litellm_import_profile()
    litellm_excludes = build_litellm_excludes(eager_modules, litellm_pkg_dir)
    print(f"✂️ litellm 裁剪：急切 {len(eager_modules)} 模块，"
          f"exclude {len(litellm_excludes)} 项")
    args += [f"--exclude-module={m}" for m in litellm_excludes]
    # LITELLM_LOCAL_MODEL_COST_MAP=True：collect_submodules 枚举子模块须 import
    # litellm，其顶层会先试拉 GitHub 最新模型价格表（国内网络不通，三次重试
    # 白等 ~30 秒后回落本地 backup）——此开关强制直接走本地，打包提速且日志干净
    # （仅影响打包进程；exe 运行时实测启动 2-3s 无此拖累，无需处理；探测子进程
    # 同款环境变量在 detect_litellm_import_profile 内自带）
    env = {**os.environ, "LITELLM_LOCAL_MODEL_COST_MAP": "True"}
    subprocess.run(args, check=True, cwd=str(backend_dir), env=env)

    # 2. 确定源文件与目标目录
    target_triple = get_target_triple()
    exe_extension = ".exe" if platform.system().lower() == "windows" else ""

    source_bin = pybuild_dir / f"dist/clara-backend{exe_extension}"
    target_dir = project_root / "src-tauri/bin"
    target_dir.mkdir(parents=True, exist_ok=True)
    
    # 3. 按照 Tauri 规范重命名并移动
    target_bin = target_dir / f"clara-backend-{target_triple}{exe_extension}"
    
    print(f"📦 移动二进制文件至: {target_bin}")
    shutil.copy(source_bin, target_bin)
    
    print("✅ Python 后端边车打包完成！")

if __name__ == "__main__":
    build_and_move()