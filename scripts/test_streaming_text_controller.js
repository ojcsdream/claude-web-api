const fs = require("fs");
const path = require("path");
const vm = require("vm");

function loadControllerScript(projectRoot) {
  const scriptPath = path.join(projectRoot, "static", "js", "chatgpt-like-output.js");
  return fs.readFileSync(scriptPath, "utf8");
}

function createSandbox() {
  let now = 0;
  let timerId = 1;
  let rafId = 1;
  const timers = new Map();
  const rafs = new Map();

  const sandbox = {
    console,
    Math,
    Date: {
      now() {
        return now;
      }
    },
    setTimeout(fn, delay = 0) {
      const id = timerId++;
      timers.set(id, {
        fn,
        time: now + Number(delay || 0)
      });
      return id;
    },
    clearTimeout(id) {
      timers.delete(id);
    },
    requestAnimationFrame(fn) {
      const id = rafId++;
      rafs.set(id, fn);
      return id;
    },
    cancelAnimationFrame(id) {
      rafs.delete(id);
    },
    window: {}
  };

  vm.createContext(sandbox);

  function runDueTimers() {
    let progressed = true;
    while (progressed) {
      progressed = false;
      const due = [...timers.entries()]
        .filter(([, task]) => task.time <= now)
        .sort((a, b) => a[1].time - b[1].time || a[0] - b[0]);
      for (const [id, task] of due) {
        timers.delete(id);
        task.fn();
        progressed = true;
      }
    }
  }

  function step(ms = 16) {
    now += ms;
    runDueTimers();
    const pendingRafs = [...rafs.entries()].sort((a, b) => a[0] - b[0]);
    rafs.clear();
    for (const [, fn] of pendingRafs) {
      fn(now);
      runDueTimers();
    }
  }

  function flushFrames(frameCount = 1, ms = 16) {
    for (let i = 0; i < frameCount; i += 1) {
      step(ms);
    }
  }

  return {
    sandbox,
    flushFrames,
    now() {
      return now;
    }
  };
}

function assert(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
}

function runSuite(projectRoot) {
  const runtime = createSandbox();
  vm.runInContext(loadControllerScript(projectRoot), runtime.sandbox);
  const api = runtime.sandbox.window.ChatGPTLikeOutput;
  assert(api && typeof api.createStreamingText === "function", "missing createStreamingText");

  const updates = [];
  const controller = api.createStreamingText({
    enabled: true,
    minDelay: 8,
    maxDelay: 24,
    resetKey: 1,
    onUpdate(displayContent, state) {
      updates.push({
        text: displayContent,
        done: !!state.done,
        remaining: state.remaining
      });
    }
  });

  controller.setContent("Hello world, this should flow smoothly.", {
    enabled: true,
    resetKey: 1
  });

  runtime.flushFrames(1);
  assert(updates.length > 0, "controller did not emit after first frame");
  assert(updates[updates.length - 1].text.length > 0, "controller did not advance visible content");

  let previousLength = 0;
  for (const update of updates) {
    assert(update.text.length >= previousLength, "visible content moved backward");
    previousLength = update.text.length;
  }

  runtime.flushFrames(40);
  assert(
    updates[updates.length - 1].text === "Hello world, this should flow smoothly.",
    "controller did not catch up to target content"
  );
  assert(updates[updates.length - 1].done === true, "controller did not mark completion");

  const longText = "0123456789 ".repeat(120);
  const frameLengths = [];
  const longUpdates = [];
  const longController = api.createStreamingText({
    enabled: true,
    minDelay: 8,
    maxDelay: 24,
    resetKey: 10,
    onUpdate(displayContent, state) {
      longUpdates.push({
        text: displayContent,
        done: !!state.done
      });
    }
  });
  longController.setContent(longText, {
    enabled: true,
    resetKey: 10
  });
  for (let i = 0; i < 6; i += 1) {
    runtime.flushFrames(1);
    frameLengths.push(longUpdates.length ? longUpdates[longUpdates.length - 1].text.length : 0);
  }
  const frameDeltas = frameLengths.map((value, index) => value - (index === 0 ? 0 : frameLengths[index - 1]));
  assert(frameDeltas[0] > 0, "long backlog did not start advancing on first frame");
  assert(frameDeltas.slice(0, 4).every(delta => delta > 0), "long backlog did not advance on consecutive frames");
  assert(Math.max(...frameDeltas) <= 48, `controller revealed too much text in one frame: ${Math.max(...frameDeltas)}`);

  controller.setContent("A much longer follow target that should accelerate as backlog grows.", {
    enabled: true,
    resetKey: 1
  });
  runtime.flushFrames(3);
  const midText = updates[updates.length - 1].text;
  assert(midText.length > 0, "controller failed to restart progression for appended content");
  assert(
    midText.length < "A much longer follow target that should accelerate as backlog grows.".length,
    "controller jumped to the full target too early"
  );

  controller.flush();
  assert(
    updates[updates.length - 1].text === "A much longer follow target that should accelerate as backlog grows.",
    "flush did not reveal full content immediately"
  );
  assert(updates[updates.length - 1].done === true, "flush did not mark completion");

  controller.setContent("Reset key should restart visible content.", {
    enabled: true,
    resetKey: 2
  });
  const resetStart = updates[updates.length - 1].text;
  assert(resetStart === "", "reset key change did not clear visible content before replay");
  runtime.flushFrames(2);
  const resumed = updates[updates.length - 1].text;
  assert(resumed.length > 0, "controller did not resume after reset");
  assert(resumed.length < "Reset key should restart visible content.".length, "reset replay advanced too far in two frames");

  console.log(`streaming text controller tests passed for ${projectRoot}`);
}

runSuite(path.join(__dirname, ".."));
