from pathlib import Path
import json
import os
import uuid
import re
import base64
from urllib.parse import quote, urlparse, parse_qs, unquote

from fastapi import UploadFile

from chat_utils import estimate_round_tokens
from config import BASE_DIR, DEFAULT_MODEL, UPLOAD_DIR
from db import db_add_message


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
        "search ", "google ", "look up", "browse", "web search", "latest"
    ]
    return any(p in value for p in patterns)


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


def fetch_search_results(query: str, max_results: int = 5) -> list[dict]:
    import urllib.request
    from html.parser import HTMLParser

    class BingParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.results = []
            self._in_link = False
            self._href = ""
            self._text_parts = []
            self._li_depth = 0
            self._h2_depth = 0

        def handle_starttag(self, tag, attrs):
            tag = tag.lower()
            attrs_dict = dict(attrs)
            if tag == "li":
                cls = attrs_dict.get("class", "")
                if "b_algo" in cls:
                    self._li_depth += 1
                return
            if tag == "h2" and self._li_depth > 0:
                self._h2_depth += 1
                return
            if tag != "a":
                return
            href = attrs_dict.get("href", "")
            if self._li_depth > 0 and self._h2_depth > 0 and href.startswith("http"):
                self._in_link = True
                self._href = href
                self._text_parts = []

        def handle_endtag(self, tag):
            tag = tag.lower()
            if tag == "a" and self._in_link:
                title = normalize_source_title("".join(self._text_parts), self._href)
                url = normalize_source_url(self._href)
                if url:
                    self.results.append({"title": title, "url": url})
                self._in_link = False
                self._href = ""
                self._text_parts = []
                return
            if tag == "h2" and self._h2_depth > 0:
                self._h2_depth -= 1
                return
            if tag == "li" and self._li_depth > 0:
                self._li_depth -= 1

        def handle_data(self, data):
            if self._in_link and data:
                self._text_parts.append(data)

    query = rewrite_search_query(query)
    if not query:
        return []

    api_results = fetch_brave_search_results(query, max_results=max_results)
    if api_results:
        return api_results

    candidates = [
        "https://www.bing.com/search?q=" + quote(query) + "&setlang=zh-Hans&ensearch=1",
        "https://cn.bing.com/search?q=" + quote(query),
        "https://html.duckduckgo.com/html/?q=" + quote(query),
    ]

    for url in candidates:
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 Claude-Web/1.0",
                    "Accept": "text/html,application/xhtml+xml",
                },
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=16) as resp:
                html = resp.read(1024 * 512).decode("utf-8", errors="ignore")
            parser = BingParser()
            parser.feed(html)
            cleaned = []
            seen = set()
            for item in parser.results:
                href = normalize_source_url(item.get("url", ""))
                if not href or href in seen:
                    continue
                seen.add(href)
                cleaned.append({
                    "title": normalize_source_title(item.get("title", ""), href),
                    "url": href,
                    "score": source_relevance_score(query, item.get("title", ""), href),
                })
            cleaned.sort(key=lambda x: x.get("score", 0), reverse=True)
            filtered = [item for item in cleaned if item.get("score", 0) >= 8][:max_results]
            if filtered:
                return filtered
        except Exception:
            continue

    return []


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


def build_sources_context_block(sources: list[dict]) -> str:
    if not sources:
        return ""

    parts = [
        "系统已经在后端完成了实时联网搜索/网页读取。以下内容就是本次实时联网检索到的来源摘录。",
        "回答时不得说“我不能联网”“我无法实时搜索”“截至我可用信息范围”等模板话；你应当基于下面来源作答。",
        "如果来源不足以证明用户问题，请明确说“本次联网搜索没有找到足够可靠的来源证明……”，并说明哪些来源是第三方报道、哪些是官方来源。",
        "如果答案引用了这些来源，请在相关句子后使用 [1]、[2] 这种编号引用。",
        "不要编造不存在的来源编号。",
        "",
    ]

    for item in sources:
        idx = item.get("index")
        title = item.get("title", "未命名来源")
        url = item.get("url", "")
        excerpt = item.get("excerpt", "")
        parts.append(f"[{idx}] {title}")
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


