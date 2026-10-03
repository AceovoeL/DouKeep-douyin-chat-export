"""Git 凭据助手：把面板里保存的只读 Token 交给 git（私有仓库的 fetch 要用）。

只由 backend/control_panel.py 通过 git 的 credential.helper 调用::

    git -c credential.helper='!<python> <本脚本路径>' fetch origin main

Token 通过环境变量 ``DSH_GIT_TOKEN`` 传入，**不会出现在命令行里**（命令行可能被
同机其它进程看到）。git 需要凭据时把 ``key=value`` 写在 stdin 上，本脚本按
``username=`` / ``password=`` 输出。
"""
import os
import sys

KEY = "DSH_GIT_TOKEN"


def main() -> int:
    token = os.environ.get(KEY) or ""
    if not token:
        return 1                     # 没有 Token：让 git 继续找下一个助手
    sys.stdout.write("username=x-access-token\n")
    sys.stdout.write(f"password={token}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
