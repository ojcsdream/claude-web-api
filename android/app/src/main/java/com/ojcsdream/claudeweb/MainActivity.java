package com.ojcsdream.claudeweb;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.ViewGroup;
import android.webkit.CookieManager;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebChromeClient.FileChooserParams;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;

import com.chaquo.python.PyObject;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

public class MainActivity extends Activity {
    private static final int FILE_CHOOSER_REQUEST = 1001;

    private WebView webView;
    private ValueCallback<Uri[]> filePathCallback;
    private final Handler mainHandler = new Handler(Looper.getMainLooper());

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        webView = new WebView(this);
        webView.setLayoutParams(new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT
        ));
        setContentView(webView);
        configureWebView();
        loadBundledFrontendShell("正在启动本地服务...");
        startLocalBackend();
    }

    @SuppressLint("SetJavaScriptEnabled")
    private void configureWebView() {
        CookieManager.getInstance().setAcceptCookie(true);
        CookieManager.getInstance().setAcceptThirdPartyCookies(webView, true);

        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setDatabaseEnabled(true);
        settings.setAllowFileAccess(true);
        settings.setAllowContentAccess(true);
        settings.setLoadWithOverviewMode(true);
        settings.setUseWideViewPort(true);
        settings.setMediaPlaybackRequiresUserGesture(false);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE);

        webView.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri uri = request.getUrl();
                String scheme = uri.getScheme();
                String host = uri.getHost();
                if ("127.0.0.1".equals(host) || "localhost".equals(host)) {
                    view.loadUrl(uri.toString());
                    return true;
                }
                openExternal(uri);
                return true;
            }
        });

        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(
                    WebView view,
                    ValueCallback<Uri[]> callback,
                    FileChooserParams params
            ) {
                if (filePathCallback != null) {
                    filePathCallback.onReceiveValue(null);
                }
                filePathCallback = callback;
                Intent intent = params.createIntent();
                try {
                    startActivityForResult(intent, FILE_CHOOSER_REQUEST);
                } catch (Exception e) {
                    filePathCallback = null;
                    return false;
                }
                return true;
            }
        });
    }

    private void startLocalBackend() {
        new Thread(() -> {
            try {
                if (!Python.isStarted()) {
                    Python.start(new AndroidPlatform(this));
                }
                Python py = Python.getInstance();
                PyObject server = py.getModule("android_server");
                String assetZip = copyBundledAppZip();
                String appDir = server.callAttr(
                        "prepare",
                        assetZip,
                        getFilesDir().getAbsolutePath()
                ).toString();
                mainHandler.post(() -> showStartupPage("资源已解包，正在启动本地后端..."));
                String localUrl = server.callAttr(
                        "start_prepared",
                        appDir,
                        "127.0.0.1",
                        8765
                ).toString();
                String ready = waitForBackend(server, localUrl);
                mainHandler.post(() -> {
                    if ("ready".equals(ready)) {
                        webView.loadUrl(localUrl);
                    } else {
                        showStartupError(ready);
                    }
                });
            } catch (Exception e) {
                mainHandler.post(() -> showStartupError(e.toString()));
            }
        }, "claude-web-startup").start();
    }

    private String waitForBackend(PyObject server, String localUrl) {
        long deadline = System.currentTimeMillis() + 90_000L;
        String last = "";
        while (System.currentTimeMillis() < deadline) {
            String ready = server.callAttr("wait_until_ready", localUrl, 1.0).toString();
            if ("ready".equals(ready)) {
                return "ready";
            }
            String status = server.callAttr("status").toString();
            last = ready + "\n\n启动阶段: " + status;
            String statusText = status;
            mainHandler.post(() -> showStartupPage("正在启动本地后端...\n\n" + statusText));
            try {
                Thread.sleep(1000L);
            } catch (InterruptedException ignored) {
                break;
            }
        }
        return last.isEmpty() ? "启动超时" : last;
    }

    private String copyBundledAppZip() throws Exception {
        File out = new File(getFilesDir(), "claude-web.zip");
        try (InputStream in = getAssets().open("claude-web.zip");
             FileOutputStream fos = new FileOutputStream(out)) {
            byte[] buffer = new byte[64 * 1024];
            int read;
            while ((read = in.read(buffer)) != -1) {
                fos.write(buffer, 0, read);
            }
        }
        return out.getAbsolutePath();
    }

    private void loadBundledFrontendShell(String statusText) {
        showStartupPage(statusText);
    }

    private void showStartupPage(String statusText) {
        String html = "<html><head><meta name='viewport' content='width=device-width,initial-scale=1'>"
                + "<style>"
                + "html,body{height:100%;margin:0;background:#101418;color:#eef2f5;font-family:sans-serif;}"
                + "body{display:flex;align-items:center;justify-content:center;}"
                + ".wrap{width:min(86vw,420px);}"
                + ".brand{font-size:26px;font-weight:700;margin-bottom:8px;}"
                + ".sub{color:#aeb8c2;line-height:1.5;margin-bottom:22px;}"
                + ".bar{height:4px;background:#2b333b;overflow:hidden;border-radius:4px;margin-bottom:18px;}"
                + ".bar:before{content:'';display:block;width:42%;height:100%;background:#58a6ff;animation:move 1.2s infinite ease-in-out;}"
                + "pre{white-space:pre-wrap;line-height:1.45;color:#c9d1d9;background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px;max-height:32vh;overflow:auto;}"
                + "@keyframes move{0%{transform:translateX(-100%)}50%{transform:translateX(90%)}100%{transform:translateX(240%)}}"
                + "</style></head><body><main class='wrap'>"
                + "<div class='brand'>Claude Web</div>"
                + "<div class='sub'>正在启动本机前端和后端，完成后会自动进入应用。</div>"
                + "<div class='bar'></div>"
                + "<pre>" + escapeHtml(statusText) + "</pre>"
                + "</main></body></html>";
        try {
            InputStream in = getAssets().open("claude-web.zip");
            in.close();
            webView.loadDataWithBaseURL("file:///android_asset/", html, "text/html", "UTF-8", null);
        } catch (Exception e) {
            webView.loadData(html, "text/html", "UTF-8");
        }
    }

    private void showStartupError(String message) {
        String html = "<html><body style='font-family:sans-serif;padding:24px'>"
                + "<h3>本地服务启动失败</h3>"
                + "<p>前端已内置在 APK 中，但本地后端没有成功监听。请截图这段错误。</p>"
                + "<pre style='white-space:pre-wrap'>"
                + escapeHtml(message)
                + "</pre></body></html>";
        webView.loadData(html, "text/html", "UTF-8");
    }

    private String escapeHtml(String value) {
        return value == null
                ? ""
                : value.replace("&", "&amp;")
                        .replace("<", "&lt;")
                        .replace(">", "&gt;")
                        .replace("\"", "&quot;");
    }

    private void openExternal(Uri uri) {
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, uri));
        } catch (Exception ignored) {
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode != FILE_CHOOSER_REQUEST || filePathCallback == null) {
            return;
        }

        Uri[] results = null;
        if (resultCode == RESULT_OK && data != null) {
            if (data.getClipData() != null) {
                int count = data.getClipData().getItemCount();
                results = new Uri[count];
                for (int i = 0; i < count; i++) {
                    results[i] = data.getClipData().getItemAt(i).getUri();
                }
            } else if (data.getData() != null) {
                results = new Uri[]{data.getData()};
            }
        }

        filePathCallback.onReceiveValue(results);
        filePathCallback = null;
    }

    @Override
    public void onBackPressed() {
        if (webView != null && webView.canGoBack()) {
            webView.goBack();
            return;
        }
        super.onBackPressed();
    }
}
