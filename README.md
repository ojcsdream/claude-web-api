# Claude Web API 单用户版

这是 Claude Web 的单用户服务端项目，适合个人部署使用。无需注册登录，开箱即用。

项目使用 `FastAPI + SQLite + 原生前端`。默认数据库是 `chat.db`，运行时由服务自动创建。

## 主要功能

- 免登录直接使用
- 支持 OpenAI / Anthropic / 兼容接口直连
- 支持 Chat Completions、Anthropic Messages、OpenAI Responses 协议
- 流式输出、停止生成、重新回答、保留旧版本
- 图片上传、文本/代码文件上传、Responses 文件输入
- 自主联网搜索，支持 Tavily 和 SerpAPI
- GitHub 链接在 Responses 协议下优先走后端 GitHub MCP 源码读取器，再把源码上下文传给模型
- 管理后台、健康检查、部署脚本、启动脚本

## 服务端结构

```text
app.py                       FastAPI 路由、聊天、上传、再生成接口
db.py                        SQLite 表结构、会话、消息和配置访问
services.py                  模型请求、Responses 请求、搜索、网页提取、视觉输入
schemas.py                   Pydantic 请求结构
chat_utils.py                上下文拼接、token 粗估、图片历史处理
static/                      前端页面和静态资源
start-local.sh               本地启动脚本，默认端口 8000
start-public.sh              带公网隧道（ngrok）启动脚本
deploy-server.sh             服务器一键部署脚本
scripts/                     运行时脚本工具集
```

## 快速部署

### 从 GitHub 拉取单用户分支

```bash
git clone -b single-user https://github.com/ojcsdream/claude-web-api.git claude-web
cd claude-web
chmod +x deploy-server.sh
./deploy-server.sh
```

部署完成后访问：

```text
http://服务器IP:8000/
```

如果服务器有防火墙，请放行端口 `8000`。

## 环境变量

可以在项目根目录创建 `.env`：

```bash
HOST="0.0.0.0"
PORT="8000"

# 单用户配置（可选）
CLAUDE_WEB_SINGLE_USERNAME="local"         # 界面显示的用户名
CLAUDE_WEB_SINGLE_EMAIL="user@example.com"  # 用户邮箱（可选）
CLAUDE_WEB_SINGLE_USER_ID=""                # 用户 ID（可选）

TAVILY_API_KEY="你的 Tavily key"
SERPAPI_API_KEY="你的 SerpAPI key"
GITHUB_TOKEN="可选：用于读取私有 GitHub 仓库"
```

说明：

- 单用户模式无需注册登录，配置后直接用。
- 如果不配置 `TAVILY_API_KEY` 和 `SERPAPI_API_KEY`，联网搜索不可用。
- 如果要让 GitHub MCP 读取私有仓库，必须配置有仓库读取权限的 `GITHUB_TOKEN` 或 `GH_TOKEN`。
- 数据库文件 (`chat.db` / `chat_multi.db`)、`uploads/`、`logs/` 默认不应提交到 Git。

## 启动方式

```bash
# 本地运行（端口 8000）
./start-local.sh

# 手动启动
uvicorn app:app --host 0.0.0.0 --port 8000

# 健康检查
curl http://127.0.0.1:8000/api/health
```

## 生产建议

- 用 Nginx / Caddy 反向代理到 `127.0.0.1:8000`
- 通过 HTTPS 暴露服务
- 不要把 `.env`、数据库、上传文件、token 提交到仓库
- 定期备份 `chat.db`
- 如果公网开放，建议增加反向代理层限流和访问日志

## GitHub 分支说明

同一个仓库内有两个独立分支：

- `single-user`：单用户版，本项目
- `multi-user`：多用户版（带登录注册、用户隔离、SMTP 邮箱验证）
