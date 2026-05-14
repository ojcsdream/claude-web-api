const fs = require("fs");
const path = require("path");
const vm = require("vm");

const html = fs.readFileSync(path.join(__dirname, "..", "static", "index.html"), "utf8");

function extractFunction(name) {
  const marker = `function ${name}(`;
  const start = html.indexOf(marker);
  if (start === -1) throw new Error(`missing function ${name}`);

  const paramsEnd = html.indexOf(")", start);
  const braceStart = html.indexOf("{", paramsEnd);
  let depth = 0;
  for (let i = braceStart; i < html.length; i += 1) {
    if (html[i] === "{") depth += 1;
    if (html[i] === "}") depth -= 1;
    if (depth === 0) return html.slice(start, i + 1);
  }

  throw new Error(`unterminated function ${name}`);
}

const sandbox = {};
vm.createContext(sandbox);
vm.runInContext(
  [
    extractFunction("findLastUnclosedFence"),
    extractFunction("getClosedFenceRanges"),
    extractFunction("isInsideRanges"),
    extractFunction("findLastUnclosedMathDelimiter"),
    extractFunction("splitStableStreamingMarkdown")
  ].join("\n"),
  sandbox
);

class TextNode {
  constructor(value, parentElement = null) {
    this.nodeValue = value;
    this.parentElement = parentElement;
  }
}

class ElementNode {
  constructor(selectorMatches = []) {
    this.selectorMatches = selectorMatches;
  }

  closest(selector) {
    return this.selectorMatches.some(match => selector.split(",").map(s => s.trim()).includes(match))
      ? this
      : null;
  }
}

const textNodeFilter = {
  FILTER_ACCEPT: 1,
  FILTER_REJECT: 2,
  SHOW_TEXT: 4
};

function createDomSandbox(nodes) {
  const domSandbox = {
    NodeFilter: textNodeFilter,
    document: {
      createTreeWalker(root, _whatToShow, filter) {
        const accepted = root.nodes.filter(node => filter.acceptNode(node) === textNodeFilter.FILTER_ACCEPT);
        let index = -1;
        return {
          currentNode: null,
          nextNode() {
            index += 1;
            this.currentNode = accepted[index] || null;
            return Boolean(this.currentNode);
          }
        };
      }
    },
    root: { nodes }
  };
  vm.createContext(domSandbox);
  vm.runInContext(
    [
      "const RENDERABLE_MATH_RE = /\\$\\$[\\s\\S]+?\\$\\$|\\\\\\[[\\s\\S]+?\\\\\\]|\\\\\\([\\s\\S]+?\\\\\\)|\\\\begin\\{(?:equation|align|aligned|gather|multline|cases|matrix|pmatrix|bmatrix|vmatrix)\\*?\\}[\\s\\S]+?\\\\end\\{(?:equation|align|aligned|gather|multline|cases|matrix|pmatrix|bmatrix|vmatrix)\\*?\\}|\\$[^\\s$][\\s\\S]*?[^\\s\\\\]\\$/;",
      extractFunction("shouldSkipMathTextNode"),
      extractFunction("findMathTextNodes"),
      extractFunction("hasRenderableMath")
    ].join("\n"),
    domSandbox
  );
  return domSandbox;
}

