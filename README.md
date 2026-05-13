# Claude Web 本地 AI 工作台

Claude Web 是一个偏移动端、偏本地部署的 AI 聊天工作台。项目使用 `FastAPI + SQLite + 单文件前端`，界面风格接近 Claude，但保留了更强的本地化能力：多接入商管理、图片与文件分析、联网搜索、系统提示词、重新回答版本管理、公网访问脚本等。

项目当前主要面向这几类使用场景：

- 在手机 Termux / Linux 小机 / 轻量云主机上自建个人 AI 工作台
- 通过第三方 OpenAI / Claude 兼容接口直连聊天
- 上传代码、文本、图片后做连续问答
- 让 AI 自主判断是否需要联网检索最新信息
- 在本地持久化保存对话、版本、来源、接入商配置

当前核心能力：

- OpenAI / Anthropic 兼容接口直连流式聊天
- Claude 风格主界面，支持流式输出、暂停回复、来源先行展示
- AI 自主联网搜索，支持 Tavily + SerpAPI 双引擎
- 用户可手动点按“强制联网搜索”按钮覆盖默认判断
- 文本文件、代码文件、图片上传与历史上下文保留
- 多系统提示词管理，可启用多个并写入数据库
- 会话搜索、固定、重命名、删除、Markdown 导出
- 重新回答并保留旧版本
- SQLite 本地持久化
- 管理后台、健康检查、公网启动脚本

默认项目路径：

```bash
/ai/claude-web
```

推荐使用普通用户运行，并通过项目脚本启动。

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

## 项目结构

```text
claude-web/
├── app.py              # FastAPI 路由、聊天入口、流式响应
├── services.py         # 直连 API、联网搜索、网页提取、视觉请求
├── db.py               # SQLite 初始化与数据访问
├── chat_utils.py       # 上下文裁剪、prompt 构造、token 粗估
├── schemas.py          # Pydantic 数据结构
├── config.py           # 默认模型、路径、限制配置
├── requirements.txt    # Python 运行依赖
├── install.sh          # 一键安装脚本
├── start-local.sh      # 启动本地服务，并默认尝试打开公网入口
├── start-public.sh     # 公网入口启动脚本
├── static/
│   ├── index.html      # 主聊天界面
│   ├── lite.html       # 极简备用界面
│   ├── admin.html      # 管理后台
│   └── sw.js           # PWA / 缓存 service worker
├── uploads/            # 上传文件目录，默认忽略
├── logs/               # 运行日志目录，默认忽略
├── chat.db             # SQLite 数据库，默认忽略
└── README.md
```

## 快速开始

### 1. 克隆与安装

运行环境：

- Linux
- Python 3.10+
- `python3-venv`
- `pip`
- `curl`
- `tar`

从 GitHub 克隆后执行：

```bash
git clone git@github.com:ojcsdream/api-.git claude-web
cd claude-web
chmod +x install.sh start-local.sh
./install.sh
```

安装脚本会：

- 创建 `.venv`
- 安装 Python 依赖
- 初始化 `uploads/`、`logs/`
- 做基础 Python 语法检查
- 尝试安装 `ngrok` 到 `~/.local/bin/ngrok`

它不会替你写入 API Key，也不会复制历史数据库。

### 2. 配置联网搜索与网页提取

当前项目联网搜索只保留两个 provider：

- `TAVILY_API_KEY`
- `SERPAPI_API_KEY`

推荐把它们放进项目根目录 `.env`：

```bash
TAVILY_API_KEY="你的 Tavily key"
SERPAPI_API_KEY="你的 SerpAPI key"
```

如果没有这些 key，普通对话依然能运行，但联网搜索和网页提取能力会明显下降。

### 3. 启动

```bash
cd /ai/claude-web
./start-local.sh
```

默认访问地址：

```text
http://127.0.0.1:8000/
https://kindling-shaft-creamer.ngrok-free.dev
```

如果要让固定公网域名自动可用，首次部署前先提供 ngrok token：

