from pathlib import Path
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import uuid
import re
import base64
import time
import html
import mimetypes
from urllib.parse import quote, urlparse, parse_qs, unquote

from fastapi import UploadFile

from chat_utils import estimate_round_tokens, is_image_file
from config import BASE_DIR, DEFAULT_MODEL, MAX_IMAGE_UPLOAD_BYTES, MAX_UPLOAD_BYTES, MODEL_TEMPERATURE, UPLOAD_DIR
from db import db_add_message


DNS_CACHE = {}
SEARCH_CACHE = {}
PLANNER_CACHE = {}
PAGE_CACHE = {}
TAVILY_CLIENT = None
GITHUB_SOURCE_CACHE = {}
STREAM_REQUEST_TIMEOUT = int(os.environ.get("CLAUDE_WEB_STREAM_TIMEOUT", "90") or "90")
VISION_STREAM_TIMEOUT = int(os.environ.get("CLAUDE_WEB_VISION_STREAM_TIMEOUT", "120") or "120")
GITHUB_SOURCE_MAX_CHARS = int(os.environ.get("GITHUB_SOURCE_MAX_CHARS", "200000") or "200000")
GITHUB_OBSERVATION_MAX_CHARS = int(os.environ.get("GITHUB_OBSERVATION_MAX_CHARS", "180000") or "180000")
GITHUB_RAW_MAX_BYTES = int(os.environ.get("GITHUB_RAW_MAX_BYTES", str(2 * 1024 * 1024)) or str(2 * 1024 * 1024))


def _cache_get(cache: dict, key: str):
    item = cache.get(key)
    if not item:
        return None
    if item.get("expires_at", 0) <= time.time():
        cache.pop(key, None)
        return None
    return item.get("value")


def _cache_set(cache: dict, key: str, value, ttl_seconds: int):
    cache[key] = {
        "value": value,
        "expires_at": time.time() + max(1, int(ttl_seconds)),
    }
    return value


def get_tavily_client():
    global TAVILY_CLIENT
    if TAVILY_CLIENT is not None:
        return TAVILY_CLIENT
    api_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not api_key:
        TAVILY_CLIENT = False
        return None
    try:
        from tavily import TavilyClient
        TAVILY_CLIENT = TavilyClient(api_key=api_key)
        return TAVILY_CLIENT
    except Exception:
        TAVILY_CLIENT = False
        return None


def _strip_html_tags(text: str) -> str:
    value = re.sub(r"<script[\s\S]*?</script>", " ", text or "", flags=re.I)
    value = re.sub(r"<style[\s\S]*?</style>", " ", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _trim_excerpt(text: str, max_chars: int = 2200) -> str:
    value = re.sub(r"\s+", " ", (text or "")).strip()
    if not value:
        return ""
    if len(value) <= max_chars:
        return value
    return value[:max_chars].rstrip() + "..."


def _recent_context_digest(context_messages=None, max_messages: int = 6, max_chars: int = 1200) -> str:
    parts = []
    total = 0
    for msg in reversed(context_messages or []):
        role = getattr(msg, "role", "")
        if role not in ("user", "assistant"):
            continue
        content = _clean_context_for_search(getattr(msg, "content", "") or "")
        if not content:
            continue
        line = f"{'用户' if role == 'user' else '助手'}: {content}"
        remain = max_chars - total
        if remain <= 0:
            break
        if len(line) > remain:
            line = line[-remain:]
        parts.append(line)
        total += len(line)
        if len(parts) >= max_messages:
            break
    return "\n".join(reversed(parts)).strip()


def _is_dns_resolution_error(exc) -> bool:
    text = str(exc).lower()
    return (
        "name resolution" in text
        or "temporary failure in name resolution" in text
        or "nodename nor servname" in text
        or "name or service not known" in text
        or "getaddrinfo failed" in text
    )


def _is_transient_connection_error(exc) -> bool:
    text = str(exc).lower()
    return (
        "connection reset by peer" in text
        or "connection aborted" in text
        or "connection refused" in text
        or "connection timed out" in text
        or "timed out" in text
        or "broken pipe" in text
        or "remote end closed connection" in text
        or "ssl" in text and "wrong version number" in text
    )


def _decode_dns_name(data: bytes, offset: int) -> tuple[str, int]:
    labels = []
    jumped = False
    next_offset = offset

    while offset < len(data):
        length = data[offset]
        if length == 0:
            offset += 1
            if not jumped:
                next_offset = offset
            break

        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(data):
                break
            pointer = ((length & 0x3F) << 8) | data[offset + 1]
            if not jumped:
                next_offset = offset + 2
            offset = pointer
            jumped = True
            continue

        offset += 1
        labels.append(data[offset:offset + length].decode("ascii", errors="ignore"))
        offset += length
        if not jumped:
            next_offset = offset

    return ".".join(label for label in labels if label), next_offset


def _query_dns_a_record(hostname: str, server: str, timeout: float = 1.8) -> list[str]:
    import random
    import socket
    import struct

    query_id = random.randint(0, 65535)
    packet = struct.pack("!HHHHHH", query_id, 0x0100, 1, 0, 0, 0)
    for label in hostname.strip(".").split("."):
        raw = label.encode("ascii")
        if not raw or len(raw) > 63:
            return []
        packet += bytes([len(raw)]) + raw
    packet += b"\x00" + struct.pack("!HH", 1, 1)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(packet, (server, 53))
        data, _ = sock.recvfrom(512)
    finally:
        sock.close()

    if len(data) < 12:
        return []

    resp_id, _flags, qdcount, ancount, _nscount, _arcount = struct.unpack("!HHHHHH", data[:12])
    if resp_id != query_id:
        return []

    offset = 12
    for _ in range(qdcount):
        _name, offset = _decode_dns_name(data, offset)
        offset += 4

    ips = []
    for _ in range(ancount):
        _name, offset = _decode_dns_name(data, offset)
        if offset + 10 > len(data):
            break
        rtype, rclass, _ttl, rdlength = struct.unpack("!HHIH", data[offset:offset + 10])
        offset += 10
        rdata = data[offset:offset + rdlength]
        offset += rdlength
        if rtype == 1 and rclass == 1 and rdlength == 4:
            ips.append(socket.inet_ntoa(rdata))

    return ips


def resolve_hostname_resilient(hostname: str) -> list[str]:
    import socket
    import time

    host = (hostname or "").strip().strip(".").lower()
    if not host or host == "localhost":
        return []

    cached = DNS_CACHE.get(host)
    now = time.time()
    if cached and cached.get("expires", 0) > now:
        return list(cached.get("ips") or [])

    ips = []
    try:
        for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM):
            ip = item[4][0]
            if ":" not in ip and ip not in ips:
                ips.append(ip)
    except Exception:
        pass

    if not ips:
        for server in ("223.5.5.5", "119.29.29.29", "8.8.8.8", "1.1.1.1"):
            try:
                for ip in _query_dns_a_record(host, server):
                    if ip not in ips:
                        ips.append(ip)
            except Exception:
                continue
            if ips:
                break

    if ips:
        DNS_CACHE[host] = {"ips": ips, "expires": now + 300}
    return ips


@contextmanager
def patched_getaddrinfo_for_host(hostname: str, ips: list[str]):
    import socket

    host = (hostname or "").strip().strip(".").lower()
    original_getaddrinfo = socket.getaddrinfo

    def patched(host_arg, port, family=0, type=0, proto=0, flags=0):
        if str(host_arg or "").strip().strip(".").lower() == host and ips:
            results = []
            socktype = type or socket.SOCK_STREAM
            protocol = proto or socket.IPPROTO_TCP
            for ip in ips:
                results.append((socket.AF_INET, socktype, protocol, "", (ip, port)))
            return results
        return original_getaddrinfo(host_arg, port, family, type, proto, flags)

    socket.getaddrinfo = patched
    try:
        yield
    finally:
        socket.getaddrinfo = original_getaddrinfo


def resilient_urlopen(req, timeout=STREAM_REQUEST_TIMEOUT):
    import time
    import urllib.request

    parsed = urlparse(req.full_url)
    host = parsed.hostname or ""
    ips = resolve_hostname_resilient(host)
    last_exc = None
    retryable_http_statuses = {
        408, 425, 429,
        500, 502, 503, 504,
        520, 521, 522, 524, 525, 526,
    }

    for attempt in range(3):
        try:
            if ips:
                with patched_getaddrinfo_for_host(host, ips):
                    return urllib.request.urlopen(req, timeout=timeout)
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            last_exc = exc
            code = int(getattr(exc, "code", 0) or 0)
            if code not in retryable_http_statuses or attempt >= 2:
                raise
            retry_after = 0
            try:
                header_value = exc.headers.get("Retry-After") if getattr(exc, "headers", None) else None
                if header_value:
                    retry_after = int(str(header_value).strip())
            except Exception:
                retry_after = 0
            if retry_after <= 0:
                if code in (520, 524):
                    retry_after = 5 * (attempt + 1) * (attempt + 1)
                else:
                    retry_after = 1 + attempt * 2
            time.sleep(max(1, min(retry_after, 30)))
            continue
        except Exception as exc:
            last_exc = exc
            if not _is_dns_resolution_error(exc) or attempt >= 2:
                raise
            DNS_CACHE.pop(host.strip().strip(".").lower(), None)
            ips = resolve_hostname_resilient(host)
            time.sleep(0.35 * (attempt + 1))

    raise last_exc


def extract_urls_from_text(text: str, max_urls: int = 3):
    """
    从文本中提取 http/https 链接。
    """
    urls = re.findall(r'https?://[^\s\]\)\}，。、“”‘’<>"]+', text or "")
    result = []
    for u in urls:
        u = u.rstrip(".,;:!?，。；：！？")
        if u and u not in result:
            result.append(u)
    return result[:max_urls]


def _github_api_headers() -> dict:
    headers = {
        "User-Agent": "Claude-Web/1.0",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN", "").strip() or os.environ.get("GH_TOKEN", "").strip()
    if token:
        headers["Authorization"] = "Bearer " + token
    return headers


def _github_api_json(url: str, timeout: int = 15):
    import urllib.request

    req = urllib.request.Request(url, headers=_github_api_headers(), method="GET")
    with resilient_urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read(1024 * 1024 * 4).decode("utf-8", errors="ignore"))


def _github_api_url(owner: str, repo: str, path: str = "", ref: str = "") -> str:
    encoded_path = "/".join(quote(part, safe="") for part in (path or "").split("/") if part)
    url = f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/contents"
    if encoded_path:
        url += "/" + encoded_path
    if ref:
        url += "?ref=" + quote(ref, safe="")
    return url


def _github_repo_api_url(owner: str, repo: str) -> str:
    return f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"


def _github_tree_api_url(owner: str, repo: str, ref: str) -> str:
    return (
        f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
        f"/git/trees/{quote(ref, safe='')}?recursive=1"
    )


def _github_raw_url(owner: str, repo: str, ref: str, path: str) -> str:
    encoded_path = "/".join(quote(part, safe="") for part in path.strip("/").split("/") if part)
    return f"https://raw.githubusercontent.com/{quote(owner, safe='')}/{quote(repo, safe='')}/{quote(ref, safe='')}/{encoded_path}"


def _is_github_url(url: str) -> bool:
    parsed = urlparse(url or "")
    host = (parsed.netloc or "").lower()
    return host == "github.com" or host.endswith(".github.com")


def extract_github_urls_from_text(text: str, max_urls: int = 4) -> list[str]:
    urls = []
    for url in extract_urls_from_text(text, max_urls=max_urls):
        if _is_github_url(url) and url not in urls:
            urls.append(url)
    return urls[:max_urls]


def _decode_github_file_content(item: dict, max_chars: int) -> str:
    content = item.get("content") or ""
    encoding = (item.get("encoding") or "").lower()
    if encoding == "base64":
        raw = base64.b64decode(content.encode("utf-8"), validate=False)
        text = raw.decode("utf-8", errors="replace")
    else:
        text = str(content)
    if len(text) > max_chars:
        return text[:max_chars] + "\n\n[GitHub 源码内容过长，已截断]"
    return text


def _summarize_github_directory(items: list[dict], max_entries: int = 80) -> str:
    rows = []
    for item in sorted(items or [], key=lambda x: (x.get("type") != "dir", x.get("name", "").lower()))[:max_entries]:
        item_type = item.get("type") or "file"
        size = item.get("size")
        path = item.get("path") or item.get("name") or ""
        suffix = f" ({size} bytes)" if item_type == "file" and isinstance(size, int) else ""
        rows.append(f"- {item_type}: {path}{suffix}")
    if not rows:
        return "[GitHub 目录为空或未返回内容]"
    text = "GitHub 目录内容：\n" + "\n".join(rows)
    if len(items or []) > max_entries:
        text += f"\n[目录项过多，仅显示前 {max_entries} 项]"
    return text


def _parse_github_url(url: str) -> dict | None:
    parsed = urlparse(url or "")
    if not _is_github_url(url):
        return None
    parts = [unquote(p) for p in (parsed.path or "").strip("/").split("/") if p]
    if len(parts) < 2:
        return None
    owner, repo = parts[0], parts[1]
    repo = repo[:-4] if repo.endswith(".git") else repo
    if not owner or not repo:
        return None
    mode = parts[2] if len(parts) >= 3 else ""
    rest = parts[3:] if mode in ("blob", "tree") else parts[2:]
    return {"owner": owner, "repo": repo, "mode": mode, "rest": rest}


def _github_default_branch(owner: str, repo: str) -> str:
    cache_key = f"github-default-branch:{owner}/{repo}"
    cached = _cache_get(GITHUB_SOURCE_CACHE, cache_key)
    if cached:
        return cached
    data = _github_api_json(_github_repo_api_url(owner, repo), timeout=15)
    branch = (data.get("default_branch") or "main").strip() or "main"
    return _cache_set(GITHUB_SOURCE_CACHE, cache_key, branch, ttl_seconds=1800)


def _extract_requested_repo_paths(text: str) -> list[str]:
    value = text or ""
    candidates = []
    patterns = [
        r"(?<![\w./-])[\w.-]+(?:/[\w.@+-]+)+\.[A-Za-z0-9]{1,12}(?![\w/-])",
        r"(?<![\w.-])[\w.-]+\.(?:py|js|ts|tsx|jsx|java|go|rs|cpp|c|h|hpp|cs|php|rb|swift|kt|kts|sh|bash|zsh|html|css|scss|vue|svelte|json|ya?ml|toml|ini|md|txt|sql)(?![\w.-])",
    ]
    for pattern in patterns:
        for match in re.findall(pattern, value, flags=re.I):
            item = str(match).strip().strip("`'\"，。；;:：!?！？()[]{}<>")
            if item and item not in candidates:
                candidates.append(item)
    lowered = value.lower()
    code_analysis_terms = (
        "源码", "代码", "函数", "调用链", "逻辑", "入口", "路由", "接口", "实现", "分析",
        "联网搜索", "搜索功能", "web_search", "search", "provider", "mcp",
        "source", "code", "function", "implementation", "call chain", "route", "endpoint",
    )
    if any(term in lowered for term in code_analysis_terms):
        for item in (
            "app.py",
            "services.py",
            "service.py",
            "chat_utils.py",
            "config.py",
            "README.md",
        ):
            if item not in candidates:
                candidates.append(item)
    return candidates[:8]


