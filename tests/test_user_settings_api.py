import importlib
import os
import tempfile
import unittest

from fastapi.testclient import TestClient


def load_app(db_path):
    os.environ["CLAUDE_WEB_DB_PATH"] = db_path
    import db
    import app

    importlib.reload(db)
    importlib.reload(app)
    return app


def register(client, username="alice", email="alice@example.com", password="secret1"):
    import db

    db.db_save_email_verification_code(email, "register", "123456", db.now_ms() + 60_000)
    response = client.post(
        "/api/auth/register",
        json={
            "username": username,
            "email": email,
            "password": password,
            "confirm_password": password,
            "verification_code": "123456",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["user"]


class UserSettingsApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app_module = load_app(os.path.join(self.tmp.name, "settings-test.db"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_update_current_user_profile(self):
        client = TestClient(self.app_module.app)
        register(client)

        response = client.patch(
            "/api/auth/me",
            json={"username": "alice_new", "email": "alice@example.com"},
        )

        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertIs(data["ok"], True)
        self.assertEqual(data["user"]["username"], "alice_new")
        self.assertEqual(data["user"]["email"], "alice@example.com")

        me = client.get("/api/auth/me")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["user"]["username"], "alice_new")

    def test_update_current_user_email_requires_new_email_code(self):
        client = TestClient(self.app_module.app)
        user = register(client)
        import db

        response = client.patch(
            "/api/auth/me",
            json={"username": "alice", "email": "alice_new@example.com"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("修改邮箱需要先验证新邮箱", response.json()["detail"])

        db.db_save_email_verification_code("alice_new@example.com", "change_email", "112233", db.now_ms() + 60_000)
        response = client.patch(
            "/api/auth/me",
            json={"username": "alice", "email": "alice_new@example.com", "email_change_code": "112233"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["user"]["email"], "alice_new@example.com")

    def test_update_current_user_rejects_duplicate_email(self):
        client_one = TestClient(self.app_module.app)
        register(client_one, "alice", "alice@example.com")

        client_two = TestClient(self.app_module.app)
        register(client_two, "bob", "bob@example.com")

        response = client_two.patch(
            "/api/auth/me",
            json={"username": "bob", "email": "alice@example.com"},
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("邮箱已被占用", response.json()["detail"])

    def test_change_password_requires_email_code(self):
        client = TestClient(self.app_module.app)
        register(client, password="oldpass")

        response = client.post(
            "/api/auth/change-password",
            json={
                "verification_code": "000000",
                "new_password": "newpass1",
                "confirm_password": "newpass1",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("邮箱验证码不正确或已过期", response.json()["detail"])

    def test_change_password_allows_login_with_new_password(self):
        client = TestClient(self.app_module.app)
        user = register(client, password="oldpass")
        import db

        db.db_save_email_verification_code(user["email"], "change_password", "654321", db.now_ms() + 60_000)

        response = client.post(
            "/api/auth/change-password",
            json={
                "verification_code": "654321",
                "new_password": "newpass1",
                "confirm_password": "newpass1",
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIs(response.json()["ok"], True)

        fresh_client = TestClient(self.app_module.app)
        old_login = fresh_client.post("/api/auth/login", json={"username": "alice", "password": "oldpass"})
        self.assertEqual(old_login.status_code, 401)

        new_login = fresh_client.post("/api/auth/login", json={"username": "alice", "password": "newpass1"})
        self.assertEqual(new_login.status_code, 200, new_login.text)

    def test_send_password_change_code_uses_current_user_email(self):
        client = TestClient(self.app_module.app)
        user = register(client, password="oldpass")
        sent = {}

        def fake_send(email, code):
            sent["email"] = email
            sent["code"] = code

        self.app_module.send_verification_email = fake_send

        response = client.post("/api/auth/send-password-change-code")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(sent["email"], user["email"])
        self.assertRegex(sent["code"], r"^\d{6}$")

    def test_account_summary_is_scoped_to_current_user(self):
        client_one = TestClient(self.app_module.app)
        register(client_one, "alice", "alice@example.com")
        created = client_one.post("/api/conversations", json={"title": "Alice chat"})
        self.assertEqual(created.status_code, 200, created.text)
        conversation_id = created.json()["id"]
        client_one.post(
            "/api/profiles",
            json={"name": "Alice API", "base_url": "https://example.com", "auth_token": "k", "model": "m"},
        )
        prompt_response = client_one.post("/api/system-prompts", json={"title": "Alice prompt", "content": "Be concise", "enabled": True})
        self.assertEqual(prompt_response.status_code, 200, prompt_response.text)

        import db

        db.db_add_message(conversation_id, "user", "hello")
        db.db_add_message(conversation_id, "assistant", "hi")

        client_two = TestClient(self.app_module.app)
        register(client_two, "bob", "bob@example.com")

        alice_summary = client_one.get("/api/auth/account-summary")
        bob_summary = client_two.get("/api/auth/account-summary")

        self.assertEqual(alice_summary.status_code, 200, alice_summary.text)
        self.assertEqual(bob_summary.status_code, 200, bob_summary.text)
        self.assertEqual(alice_summary.json()["summary"]["conversations"], 1)
        self.assertEqual(alice_summary.json()["summary"]["messages"], 2)
        self.assertEqual(alice_summary.json()["summary"]["api_profiles"], 1)
        self.assertEqual(alice_summary.json()["summary"]["system_prompts"], 1)
        self.assertEqual(bob_summary.json()["summary"]["conversations"], 0)
        self.assertEqual(bob_summary.json()["summary"]["messages"], 0)

    def test_logout_other_sessions_keeps_current_and_other_users(self):
        client_one = TestClient(self.app_module.app)
        register(client_one, "alice", "alice@example.com", "secret1")

        client_two = TestClient(self.app_module.app)
        login_two = client_two.post("/api/auth/login", json={"username": "alice", "password": "secret1"})
        self.assertEqual(login_two.status_code, 200, login_two.text)

        client_three = TestClient(self.app_module.app)
        register(client_three, "bob", "bob@example.com", "secret2")

        response = client_one.post("/api/auth/logout-other-sessions")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["closed_sessions"], 1)
        self.assertEqual(client_one.get("/api/auth/me").status_code, 200)
        self.assertEqual(client_two.get("/api/auth/me").status_code, 401)
        self.assertEqual(client_three.get("/api/auth/me").status_code, 200)

if __name__ == "__main__":
    unittest.main()
