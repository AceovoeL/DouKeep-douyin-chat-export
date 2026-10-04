"""一键更新：把代码换成最新 → 装 Python 依赖 → 构建前端。

由控制面板「关于 → 更新」在后台运行，输出被重定向到 config/logs/update.log，面板里直接
显示这份日志。也可以手动执行：

    python tools/update.py                # 安全模式：本地有真实改动时中止
    python tools/update.py --allow-dirty  # 强行更新，交给 git 判断能否快进
    python tools/update.py --archive      # 即使有 .git 也改用「下载代码包覆盖」

代码有两套换法，看目录自己选：

* **有 git 的目录**（自己 clone 下来的）：``git pull --ff-only``，能先检查有没有
  未提交的改动，也只会动有变化的部分。
* **没有 git 的目录**（从别的电脑整体拷贝过来的副本、ZIP 解压出来的目录，或者这台
  机器压根没装 git）：走 GitHub API 下载默认分支的代码包，解压后覆盖项目里的代码
  文件。这条路**不需要凭据** —— 本项目的仓库是公开的，匿名就能下。只有当项目被
  放回你自己的**私有仓库**时，才要先在面板「关于」页填那个只读 Token（且开发者模式
  开着，见 ``common/github_auth.py``）；真下不到时脚本会把该去哪儿填说清楚。下载过程会
  **边下边报进度**（已下载多少 / 共多少 / 百分之几），免得看着像卡死。

两套换法都只碰**仓库里的代码文件**：``data/``（聊天数据库、下载的媒体）、
``config/``（面板设置、登录态、日志）、``venv/``、``frontend/node_modules/`` 都不在
代码包里，不会被覆盖 —— 所以「只用来跑、不用来改」的第二台电脑可以不装 git，照样点
按钮更新。

每一步失败都会立刻停下并返回非零退出码（GitHub Actions 的 CI 是独立的一层，
这里只管把代码和构建产物换成最新）。

**重启由面板负责，不在这个脚本里**：面板跑完本脚本、确认成功之后，会交给
``tools/restart_server.py`` 把后端重新拉起来（见 control_panel.py 里的自动重启那一段）。
所以从面板点的更新是「点一下、等一会儿、页面自己刷新成新版本」；手动在命令行运行本
脚本时没人做这件事，跑完要自己重启一次服务。

面板「日志 → 重启服务」不换代码，只做「停服务 → 重建前端 → 起服务」，它复用这里的
``build_frontend()``（装前端依赖 + ``npm run build``）—— 所以那段逻辑别写成一次性的。
"""
import argparse
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_DIR = os.path.join(REPO_ROOT, "frontend")

# 直接跑 `python tools/update.py` 时 sys.path[0] 是 tools/，项目自己的模块要手动挂上。
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from common import github_auth, version  # noqa: E402  (必须在 sys.path 调整之后)


def step(message: str) -> None:
    print(f"\n=====> {message}", flush=True)


def run(args: list[str], *, cwd: str = REPO_ROOT, shell: bool = False,
        timeout: float | None = None) -> int:
    """跑一条命令并返回退出码；``timeout`` 秒还没结束就中止它（按失败算）。

    超时是给「重启服务」那条路准备的：那时旧服务已经停了，npm 要是卡住不返回，
    服务就永远起不来 —— 宁可算它失败、把服务先拉回来，也不能无限等下去。
    """
    printable = " ".join(args) if not shell else str(args)
    print(f"$ {printable}", flush=True)
    try:
        proc = subprocess.run(args, cwd=cwd, shell=shell, timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"[-] 这条命令超过 {timeout:.0f} 秒还没结束，已中止：{printable}", flush=True)
        return 1
    return proc.returncode


def git(*args: str) -> subprocess.CompletedProcess:
    """跑 git 并固定用 UTF-8 读输出（提交说明是中文，Windows 默认 GBK 会炸）。"""
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def fail(message: str, code: int = 1) -> int:
    print(f"[-] {message}", flush=True)
    return code


#: 工作区有未提交改动时的退出码：面板靠它把提示换成「是否仍然更新」
EXIT_DIRTY = 2


def npm_command() -> list[str]:
    """Windows 上 npm 是 npm.cmd，直接调用 .cmd 需要 shell。"""
    if os.name == "nt":
        return ["npm.cmd"]
    return ["npm"]


#: 国内直连 npm 官方源经常超时（装到一半断掉），默认用国内镜像。
NPM_REGISTRY = "https://registry.npmmirror.com"


