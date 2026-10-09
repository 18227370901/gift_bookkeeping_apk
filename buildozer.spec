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
# 【V3.2.0 版本区分】架构级重构：webview bootstrap 彻底移除 Kivy/SDL/GL 渲染链
version = 3.2.0

# (list) Application requirements
# 【V3.2 架构级修复】真机排查史：V1.x ~ V3.1.1 所有版本在实测机型（魅族20/Android 16）
# 上均纯空白、连内嵌诊断页都无法显示——从未有任何版本验证过 Kivy/SDL/GL 渲染链。
# V3.2 改用 p4a 官方 webview bootstrap：WebView 由 Java 层直接创建（PythonActivity），
# Python 仅负责后台 Flask，彻底移除 Kivy；Java 层自带「启动图→Loading页→错误弹窗」
# 三级兜底，任何故障都不会再出现无信息白屏。
# 注意：kivy 已从 requirements 移除；port 固定 5000（p4a webview bootstrap 默认，
# Java 侧 WebViewLoader 轮询 127.0.0.1:5000 后自动 loadUrl）。
# cryptography 仍不打包（需 Rust 工具链，代码内 try/except 延迟导入自动降级）。
requirements = python3,hostpython3,openssl,sqlite3,pyjnius,flask,sqlalchemy,markupsafe,flask_sqlalchemy,flask_wtf,flask_login,werkzeug,requests,itsdangerous,click,blinker,jinja2,wtforms

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
# 【V3.2 核心变更】sdl2 → webview：WebView 纯 Java 层创建，不依赖 Kivy/SDL/GL
p4a.bootstrap = webview

# (int) Log level
log_level = 2

# (str) Presplash (启动画面) 背景色
# presplash.color = #F4F6F9

# (bool) Enable AndroidX
android.enable_androidx = True

# (str) webview bootstrap 说明（V3.2）
# 1. WebView 由 org.kivy.android.PythonActivity 纯 Java 创建，先加载
#    assets/_load.html（Loading 页），Java 侧 WebViewLoader 轮询
#    127.0.0.1:5000，Flask 就绪后自动 loadUrl 跳转。
# 2. Python 库加载失败时 Java 层直接弹 AlertDialog（绝不白屏）。
# 3. manifest 模板自带 usesCleartextTraffic="true"，无需再注入
#    （V3.1 的 android.extra_manifest_application_arguments 已移除，
#     extra_manifest_args.txt 仅保留作历史参考）。

[buildozer]

# (int) Log level
log_level = 2

# (int) Display warning if buildozer is run as root
warn_on_root = 1
