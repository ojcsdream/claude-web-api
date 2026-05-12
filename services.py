from pathlib import Path
from contextlib import contextmanager
import json
import os
import uuid
import re
import base64
import time
import html
from urllib.parse import quote, urlparse, parse_qs, unquote

from fastapi import UploadFile

from chat_utils import estimate_round_tokens, is_image_file
from config import BASE_DIR, DEFAULT_MODEL, MAX_IMAGE_UPLOAD_BYTES, MAX_UPLOAD_BYTES, MODEL_TEMPERATURE, UPLOAD_DIR
from db import db_add_message


DNS_CACHE = {}
SEARCH_CACHE = {}
PLANNER_CACHE = {}
PAGE_CACHE = {}


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


def resilient_urlopen(req, timeout=300):
    import time
    import urllib.request

    parsed = urlparse(req.full_url)
    host = parsed.hostname or ""
    ips = resolve_hostname_resilient(host)
    last_exc = None

    for attempt in range(3):
        try:
            if ips:
                with patched_getaddrinfo_for_host(host, ips):
                    return urllib.request.urlopen(req, timeout=timeout)
            return urllib.request.urlopen(req, timeout=timeout)
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
    urls = extract_urls_from_text(user_prompt)
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
    value = re.sub(r"^(联网)?(搜索|搜一下|搜一搜|查一下|查一查|查找|检索|搜)\s*", "", value).strip()
    value = re.sub(r"^(search|look up|browse|web search)\s+", "", value, flags=re.I).strip()
    value = re.sub(r"(一下|看看|查查|搜搜|相关信息|最新消息|最新新闻)[。.!！\s]*$", "", value).strip()
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


def build_contextual_search_query(user_prompt: str, context_messages=None, max_chars: int = 120) -> str:
    prompt = (user_prompt or "").strip()
    cleaned_prompt = _strip_search_command_words(prompt)

    if cleaned_prompt and not _is_bare_search_command(prompt):
        return cleaned_prompt[:max_chars]

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
                return _strip_search_command_words(content)[:max_chars]
        return candidates[0][:max_chars]

    return cleaned_prompt[:max_chars] if cleaned_prompt else prompt[:max_chars]


BAD_SOURCE_PATTERNS = [
    "zhihu.com",
    "baidu.com/jingyan",
    "baijiahao.baidu.com",
    "tieba.baidu.com",
    "microsoft.com/store",
    "apps.microsoft.com",
    "tomato",
    "fanqie",
    "小说",
    "smapply.org",
    "open-openai.com",
]

