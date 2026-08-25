import os
import sys
import time

# Target Web Application URL
TARGET_URL = "https://ljp.loveyy.indevs.in:15001/"

def is_android_env():
    return 'ANDROID_ARGUMENT' in os.environ or hasattr(sys, 'getandroidapilevel')

def main():
    if is_android_env():
        # Android environment - Run Android native WebView
        try:
            from jnius import autoclass
            from android.runnable import run_on_ui_thread

            PythonActivity = autoclass('org.kivy.android.PythonActivity')
            WebView = autoclass('android.webkit.WebView')
            WebViewClient = autoclass('android.webkit.WebViewClient')
            WebChromeClient = autoclass('android.webkit.WebChromeClient')
            LinearLayout = autoclass('android.widget.LinearLayout')
            LayoutParams = autoclass('android.widget.LinearLayout$LayoutParams')
            Color = autoclass('android.graphics.Color')

            # Create a custom WebViewClient subclass to handle SSL & URL loading
            try:
                from jnius import PythonJavaClass, java_method

                class CustomWebViewClient(PythonJavaClass):
                    __javainterfaces__ = ['android/webkit/WebViewClient']
                    __javacontext__ = 'app'

                    def __init__(self):
                        super().__init__()

                    @java_method('(Landroid/webkit/WebView;Ljava/lang/String;)Z')
                    def shouldOverrideUrlLoading(self, view, url):
                        view.loadUrl(url)
                        return True

                    @java_method('(Landroid/webkit/WebView;Landroid/webkit/SslErrorHandler;Landroid/net/http/SslError;)V')
                    def onReceivedSslError(self, view, handler, error):
                        handler.proceed()
                
                custom_client = CustomWebViewClient()
            except Exception as client_err:
                print(f"Fallback to default WebViewClient: {client_err}")
                custom_client = WebViewClient()

            activity = PythonActivity.mActivity

            @run_on_ui_thread
            def create_webview():
                try:
                    webview = WebView(activity)
                    settings = webview.getSettings()
                    
                    settings.setJavaScriptEnabled(True)
                    settings.setDomStorageEnabled(True)
                    settings.setDatabaseEnabled(True)
                    settings.setAllowFileAccess(True)
                    settings.setAllowContentAccess(True)
                    settings.setLoadWithOverviewMode(True)
                    settings.setUseWideViewPort(True)
                    settings.setBuiltInZoomControls(True)
                    settings.setDisplayZoomControls(False)
                    settings.setSupportZoom(True)
                    settings.setJavaScriptCanOpenWindowsAutomatically(True)

                    webview.setWebViewClient(custom_client)
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

                    webview.loadUrl(TARGET_URL)
                except Exception as inner_e:
                    print(f"Error inside UI Thread: {inner_e}")

            create_webview()

            while True:
                time.sleep(1)

        except Exception as e:
            print(f"Android Native WebView Init Failed: {e}")
            launch_kivy_fallback(TARGET_URL)
    else:
        # Running on desktop (Windows / macOS / Linux) for local testing
        try:
            import webview
            webview.create_window('礼金记账簿', TARGET_URL, width=450, height=820, resizable=True)
            webview.start()
        except ImportError:
            import webbrowser
            print(f"桌面环境正在打开浏览器: {TARGET_URL}")
            webbrowser.open(TARGET_URL)
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                print("Exiting...")

def launch_kivy_fallback(url):
    try:
        from kivy.app import App
        from kivy.uix.label import Label
        from kivy.uix.boxlayout import BoxLayout
        from kivy.uix.button import Button
        import webbrowser

        class GiftBookApp(App):
            def build(self):
                layout = BoxLayout(orientation='vertical', padding=30, spacing=20)
                layout.add_widget(Label(text="礼金记账系统 (移动端)", font_size='22sp', size_hint=(1, 0.4)))
                btn = Button(text="点击进入系统", font_size='18sp', size_hint=(1, 0.3), background_color=(0.18, 0.53, 0.94, 1))
                btn.bind(on_release=lambda x: webbrowser.open(url))
                layout.add_widget(btn)
                return layout

        GiftBookApp().run()
    except Exception as e:
        print(f"Kivy fallback failed: {e}")
        import webbrowser
        webbrowser.open(url)
        while True:
            time.sleep(1)

if __name__ == '__main__':
    main()
