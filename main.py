# -*- coding: utf-8 -*-
"""
礼金记账簿 — 移动端启动入口（V3.2.0 webview bootstrap 架构）

【V3.2 架构说明】
p4a 官方 webview bootstrap：WebView 由 Java 层（org.kivy.android.PythonActivity）
直接创建，完全不经过 Kivy/SDL/GL 渲染链（V1.x~V3.1.1 白屏的最大嫌疑组件已移除）。

启动链路：
  Java: onCreate → 解包 private.tar/libpybundle → WebView 加载 _load.html(Loading页)
        → 启动 PythonThread(执行本文件) → WvThread 轮询 127.0.0.1:5000
        → Flask 就绪后 Java 侧自动 loadUrl("http://127.0.0.1:5000/")
  Python(本文件)：环境初始化 → 后台线程启动 Flask(端口5000) → 主线程保活

本文件职责（按优先级）：
  1. 后台线程启动 Flask 服务（必须监听 127.0.0.1:5000，与 Java 侧约定一致）
  2. 全程双路日志：内置 files/app_debug.log + 外置（USB 可直读）
     Android/data/<包名>/files/app_debug.log
  3. 启动 marker：多处写 startup_marker.txt，证明 Python 进程曾运行
  4. 失败兜底：错误详情写入 last_error.txt（外置可取）+ Toast 弹出摘要
"""
import os
import sys
import time
import threading
import traceback

# ==================== 常量 ====================
# webview bootstrap 的 Java 侧 WebViewLoader 轮询 127.0.0.1:5000（p4a 默认值），
# 此端口必须与约定一致，不可随意更改
FLASK_PORT = 5000
APP_VERSION = '3.2.0'

# Android 环境判定：p4a 启动时注入 ANDROID_ARGUMENT 且带 getandroidapilevel
IS_ANDROID = ('ANDROID_ARGUMENT' in os.environ or hasattr(sys, 'getandroidapilevel'))

# 日志/数据目录（Android 上由 _init_android_dirs 填充）
_INTERNAL_FILES_DIR = None   # getFilesDir()：内部私有目录（一定可写）
_EXTERNAL_FILES_DIR = None   # getExternalFilesDir()：Android/data/<包名>/files（USB 直读）


# ==================== Android 目录与基础组件 ====================

def _init_android_dirs():
    """通过 jnius 获取内部/外置应用目录（失败返回 None，不致命）"""
    global _INTERNAL_FILES_DIR, _EXTERNAL_FILES_DIR
    if not IS_ANDROID or _INTERNAL_FILES_DIR is not None:
        return
    try:
        from jnius import autoclass
        PythonActivity = autoclass('org.kivy.android.PythonActivity')
        activity = PythonActivity.mActivity
        # 内部私有目录（一定存在可写）
        try:
            _INTERNAL_FILES_DIR = str(activity.getFilesDir().getAbsolutePath())
        except Exception as e:
            print(f"[Main] getFilesDir 失败: {e}")
        # 外部应用专属目录（无需权限；USB 文件传输模式可直读）
        try:
            ext = activity.getExternalFilesDir(None)
            if ext is not None:
                _EXTERNAL_FILES_DIR = str(ext.getAbsolutePath())
        except Exception as e:
            print(f"[Main] getExternalFilesDir 失败: {e}")
    except Exception as e:
        print(f"[Main] 初始化 Android 目录失败: {e}")


def _write_file_safe(path, content):
    """安全写文件（失败只打日志，绝不抛异常）"""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'a', encoding='utf-8') as f:
            f.write(content)
        return True
    except Exception as e:
        print(f"[Main] 写文件失败 {path}: {e}")
        return False


def _log(msg):
    """三通道日志：print(logcat) + 内置 files 目录 + 外置 USB 可读目录"""
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    try:
        print(f"[Main] {msg}")
    except Exception:
        pass
    if IS_ANDROID:
        if _INTERNAL_FILES_DIR:
            _write_file_safe(os.path.join(_INTERNAL_FILES_DIR, 'app_debug.log'),
                             line + '\n')
        if _EXTERNAL_FILES_DIR:
            _write_file_safe(os.path.join(_EXTERNAL_FILES_DIR, 'app_debug.log'),
                             line + '\n')
    else:
        # 桌面调试：写到 .temp
        try:
            os.makedirs('.temp', exist_ok=True)
            with open(os.path.join('.temp', 'app_debug.log'), 'a', encoding='utf-8') as f:
                f.write(line + '\n')
        except Exception:
            pass


