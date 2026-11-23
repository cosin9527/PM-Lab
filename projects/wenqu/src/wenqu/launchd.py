"""launchd 安装与管理（macOS）。

- plist：~/Library/LaunchAgents/com.wenqu.sync.plist
- 登录自启 + KeepAlive（崩溃自动拉起）+ Nice（低进程优先级兜底）
- `wenqu sync start|stop|restart|status` 封装 launchctl bootstrap/bootout
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

LABEL = "com.wenqu.sync"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"

_PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{wenqu_bin}</string>
        <string>sync</string>
        <string>daemon</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>WENQU_HOME</key>
        <string>{wenqu_home}</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>Nice</key>
    <integer>10</integer>
    <key>StandardOutPath</key>
    <string>{log_out}</string>
    <key>StandardErrorPath</key>
    <string>{log_err}</string>
</dict>
</plist>
"""


def resolve_wenqu_bin() -> str:
    """定位 wenqu 可执行文件绝对路径（launchd 需要，GUI PATH 极简）。"""
    p = shutil.which("wenqu")
    if p:
        return str(Path(p).resolve())
    for cand in (Path.home() / ".local" / "bin" / "wenqu",):
        if cand.exists():
            return str(cand)
    raise FileNotFoundError(
        "未找到 wenqu 可执行文件——请先 `uv tool install --editable <源码目录>`"
    )


def install(wenqu_home_str: str | None = None) -> Path:
    """写入 plist 并 bootstrap。返回 plist 路径。"""
    wenqu_bin = resolve_wenqu_bin()
    home = wenqu_home_str or os.environ.get("WENQU_HOME") or str(Path.home() / "Knowledge")
    logs = Path(home) / ".wenqu" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.write_text(
        _PLIST_TEMPLATE.format(
            label=LABEL,
            wenqu_bin=wenqu_bin,
            wenqu_home=home,
            log_out=str(logs / "launchd.out.log"),
            log_err=str(logs / "launchd.err.log"),
        ),
        encoding="utf-8",
    )
    _launchctl("bootstrap", f"gui/{os.getuid()}", str(PLIST_PATH))
    return PLIST_PATH


def _service_loaded() -> bool:
    return _launchctl("print", f"gui/{os.getuid()}/{LABEL}").returncode == 0


def uninstall() -> None:
    _launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
    # bootout 异步生效：等待服务真正离开域，避免与随后同 label 的
    # bootstrap 竞争（曾导致新服务被延迟的 bootout 一并移出域）
    for _ in range(20):
        if not _service_loaded():
            break
        time.sleep(0.5)
    if PLIST_PATH.exists():
        PLIST_PATH.unlink()


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def status() -> dict:
    cp = _launchctl("print", f"gui/{os.getuid()}/{LABEL}")
    running = cp.returncode == 0
    return {
        "label": LABEL,
        "plist": str(PLIST_PATH),
        "plist_exists": PLIST_PATH.exists(),
        "loaded": running,
        "detail": (cp.stdout or cp.stderr or "").strip()[:300],
    }


def restart() -> dict:
    if _service_loaded():
        # 服务已在域：kickstart -k 原地重启（不摘服务，无 bootout/bootstrap 竞争）
        _launchctl("kickstart", "-k", f"gui/{os.getuid()}/{LABEL}")
        return status()
    if PLIST_PATH.exists():
        try:
            uninstall()
        except Exception:
            pass
    install()
    return status()
