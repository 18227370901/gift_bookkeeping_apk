import os
import sys
import time
import threading
import socket

# ==================== 本地 Flask 服务启动 ====================
# 移动端架构：Flask 后端在设备本地运行（127.0.0.1），WebView 加载本地页面
# 桌面端调试：自动打开浏览器访问本地服务

FLASK_PORT = 8765  # 本地服务端口（避免与常用端口冲突）

def find_free_port(start=8765, end=9999):
    """在指定范围内查找可用端口"""
    for port in range(start, end):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(('127.0.0.1', port))
            sock.close()
            return port
        except OSError:
            continue
    return start

def start_flask_server(port):
    """在后台线程中启动 Flask 服务器"""
    try:
        # 设置环境变量，确保使用本地 SQLite
        os.environ.setdefault('DATABASE_URL', '')  # 空值降级为 SQLite
        os.environ.setdefault('ADMIN_USER', 'admin')
        os.environ.setdefault('ADMIN_PASS', 'admin123')

        # 导入 Flask 应用（此时会自动执行 init_database 初始化数据库）
        from app import app

        # 在后台线程中运行 Flask
        def _run():
            app.run(host='127.0.0.1', port=port, debug=False, use_reloader=False, threaded=True)

        flask_thread = threading.Thread(target=_run, daemon=True)
        flask_thread.start()

        # 等待 Flask 服务器就绪
        for _ in range(30):
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(1)
                result = sock.connect_ex(('127.0.0.1', port))
                sock.close()
                if result == 0:
                    print(f"[Main] Flask 服务已在 127.0.0.1:{port} 启动")
                    return True
            except Exception:
                pass
            time.sleep(0.3)

        print("[Main] Flask 服务启动超时")
        return False
    except Exception as e:
        print(f"[Main] Flask 服务启动异常: {e}")
        return False

# ==================== Kivy + Android WebView 容器 ====================

try:
    from kivy.app import App
    from kivy.uix.widget import Widget
    from kivy.clock import Clock
    from kivy.core.window import Window
    from kivy.utils import platform
    IS_KIVY = True
except ImportError:
    IS_KIVY = False


