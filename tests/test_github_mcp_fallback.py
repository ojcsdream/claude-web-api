import ast
import unittest
from pathlib import Path


def load_function(path: str, name: str):
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            module = ast.Module(body=[node], type_ignores=[])
            ast.fix_missing_locations(module)
            ns = {}
            exec(compile(module, path, "exec"), ns)
            return ns[name]
    raise AssertionError(f"{name} not found")


class GithubMcpFallbackTest(unittest.TestCase):
    def test_empty_github_mcp_result_stays_on_mcp(self):
        build_empty_github_mcp_result = load_function("app.py", "build_empty_github_mcp_result")
        observation, sources, plan = build_empty_github_mcp_result(["https://github.com/owner/private-repo"])

        self.assertEqual(plan["tool"], "github_mcp")
        self.assertEqual(sources[0]["provider"], "github-mcp")
        self.assertIn("GitHub MCP 已被触发", observation)


if __name__ == "__main__":
    unittest.main()
