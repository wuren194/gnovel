"""Check the runtime before importing Govel's application modules."""

import platform
import sys


MIN_PYTHON = (3, 11)
SUPPORTED_SYSTEMS = ("Darwin", "Linux")


def require_supported_python() -> None:
    if sys.version_info >= MIN_PYTHON:
        return
    required = ".".join(str(part) for part in MIN_PYTHON)
    current = ".".join(str(part) for part in sys.version_info[:3])
    sys.stderr.write(
        f"Govel 需要 Python {required} 或更新版本；当前为 Python {current}"
        f"（{sys.executable}）。\n"
        "请改用受支持的 Python 解释器重新运行。macOS 可通过 brew install python 安装，"
        "然后使用 /opt/homebrew/bin/python3（Intel: /usr/local/bin/python3）；"
        "MCP 客户端请同步更新 command 为该解释器的绝对路径。\n"
    )
    raise SystemExit(2)


def require_supported_platform() -> None:
    system = platform.system()
    if system in SUPPORTED_SYSTEMS:
        return
    sys.stderr.write(
        f"Govel 当前支持 macOS 和 Linux；检测到 {system}，尚不支持原生运行。\n"
        "安全写入依赖 POSIX 文件锁、目录描述符和内核独占重命名。"
        "Windows 请按 docs/WINDOWS.md 使用 WSL2；在 WSL 内运行 "
        "python3 scripts/govel_bind.py wsl-config --json 生成 Windows 客户端配置。\n"
    )
    raise SystemExit(2)
