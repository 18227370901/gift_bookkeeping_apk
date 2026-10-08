import os
import sys
import time
import threading
import socket
import traceback

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

def _get_android_storage_dir():
    """获取 Android 可写存储目录"""
    try:
        from jnius import autoclass
        PythonActivity = autoclass('org.kivy.android.PythonActivity')
        activity = PythonActivity.mActivity
        # Android 11+ getFilesDir() 返回 /data/data/<package>/files
        files_dir = activity.getFilesDir()
        storage_path = str(files_dir.getAbsolutePath())
        if storage_path and os.path.isdir(storage_path):
            return storage_path
    except Exception:
        pass
    # 回退方案
    try:
        # p4a 默认私有存储路径
        app_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return app_root
    except Exception:
        return os.path.dirname(os.path.abspath(__file__))

def start_flask_server(port):
    """在后台线程中启动 Flask 服务器"""
    try:
        # 设置环境变量，确保使用本地 SQLite
        os.environ.setdefault('DATABASE_URL', '')
        os.environ.setdefault('ADMIN_USER', 'admin')
        os.environ.setdefault('ADMIN_PASS', 'admin123')

        # Android 环境下设置可写数据目录
        try:
            from kivy.utils import platform
            if platform == 'android':
                storage_dir = _get_android_storage_dir()
                data_dir = os.path.join(storage_dir, 'data')
                os.makedirs(data_dir, exist_ok=True)
                os.environ['GIFT_DATA_DIR'] = data_dir
                print(f"[Main] Android 数据目录: {data_dir}")
        except Exception:
            pass

        # 导入 Flask 应用（此时会自动执行 init_database 初始化数据库）
        print("[Main] 正在导入 Flask 应用...")
        from app import app

        # 设置 Flask 静态资源路径（确保在 Android 打包后能找到 static 目录）
        try:
            bundle_dir = os.path.dirname(os.path.abspath(__file__))
            static_dir = os.path.join(bundle_dir, 'static')
            if os.path.isdir(static_dir):
                app.static_folder = static_dir
                print(f"[Main] 静态资源路径: {static_dir}")
        except Exception as e:
            print(f"[Main] 设置静态资源路径失败: {e}")

        print("[Main] Flask 应用导入成功，正在启动服务...")

        # 在后台线程中运行 Flask
        def _run():
            try:
                app.run(host='127.0.0.1', port=port, debug=False, use_reloader=False, threaded=True)
            except Exception as e:
                print(f"[Main] Flask 运行异常: {e}")
                traceback.print_exc()

        flask_thread = threading.Thread(target=_run, daemon=True)
        flask_thread.start()

        # 等待 Flask 服务器就绪
        for _ in range(60):  # 增加等待次数到 60（最多 18 秒）
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

        print("[Main] Flask 服务启动超时（18秒）")
        return False
    except Exception as e:
        print(f"[Main] Flask 服务启动异常: {e}")
        traceback.print_exc()
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

        # 异步启动 Flask 服务（不阻塞 Kivy 主线程）
        global FLASK_PORT
        FLASK_PORT = find_free_port(FLASK_PORT)
        self.target_url = f"http://127.0.0.1:{FLASK_PORT}/"
        self.flask_ready = False
        self.webview_created = False

        # 在后台线程启动 Flask，避免阻塞 Kivy 事件循环
        def _start_flask_async():
            self.flask_ready = start_flask_server(FLASK_PORT)
            if not self.flask_ready:
                print("[Main] Flask 启动失败，将延迟重试...")
                # 延迟 3 秒后重试一次
                time.sleep(3)
                self.flask_ready = start_flask_server(FLASK_PORT)

        flask_init_thread = threading.Thread(target=_start_flask_async, daemon=True)
        flask_init_thread.start()

        if platform == 'android':
            # 延迟 3 秒创建 WebView，给 Flask 充足的启动时间
            Clock.schedule_once(self.init_android_webview, 3.0)
            # 5 秒后检查 WebView 是否创建成功，如果 Flask 还没好则再等
            Clock.schedule_once(self._check_webview, 5.0)
        elif IS_KIVY:
            Clock.schedule_once(self.open_desktop_browser, 5.0)
        else:
            self.open_desktop_browser()
        return root

    def _check_webview(self, *args):
        """检查 WebView 状态，如果 Flask 还没就绪则重新加载"""
        if not self.flask_ready:
            print("[Main] WebView 检查：Flask 尚未就绪，延迟重试...")
            Clock.schedule_once(self._retry_webview, 5.0)

    def _retry_webview(self, *args):
        """重试加载 WebView"""
        if self.flask_ready:
            print("[Main] Flask 已就绪，重新加载 WebView...")
            # 这里不再重新创建 WebView，而是让 WebView 自动刷新
            # 如果 WebView 已创建，通过 Java 层重新加载 URL
            try:
                from jnius import autoclass
                PythonActivity = autoclass('org.kivy.android.PythonActivity')
                activity = PythonActivity.mActivity
                # 查找已添加的 WebView 并重新加载
                webview = activity.findViewById(0x12345)  # 自定义 ID
                if webview:
                    webview.loadUrl(self.target_url)
            except Exception:
                pass
        else:
            print("[Main] Flask 仍未就绪，10 秒后再试...")
            Clock.schedule_once(self._retry_webview, 10.0)

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

                def __init__(self, target_url, flask_ready_callback=None):
                    super(SafeWebClient, self).__init__()
                    self.target_url = target_url
                    self.flask_ready_callback = flask_ready_callback

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
                    print(f"[WebView] 加载错误: code={errorCode}, desc={description}, url={failingUrl}")
                    # 延迟 2 秒后重试
                    Clock.schedule_once(lambda dt: view.loadUrl(self.target_url), 2.0)

                # Android 6+ 新版错误回调
                @java_method('(Landroid/webkit/WebView;Landroid/webkit/WebResourceRequest;Landroid/webkit/WebResourceError;)V')
                def onReceivedError(self, view, request, error):
                    try:
                        desc = str(error.getDescription()) if error else 'unknown'
                        print(f"[WebView] 资源加载错误: {desc}")
                    except Exception:
                        pass

            class CustomChromeClient(PythonJavaClass):
                __javainterfaces__ = ['android/webkit/WebChromeClient']
                __javacontext__ = 'app'

                def __init__(self):
                    super(CustomChromeClient, self).__init__()

                @java_method('(Landroid/webkit/WebView;I)V')
                def onProgressChanged(self, view, newProgress):
                    print(f"[WebView] 加载进度: {newProgress}%")

                @java_method('(Landroid/webkit/ConsoleMessage;)Z')
                def onConsoleMessage(self, consoleMessage):
                    try:
                        msg = str(consoleMessage.message()) if consoleMessage else ''
                        print(f"[WebView Console] {msg}")
                    except Exception:
                        pass
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
                        webview.setId(0x12345)  # 设置固定 ID 便于后续查找
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
                        print(f"[WebView] WebView 已创建，加载 URL: {self.url}")
                    except Exception as ex:
                        print(f"[WebView] 创建 WebView 异常: {ex}")
                        traceback.print_exc()

            activity.runOnUiThread(WebViewInitRunnable(activity, self.target_url))

        except Exception as e:
            print(f"[WebView] Android WebView 初始化异常: {e}")
            traceback.print_exc()

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