def collect_search_sources(user_prompt: str, max_results: int = 4) -> list[dict]:
    urls = extract_urls_from_text(user_prompt, max_urls=max_results)
    results = []

    lowered_prompt = (user_prompt or "").lower()
    if "openai" in lowered_prompt or "gpt" in lowered_prompt:
        for url in [
            "https://openai.com/news/",
            "https://help.openai.com/en/articles/9624314-model-release-notes",
        ]:
            if len(results) >= max_results:
                break
            excerpt = extract_webpage_via_api(url, max_chars=1400)
            results.append({
                "title": normalize_source_title("", url),
                "url": url,
                "excerpt": excerpt,
                "score": source_relevance_score(user_prompt, "", url, excerpt) + 12,
            })

    for url in urls:
        excerpt = extract_webpage_via_api(url, max_chars=2200)
        results.append({
            "title": normalize_source_title("", url),
            "url": url,
            "excerpt": excerpt,
        })

    if len(results) < max_results and looks_like_search_request(user_prompt):
        search_results = fetch_search_results(user_prompt, max_results=max_results)
        for item in search_results[:2]:
            if any(existing["url"] == item["url"] for existing in results):
                continue
            excerpt = item.get("description") or ""
            if not excerpt:
                excerpt = extract_webpage_via_api(item["url"], max_chars=1400)
            score = source_relevance_score(user_prompt, item["title"], item["url"], excerpt)
            if score < 8:
                continue
            results.append({
                "title": item["title"],
                "url": item["url"],
                "excerpt": excerpt,
                "score": score,
            })
            if len(results) >= max_results:
                break

    results.sort(key=lambda x: x.get("score", source_relevance_score(user_prompt, x.get("title", ""), x.get("url", ""), x.get("excerpt", ""))), reverse=True)
    final = []
    for idx, item in enumerate(results, 1):
        final.append({
            "index": idx,
            "title": normalize_source_title(item.get("title", ""), item.get("url", "")),
            "url": item.get("url", ""),
            "excerpt": item.get("excerpt", ""),
        })
    return final


def search_plan_for_prompt(user_prompt: str) -> dict:
    urls = extract_urls_from_text(user_prompt, max_urls=4)
    return {
        "has_urls": bool(urls),
        "should_search": looks_like_search_request(user_prompt),
        "urls": urls,
    }


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
            "temperature": 0.3,
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
            with urllib.request.urlopen(req, timeout=300) as resp:
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
            yield (
                "\n[直连OpenAI流式接口失败]\n"
                f"请求地址: {url}\n"
                + str(e)
            )

        return

    # Anthropic-compatible / Claude-compatible
    url = build_api_url(base_url, "/v1/messages")
    body = {
        "model": model,
        "max_tokens": 4096,
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
        with urllib.request.urlopen(req, timeout=300) as resp:
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
        yield (
            "\n[直连Claude流式接口失败]\n"
            f"请求地址: {url}\n"
            + str(e)
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

    lower_model = model.lower()

    # GPT / OpenAI compatible
    if lower_model.startswith("gpt") or "gpt-" in lower_model:
        content = [{"type": "text", "text": prompt}]

        for img in images:
            content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:{img['media_type']};base64,{img['base64']}"
                }
            })

        body = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": content
                }
            ],
            "temperature": 0.3
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
            with urllib.request.urlopen(req, timeout=180) as resp:
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
    content = [{"type": "text", "text": prompt}]

    for img in images:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": img["media_type"],
                "data": img["base64"],
            }
        })

    body = {
        "model": model,
        "max_tokens": 4096,
        "messages": [
            {
                "role": "user",
                "content": content,
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
        with urllib.request.urlopen(req, timeout=180) as resp:
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