class GiftBookkeepingApp(App if IS_KIVY else object):
    def build(self):
        Window.clearcolor = (0.96, 0.96, 0.98, 1)
        root = Widget()

        # 先启动 Flask 服务
        global FLASK_PORT
        FLASK_PORT = find_free_port(FLASK_PORT)
        flask_ok = start_flask_server(FLASK_PORT)
        self.target_url = f"http://127.0.0.1:{FLASK_PORT}/"

        if not flask_ok:
            print("[Main] Flask 启动失败，将尝试加载备用页面")
            self.target_url = f"http://127.0.0.1:{FLASK_PORT}/"

        if platform == 'android':
            Clock.schedule_once(self.init_android_webview, 1.0)
        elif IS_KIVY:
            Clock.schedule_once(self.open_desktop_browser, 1.5)
        else:
            self.open_desktop_browser()
        return root

    def init_android_webview(self, *args):
        try:
            from jnius import autoclass, PythonJavaClass, java_method

            PythonActivity = autoclass('org.kivy.android.PythonActivity')
            activity = PythonActivity.mActivity
            WebView = autoclass('android.webkit.WebView')
            WebSettings = autoclass('android.webkit.WebSettings')
            View = autoclass('android.view.View')
            LayoutParams = autoclass('android.view.ViewGroup$LayoutParams')
            CookieManager = autoclass('android.webkit.CookieManager')

            class SafeWebClient(PythonJavaClass):
                __javainterfaces__ = ['android/webkit/WebViewClient']
                __javacontext__ = 'app'

                def __init__(self, target_url):
                    super(SafeWebClient, self).__init__()
                    self.target_url = target_url

                @java_method('(Landroid/webkit/WebView;Ljava/lang/String;)Z')
                def shouldOverrideUrlLoading(self, view, url):
                    # 允许本地 URL 和内部导航
                    if url.startswith('http://127.0.0.1') or url.startswith('http://localhost'):
                        return False
                    # 外部链接在 WebView 内加载
                    view.loadUrl(url)
                    return True

                @java_method('(Landroid/webkit/WebView;Landroid/webkit/SslErrorHandler;Landroid/net/http/SslError;)V')
                def onReceivedSslError(self, view, handler, error):
                    handler.proceed()

                @java_method('(Landroid/webkit/WebView;ILjava/lang/String;Ljava/lang/String;)V')
                def onReceivedError(self, view, errorCode, description, failingUrl):
                    pass

            class CustomChromeClient(PythonJavaClass):
                __javainterfaces__ = ['android/webkit/WebChromeClient']
                __javacontext__ = 'app'

                def __init__(self):
                    super(CustomChromeClient, self).__init__()

                @java_method('(Landroid/webkit/WebView;I)V')
                def onProgressChanged(self, view, newProgress):
                    pass

                @java_method('(Landroid/webkit/ConsoleMessage;)Z')
                def onConsoleMessage(self, consoleMessage):
                    return True

            class WebViewInitRunnable(PythonJavaClass):
                __javainterfaces__ = ['java/lang/Runnable']
                __javacontext__ = 'app'

                def __init__(self, activity, url):
                    super(WebViewInitRunnable, self).__init__()
                    self.activity = activity
                    self.url = url

                @java_method('()V')
                def run(self):
                    try:
                        webview = WebView(self.activity)
                        settings = webview.getSettings()

                        settings.setJavaScriptEnabled(True)
                        settings.setDomStorageEnabled(True)
                        settings.setDatabaseEnabled(True)
                        settings.setAllowFileAccess(True)
                        settings.setAllowContentAccess(True)
                        settings.setUseWideViewPort(True)
                        settings.setLoadWithOverviewMode(True)
                        settings.setSupportZoom(True)
                        settings.setBuiltInZoomControls(False)
                        settings.setDisplayZoomControls(False)
                        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW)
                        settings.setCacheMode(WebSettings.LOAD_DEFAULT)
                        # 适配手机视口
                        try:
                            settings.setUserAgentString("Mozilla/5.0 (Linux; Android) GiftBookkeeping/3.0")
                        except Exception:
                            pass

                        try:
                            cookie_manager = CookieManager.getInstance()
                            cookie_manager.setAcceptCookie(True)
                            cookie_manager.setAcceptThirdPartyCookies(webview, True)
                        except Exception:
                            pass

                        webview.setWebViewClient(SafeWebClient(self.url))
                        webview.setWebChromeClient(CustomChromeClient())
                        webview.setScrollBarStyle(View.SCROLLBARS_INSIDE_OVERLAY)
                        webview.setFocusable(True)
                        webview.setFocusableInTouchMode(True)

                        params = LayoutParams(
                            LayoutParams.MATCH_PARENT,
                            LayoutParams.MATCH_PARENT
                        )
                        self.activity.addContentView(webview, params)
                        webview.loadUrl(self.url)
                        webview.requestFocus()
                    except Exception as ex:
                        print("Error creating webview:", ex)

            activity.runOnUiThread(WebViewInitRunnable(activity, self.target_url))

        except Exception as e:
            print("Android WebView Exception:", e)

    def open_desktop_browser(self, *args):
        try:
            import webbrowser
            webbrowser.open(self.target_url if hasattr(self, 'target_url') else f"http://127.0.0.1:{FLASK_PORT}/")
        except Exception:
            pass


if __name__ == '__main__':
    if IS_KIVY:
        GiftBookkeepingApp().run()
    else:
        # 桌面端：启动 Flask 并打开浏览器
        FLASK_PORT = find_free_port(FLASK_PORT)
        start_flask_server(FLASK_PORT)
        import webbrowser
        webbrowser.open(f"http://127.0.0.1:{FLASK_PORT}/")
        # 保持主线程存活
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("服务已停止")
