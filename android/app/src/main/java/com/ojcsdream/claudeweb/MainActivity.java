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
    private static final String LOCAL_URL = "http://127.0.0.1:8765/";

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
        webView.loadData(
                "<html><body style='font-family:sans-serif;padding:24px'>正在启动本地服务...</body></html>",
                "text/html",
                "UTF-8"
        );
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
                String localUrl = server.callAttr(
                        "start",
                        assetZip,
                        getFilesDir().getAbsolutePath(),
                        "127.0.0.1",
                        8765
                ).toString();
                String ready = server.callAttr("wait_until_ready", localUrl, 25.0).toString();
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

    private void showStartupError(String message) {
        String html = "<html><body style='font-family:sans-serif;padding:24px'>"
                + "<h3>本地服务启动失败</h3><pre style='white-space:pre-wrap'>"
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
