```markdown
# Claude Web 本地 AI 项目交接文档

## 一、项目概述

本项目是一个运行在 **安卓手机 Termux + proot-distro Ubuntu** 环境中的本地 AI Web 应用。

项目本质是一个：

> **Claude Web 风格前端 + FastAPI 后端 + SQLite 数据库 + Claude Code CLI 本地代理 + 第三方 API 直连流式 + Cloudflare Tunnel 公网访问** 的混合系统。

用户可以在手机浏览器中打开本地网页进行聊天，并根据需要选择不同线路：

1. **CC 本地代理线路**
   - 通过 Claude Code CLI；
   - 可以访问本地 Ubuntu 文件系统；
   - 可以执行命令；
   - 可以修改项目代码；
   - 适合项目维护、代码开发、本地命令操作。

2. **专用直连流式线路**
   - 不经过 Claude Code；
   - 直接调用第三方 OpenAI/Claude 兼容 API；
   - 响应更快；
   - 更接近真实流式输出；
   - 适合普通聊天、文件分析、图片分析、网页链接读取。

项目运行位置通常是：

```bash
/home/ai/claude-web
```

项目必须在普通用户：

```bash
ai
```

下运行，不建议使用 root。

---

## 二、运行环境

### 2.1 基础环境

设备环境：

```text
安卓手机
Termux
proot-distro Ubuntu
普通用户 ai
```

进入 Ubuntu：

```bash
proot-distro login ubuntu
```

切换到普通用户：

```bash
su - ai
```

确认身份：

```bash
whoami
```

应显示：

```bash
ai
```

---

### 2.2 项目目录

项目主目录：

```bash
/home/ai/claude-web
```

进入项目：

```bash
cd /home/ai/claude-web
```

---

### 2.3 Python 虚拟环境

项目使用 Python 虚拟环境：

```bash
/home/ai/claude-web/.venv
```

手动启动时需要激活：

```bash
cd /home/ai/claude-web
source .venv/bin/activate
```

---

### 2.4 Claude Code CLI 限制

Claude Code CLI 已经在 `ai` 用户下登录过。

重要限制：

```text
不要用 root 运行 Claude Code
不要在 root 下使用 --dangerously-skip-permissions
后端调用 Claude Code 时必须保持 ai 用户权限
```

---

## 三、项目目录结构

当前项目大致结构如下：

```text
/home/ai/claude-web/
├── app.py
├── db.py
├── chat.db
├── requirements.txt
├── push.sh
├── cloudflare-url.txt
├── static/
│   └── index.html
├── uploads/
├── logs/
├── scripts/
│   └── cloudflare-tunnel.sh      # 如果已复制到项目内
├── .venv/
├── .gitignore
└── 若干备份文件或历史文件
```

另外，项目外部可能还有两个常用管理脚本：

```text
/home/ai/claude-web-bg.sh
/home/ai/claude-web-cloudflare.sh
```

其中：

- `claude-web-bg.sh` 用于后台启动网页服务；
- `claude-web-cloudflare.sh` 用于启动 Cloudflare Tunnel。

---

## 四、核心文件说明

### 4.1 `app.py`

路径：

```bash
/home/ai/claude-web/app.py
```

这是后端主文件。

技术栈：

```text
FastAPI
StreamingResponse
SQLite
subprocess
urllib
文件上传处理
第三方 API 流式请求
Claude Code CLI 调用
```

主要职责：

1. 启动 FastAPI 应用；
2. 挂载前端静态目录；
3. 挂载上传目录；
4. 提供聊天接口；
5. 提供会话管理接口；
6. 提供消息读取和保存接口；
7. 提供 API 接入商配置接口；
8. 提供文件上传接口；
9. 提供图片处理接口；
10. 提供直连 API 流式输出；
11. 提供 Claude Code 本地代理线路；
12. 提供网页 URL 内容抓取；
13. 提供 Markdown 导出 / 分享接口；
14. 提供终端执行接口；
15. 提供 Agent 任务接口。

---

### 4.2 `db.py`

路径：

```bash
/home/ai/claude-web/db.py
```

这是数据库初始化和 SQLite 辅助文件。

主要职责：

1. 初始化 SQLite 数据库；
2. 创建会话表；
3. 创建消息表；
4. 创建 API 接入商配置表；
5. 提供数据库连接函数。

数据库文件：

```bash
/home/ai/claude-web/chat.db
```

---

### 4.3 `static/index.html`

路径：

```bash
/home/ai/claude-web/static/index.html
```

这是前端主页面，包含完整 HTML、CSS、JavaScript。

主要职责：

1. 聊天 UI；
2. 左侧会话列表；
3. 会话创建、切换、重命名、删除、置顶；
4. 消息渲染；
5. Markdown 渲染；
6. 代码高亮；
7. MathJax 数学公式渲染；
8. 文件上传；
9. 图片上传；
10. 模型选择；
11. API 接入商配置；
12. 线路切换；
13. 流式输出显示；
14. 自动滚动逻辑；
15. 分享 / 导出按钮；
16. 重新生成回复；
17. 删除消息；
18. Agent 或终端入口，如果当前版本启用了相关 UI。

---

### 4.4 `chat.db`

路径：

```bash
/home/ai/claude-web/chat.db
```

SQLite 数据库文件。

存储内容包括：

- 会话；
- 消息；
- 接入商配置；
- 模型名称；
- provider name；
- token 估算；
- 图片预览路径；
- 附件名称；
- 会话置顶状态。

注意：

```text
chat.db 可能包含隐私数据、聊天记录、API 配置，不建议上传 GitHub。
```

---

### 4.5 `uploads/`

路径：

```bash
/home/ai/claude-web/uploads/
```

用于保存用户上传的文件和图片。

注意：

```text
uploads/ 可能包含个人文件、图片或敏感内容，不建议上传 GitHub。
```

---

### 4.6 `logs/`

路径：

```bash
/home/ai/claude-web/logs/
```

用于保存运行日志。

常见文件可能包括：

```text
cloudflared.log
uvicorn 日志
后台脚本日志
```

注意：

```text
logs/ 不建议上传 GitHub。
```

---

### 4.7 `requirements.txt`

路径：

```bash
/home/ai/claude-web/requirements.txt
```

Python 依赖列表。

换手机或重建环境时可使用：

```bash
cd /home/ai/claude-web
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