```bash
export NGROK_AUTHTOKEN="你的 ngrok token"
./install.sh
```

首次启动会自动创建新的 `chat.db`。API 接入商建议在 Web 界面中配置；不要把 `.env`、`chat.db`、`uploads/` 提交到公开仓库。

## 启动方式

推荐使用项目内脚本：

```bash
cd /ai/claude-web
./start-local.sh
```

`start-local.sh` 会：

- 停掉旧的 `127.0.0.1:8000` uvicorn 进程
- 使用 `--workers 1` 启动服务
- 关闭 access log，减少手机环境下的日志压力
- 写入 `logs/uvicorn-local.log`
- 等待 `/api/health` 可用
- 自动调用 `start-public.sh`，打开固定 ngrok 公网入口
- 把公网地址写入 `ngrok-url.txt`
- 如果公网隧道启动失败，本地服务仍会保持运行并输出提示

手动启动：

```bash
cd /ai/claude-web
source .venv/bin/activate
python -m uvicorn app:app --host 127.0.0.1 --port 8000 --workers 2 --no-access-log
```

健康检查：

```bash
curl http://127.0.0.1:8000/api/health
```

## 公网访问

项目启动脚本默认会启动本地服务并打开固定 ngrok 公网入口。如果需要换成其它公网方案，建议使用 Nginx、Caddy、SSH tunnel、Cloudflare Tunnel 或其它受控反向代理，并先处理认证、CORS 和 API Key 安全。

固定 ngrok 域名需要已配置 ngrok 账号 token。如果 `./start-local.sh` 提示公网隧道未启动，本地入口 `http://127.0.0.1:8000/` 仍然可用；配置 `NGROK_AUTHTOKEN` 后重新运行 `./install.sh` 和 `./start-local.sh` 即可。

### 默认公网启动

当前 `./start-local.sh` 已默认打开固定 ngrok 域名；也可以直接运行公网脚本：

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

`start-ngrok.sh` 是底层 ngrok 启动脚本；一般直接使用 `start-local.sh` 或 `start-public.sh`。

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

## 功能概览

### 1. 聊天体验

- OpenAI / Anthropic 兼容接口真流式输出
- 回复过程中支持暂停
- 重新回答时可以保留旧版本
- Markdown、代码块、高亮、公式渲染
- 来源会在正文前提前显示，更接近研究型聊天体验

### 2. AI 自主联网搜索

联网搜索不再完全依赖手动按钮。当前逻辑是：

- 默认由后端判断问题是否需要联网
- 如果是“最新、新闻、搜索、网页、核验事实”这类问题，会自动触发联网
- 用户也可以点输入框右侧的“强制联网搜索”按钮覆盖默认判断

当前搜索链路：

- 第一优先级：`Tavily`
- 第二优先级：`SerpAPI`

搜索结果会：

- 先于正文流式出现在界面上
- 写入 `messages.sources`
- 在回答中用来源卡片和引用编号展示

#### 联网搜索原理

这套搜索不是“后端搜完直接拼到 prompt 里”，而是一个标准的工具回合。整体分成四层：

1. 触发判断
2. 搜索词生成
3. 搜索执行与来源筛选
4. 工具结果回灌给模型，由模型自己组织最终回答

**1. 触发判断**

后端只在这些场景进入搜索回合：

- 用户手动开启联网搜索
- 用户消息里带 URL
- 后端判断这条消息确实属于需要外部实时信息的请求

普通聊天、代码解释、翻译、总结已有内容，默认不出网。

**2. 搜索词生成**

搜索词的目标不是“像人说话”，而是“像搜索引擎 query”。

当前做法有两条线：

- 先让模型判断是否需要搜索，并输出结构化 JSON
- 再由后端对 query 做清洗、补全和改写

后端会做这些处理：

- 去掉“查一下 / 搜一下 / 最新消息”这类空泛命令词
- 把“这个 / 它 / 上述 / 刚才”之类的指代补成上下文主体
- 对明显的实体问题做 query 模板化
- 对过短、过泛、没有有效实体的 query 直接判定为无效

