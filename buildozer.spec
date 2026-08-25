[app]

# (str) Title of your application
title = GiftBookkeeping

# (str) Package name
package.name = giftbookkeeping

# (str) Package domain (needed for android packaging)
package.domain = org.giftbookkeeping.app

# (str) Source code where the main.py lives
source.dir = .

# (list) Source files to include (let empty to include all the files)
source.include_exts = py,png,jpg,jpeg,html,css,js,ttf,woff,woff2,svg,ico,json

# (list) List of directory to include
source.include_dirs = templates,static

# (list) List of exclusions using pattern matching
source.exclude_dirs = tests, bin, .gradle, .buildozer, .git, .github

# (list) List of exclusions using pattern matching for extensions
source.exclude_exts = spec, pyc, pyd, pyo

# (str) Application versioning
version = 1.0.0

# (list) Application requirements
requirements = python3,hostpython3,openssl,pyjnius,kivy

# (str) Supported orientation
orientation = portrait

# (bool) Indicate if the application should be fullscreen
fullscreen = 0

# (list) Permissions
android.permissions = INTERNET,ACCESS_NETWORK_STATE

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

# (str) The Android arch to build for
android.archs = arm64-v8a

# (bool) enables Android auto backup feature
android.allow_backup = True

# (str) Bootstrap to use
p4a.bootstrap = sdl2

# (int) Log level
log_level = 2

[buildozer]

# (int) Log level
log_level = 2

# (int) Display warning if buildozer is run as root
warn_on_root = 1
