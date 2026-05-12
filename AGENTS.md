# Project Instructions

## Image Link Handling

When the user provides an image URL, local image share URL, or File Browser share URL, download and identify it before answering image-content questions.

Use the local helper first:

```bash
scripts/fetch_image_binary.py "<url>"
```

The helper supports direct image URLs and File Browser share links such as:

```text
http://127.0.0.1:8080/share/<hash>
```

It resolves File Browser shares to `/api/public/dl/<hash>/<filename>`, downloads the real image binary into `/tmp/codex-images/`, and prints JSON metadata including `path`, `mime`, `width`, `height`, and `bytes`.

After it succeeds, open the reported local `path` with the image viewing tool before describing the image.

## Default Public Startup

When the user asks to start this project, start it with the stable ngrok public URL by default:

```bash
cd /home/ai/claude-web
./start-local.sh
```

`start-local.sh` starts the local backend and then calls `start-public.sh`, which uses:

```bash
NGROK_URL=kindling-shaft-creamer.ngrok-free.dev ./start-ngrok.sh
```

The expected public entry is:

```text
https://kindling-shaft-creamer.ngrok-free.dev
```

Do not use Cloudflare Tunnel as the default public entry unless the user explicitly asks for Cloudflare.

## Runtime Monitoring And Termux Keepalive

This project is intended to run on Termux/Android and can be killed by Android power management. Always check the runtime system before assuming the app code is broken.

Primary paths:

```text
Project: /home/ai/claude-web
Local health: http://127.0.0.1:8000/api/health
Backend tmux session: claude-web-backend
Public URL file: /home/ai/claude-web/ngrok-url.txt
ngrok log: /home/ai/claude-web/logs/ngrok.log
Backend log: /home/ai/claude-web/logs/backend.log
```

Termux scripts currently installed outside the repository:

```text
/data/data/com.termux/files/home/keep-termux-alive.sh
/data/data/com.termux/files/home/check-claude-web.sh
/data/data/com.termux/files/home/check-claude-web-public.sh
/data/data/com.termux/files/home/restart-claude-web.sh
/data/data/com.termux/files/home/.termux/boot/keepalive
```

JobScheduler tasks:

```text
9001 keep-termux-alive.sh: keeps wake lock and persistent notification active.
9002 check-claude-web.sh: checks local backend health and restarts backend if needed.
9003 check-claude-web-public.sh: checks public ngrok health and restarts public tunnel if needed.
```

Use these commands to inspect the monitoring system:

```bash
termux-job-scheduler --pending
/data/data/com.termux/files/home/check-claude-web.sh
/data/data/com.termux/files/home/check-claude-web-public.sh
curl -fsS http://127.0.0.1:8000/api/health
tmux ls
pgrep -af 'uvicorn|ngrok|cloudflared'
tail -n 80 /home/ai/claude-web/logs/backend.log
tail -n 80 /home/ai/claude-web/logs/ngrok.log
```

Use these commands to restart components:

```bash
/data/data/com.termux/files/home/restart-claude-web.sh
cd /home/ai/claude-web && ./start-local.sh
```

Important operating notes:

- `termux-wake-lock` reduces sleep-related kills, but it cannot override aggressive OEM background killing by itself.
- Termux, Termux:API, and the hosting app should be set to unrestricted battery mode in Android settings and locked in Recents when possible.
- A public ngrok link is not a hard uptime guarantee. The monitor can restart it, but network loss, ngrok account limits, Android process kills, or heartbeat timeouts can still interrupt access.
- `start-local.sh` is the default project startup script and opens the public ngrok entry when possible. `start-public.sh` and `start-ngrok.sh` perform the actual ngrok launch. `start-cloudflare.sh` exists but should only be used when Cloudflare is explicitly requested.
- The external helper `/home/ai/claude-web-ngrok.sh` is a more verbose tmux-based ngrok supervisor, but the current active default is the repository script `./start-local.sh`.
- Do not reintroduce `/root/claude-web` paths. The active project is `/home/ai/claude-web`.
