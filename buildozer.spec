[app]

# (str) Title of your application
title = 礼金记账簿

# (str) Package name
package.name = giftbookkeeping

# (str) Package domain (needed for android packaging)
package.domain = org.giftbookkeeping.app

# (str) Source code where the main.py lives
source.dir = .

# (list) Source files to include (let empty to include all the files)
source.include_exts = py,png,jpg,jpeg,html,css,js,ttf,woff,woff2,svg,ico,json,txt,csv

# (list) List of directory to include
source.include_dirs = templates,static

# (list) List of exclusions using pattern matching
source.exclude_dirs = tests, bin, .gradle, .buildozer, .git, .github, __pycache__, .temp, data

# (list) List of exclusions using pattern matching for extensions
source.exclude_exts = spec, pyc, pyd, pyo, db, bak, log, png_bak

# (str) Application versioning
version = 3.1.0

# (list) Application requirements
# 【V3.1 关键修复】此前版本误将纯 Python 包写在无效的 python_depends 键中，
# 该键被 buildozer 静默忽略，导致 flask_sqlalchemy/flask_wtf/flask_login 等
# 根本没有打进 APK，import 即报 ModuleNotFoundError，Flask 起不来，页面空白。
# 正确方式：p4a 对 requirements 中【有 recipe 的包】用 recipe 交叉编译，
# 对【无 recipe 的纯 Python 包】自动 pip 安装进 APK，因此全部写在 requirements 即可。
# 注意：cryptography 需要 Rust 工具链，仍不打包（代码内 try/except 延迟导入自动降级）。
requirements = python3,hostpython3,openssl,sqlite3,pyjnius,kivy,flask,sqlalchemy,markupsafe,greenlet,flask_sqlalchemy,flask_wtf,flask_login,werkzeug,requests,itsdangerous,click,blinker,jinja2,wtforms

# (str) Supported orientation
orientation = portrait

# (bool) Indicate if the application should be fullscreen
fullscreen = 0

# (list) Permissions
# INTERNET: Webhook 推送、AI 联网搜索
# ACCESS_NETWORK_STATE: 网络状态检测
# CAMERA: OCR 图片识别录入（功能三）
# READ_EXTERNAL_STORAGE / WRITE_EXTERNAL_STORAGE: 备份导出与文件操作
android.permissions = INTERNET,ACCESS_NETWORK_STATE,CAMERA,READ_EXTERNAL_STORAGE,WRITE_EXTERNAL_STORAGE

# (int) Target Android API
android.api = 33

# (int) Minimum API your APP will support
android.minapi = 21

# (int) Android SDK version to use
android.sdk = 33

# (int) Android NDK version to use
android.ndk = 25b

# (bool) Use --private data storage
android.private_storage = True

# (bool) If True, then automatically accept SDK license
android.accept_sdk_license = True

# (str) The Android arch to build for (arm64-v8a only for faster CI builds)
android.archs = arm64-v8a

# (bool) enables Android auto backup feature
android.allow_backup = True

# (str) Bootstrap to use
p4a.bootstrap = sdl2

# (int) Log level
log_level = 2

# (str) Presplash (启动画面) 背景色
# presplash.color = #F4F6F9

# (bool) Enable AndroidX
android.enable_androidx = True

# (str) 注入 <application> 标签的额外属性（V3.1 新增）
# Android 9+(API 28+) targetSdk>=28 时 usesCleartextTraffic 默认 false，
# WebView 加载 http://127.0.0.1 本地服务可能被拒（ERR_CLEARTEXT_NOT_PERMITTED）。
# 通过此配置显式允许明文 HTTP，保障本地回环服务可用。
android.extra_manifest_application_arguments = extra_manifest_args.txt

[buildozer]

# (int) Log level
log_level = 2

# (int) Display warning if buildozer is run as root
warn_on_root = 1
