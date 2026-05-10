# Claude Web Android

This Android project builds an installable WebView APK for Claude Web.

The APK loads the deployed web app URL configured by the Gradle property `appUrl`.
Default:

```text
https://kindling-shaft-creamer.ngrok-free.dev
```

Build locally:

```bash
gradle assembleDebug -PappUrl="https://your-server.example.com"
```

The GitHub Actions workflow publishes an APK when a tag matching `android-v*` is pushed.
