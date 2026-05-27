(function () {
  "use strict";

  const FRAME_MS = 16;
  const DEFAULT_MIN_DELAY = 8;
  const DEFAULT_MAX_DELAY = 22;
  const STREAMING_TYPESAT_MIN_INTERVAL = 420;
  const STREAMING_TYPESAT_MIN_DELTA = 96;
  const FORCE_FULL_SYNC_MS = 900;
  const LONG_STREAM_TEXT_LENGTH = 99999;
  const LONG_STREAM_REVEAL_CHARS = 160;

  const HTML_VOID_TAGS = new Set([
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr"
  ]);
  const CODE_FENCE_RE = /^ {0,3}(```+|~~~+)/;
  const BLOCK_MATH_BEGIN_RE = /(?:^|\n)(\$\$|\\\[|\\begin\{(?:equation|align|aligned|gather|multline|cases|matrix|pmatrix|bmatrix|vmatrix)\*?\})/;
  const BLOCK_MATH_END_RE = /(\$\$|\\\]|\\end\{(?:equation|align|aligned|gather|multline|cases|matrix|pmatrix|bmatrix|vmatrix)\*?\})(?:\n|$)/;

  function escapeHtml(value) {
    return String(value || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function clamp(value, min, max) {
    return Math.max(min, Math.min(max, value));
  }

  function isWhitespace(ch) {
    return /\s/.test(ch || "");
  }

  function isAsciiWordChar(ch) {
    return /[A-Za-z0-9_]/.test(ch || "");
  }

  function isCjkChar(ch) {
    return /[\u3400-\u9fff\uf900-\ufaff]/.test(ch || "");
  }

  function isSoftPunctuation(ch) {
    return /[,:;，、：；]/.test(ch || "");
  }

  function isHardPunctuation(ch) {
    return /[.!?。！？]/.test(ch || "");
  }

  function getStepAndDelay(remaining, minDelay, maxDelay) {
    if (remaining > 2600) return { minStep: 14, maxStep: 28, cps: 1500, frameMs: minDelay };
    if (remaining > 1500) return { minStep: 12, maxStep: 24, cps: 1240, frameMs: minDelay + 1 };
    if (remaining > 900) return { minStep: 10, maxStep: 20, cps: 980, frameMs: minDelay + 2 };
    if (remaining > 480) return { minStep: 8, maxStep: 16, cps: 720, frameMs: minDelay + 4 };
    if (remaining > 220) return { minStep: 6, maxStep: 12, cps: 500, frameMs: minDelay + 6 };
    if (remaining > 90) return { minStep: 4, maxStep: 8, cps: 320, frameMs: minDelay + 8 };
    if (remaining > 32) return { minStep: 2, maxStep: 5, cps: 190, frameMs: minDelay + 10 };
    return { minStep: 1, maxStep: 3, cps: 120, frameMs: maxDelay };
  }

  function consumeWhitespace(source, end) {
    while (end < source.length && isWhitespace(source[end]) && source[end] !== "\n") end += 1;
    return end;
  }

  function findNaturalEnd(source, start, timing) {
    const hardLimit = source.length;
    const minEnd = Math.min(hardLimit, start + timing.minStep);
    const maxEnd = Math.min(hardLimit, start + timing.maxStep);
    let end = start;

    if (start >= hardLimit) return hardLimit;
    if (source[start] === "\n") {
      let lineEnd = Math.min(hardLimit, start + 1);
      while (lineEnd < hardLimit && source[lineEnd] === "\n" && lineEnd - start < 2) lineEnd += 1;
      return lineEnd;
    }

    while (end < minEnd) {
      if (source[end] === "\n") return end + 1;
      end += 1;
    }

    while (end < maxEnd) {
      const ch = source[end] || "";
      const prev = source[end - 1] || "";

      if (ch === "\n") return end;
      if (isHardPunctuation(prev)) return consumeWhitespace(source, end);
      if (isSoftPunctuation(prev) && end - start >= timing.minStep + 4) return consumeWhitespace(source, end);
      if (isWhitespace(ch) && end - start >= timing.minStep + 6) return consumeWhitespace(source, end + 1);
      if (isCjkChar(ch) && /[，、。！？：；]/.test(prev) && end - start >= timing.minStep + 6) return end;
      end += 1;
    }

    while (end < hardLimit && isAsciiWordChar(source[end - 1]) && isAsciiWordChar(source[end])) end += 1;
    if (end < hardLimit && isWhitespace(source[end])) end += 1;
    return Math.max(start + 1, end);
  }

  function hasUnclosedCodeFence(text) {
    const lines = String(text || "").split("\n");
    let fence = null;
    for (const line of lines) {
      const match = line.match(CODE_FENCE_RE);
      if (!match) continue;
      const marker = match[1];
      if (!fence) {
        fence = marker;
        continue;
      }
      if (marker[0] === fence[0] && marker.length >= fence.length) {
        fence = null;
      }
    }
    return !!fence;
  }

  function countUnescaped(text, needle) {
    let count = 0;
    for (let i = 0; i < text.length; i += 1) {
      if (text.startsWith(needle, i) && text[i - 1] !== "\\") {
        count += 1;
        i += needle.length - 1;
      }
    }
    return count;
  }

  function isInsideOpenCodeFence(text, index) {
    return hasUnclosedCodeFence(String(text || "").slice(0, index));
  }

  function repairInlineMarkdown(text) {
    let output = String(text || "");
    if (isInsideOpenCodeFence(output, output.length)) return output;

    const backticks = countUnescaped(output, "`");
    if (backticks % 2 === 1) output += "`";

    const bold = countUnescaped(output, "**");
    if (bold % 2 === 1) output += "**";

    const strike = countUnescaped(output, "~~");
    if (strike % 2 === 1) output += "~~";

    const imageMatch = output.match(/!\[[^\]]*]\([^)\n]*$/);
    if (imageMatch) {
      output = output.slice(0, imageMatch.index);
    } else {
      const linkMatch = output.match(/\[([^\]\n]+)]\([^)\n]*$/);
      if (linkMatch) {
        output = output.slice(0, linkMatch.index) + `[${linkMatch[1]}](streamdown:incomplete-link)`;
      } else {
        const openBracket = output.match(/\[([^\]\n]*)$/);
        if (openBracket) {
          output = output.slice(0, openBracket.index) + openBracket[1];
        }
      }
    }

    return output;
  }

  function repairMath(text) {
    let output = String(text || "");
    const displayDollarCount = countUnescaped(output, "$$");
    if (displayDollarCount % 2 === 1) output += "\n$$";

    const parenOpen = countUnescaped(output, "\\(");
    const parenClose = countUnescaped(output, "\\)");
    if (parenOpen > parenClose) output += "\\)";

    const bracketOpen = countUnescaped(output, "\\[");
    const bracketClose = countUnescaped(output, "\\]");
    if (bracketOpen > bracketClose) output += "\n\\]";

    const beginMatch = [...output.matchAll(/\\begin\{([a-zA-Z*]+)\}/g)].map(m => m[1]);
    const endMatch = [...output.matchAll(/\\end\{([a-zA-Z*]+)\}/g)].map(m => m[1]);
    for (let i = beginMatch.length - 1; i >= 0; i -= 1) {
      const env = beginMatch[i];
      const endIndex = endMatch.lastIndexOf(env);
      if (endIndex === -1 || endIndex < i) {
        output += `\n\\end{${env}}`;
        break;
      }
    }

    return output;
  }

  function stripIncompleteHtmlTail(text) {
    const src = String(text || "");
    const tail = src.match(/<[^>\n]*$/);
    if (!tail) return src;
    return src.slice(0, tail.index);
  }

  function repairStreamingMarkdown(text) {
    let output = stripIncompleteHtmlTail(text);
    if (hasUnclosedCodeFence(output)) output += "\n```";
    output = repairMath(output);
    output = repairInlineMarkdown(output);
    output = output.replace(/(^|\n)( {0,3})([-=]{1,2})$/g, "$1$2$3\u200B");
    return output;
  }

  function startsHtmlBlock(line) {
    const match = line.match(/^ {0,3}<([a-zA-Z][\w:-]*)(?=[\s>/])/);
    return match && !HTML_VOID_TAGS.has(match[1].toLowerCase());
  }

  function splitClosedMathSegments(text) {
    const src = String(text || "");
    const parts = [];
    let cursor = 0;
    const pattern = /(^|\n)(\$\$[\s\S]*?\$\$|\\\[[\s\S]*?\\\]|\\begin\{([a-zA-Z*]+)\}[\s\S]*?\\end\{\3\})(?=\n|$)/g;
    let match;
    while ((match = pattern.exec(src))) {
      const start = match.index + match[1].length;
      if (start > cursor) parts.push(src.slice(cursor, start));
      parts.push(src.slice(start, pattern.lastIndex));
      cursor = pattern.lastIndex;
    }
    if (cursor < src.length) parts.push(src.slice(cursor));
    return parts.filter(Boolean);
  }

  function splitStreamingMarkdownBlocks(markdown) {
    const src = String(markdown || "");
    if (!src) return [];
    if (/\[\^[\w-]{1,200}\](?!:)|\[\^[\w-]{1,200}\]:/.test(src)) return [src];

    const lines = src.split(/(\n)/);
    const logicalLines = [];
    for (let i = 0; i < lines.length; i += 2) {
      logicalLines.push((lines[i] || "") + (lines[i + 1] || ""));
    }

    const blocks = [];
    let current = "";
    let inFence = false;
    let fenceMarker = "";
    let inMath = false;
    let htmlDepth = 0;

    function pushCurrent() {
      if (!current) return;
      blocks.push(current);
      current = "";
    }

    for (const line of logicalLines) {
      const trimmed = line.trim();
      const fence = line.match(CODE_FENCE_RE);

      if (fence) {
        if (!inFence) {
          if (current && !current.endsWith("\n\n")) pushCurrent();
          inFence = true;
          fenceMarker = fence[1];
        } else if (fence[1][0] === fenceMarker[0] && fence[1].length >= fenceMarker.length) {
          inFence = false;
        }
        current += line;
        if (!inFence) pushCurrent();
        continue;
      }

      if (!inFence && !inMath && BLOCK_MATH_BEGIN_RE.test(line)) {
        if (current && !current.endsWith("\n\n")) pushCurrent();
        inMath = (
          countUnescaped(line, "$$") === 1
          || (line.includes("\\[") && !line.includes("\\]"))
          || (/\\begin\{([a-zA-Z*]+)\}/.test(line) && !/\\end\{([a-zA-Z*]+)\}/.test(line))
        );
        current += line;
        if (!inMath) pushCurrent();
        continue;
      }

      if (inMath) {
        current += line;
        if (BLOCK_MATH_END_RE.test(line)) {
          inMath = false;
          pushCurrent();
        }
        continue;
      }

      if (!inFence && startsHtmlBlock(line)) htmlDepth += 1;
      if (htmlDepth > 0) {
        current += line;
        if (/<\/[a-zA-Z][\w:-]*>\s*$/.test(trimmed)) htmlDepth = Math.max(0, htmlDepth - 1);
        if (htmlDepth === 0) pushCurrent();
        continue;
      }

      current += line;
      if (!inFence && trimmed === "") {
        pushCurrent();
      }
    }
    pushCurrent();
    return blocks
      .flatMap(splitClosedMathSegments)
      .filter(block => block && block.trim());
  }

  function createRenderer(options = {}) {
    const getContextKey = typeof options.getContextKey === "function" ? options.getContextKey : () => "";
    const buildAssistantBubbleHtml = options.buildAssistantBubbleHtml || window.buildAssistantBubbleHtml;
    const prepareAssistantBubbleDom = options.prepareAssistantBubbleDom || window.prepareAssistantBubbleDom;
    const typesetMathNow = options.typesetMathNow || window.typesetMathNow;
    const hasRenderableMathInHtml = options.hasRenderableMathInHtml || window.hasRenderableMathInHtml;
    const revealPendingMathSources = options.revealPendingMathSources || window.revealPendingMathSources;
    const queueStreamingMathTypeset = options.queueStreamingMathTypeset || window.queueStreamingMathTypesetLight;
    const buildAssistantContentHtml = options.buildAssistantContentHtml || ((text, sources, finalRender, animateFrom) => {
      const html = buildAssistantBubbleHtml(text, sources, finalRender, false, animateFrom);
      return String(html || "")
        .replace(/^[\s\S]*?(?=<div class="stream-stable"|<div class="stream-active"|<p|<h|<ul|<ol|<pre|<blockquote|<table|$)/, "")
        .replace(/<span class="chatgpt-like-cursor">[\s\S]*?<\/span>\s*$/, "")
        .replace(/<div class="answer-meta stream-meta-placeholder"[\s\S]*?<\/div>\s*$/, "");
    });

    let contextKey = "";
    let seq = 0;
    let disposed = false;
    let bubbleRef = null;
    let frameHandle = 0;
    let frameTimer = 0;
    let renderToken = 0;
    let state = createState();

    function createState() {
      return {
        targetText: "",
        displayText: "",
        finalRender: false,
        showCaret: false,
        animateFrom: null,
        sources: [],
        carry: 0,
        lastFrameAt: 0,
        lastTypesetAt: 0,
        lastTypesetLength: 0,
        rawText: "",
        repairedText: "",
        blocks: [],
        blockOffsets: [],
        blockNodes: [],
        lastRenderedCharCount: 0,
        lastFullSyncAt: 0
      };
    }

    function reset(nextKey = getContextKey()) {
      stopAnimation();
      contextKey = nextKey;
      seq += 1;
      bubbleRef = null;
      renderToken += 1;
      state = createState();
    }

    function ensureContext() {
      const nextKey = getContextKey();
      if (nextKey !== contextKey) {
        reset(nextKey);
      }
      return contextKey;
    }

    function clearFrameTimer() {
      if (!frameTimer) return;
      clearTimeout(frameTimer);
      frameTimer = 0;
    }

    function clearAnimationFrameHandle() {
      if (!frameHandle) return;
      cancelAnimationFrame(frameHandle);
      frameHandle = 0;
    }

    function stopAnimation() {
      clearFrameTimer();
      clearAnimationFrameHandle();
    }

    function shouldResetDisplay(nextText) {
      if (!state.displayText) return false;
      if (!nextText) return true;
      return !nextText.startsWith(state.displayText);
    }

    function renderHtmlIntoBubble(bubble, text, renderOptions = {}) {
      if (disposed || !bubble) return Promise.resolve(false);
      const nextKey = ensureContext();
      const finalRender = !!renderOptions.finalRender;
      const showCaret = !!renderOptions.showCaret;
      const animateFrom = renderOptions.animateFrom === undefined ? null : renderOptions.animateFrom;
      const sources = Array.isArray(renderOptions.sources) ? renderOptions.sources : [];
      const currentSeq = ++seq;
      const currentToken = ++renderToken;
      const html = buildAssistantBubbleHtml(text, sources, finalRender, showCaret, animateFrom);
      const staging = document.createElement("div");
      staging.className = bubble.className;
      staging.style.cssText = "position:absolute;left:-10000px;top:0;width:" + Math.max(1, bubble.clientWidth || 680) + "px;visibility:hidden;pointer-events:none;";
      staging.innerHTML = html;
      document.body.appendChild(staging);
      prepareAssistantBubbleDom(staging, sources, finalRender);

      const isLongStream = text.length >= LONG_STREAM_TEXT_LENGTH;
      const shouldTypeset = finalRender
        || (
          hasRenderableMathInHtml(html)
          && !isLongStream
          && (
            Date.now() - state.lastTypesetAt >= STREAMING_TYPESAT_MIN_INTERVAL
            || Math.abs(text.length - state.lastTypesetLength) >= STREAMING_TYPESAT_MIN_DELTA
          )
        );

      const apply = () => {
        if (disposed || currentSeq !== seq || currentToken !== renderToken || nextKey !== contextKey || !bubble.isConnected) return false;
        bubble.classList.toggle("chatgpt-like-streaming", !finalRender);
        bubble.classList.toggle("codex-streaming-output", !finalRender);
        bubble.innerHTML = staging.innerHTML;
        prepareAssistantBubbleDom(bubble, sources, finalRender);
        if (!finalRender && hasRenderableMathInHtml(staging.innerHTML)) {
          revealPendingMathSources(bubble);
        }
        if (shouldTypeset) {
          state.lastTypesetAt = Date.now();
          state.lastTypesetLength = text.length;
        }
        return true;
      };

      const cleanup = () => staging.remove();
      if (finalRender && shouldTypeset && typeof typesetMathNow === "function") {
        return typesetMathNow(staging, () => currentSeq === seq && nextKey === contextKey)
          .then(apply)
          .finally(cleanup);
      }

      const result = apply();
      if (!finalRender && shouldTypeset && typeof queueStreamingMathTypeset === "function") {
        queueStreamingMathTypeset(bubble, text.length);
      }
      cleanup();
      return Promise.resolve(result);
    }

    function makeBlockNode(index) {
      const node = document.createElement("div");
      node.className = "streamdown-block";
      if (!node.dataset) node.dataset = {};
      node.dataset.streamBlockIndex = String(index);
      return node;
    }

    function commitBubbleShell(bubble, finalRender) {
      bubble.classList.toggle("chatgpt-like-streaming", !finalRender);
      bubble.classList.toggle("codex-streaming-output", !finalRender);
    }

    function renderBlockHtml(block, index, blockOffsets, animateFrom, finalRender) {
      const previousLength = blockOffsets[index] || 0;
      const localAnimateFrom = animateFrom === null || animateFrom === undefined
        ? null
        : Math.max(0, Number(animateFrom) - previousLength);
      return buildAssistantContentHtml(block, state.sources, !!finalRender, localAnimateFrom);
    }

    function syncBlockNodesInPlace(bubble, nextNodes) {
      if (typeof bubble.insertBefore !== "function" || typeof bubble.appendChild !== "function" || typeof bubble.removeChild !== "function") {
        if (typeof bubble.replaceChildren === "function") {
          bubble.replaceChildren(...nextNodes);
        } else {
          bubble.innerHTML = nextNodes.map(node => node.outerHTML || node.innerHTML || "").join("");
        }
        return;
      }

      let cursor = bubble.firstChild;
      nextNodes.forEach(node => {
        if (node.parentNode === bubble) {
          if (node !== cursor) bubble.insertBefore(node, cursor || null);
        } else {
          bubble.insertBefore(node, cursor || null);
        }
        cursor = node.nextSibling;
      });

      while (cursor) {
        const next = cursor.nextSibling;
        if (cursor.classList && (cursor.classList.contains("thinking") || cursor.classList.contains("tool-status"))) {
          cursor = next;
          continue;
        }
        bubble.removeChild(cursor);
        cursor = next;
      }
    }

    function renderBlocksIntoBubble(bubble, text, renderOptions = {}) {
      if (disposed || !bubble) return Promise.resolve(false);
      const nextKey = ensureContext();
      const currentSeq = ++seq;
      const currentToken = ++renderToken;
      const finalRender = !!renderOptions.finalRender;
      const showCaret = !!renderOptions.showCaret;
      const animateFrom = renderOptions.animateFrom === undefined ? null : renderOptions.animateFrom;
      const sources = Array.isArray(renderOptions.sources) ? renderOptions.sources : [];
      const repaired = repairStreamingMarkdown(text);
      const blocks = splitStreamingMarkdownBlocks(repaired);
      const blockOffsets = new Array(blocks.length);
      const now = Date.now();
      const shouldForceFullSync = false;

      if (shouldForceFullSync) {
        state.lastFullSyncAt = now;
      }

      commitBubbleShell(bubble, finalRender);
      if (!state.blockNodes.length || shouldForceFullSync) {
        bubble.innerHTML = "";
        state.blockNodes = [];
      }

      const fragment = document.createDocumentFragment ? document.createDocumentFragment() : null;
      const nextNodes = [];
      const max = blocks.length;
      let offset = 0;
      for (let i = 0; i < max; i += 1) {
        const block = blocks[i];
        blockOffsets[i] = offset;
        offset += block.length;
        const existing = state.blockNodes[i];
        const canReuse = existing && state.blocks[i] === block && !shouldForceFullSync;
        const node = canReuse ? existing : makeBlockNode(i);
        if (!canReuse) {
          node.innerHTML = renderBlockHtml(block, i, blockOffsets, animateFrom, finalRender);
        }
        nextNodes.push(node);
        if (!canReuse && fragment) fragment.appendChild(node);
      }

      const preservedNodes = [];
      if (!finalRender && typeof bubble.querySelectorAll === "function" && typeof bubble.removeChild === "function") {
        bubble.querySelectorAll(":scope > .thinking, :scope > .tool-status").forEach(node => {
          preservedNodes.push(node);
          bubble.removeChild(node);
        });
      }

      syncBlockNodesInPlace(bubble, nextNodes);

      if (!finalRender && preservedNodes.length && typeof bubble.insertBefore === "function") {
        const anchor = bubble.firstChild;
        preservedNodes.forEach(node => {
          bubble.insertBefore(node, anchor);
        });
      }

      if (showCaret && text.trim() && text.trim() !== "思考中...") {
        const caret = document.createElement("span");
        caret.className = "chatgpt-like-cursor";
        caret.textContent = "▍";
        if (typeof bubble.appendChild === "function") {
          bubble.appendChild(caret);
        } else {
          bubble.innerHTML += '<span class="chatgpt-like-cursor">▍</span>';
        }
      }

      state.rawText = text;
      state.repairedText = repaired;
      state.blocks = blocks;
      state.blockOffsets = blockOffsets;
      state.blockNodes = nextNodes;
      state.lastRenderedCharCount = repaired.length;

      if (finalRender) {
        prepareAssistantBubbleDom(bubble, sources, finalRender);
      }
      if (hasRenderableMathInHtml(repaired) || hasRenderableMathInHtml(text)) {
        revealPendingMathSources(bubble);
        if (
          now - state.lastTypesetAt >= STREAMING_TYPESAT_MIN_INTERVAL
          || Math.abs(text.length - state.lastTypesetLength) >= STREAMING_TYPESAT_MIN_DELTA
        ) {
          state.lastTypesetAt = now;
          state.lastTypesetLength = text.length;
          if (typeof queueStreamingMathTypeset === "function") {
            queueStreamingMathTypeset(bubble, text.length);
          }
        }
      }
      if (!finalRender) {
        prepareAssistantBubbleDom(bubble, sources, finalRender);
      }

      return Promise.resolve(!disposed && currentSeq === seq && currentToken === renderToken && nextKey === contextKey);
    }

    function scheduleNextFrame(delay) {
      if (disposed) return;
      stopAnimation();
      const wait = Math.max(0, Number(delay) || 0);
      if (wait > 0) {
        frameTimer = setTimeout(() => {
          frameTimer = 0;
          frameHandle = requestAnimationFrame(tick);
        }, wait);
        return;
      }
      frameHandle = requestAnimationFrame(tick);
    }

    function tick(timestamp) {
      frameHandle = 0;
      if (disposed || !bubbleRef || !bubbleRef.isConnected) return;
      const remaining = state.targetText.length - state.displayText.length;
      if (remaining <= 0) {
        renderBlocksIntoBubble(bubbleRef, state.displayText, {
          finalRender: false,
          showCaret: state.showCaret,
          animateFrom: state.displayText.length,
          sources: state.sources
        });
        return;
      }

      const timing = getStepAndDelay(Math.min(remaining, LONG_STREAM_TEXT_LENGTH), DEFAULT_MIN_DELAY, DEFAULT_MAX_DELAY);
      const now = timestamp || Date.now();
      const deltaMs = state.lastFrameAt > 0 ? clamp(now - state.lastFrameAt, 8, 34) : FRAME_MS;
      state.lastFrameAt = now;
      state.carry += (timing.cps * deltaMs) / 1000;

      if (state.carry < 1) {
        scheduleNextFrame(timing.frameMs);
        return;
      }

      const start = state.displayText.length;
      const budget = Math.max(1, Math.floor(state.carry));
      const isLongStream = state.targetText.length >= LONG_STREAM_TEXT_LENGTH;
      const boundedTiming = isLongStream
        ? {
            minStep: Math.max(2, Math.min(14, Math.max(timing.minStep, Math.floor(budget * 0.55)))),
            maxStep: Math.max(10, Math.min(40, Math.max(timing.maxStep, budget)))
          }
        : {
            minStep: Math.max(1, Math.min(timing.maxStep, Math.max(timing.minStep, Math.floor(budget * 0.55)))),
            maxStep: Math.max(timing.minStep, Math.min(56, Math.max(timing.maxStep, budget)))
          };
      const end = findNaturalEnd(state.targetText, start, boundedTiming);
      const advanced = Math.max(1, end - start);
      state.carry = Math.max(0, state.carry - advanced);
      const previousLength = state.displayText.length;
      state.displayText = state.targetText.slice(0, end);

      renderBlocksIntoBubble(bubbleRef, state.displayText, {
        finalRender: false,
        showCaret: state.showCaret,
        animateFrom: previousLength,
        sources: state.sources
      }).finally(() => {
        if (state.displayText.length < state.targetText.length) {
          scheduleNextFrame(timing.frameMs);
        }
      });
    }

    function render(bubble, text, renderOptions = {}) {
      if (disposed || !bubble) return Promise.resolve(false);
      ensureContext();
      bubbleRef = bubble;
      const nextText = String(text || "");
      const finalRender = !!renderOptions.finalRender;
      const showCaret = !!renderOptions.showCaret;
      const animateFrom = renderOptions.animateFrom === undefined ? null : renderOptions.animateFrom;
      const sources = Array.isArray(renderOptions.sources) ? renderOptions.sources : [];

      if (shouldResetDisplay(nextText)) {
        state.displayText = "";
        state.carry = 0;
        state.lastFrameAt = 0;
      }

      state.targetText = nextText;
      state.finalRender = finalRender;
      state.showCaret = showCaret;
      state.animateFrom = animateFrom;
      state.sources = sources;

      if (finalRender) {
        stopAnimation();
        state.displayText = nextText;
        state.carry = 0;
        state.lastFrameAt = 0;
        return renderHtmlIntoBubble(bubbleRef, nextText, {
          finalRender: true,
          showCaret: false,
          animateFrom,
          sources
        });
      }

      if (!state.displayText && nextText) {
        const initialTiming = getStepAndDelay(nextText.length, DEFAULT_MIN_DELAY, DEFAULT_MAX_DELAY);
        const end = findNaturalEnd(nextText, 0, initialTiming);
        state.displayText = nextText.slice(0, end);
      }

      const previousLength = Math.max(0, Math.min(state.displayText.length, nextText.length));
      const renderPromise = renderBlocksIntoBubble(bubbleRef, state.displayText, {
        finalRender: false,
        showCaret,
        animateFrom: previousLength,
        sources
      });

      if (state.displayText.length < state.targetText.length) {
        scheduleNextFrame(0);
      } else {
        stopAnimation();
      }

      return renderPromise;
    }

    return {
      reset,
      dispose() {
        disposed = true;
        stopAnimation();
        reset(getContextKey());
      },
      render
    };
  }

  window.CodexStreamOutput = {
    createRenderer,
    repairStreamingMarkdown,
    splitStreamingMarkdownBlocks
  };
})();
