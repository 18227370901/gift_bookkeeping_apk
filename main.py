import os
import sys
import time
import threading
import socket
import traceback

# ==================== 本地 Flask 服务启动（V3.1） ====================
# 移动端架构：Flask 后端在设备本地运行（127.0.0.1），WebView 加载本地页面
# V3.1 核心改进：
#   1. WebView 首屏加载内嵌"启动中"诊断页，JS 轮询本地服务，就绪后自动跳转
#      —— 彻底根治 Flask 启动时序问题（不再依赖固定延迟秒数）
#   2. Flask 启动失败时，将 Python traceback 渲染到 WebView 上
#      —— 不再白屏盲调，用户可直接截图看到具体报错
#   3. 全程日志写入 files/app_debug.log，可 adb pull / 文件管理器查看

FLASK_PORT = 8765  # 本地服务端口（避免与常用端口冲突）

# 全局引用：WebView 实例（用于后续推送错误页）与启动错误信息
WEBVIEW_REF = None
_webview_lock = threading.Lock()


def _get_log_path():
    """获取日志文件路径（Android 写入应用私有 files 目录，桌面端返回 None 仅打屏）"""
    global _log_path
    try:
        return _log_path
    except NameError:
        pass
    path = None
    try:
        from kivy.utils import platform
        if platform == 'android':
            from jnius import autoclass
            PythonActivity = autoclass('org.kivy.android.PythonActivity')
            files_dir = str(PythonActivity.mActivity.getFilesDir().getAbsolutePath())
            path = os.path.join(files_dir, 'app_debug.log')
    except Exception:
        path = None
    globals()['_log_path'] = path
    return path


def _log(msg):
    """双通道日志：print（logcat 可见）+ 文件（adb pull 可见）"""
    try:
        print(f"[Main] {msg}")
    except Exception:
        pass
    path = _get_log_path()
    if path:
        try:
            with open(path, 'a', encoding='utf-8') as f:
                f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
        except Exception:
            pass


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
    """获取 Android 可写存储目录（应用私有 files 目录）"""
    try:
        from jnius import autoclass
        PythonActivity = autoclass('org.kivy.android.PythonActivity')
        files_dir = str(PythonActivity.mActivity.getFilesDir().getAbsolutePath())
        if files_dir and os.path.isdir(files_dir):
            return files_dir
    except Exception as e:
        _log(f"获取 Android 存储目录失败: {e}")
    return os.path.dirname(os.path.abspath(__file__))


# ==================== 内嵌诊断页模板 ====================

