#!/bin/bash
# 双击本文件即可启动「多视角人生参谋」
cd "$(dirname "$0")" || exit 1

if ! command -v python3 >/dev/null 2>&1; then
  echo "没有找到 python3。请先安装 Python 3（https://www.python.org/downloads/），或执行： brew install python3"
  read -r -p "按回车键关闭窗口..."
  exit 1
fi

echo "正在启动 多视角人生参谋 ..."
echo "（如果浏览器没有自动打开，请手动访问 http://127.0.0.1:8765/ ）"
python3 app.py

echo
echo "服务已退出。如果上面有报错信息，请把它截图发给 AI 助手。"
read -r -p "按回车键关闭窗口..."