def _default_repo_paths_for_prompt(user_prompt: str) -> list[str]:
    paths = _extract_requested_repo_paths(user_prompt)
    for item in (
        "README.md",
        "app.py",
        "main.py",
        "server.py",
        "api.py",
        "services.py",
        "service.py",
        "routes.py",
        "config.py",
        "settings.py",
        "chat_utils.py",
        "package.json",
        "pyproject.toml",
        "requirements.txt",
    ):
        if item not in paths:
            paths.append(item)
    return paths


def _github_repo_files(owner: str, repo: str, ref: str = "") -> tuple[str, list[str]]:
    branch = ref or _github_default_branch(owner, repo)
    cache_key = f"github-tree:{owner}/{repo}:{branch}"
    tree = _cache_get(GITHUB_SOURCE_CACHE, cache_key)
    if tree is None:
        data = _github_api_json(_github_tree_api_url(owner, repo, branch), timeout=20)
        tree = data.get("tree") or []
        _cache_set(GITHUB_SOURCE_CACHE, cache_key, tree, ttl_seconds=900)

    files = [
        item.get("path", "")
        for item in tree
        if item.get("type") == "blob" and item.get("path")
    ]
    return branch, files


def _github_find_matching_paths(owner: str, repo: str, requested_paths: list[str], ref: str = "", limit: int = 4) -> list[str]:
    if not requested_paths:
        return []
    _branch, files = _github_repo_files(owner, repo, ref=ref)
    matched = []
    lowered_files = [(path, path.lower()) for path in files]
    for requested in requested_paths:
        req = requested.strip("/").lower()
        if not req:
            continue
        req_name = req.rsplit("/", 1)[-1]
        req_names = [req_name]
        if "." in req_name:
            stem, ext = req_name.rsplit(".", 1)
            if stem.endswith("s") and len(stem) > 1:
                req_names.append(stem[:-1] + "." + ext)
            else:
                req_names.append(stem + "s." + ext)
        exact = [path for path, low in lowered_files if low == req]
        suffix = [path for path, low in lowered_files if low.endswith("/" + req)]
        basename = [path for path, low in lowered_files if low.rsplit("/", 1)[-1] in req_names]
        for path in exact + suffix + basename:
            if path not in matched:
                matched.append(path)
            if len(matched) >= max(1, int(limit or 4)):
                return matched
    return matched


def _github_prompt_terms(user_prompt: str) -> list[str]:
    value = re.sub(r"https?://\\S+", " ", (user_prompt or "").lower())
    raw_terms = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{2,}|[\u4e00-\u9fff]{2,}", value)
    stop = {
        "https", "http", "github", "com", "www", "这个", "项目", "仓库", "源码", "代码",
        "分析", "具体", "详细", "一下", "功能", "逻辑", "函数", "文件", "读取",
        "source", "code", "repo", "repository", "project", "please", "function",
    }
    terms = []
    for term in raw_terms:
        if term in stop or len(term) > 40:
            continue
        if term not in terms:
            terms.append(term)
    return terms[:20]


def _github_file_score(path: str, user_prompt: str, terms: list[str]) -> int:
    low = path.lower()
    name = low.rsplit("/", 1)[-1]
    score = 0
    if name in ("readme.md", "readme.rst", "readme.txt"):
        score += 42
    if name in ("app.py", "main.py", "server.py", "api.py", "services.py", "service.py", "routes.py", "views.py"):
        score += 45
    if name in ("package.json", "pyproject.toml", "requirements.txt", "config.py", "settings.py", "docker-compose.yml"):
        score += 18
    if low.startswith(("src/", "app/", "server/", "backend/", "api/")):
        score += 12
    if low.startswith(("test/", "tests/", "docs/", ".github/", "android/", "ios/")):
        score -= 12
    if any(part in low for part in ("node_modules/", "dist/", "build/", "vendor/", ".next/", "__pycache__/")):
        score -= 80
    if name.endswith((".lock", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".pdf", ".zip", ".gz")):
        score -= 90

    for term in terms:
        if term in low:
            score += 16

    prompt = (user_prompt or "").lower()
    intent_keywords = {
        "search": ("search", "web_search", "tavily", "serpapi", "google", "bing", "duckduckgo", "browser", "mcp"),
        "联网搜索": ("search", "web_search", "tavily", "serpapi", "google", "bing", "duckduckgo", "browser", "mcp"),
        "路由": ("route", "routes", "app.py", "api", "server"),
        "接口": ("route", "routes", "app.py", "api", "server"),
        "前端": ("static/", "index.html", "src/", "components", "pages"),
        "配置": ("config", "settings", ".env", "toml", "yaml", "yml"),
    }
    for key, needles in intent_keywords.items():
        if key in prompt:
            for needle in needles:
                if needle in low:
                    score += 22
    return score


def _candidate_branches(ref: str = "") -> list[str]:
    branches = []
    for item in (ref, "main", "master", "dev", "develop"):
        item = (item or "").strip()
        if item and item not in branches:
            branches.append(item)
    return branches


def _github_select_relevant_paths(owner: str, repo: str, user_prompt: str, ref: str = "", limit: int = 4) -> list[str]:
    _branch, files = _github_repo_files(owner, repo, ref=ref)
    terms = _github_prompt_terms(user_prompt)
    scored = []
    for path in files:
        score = _github_file_score(path, user_prompt, terms)
        if score > -50:
            scored.append((score, path.count("/"), len(path), path))
    scored.sort(key=lambda item: (-item[0], item[1], item[2], item[3].lower()))
    picked = []
    for score, _depth, _length, path in scored:
        if score <= 0 and picked:
            break
        if path not in picked:
            picked.append(path)
        if len(picked) >= limit:
            break
    if picked:
        return picked
    fallback_names = ("README.md", "app.py", "main.py", "server.py", "services.py", "package.json", "pyproject.toml")
    lowered = {path.lower(): path for path in files}
    for name in fallback_names:
        path = lowered.get(name.lower())
        if path and path not in picked:
            picked.append(path)
        if len(picked) >= limit:
            break
    return picked


def _fetch_github_raw_source(owner: str, repo: str, path: str, refs: list[str]) -> dict | None:
    import urllib.error
    import urllib.request

    for ref in refs:
        raw_url = _github_raw_url(owner, repo, ref, path)
        try:
            req = urllib.request.Request(
                raw_url,
                headers={
                    "User-Agent": "Claude-Web/1.0",
                    "Accept": "text/plain,*/*",
                },
                method="GET",
            )
            with resilient_urlopen(req, timeout=15) as resp:
                raw = resp.read(GITHUB_RAW_MAX_BYTES)
            text = raw.decode("utf-8", errors="replace")
            if len(text) > GITHUB_SOURCE_MAX_CHARS:
                text = text[:GITHUB_SOURCE_MAX_CHARS] + "\n\n[GitHub raw 源码内容过长，已截断]"
            return {
                "title": f"{owner}/{repo}: {path}",
                "url": f"https://github.com/{owner}/{repo}/blob/{ref}/{path}",
                "excerpt": text,
                "provider": "github-mcp",
                "quality": "official",
                "query": "",
            }
        except urllib.error.HTTPError as exc:
            if int(getattr(exc, "code", 0) or 0) == 404:
                continue
        except Exception:
            continue
    return None


def _github_source_from_item(url: str, owner: str, repo: str, item, ref: str = "") -> dict:
    if isinstance(item, list):
        excerpt = _summarize_github_directory(item)
        parsed_url = _parse_github_url(url) or {}
        rest = parsed_url.get("rest") or []
        title_path = "/".join(rest[1:]) if (parsed_url.get("mode") == "tree" and len(rest) > 1) else ""
        title = f"{owner}/{repo}" + (f": {title_path}" if title_path else " directory")
        return {
            "title": title,
            "url": url,
            "excerpt": excerpt,
            "provider": "github-mcp",
            "quality": "official",
            "query": "",
        }

    item_type = item.get("type") or "file"
    path = item.get("path") or ""
    title = f"{owner}/{repo}: {path or item.get('name') or ref or 'repository'}"
    if item_type == "file":
        excerpt = _decode_github_file_content(item, max_chars=GITHUB_SOURCE_MAX_CHARS)
    elif item_type == "dir":
        nested = _github_api_json(_github_api_url(owner, repo, path, ref), timeout=15)
        excerpt = _summarize_github_directory(nested)
    else:
        excerpt = json.dumps(item, ensure_ascii=False)[:4000]
    return {
        "title": title,
        "url": item.get("html_url") or url,
        "excerpt": excerpt,
        "provider": "github-mcp",
        "quality": "official",
        "query": "",
    }


def fetch_github_source(url: str) -> dict | None:
    parsed = _parse_github_url(url)
    if not parsed:
        return None

    cache_key = "github:" + normalize_source_url(url)
    cached = _cache_get(GITHUB_SOURCE_CACHE, cache_key)
    if cached is not None:
        return dict(cached)

    owner = parsed["owner"]
    repo = parsed["repo"]
    mode = parsed["mode"]
    rest = parsed["rest"]

    try:
        if mode in ("blob", "tree") and rest:
            errors = []
            for split_at in range(1, len(rest) + 1):
                ref = "/".join(rest[:split_at])
                path = "/".join(rest[split_at:])
                try:
                    item = _github_api_json(_github_api_url(owner, repo, path, ref), timeout=15)
                    source = _github_source_from_item(url, owner, repo, item, ref=ref)
                    return _cache_set(GITHUB_SOURCE_CACHE, cache_key, source, ttl_seconds=900)
                except Exception as exc:
                    errors.append(str(exc))
                    continue
            raise RuntimeError(errors[-1] if errors else "无法解析 GitHub ref/path")

        item = _github_api_json(_github_api_url(owner, repo), timeout=15)
        source = _github_source_from_item(url, owner, repo, item)
        return _cache_set(GITHUB_SOURCE_CACHE, cache_key, source, ttl_seconds=900)
    except Exception as exc:
        source = {
            "title": f"{owner}/{repo}",
            "url": url,
            "excerpt": f"[GitHub 源码读取失败：{exc}]",
            "provider": "github-mcp",
            "quality": "official",
            "query": "",
        }
        return _cache_set(GITHUB_SOURCE_CACHE, cache_key, source, ttl_seconds=120)


def collect_github_sources_from_urls(urls: list[str], max_sources: int = 4, user_prompt: str = "") -> list[dict]:
    sources = []
    seen = set()
    requested_paths = _extract_requested_repo_paths(user_prompt)
    for url in urls or []:
        href = normalize_source_url(url)
        if not href or href in seen or not _is_github_url(href):
            continue
        seen.add(href)
        parsed = _parse_github_url(href)
        if parsed and parsed.get("mode") not in ("blob", "tree"):
            matched_file_count = 0
            owner = parsed["owner"]
            repo = parsed["repo"]
            branch = ""
            try:
                branch = _github_default_branch(owner, repo)
                selected_paths = _github_find_matching_paths(
                    owner,
                    repo,
                    requested_paths,
                    ref=branch,
                    limit=max_sources,
                )
                if len(selected_paths) < max_sources:
                    for path in _github_select_relevant_paths(
                        owner,
                        repo,
                        user_prompt,
                        ref=branch,
                        limit=max_sources,
                    ):
                        if path not in selected_paths:
                            selected_paths.append(path)
                        if len(selected_paths) >= max_sources:
                            break
                for path in selected_paths:
                    file_url = f"https://github.com/{owner}/{repo}/blob/{branch}/{path}"
                    source = fetch_github_source(file_url)
                    if source and not str(source.get("excerpt", "")).startswith("[GitHub 源码读取失败"):
                        sources.append(source)
                        matched_file_count += 1
                    if len(sources) >= max_sources:
                        return sources
            except Exception:
                pass
            if matched_file_count:
                continue
            for path in _default_repo_paths_for_prompt(user_prompt):
                source = _fetch_github_raw_source(owner, repo, path, _candidate_branches(branch))
                if source:
                    sources.append(source)
                if len(sources) >= max_sources:
                    return sources
            if sources:
                continue
        source = fetch_github_source(href)
        if source and not str(source.get("excerpt", "")).startswith("[GitHub 源码读取失败"):
            sources.append(source)
        if len(sources) >= max_sources:
            break
    return sources


def fetch_webpage_text(url: str, max_chars: int = 12000, timeout: int = 8) -> str:
    """
    读取网页并提取正文纯文本。
    只使用 Python 标准库，避免新增依赖。
    """
    import urllib.request
    from html.parser import HTMLParser

    class SimpleTextParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts = []
            self.skip_tag = None

        def handle_starttag(self, tag, attrs):
            tag = tag.lower()
            if tag in ("script", "style", "noscript", "svg"):
                self.skip_tag = tag
            if tag in ("p", "br", "div", "section", "article", "li", "h1", "h2", "h3"):
                self.parts.append("\n")

        def handle_endtag(self, tag):
            tag = tag.lower()
            if self.skip_tag == tag:
                self.skip_tag = None
            if tag in ("p", "div", "section", "article", "li"):
                self.parts.append("\n")

        def handle_data(self, data):
            if self.skip_tag:
                return
            data = data.strip()
            if data:
                self.parts.append(data + " ")

        def get_text(self):
            return "".join(self.parts)

    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 Claude-Web/1.0",
                "Accept": "text/html,text/plain,*/*",
            },
            method="GET",
        )

        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content_type = resp.headers.get("Content-Type", "")
            raw = resp.read(1024 * 1024 * 2)

        charset = "utf-8"
        m = re.search(r"charset=([\w\-]+)", content_type, re.I)
        if m:
            charset = m.group(1)

        html = raw.decode(charset, errors="ignore")

        if "text/plain" in content_type:
            text = html
        else:
            parser = SimpleTextParser()
            parser.feed(html)
            text = parser.get_text()

        text = re.sub(r"\s+", " ", text).strip()

        if not text:
            return "[网页读取成功，但没有提取到有效正文]"

        if len(text) > max_chars:
            text = text[:max_chars] + "\n\n[网页内容过长，已截断]"

        return text

    except Exception as e:
        return f"[读取网页失败：{e}]"