function createBufferedRenderSandbox({ math = true } = {}) {
  const bubble = {
    className: "bubble",
    clientWidth: 640,
    innerHTML: "previous-rendered",
    isConnected: true,
    closest() { return null; }
  };
  const stagingNodes = [];
  const sandbox = {
    window: {},
    MathJax: math ? {
      typesetPromise(roots) {
        roots[0].innerHTML = roots[0].innerHTML.replace("\\(x+1\\)", "<mjx-container>x+1</mjx-container>");
        return Promise.resolve();
      },
      typesetClear() {}
    } : undefined,
    document: {
      body: {
        appendChild(node) {
          node.isConnected = true;
          stagingNodes.push(node);
        }
      },
      createElement(tag) {
        return {
          tag,
          className: "",
          style: { cssText: "" },
          innerHTML: "",
          clientWidth: 640,
          isConnected: false,
          closest() { return null; },
          querySelectorAll() { return []; },
          remove() { this.isConnected = false; }
        };
      },
      createTreeWalker(root, _whatToShow, filter) {
        const text = root.innerHTML || "";
        const parentElement = {
          closest(selector) {
            return selector.includes("code") && /<code/.test(text) ? this : null;
          }
        };
        const node = new TextNode(text.replace(/<[^>]*>/g, ""), parentElement);
        const accepted = filter.acceptNode(node) === textNodeFilter.FILTER_ACCEPT ? [node] : [];
        let index = -1;
        return {
          currentNode: null,
          nextNode() {
            index += 1;
            this.currentNode = accepted[index] || null;
            return Boolean(this.currentNode);
          }
        };
      }
    },
    NodeFilter: textNodeFilter,
    Promise,
    Math,
    getScrollTop: () => 0,
    getPageHeight: () => 1000,
    isNearBottom: () => true,
    followBottomDuringStreaming: () => {},
    bindThinkingPanels: () => {},
    injectCitationMarkers: () => {},
    enhanceRichText: () => {},
    enhanceCodeBlocks: () => {},
    applyMessageCollapse: () => {},
    renderMarkdown: text => text,
    renderStreamingMarkdown: text => text,
    renderThinkingHtml: () => "",
    renderToolStatusesHtml: () => "",
    renderSourcesStrip: () => "",
    AUTO_SCROLL_THRESHOLD: 160,
    mathRenderPromise: Promise.resolve(),
    mathRenderSeq: 0,
    streamingBubbleRenderSeq: 0,
    streamingLastCommittedHtml: ""
  };
  sandbox.window.MathJax = sandbox.MathJax;
  vm.createContext(sandbox);
  vm.runInContext(
    [
      "const RENDERABLE_MATH_RE = /\\$\\$[\\s\\S]+?\\$\\$|\\\\\\[[\\s\\S]+?\\\\\\]|\\\\\\([\\s\\S]+?\\\\\\)|\\\\begin\\{(?:equation|align|aligned|gather|multline|cases|matrix|pmatrix|bmatrix|vmatrix)\\*?\\}[\\s\\S]+?\\\\end\\{(?:equation|align|aligned|gather|multline|cases|matrix|pmatrix|bmatrix|vmatrix)\\*?\\}|\\$[^\\s$][\\s\\S]*?(?:[\\\\^_{}=+\\-*/]|\\\\[a-zA-Z]+)[\\s\\S]*?[^\\s\\\\]\\$/;",
      extractFunction("shouldSkipMathTextNode"),
      extractFunction("findMathTextNodes"),
      extractFunction("hasRenderableMath"),
      extractFunction("revealPendingMathSources"),
      extractFunction("typesetMathNow"),
      extractFunction("buildAssistantBubbleHtml"),
      extractFunction("prepareAssistantBubbleDom"),
      extractFunction("commitAssistantBubbleHtml"),
      extractFunction("renderAssistantBubbleBuffered")
    ].join("\n"),
    sandbox
  );
  sandbox.bubble = bubble;
  sandbox.stagingNodes = stagingNodes;
  return sandbox;
}