### 4.8 `push.sh`

路径：

```bash
/home/ai/claude-web/push.sh
```

用于 GitHub 推送的便捷脚本。

如果当前存在并已赋权，可以这样使用：

```bash
cd /home/ai/claude-web
./push.sh "update project"
```

如果没有执行权限：

```bash
chmod +x push.sh
```

---

### 4.9 `.gitignore`

路径：

```bash
/home/ai/claude-web/.gitignore
```

建议包含：

```gitignore
__pycache__/
*.pyc

.venv/

logs/
*.log

chat.db
uploads/

*.bak
*.bak_*
app.py.bak*
static/index.html.bak*

.env
*.key
```

目的：

- 避免上传虚拟环境；
- 避免上传聊天数据库；
- 避免上传上传文件；
- 避免上传日志；
- 避免上传备份文件；
- 避免泄露密钥。

---

### 4.10 `/home/ai/claude-web-bg.sh`

路径：

```bash
/home/ai/claude-web-bg.sh
```

这是网页服务后台管理脚本。

常用命令：

```bash
/home/ai/claude-web-bg.sh start
/home/ai/claude-web-bg.sh stop
/home/ai/claude-web-bg.sh restart
/home/ai/claude-web-bg.sh status
/home/ai/claude-web-bg.sh logs
```

主要作用：

- 使用 tmux 或后台方式启动 FastAPI；
- 自动进入项目目录；
- 自动激活虚拟环境；
- 启动 uvicorn；
- 方便查看日志和状态。

---

### 4.11 `/home/ai/claude-web-cloudflare.sh`

路径：

```bash
/home/ai/claude-web-cloudflare.sh
```

这是 Cloudflare Tunnel 管理脚本。

常用命令：

```bash
/home/ai/claude-web-cloudflare.sh start
/home/ai/claude-web-cloudflare.sh stop
/home/ai/claude-web-cloudflare.sh restart
/home/ai/claude-web-cloudflare.sh status
/home/ai/claude-web-cloudflare.sh logs
/home/ai/claude-web-cloudflare.sh url
```

主要作用：

1. 检查 / 安装 `cloudflared`；
2. 后台启动临时 Cloudflare Tunnel；
3. 将公网地址映射到本地服务：