def extract_webpage_via_api(url: str, max_chars: int = 12000) -> str:
    """
    Prefer API-based extraction to avoid direct-site 403s.
    Env options:
    - TAVILY_API_KEY: Tavily Extract API
    - FIRECRAWL_API_KEY: Firecrawl scrape API
    - JINA_API_KEY: optional token for r.jina.ai Reader
    """
    import urllib.request
    import urllib.error

    tavily_client = get_tavily_client()
    if tavily_client:
        try:
            data = tavily_client.extract(urls=[url], extract_depth="basic", include_images=False)
            results = data.get("results") or []
            if results:
                text = (results[0].get("raw_content") or results[0].get("content") or "").strip()
                if text:
                    return text[:max_chars] + ("\n\n[网页内容过长，已截断]" if len(text) > max_chars else "")
        except Exception as e:
            return f"[Tavily 网页解析失败：{e}]"

    tavily_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if tavily_key:
        try:
            payload = json.dumps({
                "urls": [url],
                "extract_depth": "basic",
                "include_images": False,
            }).encode("utf-8")
            req = urllib.request.Request(
                "https://api.tavily.com/extract",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer " + tavily_key,
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="ignore"))
            results = data.get("results") or []
            if results:
                text = (results[0].get("raw_content") or results[0].get("content") or "").strip()
                if text:
                    return text[:max_chars] + ("\n\n[网页内容过长，已截断]" if len(text) > max_chars else "")
        except Exception as e:
            return f"[Tavily 网页解析失败：{e}]"

    firecrawl_key = os.environ.get("FIRECRAWL_API_KEY", "").strip()
    if firecrawl_key:
        try:
            payload = json.dumps({
                "url": url,
                "formats": ["markdown"],
                "onlyMainContent": True,
            }).encode("utf-8")
            req = urllib.request.Request(
                "https://api.firecrawl.dev/v1/scrape",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer " + firecrawl_key,
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=25) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="ignore"))
            page = data.get("data") or {}
            text = (page.get("markdown") or page.get("content") or "").strip()
            if text:
                return text[:max_chars] + ("\n\n[网页内容过长，已截断]" if len(text) > max_chars else "")
        except Exception as e:
            return f"[Firecrawl 网页解析失败：{e}]"

    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return fetch_webpage_text(url, max_chars=max_chars, timeout=8)
        reader_url = "https://r.jina.ai/http://" + url.split("://", 1)[1]
        headers = {
            "User-Agent": "Mozilla/5.0 Claude-Web/1.0",
            "Accept": "text/plain,application/json,*/*",
            "X-No-Cache": "true",
        }
        jina_key = os.environ.get("JINA_API_KEY", "").strip()
        if jina_key:
            headers["Authorization"] = "Bearer " + jina_key
        req = urllib.request.Request(reader_url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=20) as resp:
            text = resp.read(1024 * 1024).decode("utf-8", errors="ignore").strip()
        if text:
            return text[:max_chars] + ("\n\n[网页内容过长，已截断]" if len(text) > max_chars else "")
    except Exception:
        pass

    return fetch_webpage_text(url, max_chars=max_chars, timeout=8)


def enhance_prompt_with_url_fetch(user_prompt: str) -> str:
    """
    直连模式专用：
    如果用户输入里包含 URL，则读取网页内容并拼接到 prompt。
    """
    urls = [url for url in extract_urls_from_text(user_prompt) if not _is_github_url(url)]
    if not urls:
        return user_prompt

    parts = []
    parts.append("用户输入中包含网页链接。以下是后端自动读取到的网页内容，请结合这些内容回答。")
    parts.append("")

    for i, url in enumerate(urls, 1):
        parts.append(f"【网页 {i}】{url}")
        parts.append(extract_webpage_via_api(url))
        parts.append("")

    parts.append("用户原始问题：")
    parts.append(user_prompt)

    return "\n".join(parts)


def looks_like_search_request(text: str) -> bool:
    value = (text or "").lower()
    patterns = [
        "搜索", "搜一下", "搜一搜", "查一下", "查一查", "查找", "检索",
        "最新", "最近", "新闻", "消息", "动态", "网页", "浏览网页",
        "新功能", "功能清单", "有什么", "有哪些", "区别", "变化", "不一样",
        "search ", "google ", "look up", "browse", "web search", "latest", "what's new"
    ]
    return any(p in value for p in patterns)


SEARCH_COMMAND_PATTERNS = [
    r"^(你)?(帮我|给我|请)?(联网)?(搜索|搜一下|搜一搜|查一下|查一查|查找|检索|搜)(这个|一下|看看|下)?[。.!！\s]*$",
    r"^(search|look up|browse|web search)\s*(this|it|that)?[。.!！\s]*$",
]


def _strip_search_command_words(text: str) -> str:
    value = (text or "").strip()
    value = re.sub(r"^(你)?(帮我|给我|请)?", "", value).strip()
    value = re.sub(r"^(看一下|看看|了解一下|研究一下)\s*", "", value).strip()
    value = re.sub(r"^(联网)?(搜索|搜一下|搜一搜|查一下|查一查|查找|检索|搜)\s*", "", value).strip()
    value = re.sub(r"^(search|look up|browse|web search)\s+", "", value, flags=re.I).strip()
    value = re.sub(r"(一下|看看|查查|搜搜|相关信息|最新消息|最新新闻)[。.!！\s]*$", "", value).strip()
    return value


def _strip_leading_search_command_words(text: str) -> str:
    value = (text or "").strip()
    value = re.sub(r"^(你)?(帮我|给我|请)?", "", value).strip()
    value = re.sub(r"^(看一下|看看|了解一下|研究一下)\s*", "", value).strip()
    value = re.sub(r"^(联网)?(搜索|搜一下|搜一搜|查一下|查一查|查找|检索|搜)\s*", "", value).strip()
    value = re.sub(r"^(search|look up|browse|web search)\s+", "", value, flags=re.I).strip()
    value = re.sub(r"\s+", " ", value).strip(" ，,。.!！?？")
    return value


def _is_bare_search_command(text: str) -> bool:
    value = (text or "").strip().lower()
    if not value:
        return False
    return any(re.match(pattern, value, flags=re.I) for pattern in SEARCH_COMMAND_PATTERNS)


def _clean_context_for_search(text: str) -> str:
    value = re.sub(r"\s+", " ", (text or "")).strip()
    value = re.sub(r"\[[0-9]+\]", "", value)
    value = re.sub(r"```[\s\S]*?```", " ", value)
    return value.strip()


CONTEXTUAL_REFERENCE_PATTERNS = [
    "这个", "这个事", "这件事", "这家公司", "这个公司", "这款", "这个模型", "该模型",
    "他", "她", "它", "他们", "它们", "其", "该", "上述", "前面", "刚才", "你说的",
    "this", "that", "it", "they", "them", "the company", "the model", "above",
]


GENERIC_SEARCH_WORDS = {
    "这个", "这个事", "这件事", "这家公司", "这个公司", "这款", "这个模型", "该模型",
    "他", "她", "它", "他们", "它们", "其", "该", "上述", "前面", "刚才", "你说的",
    "最新", "最近", "新闻", "消息", "动态", "进展", "变化", "现在", "目前", "什么", "有什么",
    "官方", "来源", "查证", "核验", "联网", "优先", "更新",
    "this", "that", "it", "they", "them", "above", "latest", "recent", "news", "updates", "current",
    "official", "source", "sources", "verify",
}


def _strip_context_reference_words(text: str) -> str:
    value = (text or "").strip()
    for word in sorted(GENERIC_SEARCH_WORDS, key=len, reverse=True):
        value = re.sub(rf"\b{re.escape(word)}\b", " ", value, flags=re.I)
        value = value.replace(word, " ")
    value = re.sub(r"\s+", " ", value).strip(" ，,。.!！?？")
    return value


def _finalize_search_query(query: str) -> str:
    value = re.sub(r"\s+", " ", (query or "")).strip()
    if not value:
        return ""
    value = _strip_leading_search_command_words(value)
    value = rewrite_search_query(value)
    value = re.sub(r"\s+", " ", value).strip(" ，,。.!！?？")
    return value


def _looks_like_time_request(text: str) -> bool:
    value = (text or "").strip().lower()
    if not value:
        return False
    return any(token in value for token in (
        "几点", "几点了", "现在时间", "当前时间", "北京时间", "日期", "今天几号",
        "what time", "current time", "time in", "date in", "current date",
    ))


def _query_intent_from_prompt(prompt: str) -> str:
    value = (prompt or "").strip()
    lowered = value.lower()
    if not value:
        return ""
    if "gpt-5.5" in lowered or ("openai" in lowered and "gpt" in lowered):
        if any(word in value for word in ("发布日期", "发布时间", "什么时候发布", "何时发布", "release date", "released")):
            return "OpenAI GPT-5.5 release date official"
        if any(word in value for word in ("最新", "最新情况", "最新消息", "最近更新", "recent", "latest", "news", "update", "updates")):
            return "OpenAI GPT-5.5 latest news official"
        return "OpenAI GPT-5.5 official"
    if any(token in lowered for token in ("claude code", "anthropic", "claude")):
        if any(word in value for word in ("发布日期", "发布时间", "什么时候发布", "何时发布", "release date", "released")):
            return "Claude Code release date Anthropic official"
        if any(word in value for word in ("最新", "最新情况", "最新消息", "最近更新", "recent", "latest", "news", "update", "updates")):
            return "Claude Code latest update Anthropic official"
        return "Claude Code Anthropic official"
    if any(token in lowered for token in ("python",)) and re.search(r"3\.\d+", lowered):
        version = re.search(r"3\.\d+", lowered).group(0)
        if any(word in value for word in ("发布日期", "发布时间", "什么时候发布", "何时发布", "release date", "released")):
            return f"Python {version} release date official site:python.org"
        if any(word in value for word in ("最新", "最新消息", "最近更新", "recent", "latest", "news", "update", "updates")):
            return f"Python {version} latest update official site:python.org"
        return f"Python {version} official site:python.org"
    if any(token in lowered for token in ("apple", "m4", "iphone", "ipad", "mac")):
        if any(word in value for word in ("最新", "最新消息", "最近更新", "recent", "latest", "news", "update", "updates")):
            return "Apple latest news official site:apple.com"
    if any(token in lowered for token in ("github", "github.com")) and any(word in value for word in ("最新", "最新消息", "最近更新", "recent", "latest", "news", "update", "updates")):
        return "GitHub latest update official"
    if _looks_like_time_request(value):
        if "北京" in value or "beijing" in lowered:
            return "current Beijing time"
        if "纽约" in value or "new york" in lowered or "eastern" in lowered:
            return "current New York time"
        if "东京" in value or "tokyo" in lowered or "japan" in lowered:
            return "current Tokyo time"
        if "伦敦" in value or "london" in lowered or "uk" in lowered:
            return "current London time"
        return "current time"
    return value


def _is_comparison_request(text: str) -> bool:
    value = (text or "").lower()
    return any(token in value for token in (
        "对比", "比较", "相比", "区别", "差异", "优劣", "哪个更", "全方面", "全面",
        "vs", " versus ", "compare", "comparison", "differences", "better",
    ))


def _contains_gpt55(text: str) -> bool:
    return bool(re.search(r"gpt[-\s]?5\.5", (text or "").lower()))


def _contains_claude_opus47(text: str) -> bool:
    value = (text or "").lower()
    return (
        "opus4.7" in value
        or "opus 4.7" in value
        or "claude opus4.7" in value
        or "claude opus 4.7" in value
        or ("claude" in value and "opus" in value and "4.7" in value)
    )


def _comparison_search_queries(text: str) -> list[str]:
    if not _is_comparison_request(text):
        return []
    queries = []
    if _contains_gpt55(text) and _contains_claude_opus47(text):
        queries.extend([
            "GPT-5.5 Claude Opus 4.7 comprehensive comparison official specs",
            "OpenAI GPT-5.5 official model details",
            "Anthropic Claude Opus 4.7 official model details",
        ])
    return queries


def _dedupe_queries(items, limit: int = 4) -> list[str]:
    queries = []
    for item in items or []:
        q = _strip_leading_search_command_words(str(item or ""))
        q = re.sub(r"\s+", " ", q).strip(" ，,。.!！?？")
        if q and not _is_bad_search_query(q) and q not in queries:
            queries.append(q[:180])
        if len(queries) >= limit:
            break
    return queries



def _query_expansion_candidates(primary_query: str, user_prompt: str = "", max_queries: int = 3) -> list[str]:
    primary = _finalize_search_query(primary_query or user_prompt or "")
    if not primary or _is_bad_search_query(primary):
        return []

    candidates = [primary]
    prompt = user_prompt or primary
    lowered = prompt.lower()
    time_sensitive = any(word in prompt for word in ("最新", "最近", "新闻", "消息", "动态", "更新", "发布时间", "发布日期")) or any(
        word in lowered for word in ("latest", "recent", "news", "update", "updates", "release date", "released")
    )
    comparison = _is_comparison_request(prompt)

    primary_lowered = primary.lower()

    def add_expansion(suffix: str):
        words = [w for w in suffix.lower().split() if w]
        missing = [w for w in words if w not in primary_lowered]
        if missing:
            candidates.append(f"{primary} {' '.join(missing)}")

    if time_sensitive:
        add_expansion("official latest update")
    if any(word in prompt for word in ("官方", "来源", "文档", "公告")) or any(word in lowered for word in ("official", "docs", "documentation", "announcement")):
        add_expansion("official documentation announcement")
    if comparison:
        add_expansion("comparison review benchmark")
    if any(word in prompt for word in ("价格", "费用", "定价")) or any(word in lowered for word in ("price", "pricing", "cost")):
        add_expansion("pricing official")
    if any(word in prompt for word in ("错误", "报错", "失败", "修复", "问题")) or any(word in lowered for word in ("error", "bug", "fix", "issue", "failed")):
        add_expansion("issue fix documentation")

    filtered = _filter_aligned_search_queries(
        _dedupe_queries(candidates, limit=max_queries + 2),
        user_prompt=prompt,
        fallback_queries=[primary],
        limit=max_queries,
    )
    return filtered or [primary[:180]]

def build_fallback_search_queries(user_prompt: str, context_messages=None, max_queries: int = 3) -> list[str]:
    limit = max(1, min(int(max_queries or 3), 4))
    queries = []

    # CHIQ-style: first rewrite the conversational turn into a standalone search query.
    # Jina-style: keep that primary query, then add only aligned expansion queries.
    for query in _comparison_search_queries(user_prompt):
        if query and query not in queries:
            queries.append(query)
    contextual = build_contextual_search_query(user_prompt, context_messages=context_messages, max_chars=180)
    base = contextual or _strip_leading_search_command_words(user_prompt or "")
    for query in _query_expansion_candidates(base, user_prompt=user_prompt, max_queries=limit):
        if query and query not in queries:
            queries.append(query)
    return queries[:limit]


def _has_specific_search_entity(text: str) -> bool:
    value = (text or "").strip()
    if re.search(r"[A-Za-z][A-Za-z0-9.\-]{1,}", value):
        return True
    terms = [t for t in extract_query_terms(value) if t not in GENERIC_SEARCH_WORDS]
    return len(terms) >= 2


def _looks_context_dependent_search(prompt: str) -> bool:
    value = (prompt or "").strip().lower()
    if not value:
        return False
    if _is_bare_search_command(value):
        return True
    if any(token in value for token in CONTEXTUAL_REFERENCE_PATTERNS):
        return True
    if _has_specific_search_entity(value):
        return False
    return bool(looks_like_search_request(value) and len(extract_query_terms(value)) <= 3)


def _latest_user_context(context_messages=None, max_chars: int = 220) -> str:
    for msg in reversed(context_messages or []):
        if getattr(msg, "role", "") != "user":
            continue
        content = _clean_context_for_search(getattr(msg, "content", "") or "")
        if content and not _is_bare_search_command(content):
            return _strip_search_command_words(content)[-max_chars:]
    return ""


def _latest_assistant_context(context_messages=None, max_chars: int = 260) -> str:
    for msg in reversed(context_messages or []):
        if getattr(msg, "role", "") != "assistant":
            continue
        content = _clean_context_for_search(getattr(msg, "content", "") or "")
        if content:
            return content[-max_chars:]
    return ""


