# Streaming Smoothing Design

## Goal

Make assistant streaming in `/opt/api-` feel continuously fluid, with almost no visible typewriter cadence, while preserving the existing incremental markdown rendering, scroll behavior, and math/code safety rules.

## Problem

The current UI already avoids full-list re-rendering, but the visible output still feels jerky because:

- Streaming text is displayed in timed bursts that intentionally pause on words and punctuation.
- Newly revealed text uses strong blur/slide animations that amplify chunk boundaries.
- Final catch-up can visibly “finish typing” after the backend has already completed.

This makes the UI feel like a typewriter instead of a near-continuous stream.

## Chosen Approach

Replace the timer-based typewriter cadence with a frame-driven continuous follow controller:

- Incoming backend chunks still append to a target string immediately.
- The visible string follows that target on `requestAnimationFrame`, using a dynamic characters-per-second rate based on backlog size.
- Small backlogs catch up almost immediately; large backlogs accelerate so the UI stays close to the stream without exposing raw network chunk jitter.
- Token animation remains only as a very light fade on newly revealed text.

## Non-Goals

- No backend protocol changes.
- No rewrite of markdown rendering or math detection.
- No change to message persistence, source rendering, or auth logic.

## Rendering Rules

### Streaming Controller

- Maintain `content` as the latest backend text and `displayContent` as the visible prefix.
- Advance `displayContent` on animation frames instead of punctuation-based timeout bursts.
- Use a backlog-sensitive speed so the UI remains within a short visual lag window.
- Preserve monotonic forward-only updates unless a reset key or content reset explicitly restarts the controller.

### Markdown And Math

- Keep the stable/active segmented markdown strategy already used by `renderStreamingMarkdownSegmented`.
- Keep the guarded math behavior:
  - unfinished math stays hidden or pending
  - final math rendering can still use buffered staging
  - light math typeset remains throttled during streaming

### Visual Presence

- Reduce token animation from blur/slide emphasis to a short, subtle fade.
- Reduce stream container effects so text does not “breathe” or pulse noticeably while streaming.
- Keep caret behavior, but ensure it does not visually dominate.

## Testing

Add a dedicated script-level regression test for the streaming controller to verify:

- visible content only moves forward
- controller catches up to target content after repeated frame advances
- `flush()` reveals the full content immediately
- changing the reset key restarts visible content from the beginning

Keep the existing streaming markdown/math regression script unchanged except for compatibility with the new controller behavior if needed.

## Files

- Modify `static/js/chatgpt-like-output.js` to replace the controller timing model.
- Modify `static/index.html` to remove residual typewriter assumptions and tighten final flush behavior.
- Modify `static/css/chatgpt-like-output.css` to soften token and streaming-state animations.
- Add `scripts/test_streaming_text_controller.js` for controller-level regression coverage.