def _android_toast(msg):
    """在 UI 线程弹出 Toast（失败静默——日志与文件才是主通道）"""
    if not IS_ANDROID:
        return
    try:
        from jnius import autoclass, PythonJavaClass, java_method
        PythonActivity = autoclass('org.kivy.android.PythonActivity')
        activity = PythonActivity.mActivity
        Toast = autoclass('android.widget.Toast')
        Handler = autoclass('android.os.Handler')
        Looper = autoclass('android.os.Looper')

        class _ToastRunnable(PythonJavaClass):
            __javainterfaces__ = ['java/lang/Runnable']
            __javacontext__ = 'app'

            def __init__(self, text):
                super(_ToastRunnable, self).__init__()
                self._text = text

            @java_method('()V')
            def run(self):
                try:
                    Toast.makeText(activity, self._text,
                                   Toast.LENGTH_LONG).show()
                except Exception:
                    pass

        handler = Handler(Looper.getMainLooper())
        handler.post(_ToastRunnable(msg))
    except Exception as e:
        print(f"[Main] Toast 失败(可忽略): {e}")


def _write_startup_marker(stage, detail=''):
    """启动 marker：证明 Python 进程运行到了哪个阶段（USB 直读即可取证）"""
    content = (f"version={APP_VERSION}\n"
               f"time={time.strftime('%Y-%m-%d %H:%M:%S')}\n"
               f"stage={stage}\n"
               f"python={sys.version.split()[0]}\n"
               f"detail={detail}\n"
               f"----\n")
    if IS_ANDROID:
        if _INTERNAL_FILES_DIR:
            _write_file_safe(os.path.join(_INTERNAL_FILES_DIR, 'startup_marker.txt'), content)
        if _EXTERNAL_FILES_DIR:
            _write_file_safe(os.path.join(_EXTERNAL_FILES_DIR, 'startup_marker.txt'), content)
    else:
        _write_file_safe(os.path.join('.temp', 'startup_marker.txt'), content)


def _write_last_error(error_detail):
    """启动失败详情写入外置目录，供 USB 直读取证"""
    content = (f"version={APP_VERSION}\n"
               f"time={time.strftime('%Y-%m-%d %H:%M:%S')}\n"
               f"error=\n{error_detail}\n")
    if IS_ANDROID:
        if _INTERNAL_FILES_DIR:
            _write_file_safe(os.path.join(_INTERNAL_FILES_DIR, 'last_error.txt'), content)
        if _EXTERNAL_FILES_DIR:
            _write_file_safe(os.path.join(_EXTERNAL_FILES_DIR, 'last_error.txt'), content)


# ==================== Flask 启动 ====================

def _run_flask_with_retry(max_attempts=3, retry_delay=3.0):
    """Flask 启动线程：失败自动重试，全部失败后写 last_error + Toast 摘要"""
    last_err = None
    for attempt in range(1, max_attempts + 1):
        _log(f"Flask 启动尝试 {attempt}/{max_attempts} ...")
        _write_startup_marker(f'flask_attempt_{attempt}')
        # start_flask_server 内 app.run 常驻：若它返回即已失败
        # —— 因此重试需在线程内自身包一层：fork 新线程跑 run、主判听端口
        ok, err = _run_flask_and_watch()
        if ok:
            return
        last_err = err
        _log(f"Flask 第 {attempt} 次启动失败")
        if attempt < max_attempts:
            time.sleep(retry_delay)

    # 全部失败：外置错误详情 + Toast 摘要（Java Loading 页会一直停着等 5000）
    _write_last_error(last_err or 'unknown')
    _android_toast('礼金记账簿启动失败：本地服务初始化异常，请连接电脑查看 app_debug.log')
    # 不退出进程：保活以便用户随时 USB 取证
    while True:
        time.sleep(60)


