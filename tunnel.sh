#!/usr/bin/env bash

PORT=${1:-3000}

echo "正在把本机 http://127.0.0.1:$PORT 暴露到公网..."
echo "请保持此窗口不要关闭。"
echo

ssh -o StrictHostKeyChecking=no -p 443 -R0:localhost:$PORT a.pinggy.io