```text
http://127.0.0.1:8000
```

4. 自动提取：

```text
https://xxxx.trycloudflare.com
```

5. 保存到：

```bash
/home/ai/claude-web/cloudflare-url.txt
```

---

## 五、数据库结构

数据库文件：

```bash
chat.db
```

由 `db.py` 初始化。

---

### 5.1 `conversations` 表

用于保存会话。

字段大致包括：

```text
id
title
created_at
updated_at
is_pinned
```

作用：

- 保存会话 ID；
- 保存标题；
- 保存创建时间；
- 保存更新时间；
- 保存是否置顶。

---

### 5.2 `messages` 表

用于保存聊天消息。

字段大致包括：

```text
id
conversation_id
role
content
file_name
image_preview
created_at
model
provider_name
token_count
```

作用：

- 保存用户消息；
- 保存助手回复；
- 保存附件名称；
- 保存图片预览路径；
- 保存模型信息；
- 保存接入商信息；
- 保存 token 估算。

---

### 5.3 `api_profiles` 表

用于保存第三方 API 接入商配置。

字段大致包括：

```text
id
name
base_url
auth_token
model
is_default
created_at
updated_at
```

作用：

- 保存接入商名称；
- 保存 API Base URL；
- 保存 API Key；
- 保存模型名称；
- 保存默认接入商。

注意：

```text
api_profiles 里可能包含 API Key，因此 chat.db 不建议上传 GitHub。
```

---

## 六、整体架构

项目整体结构如下：

```text
手机浏览器
   |
   | 访问 http://127.0.0.1:8000
   |
FastAPI 后端 app.py
   |
   ├── 返回 static/index.html 前端页面
   |
   ├── SQLite chat.db
   |      ├── conversations
   |      ├── messages
   |      └── api_profiles
   |
   ├── CC 本地代理线路
   |      └── subprocess 调用 Claude Code CLI
   |
   ├── Direct 直连流式线路
   |      └── 请求第三方 OpenAI/Claude 兼容 API
   |
   ├── uploads/
   |      └── 文件 / 图片上传
   |
   └── export.md
          └── 对话 Markdown 导出
```

如果启动 Cloudflare Tunnel：

```text
公网用户 / 其他设备浏览器
   |
   | https://xxxx.trycloudflare.com
   |
Cloudflare Tunnel
   |
   | 转发到
   |
http://127.0.0.1:8000
   |
FastAPI app.py
```

---

## 七、两条聊天线路

### 7.1 CC 本地代理线路

前端显示通常类似：

```text
CC 本地代理
```

后端通过 Claude Code CLI 调用本地 Claude Code。

适合任务：

- 修改项目代码；
- 读取本地文件；
- 执行命令；
- 操作 Git；
- 查看日志；
- 启动或重启服务；
- 调试 Python / HTML / JS；
- 项目维护。

特点：

```text
功能强
能操作本地环境
速度可能比直连慢
依赖 Claude Code 登录状态
必须在 ai 用户下运行
```

---

### 7.2 Direct 专用直连流式线路

前端显示通常类似：

```text
专用直连流式
```

后端直接请求第三方 API。

适合任务：

- 普通聊天；
- 快速问答；
- 长文本生成；
- 文件分析；
- 图片视觉分析；
- 网页链接读取；
- 快速流式输出。

特点：

```text
速度快
流式体验更好
不控制本地 Ubuntu
依赖第三方 API Key 和 Base URL
不同服务商兼容性不同
```

---

## 八、直连 API 兼容性说明

后端直连函数主要在 `app.py` 中：

```python
stream_direct_api_text(...)
```

当前逻辑大致是：

- 如果模型名像 `gpt`，走 OpenAI-compatible：

```text
/v1/chat/completions
```

- 如果是 Claude/Anthropic 兼容模型，走：

```text
/v1/messages
```

项目后来做过优化：

1. 自动兼容 Base URL 是否带 `/v1`；
2. 增加浏览器风格 `User-Agent`；
3. 增加 `Accept` header；
4. 错误时输出请求地址和 HTTP 状态；
5. 防止 `/v1/v1/chat/completions` 这种拼接错误。

如果某个服务商返回：

```text
error
code: 1010
```

一般是服务商 Cloudflare 风控拦截，或者 API 地址不兼容，不一定是本项目问题。