def _run_flask_and_watch(watch_timeout=90):
    """
    在子线程跑 app.run，本线程轮询端口判断 Flask 是否真正就绪。
    返回（是否成功，错误详情）。
    """
    import socket
    err_holder = {}

    def _worker():
        try:
            _log(f"正在导入 Flask 应用 v{APP_VERSION}（含数据库初始化）...")
            t0 = time.time()
            from app import app
            _log(f"Flask 应用导入成功（耗时 {time.time() - t0:.1f}s）")

            _write_startup_marker('app_imported')
            _log(f"启动 HTTP 服务 127.0.0.1:{FLASK_PORT} ...")
            _write_startup_marker('flask_running')
            app.run(host='127.0.0.1', port=FLASK_PORT, debug=False,
                    use_reloader=False, threaded=True)
        except Exception:
            err_holder['err'] = traceback.format_exc()

    t = threading.Thread(target=_worker, daemon=True)
    t.start()

    deadline = time.time() + watch_timeout
    while time.time() < deadline:
        # import 阶段抛错会很快写入 err_holder
        if 'err' in err_holder:
            _log(f"Flask 子线程异常：\n{err_holder['err']}")
            return False, err_holder['err']
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            result = sock.connect_ex(('127.0.0.1', FLASK_PORT))
            sock.close()
            if result == 0:
                _log(f"Flask 服务已在 127.0.0.1:{FLASK_PORT} 就绪（Java 侧将自动跳转）")
                _write_startup_marker('flask_ready')
                return True, None
        except Exception as e:
            _log(f"端口探测异常(重试中): {e}")
        time.sleep(0.5)

    _log(f"Flask 服务启动超时（{watch_timeout} 秒未监听端口）")
    return False, "Flask 服务启动超时（端口 {:.0f}s 未监听），详见 app_debug.log".format(watch_timeout)


# ==================== 主入口 ====================

def main():
    _write_startup_marker('main_entered')
    # 环境变量统一在此设置（与 app.py 的读取约定一致）
    os.environ.setdefault('DATABASE_URL', '')
    os.environ.setdefault('ADMIN_USER', 'admin')
    os.environ.setdefault('ADMIN_PASS', 'admin123')
    os.environ.setdefault('SECRET_KEY', f'gift-bookkeeping-android-{APP_VERSION}')

    if IS_ANDROID:
        _init_android_dirs()
        # Android：SQLite 库放内部私有目录（app_root 是 p4a 解包目录，
        # data/ 独立出来避免与解包内容混淆）
        if _INTERNAL_FILES_DIR:
            data_dir = os.path.join(_INTERNAL_FILES_DIR, 'data')
            os.makedirs(data_dir, exist_ok=True)
            os.environ['GIFT_DATA_DIR'] = data_dir

        _log(f"礼金记账簿 v{APP_VERSION} 启动（webview bootstrap 架构，Python 入口运行中）")
        _log(f"内部目录: {_INTERNAL_FILES_DIR}")
        _log(f"外置目录: {_EXTERNAL_FILES_DIR}")
        _write_startup_marker('dirs_ready', detail=(
            f"internal={_INTERNAL_FILES_DIR}; external={_EXTERNAL_FILES_DIR}"))
        _android_toast(f'礼金记账簿 v{APP_VERSION} 启动中...')

        # 后台线程：Flask 启动 + 重试 + 失败兜底
        threading.Thread(target=_run_flask_with_retry, daemon=True).start()

        # 主线程保活（p4a webview bootstrap 要求 Python 进程持续运行）
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            _log("收到退出信号，Python 主线程结束")
    else:
        # 桌面调试：直接启动 Flask + 打开浏览器
        print(f"[桌面调试] 礼金记账簿 v{APP_VERSION}，端口 {FLASK_PORT}")
        threading.Thread(target=_run_flask_with_retry, daemon=True).start()
        try:
            import webbrowser
            time.sleep(2.0)
            webbrowser.open(f"http://127.0.0.1:{FLASK_PORT}/")
        except Exception:
            pass
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("服务已停止")


if __name__ == '__main__':
    main()
