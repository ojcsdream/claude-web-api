const fs = require("fs");
const path = require("path");
const vm = require("vm");

const html = fs.readFileSync(path.join(__dirname, "..", "static", "index.html"), "utf8");

function extractConst(name) {
  const marker = `const ${name} = `;
  const start = html.indexOf(marker);
  if (start === -1) throw new Error(`missing const ${name}`);
  const end = html.indexOf(";", start);
  return html.slice(start, end + 1);
}

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

const sandbox = {
  activeToolStatuses: [],
  activeStreamingSources: []
};
vm.createContext(sandbox);
vm.runInContext(
  [
    extractConst("STREAM_STATUS_RE"),
    extractConst("STREAM_SOURCES_RE"),
    extractConst("STREAM_PARTIAL_MARKER_RE"),
    extractConst("STREAM_REGENERATE_DEBUG_RE"),
    extractFunction("stripStreamingStatusMarkers")
  ].join("\n"),
  sandbox
);

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

const fullMarker = sandbox.stripStreamingStatusMarkers(
  '[[STATUS:planning_search]]{"debug":true}[[SOURCES:[{"index":1,"title":"Doc","url":"https://example.com"}]]]最终答案'
);
assert(fullMarker === '{"debug":true}最终答案', "full known markers should be removed while preserving non-marker text");
assert(Array.isArray(sandbox.activeStreamingSources) && sandbox.activeStreamingSources.length === 1, "sources marker should populate active sources");
assert(sandbox.activeToolStatuses.includes("planning_search"), "status marker should populate active statuses");

const partialStatus = sandbox.stripStreamingStatusMarkers('前缀[[STATUS:planning_search');
assert(partialStatus === "前缀", "partial status marker should not leak into visible content");

const partialSources = sandbox.stripStreamingStatusMarkers('回答[[SOURCES:[{"index":1');
assert(partialSources === "回答", "partial sources marker should not leak JSON fragments into visible content");

const partialVisionDebug = sandbox.stripStreamingStatusMarkers("【重新回答｜视觉直连｜图片数: 2｜模型: test｜接入商: demo】\n\n真正回答");
assert(partialVisionDebug === "真正回答", "vision regenerate debug preface should be hidden from visible content");

console.log("streaming marker cleanup tests passed");
