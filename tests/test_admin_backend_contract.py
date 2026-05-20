import importlib
import os
import sys
import tempfile
import unittest

from fastapi.testclient import TestClient


def load_app(base_dir, db_path):
    os.environ["CLAUDE_WEB_BASE_DIR"] = base_dir
    os.environ["CLAUDE_WEB_DB_PATH"] = db_path
    os.makedirs(base_dir, exist_ok=True)
    for name in ("admin_backend", "app", "db", "config"):
        sys.modules.pop(name, None)

    import db
    import app

    importlib.reload(db)
    importlib.reload(app)
    return app


class AdminBackendContractTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        static_dir = os.path.join(self.tmp.name, "static")
        os.makedirs(static_dir, exist_ok=True)
        uploads_dir = os.path.join(self.tmp.name, "uploads")
        os.makedirs(uploads_dir, exist_ok=True)
        with open(os.path.join(static_dir, "admin.html"), "w", encoding="utf-8") as fh:
            fh.write("admin")
        with open(os.path.join(static_dir, "admin-live.html"), "w", encoding="utf-8") as fh:
            fh.write("admin-live")
        db_path = os.path.join(self.tmp.name, "chat_multi.db")
        self.app_module = load_app(self.tmp.name, db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_admin_routes_keep_contract(self):
        client = TestClient(self.app_module.app)

        hint = client.get("/api/admin/token-hint")
        self.assertEqual(hint.status_code, 200)
        self.assertTrue(hint.json()["ok"])

        unauthorized = client.get("/api/admin/stats")
        self.assertEqual(unauthorized.status_code, 401)

        authorized = client.get("/api/admin/stats", headers={"X-Admin-Token": "114514"})
        self.assertEqual(authorized.status_code, 200, authorized.text)
        payload = authorized.json()
        self.assertIn("total", payload)
        self.assertIn("errors", payload)
        self.assertIn("chat_count", payload)
        self.assertIn("server_time", payload)

    def test_multi_user_auth_contract_still_blocks_guest(self):
        client = TestClient(self.app_module.app)
        response = client.get("/api/auth/me")
        self.assertEqual(response.status_code, 401)

    def test_admin_pages_still_resolve(self):
        client = TestClient(self.app_module.app)
        self.assertEqual(client.get("/admin").status_code, 200)
        self.assertEqual(client.get("/admin/live").status_code, 200)


if __name__ == "__main__":
    unittest.main()