LOADING_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>启动中</title>
<style>
body{font-family:-apple-system,"PingFang SC","Noto Sans SC",sans-serif;background:#f4f6f9;
display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;color:#212529}
.card{background:#fff;border-radius:14px;padding:36px 28px;max-width:340px;width:88%;
box-shadow:0 4px 20px rgba(0,0,0,.08);text-align:center}
.spinner{width:46px;height:46px;border:4px solid #e9ecef;border-top-color:#4caf50;
border-radius:50%;margin:0 auto 18px;animation:spin 0.9s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
h2{font-size:18px;margin:0 0 8px}
p{font-size:14px;color:#6c757d;margin:4px 0;line-height:1.6}
.timer{font-size:13px;color:#adb5bd;margin-top:10px}
.hint{display:none;margin-top:16px;padding:12px;background:#fff8e1;border-radius:8px;
font-size:12.5px;color:#856404;text-align:left;line-height:1.7}
.dot{animation:blink 1.2s infinite}
@keyframes blink{50%{opacity:.2}}
</style>
</head>
<body>
<div class="card">
  <div class="spinner"></div>
  <h2>礼金记账簿启动中<span class="dot">.</span><span class="dot" style="animation-delay:.2s">.</span><span class="dot" style="animation-delay:.4s">.</span></h2>
  <p>正在初始化本地数据库与服务</p>
  <p>首次启动需要数秒，请稍候</p>
  <div class="timer" id="timer">已等待 0 秒</div>
  <div class="hint" id="hint">
    等待时间较长？可能原因：<br>
    1. 首次启动正在创建 22 张数据表<br>
    2. 本地服务仍在预热中<br>
    3. 若超过 2 分钟仍无响应，请完全退出后重开
  </div>
</div>
<script>
var start = Date.now();
var tries = 0;
var timerEl = document.getElementById('timer');
var hintEl = document.getElementById('hint');
setInterval(function(){
  timerEl.textContent = '已等待 ' + Math.round((Date.now()-start)/1000) + ' 秒';
  if (tries > 40) hintEl.style.display = 'block';
}, 500);
function poll(){
  fetch('/login', {method:'GET', cache:'no-store'})
    .then(function(r){
      if (r.status >= 200 && r.status < 500){
        location.replace('/login');
      } else { tries++; }
    })
    .catch(function(){ tries++; });
}
setInterval(poll, 700);
poll();
</script>
</body>
</html>"""

ERROR_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>启动失败</title>
<style>
body{font-family:-apple-system,"PingFang SC","Noto Sans SC",sans-serif;background:#f4f6f9;
margin:0;padding:24px 14px;color:#212529}
.card{background:#fff;border-radius:14px;padding:22px 18px;max-width:420px;margin:0 auto;
box-shadow:0 4px 20px rgba(0,0,0,.08)}
h2{font-size:17px;color:#dc3545;margin:0 0 10px}
p{font-size:13.5px;color:#495057;line-height:1.7;margin:8px 0}
pre{background:#1e1e2e;color:#ffcc66;padding:14px;border-radius:8px;font-size:11.5px;
white-space:pre-wrap;word-break:break-all;max-height:52vh;overflow:auto;line-height:1.5}
.tag{font-size:12px;color:#6c757d;margin-top:12px;line-height:1.7}
</style>
</head>
<body>
<div class="card">
  <h2>启动失败</h2>
  <p>本地服务初始化遇到异常，详细信息如下（请截图反馈给开发者）：</p>
  <pre>__ERROR_DETAIL__</pre>
  <div class="tag">版本：礼金记账簿 v3.1.1 Android<br>可尝试：完全退出应用后重新打开</div>
</div>
</body>
</html>"""


def _escape_html(text):
    """HTML 转义，防注入"""
    return (str(text).replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;').replace('"', '&quot;'))


def start_flask_server(port):
    """启动 Flask 服务器，返回 (是否成功, 错误详情traceback或None)"""
    try:
        os.environ.setdefault('DATABASE_URL', '')
        os.environ.setdefault('ADMIN_USER', 'admin')
        os.environ.setdefault('ADMIN_PASS', 'admin123')

        # Android 环境下设置可写数据目录（APK 内部路径只读，无法建库）
        try:
            from kivy.utils import platform
            if platform == 'android':
                storage_dir = _get_android_storage_dir()
                data_dir = os.path.join(storage_dir, 'data')
                os.makedirs(data_dir, exist_ok=True)
                os.environ['GIFT_DATA_DIR'] = data_dir
                _log(f"Android 数据目录: {data_dir}")
        except Exception as e:
            _log(f"设置数据目录异常(可忽略): {e}")

        _log("正在导入 Flask 应用（含数据库初始化）...")
        from app import app
        _log("Flask 应用导入成功，启动 HTTP 服务...")

        def _run():
            try:
                app.run(host='127.0.0.1', port=port, debug=False,
                        use_reloader=False, threaded=True)
            except Exception as e:
                _log(f"Flask 运行异常: {e}\n{traceback.format_exc()}")

        threading.Thread(target=_run, daemon=True).start()

        # 等待 Flask 就绪（最长 60 秒，覆盖首次建表场景）
        for i in range(200):
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(1)
                result = sock.connect_ex(('127.0.0.1', port))
                sock.close()
                if result == 0:
                    _log(f"Flask 服务已在 127.0.0.1:{port} 就绪")
                    return True, None
            except Exception:
                pass
            time.sleep(0.3)

        _log("Flask 服务启动超时（60秒）")
        return False, "Flask 服务启动超时（60 秒未监听端口），详见 app_debug.log"
    except Exception as e:
        detail = traceback.format_exc()
        _log(f"Flask 启动异常: {e}\n{detail}")
        return False, detail


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

        global FLASK_PORT
        FLASK_PORT = find_free_port(FLASK_PORT)
        self.base_url = f"http://127.0.0.1:{FLASK_PORT}/"
        self.flask_ready = False
        self.startup_error = None

        # 后台线程异步启动 Flask（不阻塞 Kivy 主线程，避免 ANR）
        def _start_flask_async():
            ok, err = start_flask_server(FLASK_PORT)
            self.flask_ready = ok
            self.startup_error = err
            if not ok:
                _log("Flask 首次启动失败，3 秒后重试一次...")
                time.sleep(3)
                ok2, err2 = start_flask_server(FLASK_PORT)
                self.flask_ready = ok2
                if not ok2:
                    self.startup_error = err2 or err

        threading.Thread(target=_start_flask_async, daemon=True).start()

        if platform == 'android':
            # WebView 尽早创建并显示"启动中"诊断页（loading 页自己轮询跳转，
            # 不再依赖 Python 侧固定延迟）
            Clock.schedule_once(self.init_android_webview, 0.8)
            # 12 秒后若启动失败，把 traceback 推送到 WebView 展示
            Clock.schedule_once(self._push_error_page_if_any, 12.0)
        elif IS_KIVY:
            Clock.schedule_once(self.open_desktop_browser, 5.0)
        else:
            self.open_desktop_browser()
        return root

    # ---------------- WebView 初始化 ----------------
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

            base_url = self.base_url
            loading_html = LOADING_HTML_TEMPLATE

            class SafeWebClient(PythonJavaClass):
                __javainterfaces__ = ['android/webkit/WebViewClient']
                __javacontext__ = 'app'

                def __init__(self, target_base):
                    super(SafeWebClient, self).__init__()
                    self.target_base = target_base

                @java_method('(Landroid/webkit/WebView;Ljava/lang/String;)Z')
                def shouldOverrideUrlLoading(self, view, url):
                    # 本地服务内导航全部放行（含 loading 页 location.replace）
                    if url.startswith('http://127.0.0.1') or url.startswith('http://localhost'):
                        return False
                    view.loadUrl(url)
                    return True

                @java_method('(Landroid/webkit/WebView;Landroid/webkit/SslErrorHandler;Landroid/net/http/SslError;)V')
                def onReceivedSslError(self, view, handler, error):
                    handler.proceed()

                @java_method('(Landroid/webkit/WebView;ILjava/lang/String;Ljava/lang/String;)V')
                def onReceivedError(self, view, errorCode, description, failingUrl):
                    _log(f"WebView 加载错误: code={errorCode} desc={description} url={failingUrl}")

            class CustomChromeClient(PythonJavaClass):
                __javainterfaces__ = ['android/webkit/WebChromeClient']
                __javacontext__ = 'app'

                def __init__(self):
                    super(CustomChromeClient, self).__init__()

                @java_method('(Landroid/webkit/WebView;I)V')
                def onProgressChanged(self, view, newProgress):
                    if newProgress in (0, 100):
                        _log(f"WebView 加载进度: {newProgress}%")

                @java_method('(Landroid/webkit/ConsoleMessage;)Z')
                def onConsoleMessage(self, consoleMessage):
                    try:
                        _log(f"[JS] {consoleMessage.message()}")
                    except Exception:
                        pass
                    return True

            class WebViewInitRunnable(PythonJavaClass):
                __javainterfaces__ = ['java/lang/Runnable']
                __javacontext__ = 'app'

                def __init__(self, activity, base_url, html):
                    super(WebViewInitRunnable, self).__init__()
                    self.activity = activity
                    self.base_url = base_url
                    self.html = html

                @java_method('()V')
                def run(self):
                    try:
                        global WEBVIEW_REF
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
                        try:
                            settings.setUserAgentString("Mozilla/5.0 (Linux; Android) GiftBookkeeping/3.1.1")
                        except Exception:
                            pass

                        try:
                            CookieManager.getInstance().setAcceptCookie(True)
                            CookieManager.getInstance().setAcceptThirdPartyCookies(webview, True)
                        except Exception:
                            pass

                        # 开启远程调试：电脑 Chrome 访问 chrome://inspect 可实时查看
                        try:
                            WebView.setWebContentsDebuggingEnabled(True)
                        except Exception:
                            pass

                        webview.setWebViewClient(SafeWebClient(self.base_url))
                        webview.setWebChromeClient(CustomChromeClient())
                        webview.setScrollBarStyle(View.SCROLLBARS_INSIDE_OVERLAY)
                        webview.setFocusable(True)
                        webview.setFocusableInTouchMode(True)

                        params = LayoutParams(
                            LayoutParams.MATCH_PARENT,
                            LayoutParams.MATCH_PARENT
                        )
                        self.activity.addContentView(webview, params)

                        # 【核心】先加载内嵌"启动中"诊断页：
                        # baseURL 指向本地服务 → 页面内 fetch('/login') 同源不受 CORS 限制
                        # 服务就绪后 JS 自动 location.replace 跳转真实页面
                        webview.loadDataWithBaseURL(self.base_url, self.html,
                                                    'text/html', 'utf-8', None)
                        webview.requestFocus()
                        with _webview_lock:
                            WEBVIEW_REF = webview
                        _log(f"WebView 已创建，诊断页已加载（等待 {self.base_url} 就绪后自动跳转）")
                    except Exception as ex:
                        _log(f"创建 WebView 异常: {ex}\n{traceback.format_exc()}")

            activity.runOnUiThread(WebViewInitRunnable(activity, base_url, loading_html))

        except Exception as e:
            _log(f"Android WebView 初始化异常: {e}\n{traceback.format_exc()}")

    # ---------------- 失败时推送错误页 ----------------
    def _push_error_page_if_any(self, *args):
        """Flask 启动失败时，将 traceback 渲染到 WebView（不再白屏盲调）"""
        if self.flask_ready or not self.startup_error:
            return
        try:
            if WEBVIEW_REF is None:
                # WebView 尚未就绪，5 秒后再试
                Clock.schedule_once(self._push_error_page_if_any, 5.0)
                return

            from jnius import autoclass, PythonJavaClass, java_method
            PythonActivity = autoclass('org.kivy.android.PythonActivity')
            activity = PythonActivity.mActivity

            error_html = ERROR_HTML_TEMPLATE.replace(
                '__ERROR_DETAIL__', _escape_html(self.startup_error))

            webview = WEBVIEW_REF
            base_url = self.base_url

            class ErrorPageRunnable(PythonJavaClass):
                __javainterfaces__ = ['java/lang/Runnable']
                __javacontext__ = 'app'

                def __init__(self):
                    super(ErrorPageRunnable, self).__init__()

                @java_method('()V')
                def run(self):
                    try:
                        webview.loadDataWithBaseURL(base_url, error_html,
                                                    'text/html', 'utf-8', None)
                        _log("已将启动错误详情推送到 WebView 展示")
                    except Exception as ex:
                        _log(f"推送错误页失败: {ex}")

            activity.runOnUiThread(ErrorPageRunnable())
        except Exception as e:
            _log(f"推送错误页异常: {e}")

    def open_desktop_browser(self, *args):
        try:
            import webbrowser
            webbrowser.open(self.base_url)
        except Exception:
            pass


if __name__ == '__main__':
    if IS_KIVY:
        GiftBookkeepingApp().run()
    else:
        # 桌面端：启动 Flask 并打开浏览器
        FLASK_PORT = find_free_port(FLASK_PORT)
        ok, err = start_flask_server(FLASK_PORT)
        if not ok:
            print(f"[桌面调试] Flask 启动失败:\n{err}")
        import webbrowser
        webbrowser.open(f"http://127.0.0.1:{FLASK_PORT}/")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("服务已停止")