def vite_available() -> bool:
    """前端依赖是否真的装好了（以 vite 可执行文件为准）。

    只判断 node_modules 目录存在是不够的：目录可能残缺（从别的电脑整体复制过来，
    或者上次 npm install 被网络中断），那时目录在、vite 却没有，构建会报
    「'vite' 不是内部或外部命令」。
    """
    bin_dir = os.path.join(FRONTEND_DIR, "node_modules", ".bin")
    names = ("vite.cmd",) if os.name == "nt" else ("vite",)
    return any(os.path.exists(os.path.join(bin_dir, name)) for name in names)


def changed_tracked_paths() -> list[str]:
    """相对 HEAD 有真实改动的已跟踪文件。

    Windows 上 core.autocrlf=true 时，LF 换行的文件即使内容没改也会出现在
    ``git status`` 里。``git diff --ignore-cr-at-eol`` 只看真实内容差异，能把
    这种纯换行噪音滤掉，避免把用户正常的 checkout 误判成「有未提交改动」。
    """
    paths: set[str] = set()
    for args in (
        ("diff", "--ignore-cr-at-eol", "--name-only"),
        ("diff", "--cached", "--name-only"),
    ):
        result = git(*args)
        if result.returncode != 0:
            print(f"[!] git {' '.join(args)} 失败: {(result.stderr or '').strip()}", flush=True)
            continue
        paths.update(line.strip() for line in result.stdout.splitlines() if line.strip())
    return sorted(paths)


def untracked_paths() -> list[str]:
    """新增但还没纳入 git 的文件（被 .gitignore 忽略的不算）。

    这些文件通常不会被 git pull 覆盖（除非远端新增同名文件），所以只提示不拦截。
    """
    result = git("ls-files", "--others", "--exclude-standard")
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


# ── 没有 git 时的第二套更新方式：下载代码包覆盖 ──────────────────────────────

#: GitHub API 的仓库接口前缀
GITHUB_API = "https://api.github.com"

#: 下载代码包的超时时间（秒）。代码包只有几 MB，是国内网络最容易卡的一步。
DOWNLOAD_TIMEOUT = 180

#: 下载时每次读多少字节
DOWNLOAD_CHUNK = 64 * 1024
#: 下载进度按百分比报，每隔这么多个百分点打一行（日志框只看最后几十行，不能每读一块
#: 就刷一行，否则真正有用的报错会被刷没）
DOWNLOAD_REPORT_STEP = 5
#: 服务端没给 Content-Length、总大小未知时，至少隔这么久报一行
DOWNLOAD_REPORT_SECONDS = 3.0


def format_size(num_bytes: float) -> str:
    """把字节数写成人看的大小（KB / MB）。"""
    if num_bytes >= 1024 * 1024:
        return f"{num_bytes / 1024 / 1024:.1f} MB"
    return f"{num_bytes / 1024:.0f} KB"


def response_length(response) -> int:
    """响应体一共多少字节；服务端没给 Content-Length（分块传输）时返回 0。"""
    try:
        value = int(response.headers.get("Content-Length") or 0)
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


def has_git_worktree() -> bool:
    """目录里有没有 git 记录。

    用 ``os.path.exists`` 而不是 ``isdir``：worktree / submodule 里 ``.git``
    是一个文件（内容是 ``gitdir: ...``）。
    """
    return os.path.exists(os.path.join(REPO_ROOT, ".git"))


def git_available() -> bool:
    """本机有没有 git 命令（有 .git 却没装 git 时只能走下载那条路）。"""
    return shutil.which("git") is not None


def archive_url(slug: str, branch: str) -> str:
    """分支代码包的下载地址（GitHub 的 zipball 接口）。"""
    return f"{GITHUB_API}/repos/{slug}/zipball/{branch}"


