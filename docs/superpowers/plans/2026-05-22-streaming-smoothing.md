# Streaming Smoothing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make assistant streaming feel continuously fluid with almost no visible typewriter effect in `/opt/api-`.

**Architecture:** Keep the existing incremental markdown pipeline and replace only the visible text pacing layer. A `requestAnimationFrame`-driven controller will let visible text continuously follow the latest backend content, while CSS changes reduce animation presence on new text.

**Tech Stack:** FastAPI static frontend, vanilla JavaScript, CSS, Node-based regression scripts

---

### Task 1: Add Controller Regression Coverage

**Files:**
- Create: `scripts/test_streaming_text_controller.js`

- [ ] **Step 1: Write the failing test**

```js
// Assert that createStreamingText updates visible content monotonically,
// catches up after frame advances, flushes immediately, and resets on resetKey change.
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node scripts/test_streaming_text_controller.js`
Expected: FAIL before the controller is updated or before the test file exists.

- [ ] **Step 3: Write minimal implementation support**

```js
// Load static/js/chatgpt-like-output.js in a VM sandbox with mock RAF,
// then drive the controller through deterministic frame steps.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `node scripts/test_streaming_text_controller.js`
Expected: PASS with a summary line.

- [ ] **Step 5: Commit**

```bash
git add scripts/test_streaming_text_controller.js
git commit -m "test: add streaming controller regression coverage"
```

### Task 2: Replace Timer-Based Typewriter Pacing

**Files:**
- Modify: `static/js/chatgpt-like-output.js`
- Test: `scripts/test_streaming_text_controller.js`

- [ ] **Step 1: Write the failing test**

```js
// Extend the controller test so timeout-style burst pacing no longer satisfies
// the required quick catch-up and reset behavior.
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node scripts/test_streaming_text_controller.js`
Expected: FAIL until the controller follows content on animation frames.

- [ ] **Step 3: Write minimal implementation**

```js
// Replace timeout scheduling with requestAnimationFrame scheduling,
// track fractional progress, and advance by backlog-sensitive speed.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `node scripts/test_streaming_text_controller.js`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add static/js/chatgpt-like-output.js scripts/test_streaming_text_controller.js
git commit -m "feat: smooth streaming text follow cadence"
```

### Task 3: Reduce Frontend Typewriter Presence

**Files:**
- Modify: `static/index.html`
- Modify: `static/css/chatgpt-like-output.css`
- Test: `scripts/test_streaming_math_render.js`

- [ ] **Step 1: Write the failing test**

```js
// Re-run the existing streaming markdown/math regression suite after changing
// final flush and streaming update wiring to ensure no rendering safety regression.
```

- [ ] **Step 2: Run test to verify current behavior baseline**

Run: `node scripts/test_streaming_math_render.js`
Expected: PASS before the UI pacing changes.

- [ ] **Step 3: Write minimal implementation**

```js
// Remove residual typewriter-specific scheduling assumptions from index.html
// and reduce CSS token/stream animations to short, subtle fades.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `node scripts/test_streaming_math_render.js && node scripts/test_streaming_text_controller.js`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add static/index.html static/css/chatgpt-like-output.css
git commit -m "refactor: soften streaming UI presentation"
```
