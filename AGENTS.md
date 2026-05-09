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
./start-public.sh
```

`start-public.sh` uses:

```bash
NGROK_URL=kindling-shaft-creamer.ngrok-free.dev ./start-ngrok.sh
```

The expected public entry is:

```text
https://kindling-shaft-creamer.ngrok-free.dev
```

Do not use Cloudflare Tunnel as the default public entry unless the user explicitly asks for Cloudflare.
