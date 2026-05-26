const fs = require("fs");
const path = require("path");
const vm = require("vm");

const html = fs.readFileSync(path.join(__dirname, "..", "static", "index.html"), "utf8");

function extractFunction(name) {
  const marker = `async function ${name}(`;
  const start = html.indexOf(marker);
  if (start === -1) throw new Error(`missing function ${name}`);
  const braceStart = html.indexOf("{", start);
  let depth = 0;
  for (let i = braceStart; i < html.length; i += 1) {
    if (html[i] === "{") depth += 1;
    if (html[i] === "}") depth -= 1;
    if (depth === 0) return html.slice(start, i + 1);
  }
  throw new Error(`unterminated function ${name}`);
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

async function run() {
  let capturedBody = null;
  let renderWebSearchButtonCalls = 0;
  let flushCalls = 0;
  let renderAllCalls = 0;
  let renderSendButtonStateCalls = 0;
  let sandbox;

  sandbox = {
    console,
    confirm: () => true,
    TextDecoder,
    TextEncoder,
    AbortController,
    messages: [
      { id: "u1", role: "user", content: "question" },
      { id: "a1", role: "assistant", content: "answer" }
    ],
    sending: false,
    activeConversationId: "conv-1",
    apiUrl: { value: "https://example.com" },
    apiKey: { value: "token" },
    apiModel: { value: "" },
    apiProtocol: { value: "responses" },
    providerName: () => "provider",
    activeSystemPromptText: () => "system",
    webSearchEnabled: null,
    defaultWebSearch: true,
    autoScrollEnabled: false,
    userScrollActive: true,
    activeStreamController: null,
    lastUserPromptForThinking: "",
    renderWebSearchButton: () => { renderWebSearchButtonCalls += 1; },
    renderSendButtonState: () => { renderSendButtonStateCalls += 1; },
    setThinkingContextFromInput: () => {},
    beginStreamingAssistantMessage: () => {},
    renderMessages: () => {},
    scheduleStreamingAssistantUpdate: () => {},
    flushStreamingAssistantUpdate: () => { flushCalls += 1; },
    loadMessages: async () => {},
    renderAll: () => { renderAllCalls += 1; },
    stopStreamingTypewriter: () => {},
    findMsgIdxById: (id) => sandbox.messages.findIndex((msg) => msg.id === id),
    fetch: async (_url, options) => {
      capturedBody = JSON.parse(options.body);
      return {
        ok: true,
        body: {
          getReader() {
            let done = false;
            return {
              async read() {
                if (done) return { done: true, value: undefined };
                done = true;
                return { done: false, value: new TextEncoder().encode("regenerated") };
              }
            };
          }
        }
      };
    }
  };

  vm.createContext(sandbox);
  vm.runInContext(`${extractFunction("regenerateFrom")}\nthis.regenerateFrom = regenerateFrom;`, sandbox);
  await sandbox.regenerateFrom("a1");

  assert(!!capturedBody, "fetch should be called");
  assert(capturedBody.web_search === true, "regenerate should use resolved web_search");
  assert(capturedBody.web_search_explicit === true, "regenerate should use resolved web_search_explicit");
  assert(sandbox.webSearchEnabled === null, "webSearchEnabled should reset to null");
  assert(renderWebSearchButtonCalls === 1, "renderWebSearchButton should run once");
  assert(flushCalls === 1, "stream flush should run once");
  assert(renderAllCalls === 1, "renderAll should run once");
  assert(renderSendButtonStateCalls >= 2, "send button state should update before and after");

  console.log("regenerate web search payload test passed");
}

run().catch((error) => {
  console.error(error);
  process.exit(1);
});