PREFERRED_SOURCE_PATTERNS = [
    "openai.com",
    "help.openai.com",
    "platform.openai.com",
    "github.com/openai",
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
    if "gpt-5.5" in lowered:
        return "OpenAI GPT-5.5 latest news"
    if "openai" in lowered and ("最新" in q or "latest" in lowered or "news" in lowered):
        return "OpenAI latest news"
    return q


def source_relevance_score(query: str, title: str, url: str, excerpt: str = "") -> int:
    haystack = " ".join([title or "", url or "", excerpt or ""]).lower()
    score = 0
    query_terms = extract_query_terms(rewrite_search_query(query))

    for term in query_terms:
      if term in haystack:
          score += 6

    for pattern in PREFERRED_SOURCE_PATTERNS:
        if pattern in (url or "").lower():
            score += 10

    for pattern in BAD_SOURCE_PATTERNS:
        if pattern in (url or "").lower() or pattern in (title or "").lower():
            score -= 18

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


def fetch_searchfree_results(query: str, max_results: int = 5) -> list[dict]:
    import urllib.request
    import urllib.error

    q = (query or "").strip()
    if not q:
        return []

    payload = json.dumps({
        "query": q,
        "search_depth": "advanced",
        "max_results": max(1, min(int(max_results or 5), 10)),
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://searchfree.site/api/search",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Claude-Web/1.0",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
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
        excerpt = (item.get("content") or item.get("description") or "").strip()
        cleaned.append({
            "title": title,
            "url": href,
            "description": excerpt[:2200],
            "score": source_relevance_score(q, title, href, excerpt) + 14,
            "provider": "searchfree",
        })
        if len(cleaned) >= max_results:
            break

    cleaned.sort(key=lambda x: x.get("score", 0), reverse=True)
    return cleaned[:max_results]


def fetch_bing_search_results(query: str, max_results: int = 5) -> list[dict]:
    import urllib.request

    q = rewrite_search_query((query or "").strip())
    if not q:
        return []

    url = "https://www.bing.com/search?q=" + quote(q) + "&setlang=en-US&cc=us"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        },
        method="GET",
    )

    try:
        with resilient_urlopen(req, timeout=16) as resp:
            raw_html = resp.read(1024 * 1024).decode("utf-8", errors="ignore")
    except Exception:
        return []

    items = []
    seen = set()
    pattern = re.compile(
        r'<li class="b_algo".*?<h2><a href="(?P<url>[^"]+)".*?>(?P<title>.*?)</a></h2>.*?(?:<p>(?P<desc>.*?)</p>)?',
        re.I | re.S,
    )

    for match in pattern.finditer(raw_html):
        href = normalize_source_url(match.group("url") or "")
        if not href or href in seen:
            continue
        seen.add(href)
        title = normalize_source_title(_strip_html_tags(match.group("title") or ""), href)
        desc = _trim_excerpt(_strip_html_tags(match.group("desc") or ""), 1200)
        score = source_relevance_score(q, title, href, desc) + 6
        if score < 4:
            continue
        items.append({
            "title": title,
            "url": href,
            "description": desc,
            "score": score,
            "provider": "bing",
        })
        if len(items) >= max(3, max_results * 2):
            break

    items.sort(key=lambda x: x.get("score", 0), reverse=True)
    return items[:max_results]


def fetch_search1api_results(query: str, max_results: int = 5) -> list[dict]:
    import urllib.request

    token = os.environ.get("SEARCH1API_KEY", "").strip()
    if not token:
        return []

    q = (query or "").strip()
    if not q:
        return []

    payload = json.dumps({
        "query": q,
        "search_service": "google",
        "max_results": max(1, min(int(max_results or 5), 10)),
        "crawl_results": 0,
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.search1api.com/search",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": "Bearer " + token,
            "User-Agent": "Claude-Web/1.0",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
    except Exception:
        return []

    items = data.get("results") or data.get("data") or []
    cleaned = []
    seen = set()

    for item in items:
        href = normalize_source_url(item.get("link") or item.get("url") or "")
        if not href or href in seen:
            continue
        seen.add(href)
        title = normalize_source_title(item.get("title", ""), href)
        excerpt = (item.get("content") or item.get("snippet") or item.get("description") or "").strip()
        cleaned.append({
            "title": title,
            "url": href,
            "description": excerpt[:2200],
            "score": source_relevance_score(q, title, href, excerpt) + 10,
            "provider": "search1api",
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
    return fetch_search1api_results(query, max_results=max_results)


def select_balanced_sources(results: list[dict], max_results: int) -> list[dict]:
    if not results:
        return []

    limit = max(1, int(max_results or 4))
    sorted_results = sorted(
        results,
        key=lambda x: x.get("score", source_relevance_score("", x.get("title", ""), x.get("url", ""), x.get("excerpt", ""))),
        reverse=True,
    )

    selected = []
    selected_urls = set()

    def add_first(predicate):
        if len(selected) >= limit:
            return
        for item in sorted_results:
            url = item.get("url", "")
            if not url or url in selected_urls:
                continue
            if predicate(item):
                selected.append(item)
                selected_urls.add(url)
                return

    add_first(lambda item: not item.get("provider"))
    add_first(lambda item: item.get("provider") == "search1api")
    add_first(lambda item: item.get("provider") == "searchfree")

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
    groups = [
        fetch_search1api_results(q, max_results=limit),
        fetch_bing_search_results(q, max_results=limit),
        fetch_brave_search_results(q, max_results=limit),
        fetch_searchfree_results(q, max_results=limit),
    ]

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


def fetch_brave_search_results(query: str, max_results: int = 5) -> list[dict]:
    import urllib.request

    token = os.environ.get("BRAVE_SEARCH_API_KEY", "").strip()
    if not token:
        return []

    query = rewrite_search_query(query)
    if not query:
        return []

    url = (
        "https://api.search.brave.com/res/v1/web/search?q="
        + quote(query)
        + "&count="
        + str(max(1, min(max_results, 10)))
        + "&search_lang=en&country=us&spellcheck=1&text_decorations=0"
    )

    try:
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "X-Subscription-Token": token,
            },
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
        web_results = ((data.get("web") or {}).get("results") or [])
        cleaned = []
        seen = set()
        for item in web_results:
            href = normalize_source_url(item.get("url", ""))
            if not href or href in seen:
                continue
            seen.add(href)
            title = normalize_source_title(item.get("title", ""), href)
            desc = item.get("description", "") or ""
            score = source_relevance_score(query, title, href, desc)
            if score < 8:
                continue
            cleaned.append({
                "title": title,
                "url": href,
                "description": desc,
                "score": score,
                "provider": "brave",
            })
        cleaned.sort(key=lambda x: x.get("score", 0), reverse=True)
        return cleaned[:max_results]
    except Exception:
        return []


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
    search_results = fetch_search1api_results(search_query, max_results=max_results)
    for item in search_results:
        excerpt = item.get("description") or ""
        score = item.get("score", source_relevance_score(user_prompt, item["title"], item["url"], excerpt))
        results.append({
            "title": item["title"],
            "url": item["url"],
            "excerpt": excerpt,
            "score": score,
            "provider": "search1api",
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


def call_direct_text_api(
    prompt: str,
    api_base_url: str,
    api_auth_token: str,
    api_model: str = DEFAULT_MODEL,
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

    lower_model = model.lower()

    if lower_model.startswith("gpt") or "gpt-" in lower_model:
        url = build_api_url(base_url, "/v1/chat/completions")
        body = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
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

    url = build_api_url(base_url, "/v1/messages")
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }
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


def plan_search_actions(
    user_prompt: str,
    context_messages=None,
    api_base_url: str = "",
    api_auth_token: str = "",
    api_model: str = DEFAULT_MODEL,
) -> dict:
    prompt = (user_prompt or "").strip()
    context_digest = _recent_context_digest(context_messages=context_messages)
    cache_key = json.dumps({
        "prompt": prompt[:600],
        "context": context_digest[:1200],
        "model": (api_model or DEFAULT_MODEL).strip(),
    }, ensure_ascii=False, sort_keys=True)
    cached = _cache_get(PLANNER_CACHE, cache_key)
    if cached is not None:
        return cached

    urls = extract_urls_from_text(prompt, max_urls=4)
    heuristic = {
        "should_search": bool(looks_like_search_request(prompt)),
        "search_queries": [build_contextual_search_query(prompt, context_messages=context_messages)] if prompt else [],
        "parse_links": urls[:2],
    }

    if not api_base_url.strip() or not api_auth_token.strip():
        return _cache_set(PLANNER_CACHE, cache_key, heuristic, ttl_seconds=180)

    planner_prompt = "\n".join([
        "你是一个搜索规划器。你不能直接回答用户问题，只能决定是否需要联网搜索和读取网页。",
        "请仅输出 JSON，不要输出解释、Markdown、代码块。",
        'JSON 格式: {"should_search": boolean, "search_queries": ["..."], "parse_links": ["https://..."]}',
        "规则：",
        "1. 只有当问题需要最新信息、外部事实核验、新闻、版本变化、网页内容时，should_search 才为 true。",
        "2. search_queries 最多 3 个，必须短、具体，优先英文检索词。",
        "3. parse_links 只填写用户消息里明确给出的 URL，最多 2 个。",
        "4. 如果问题可直接基于上下文回答，不要搜索。",
        "",
        "最近对话：",
        context_digest or "(无)",
        "",
        "当前用户问题：",
        prompt or "(空)",
    ])

    try:
        raw = call_direct_text_api(
            planner_prompt,
            api_base_url,
            api_auth_token,
            api_model,
            max_tokens=500,
            temperature=0,
        )
        obj = _extract_json_object(raw) or {}
        planned_queries = obj.get("search_queries")
        if not isinstance(planned_queries, list):
            planned_queries = []
        planned_links = obj.get("parse_links")
        if not isinstance(planned_links, list):
            planned_links = []
        result = {
            "should_search": bool(obj.get("should_search")) or bool(planned_queries),
            "search_queries": [str(x).strip()[:140] for x in planned_queries if str(x).strip()][:3],
            "parse_links": [str(x).strip() for x in planned_links if str(x).strip().startswith(("http://", "https://"))][:2],
        }
        if not result["search_queries"] and heuristic["search_queries"]:
            result["search_queries"] = heuristic["search_queries"][:1]
        if not result["parse_links"] and heuristic["parse_links"]:
            result["parse_links"] = heuristic["parse_links"][:2]
        if not result["should_search"] and result["parse_links"]:
            result["should_search"] = True
        return _cache_set(PLANNER_CACHE, cache_key, result, ttl_seconds=180)
    except Exception:
        return _cache_set(PLANNER_CACHE, cache_key, heuristic, ttl_seconds=180)


def compile_search_sources_from_queries(
    user_prompt: str,
    search_queries: list[str],
    parse_links: list[str] | None = None,
    max_results: int = 4,
) -> list[dict]:
    results = []
    seen = set()
    links = parse_links or []

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
                "query": query,
            })

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
            "query": item.get("query", ""),
        })
    return final


def enrich_sources_with_page_content(sources: list[dict], max_pages: int = 2, max_chars: int = 2200) -> list[dict]:
    enriched = []
    page_reads = 0
    for source in sources or []:
        item = dict(source)
        url = item.get("url", "")
        current_excerpt = (item.get("excerpt") or "").strip()
        should_read = page_reads < max_pages and url and (
            len(current_excerpt) < 220
            or item.get("provider") in ("direct-link", "bing")
        )
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
) -> tuple[list[dict], dict]:
    plan = plan_search_actions(
        user_prompt,
        context_messages=context_messages,
        api_base_url=api_base_url,
        api_auth_token=api_auth_token,
        api_model=api_model,
    )
    queries = [q for q in plan.get("search_queries", []) if q][:3]
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
    sources = enrich_sources_with_page_content(sources, max_pages=2, max_chars=2200)
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

    lower_model = model.lower()

    # OpenAI-compatible / GPT-compatible
    if lower_model.startswith("gpt") or "gpt-" in lower_model:
        url = build_api_url(base_url, "/v1/chat/completions")
        body = {
            "model": model,
            "messages": [
                {"role": "user", "content": prompt}
            ],
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
            with resilient_urlopen(req, timeout=300) as resp:
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
    body = {
        "model": model,
        "max_tokens": 4096,
        "temperature": MODEL_TEMPERATURE,
        "messages": [
            {"role": "user", "content": prompt}
        ],
        "stream": True,
    }
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
        with resilient_urlopen(req, timeout=300) as resp:
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
    provider_name: str = "",
    sources: str = "",
):
    full = ""
    for chunk in stream_direct_api_text(
        final_prompt,
        api_base_url,
        api_auth_token,
        api_model,
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


def stream_direct_vision_api_text(
    prompt: str,
    image_local_paths: list[str],
    api_base_url: str,
    api_auth_token: str,
    api_model: str,
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
    lower_model = model.lower()

    if lower_model.startswith("gpt") or "gpt-" in lower_model:
        url = build_api_url(base_url, "/v1/chat/completions")
        body = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": openai_content,
                }
            ],
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
            with resilient_urlopen(req, timeout=300) as resp:
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
    body = {
        "model": model,
        "max_tokens": 4096,
        "temperature": MODEL_TEMPERATURE,
        "messages": [
            {
                "role": "user",
                "content": anthropic_content,
            }
        ],
        "stream": True,
    }
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
        with resilient_urlopen(req, timeout=300) as resp:
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

    lower_model = model.lower()

    # GPT / OpenAI compatible
    if lower_model.startswith("gpt") or "gpt-" in lower_model:
        body = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": openai_content
                }
            ],
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
            with resilient_urlopen(req, timeout=180) as resp:
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
    body = {
        "model": model,
        "max_tokens": 4096,
        "temperature": MODEL_TEMPERATURE,
        "messages": [
            {
                "role": "user",
                "content": anthropic_content,
            }
        ]
    }

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
        with resilient_urlopen(req, timeout=180) as resp:
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
