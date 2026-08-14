"""启动脚本：双击「启动周报Agent.bat」或直接 python run.py。

启动后会：
  - 在 127.0.0.1:8765 启动 FastAPI 后端
  - 1.5 秒后自动用系统默认浏览器打开 UI
  - 关闭终端窗口即退出服务
"""
from __future__ import annotations

import os
import threading
import time
import webbrowser

import uvicorn

from app.config import settings


def _open_browser() -> None:
    time.sleep(2.0)  # 多等一点，避免浏览器打开过早
    url = f"http://{settings.host}:{settings.port}/"
    try:
        if os.name == "nt":
            os.startfile(url)  # type: ignore[attr-defined]
        else:
            webbrowser.open(url)
    except Exception:
        webbrowser.open(url)


def main() -> None:
    threading.Thread(target=_open_browser, daemon=True).start()
    print("=" * 60)
    print(" CICC 周报 Agent")
    print(f" 已在 http://{settings.host}:{settings.port}/ 启动")
    print(" 浏览器会自动打开；使用完毕后关闭此窗口即可退出")
    print("=" * 60)
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
