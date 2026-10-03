#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$DIR/.server.pid"

if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "服务已在运行 (PID $(cat "$PID_FILE"))，请先运行 stop.sh"
  exit 1
fi

# 国内直连 npm 官方源经常超时，默认用国内镜像。
NPM_REGISTRY="https://registry.npmmirror.com"

cd "$DIR/frontend"

# 只看 node_modules 目录是否存在不够：目录可能残缺（从别的电脑复制过来、或上次
# npm install 因网络中断没装完），那时目录在、vite 却不可用，构建会报「命令未找到」。
if [ -d node_modules ] && { [ -x node_modules/.bin/vite ] || [ -x node_modules/.bin/vite.cmd ]; }; then
  echo "已存在 node_modules 且 vite 可用，跳过 npm install"
else
  if [ -d node_modules ]; then
    echo "node_modules 存在但缺少 vite（上次安装不完整），重新安装补齐..."
  else
    echo "未检测到前端依赖，正在安装: npm install"
  fi
  if ! npm install --registry="$NPM_REGISTRY"; then
    echo "镜像源安装失败，换官方源再试一次..."
    npm install || { echo "错误: npm install 失败，请检查网络或 Node.js 版本"; exit 1; }
  fi
fi

# 构建前端（npm run build 失败时必须中止，否则会拿旧产物启动，问题被藏起来）
echo "构建前端..."
if ! npm run build; then
  echo "错误: 前端构建失败，请检查上面的错误输出"
  exit 1
fi

cd "$DIR"

# 监听地址由面板「设置 → 局域网访问」决定（配置在 data/panel_config.json）：
#   打开 = 监听 0.0.0.0（局域网里的设备也能连），关闭 = 只监听 127.0.0.1。
# 读不到配置或读失败时一律退回「只监听本机」这个安全默认值。
mkdir -p "$DIR/data"
LISTEN_HOST="127.0.0.1"
if [ -f "$DIR/data/panel_config.json" ] && grep -Eq '"lan_access"[[:space:]]*:[[:space:]]*"?true"?' "$DIR/data/panel_config.json"; then
  LISTEN_HOST="0.0.0.0"
fi

# 启动后端（同时 serve 前端 dist）
# 输出写进 data/server.log，和 Windows 那边（start.ps1 / 「启动服务（双击）.bat」）一致：
# 面板左侧「日志」页读的就是这份文件，服务没有窗口时也能回头看它打了什么。
PYTHONUTF8=1 PYTHONIOENCODING=utf-8 \
  nohup "$DIR/venv/bin/python3" -m uvicorn backend.main:app \
  --host "$LISTEN_HOST" --port 8000 \
  >> "$DIR/data/server.log" 2>&1 &

echo $! > "$PID_FILE"
sleep 1

if kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "✓ 服务已启动 (PID $(cat "$PID_FILE"))"
  echo "  浏览器访问: http://127.0.0.1:8000"
  if [ "$LISTEN_HOST" = "0.0.0.0" ]; then
    echo "  已向局域网开放（监听 0.0.0.0）：同一个局域网里的设备可以用这台电脑的 IP 加端口访问"
  fi
  echo "  服务输出: data/server.log（面板「日志」页也能直接看）"
  echo "  停止服务: 面板「日志」页 →「停止程序」，或运行 ./stop.sh"
  open "http://127.0.0.1:8000"
else
  echo "✗ 启动失败，查看日志: $DIR/data/server.log"
  rm -f "$PID_FILE"
  exit 1
fi