---

## 九、前端流式渲染优化

前端 `static/index.html` 曾经存在一个问题：

```text
流式输出时，页面会强制滚到底部，用户无法自由向上翻看历史。
```

原始逻辑类似：

```js
requestAnimationFrame(() => {
  const last = chatList.lastElementChild;
  if (last) last.scrollIntoView({ behavior: "smooth", block: "end" });
});
```

后来做过几轮优化。

---

### 9.1 智能自动滚动

目标：

```text
用户不动：自动跟随到底部
用户手动滑动：停止自动跟随
用户重新回到底部附近：恢复自动跟随
用户发送新消息：强制回到底部
```

核心变量可能包括：

```js
let autoScrollEnabled = true;
const AUTO_SCROLL_THRESHOLD = 140;
let userScrollActive = false;
let userScrollTimer = null;
```

核心函数可能包括：

```js
isNearBottom()
scrollToBottomIfNeeded()
restoreScrollPosition()
pauseAutoScrollByUser()
maybeResumeAutoScroll()
```

---

### 9.2 流式增量渲染优化

之前流式每收到一个 chunk 都调用：

```js
renderMessages();
```

而 `renderMessages()` 会清空整个聊天列表：

```js
chatList.innerHTML = "";
```

这会导致手机端：

- 卡顿；
- 发热；
- 页面重绘；
- 滚动位置被影响；
- Markdown 重复渲染；
- MathJax 重复处理。

后来建议优化为：

```js
scheduleStreamingAssistantUpdate(full);
```

核心思路：

```text
流式过程中只更新最后一个 assistant bubble
不再重绘整个聊天列表
用 requestAnimationFrame 合并高频更新
最终结束后再做代码高亮和 MathJax
```

核心函数可能包括：

```js
getLastAssistantBubble()
renderAssistantContentIntoBubble()
followBottomDuringStreaming()
updateLastAssistantBubble()
scheduleStreamingAssistantUpdate()
flushStreamingAssistantUpdate()
```

---

## 十、分享 / 导出功能

前端顶部有：

```text
Share
```

按钮。

作用：

```text
导出当前对话为 Markdown 文件
```

前端调用地址：

```text
/api/conversations/{conversation_id}/export.md
```

后端对应接口：

```python
@app.get("/api/conversations/{conversation_id}/export.md")
def export_conversation_markdown(conversation_id: str):
```

曾经出现过两个问题。

---

### 10.1 Internal Server Error

原因是导出接口没有充分容错。

后来改为：

- try/except 包裹；
- 找不到对话时使用默认标题；
- 字段为空时安全处理；
- 返回失败文本，而不是直接白屏。

---

### 10.2 中文文件名导致 latin-1 错误

报错类似：

```text
'latin-1' codec can't encode character ...
ordinal not in range(256)
```

原因：

HTTP Header 中：

```python
Content-Disposition
```

不能直接放中文文件名。

修复方式：

- `filename` 使用 ASCII 安全名；
- `filename*` 使用 UTF-8 URL 编码中文名。

逻辑类似：

```python
from urllib.parse import quote

ascii_name = ...
utf8_name = quote(f"{title}.md")

headers={
    "Content-Disposition": f"attachment; filename=\"{ascii_name}.md\"; filename*=UTF-8''{utf8_name}"
}
```

---

## 十一、Cloudflare Tunnel

项目支持通过 Cloudflare Tunnel 暴露公网访问链接。

脚本路径：

```bash
/home/ai/claude-web-cloudflare.sh
```

启动：

```bash
/home/ai/claude-web-cloudflare.sh start
```

重启：

```bash
/home/ai/claude-web-cloudflare.sh restart
```

查看链接：

```bash
/home/ai/claude-web-cloudflare.sh url
```

查看日志：

```bash
/home/ai/claude-web-cloudflare.sh logs
```

停止：

```bash
/home/ai/claude-web-cloudflare.sh stop
```

链接保存位置：

```bash
/home/ai/claude-web/cloudflare-url.txt
```

完整启动顺序：

```bash
proot-distro login ubuntu
su - ai
/home/ai/claude-web-bg.sh restart
/home/ai/claude-web-cloudflare.sh restart
/home/ai/claude-web-cloudflare.sh url
```

---

## 十二、启动方式