def download_archive(slug: str, branch: str, token: str) -> bytes:
    """下载分支代码包，返回 zip 的原始字节；``token`` 为空就是不认证地下载。

    走 API 的 zipball 接口而不是 ``github.com/<slug>/archive/refs/heads/<branch>.zip``：
    后者只认浏览器登录态的 cookie，Token 递过去也当没看见（私有仓库会直接 404）。
    zipball 会 302 到 codeload，urllib 跟随跳转时会保留 Authorization 头，
    私有仓库照样下得下来。

    **边下边报进度**：这一步在国内网络上可能耗掉半分钟以上，日志里一直没动静的话
    用户会以为卡死了。每读一块就更新「已下载多少 / 共多少（百分之几）」，
    但只按一定的间隔打行，免得把日志刷爆。
    """
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "douyin-chat-export-updater",
    }
    if token:                       # 公开仓库不带这个头也能下；私有仓库必须有
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(archive_url(slug, branch), headers=headers)
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response:
        total = response_length(response)
        hint = f"（共 {format_size(total)}）" if total else "（总大小未知）"
        print(f"[i] 正在下载代码包{hint}…", flush=True)
        parts: list[bytes] = []
        done = 0
        next_step = DOWNLOAD_REPORT_STEP
        last_report = started
        while True:
            chunk = response.read(DOWNLOAD_CHUNK)
            if not chunk:
                break
            parts.append(chunk)
            done += len(chunk)
            now = time.monotonic()
            if total:
                percent = done * 100 // total
                if percent >= next_step or done >= total:
                    print(f"    已下载 {percent}%  {format_size(done)} / {format_size(total)}", flush=True)
                    next_step = percent + DOWNLOAD_REPORT_STEP
                    last_report = now
            elif now - last_report >= DOWNLOAD_REPORT_SECONDS:
                print(f"    已下载 {format_size(done)}…", flush=True)
                last_report = now

        data = b"".join(parts)
    elapsed = max(0.001, time.monotonic() - started)
    speed = len(data) / elapsed / 1024
    print(
        f"[+] 下载完成：{format_size(len(data))}，用时 {elapsed:.0f} 秒（{speed:.0f} KB/s）",
        flush=True,
    )
    return data


def archive_prefix(infos: list[zipfile.ZipInfo]) -> str:
    """代码包最外层那层目录名（形如 ``AceovoeL-DouKeep-douyin-chat-export-1a2b3c4/``）。

    GitHub 的 zipball 总是把所有文件裹在一层以 commit 短 sha 结尾的目录里，
    解压时必须把它剥掉，否则代码会被铺到子目录里、白白多一层没人读的路径。
    认不出来（例如包里根目录直接就是文件）时返回空串，表示没有外层目录。
    """
    heads: set[str] = set()
    for info in infos:
        name = info.filename.lstrip("/")
        head, sep, _ = name.partition("/")
        if sep:
            heads.add(head + "/")
        elif name:
            heads.add("")                 # 根目录下直接有文件 → 没有外层目录
    if len(heads) == 1:
        return heads.pop()
    return ""


def restore_exec_bit(path: str, info: zipfile.ZipInfo) -> None:
    """还原 Unix 可执行位。

    zip 格式没有专门的权限字段，但 zipfile 会把 Unix mode 塞进 ``external_attr``
    的高 16 位。不还原的话（zipfile 自己解压也不管），``start.sh`` / ``stop.sh``
    下载下来就没有执行权限。
    """
    if os.name == "nt":
        return
    mode = (info.external_attr >> 16) & 0o777
    if mode & 0o111:
        os.chmod(path, 0o755)


def extract_archive(data: bytes, dest: str) -> int:
    """把代码包解压到 ``dest``，返回写出的文件数。

    先落到临时目录、再整体覆盖项目，是为了「下载/解压坏掉时项目里的文件一点没动」。
    包里的路径会做越界检查：``../`` 这种指到目标目录外面的路径直接报错。
    """
    root = os.path.abspath(dest)
    written = 0
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
        prefix = archive_prefix(infos)
        for info in infos:
            name = info.filename
            if name.endswith("/"):        # 目录条目，写文件时自然会建出来
                continue
            relative = name[len(prefix):] if prefix and name.startswith(prefix) else name
            if not relative:
                continue
            target = os.path.abspath(os.path.join(root, *relative.split("/")))
            if target != root and not target.startswith(root + os.sep):
                raise ValueError(f"代码包里有越界路径：{name}")
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with archive.open(info) as source, open(target, "wb") as handle:
                shutil.copyfileobj(source, handle)
            restore_exec_bit(target, info)
            written += 1
    return written


def merge_tree(source: str, target: str) -> int:
    """把 ``source`` 整棵树覆盖到 ``target`` 上（同名文件直接覆盖），返回写入数。

    只覆盖不删除：远端删掉的老文件会留在本地（没有任何东西引用它，不影响运行）。
    """
    written = 0
    for current, _dirs, files in os.walk(source):
        relative = os.path.relpath(current, source)
        directory = target if relative == "." else os.path.join(target, relative)
        os.makedirs(directory, exist_ok=True)
        for name in files:
            shutil.copy2(os.path.join(current, name), os.path.join(directory, name))
            written += 1
    return written


