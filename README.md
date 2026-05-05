# Claude Web 本地 AI 工作台

这是一个运行在安卓手机 Termux + proot-distro Ubuntu 环境中的本地 AI Web 应用。

项目提供一个 Claude Web 风格的移动端聊天界面，并同时支持：

- Claude Code CLI 本地代理线路
- 第三方 OpenAI/Claude 兼容 API 直连流式线路
- SQLite 本地会话持久化
- 文本文件、代码文件、图片上传
- 历史文件上下文保留
- 网页 URL 内容读取
- Markdown 导出
- 会话搜索、固定、重命名、删除
- API 接入商管理
- Agent 执行能力
- 管理后台请求日志和系统状态页

默认项目路径：

```bash
/home/ai/claude-web
```

推荐在普通用户 `ai` 下运行，不要使用 root 运行 Claude Code。

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

## 最新目录结构

```text
claude-web/
├── app.py              # FastAPI app 和路由编排
├── config.py           # 路径、默认模型、上下文限制等配置
├── schemas.py          # Pydantic 请求/响应数据结构
├── db.py               # SQLite 初始化和数据访问层
├── chat_utils.py       # token 估算、上下文裁剪、prompt 构造
├── services.py         # Claude CLI、直连 API、视觉 API、上传/网页读取服务
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

## 两条聊天线路

### 直连流式线路

前端选择 `direct` 时使用。

特点：

- 直接请求第三方 OpenAI/Claude 兼容 API
- 支持真流式输出
- 支持文本文件和代码文件分析
- 支持图片视觉输入
- 支持自动读取用户消息中的网页 URL
- 不具备本地文件系统控制能力

### Claude Code 本地代理线路

前端选择 `cc` 时使用。

特点：

- 通过本机 `claude` CLI 调用 Claude Code
- 可以读取和修改本地项目文件
- 可以执行本地命令
- 适合项目维护、代码修改、本机诊断

注意：

```text
不要用 root 跑 Claude Code
不要在 root 下使用 --dangerously-skip-permissions
```

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

高风险能力包括：

- `/api/agent/run` 可以让模型规划并执行命令
- Claude Code 线路可访问本地文件系统
- API Key 保存在本地 SQLite
- 管理后台密码仍是固定值

如果要通过 Cloudflare Tunnel 或其它方式暴露公网，建议先做：

- 强认证
- 管理后台密码改环境变量
- 禁用或保护终端/Agent/debug 接口
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
```

不要提交：

- `chat.db`
- `uploads/`
- `logs/`
- `.venv/`
- `.claude/`
- 备份文件
- 私钥或 API Key

## GitHub

当前远程仓库：

```text
git@github.com:ojcsdream/api-.git
```

常规提交：

```bash
git add app.py db.py config.py schemas.py chat_utils.py services.py static/index.html static/lite.html static/admin.html start-local.sh README.md
git commit -m "Update project"
git push origin main
```
