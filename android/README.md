# Claude Web Android

This Android project builds a local-first APK for Claude Web.

The APK embeds:

- the FastAPI backend
- the static frontend
- a private on-device SQLite database
- an on-device upload directory

On launch, the app starts the Python backend on `127.0.0.1:8765` and loads it in
the WebView. It does not require the ngrok/public web deployment to open the UI.
External model providers still require network access when configured in the app.

Build locally:

```bash
gradle assembleDebug
```

The GitHub Actions workflow builds the APK on demand and publishes an APK when a
tag matching `android-v*` is pushed.
