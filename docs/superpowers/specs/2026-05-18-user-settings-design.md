# User Settings Design

## Goal

Add a gear settings entry in the left sidebar so signed-in users can manage basic account information and everyday preferences without leaving the chat UI.

## Scope

- Add a gear button in the left drawer.
- Open a settings modal from that button.
- Show current username, email, account creation date, and local app preferences.
- Let the current user update username and email with server validation.
- Let the current user change password by entering current password plus a new password confirmation.
- Keep theme and compact-message preference in browser localStorage.
- Keep logout available in settings.
- Add user-facing utility actions for exporting the active conversation, clearing local UI cache, clearing the current draft/attachments, and deleting the active conversation.

## API Design

- `PATCH /api/auth/me` updates `username` and `email` for the current session user.
- `POST /api/auth/change-password` changes password for the current session user.
- Both endpoints require the existing session cookie and return public user data only.
- Username validation matches registration rules.
- Email validation matches registration rules and rejects another user's email.
- Password changes require the current password, minimum 6 characters for the new password, and matching confirmation.

## UI Design

- The drawer header includes a small gear icon button.
- The settings modal uses the existing glass panel style and 8px-radius form controls.
- Sections are:
  - Account: username and email fields with save button.
  - Security: current password, new password, confirmation, and change button.
  - Preferences: theme selector and compact long-answer toggle, stored locally.
  - Data: active conversation export, active conversation delete, local cache cleanup, draft cleanup.
  - Session: logout button.

## Non-Goals

- Avatar upload.
- Email verification for changing email.
- Server-side preference storage.
- Account deletion.
