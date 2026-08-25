import os
import sys

TARGET_URL = "https://ljp.loveyy.indevs.in:15001/"

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
        if platform == 'android':
            Clock.schedule_once(self.init_android_webview, 0.1)
        else:
            Clock.schedule_once(self.open_desktop_browser, 0.5)
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

            activity.runOnUiThread(WebViewInitRunnable(activity, TARGET_URL))

        except Exception as e:
            print("Android WebView Exception:", e)

    def open_desktop_browser(self, *args):
        try:
            import webbrowser
            webbrowser.open(TARGET_URL)
        except Exception:
            pass


if __name__ == '__main__':
    if IS_KIVY:
        GiftBookkeepingApp().run()
    else:
        import webbrowser
        webbrowser.open(TARGET_URL)
