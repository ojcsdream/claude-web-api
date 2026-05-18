# User Settings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add left-sidebar user settings for account profile edits, password changes, local preferences, and logout.

**Architecture:** Extend the existing FastAPI auth module with two authenticated account endpoints and small database helpers. Add a settings modal directly in `static/index.html`, reusing existing auth and theme state.

**Tech Stack:** FastAPI, SQLite, Pydantic, vanilla HTML/CSS/JavaScript.

---

### Task 1: Backend Account Endpoints

**Files:**
- Modify: `schemas.py`
- Modify: `db.py`
- Modify: `app.py`
- Create: `tests/test_user_settings_api.py`

- [ ] Add request schemas for profile updates and password changes.
- [ ] Add DB helpers to update a user and fetch password hash by user id.
- [ ] Add tests for profile update conflicts and password change validation.
- [ ] Add `PATCH /api/auth/me` and `POST /api/auth/change-password`.
- [ ] Run the backend tests.

### Task 2: Sidebar Settings UI

**Files:**
- Modify: `static/index.html`

- [ ] Add a gear button in the drawer header.
- [ ] Add settings modal markup.
- [ ] Add modal CSS for account, security, preferences, status messages, and responsive layout.
- [ ] Wire modal open/close, form population, API calls, local preferences, and logout.
- [ ] Run static syntax checks and a browser smoke check if practical.