def version_on_disk() -> str:
    """读磁盘上的版本号（进程里 import 的那份在覆盖之后已经过时了）。"""
    path = os.path.join(REPO_ROOT, "common", "version.py")
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return version.VERSION
    found = re.search(r'^VERSION\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return found.group(1) if found else version.VERSION


def download_code_archive(slug: str, branch: str, token: str) -> bytes:
    """下载代码包：有凭据就用凭据，凭据被拒（401 / 403）就退回匿名再试一次。

    公开仓库匿名就能下，所以「Token 过期/被撤销」不该让更新彻底做不成 —— 先按不带
    凭据的方式再来一次。两次都不行就照实往上抛，由调用方给出「去哪儿填 Token」的提示
    （见 ``archive_auth_hint``）。
    """
    if not token:
        return download_archive(slug, branch, "")
    try:
        return download_archive(slug, branch, token)
    except urllib.error.HTTPError as exc:
        if exc.code not in (401, 403):
            raise
        print(f"[-] 带凭据的下载被拒（HTTP {exc.code}），改用不带凭据的方式再试一次", flush=True)
        return download_archive(slug, branch, "")


def archive_auth_hint() -> str:
    """下不到代码包时提示该去哪儿补什么。

    公开仓库匿名就能下载，所以走到这里基本就是「私有仓库 + 凭据没被用起来」。
    「存过 Token，但开发者模式关着所以没用」和「压根没填过」是两回事，提示要分开
    —— 不然用户会以为 Token 丢了，又去粘一遍。
    """
    if github_auth.load_for_use():
        return "凭据可能无效，或没有这个仓库的读取权限"
    if github_auth.load():
        return ("面板「关于」页的开发者模式关着，这条路不会使用已保存的凭据："
                "请打开「关于 → 开发者模式」后重试")
    return "仓库如果是私有的，请在面板「关于 → 私有仓库凭据」里填写只读 Token"


def update_code_via_archive() -> int:
    """没有 git（或指定了 --archive）时的更新：下载代码包 → 解压 → 覆盖。"""
    step("改用「下载代码包覆盖」的方式更新代码")
    if has_git_worktree():
        print("[i] 本机找不到 git 命令（或指定了 --archive），没法用 git pull", flush=True)
    else:
        print("[i] 这个目录里没有 .git（多半是整体拷贝过来的副本），没法用 git pull", flush=True)
    print("[i] 没有 git 就没法检查本地改动：项目里的代码文件会被换成最新版。"
          "data/、config/、venv/、frontend/node_modules/ 不在代码包里，不会被覆盖。", flush=True)

    token = github_auth.load_for_use()
    slug = version.REPOSITORY_SLUG
    branch = version.REPOSITORY_BRANCH
    # 不分「先检查有没有 Token」：公开仓库匿名就能下，拿不到代码包时再回头提示填凭据
    step(f"从 GitHub 下载 {slug} 的 {branch} 分支代码包"
         f"（{'带凭据' if token else '不带凭据'}）")
    try:
        data = download_code_archive(slug, branch, token)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403, 404):
            return fail(f"下载失败（HTTP {exc.code}）：{archive_auth_hint()}，或手动下载最新代码覆盖本目录")
        return fail(f"下载失败（HTTP {exc.code}），请稍后重试")
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return fail(f"下载失败：{exc} —— 检查网络后重试")
    # 下载进度和「下载完成」都由 download_archive 自己打（它才知道总大小和耗时）

    step("解压并覆盖项目里的代码文件")
    staging = tempfile.mkdtemp(prefix="douyin-update-")
    try:
        unpacked = extract_archive(data, staging)
        written = merge_tree(staging, REPO_ROOT)
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        return fail(f"解压或覆盖失败：{exc} —— 请重试，必要时手动下载最新代码")
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    print(f"[i] 代码包里 {unpacked} 个文件，已写入 {written} 个；"
          "远端删掉的老文件会留在本地（不影响运行）", flush=True)
    print(f"[+] 代码已更新到 v{version_on_disk()}", flush=True)
    return 0


