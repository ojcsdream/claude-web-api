# Claude Web 本地 AI 工作台

这是一个运行在安卓手机 Termux + proot-distro Ubuntu 环境中的本地 AI Web 应用。

项目提供一个 Claude Web 风格的移动端聊天界面，并支持：

- 第三方 OpenAI/Claude 兼容 API 直连流式模式
- SQLite 本地会话持久化
- 文本文件、代码文件、图片上传
- 历史文件上下文保留
- 网页 URL 内容读取
- Markdown 导出
- 会话搜索、固定、重命名、删除
- API 接入商管理
- 管理后台请求日志和系统状态页

默认项目路径：

```bash
/home/ai/claude-web
```

推荐在普通用户 `ai` 下运行。

## 页面入口

主页面：

```text
http://127.0.0.1:8000/
```

极简备用页面：

```text
http://127.0.0.1:8000/lite
```

管理后台：

```text
http://127.0.0.1:8000/admin
```

## 本地辅助工具

### 图片链接二进制下载识别器

当用户提供图片直链或 File Browser 分享链接时，先使用：

```bash
scripts/fetch_image_binary.py "<url>"
```

该工具会解析 `http://127.0.0.1:8080/share/<hash>` 这类分享页，下载真实图片二进制到 `/tmp/codex-images/`，并输出本地路径、MIME、宽高和文件大小。拿到输出里的 `path` 后，再用图片查看工具识别图片内容。

## 最新目录结构

```text
claude-web/
├── app.py              # FastAPI app 和路由编排
├── config.py           # 路径、默认模型、上下文限制等配置
├── schemas.py          # Pydantic 请求/响应数据结构
├── db.py               # SQLite 初始化和数据访问层
├── chat_utils.py       # token 估算、上下文裁剪、prompt 构造
├── services.py         # 直连 API、视觉 API、上传/网页读取服务
├── requirements.txt    # Python 运行依赖
├── install.sh          # Linux 一键安装脚本
├── start-local.sh      # 本地后台启动脚本
├── static/
│   ├── index.html      # 主聊天界面
│   ├── lite.html       # 极简备用聊天界面
│   └── admin.html      # 管理后台
├── uploads/            # 上传文件目录，已被 .gitignore 忽略
├── logs/               # 日志目录，已被 .gitignore 忽略
├── chat.db             # SQLite 数据库，已被 .gitignore 忽略
└── README.md
```

## Linux 一键部署

运行环境：

- Linux
- Python 3.10+
- `python3-venv`
- `pip`
- `curl`

从 GitHub 克隆后执行：

```bash
git clone <你的仓库地址> claude-web
cd claude-web
chmod +x install.sh start-local.sh
./install.sh
./start-local.sh
```

安装脚本会创建 `.venv`、安装 Python 依赖、创建 `uploads/` 和 `logs/`，并进行 Python 语法检查。它不会写入 API Key，也不会复制历史数据库。

首次启动时会自动创建全新的 `chat.db`。请在 Web 管理界面配置 API 接入商；不要把 API Key 写入公开仓库。

默认访问地址：

```text
http://127.0.0.1:8000/
```

## 启动方式

推荐使用项目内脚本：

```bash
cd /home/ai/claude-web
./start-local.sh
```

`start-local.sh` 会：

- 停掉旧的 `127.0.0.1:8000` uvicorn 进程
- 使用 `--workers 2` 启动服务
- 关闭 access log，减少手机环境下的日志压力
- 写入 `logs/uvicorn-local.log`
- 等待 `/api/health` 可用

手动启动：

```bash
cd /home/ai/claude-web
source .venv/bin/activate
python -m uvicorn app:app --host 127.0.0.1 --port 8000 --workers 2 --no-access-log
```

健康检查：

```bash
curl http://127.0.0.1:8000/api/health
```

## 公网访问

默认服务只监听 `127.0.0.1:8000`。如果需要公网访问，建议使用 Nginx、Caddy、SSH tunnel、Cloudflare Tunnel 或其它受控反向代理，并先处理认证、CORS 和 API Key 安全。

### 默认公网启动

当前推荐默认使用固定 ngrok 域名启动：

```bash
cd /home/ai/claude-web
./start-public.sh
```

固定公网入口：

```text
https://kindling-shaft-creamer.ngrok-free.dev
```

### Cloudflare Tunnel 公网入口

推荐使用 Cloudflare Tunnel 暴露本地服务。当前脚本默认使用 `cloudflared tunnel --url` 创建临时 HTTPS 地址，适合快速公网访问。

```bash
cd /home/ai/claude-web
chmod +x start-cloudflare.sh
./start-cloudflare.sh
```

该脚本会：

- 自动确保本地 `127.0.0.1:8000` 服务已启动
- 清理旧的同端口 `cloudflared tunnel` 进程
- 启动新的 Cloudflare Tunnel
- 把公网地址写入 `cloudflare-url.txt`
- 尝试做一次公网 `/api/health` 检查

日志文件：

```text
logs/cloudflared.log
logs/cloudflared.log.stdout
```

需要固定域名和更稳定的入口时，在 Cloudflare Zero Trust 里创建 Named Tunnel，把公网 Hostname 指向本地服务 `http://127.0.0.1:8000`，然后用 token 启动：

```bash
export CLOUDFLARE_TUNNEL_TOKEN="你的 tunnel token"
export CLOUDFLARE_PUBLIC_URL="https://你的域名"
./start-cloudflare.sh
```

`start-ngrok.sh` 仅建议用于临时调试，不建议默认公网暴露，也不要提交 `ngrok-url.txt` 或任何隧道 token。

公网 ngrok 隧道：

```bash
cd /home/ai/claude-web
./start-ngrok.sh
```

