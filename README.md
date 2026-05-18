# Claude Web API 多用户版

这是 Claude Web 的多用户服务端项目，适合部署成多人共用的 AI 聊天工作台。它基于单用户版扩展了账号体系、会话归属、用户配置隔离和密码管理能力。

项目使用 `FastAPI + SQLite + 原生前端`。默认数据库是 `chat_multi.db`，运行时由服务自动创建。

## 主要功能

- 多用户登录、注册、退出
- 邮箱验证码注册、重置密码、修改密码
- 用户资料修改
- 会话、消息、系统提示词、API 接入商按用户隔离
- OpenAI / Anthropic / 兼容接口直连
- 支持 Chat Completions、Anthropic Messages、OpenAI Responses 协议
- 流式输出、停止生成、重新回答、保留旧版本
- 图片上传、文本/代码文件上传、Responses 文件输入
- 自主联网搜索，支持 Tavily 和 SerpAPI
- GitHub 链接在 Responses 协议下优先走后端 GitHub MCP 源码读取器，再把源码上下文传给模型
- 管理后台、健康检查、部署脚本、启动脚本

## 服务端结构

```text
app.py                       FastAPI 路由、认证、聊天、上传、再生成接口
db.py                        SQLite 表结构、用户、会话、消息和配置访问
services.py                  模型请求、Responses 请求、搜索、网页提取、视觉输入
schemas.py                   Pydantic 请求结构
chat_utils.py                上下文拼接、token 粗估、图片历史处理
static/                      前端页面和静态资源
tests/test_user_settings_api.py
tests/test_responses_payload.py
start-multi.sh               多用户服务启动脚本，默认端口 8002
deploy-server.sh             服务器一键部署脚本
```

## 快速部署

### 从 GitHub 拉取多用户分支

```bash
git clone -b multi-user https://github.com/ojcsdream/claude-web-api.git claude-web-multi
cd claude-web-multi
chmod +x deploy-server.sh
./deploy-server.sh
```

部署完成后访问：

```text
http://服务器IP:8002/
```

如果服务器有防火墙，请放行端口 `8002`。

## 环境变量

可以在项目根目录创建 `.env.multi`：

```bash
HOST="0.0.0.0"
PORT="8002"
CLAUDE_WEB_DB_PATH="./chat_multi.db"

TAVILY_API_KEY="你的 Tavily key"
SERPAPI_API_KEY="你的 SerpAPI key"
GITHUB_TOKEN="可选：用于读取私有 GitHub 仓库"

SMTP_HOST="smtp.example.com"
SMTP_PORT="587"
SMTP_USER="你的邮箱账号"
SMTP_PASSWORD="你的邮箱授权码"
SMTP_FROM="你的发件邮箱"
SMTP_SSL="0"
```

说明：

- 如果不配置 SMTP，注册/重置密码邮件会不可用。
- 如果要让 GitHub MCP 读取私有仓库，必须配置有仓库读取权限的 `GITHUB_TOKEN` 或 `GH_TOKEN`。
- Gmail、QQ 邮箱等通常需要使用“应用专用密码/授权码”，不要使用网页登录密码。
- `.env.multi`、`chat_multi.db`、`uploads/`、`logs/` 默认不应提交到 Git。

## 一键部署脚本

`deploy-server.sh` 会执行：

1. 检查并安装 Python 运行环境
2. 创建或修复 `.venv`
3. 安装 `requirements.txt`
4. 编译检查核心 Python 文件
5. 启动多用户服务
6. 调用 `/api/health` 做健康检查

常用参数：

```bash
PORT=8012 ./deploy-server.sh
CLAUDE_WEB_DB_PATH=/data/claude-web/chat_multi.db ./deploy-server.sh
```

手动启动：

```bash
./install.sh
./start-multi.sh
```

健康检查：

```bash
curl http://127.0.0.1:8002/api/health
```

## 生产建议

- 用 Nginx / Caddy 反向代理到 `127.0.0.1:8002`
- 通过 HTTPS 暴露服务
- 配置 SMTP 后再开放注册功能
- 不要把 `.env.multi`、数据库、上传文件、token 提交到仓库
- 定期备份 `chat_multi.db`
- 如果公网开放，建议增加反向代理层限流和访问日志

## GitHub 分支说明

同一个仓库内有两个独立分支：

- `single-user`：单用户版
- `multi-user`：多用户版，本项目