def _context_specific_queries(context_messages=None, limit: int = 3) -> list[str]:
    queries = []
    for msg in reversed(context_messages or []):
        if getattr(msg, "role", "") != "user":
            continue
        content = _clean_context_for_search(getattr(msg, "content", "") or "")
        if not content or _is_bare_search_command(content):
            continue
        candidate = _finalize_search_query(content)
        if _is_bad_search_query(candidate):
            continue
        if _has_specific_search_entity(candidate) and candidate not in queries:
            queries.append(candidate[:140])
        if len(queries) >= limit:
            break
    return queries


def build_contextual_search_query(user_prompt: str, context_messages=None, max_chars: int = 120) -> str:
    prompt = (user_prompt or "").strip()
    comparison_queries = _comparison_search_queries(prompt)
    if comparison_queries:
        return comparison_queries[0][:max_chars]
    cleaned_prompt = _strip_search_command_words(prompt)
    bare_command = _is_bare_search_command(prompt)
    context_candidates = _context_specific_queries(context_messages=context_messages, limit=3)
    context_dependent = _looks_context_dependent_search(prompt)

    if _has_specific_search_entity(prompt) and not context_dependent and not _is_bare_search_command(prompt):
        specific_prompt = _strip_leading_search_command_words(prompt)
        if specific_prompt:
            return _finalize_search_query(specific_prompt)[:max_chars]

    if cleaned_prompt and not context_dependent:
        return _finalize_search_query(cleaned_prompt)[:max_chars]

    last_user = _latest_user_context(context_messages)
    last_assistant = _latest_assistant_context(context_messages)
    context_basis = last_user or last_assistant

    if bare_command:
        if not context_candidates:
            return ""
        if len(context_candidates) == 1:
            return context_candidates[0][:max_chars]
        return ""

    if cleaned_prompt and context_basis:
        cleaned_reference = _strip_context_reference_words(cleaned_prompt)
        if not cleaned_reference and len(context_candidates) > 1:
            return ""
        if not cleaned_reference and not context_candidates:
            return ""
        query = f"{context_basis} {cleaned_reference}".strip() if cleaned_reference else context_basis
        return _finalize_search_query(query)[:max_chars]

    candidates = []
    for msg in reversed(context_messages or []):
        if getattr(msg, "role", "") not in ("user", "assistant"):
            continue
        content = _clean_context_for_search(getattr(msg, "content", "") or "")
        if not content or _is_bare_search_command(content):
            continue
        candidates.append(content)
        if len(candidates) >= 3:
            break

    if candidates:
        # 优先使用最近一条实质用户问题；如果没有，再用助手回答的主题。
        for msg in reversed(context_messages or []):
            if getattr(msg, "role", "") != "user":
                continue
            content = _clean_context_for_search(getattr(msg, "content", "") or "")
            if content and not _is_bare_search_command(content):
                return _finalize_search_query(_strip_search_command_words(content))[:max_chars]
        return _finalize_search_query(candidates[0])[:max_chars]

    return _finalize_search_query(cleaned_prompt if cleaned_prompt else prompt)[:max_chars]


def build_search_planner_prompt(user_prompt: str, context_messages=None) -> str:
    context = _recent_context_digest(context_messages=context_messages, max_messages=8, max_chars=1800)
    heuristic_query = build_contextual_search_query(user_prompt, context_messages=context_messages, max_chars=140)
    return (
        "你是联网搜索规划器。你的任务不是回答用户，而是先理解用户真实想查什么，再提交准确的搜索关键词。\n"
        "要求：\n"
        "1. 先在内部判断用户最新问题的真实对象、限制条件和搜索目的。不要输出思考过程。\n"
        "2. 搜索词必须和用户需求一致，不能改问另一个问题，不能扩大成泛泛的行业新闻。\n"
        "3. 如果用户说“这个/他/它/上述/刚才/查一下/最新消息”等指代，必须从上下文补全真实主体；无法确定主体时 should_search=false 且 search_queries=[]。\n"
        "4. 搜索词要像真实搜索引擎查询：短、具体、可检索；保留专名、产品名、公司名、人名、版本号、地点、时间范围、用户指定的比较对象。\n"
        "5. 禁止只输出“最新消息”“相关信息”“这个”“搜索一下”“查一下”等空泛词，也禁止只复述命令词。\n"
        "6. 用户只是普通聊天、写作、解释代码、数学推导、翻译、总结已给内容时，不需要联网，should_search=false。\n"
        "7. 可以给 1-4 个搜索词。第一条必须是独立主查询；后续只能是同主题扩展关键词，不能替换或泛化第一条。比较、评测、全方面对比类问题，不要只搜其中一方，优先让 queries 覆盖双方和综合比较。\n"
        "8. 只输出 JSON，不要解释。\n"
        'JSON 格式：{"should_search":true,"search_queries":["准确关键词"],"parse_links":[]}\n\n'
        f"后端基于上下文得到的候选关键词，仅供校验，不要盲从：\n{heuristic_query or '（无）'}\n\n"
        f"最近对话上下文：\n{context or '（无）'}\n\n"
        f"用户最新问题：\n{user_prompt or ''}"
    )


def _is_bad_search_query(query: str) -> bool:
    q = re.sub(r"\s+", " ", (query or "")).strip().lower()
    if not q:
        return True
    if len(q) <= 2:
        return True
    bad_exact = {
        "搜索", "查一下", "搜一下", "搜一搜", "联网搜索", "最新", "最近", "消息", "新闻",
        "最新消息", "最新新闻", "相关信息", "这个", "这个事", "这件事", "上述", "前面",
        "search", "look up", "browse", "latest", "news", "updates", "recent updates",
    }
    if q in bad_exact:
        return True
    stripped = _strip_context_reference_words(_strip_search_command_words(q))
    if not stripped or stripped in bad_exact:
        return True
    meaningful_terms = [t for t in extract_query_terms(stripped) if t not in GENERIC_SEARCH_WORDS]
    return not meaningful_terms



def _model_like_search_terms(text: str) -> list[str]:
    value = (text or "").lower()
    terms = []
    patterns = [
        r"\b[a-z]+[-\s]?[a-z]*[-\s]?\d+(?:\.\d+)?[a-z0-9.-]*\b",
        r"\b\d+(?:\.\d+)+(?:[-\w.]*)?\b",
        r"\b[a-z0-9_.+-]+@[a-z0-9.-]+\.[a-z]{2,}\b",
        r"\b[a-z0-9.-]+\.[a-z]{2,}\b",
    ]
    for pattern in patterns:
        for match in re.findall(pattern, value, flags=re.I):
            term = re.sub(r"\s+", " ", str(match).strip().lower())
            if term and term not in terms:
                terms.append(term)
    return terms


def _important_search_terms(text: str) -> list[str]:
    terms = []
    for term in extract_query_terms(text):
        if term in GENERIC_SEARCH_WORDS:
            continue
        if term in {"search", "latest", "news", "update", "updates", "official", "source", "sources"}:
            continue
        if re.search(r"[a-z0-9]", term) and len(term) >= 2 and term not in terms:
            terms.append(term)
    for term in _model_like_search_terms(text):
        if term not in terms:
            terms.append(term)
    return terms


def _query_keeps_user_subject(query: str, user_prompt: str = "", context_messages=None, fallback_queries=None) -> bool:
    q = (query or "").lower()
    if not q:
        return False

    basis_parts = [user_prompt or ""]
    basis_parts.extend(fallback_queries or [])
    contextual = build_contextual_search_query(user_prompt, context_messages=context_messages, max_chars=180)
    if contextual:
        basis_parts.append(contextual)
    basis = "\n".join(str(part or "") for part in basis_parts)

    model_terms = _model_like_search_terms(basis)
    if model_terms and not any(term in q or term.replace(" ", "-") in q or term.replace("-", " ") in q for term in model_terms):
        return False

    important_terms = _important_search_terms(basis)
    broad_entity_terms = {
        "openai", "anthropic", "google", "apple", "microsoft", "github",
        "meta", "amazon", "aws", "claude", "gpt",
    }
    anchor_terms = [term for term in important_terms if term not in broad_entity_terms]
    terms_to_check = anchor_terms or important_terms
    if terms_to_check:
        return any(
            term in q or term.replace(" ", "-") in q or term.replace("-", " ") in q
            for term in terms_to_check
        )

    return True


def _filter_aligned_search_queries(queries: list[str], user_prompt: str = "", context_messages=None, fallback_queries=None, limit: int = 4) -> list[str]:
    filtered = []
    for query in queries or []:
        q = re.sub(r"\s+", " ", str(query or "")).strip(" ，,。.!！?？")
        if not q or _is_bad_search_query(q):
            continue
        if not _query_keeps_user_subject(q, user_prompt=user_prompt, context_messages=context_messages, fallback_queries=fallback_queries):
            continue
        if q not in filtered:
            filtered.append(q[:180])
        if len(filtered) >= limit:
            break
    return filtered

def normalize_search_plan(raw_plan: dict | None, fallback: dict, user_prompt: str = "", context_messages=None) -> dict:
    if not isinstance(raw_plan, dict):
        return fallback

    queries = []
    for item in raw_plan.get("search_queries") or []:
        q = _strip_leading_search_command_words(str(item or ""))
        q = re.sub(r"\s+", " ", q).strip(" ，,。.!！?？")
        if _is_bad_search_query(q):
            continue
        if q and q not in queries:
            queries.append(q[:180])
        if len(queries) >= 4:
            break

    links = []
    for item in raw_plan.get("parse_links") or []:
        url = normalize_source_url(str(item or ""))
        if url and url not in links:
            links.append(url)
        if len(links) >= 2:
            break

    fallback_queries = _dedupe_queries(fallback.get("search_queries") or [], limit=3)
    queries = _filter_aligned_search_queries(
        queries,
        user_prompt=user_prompt,
        context_messages=context_messages,
        fallback_queries=fallback_queries,
        limit=4,
    )

    should_search = bool(raw_plan.get("should_search")) or bool(queries) or bool(links)
    if not queries and fallback_queries:
        queries = fallback_queries
    if should_search and not queries and not links:
        queries = _dedupe_queries(
            build_fallback_search_queries(user_prompt, context_messages=context_messages, max_queries=3),
            limit=3,
        )
    if should_search and not queries and not links:
        should_search = False

    return {
        "should_search": should_search,
        "search_queries": queries,
        "parse_links": links or fallback.get("parse_links", [])[:2],
    }


BAD_SOURCE_PATTERNS = [
    "zhihu.com",
    "quora.com",
    "reddit.com",
    "stackoverflow.com/questions",
    "medium.com",
    "dev.to",
    "csdn.net",
    "jianshu.com",
    "cnblogs.com",
    "baidu.com/jingyan",
    "baijiahao.baidu.com",
    "tieba.baidu.com",
    "weixin.qq.com",
    "mp.weixin.qq.com",
    "microsoft.com/store",
    "apps.microsoft.com",
    "softonic.com",
    "alternativeto.net",
    "tomato",
    "fanqie",
    "小说",
    "smapply.org",
    "open-openai.com",
    "claudelog.com",
    "pressreader.com",
    "newsbreak.com",
    "benzinga.com/pressreleases",
]

PREFERRED_SOURCE_PATTERNS = [
    "openai.com",
    "help.openai.com",
    "platform.openai.com",
    "anthropic.com",
    "docs.anthropic.com",
    "python.org",
    "docs.python.org",
    "developer.apple.com",
    "apple.com/newsroom",
    "microsoft.com/en-us/research",
    "learn.microsoft.com",
    "developers.google.com",
    "cloud.google.com/docs",
    "aws.amazon.com/blogs",
    "docs.aws.amazon.com",
    "github.com/openai",
    "github.com/anthropics",
    "github.com/python",
    "github.com/",
    "techcrunch.com",
    "theverge.com",
    "reuters.com",
    "bloomberg.com",
    "cnbc.com",
    "cnn.com",
    "tomshardware.com",
    "computerworld.com",
    "wired.com",
    "arstechnica.com",
]

OFFICIAL_DOC_PATTERNS = [
    "/docs",
    "docs.",
    "documentation",
    "developer.",
    "developers.",
    "learn.",
    "help.",
    "support.",
]

ORIGINAL_RELEASE_PATTERNS = [
    "/blog/",
    "/news/",
    "/newsroom/",
    "/press/",
    "/releases/",
    "/release",
    "/changelog",
    "/announcements/",
]

AUTHORITY_MEDIA_DOMAINS = [
    "reuters.com",
    "apnews.com",
    "bloomberg.com",
    "wsj.com",
    "ft.com",
    "theverge.com",
    "techcrunch.com",
    "arstechnica.com",
    "wired.com",
    "cnbc.com",
]


def _source_url_parts(url: str) -> tuple[str, str]:
    parsed = urlparse(url or "")
    return (parsed.netloc.lower().replace("www.", ""), parsed.path.lower())


def classify_source_quality(url: str, title: str = "") -> str:
    host, path = _source_url_parts(url)
    haystack = f"{host}{path} {(title or '').lower()}"
    if "github.com/" in haystack and len([p for p in path.split("/") if p]) >= 2:
        return "project_repo"
    if host.endswith(("openai.com", "anthropic.com")) and path.startswith("/index/"):
        return "original_release"
    if any(pattern in haystack for pattern in OFFICIAL_DOC_PATTERNS):
        return "official_docs"
    if any(pattern in haystack for pattern in ORIGINAL_RELEASE_PATTERNS):
        return "original_release"
    if any(host.endswith(domain) for domain in AUTHORITY_MEDIA_DOMAINS):
        return "authority_media"
    if any(pattern in haystack for pattern in BAD_SOURCE_PATTERNS):
        return "low_quality"
    return "general"


def extract_query_terms(text: str) -> list[str]:
    raw = re.findall(r"[A-Za-z0-9\.\-]+|[\u4e00-\u9fff]{2,}", (text or "").lower())
    stop_words = {
        "搜索", "一下", "最新", "消息", "新闻", "请", "看看", "关于", "并", "简短", "总结",
        "latest", "news", "about", "search", "please", "summarize", "summary"
    }
    terms = []
    for token in raw:
        token = token.strip().lower()
        if not token or token in stop_words:
            continue
        if token not in terms:
            terms.append(token)
    return terms


def rewrite_search_query(query: str) -> str:
    q = (query or "").strip()
    lowered = q.lower()
    if _is_comparison_request(q) or (_contains_gpt55(q) and _contains_claude_opus47(q)):
        return re.sub(r"\bgpt\s?5\.5\b", "GPT-5.5", q, flags=re.I)
    if ("北京时间" in q or "北京" in q or "beijing" in lowered) and any(word in q for word in ("几点", "时间", "现在", "当前", "日期", "几号")):
        if "日期" in q or "几号" in q:
            return "current date in Beijing China"
        return "current Beijing time"
    if "gpt-5.5" in lowered:
        if any(word in q for word in ("发布日期", "发布时间", "什么时候发布", "何时发布", "release date", "released")):
            return "OpenAI GPT-5.5 release date official"
        if any(word in q for word in ("最新", "latest", "news", "update", "updates", "最近更新")):
            return "OpenAI GPT-5.5 latest news official"
        return "OpenAI GPT-5.5 official"
    if "claude code" in lowered:
        if any(word in q for word in ("发布日期", "发布时间", "什么时候发布", "何时发布", "release date", "released")):
            return "Claude Code release date Anthropic official"
        if any(word in q for word in ("最新", "latest", "news", "update", "updates", "最近更新")):
            return "Claude Code latest update Anthropic official"
        return "Claude Code Anthropic official"
    if "python" in lowered and re.search(r"3\.\d+", lowered):
        version = re.search(r"3\.\d+", lowered).group(0)
        if any(word in q for word in ("发布日期", "发布时间", "什么时候发布", "何时发布", "release date", "released")):
            return f"Python {version} release date official site:python.org"
        if any(word in q for word in ("最新", "latest", "news", "update", "updates", "最近更新")):
            return f"Python {version} latest update official site:python.org"
        return f"Python {version} official site:python.org"
    return q


