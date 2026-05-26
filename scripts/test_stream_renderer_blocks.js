const fs = require("fs");
const path = require("path");
const vm = require("vm");

const source = fs.readFileSync(path.join(__dirname, "..", "static", "js", "stream-output-renderer.js"), "utf8");

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function createSandbox() {
  const nodes = [];
  const classList = () => ({
    values: new Set(),
    toggle(name, enabled) {
      if (enabled) this.values.add(name);
      else this.values.delete(name);
    },
    contains(name) {
      return this.values.has(name);
    },
    add(name) {
      this.values.add(name);
    },
    remove(name) {
      this.values.delete(name);
    }
  });

  const sandbox = {
    window: {},
    document: {
      body: {
        appendChild(node) {
          node.isConnected = true;
          nodes.push(node);
        }
      },
      createElement(tag) {
        return {
          tag,
          className: "",
          style: { cssText: "" },
          innerHTML: "",
          textContent: "",
          dataset: {},
          childNodes: [],
          children: [],
          isConnected: false,
          clientWidth: 640,
          classList: classList(),
          appendChild(child) {
            child.parentNode = this;
            child.isConnected = true;
            this.childNodes.push(child);
            this.children.push(child);
            this.innerHTML += child.outerHTML || child.innerHTML || child.textContent || "";
            return child;
          },
          replaceChildren(...children) {
            this.childNodes = [];
            this.children = [];
            this.innerHTML = "";
            children.forEach(child => this.appendChild(child));
          },
          remove() {
            this.isConnected = false;
          },
          querySelectorAll() {
            return [];
          }
        };
      }
    },
    Date,
    Math,
    Promise,
    setTimeout(fn) {
      fn();
      return 1;
    },
    clearTimeout() {},
    requestAnimationFrame(fn) {
      fn(Date.now());
      return 1;
    },
    cancelAnimationFrame() {}
  };
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox);
  return sandbox;
}

async function run() {
  const sandbox = createSandbox();
  let fullRenders = 0;
  let blockRenders = 0;
  const renderedBlocks = [];

  const bubble = sandbox.document.createElement("div");
  bubble.className = "bubble";
  bubble.isConnected = true;

  const renderer = sandbox.window.CodexStreamOutput.createRenderer({
    getContextKey: () => "user|conversation",
    buildAssistantBubbleHtml: (text, _sources, finalRender) => {
      fullRenders += 1;
      return `${finalRender ? "final" : "full"}:${text}`;
    },
    buildAssistantContentHtml: (text) => {
      blockRenders += 1;
      renderedBlocks.push(text);
      return `<p>${text}</p>`;
    },
    prepareAssistantBubbleDom: () => {},
    hasRenderableMathInHtml: () => false,
    revealPendingMathSources: () => {},
    typesetMathNow: async () => {},
    queueStreamingMathTypeset: () => {}
  });

  const first = ["alpha", "beta", "gamma"].join("\n\n");
  const second = ["alpha", "beta", "gamma delta"].join("\n\n");

  await renderer.render(bubble, first, { finalRender: false, showCaret: true, sources: [] });
  await renderer.render(bubble, second, { finalRender: false, showCaret: true, sources: [] });

  assert(fullRenders === 0, `streaming block path should not use full bubble renders, saw ${fullRenders}`);
  assert(renderedBlocks.filter(block => block === "alpha\n\n").length === 1, "first stable block was re-rendered");
  assert(renderedBlocks.filter(block => block === "beta\n\n").length === 1, "second stable block was re-rendered");
  assert(/gamma delta/.test(bubble.innerHTML), "updated tail block was not committed");

  await renderer.render(bubble, second, { finalRender: true, showCaret: false, sources: [] });
  assert(fullRenders === 1, "final render should use full bubble render once");

  const api = sandbox.window.CodexStreamOutput;
  assert(api.repairStreamingMarkdown("```js\nconst x = 1").endsWith("\n```"), "open code fence should be repaired");
  assert(api.repairStreamingMarkdown("see [docs](https://example.com").includes("streamdown:incomplete-link"), "open link should be repaired");
  assert(api.splitStreamingMarkdownBlocks("a\n\nb\n\nc").length === 3, "paragraph blocks should split");

  const mathRenders = [];
  const mathBubble = sandbox.document.createElement("div");
  mathBubble.className = "bubble";
  mathBubble.isConnected = true;
  const mathRenderer = sandbox.window.CodexStreamOutput.createRenderer({
    getContextKey: () => "user|math-conversation",
    buildAssistantBubbleHtml: (text, _sources, finalRender) => `${finalRender ? "final" : "full"}:${text}`,
    buildAssistantContentHtml: (text) => {
      mathRenders.push(text);
      return `<p>${text}</p>`;
    },
    prepareAssistantBubbleDom: () => {},
    hasRenderableMathInHtml: (html) => html.includes("$$"),
    revealPendingMathSources: () => {},
    typesetMathNow: async () => {},
    queueStreamingMathTypeset: () => {}
  });
  const mathBase = "公式：\n$$\na^2+b^2=c^2\n$$\n\n第一段解释。";
  const mathLong = `${mathBase}\n\n${"后续长对话保持稳定。".repeat(80)}`;

  await mathRenderer.render(mathBubble, mathBase, { finalRender: false, showCaret: true, sources: [] });
  for (let i = 0; i < 80; i += 1) await Promise.resolve();
  const rendersAfterMathBase = mathRenders.length;
  await mathRenderer.render(mathBubble, mathLong, { finalRender: false, showCaret: true, sources: [] });
  for (let i = 0; i < 80; i += 1) await Promise.resolve();

  assert(
    mathRenders.slice(rendersAfterMathBase).filter(block => block.includes("a^2+b^2=c^2")).length === 0,
    "completed math block was re-rendered after long text appended"
  );
  assert(/后续长对话保持稳定/.test(mathBubble.innerHTML), "long text after math was not committed");

  console.log("stream renderer block tests passed");
}

run().catch((error) => {
  console.error(error);
  process.exit(1);
});