function createDeferredMarkdownSandbox() {
  const sandbox = {
    window: {
      marked: {
        parse(text) {
          return text;
        },
        Renderer: function Renderer() {
          this.link = () => "";
        },
        setOptions() {}
      }
    },
    marked: null,
    document: {
      createElement() {
        const content = {
          html: "",
          elements: [],
          querySelectorAll(selector) {
            if (selector !== ".math-source") return [];
            return this.elements;
          }
        };
        return {
          content,
          set innerHTML(value) {
            content.html = value;
            content.elements = [...content.html.matchAll(/<(span|div) class="([^"]*math-source[^"]*)" data-math-id="(\d+)"><\/\1>/g)]
              .map(match => ({
                dataset: { mathId: match[3] },
                classList: {
                  classes: new Set(match[2].split(/\s+/)),
                  add(cls) { this.classes.add(cls); }
                },
                attrs: {},
                textContent: "",
                setAttribute(name, value) { this.attrs[name] = value; },
                _match: match
              }));
          },
          get innerHTML() {
            let output = content.html;
            content.querySelectorAll(".math-source").forEach(el => {
              const className = [...el.classList.classes].join(" ");
              const attrs = Object.entries(el.attrs).map(([key, value]) => ` ${key}="${value}"`).join("");
              const replacement = `<${el._match[1]} class="${className}" data-math-id="${el.dataset.mathId}"${attrs}>${el.textContent}</${el._match[1]}>`;
              output = output.replace(el._match[0], replacement);
            });
            return output;
          }
        };
      }
    },
    escapeHtml(value) {
      return String(value || "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;");
    }
  };
  sandbox.marked = sandbox.window.marked;
  vm.createContext(sandbox);
  vm.runInContext(
    [
      extractFunction("protectCodeSpans"),
      extractFunction("restoreCodeSpans"),
      extractFunction("normalizeDisplayMathBlocks"),
      extractFunction("protectMathForMarkdown"),
      extractFunction("restoreMarkdownMath"),
      extractFunction("normalizeMathDelimiters"),
      extractFunction("configureMarked"),
      extractFunction("renderMarkdown"),
      extractFunction("renderMarkdownForDeferredMath")
    ].join("\n"),
    sandbox
  );
  return sandbox;
}

const cases = [
  {
    name: "plain text remains stable",
    input: "结论是普通文本。",
    stable: "结论是普通文本。",
    pending: ""
  },
  {
    name: "closed inline paren math remains stable",
    input: "令 \\(x^2 + 1\\) 成立，继续。",
    stable: "令 \\(x^2 + 1\\) 成立，继续。",
    pending: ""
  },
  {
    name: "unclosed inline paren math is held pending",
    input: "令 \\(x^2 + 1",
    stable: "令 ",
    pending: "\\(x^2 + 1"
  },
  {
    name: "closed display dollar math remains stable",
    input: "推导：\n$$\na^2+b^2=c^2\n$$\n完成。",
    stable: "推导：\n$$\na^2+b^2=c^2\n$$\n完成。",
    pending: ""
  },
  {
    name: "unclosed display dollar math is held pending",
    input: "推导：\n$$\na^2+b^2",
    stable: "推导：\n",
    pending: "$$\na^2+b^2"
  },
  {
    name: "unclosed single dollar math is held pending",
    input: "答案是 $E=mc",
    stable: "答案是 ",
    pending: "$E=mc"
  },
  {
    name: "closed single dollar math remains stable",
    input: "答案是 $E=mc^2$。",
    stable: "答案是 $E=mc^2$。",
    pending: ""
  },
  {
    name: "currency with one dollar sign remains stable",
    input: "价格是 $1.00，继续输出更多文本",
    stable: "价格是 $1.00，继续输出更多文本",
    pending: ""
  },
  {
    name: "open code fence is held pending before math scanning",
    input: "代码：\n```js\nconst x = \"$not math",
    stable: "代码：\n",
    pending: "```js\nconst x = \"$not math"
  },
  {
    name: "unclosed latex environment is held pending",
    input: "推导：\n\\begin{align}\na &= b + c",
    stable: "推导：\n",
    pending: "\\begin{align}\na &= b + c"
  },
  {
    name: "closed latex environment remains stable",
    input: "推导：\n\\begin{align}\na &= b + c\n\\end{align}\n完成。",
    stable: "推导：\n\\begin{align}\na &= b + c\n\\end{align}\n完成。",
    pending: ""
  },
  {
    name: "closed code fence with math-like text remains stable",
    input: "代码：\n```js\nconst x = \"$not math\";\n```\n完成。",
    stable: "代码：\n```js\nconst x = \"$not math\";\n```\n完成。",
    pending: ""
  }
];

