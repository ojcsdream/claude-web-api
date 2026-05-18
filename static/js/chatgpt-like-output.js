(function () {
  "use strict";

  const DEFAULT_MIN_DELAY = 8;
  const DEFAULT_MAX_DELAY = 170;

  function clamp(value, min, max) {
    return Math.max(min, Math.min(max, value));
  }

  function isCjkChar(ch) {
    return /[\u3400-\u9fff\uf900-\ufaff]/.test(ch || "");
  }

  function isWhitespace(ch) {
    return /\s/.test(ch || "");
  }

  function isAsciiWordChar(ch) {
    return /[A-Za-z0-9_]/.test(ch || "");
  }

  function isSoftPunctuation(ch) {
    return /[,:;，、：；]/.test(ch || "");
  }

  function isHardPunctuation(ch) {
    return /[.!?。！？]/.test(ch || "");
  }

  function getStepAndDelay(remaining, minDelay, maxDelay) {
    if (remaining > 2400) return { minStep: 72, maxStep: 150, delay: minDelay };
    if (remaining > 1400) return { minStep: 54, maxStep: 116, delay: minDelay + 2 };
    if (remaining > 800) return { minStep: 38, maxStep: 86, delay: minDelay + 8 };
    if (remaining > 420) return { minStep: 24, maxStep: 54, delay: minDelay + 18 };
    if (remaining > 180) return { minStep: 14, maxStep: 32, delay: minDelay + 32 };
    if (remaining > 70) return { minStep: 8, maxStep: 18, delay: minDelay + 58 };
    if (remaining > 28) return { minStep: 4, maxStep: 10, delay: minDelay + 84 };
    return { minStep: 1, maxStep: 4, delay: maxDelay };
  }

  function getCadenceDelay(baseDelay, emittedLength, lastChar, remaining, minDelay, maxDelay) {
    let delay = baseDelay;
    const wave = emittedLength % 17;
    if (wave === 0) delay += 44;
    else if (wave === 6) delay += 22;
    else if (wave === 11) delay += 12;

    if (lastChar === "\n") delay += 88;
    else if (isHardPunctuation(lastChar)) delay += 62;
    else if (isSoftPunctuation(lastChar)) delay += 30;
    else if (isWhitespace(lastChar)) delay += 8;

    if (remaining < 220) delay += 8;
    if (remaining < 90) delay += 18;
    if (remaining < 35) delay += 28;

    return clamp(delay, minDelay, maxDelay);
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
    if (source[start] === "\n") return Math.min(hardLimit, start + 1);

    while (end < minEnd) {
      const ch = source[end];
      if (ch === "\n") return end + 1;
      end += 1;
    }

    while (end < maxEnd) {
      const ch = source[end] || "";
      const prev = source[end - 1] || "";

      if (ch === "\n") return end;
      if (isHardPunctuation(prev)) return consumeWhitespace(source, end);
      if (isSoftPunctuation(prev) && end - start >= timing.minStep + 4) return consumeWhitespace(source, end);
      if (isWhitespace(ch) && end - start >= timing.minStep + 6) return consumeWhitespace(source, end + 1);
      if (isCjkChar(ch) && end - start >= timing.minStep + 8 && /[，、。！？：；]/.test(source[end - 1] || "")) return end;

      end += 1;
    }

    while (end < hardLimit && isAsciiWordChar(source[end - 1]) && isAsciiWordChar(source[end])) end += 1;
    if (end < hardLimit && isWhitespace(source[end])) end += 1;
    return Math.max(start + 1, end);
  }

  class StreamingTextController {
    constructor(options) {
      this.content = "";
      this.displayContent = "";
      this.enabled = options.enabled !== false;
      this.minDelay = Number.isFinite(options.minDelay) ? options.minDelay : DEFAULT_MIN_DELAY;
      this.maxDelay = Number.isFinite(options.maxDelay) ? options.maxDelay : DEFAULT_MAX_DELAY;
      this.resetKey = options.resetKey;
      this.onUpdate = typeof options.onUpdate === "function" ? options.onUpdate : function () {};
      this.timer = null;
      this.disposed = false;
    }

    setContent(content, options = {}) {
      if (this.disposed) return;

      const nextContent = String(content || "");
      const nextEnabled = options.enabled !== undefined ? !!options.enabled : this.enabled;
      const nextResetKey = options.resetKey !== undefined ? options.resetKey : this.resetKey;
      const shouldReset = nextResetKey !== this.resetKey || !nextContent || !nextContent.startsWith(this.displayContent);

      this.enabled = nextEnabled;
      this.resetKey = nextResetKey;
      this.content = nextContent;

      if (shouldReset) {
        this.displayContent = "";
        this.emit(false);
      }

      if (!this.enabled) {
        this.displayContent = this.content;
        this.clearTimer();
        this.emit(true);
        return;
      }

      this.schedule(0);
    }

    reset(content = "", resetKey = this.resetKey) {
      this.clearTimer();
      this.content = String(content || "");
      this.displayContent = "";
      this.resetKey = resetKey;
      this.emit(false);
      if (this.content && this.enabled) this.schedule(0);
    }

    flush() {
      if (this.disposed) return;
      this.clearTimer();
      this.displayContent = this.content;
      this.emit(true);
    }

    stop() {
      this.clearTimer();
    }

    dispose() {
      this.disposed = true;
      this.clearTimer();
    }

    clearTimer() {
      if (this.timer) {
        clearTimeout(this.timer);
        this.timer = null;
      }
    }

    schedule(delay) {
      if (this.timer || this.disposed) return;
      this.timer = setTimeout(() => {
        this.timer = null;
        this.tick();
      }, delay);
    }

    tick() {
      if (this.disposed || !this.enabled) return;
      const remaining = this.content.length - this.displayContent.length;
      if (remaining <= 0) {
        this.emit(true);
        return;
      }

      const timing = getStepAndDelay(remaining, this.minDelay, this.maxDelay);
      const start = this.displayContent.length;
      const end = findNaturalEnd(this.content, start, timing);
      this.displayContent = this.content.slice(0, end);
      this.emit(this.displayContent.length >= this.content.length);

      if (this.displayContent.length < this.content.length) {
        const lastChar = this.displayContent[this.displayContent.length - 1] || "";
        const delay = getCadenceDelay(timing.delay, this.displayContent.length, lastChar, this.content.length - this.displayContent.length, this.minDelay, this.maxDelay);
        this.schedule(delay);
      }
    }

    emit(done) {
      this.onUpdate(this.displayContent, {
        content: this.content,
        done: !!done,
        remaining: Math.max(0, this.content.length - this.displayContent.length),
        resetKey: this.resetKey
      });
    }
  }

  window.ChatGPTLikeOutput = {
    createStreamingText(options = {}) {
      return new StreamingTextController(options);
    },
    getStepAndDelay
  };
})();