def update_code_via_git(*, allow_dirty: bool) -> int:
    """有 git 时的更新：检查工作区 → ``git pull --ff-only``。"""
    # ── 1. 工作区检查 ──
    step("检查工作区是否有未提交改动")
    dirty = changed_tracked_paths()
    untracked = untracked_paths()
    if untracked:
        print(f"[i] 另有 {len(untracked)} 个未跟踪的新文件（不影响更新，会原样保留）：", flush=True)
        for path in untracked[:5]:
            print(f"  {path}", flush=True)
        if len(untracked) > 5:
            print(f"  ...（共 {len(untracked)} 个）", flush=True)
    if dirty:
        print("以下已跟踪文件有未提交的改动：", flush=True)
        for path in dirty[:20]:
            print(f"  {path}", flush=True)
        if len(dirty) > 20:
            print(f"  ...（共 {len(dirty)} 个）", flush=True)
        if not allow_dirty:
            return fail(
                "工作区有未提交的改动，已停止更新（不会覆盖你的改动）。"
                "确认可以更新时，在面板上点「仍然更新」，或手动执行 "
                "python tools/update.py --allow-dirty，也可以先 git stash",
                EXIT_DIRTY,
            )
        print("[!] --allow-dirty：继续更新，若本地改动与远端冲突，git 会中止", flush=True)
    else:
        print("[+] 工作区干净（纯换行差异不算改动）", flush=True)

    # ── 2. 拉取最新代码 ──
    step("拉取最新代码 git pull --ff-only")
    pull = subprocess.run(
        ["git", "pull", "--ff-only"],
        cwd=REPO_ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    for stream in (pull.stdout, pull.stderr):
        if stream and stream.strip():
            print(stream.rstrip(), flush=True)
    if pull.returncode != 0:
        return fail(
            "git pull 失败：本地与远端可能已经分叉，或本地改动会被覆盖，"
            "请手动处理（git status / git stash / git pull）；"
            "只想拿最新代码的话可以执行 python tools/update.py --archive 直接覆盖"
        )
    return 0


#: 构建前端最多等多久（秒）：npm install / vite build 卡在网络上时不能无限等下去。
#: 面板「日志 → 重启服务」是**先把服务停掉再构建**的，卡在这里等于服务一直不回来。
FRONTEND_BUILD_TIMEOUT = 900


def build_frontend() -> int:
    """装前端依赖（缺了才装）+ 构建前端产物；返回 0 表示成功。

    这段本来长在 ``install_and_build`` 里，「一键更新」和面板的「重启服务」都要它，
    所以单独拎出来共用：两边都必须是「依赖缺了先装、再构建」，只判断
    ``node_modules`` 目录在不在是不够的（目录可能残缺，见 ``vite_available``）。
    """
    if not vite_available():
        step("安装前端依赖 npm install")
        # 国内直连官方源常超时，先走镜像源，失败再回退官方源。
        if run(npm_command() + ["install", "--registry=" + NPM_REGISTRY],
               cwd=FRONTEND_DIR, shell=(os.name == "nt"),
               timeout=FRONTEND_BUILD_TIMEOUT) != 0:
            print("[-] 镜像源安装失败，换官方源再试一次", flush=True)
            if run(npm_command() + ["install"],
                   cwd=FRONTEND_DIR, shell=(os.name == "nt"),
                   timeout=FRONTEND_BUILD_TIMEOUT) != 0:
                return fail("npm install 失败，请检查网络与 Node.js 版本（需要 20.19+ 或 22.12+）")
        if not vite_available():
            return fail("依赖装完仍找不到 vite，请检查 frontend/package.json 的 devDependencies 是否包含 vite")

    step("构建前端 npm run build")
    if run(npm_command() + ["run", "build"], cwd=FRONTEND_DIR, shell=(os.name == "nt"),
           timeout=FRONTEND_BUILD_TIMEOUT) != 0:
        return fail("npm run build 失败，前端界面未更新")
    return 0


def install_and_build() -> int:
    """代码换好之后的公共步骤：装 Python 依赖 → 构建前端。"""
    # ── 3. Python 依赖（依赖没变化时这一步等于空跑） ──
    step("安装 Python 依赖")
    if run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"]) != 0:
        return fail("pip install 失败，请检查网络后重试")

    # ── 4. 前端依赖 + 构建 ──
    if build_frontend() != 0:
        return 1

    step("更新完成")
    print("[+] 代码与前端产物都已更新。面板发起的更新接下来会自动重启服务；"
          "手动运行时请自己重启一次后端服务，新前端才会生效", flush=True)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="拉取最新代码并重建前端")
    parser.add_argument("--allow-dirty", action="store_true",
                        help="工作区有本地改动时也继续，交给 git 判断能否快进")
    parser.add_argument("--archive", action="store_true",
                        help="即使目录里有 .git 也用「下载代码包覆盖」（本地与远端分叉时可用）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if has_git_worktree() and git_available() and not args.archive:
        code = update_code_via_git(allow_dirty=args.allow_dirty)
    else:
        code = update_code_via_archive()
    if code:
        return code
    return install_and_build()


if __name__ == "__main__":
    sys.exit(main())