const domCases = [
  {
    name: "plain dom text has no renderable math",
    nodes: [new TextNode("普通文本", new ElementNode())],
    expected: false
  },
  {
    name: "closed dom formula is renderable",
    nodes: [new TextNode("公式 \\(x+1\\) 完成", new ElementNode())],
    expected: true
  },
  {
    name: "code dom formula-like text is skipped",
    nodes: [new TextNode("const x = '$not math$';", new ElementNode(["code"]))],
    expected: false
  },
  {
    name: "pending streaming formula is skipped",
    nodes: [new TextNode("$$\na+b", new ElementNode([".streaming-math-pending"]))],
    expected: false
  },
  {
    name: "deferred hidden formula source is still renderable",
    nodes: [new TextNode("\\(x+1\\)", new ElementNode([".math-source-pending"]))],
    expected: true
  },
  {
    name: "currency is not treated as single-dollar math",
    nodes: [new TextNode("价格是 $1.00，折后 $0.80。", new ElementNode())],
    expected: false
  },
  {
    name: "single-dollar math with operator is renderable",
    nodes: [new TextNode("公式 $E=mc^2$ 完成", new ElementNode())],
    expected: true
  }
];

let failures = 0;
for (const testCase of cases) {
  const actual = sandbox.splitStableStreamingMarkdown(testCase.input);
  const ok = actual.stable === testCase.stable && actual.pending === testCase.pending;
  if (!ok) {
    failures += 1;
    console.error(`FAIL ${testCase.name}`);
    console.error("actual:", JSON.stringify(actual));
    console.error("expect:", JSON.stringify({ stable: testCase.stable, pending: testCase.pending }));
  }
}

for (const testCase of domCases) {
  const domSandbox = createDomSandbox(testCase.nodes);
  const actual = domSandbox.hasRenderableMath(domSandbox.root);
  if (actual !== testCase.expected) {
    failures += 1;
    console.error(`FAIL ${testCase.name}`);
    console.error("actual:", actual);
    console.error("expect:", testCase.expected);
  }
}

async function runAsyncCases() {
  const buffered = createBufferedRenderSandbox();
  const promise = buffered.renderAssistantBubbleBuffered(buffered.bubble, "公式 \\(x+1\\)", [], false, false);
  if (buffered.bubble.innerHTML !== "previous-rendered") {
    failures += 1;
    console.error("FAIL buffered math does not expose raw TeX before typeset");
    console.error("actual:", buffered.bubble.innerHTML);
  }
  await promise;
  if (!buffered.bubble.innerHTML.includes("mjx-container")) {
    failures += 1;
    console.error("FAIL buffered math commits rendered MathJax output");
    console.error("actual:", buffered.bubble.innerHTML);
  }

  const stale = createBufferedRenderSandbox();
  const stalePromise = stale.renderAssistantBubbleBuffered(stale.bubble, "公式 \\(x+1\\)", [], false, false);
  stale.streamingBubbleRenderSeq += 1;
  await stalePromise;
  if (stale.bubble.innerHTML !== "previous-rendered") {
    failures += 1;
    console.error("FAIL stale buffered math frame is discarded");
    console.error("actual:", stale.bubble.innerHTML);
  }

  const markdown = createDeferredMarkdownSandbox();
  const html = markdown.renderMarkdownForDeferredMath("公式 \\(x+1\\) 完成");
  if (!html.includes("math-source-pending") || !html.includes('aria-hidden="true"')) {
    failures += 1;
    console.error("FAIL deferred markdown hides raw formula source");
    console.error("actual:", html);
  }
}

runAsyncCases().then(() => {
  if (failures > 0) {
    process.exit(1);
  }

  console.log(`streaming math render tests passed (${cases.length + domCases.length + 4})`);
});