def source_relevance_score(query: str, title: str, url: str, excerpt: str = "") -> int:
    haystack = " ".join([title or "", url or "", excerpt or ""]).lower()
    score = 0
    rewritten_query = rewrite_search_query(query)
    lowered_query = rewritten_query.lower()
    query_terms = extract_query_terms(rewrite_search_query(query))

    for term in query_terms:
      if term in haystack:
          score += 6

    important_phrases = [
        "claude code",
        "gpt-5.5",
        "openai gpt-5.5",
        "python 3.",
    ]
    for phrase in important_phrases:
        if phrase in lowered_query and phrase not in haystack:
            score -= 35

    for pattern in PREFERRED_SOURCE_PATTERNS:
        if pattern in (url or "").lower():
            score += 10

    for pattern in BAD_SOURCE_PATTERNS:
        if pattern in (url or "").lower() or pattern in (title or "").lower():
            score -= 18

    quality = classify_source_quality(url, title)
    if quality == "official_docs":
        score += 28
    elif quality == "original_release":
        score += 24
    elif quality == "project_repo":
        score += 22
    elif quality == "authority_media":
        score += 16
    elif quality == "low_quality":
        score -= 24

    if "openai" in haystack:
        score += 5
    if "gpt-5.5" in haystack:
        score += 12
    if "latest" in haystack or "最新" in haystack or "news" in haystack or "新闻" in haystack:
        score += 4

    return score


def normalize_source_title(title: str, fallback_url: str) -> str:
    title = re.sub(r"\s+", " ", (title or "")).strip()
    if title:
        return title[:160]
    parsed = urlparse(fallback_url or "")
    host = parsed.netloc or fallback_url or "未知来源"
    return host[:160]


def normalize_source_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.netloc.endswith("duckduckgo.com"):
        qs = parse_qs(parsed.query or "")
        uddg = qs.get("uddg")
        if uddg and uddg[0]:
            return unquote(uddg[0]).strip()
    if parsed.netloc.endswith("bing.com") and parsed.path.startswith("/ck/a"):
        qs = parse_qs(parsed.query or "")
        target = qs.get("u")
        if target and target[0]:
            candidate = unquote(target[0]).strip()
            if candidate.startswith("http"):
                return candidate
            if candidate.startswith("a1"):
                payload = candidate[2:]
                try:
                    padding = "=" * (-len(payload) % 4)
                    decoded = base64.b64decode(payload + padding).decode("utf-8", errors="ignore").strip()
                    if decoded.startswith("http"):
                        return decoded
                except Exception:
                    pass
    return url


def fetch_tavily_search_results(query: str, max_results: int = 5) -> list[dict]:
    client = get_tavily_client()
    if not client:
        return []

    q = rewrite_search_query((query or "").strip())
    if not q:
        return []

    try:
        data = client.search(
            q,
            search_depth="advanced",
            max_results=max(1, min(int(max_results or 5), 10)),
            include_raw_content=False,
        )
    except Exception:
        return []

    items = data.get("results") or []
    cleaned = []
    seen = set()
    for item in items:
        href = normalize_source_url(item.get("url", ""))
        if not href or href in seen:
            continue
        seen.add(href)
        title = normalize_source_title(item.get("title", ""), href)
        excerpt = _trim_excerpt(item.get("content") or item.get("snippet") or "", 2200)
        score = source_relevance_score(q, title, href, excerpt) + 18
        cleaned.append({
            "title": title,
            "url": href,
            "description": excerpt,
            "score": score,
            "provider": "tavily",
            "quality": classify_source_quality(href, title),
        })
    cleaned.sort(key=lambda x: x.get("score", 0), reverse=True)
    return cleaned[:max_results]


def fetch_serpapi_search_results(query: str, max_results: int = 5) -> list[dict]:
    token = os.environ.get("SERPAPI_API_KEY", "").strip()
    if not token:
        return []

    q = rewrite_search_query((query or "").strip())
    if not q:
        return []

    try:
        import serpapi
        client = serpapi.Client(api_key=token)
        data = client.search({
            "engine": "google",
            "q": q,
            "location": "Austin, Texas, United States",
            "google_domain": "google.com",
            "hl": "en",
            "gl": "us",
            "num": max(1, min(int(max_results or 5), 10)),
        })
    except Exception:
        return []

    items = data.get("organic_results") or data.get("results") or []
    cleaned = []
    seen = set()

    for item in items:
        href = normalize_source_url(item.get("link") or item.get("url") or "")
        if not href or href in seen:
            continue
        seen.add(href)
        title = normalize_source_title(item.get("title", ""), href)
        excerpt = _trim_excerpt(item.get("snippet") or item.get("snippet_highlighted_words") or item.get("description") or "", 2200)
        cleaned.append({
            "title": title,
            "url": href,
            "description": excerpt,
            "score": source_relevance_score(q, title, href, excerpt) + 14,
            "provider": "serpapi",
            "quality": classify_source_quality(href, title),
        })
        if len(cleaned) >= max_results:
            break

    cleaned.sort(key=lambda x: x.get("score", 0), reverse=True)
    return cleaned[:max_results]


def merge_search_results(result_groups: list[list[dict]], max_results: int = 5) -> list[dict]:
    merged = []
    seen = set()

    for group in result_groups:
        for item in group or []:
            href = normalize_source_url(item.get("url", ""))
            if not href or href in seen:
                continue
            seen.add(href)
            normalized = dict(item)
            normalized["url"] = href
            merged.append(normalized)

    merged.sort(key=lambda x: x.get("score", 0), reverse=True)
    return merged[:max_results]


def fetch_primary_search_results(query: str, max_results: int = 5) -> list[dict]:
    return fetch_tavily_search_results(query, max_results=max_results)


def select_balanced_sources(results: list[dict], max_results: int) -> list[dict]:
    if not results:
        return []

    limit = max(1, int(max_results or 4))
    sorted_results = sorted(
        results,
        key=lambda x: x.get("score", source_relevance_score("", x.get("title", ""), x.get("url", ""), x.get("excerpt", ""))),
        reverse=True,
    )
    best_score = sorted_results[0].get("score", 0)

    selected = []
    selected_urls = set()

    def add_first(predicate):
        if len(selected) >= limit:
            return
        for item in sorted_results:
            url = item.get("url", "")
            if not url or url in selected_urls:
                continue
            if item.get("score", 0) < max(1, best_score - 28):
                continue
            if predicate(item):
                selected.append(item)
                selected_urls.add(url)
                return

    add_first(lambda item: item.get("provider") == "tavily")
    add_first(lambda item: item.get("provider") == "serpapi")

    for item in sorted_results:
        if len(selected) >= limit:
            break
        url = item.get("url", "")
        if not url or url in selected_urls:
            continue
        selected.append(item)
        selected_urls.add(url)

    return selected


def fetch_search_results(query: str, max_results: int = 5) -> list[dict]:
    q = (query or "").strip()
    if not q:
        return []

    cache_key = f"search:{rewrite_search_query(q)}:{int(max_results or 5)}"
    cached = _cache_get(SEARCH_CACHE, cache_key)
    if cached is not None:
        return cached

    limit = max(1, min(int(max_results or 5), 10))
    groups = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(fetch_tavily_search_results, q, limit),
            executor.submit(fetch_serpapi_search_results, q, limit),
        ]
        for future in as_completed(futures):
            try:
                groups.append(future.result() or [])
            except Exception:
                groups.append([])

    round_robin = []
    seen = set()
    max_pool = max(limit * 3, 8)
    idx = 0
    has_more = True
    while has_more and len(round_robin) < max_pool:
        has_more = False
        for group in groups:
            if idx < len(group):
                item = dict(group[idx])
                href = normalize_source_url(item.get("url", ""))
                if href and href not in seen:
                    seen.add(href)
                    item["url"] = href
                    round_robin.append(item)
                has_more = True
        idx += 1

    merged = merge_search_results([round_robin], max_results=max_pool)
    selected = select_balanced_sources(merged, limit)
    return _cache_set(SEARCH_CACHE, cache_key, selected, ttl_seconds=300)


def build_sources_context_block(sources: list[dict], search_meta: dict | None = None) -> str:
    search_meta = search_meta or {}
    searched = bool(search_meta.get("searched"))
    search_queries = [str(x).strip() for x in (search_meta.get("queries") or []) if str(x).strip()]
    parse_links = [str(x).strip() for x in (search_meta.get("parse_links") or []) if str(x).strip()]

    if not sources and not searched:
        return ""

    if not sources and searched:
        parts = [
            "系统已经在后端完成了实时联网搜索/网页读取，但本次没有检索到足够可靠的来源摘录。",
            "回答时不得说“我不能联网”“我无法实时搜索”“截至我可用信息范围”等模板话。",
            "你必须明确说明：本次联网搜索已经执行，但没有找到足够可靠的来源来确认答案。",
            "如果用户需要，你可以建议用户换一个更具体的搜索目标、提供官方网站链接，或缩小时间范围。",
            "",
        ]
        if search_queries:
            parts.append("本次实际搜索词：")
            for idx, query in enumerate(search_queries, 1):
                parts.append(f"{idx}. {query}")
            parts.append("")
        if parse_links:
            parts.append("本次尝试读取的链接：")
            for idx, url in enumerate(parse_links, 1):
                parts.append(f"{idx}. {url}")
            parts.append("")
        return "\n".join(parts).strip()

    parts = [
        "系统已经在后端完成了实时联网搜索/网页读取。以下内容就是本次实时联网检索到的来源摘录。",
        "回答时不得说“我不能联网”“我无法实时搜索”“截至我可用信息范围”等模板话；你应当基于下面来源作答。",
        "如果来源不足以证明用户问题，请明确说“本次联网搜索没有找到足够可靠的来源证明……”，并说明哪些来源是第三方报道、哪些是官方来源。",
        "如果答案引用了这些来源，请在相关句子后使用 [1]、[2] 这种编号引用。",
        "不要编造不存在的来源编号。",
        "",
    ]

    if search_queries:
        parts.append("本次实际搜索词：")
        for idx, query in enumerate(search_queries, 1):
            parts.append(f"{idx}. {query}")
        parts.append("")

    for item in sources:
        idx = item.get("index")
        title = item.get("title", "未命名来源")
        url = item.get("url", "")
        excerpt = item.get("excerpt", "")
        query = item.get("query", "")
        parts.append(f"[{idx}] {title}")
        if query:
            parts.append(f"实际搜索词: {query}")
        if url:
            parts.append(f"URL: {url}")
        if excerpt:
            if excerpt.startswith("[读取网页失败"):
                parts.append("状态:")
                parts.append("该来源链接已找到，但后端未能读取正文。只能作为官方/原始链接核验入口，不可把它当作已读取正文证据。")
            else:
                parts.append("摘录:")
                parts.append(excerpt)
        parts.append("")

    return "\n".join(parts).strip()


def collect_search_sources(user_prompt: str, max_results: int = 4, context_messages=None) -> list[dict]:
    results = []

    search_query = build_contextual_search_query(user_prompt, context_messages=context_messages)
    search_results = fetch_search_results(search_query, max_results=max_results)
    for item in search_results:
        excerpt = item.get("description") or ""
        score = item.get("score", source_relevance_score(user_prompt, item["title"], item["url"], excerpt))
        results.append({
            "title": item["title"],
            "url": item["url"],
            "excerpt": excerpt,
            "score": score,
            "provider": item.get("provider", ""),
            "quality": item.get("quality") or classify_source_quality(item.get("url", ""), item.get("title", "")),
            "query": search_query,
        })

    results = results[:max_results]
    final = []
    for idx, item in enumerate(results, 1):
        final.append({
            "index": idx,
            "title": normalize_source_title(item.get("title", ""), item.get("url", "")),
            "url": item.get("url", ""),
            "excerpt": item.get("excerpt", ""),
            "provider": item.get("provider", ""),
            "quality": item.get("quality") or classify_source_quality(item.get("url", ""), item.get("title", "")),
            "query": item.get("query", ""),
        })
    return final


def search_plan_for_prompt(user_prompt: str) -> dict:
    urls = extract_urls_from_text(user_prompt, max_urls=4)
    return {
        "has_urls": bool(urls),
        "should_search": looks_like_search_request(user_prompt),
        "urls": urls,
    }


def _extract_json_object(text: str) -> dict | None:
    if not text:
        return None
    candidates = re.findall(r"\{[\s\S]*\}", text)
    for raw in candidates:
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue
    return None


def _split_system_prompt(system_prompt: str) -> str:
    value = (system_prompt or "").strip()
    if not value:
        return ""
    value = re.sub(r"^\s*系统提示词：\s*\n?", "", value)
    value = re.sub(r"\n\s*请在整个回复中遵守上面的系统提示词。\s*$", "", value)
    return value.strip()


def _normalize_protocol(protocol: str, api_model: str = "") -> str:
    value = (protocol or "").strip().lower()
    if value in ("claude", "completions", "responses"):
        return value
    model = (api_model or "").strip().lower()
    if model.startswith("gpt") or "gpt-" in model:
        return "completions"
    return "claude"


def _add_default_claude_web_search_tool(body: dict) -> dict:
    if isinstance(body, dict) and not body.get("tools"):
        body["tools"] = [{
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": 5,
        }]
    return body


def _add_anthropic_system_prompt(body: dict, system_text: str) -> dict:
    system_text = (system_text or "").strip()
    if system_text:
        body["system"] = system_text
    return body


def _extract_response_output_text(data: dict) -> str:
    if not isinstance(data, dict):
        return ""

    if isinstance(data.get("output_text"), str):
        return data.get("output_text", "").strip()

    parts = []

    def walk(node):
        if isinstance(node, dict):
            text = node.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
            if isinstance(node.get("content"), list):
                for item in node.get("content") or []:
                    walk(item)
            if isinstance(node.get("output"), list):
                for item in node.get("output") or []:
                    walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data.get("output"))
    if not parts:
        walk(data)
    return "".join(parts).strip()