例如：

- `北京现在几点` -> `current Beijing time`
- `Python 3.13 什么时候发布` -> `Python 3.13 release date official site:python.org`
- `Claude Code 最近更新` -> `Claude Code latest update Anthropic official`
- `OpenAI GPT-5.5 的最新消息` -> `OpenAI GPT-5.5 latest news official`

如果用户只说“帮我查一下”这种纯命令，但没有上下文主体，系统会宁可不搜，也不会乱猜。

**3. 搜索执行与来源筛选**

真正执行搜索时，后端会并行调用两个 provider：

- `Tavily`
- `SerpAPI`

然后再做这些处理：

- 合并结果
- 去重
- 按 query 命中、来源质量、标题相关度等因素打分
- 选择相对平衡的结果集
- 必要时再读取网页正文

如果搜索源没有返回结果，或者本环境没有配置对应 API Key，搜索链路会退化为“没有足够来源”，不会凭空编答案。

**4. 工具结果回灌给模型**

搜索完成后，后端不会直接把“搜索摘要”硬塞给用户。

它会把本轮工具调用记录整理成一段工具观测，让模型自己消费这些结果，再由模型写最终回答。

这样做的好处是：

- 模型能自己判断哪些来源值得引用
- 不会把一堆无关搜索结果硬拼进回答
- 更容易保留“来源先行”的研究型输出

**来源筛选规则**

来源筛选阶段会优先考虑：

- 官方文档
- 原始发布页
- 项目仓库
- 权威媒体

并降低这些来源的权重：

- 论坛搬运
- SEO 冗余页
- 明显二次转载
- 内容农场

最终展示给前端的来源，通常是经过 AI 筛选后保留下来的少量结果。

**前端展示**

前端会把搜索相关状态分开显示：

- `thinking`
- `planning_search`
- `searching`
- `reading_sources`

用户看到的是“正在规划搜索、正在检索、更可靠来源、正在读取来源”这一串状态，而不是一个静态的等待圈。

**失败模式**

如果你发现搜索不准，通常不是一个点坏了，而是下面几类问题之一：

- 用户问题本身太空
- 句子里没有明确主体
- query 被判定为无效并被压掉
- 搜索 provider 没有配置 key
- 外部结果确实为空

这时系统会优先选择“不瞎答”，而不是强行编造。

### 3. 文件与图片

- 支持上传文本、代码、日志、配置文件
- 支持上传图片并走视觉接口
- 历史文件和图片上下文会保留到后续对话
- 用户消息中的 URL 可自动读取网页正文

### 4. 系统提示词

- 支持多个系统提示词
- 支持启用 / 禁用
- 可同时启用多个并自动拼接
- 数据库存储，刷新后仍保留

### 5. 会话与版本

- 会话搜索
- 会话固定 / 取消固定
- 新建、重命名、删除
- Markdown 导出
- 消息重新回答并保留历史版本

### 6. 接入商管理

- 保存多个 API 接入商
- 设置默认接入商
- 拉取当前接入商模型列表
- 本地保存模型、URL、Key 等信息

### 7. 部署与公网

- 本地 `FastAPI` 服务
- 默认启动脚本可同时尝试拉起固定 ngrok 公网入口
- 支持 Cloudflare Tunnel
- 自带健康检查和日志文件

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

网页正文提取当前优先走 `Tavily Extract API`。如果命中了用户显式提供的 URL，系统会优先尝试提取正文，再把摘要结果拼进回答上下文。

如果没有配置 `TAVILY_API_KEY`，项目仍可工作，但网页提取稳定性会下降。

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
- `system_prompts`
- `admin_request_logs`

重要字段：

- `conversations.is_pinned`：会话固定状态
- `messages.file_context`：历史上传文件内容
- `messages.image_preview`：图片预览路径
- `messages.sources`：联网搜索来源 JSON
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
- 默认使用 `./start-local.sh`
- 保留单 worker，减少轻量设备上的内存压力

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
