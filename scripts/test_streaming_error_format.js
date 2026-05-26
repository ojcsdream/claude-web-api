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
vm.runInContext(extractFunction("formatStreamRequestError"), sandbox);

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

assert(
  sandbox.formatStreamRequestError({ message: "HTTP 500: {\"detail\":\"bad\"}" }, "请求") === "请求失败（HTTP 500）",
  "HTTP errors should be reduced to a short label"
);

assert(
  sandbox.formatStreamRequestError({ message: "AbortError: The operation was aborted." }, "重新生成") === "已暂停本次回复。",
  "Abort errors should map to the pause copy"
);

assert(
  sandbox.formatStreamRequestError("unexpected payload", "重新生成") === "重新生成失败",
  "generic failures should stay short"
);

console.log("streaming error format tests passed");
