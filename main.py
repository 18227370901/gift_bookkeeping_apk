import os
import sys
import threading
import time
import socket

# Ensure local directory is in sys.path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# Set Android writable directory for SQLite DB if on Android
if 'ANDROID_ARGUMENT' in os.environ or hasattr(sys, 'getandroidapilevel'):
    # On Android, data should be saved in app's internal storage
    try:
        from jnius import autoclass
        PythonActivity = autoclass('org.kivy.android.PythonActivity')
        activity = PythonActivity.mActivity
        app_dir = activity.getFilesDir().getAbsolutePath()
        os.environ['DATABASE_URL'] = f"sqlite:///{os.path.join(app_dir, 'gift_bookkeeping.db')}"
        os.environ['LOG_DIR'] = app_dir
    except Exception as e:
        print(f"Android storage init fallback: {e}")

# Set secret key and config if not present
if not os.environ.get('SECRET_KEY'):
    os.environ['SECRET_KEY'] = 'gift-bookkeeping-android-secret-key-2026'

from app import app, db

def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]

def run_flask(port):
    with app.app_context():
        try:
            db.create_all()
        except Exception as e:
            print(f"DB Init error: {e}")
    # Run Flask server locally
    app.run(host='127.0.0.1', port=port, debug=False, use_reloader=False, threaded=True)

def wait_for_server(port, timeout=10.0):
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=1.0):
                return True
        except (socket.error, ConnectionRefusedError):
            time.sleep(0.2)
    return False

def main():
    port = find_free_port()
    server_url = f"http://127.0.0.1:{port}"

    # Start Flask in background daemon thread
    flask_thread = threading.Thread(target=run_flask, args=(port,), daemon=True)
    flask_thread.start()

    # Wait for server ready
    wait_for_server(port, timeout=5.0)

    # Check if running on Android
    is_android = 'ANDROID_ARGUMENT' in os.environ or hasattr(sys, 'getandroidapilevel')

    if is_android:
        # Use Android WebView through pyjnius
        try:
            from jnius import autoclass, cast
            from android.runnable import run_on_ui_thread

            PythonActivity = autoclass('org.kivy.android.PythonActivity')
            WebView = autoclass('android.webkit.WebView')
            WebViewClient = autoclass('android.webkit.WebViewClient')
            WebChromeClient = autoclass('android.webkit.WebChromeClient')
            WebSettings = autoclass('android.webkit.WebSettings')
            LinearLayout = autoclass('android.widget.LinearLayout')
            ViewGroup = autoclass('android.view.ViewGroup')
            LayoutParams = autoclass('android.widget.LinearLayout$LayoutParams')
            Color = autoclass('android.graphics.Color')

            activity = PythonActivity.mActivity

            @run_on_ui_thread
            def create_webview():
                webview = WebView(activity)
                settings = webview.getSettings()
                settings.setJavaScriptEnabled(True)
                settings.setDomStorageEnabled(True)
                settings.setDatabaseEnabled(True)
                settings.setAllowFileAccess(True)
                settings.setLoadWithOverviewMode(True)
                settings.setUseWideViewPort(True)
                settings.setBuiltInZoomControls(True)
                settings.setDisplayZoomControls(False)
                settings.setSupportZoom(True)
                
                webview.setWebViewClient(WebViewClient())
                webview.setWebChromeClient(WebChromeClient())
                webview.setBackgroundColor(Color.WHITE)
                
                layout = LinearLayout(activity)
                layout.setOrientation(LinearLayout.VERTICAL)
                layout_params = LayoutParams(
                    LayoutParams.MATCH_PARENT,
                    LayoutParams.MATCH_PARENT
                )
                layout.addView(webview, layout_params)
                activity.setContentView(layout)
                
                webview.loadUrl(server_url)

            create_webview()

            # Keep main thread alive
            while True:
                time.sleep(1)

        except Exception as e:
            print(f"Android WebView UI Error: {e}")
            # Fallback to Kivy UI
            launch_kivy_app(server_url)
    else:
        # Running on desktop (Windows/macOS/Linux)
        # Try webview (pywebview) or default browser or Kivy
        try:
            import webview
            webview.create_window('礼金记账簿 - 移动端', server_url, width=450, height=800, resizable=True)
            webview.start()
        except ImportError:
            import webbrowser
            print(f"打开浏览器访问: {server_url}")
            webbrowser.open(server_url)
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                print("Exiting...")

def launch_kivy_app(url):
    from kivy.app import App
    from kivy.uix.label import Label
    from kivy.uix.boxlayout import BoxLayout
    from kivy.uix.button import Button
    import webbrowser

    class GiftBookApp(App):
        def build(self):
            layout = BoxLayout(orientation='vertical', padding=20, spacing=10)
            layout.add_widget(Label(text="礼金记账系统正在运行...", font_size='20sp'))
            btn = Button(text="点击进入记账系统", size_hint=(1, 0.2))
            btn.bind(on_release=lambda x: webbrowser.open(url))
            layout.add_widget(btn)
            return layout

    GiftBookApp().run()

if __name__ == '__main__':
    main()