def call_direct_text_api(
    prompt: str,
    api_base_url: str,
    api_auth_token: str,
    api_model: str = DEFAULT_MODEL,
    api_protocol: str = "",
    system_prompt: str = "",
    max_tokens: int = 900,
    temperature: float = 0.1,
) -> str:
    import urllib.request
    import urllib.error

    base_url = (api_base_url or "").strip().rstrip("/")
    token = (api_auth_token or "").strip()
    model = (api_model or DEFAULT_MODEL).strip()

    if not base_url:
        raise RuntimeError("缺少 API URL")
    if not token:
        raise RuntimeError("缺少 API Key")

    protocol = _normalize_protocol(api_protocol, model)

    if protocol == "responses":
        return call_direct_responses_api(
            prompt,
            base_url,
            token,
            api_model=model,
            system_prompt=system_prompt,
            max_output_tokens=max_tokens,
        )

    lower_model = model.lower()

    if protocol == "completions" or lower_model.startswith("gpt") or "gpt-" in lower_model:
        return call_direct_chat_completions_text(
            prompt,
            base_url,
            token,
            api_model=model,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    url = build_api_url(base_url, "/v1/messages")
    system_text = _split_system_prompt(system_prompt)
    messages = [{"role": "user", "content": prompt}]
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": messages,
        "stream": False,
    }
    _add_anthropic_system_prompt(body, system_text)
    _add_default_claude_web_search_tool(body)
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "x-api-key": token,
            "Authorization": "Bearer " + token,
            "anthropic-version": "2023-06-01",
            "User-Agent": "Mozilla/5.0 Claude-Web/1.0",
        },
        method="POST",
    )
    try:
        with resilient_urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
        parts = []
        for item in data.get("content", []):
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return "\n".join(parts).strip()
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError("Anthropic接口失败: " + (err or str(e)))


def call_direct_chat_completions_text(
    prompt: str,
    api_base_url: str,
    api_auth_token: str,
    api_model: str = DEFAULT_MODEL,
    system_prompt: str = "",
    max_tokens: int = 900,
    temperature: float = 0.1,
) -> str:
    import urllib.request
    import urllib.error

    base_url = (api_base_url or "").strip().rstrip("/")
    token = (api_auth_token or "").strip()
    model = (api_model or DEFAULT_MODEL).strip()

    if not base_url:
        raise RuntimeError("缺少 API URL")
    if not token:
        raise RuntimeError("缺少 API Key")

    url = build_api_url(base_url, "/v1/chat/completions")
    messages = []
    system_text = _split_system_prompt(system_prompt)
    if system_text:
        messages.append({"role": "system", "content": system_text})
    messages.append({"role": "user", "content": prompt})
    body = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": "Bearer " + token,
            "User-Agent": "Mozilla/5.0 Claude-Web/1.0",
        },
        method="POST",
    )
    try:
        with resilient_urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(item.get("text", ""))
            return "\n".join(parts).strip()
        return str(content or "").strip()
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError("OpenAI接口失败: " + (err or str(e)))