该脚本会：

- 自动确保本地 `127.0.0.1:8000` 服务已启动
- 清理旧的 `ngrok http` 进程
- 启动新的 HTTPS ngrok 隧道
- 把公网地址写入 `ngrok-url.txt`
- 尝试做一次公网 `/api/health` 检查

日志文件：

```text
logs/ngrok.log
```

## 直连聊天模式

项目当前只保留第三方 API 直连流式模式。

特点：

- 直接请求第三方 OpenAI/Claude 兼容 API
- 支持真流式输出
- 支持文本文件和代码文件分析
- 支持图片视觉输入
- 支持自动读取用户消息中的网页 URL
- 不内置本地代理，也不提供本地终端执行能力

## 网页解析

后端支持 API 优先的网页解析，避免直接抓网页时频繁遇到 403。

推荐配置：

```bash
export TAVILY_API_KEY="你的 Tavily API key"
```

可选配置：

```bash
export FIRECRAWL_API_KEY="你的 Firecrawl API key"
export JINA_API_KEY="你的 Jina Reader API key"
```

优先级：

- 网页解析：优先 Tavily Extract API，其次 Firecrawl Scrape API，其次 Jina Reader，最后才直接抓网页。

如果没有配置这些 key，功能仍可运行，但稳定性和抗 403 能力会明显下降。

## 主要功能

### 会话

- 会话列表
- 会话搜索
- 会话固定/取消固定
- 新建、重命名、删除
- Markdown 导出
- 消息删除及从某条回复重新生成

### 文件和图片

上传文本/代码文件时，后端会：

1. 保存原文件到 `uploads/`
2. 读取 UTF-8 文本内容
3. 将文件名、路径、内容写入 `messages.file_context`
4. 后续对话会通过 `chat_utils.format_message_for_context()` 自动带上历史文件上下文

上传图片时，后端会：

1. 保存图片到 `uploads/`
2. 在消息里保存图片预览路径
3. 视觉请求中读取图片并转为 base64
4. 调用 OpenAI/Anthropic 兼容视觉接口

### API 接入商

前端可以保存多个 API 接入商：

- 名称
- API URL
- API Key
- 模型 ID
- 默认接入商

数据库表：`api_profiles`

注意：API Key 保存在本地 SQLite 中，不要上传 `chat.db`。

### 管理后台

入口：

```text
http://127.0.0.1:8000/admin
```

当前管理后台提供：

- 请求统计
- 请求日志
- 错误筛选
- 接入商健康检查
- 系统信息
- 上传目录和数据库大小

默认后台密码目前在代码中配置为：

```text
114514
```

公网暴露前建议改为环境变量或更强认证。

## 数据库

SQLite 数据库：

```text
chat.db
```

核心表：

- `conversations`
- `messages`
- `api_profiles`
- `admin_request_logs`

重要字段：

- `conversations.is_pinned`：会话固定状态
- `messages.file_context`：历史上传文件内容
- `messages.image_preview`：图片预览路径
- `messages.model` / `provider_name` / `token_count`：回复元信息

## 安全注意事项

本项目是本地 AI 工作台，不是默认安全的公网服务。

高风险事项包括：

- API Key 保存在本地 SQLite
- 管理后台密码仍是固定值

如果要通过 Cloudflare Tunnel 或其它方式暴露公网，建议先做：

- 强认证
- 管理后台密码改环境变量
- 限制 CORS
- 定期备份并保护 `chat.db`

## 手机环境建议

在安卓手机 Termux/proot 环境中运行时：

- 关闭省电模式
- 将 Termux 和浏览器加入电池优化白名单
- 尽量保持屏幕唤醒
- 使用 `./start-local.sh` 后台启动
- 保留 `--workers 2`，避免长流式请求影响页面刷新

如果主页面出现性能问题，可以临时使用：

```text
http://127.0.0.1:8000/lite
```

## 开发和验证

修改后先编译：

```bash
.venv/bin/python -m py_compile app.py db.py config.py schemas.py chat_utils.py services.py
```

启动：

```bash
./start-local.sh
```

检查：

```bash
curl http://127.0.0.1:8000/api/health
curl http://127.0.0.1:8000/api/conversations
```

Git 提交前确认：

```bash
git status --short
git ls-files | grep -E '(^chat\.db$|admin-token\.txt|filebrowser\.db|ngrok-url\.txt|^uploads/|^logs/|^\.venv/|^\.env|\.key$|\.pem$|\.secret$|broken|backup|\.bak$|\.bad|\.before|^picture/)' || true
grep -RInE 'api[_-]?key|auth[_-]?token|secret|password|Bearer |sk-[A-Za-z0-9]' . --exclude-dir=.git --exclude-dir=.venv --exclude-dir=.claude --exclude=chat.db || true
```

第二条命令如果有输出，需要确认这些文件是否不应发布；第三条命令可能包含字段名或文档说明等误报，需要人工复核是否存在真实密钥。

不要提交：

- `chat.db`：包含 API 接入商 token、历史对话和管理日志
- `uploads/`：用户上传文件和图片
- `logs/`：运行日志
- `.venv/`：本地虚拟环境
- `.env` / `.env.*`：本地环境变量
- `admin-token.txt`
- `filebrowser.db`
- `ngrok-url.txt`
- `.claude/`
- `picture/`
- 备份文件、坏版本快照
- 私钥、token 或 API Key

## GitHub

当前远程仓库：

```text
git@github.com:ojcsdream/api-.git
```

常规提交：

```bash
git add app.py db.py config.py schemas.py chat_utils.py services.py static/index.html static/lite.html static/admin.html requirements.txt install.sh start-local.sh README.md .gitignore
git commit -m "Update project"
git push origin main
```