### 12.1 后台启动网页服务

推荐：

```bash
proot-distro login ubuntu
su - ai
/home/ai/claude-web-bg.sh restart
```

查看状态：

```bash
/home/ai/claude-web-bg.sh status
```

查看日志：

```bash
/home/ai/claude-web-bg.sh logs
```

浏览器访问：

```text
http://127.0.0.1:8000
```

---

### 12.2 手动启动网页服务

```bash
proot-distro login ubuntu
su - ai
cd /home/ai/claude-web
source .venv/bin/activate
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

这个方式会占用当前终端。

---

### 12.3 启动公网访问

```bash
/home/ai/claude-web-cloudflare.sh restart
/home/ai/claude-web-cloudflare.sh url
```

输出类似：

```text
https://xxxx.trycloudflare.com
```

---

## 十三、GitHub 管理

远程仓库使用 SSH。

查看远程地址：

```bash
cd /home/ai/claude-web
git remote -v
```

应类似：

```text
origin git@github.com:ojcsdream/api-.git
```

常用提交方式：

```bash
cd /home/ai/claude-web

git status

git add app.py db.py static/index.html requirements.txt push.sh scripts/ .gitignore

git commit -m "update project"

git push origin main
```

注意不要直接：

```bash
git add .
```

除非确认 `.gitignore` 已经完全正确。

---

### 13.1 Git 常见问题

#### 问题一：`Everything up-to-date` 但 GitHub 没更新

原因通常是：

```text
文件改了，但没有 git add / git commit
```

检查：

```bash
git status
```

如果看到：

```text
Changes not staged for commit
```

说明还没 add。

如果看到：

```text
Changes to be committed
```

说明 add 了但没 commit。

如果看到：

```text
Your branch is ahead of origin/main
```

说明 commit 了但没 push。

---

#### 问题二：commit 失败，提示 Author identity unknown

执行：

```bash
git config --global user.name "ojcsdream"
git config --global user.email "ojcsdream@users.noreply.github.com"
```

然后重新 commit。

---

#### 问题三：确认是否真的推上去

```bash
git log --oneline -1
git ls-remote origin main
```

如果本地最新 commit hash 和远端 hash 开头一致，说明已成功推送。

---

## 十四、开发维护原则

这个项目运行在手机上，资源有限，维护时建议遵守以下原则。

---

### 14.1 修改前备份

例如修改前端：

```bash
cp static/index.html static/index.html.bak_$(date +%Y%m%d_%H%M%S)
```

修改后端：

```bash
cp app.py app.py.bak_$(date +%Y%m%d_%H%M%S)
```

---

### 14.2 后端修改后检查语法

```bash
cd /home/ai/claude-web
python3 -m py_compile app.py
```

通过后再重启：

```bash
/home/ai/claude-web-bg.sh restart
```

---

### 14.3 前端修改后刷新浏览器

前端是单文件：

```bash
static/index.html
```

修改后通常只需要刷新浏览器即可。

如果服务缓存或静态文件异常，可以重启后端：

```bash
/home/ai/claude-web-bg.sh restart
```

---

### 14.4 不要上传敏感文件

不建议上传：

```text
chat.db
uploads/
logs/
.venv/
.env
*.key
__pycache__/
*.pyc
*.bak
```

---

## 十五、常用命令速查

### 15.1 进入系统

```bash
proot-distro login ubuntu
su - ai
```

---

### 15.2 启动网页

```bash
/home/ai/claude-web-bg.sh restart
```

---

### 15.3 查看网页服务状态

```bash
/home/ai/claude-web-bg.sh status
```

---

### 15.4 查看网页日志

```bash
/home/ai/claude-web-bg.sh logs
```

---

### 15.5 启动 Cloudflare

```bash
/home/ai/claude-web-cloudflare.sh restart
```

---

### 15.6 查看 Cloudflare 链接

```bash
/home/ai/claude-web-cloudflare.sh url
```

---

### 15.7 停止 Cloudflare

```bash
/home/ai/claude-web-cloudflare.sh stop
```

---

### 15.8 手动启动 FastAPI

```bash
cd /home/ai/claude-web
source .venv/bin/activate
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

---

### 15.9 Git 推送

```bash
cd /home/ai/claude-web
git status
git add app.py db.py static/index.html requirements.txt push.sh scripts/ .gitignore
git commit -m "update project"
git push origin main
```

