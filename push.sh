#!/usr/bin/env bash
set -e

cd /home/ai/claude-web

MSG="${1:-update}"

echo "当前目录：$(pwd)"
echo "开始添加文件..."
git add .

echo "开始提交..."
git commit -m "$MSG" || echo "没有新的更改可提交"

echo "开始推送..."
git push

echo "推送完成。"
