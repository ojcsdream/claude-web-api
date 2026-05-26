const fs = require("fs");
const path = require("path");
const vm = require("vm");

const source = fs.readFileSync(path.join(__dirname, "..", "static", "js", "stream-output-renderer.js"), "utf8");

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

async function run() {
  let syncTypesetCalls = 0;
  let deferredTypesetCalls = 0;

  const bubble = {
    className: "bubble",
    clientWidth: 640,
    innerHTML: "",
    isConnected: true,
    classList: {
      toggle() {}
    }
  };

  const sandbox = {
    window: {},
    document: {
      body: {
        appendChild(node) {
          node.isConnected = true;
        }
      },
      createElement() {
        return {
          className: "",
          style: { cssText: "" },
          innerHTML: "",
          isConnected: false,
          clientWidth: 640,
          remove() { this.isConnected = false; }
        };
      }
    },
    Date,
    Math,
    Promise,
    requestAnimationFrame(cb) {
      cb(Date.now());
      return 1;
    },
    cancelAnimationFrame() {},
    setTimeout(fn) {
      fn();
      return 1;
    },
    clearTimeout() {}
  };

  vm.createContext(sandbox);
  vm.runInContext(source, sandbox);

  const renderer = sandbox.window.CodexStreamOutput.createRenderer({
    getContextKey: () => "u|c",
    buildAssistantBubbleHtml: (text) => text,
    prepareAssistantBubbleDom: () => {},
    hasRenderableMathInHtml: (html) => html.includes("\\("),
    revealPendingMathSources: () => {},
    typesetMathNow: async () => {
      syncTypesetCalls += 1;
    },
    queueStreamingMathTypeset: () => {
      deferredTypesetCalls += 1;
    }
  });

  await renderer.render(bubble, "math \\(x+1\\)", { finalRender: false, showCaret: true, sources: [] });
  assert(syncTypesetCalls === 0, "streaming render should not wait for sync math typeset");
  assert(deferredTypesetCalls >= 1, "streaming render should queue deferred math typeset");

  await renderer.render(bubble, "math \\(x+1\\)", { finalRender: true, showCaret: false, sources: [] });
  assert(syncTypesetCalls === 1, "final render should perform sync math typeset");

  console.log("stream renderer math deferral test passed");
}

run().catch((error) => {
  console.error(error);
  process.exit(1);
});
