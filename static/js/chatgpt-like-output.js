(function () {
  "use strict";

  const DEFAULT_MIN_DELAY = 8;
  const DEFAULT_MAX_DELAY = 24;
  const FRAME_MS = 16;

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
      this.timerKind = "";
      this.carry = 0;
      this.lastFrameAt = 0;
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
        this.carry = 0;
        this.lastFrameAt = 0;
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
      this.carry = 0;
      this.lastFrameAt = 0;
      this.emit(false);
      if (this.content && this.enabled) this.schedule(0);
    }

    flush() {
      if (this.disposed) return;
      this.clearTimer();
      this.displayContent = this.content;
      this.carry = 0;
      this.lastFrameAt = 0;
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
      if (!this.timer) return;
      if (this.timerKind === "raf" && typeof cancelAnimationFrame === "function") {
        cancelAnimationFrame(this.timer);
      } else {
        clearTimeout(this.timer);
      }
      this.timer = null;
      this.timerKind = "";
    }

    schedule(delay) {
      if (this.timer || this.disposed) return;
      const wait = Math.max(0, Number(delay) || 0);

      if (typeof requestAnimationFrame === "function") {
        if (wait > 0) {
          this.timerKind = "timeout";
          this.timer = setTimeout(() => {
            this.timer = null;
            this.timerKind = "";
            this.scheduleFrame();
          }, wait);
          return;
        }
        this.scheduleFrame();
        return;
      }

      this.timerKind = "timeout";
      this.timer = setTimeout(() => {
        this.timer = null;
        this.timerKind = "";
        this.tick(Date.now());
      }, wait || FRAME_MS);
    }

    scheduleFrame() {
      if (this.timer || this.disposed) return;
      this.timerKind = "raf";
      this.timer = requestAnimationFrame((timestamp) => {
        this.timer = null;
        this.timerKind = "";
        this.tick(timestamp);
      });
    }

    tick(timestamp) {
      if (this.disposed || !this.enabled) return;
      const remaining = this.content.length - this.displayContent.length;
      if (remaining <= 0) {
        this.emit(true);
        return;
      }

      const timing = getStepAndDelay(remaining, this.minDelay, this.maxDelay);
      const now = timestamp || Date.now();
      const deltaMs = this.lastFrameAt > 0 ? clamp(now - this.lastFrameAt, 8, 34) : FRAME_MS;
      this.lastFrameAt = now;
      this.carry += (timing.cps * deltaMs) / 1000;

      if (this.carry < 1) {
        this.schedule(timing.frameMs);
        return;
      }

      const start = this.displayContent.length;
      const budget = Math.max(1, Math.floor(this.carry));
      const boundedTiming = {
        minStep: Math.max(1, Math.min(timing.maxStep, Math.max(timing.minStep, Math.floor(budget * 0.55)))),
        maxStep: Math.max(timing.minStep, Math.min(56, Math.max(timing.maxStep, budget)))
      };
      const end = findNaturalEnd(this.content, start, boundedTiming);
      const advanced = Math.max(1, end - start);
      this.carry = Math.max(0, this.carry - advanced);
      this.displayContent = this.content.slice(0, end);
      this.emit(this.displayContent.length >= this.content.length);

      if (this.displayContent.length < this.content.length) {
        this.schedule(timing.frameMs);
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