---

## 十六、当前已知注意事项

### 16.1 Direct API 的 1010 错误

如果出现：

```text
[直连OpenAI流式接口失败]
error
code: 1010
```

通常是第三方 API 服务商风控，不一定是本项目问题。

建议：

- 换接入商；
- 检查 Base URL；
- 确认是否重复 `/v1/v1`；
- 用 curl 单独测试；
- 优先使用已验证可用的接入商，例如之前截图中 `linkw` 可以正常回复。

---

### 16.2 Cloudflare Tunnel 不是 API 转发原因

Cloudflare Tunnel 只是把浏览器访问转发到本地：

```text
公网浏览器 -> trycloudflare.com -> 127.0.0.1:8000
```

第三方 API 请求仍然是手机 Ubuntu 后端主动发出去的。

所以如果第三方 API 报错，通常与 Tunnel 无关。

---

### 16.3 自动滚动问题

前端已多次优化，但如果未来仍出现：

```text
用户上滑时被流式输出拖到底部
```

优先检查：

```js
renderMessages()
scheduleStreamingAssistantUpdate()
scrollToBottomIfNeeded()
pauseAutoScrollByUser()
```

核心原则：

```text
流式输出期间不要全量重绘整个聊天列表
只更新最后一个 assistant bubble
用户交互后暂停自动跟随
用户回到底部附近后恢复跟随
```

---

## 十七、推荐后续优化方向

后续可以继续做：

1. 完善前端流式增量渲染；
2. 把前端从单文件拆成 CSS / JS 模块；
3. 给 API 接入商增加测试按钮；
4. 给直连模式增加更完善的错误分类；
5. 优化 Cloudflare Tunnel 链接展示；
6. 增加一键启动全部服务脚本；
7. 增加数据库备份脚本；
8. 增加导出全部会话功能；
9. 增加搜索聊天记录；
10. 增加移动端 UI 优化；
11. 增加系统诊断页面；
12. 增加日志查看页面；
13. 增加 GitHub 一键推送按钮或脚本。

---

## 十八、建议的一键总启动脚本

未来可以做一个：

```bash
/home/ai/start-claude-web-all.sh
```

内容类似：

```bash
#!/usr/bin/env bash
set -e

su - ai -c "/home/ai/claude-web-bg.sh restart"
su - ai -c "/home/ai/claude-web-cloudflare.sh restart"
su - ai -c "/home/ai/claude-web-cloudflare.sh url"
```

但如果已经在 `ai` 用户下，可以简化为：

```bash
#!/usr/bin/env bash
set -e

/home/ai/claude-web-bg.sh restart
/home/ai/claude-web-cloudflare.sh restart
/home/ai/claude-web-cloudflare.sh url
```

---

## 十九、项目交接总结

这个项目不是简单聊天页面，而是一个完整的手机本地 AI 工作台：

```text
安卓手机
  └── Termux
      └── proot-distro Ubuntu
          └── ai 用户
              └── /home/ai/claude-web
                  ├── FastAPI 后端
                  ├── Claude Web 风格前端
                  ├── SQLite 聊天数据库
                  ├── Claude Code 本地代理线路
                  ├── 第三方 API 直连流式线路
                  ├── 文件 / 图片上传
                  ├── 网页 URL 读取
                  ├── Markdown 导出分享
                  ├── tmux 后台运行
                  ├── Cloudflare Tunnel 公网访问
                  └── GitHub SSH 推送
```

核心维护原则：

```text
必须用 ai 用户运行
不要用 root 跑 Claude Code
改代码前先备份
后端修改后先 py_compile
确认正常再重启服务
不要上传 chat.db / uploads / .venv / logs
直连 API 报错时先区分是项目问题还是服务商风控
Git 推送必须 add -> commit -> push
```

常用启动命令：

```bash
proot-distro login ubuntu
su - ai
/home/ai/claude-web-bg.sh restart
/home/ai/claude-web-cloudflare.sh restart
/home/ai/claude-web-cloudflare.sh url
```

本地访问：

```text
http://127.0.0.1:8000
```

公网访问：

```text
https://xxxx.trycloudflare.com
```

这份文档可以直接交给下一位 AI 或开发者作为完整项目交接说明。
```