def call_direct_responses_api(
    prompt: str,
    api_base_url: str,
    api_auth_token: str,
    api_model: str = DEFAULT_MODEL,
    system_prompt: str = "",
    max_output_tokens: int = 900,
    search_context_size: str = "low",
    use_web_search: bool | None = None,
    input_payload=None,
) -> str:
    import urllib.request
    import urllib.error

    base_url = (api_base_url or "").strip().rstrip("/")
    token = (api_auth_token or "").strip()
    model = (api_model or DEFAULT_MODEL).strip()
    if not base_url:
        raise RuntimeError("缺少 API URL")
    if not token:
        raise RuntimeError("缺少 API Key")

    url = build_api_url(base_url, "/v1/responses")
    use_web_search = True if use_web_search is None else bool(use_web_search)
    body = {
        "model": model,
        "instructions": _split_system_prompt(system_prompt) or None,
        "input": input_payload if input_payload is not None else prompt,
        "max_output_tokens": max_output_tokens,
    }
    if use_web_search:
        body["tools"] = [{"type": "web_search"}]
        body["tool_choice"] = "auto"
    body = {k: v for k, v in body.items() if v not in (None, "", [], {})}
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": "Bearer " + token,
            "User-Agent": "Mozilla/5.0 Claude-Web/1.0",
        },
        method="POST",
    )
    try:
        with resilient_urlopen(req, timeout=STREAM_REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
        return _extract_response_output_text(data) or "(responses 接口无输出)"
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError("Responses接口失败: " + (err or str(e)))


def stream_direct_responses_api_text(
    prompt: str,
    api_base_url: str,
    api_auth_token: str,
    api_model: str = DEFAULT_MODEL,
    system_prompt: str = "",
    max_output_tokens: int = 4096,
    search_context_size: str = "low",
    use_web_search: bool | None = None,
    input_payload=None,
):
    import urllib.request
    import urllib.error

    base_url = (api_base_url or "").strip().rstrip("/")
    token = (api_auth_token or "").strip()
    model = (api_model or DEFAULT_MODEL).strip()
    if not base_url:
        yield "缺少 API URL"
        return
    if not token:
        yield "缺少 API Key"
        return

    url = build_api_url(base_url, "/v1/responses")
    use_web_search = True if use_web_search is None else bool(use_web_search)
    body = {
        "model": model,
        "instructions": _split_system_prompt(system_prompt) or None,
        "input": input_payload if input_payload is not None else prompt,
        "max_output_tokens": max_output_tokens,
        "stream": True,
    }
    if use_web_search:
        body["tools"] = [{"type": "web_search"}]
        body["tool_choice"] = "auto"
    body = {k: v for k, v in body.items() if v not in (None, "", [], {})}
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream, application/json, text/plain, */*",
            "Authorization": "Bearer " + token,
            "User-Agent": "Mozilla/5.0 Claude-Web/1.0",
        },
        method="POST",
    )
    try:
        with resilient_urlopen(req, timeout=STREAM_REQUEST_TIMEOUT) as resp:
            event_name = ""
            data_lines = []
            def flush_event():
                nonlocal event_name, data_lines
                payload = "\n".join(data_lines).strip()
                if payload and payload != "[DONE]":
                    try:
                        obj = json.loads(payload)
                        typ = obj.get("type") or event_name
                        if typ == "response.output_text.delta":
                            delta = obj.get("delta") or ""
                            if delta:
                                yield delta
                        elif typ == "error" or obj.get("error"):
                            err = obj.get("error") or obj
                            yield "\n[Responses流式接口失败]\n" + json.dumps(err, ensure_ascii=False)
                    except Exception:
                        pass
                event_name = ""
                data_lines = []

            for raw in resp:
                text = raw.decode("utf-8", errors="ignore")
                for line in text.splitlines():
                    line = line.rstrip("\r")
                    if not line:
                        yield from flush_event()
                        continue
                    if line.startswith(":"):
                        continue
                    if line.startswith("event:"):
                        event_name = line[6:].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[5:].strip())
            if data_lines:
                yield from flush_event()
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="ignore")
        yield (
            "\n[Responses流式接口失败]\n"
            f"HTTP {getattr(e, 'code', '')} {getattr(e, 'reason', '')}\n"
            f"请求地址: {url}\n"
            + (err or str(e))
        )
    except Exception as e:
        yield "\n[Responses流式接口失败]\n" + str(e)


def plan_search_actions(
    user_prompt: str,
    context_messages=None,
    api_base_url: str = "",
    api_auth_token: str = "",
    api_model: str = DEFAULT_MODEL,
) -> dict:
    prompt = (user_prompt or "").strip()
    cache_key = json.dumps({
        "prompt": prompt[:600],
        "context": _recent_context_digest(context_messages=context_messages)[:600],
    }, ensure_ascii=False, sort_keys=True)
    cached = _cache_get(PLANNER_CACHE, cache_key)
    if cached is not None:
        return cached

    urls = extract_urls_from_text(prompt, max_urls=4)
    fallback_queries = build_fallback_search_queries(prompt, context_messages=context_messages, max_queries=3)
    heuristic = {
        "should_search": bool(looks_like_search_request(prompt)),
        "search_queries": fallback_queries,
        "parse_links": urls[:2],
    }
    if heuristic["parse_links"]:
        heuristic["should_search"] = True

    if api_base_url and api_auth_token:
        try:
            planner_text = call_direct_text_api(
                build_search_planner_prompt(prompt, context_messages=context_messages),
                api_base_url,
                api_auth_token,
                api_model=api_model or DEFAULT_MODEL,
                max_tokens=260,
                temperature=0,
            )
            planned = normalize_search_plan(
                _extract_json_object(planner_text),
                heuristic,
                user_prompt=prompt,
                context_messages=context_messages,
            )
            if heuristic.get("parse_links"):
                planned["should_search"] = True
                planned["parse_links"] = planned.get("parse_links") or heuristic.get("parse_links", [])
            return _cache_set(PLANNER_CACHE, cache_key, planned, ttl_seconds=300)
        except Exception:
            pass

    return _cache_set(PLANNER_CACHE, cache_key, heuristic, ttl_seconds=300)


def build_search_tool_call_prompt(user_prompt: str, context_messages=None, force: bool = False) -> str:
    context = _recent_context_digest(context_messages=context_messages, max_messages=10, max_chars=2400)
    fallback_queries = build_fallback_search_queries(user_prompt, context_messages=context_messages, max_queries=3)
    candidate_query = "\n".join(fallback_queries)
    force_rule = "本轮用户已经明确按下联网搜索按钮；除非没有任何可搜索对象，否则必须调用 web_search。" if force else "只有确实需要外部实时信息、事实核验、网页读取或用户明确要求搜索时，才调用 web_search。"
    return (
        "你现在处在工具调用决策阶段。你不能回答用户，只能决定是否调用搜索工具。\n"
        "可用工具：\n"
        "web_search({\"queries\":[\"主查询\",\"补充查询1\",\"补充查询2\"], \"read_urls\":[\"可选URL\"]})\n\n"
        "说明：如果 read_urls 里包含 github.com 链接，后端会优先用 GitHub 源码读取器解析文件或目录内容。\n\n"
        "决策规则：\n"
        f"1. {force_rule}\n"
        "2. 采用 CHIQ 风格：先判断新问题是新主题还是延续旧主题；若有指代，必须用最近对话补全为独立、无歧义的主查询。\n"
        "3. 采用 query expansion 风格：queries[0] 必须是保留用户原始主体的主查询；后续 query 只能添加同主题关键词/短语用于提升召回，不能替换或泛化主查询。\n"
        "4. query 必须和用户问题一致，保留专名、产品名、公司名、人名、版本号、地点、时间范围、比较对象、错误码/API 名称等关键锚点。\n"
        "5. 如果上下文不足以确定要搜索什么，输出 tool=none，不要猜。\n"
        "6. queries 要短、具体、可检索。补充查询最多 2 条，可加入 official/documentation/release notes/pricing/issue/benchmark 等限定词。\n"
        "7. 禁止输出只含“最新消息”“相关信息”“这个”“查一下”“搜索一下”或只含厂商名的空泛 query。\n"
        "8. 普通写作、翻译、数学、代码解释、总结用户已给内容，不调用搜索。\n"
        "9. 只输出 JSON，不要解释。\n\n"
        "输出格式二选一：\n"
        "{\"tool\":\"web_search\",\"queries\":[\"独立主查询\",\"同主题补充查询\"],\"read_urls\":[]}\n"
        "{\"tool\":\"none\",\"query\":\"\",\"read_urls\":[]}\n\n"
        f"后端候选 query，仅供校验；如果合理，应优先保留第一条：\n{candidate_query or '（无）'}\n\n"
        f"最近对话：\n{context or '（无）'}\n\n"
        f"用户最新问题：\n{user_prompt or ''}"
    )


def normalize_search_tool_call(raw_call: dict | None, fallback: dict, user_prompt: str = "", context_messages=None, force: bool = False) -> dict:
    if not isinstance(raw_call, dict):
        raw_call = {}

    tool = str(raw_call.get("tool") or "").strip().lower()
    raw_query = str(raw_call.get("query") or "").strip()
    raw_queries = raw_call.get("queries") or raw_call.get("search_queries") or []
    read_urls = raw_call.get("read_urls") or raw_call.get("urls") or []
    parse_links = []
    for item in read_urls:
        url = normalize_source_url(str(item or ""))
        if url and url not in parse_links:
            parse_links.append(url)
        if len(parse_links) >= 2:
            break

    queries = []
    queries = _dedupe_queries(list(raw_queries or []) + ([raw_query] if raw_query else []), limit=4)
    fallback_queries = _dedupe_queries(fallback.get("search_queries") or [], limit=3)
    queries = _filter_aligned_search_queries(
        queries,
        user_prompt=user_prompt,
        context_messages=context_messages,
        fallback_queries=fallback_queries,
        limit=4,
    )

    should_search = tool == "web_search" or force or bool(parse_links)
    if not should_search and fallback.get("should_search") and fallback_queries:
        should_search = True
    if should_search and not queries and fallback_queries:
        queries = fallback_queries
    if should_search and not queries and not parse_links:
        queries = _dedupe_queries(
            build_fallback_search_queries(user_prompt, context_messages=context_messages, max_queries=3),
            limit=3,
        )
    if should_search and not queries and not parse_links:
        should_search = False

    return {
        "should_search": should_search,
        "search_queries": queries[:4],
        "parse_links": parse_links or fallback.get("parse_links", [])[:2],
        "tool": "web_search" if should_search else "none",
    }


def build_source_selection_prompt(user_prompt: str, context_messages, sources: list[dict], plan: dict) -> str:
    context = _recent_context_digest(context_messages=context_messages, max_messages=8, max_chars=1600)
    compact_sources = []
    for item in sources[:8]:
        compact_sources.append({
            "index": item.get("index"),
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "excerpt": _trim_excerpt(item.get("excerpt", ""), 700),
            "provider": item.get("provider", ""),
            "quality": item.get("quality") or classify_source_quality(item.get("url", ""), item.get("title", "")),
            "query": item.get("query", ""),
        })
    return (
        "你现在处在搜索结果筛选阶段。不要回答用户，只选择哪些来源值得给最终回答使用。\n"
        "规则：\n"
        "1. 只选择与用户真实问题直接相关的来源。\n"
        "2. 来源优先级：官方文档 > 原始发布页 > 项目仓库 > 权威媒体 > 普通网页。\n"
        "3. 明显论坛搬运、SEO 冗余页、二次转载、内容农场、应用商店聚合页要降权；除非没有更好来源，否则不要选。\n"
        "4. 如果结果都不相关，selected_indices=[]。\n"
        "5. 最多选择 4 个来源。\n"
        "6. 只输出 JSON，不要解释。\n\n"
        "输出格式：{\"selected_indices\":[1,2],\"reason\":\"一句很短的筛选依据\"}\n\n"
        f"最近对话：\n{context or '（无）'}\n\n"
        f"用户最新问题：\n{user_prompt or ''}\n\n"
        f"本轮工具调用：{json.dumps({'tool': plan.get('tool', 'web_search'), 'queries': plan.get('search_queries', []), 'parse_links': plan.get('parse_links', [])}, ensure_ascii=False)}\n\n"
        f"候选来源 JSON：\n{json.dumps(compact_sources, ensure_ascii=False)}"
    )


def select_sources_via_ai(user_prompt: str, context_messages, sources: list[dict], plan: dict, api_base_url: str, api_auth_token: str, api_model: str) -> list[dict]:
    if not sources or not api_base_url or not api_auth_token:
        return sources[:4]
    try:
        selector_text = call_direct_text_api(
            build_source_selection_prompt(user_prompt, context_messages, sources, plan),
            api_base_url,
            api_auth_token,
            api_model=api_model or DEFAULT_MODEL,
            max_tokens=260,
            temperature=0,
        )
        obj = _extract_json_object(selector_text) or {}
        selected_indices = []
        for item in obj.get("selected_indices") or []:
            try:
                idx = int(item)
            except Exception:
                continue
            if idx not in selected_indices:
                selected_indices.append(idx)
        if not selected_indices:
            return []
        selected = [item for item in sources if int(item.get("index") or 0) in selected_indices]
        return selected[:4] if selected else sources[:4]
    except Exception:
        return sources[:4]


def build_search_tool_observation(user_prompt: str, sources: list[dict], plan: dict) -> str:
    tool_name = "github_mcp" if sources and all(item.get("provider") == "github-mcp" for item in sources) else "web_search"
    tool_call = {
        "tool": tool_name,
        "queries": plan.get("search_queries") or [],
        "read_urls": plan.get("parse_links", []),
    }
    compact_sources = []
    for item in sources:
        excerpt_chars = GITHUB_OBSERVATION_MAX_CHARS if item.get("provider") == "github-mcp" else 1800
        compact_sources.append({
            "index": item.get("index"),
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "excerpt": _trim_excerpt(item.get("excerpt", ""), excerpt_chars),
            "provider": item.get("provider", ""),
            "quality": item.get("quality") or classify_source_quality(item.get("url", ""), item.get("title", "")),
            "query": item.get("query", ""),
        })
    return (
        "以下是本轮 AI 自主调用搜索工具后的工具记录。最终回答必须由你自行筛选这些工具结果，不要机械复述。\n"
        "如果来源 provider=github-mcp，表示后端已经读取 GitHub 仓库的具体源码文件或目录；回答源码问题时优先使用这些内容。\n"
        "如果工具结果不足以回答，就明确说本次后端联网搜索没有找到足够可靠的来源，不要编造。\n"
        "回答时不得说“我不能联网”“我无法实时搜索”“截至我可用信息范围”等模板话；后端已经完成了本轮工具决策/检索流程。\n"
        "引用来源时使用 [1]、[2] 这样的编号；不要引用没有使用的来源编号。\n\n"
        f"assistant to={tool_name}:\n{json.dumps(tool_call, ensure_ascii=False)}\n\n"
        f"tool {tool_name} result:\n{json.dumps(compact_sources, ensure_ascii=False)}\n\n"
        f"用户当前问题：\n{user_prompt or ''}"
    )


def run_search_tool_round(
    user_prompt: str,
    context_messages=None,
    api_base_url: str = "",
    api_auth_token: str = "",
    api_model: str = DEFAULT_MODEL,
    force: bool = False,
    max_results: int = 4,
) -> tuple[str, list[dict], dict]:
    prompt = (user_prompt or "").strip()
    urls = extract_urls_from_text(prompt, max_urls=4)
    fallback_queries = build_fallback_search_queries(prompt, context_messages=context_messages, max_queries=3)
    fallback = {
        "should_search": bool(force or looks_like_search_request(prompt) or urls),
        "search_queries": fallback_queries,
        "parse_links": urls[:2],
    }

    raw_call = None
    if api_base_url and api_auth_token:
        try:
            tool_text = call_direct_text_api(
                build_search_tool_call_prompt(prompt, context_messages=context_messages, force=force),
                api_base_url,
                api_auth_token,
                api_model=api_model or DEFAULT_MODEL,
                max_tokens=260,
                temperature=0,
            )
            raw_call = _extract_json_object(tool_text)
        except Exception:
            raw_call = None

    plan = normalize_search_tool_call(raw_call, fallback, user_prompt=prompt, context_messages=context_messages, force=force)
    if not plan.get("should_search") and not plan.get("parse_links"):
        return "", [], plan

    github_urls = extract_github_urls_from_text(prompt, max_urls=4)
    for url in plan.get("parse_links", []) or []:
        if _is_github_url(url) and url not in github_urls:
            github_urls.append(url)
    github_sources = collect_github_sources_from_urls(
        github_urls,
        max_sources=max(6, max_results),
        user_prompt=prompt,
    )
    github_sources = [
        item for item in github_sources
        if item.get("excerpt") and not str(item.get("excerpt", "")).startswith("[GitHub 源码读取失败")
    ]
    if github_sources:
        for idx, item in enumerate(github_sources, 1):
            item["index"] = idx
        plan["tool"] = "github_mcp"
        observation = build_search_tool_observation(prompt, github_sources, plan)
        return observation, github_sources, plan

    queries = [q for q in plan.get("search_queries", []) if q][:3]
    sources = compile_search_sources_from_queries(
        prompt,
        queries,
        parse_links=plan.get("parse_links", []),
        max_results=max(6, max_results + 2),
    )
    sources = enrich_sources_with_page_content(
        sources,
        parse_links=plan.get("parse_links", []),
        max_pages=2,
        max_chars=2000,
    )
    merged_sources = []
    seen_urls = set()
    for item in list(github_sources) + list(sources):
        href = normalize_source_url(item.get("url", ""))
        if href and href in seen_urls:
            continue
        if href:
            seen_urls.add(href)
        merged_sources.append(item)
    sources = merged_sources

    for idx, item in enumerate(sources, 1):
        item["index"] = idx

    selected_github = [item for item in sources if item.get("provider") == "github-mcp"]
    non_github_sources = [item for item in sources if item.get("provider") != "github-mcp"]
    selected = selected_github + select_sources_via_ai(
        prompt,
        context_messages,
        non_github_sources,
        plan,
        api_base_url,
        api_auth_token,
        api_model,
    )
    if not selected:
        selected = selected_github
    selected = selected[:max(1, max_results)]
    for idx, item in enumerate(selected, 1):
        item["index"] = idx
    observation = build_search_tool_observation(prompt, selected, plan) if selected else build_search_tool_observation(prompt, [], plan)
    return observation, selected, plan


def compile_search_sources_from_queries(
    user_prompt: str,
    search_queries: list[str],
    parse_links: list[str] | None = None,
    max_results: int = 4,
) -> list[dict]:
    results = []
    seen = set()
    links = parse_links or []

    per_query_limit = max(3, int(max_results or 4))
    for query in search_queries[:3]:
        search_results = fetch_search_results(query, max_results=max(4, max_results))
        for item in search_results:
            href = normalize_source_url(item.get("url", ""))
            if not href or href in seen:
                continue
            seen.add(href)
            excerpt = item.get("description") or ""
            score = item.get("score", source_relevance_score(user_prompt, item.get("title", ""), href, excerpt))
            results.append({
                "title": item.get("title", ""),
                "url": href,
                "excerpt": excerpt,
                "score": score,
                "provider": item.get("provider", ""),
                "quality": item.get("quality") or classify_source_quality(href, item.get("title", "")),
                "query": query,
            })
            if len([r for r in results if r.get("query") == query]) >= per_query_limit:
                break

    for url in links[:2]:
        href = normalize_source_url(url)
        if not href or href in seen:
            continue
        seen.add(href)
        results.append({
            "title": normalize_source_title("", href),
            "url": href,
            "excerpt": "",
            "score": source_relevance_score(user_prompt, "", href, "") + 16,
            "provider": "direct-link",
            "quality": classify_source_quality(href, ""),
            "query": "",
        })

    picked = select_balanced_sources(results, max_results=max_results)
    final = []
    for idx, item in enumerate(picked, 1):
        final.append({
            "index": idx,
            "title": normalize_source_title(item.get("title", ""), item.get("url", "")),
            "url": item.get("url", ""),
            "excerpt": _trim_excerpt(item.get("excerpt", ""), 2200),
            "provider": item.get("provider", ""),
            "quality": item.get("quality") or classify_source_quality(item.get("url", ""), item.get("title", "")),
            "query": item.get("query", ""),
        })
    return final


def enrich_sources_with_page_content(
    sources: list[dict],
    parse_links: list[str] | None = None,
    max_pages: int = 2,
    max_chars: int = 2200,
) -> list[dict]:
    enriched = []
    page_reads = 0
    explicit_links = {normalize_source_url(url) for url in (parse_links or []) if normalize_source_url(url)}
    for source in sources or []:
        item = dict(source)
        url = item.get("url", "")
        current_excerpt = (item.get("excerpt") or "").strip()
        should_read = page_reads < max_pages and url and url in explicit_links
        if should_read:
            cache_key = f"page:{url}:{max_chars}"
            fetched = _cache_get(PAGE_CACHE, cache_key)
            if fetched is None:
                fetched = extract_webpage_via_api(url, max_chars=max_chars)
                _cache_set(PAGE_CACHE, cache_key, fetched, ttl_seconds=1800)
            if fetched and not str(fetched).startswith("["):
                item["excerpt"] = _trim_excerpt(fetched, max_chars)
                page_reads += 1
        enriched.append(item)
    return enriched


def collect_search_sources_autonomous(
    user_prompt: str,
    context_messages=None,
    api_base_url: str = "",
    api_auth_token: str = "",
    api_model: str = DEFAULT_MODEL,
    max_results: int = 4,
    plan: dict | None = None,
) -> tuple[list[dict], dict]:
    if plan is None:
        plan = plan_search_actions(
            user_prompt,
            context_messages=context_messages,
            api_base_url=api_base_url,
            api_auth_token=api_auth_token,
            api_model=api_model,
        )
    if not plan.get("should_search") and not plan.get("parse_links"):
        return [], plan
    queries = [q for q in plan.get("search_queries", []) if q][:1]
    if not queries and plan.get("should_search"):
        fallback_query = build_contextual_search_query(user_prompt, context_messages=context_messages)
        if fallback_query:
            queries = [fallback_query]
    sources = compile_search_sources_from_queries(
        user_prompt,
        queries,
        parse_links=plan.get("parse_links", []),
        max_results=max_results,
    )
    sources = enrich_sources_with_page_content(
        sources,
        parse_links=plan.get("parse_links", []),
        max_pages=1,
        max_chars=1800,
    )
    for idx, item in enumerate(sources, 1):
        item["index"] = idx
    return sources, plan


def build_api_url(base_url: str, endpoint: str) -> str:
    """
    兼容两种 base_url 写法：
    1. https://api.xxx.com
    2. https://api.xxx.com/v1

    endpoint 示例：
    /v1/chat/completions
    /v1/messages
    """
    base = (base_url or "").strip().rstrip("/")
    ep = endpoint if endpoint.startswith("/") else "/" + endpoint

    if base.endswith("/v1") and ep.startswith("/v1/"):
        return base + ep[3:]

    return base + ep


def stream_direct_api_text(
    prompt: str,
    api_base_url: str,
    api_auth_token: str,
    api_model: str = DEFAULT_MODEL,
    api_protocol: str = "",
    system_prompt: str = "",
    use_web_search: bool = False,
):
    """
    第三方 API 直连真流式。
    - GPT/OpenAI兼容模型：走 /v1/chat/completions stream
    - Claude/Anthropic兼容模型：走 /v1/messages stream
    注意：这条路径不提供本地文件系统控制能力。
    """
    import urllib.request
    import urllib.error

    base_url = api_base_url.strip().rstrip("/")
    token = api_auth_token.strip()
    model = (api_model or DEFAULT_MODEL).strip()

    if not base_url:
        yield "直连模式缺少 API URL"
        return
    if not token:
        yield "直连模式缺少 API Key"
        return

    protocol = _normalize_protocol(api_protocol, model)
    lower_model = model.lower()

    if protocol == "responses":
        try:
            yield from stream_direct_responses_api_text(
                prompt,
                base_url,
                token,
                api_model=model,
                system_prompt=system_prompt,
                max_output_tokens=4096,
                use_web_search=use_web_search,
            )
        except Exception as e:
            yield "\n[直连Responses接口失败]\n" + str(e)
        return

    # OpenAI-compatible / GPT-compatible
    if protocol == "completions" or lower_model.startswith("gpt") or "gpt-" in lower_model:
        url = build_api_url(base_url, "/v1/chat/completions")
        system_text = _split_system_prompt(system_prompt)
        messages = []
        if system_text:
            messages.append({"role": "system", "content": system_text})
        messages.append({"role": "user", "content": prompt})
        body = {
            "model": model,
            "messages": messages,
            "temperature": MODEL_TEMPERATURE,
            "stream": True,
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream, application/json, text/plain, */*",
            "Authorization": "Bearer " + token,
            "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
        }

        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        try:
            with resilient_urlopen(req, timeout=STREAM_REQUEST_TIMEOUT) as resp:
                for raw in resp:
                    line = raw.decode("utf-8", errors="ignore").strip()
                    if not line:
                        continue
                    if not line.startswith("data:"):
                        continue

                    data = line[5:].strip()
                    if data == "[DONE]":
                        break

                    try:
                        obj = json.loads(data)
                        delta = (
                            obj.get("choices", [{}])[0]
                            .get("delta", {})
                            .get("content", "")
                        )
                        if delta:
                            yield delta
                    except Exception:
                        continue

        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="ignore")
            yield (
                "\n[直连OpenAI流式接口失败]\n"
                f"HTTP {getattr(e, 'code', '')} {getattr(e, 'reason', '')}\n"
                f"请求地址: {url}\n"
                + (err or str(e))
            )
        except Exception as e:
            if _is_transient_connection_error(e):
                try:
                    fallback = call_direct_text_api(
                        prompt,
                        api_base_url,
                        api_auth_token,
                        api_model=api_model,
                        api_protocol=api_protocol,
                        system_prompt=system_prompt,
                        max_tokens=4096,
                        temperature=MODEL_TEMPERATURE,
                    )
                    if fallback:
                        yield fallback
                        return
                except Exception as fallback_exc:
                    e = fallback_exc
            hint = ""
            if _is_dns_resolution_error(e):
                parsed = urlparse(url)
                ips = resolve_hostname_resilient(parsed.hostname or "")
                hint = "\nDNS解析失败，已尝试系统DNS和备用DNS回退。"
                if ips:
                    hint += " 备用解析结果: " + ", ".join(ips)
            yield (
                "\n[直连OpenAI流式接口失败]\n"
                f"请求地址: {url}\n"
                + str(e)
                + hint
            )
        return

    # Anthropic-compatible / Claude-compatible
    url = build_api_url(base_url, "/v1/messages")
    system_text = _split_system_prompt(system_prompt)
    messages = [{"role": "user", "content": prompt}]
    body = {
        "model": model,
        "max_tokens": 4096,
        "temperature": MODEL_TEMPERATURE,
        "messages": messages,
        "stream": True,
    }
    _add_anthropic_system_prompt(body, system_text)
    _add_default_claude_web_search_tool(body)
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream, application/json, text/plain, */*",
        "x-api-key": token,
        "Authorization": "Bearer " + token,
        "anthropic-version": "2023-06-01",
        "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    try:
        with resilient_urlopen(req, timeout=STREAM_REQUEST_TIMEOUT) as resp:
            for raw in resp:
                line = raw.decode("utf-8", errors="ignore").strip()
                if not line:
                    continue

                if not line.startswith("data:"):
                    continue

                data = line[5:].strip()
                if data == "[DONE]":
                    break

                try:
                    obj = json.loads(data)
                    typ = obj.get("type")

                    # Anthropic 标准流式文本增量
                    if typ == "content_block_delta":
                        delta = obj.get("delta", {})
                        text = delta.get("text", "")
                        if text:
                            yield text

                    # 兼容某些代理把文本放在 completion/content 里
                    elif "completion" in obj:
                        text = obj.get("completion") or ""
                        if text:
                            yield text

                except Exception:
                    continue

    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="ignore")
        yield (
            "\n[直连Claude流式接口失败]\n"
            f"HTTP {getattr(e, 'code', '')} {getattr(e, 'reason', '')}\n"
            f"请求地址: {url}\n"
            + (err or str(e))
        )
    except Exception as e:
        if _is_transient_connection_error(e):
            try:
                fallback = call_direct_text_api(
                    prompt,
                    api_base_url,
                    api_auth_token,
                    api_model=api_model,
                    api_protocol=api_protocol,
                    system_prompt=system_prompt,
                    max_tokens=4096,
                    temperature=MODEL_TEMPERATURE,
                )
                if fallback:
                    yield fallback
                    return
            except Exception as fallback_exc:
                e = fallback_exc
        hint = ""
        if _is_dns_resolution_error(e):
            parsed = urlparse(url)
            ips = resolve_hostname_resilient(parsed.hostname or "")
            hint = "\nDNS解析失败，已尝试系统DNS和备用DNS回退。"
            if ips:
                hint += " 备用解析结果: " + ", ".join(ips)
        yield (
            "\n[直连Claude流式接口失败]\n"
            f"请求地址: {url}\n"
            + str(e)
            + hint
        )


def stream_direct_and_save(
    conversation_id: str,
    final_prompt: str,
    api_base_url: str,
    api_auth_token: str,
    api_model: str,
    api_protocol: str = "",
    provider_name: str = "",
    system_prompt: str = "",
    sources: str = "",
    use_web_search: bool = False,
):
    full = ""
    for chunk in stream_direct_api_text(
        final_prompt,
        api_base_url,
        api_auth_token,
        api_model,
        api_protocol,
        system_prompt,
        use_web_search,
    ):
        full += chunk
        yield chunk

    token_count = estimate_round_tokens(final_prompt, full)

    db_add_message(
        conversation_id,
        "assistant",
        full,
        model=api_model,
        provider_name=(provider_name or "") + "｜直连流式",
        token_count=token_count,
        sources=sources,
    )


def stream_direct_vision_and_save(
    conversation_id: str,
    vision_prompt: str,
    image_local_paths: list[str],
    api_base_url: str,
    api_auth_token: str,
    api_model: str,
    provider_name: str = "",
    system_prompt: str = "",
):
    """
    流式视觉 API 调用：先调用同步视觉 API，然后分块 yield 输出，模拟流式效果
    """
    try:
        answer = call_direct_vision_api(
            vision_prompt,
            image_local_paths,
            api_base_url,
            api_auth_token,
            api_model,
            system_prompt=system_prompt,
        )

        # 分块输出，模拟流式效果
        chunk_size = 20
        for i in range(0, len(answer), chunk_size):
            yield answer[i:i + chunk_size]

        token_count = estimate_round_tokens(vision_prompt, answer, image_count=len(image_local_paths))

        db_add_message(
            conversation_id,
            "assistant",
            answer,
            model=api_model,
            provider_name=provider_name,
            token_count=token_count,
        )
    except Exception as e:
        error_msg = (
            "【视觉接口调用失败】\n\n"
            + str(e)
            + "\n\n这说明当前接入商或模型可能不支持图片视觉输入，"
            + "或者它的视觉接口格式不是 OpenAI/Anthropic 标准格式。"
        )
        yield error_msg

        db_add_message(
            conversation_id,
            "assistant",
            error_msg,
            model=api_model,
            provider_name=provider_name,
        )


def build_vision_payload_parts(prompt: str, image_local_paths: list[str]):
    if not image_local_paths:
        raise RuntimeError("没有图片")

    images = []
    for local_path in image_local_paths:
        abs_path = local_upload_path_to_abs(local_path)
        if not abs_path.exists():
            raise RuntimeError(f"图片不存在: {local_path}")

        raw = abs_path.read_bytes()
        b64 = base64.b64encode(raw).decode("utf-8")
        media_type = guess_media_type(local_path)
        images.append({
            "local_path": local_path,
            "media_type": media_type,
            "base64": b64,
        })

    openai_content = [{"type": "text", "text": prompt}]
    anthropic_content = [{"type": "text", "text": prompt}]

    for img in images:
        openai_content.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:{img['media_type']};base64,{img['base64']}"
            }
        })
        anthropic_content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": img["media_type"],
                "data": img["base64"],
            }
        })

    return openai_content, anthropic_content


def guess_file_media_type(path: str) -> str:
    guessed, _ = mimetypes.guess_type(path)
    return guessed or "application/octet-stream"


def _file_data_url(local_path: str, media_type: str = "") -> str:
    abs_path = local_upload_path_to_abs(local_path)
    if not abs_path.exists():
        raise RuntimeError(f"文件不存在: {local_path}")
    raw = abs_path.read_bytes()
    b64 = base64.b64encode(raw).decode("utf-8")
    return f"data:{media_type or guess_file_media_type(local_path)};base64,{b64}"


def build_responses_input_payload(
    prompt: str,
    image_local_paths: list[str] | None = None,
    file_items: list[dict] | None = None,
):
    content = [{
        "type": "input_text",
        "text": prompt,
    }]

    for local_path in image_local_paths or []:
        media_type = guess_media_type(local_path)
        content.append({
            "type": "input_image",
            "detail": "auto",
            "image_url": _file_data_url(local_path, media_type),
        })

    for item in file_items or []:
        local_path = item.get("local_path") if isinstance(item, dict) else ""
        if not local_path:
            continue
        filename = item.get("name") or Path(local_path).name
        content.append({
            "type": "input_file",
            "filename": filename,
            "file_data": _file_data_url(local_path),
        })

    return [{
        "type": "message",
        "role": "user",
        "content": content,
    }]


def stream_direct_vision_api_text(
    prompt: str,
    image_local_paths: list[str],
    api_base_url: str,
    api_auth_token: str,
    api_model: str,
    api_protocol: str = "",
    system_prompt: str = "",
    file_items: list[dict] | None = None,
    use_web_search: bool = False,
):
    import urllib.request
    import urllib.error

    base_url = api_base_url.strip().rstrip("/")
    token = api_auth_token.strip()
    model = api_model.strip() or DEFAULT_MODEL

    if not base_url:
        yield "缺少 API URL"
        return
    if not token:
        yield "缺少 API Key"
        return

    openai_content, anthropic_content = build_vision_payload_parts(prompt, image_local_paths)
    system_text = _split_system_prompt(system_prompt)
    protocol = _normalize_protocol(api_protocol, model)
    lower_model = model.lower()

    if protocol == "responses":
        try:
            input_payload = build_responses_input_payload(
                prompt,
                image_local_paths=image_local_paths,
                file_items=file_items,
            )
            yield from stream_direct_responses_api_text(
                prompt,
                base_url,
                token,
                api_model=model,
                system_prompt=system_prompt,
                max_output_tokens=4096,
                use_web_search=use_web_search,
                input_payload=input_payload,
            )
        except Exception as e:
            yield "\n[Responses视觉/文件流式接口失败]\n" + str(e)
        return

    if protocol == "completions" or lower_model.startswith("gpt") or "gpt-" in lower_model:
        url = build_api_url(base_url, "/v1/chat/completions")
        messages = []
        if system_text:
            messages.append({"role": "system", "content": system_text})
        messages.append({"role": "user", "content": openai_content})
        body = {
            "model": model,
            "messages": messages,
            "temperature": MODEL_TEMPERATURE,
            "stream": True,
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream, application/json, text/plain, */*",
                "Authorization": "Bearer " + token,
            },
            method="POST",
        )

        try:
            with resilient_urlopen(req, timeout=STREAM_REQUEST_TIMEOUT) as resp:
                for raw in resp:
                    line = raw.decode("utf-8", errors="ignore").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                        delta = obj.get("choices", [{}])[0].get("delta", {}).get("content", "")
                        if delta:
                            yield delta
                    except Exception:
                        continue
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="ignore")
            yield "\n[视觉流式接口失败]\n" + (err or str(e))
        except Exception as e:
            yield "\n[视觉流式接口失败]\n" + str(e)
        return

    url = build_api_url(base_url, "/v1/messages")
    messages = [{"role": "user", "content": anthropic_content}]
    body = {
        "model": model,
        "max_tokens": 4096,
        "temperature": MODEL_TEMPERATURE,
        "messages": messages,
        "stream": True,
    }
    _add_anthropic_system_prompt(body, system_text)
    _add_default_claude_web_search_tool(body)
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream, application/json, text/plain, */*",
            "x-api-key": token,
            "Authorization": "Bearer " + token,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )

    try:
        with resilient_urlopen(req, timeout=STREAM_REQUEST_TIMEOUT) as resp:
            for raw in resp:
                line = raw.decode("utf-8", errors="ignore").strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                    if obj.get("type") == "content_block_delta":
                        text = obj.get("delta", {}).get("text", "")
                        if text:
                            yield text
                    elif "completion" in obj:
                        text = obj.get("completion") or ""
                        if text:
                            yield text
                except Exception:
                    continue
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="ignore")
        yield "\n[视觉流式接口失败]\n" + (err or str(e))
    except Exception as e:
        yield "\n[视觉流式接口失败]\n" + str(e)


def guess_media_type(path: str) -> str:
    ext = Path(path).suffix.lower()
    if ext in [".jpg", ".jpeg"]:
        return "image/jpeg"
    if ext == ".png":
        return "image/png"
    if ext == ".webp":
        return "image/webp"
    if ext == ".gif":
        return "image/gif"
    return "image/jpeg"


def local_upload_path_to_abs(local_path: str) -> Path:
    # local_path like ./uploads/xxx.jpg
    clean = local_path.replace("./", "", 1)
    return BASE_DIR / clean


def call_direct_vision_api(
    prompt: str,
    image_local_paths: list[str],
    api_base_url: str,
    api_auth_token: str,
    api_model: str,
    api_protocol: str = "",
    system_prompt: str = "",
) -> str:
    """
    真正把图片作为 base64 视觉输入发给 API。
    GPT 模型走 OpenAI chat.completions。
    Claude 模型走 Anthropic messages。
    """
    import base64
    import urllib.request
    import urllib.error

    base_url = api_base_url.strip().rstrip("/")
    token = api_auth_token.strip()
    model = api_model.strip() or DEFAULT_MODEL

    if not base_url:
        raise RuntimeError("缺少 API URL")
    if not token:
        raise RuntimeError("缺少 API Key")
    openai_content, anthropic_content = build_vision_payload_parts(prompt, image_local_paths)
    system_text = _split_system_prompt(system_prompt)

    protocol = _normalize_protocol(api_protocol, model)
    lower_model = model.lower()

    if protocol == "responses":
        input_payload = build_responses_input_payload(prompt, image_local_paths=image_local_paths)
        return call_direct_responses_api(
            prompt,
            base_url,
            token,
            api_model=model,
            system_prompt=system_prompt,
            max_output_tokens=4096,
            input_payload=input_payload,
        )

    # GPT / OpenAI compatible
    if protocol == "completions" or lower_model.startswith("gpt") or "gpt-" in lower_model:
        messages = []
        if system_text:
            messages.append({"role": "system", "content": system_text})
        messages.append({"role": "user", "content": openai_content})
        body = {
            "model": model,
            "messages": messages,
            "temperature": MODEL_TEMPERATURE
        }

        url = base_url + "/v1/chat/completions"

        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + token,
            },
            method="POST",
        )

        try:
            with resilient_urlopen(req, timeout=VISION_STREAM_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="ignore"))

            return (
                data.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                    .strip()
            ) or "(视觉接口无输出)"

        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="ignore")
            raise RuntimeError("OpenAI视觉接口失败: " + (err or str(e)))

    # Anthropic compatible
    messages = [{"role": "user", "content": anthropic_content}]
    body = {
        "model": model,
        "max_tokens": 4096,
        "temperature": MODEL_TEMPERATURE,
        "messages": messages
    }
    _add_anthropic_system_prompt(body, system_text)
    _add_default_claude_web_search_tool(body)

    url = base_url + "/v1/messages"

    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-api-key": token,
            "Authorization": "Bearer " + token,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )

    try:
        with resilient_urlopen(req, timeout=VISION_STREAM_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))

        parts = []
        for item in data.get("content", []):
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))

        return "\\n".join(parts).strip() or "(视觉接口无输出)"

    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError("Anthropic视觉接口失败: " + (err or str(e)))


async def read_uploaded_text(file: UploadFile) -> str:
    raw = await file.read()
    if len(raw) > 1024 * 1024:
        raw = raw[:1024 * 1024]
    text = raw.decode("utf-8", errors="ignore").strip()
    return text or "(文件为空，或不是可直接按 UTF-8 读取的文本文件)"


def load_uploaded_text_from_path(local_path: str) -> str:
    abs_path = local_upload_path_to_abs(local_path)
    try:
        raw = abs_path.read_bytes()
    except Exception:
        return ""

    if len(raw) > 1024 * 1024:
        raw = raw[:1024 * 1024]

    return raw.decode("utf-8", errors="ignore").strip()


def is_responses_native_document(filename: str) -> bool:
    suffix = Path(filename or "").suffix.lower()
    return suffix in {
        ".pdf",
        ".doc",
        ".docx",
        ".ppt",
        ".pptx",
        ".xls",
        ".xlsx",
        ".odt",
        ".ods",
        ".odp",
        ".rtf",
    }


async def save_uploaded_file_dual_paths(file: UploadFile) -> tuple[str, str, str]:
    """
    返回:
    - original_name: 原文件名
    - local_path: 给后端读取上传文件的本地相对路径 ./uploads/xxx
    - web_path: 给浏览器显示的 URL 路径 /uploads/xxx
    """
    original_name = file.filename or "uploaded_file"
    suffix = Path(original_name).suffix or ".bin"
    safe_name = f"{uuid.uuid4().hex}{suffix}"
    save_path = UPLOAD_DIR / safe_name

    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise RuntimeError(f"文件过大，当前限制为 {MAX_UPLOAD_BYTES // 1024 // 1024}MB")
    if is_image_file(original_name) and len(raw) > MAX_IMAGE_UPLOAD_BYTES:
        raise RuntimeError(f"图片过大，当前限制为 {MAX_IMAGE_UPLOAD_BYTES // 1024 // 1024}MB。请先压缩图片后再上传。")
    save_path.write_bytes(raw)

    local_path = f"./uploads/{safe_name}"
    web_path = f"/uploads/{safe_name}"

    return original_name, local_path, web_path


async def save_uploaded_file(file: UploadFile) -> tuple[str, str]:
    original_name = file.filename or "uploaded_file"
    suffix = Path(original_name).suffix
    safe_name = f"{uuid.uuid4().hex}{suffix}"
    save_path = UPLOAD_DIR / safe_name

    raw = await file.read()
    save_path.write_bytes(raw)

    rel_path = f"./uploads/{safe_name}"
    return original_name, rel_path
