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
version = 2.0.0

# (list) Application requirements
# 包含 Flask 生态核心依赖 + cryptography（AES-256-GCM 加密）
# 重型可选依赖（openai/reportlab/openpyxl/pyzipper 等）代码中已做延迟导入，
# 未编译进 APK 时自动降级，不影响核心功能
requirements = python3,hostpython3,openssl,pyjnius,kivy,sqlalchemy,flask,flask_sqlalchemy,flask_wtf,flask_login,werkzeug,cryptography,requests

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
android.api = 34

# (int) Minimum API your APP will support
android.minapi = 21

# (int) Android SDK version to use
android.sdk = 34

# (int) Android NDK version to use
android.ndk = 25b

# (bool) Use --private data storage
android.private_storage = True

# (bool) If True, then automatically accept SDK license
android.accept_sdk_license = True

# (str) The Android arch to build for
android.archs = arm64-v8a,armeabi-v7a

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

# (str) JSON/Assets 递归包含
source.include_patterns = templates/*,static/*

[buildozer]

# (int) Log level
log_level = 2

# (int) Display warning if buildozer is run as root
warn_on_root = 1
