#!/usr/bin/env python3
"""Start the FastAPI backend and open the Vue dev server instructions."""
import subprocess
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from extractor.models import init_db

if __name__ == "__main__":
    # Ensure database exists
    init_db()
    print("[+] 数据库已初始化")
    print()
    print("启动后端 API 服务 (端口 8000)...")
    print("前端请在另一个终端运行: cd frontend && npm run dev")
    print()

    subprocess.run([
        sys.executable, "-m", "uvicorn",
        "backend.main:app",
        "--host", "127.0.0.1",
        "--port", "8000",
        "--reload",
        "--no-access-log",
        "--log-level", "info",
    ])
