# 礼金记账簿 Android APK (Gift Bookkeeping APK)

本项目是将【礼金记账簿】(Gift Bookkeeping App) 完整移植为 Android 手机端原生可安装运行的 APK 应用。通过轻量嵌入式本地服务 + 原生 Android WebView 容器技术，用户可以在 Android 手机/平板上脱机离线使用完整的礼金记账、亲友管理、统计分析、导入导出等所有功能。

---

## 📱 核心功能特性

1. **完全包含 Web 版的所有功能**：
   - 💰 **礼金收支记录**：支持客人姓名、年龄、住址、联系电话、金额、办席事由（婚宴、满月酒、周岁宴、寿宴、升学宴、乔迁宴、白事人情、自定义）、备注等全字段管理。
   - 📊 **统计面板与图表**：实时统计总礼金、记录总笔数、平均单笔礼金、办席事由占比分析等。
   - 🔍 **高效筛选与排序**：支持多关键字搜索（姓名/电话/地址/金额/事由/备注）、按事由筛选、按时间及金额正倒序排列。
   - ⚡ **批量操作**：批量选择并一键删除记录。
   - 📑 **数据导入与导出**：支持 CSV/Excel 批量导入（含模板下载及校验提示）与全量数据导出。
   - 👤 **账户与权限系统**：支持用户注册/登录、密保问题找回密码、修改密码；内置系统管理员账户 `admin`（初始密码：`admin123`），支持用户管理与操作审计日志查看。
2. **手机端独立运行**：
   - 采用嵌入式 SQLite 数据库，数据安全保存在手机内部私有存储空间，无需联网即可使用。
   - 原生全屏 WebView 沉浸式交互体验，UI 自动适配手机触摸手势与各种屏幕分辨率。
3. **CI/CD 自动化构建**：
   - 内置 GitHub Actions 工作流（`.github/workflows/build_apk.yml`），每次提交或发布 Release 标签自动在云端编译生成 APK 安装包。

---

## 🛠️ 项目架构

```
gift_bookkeeping_apk/
├── .github/
│   └── workflows/
│       └── build_apk.yml     # GitHub Actions 自动化编译打包 APK 工作流
├── templates/                 # 页面 UI 模板（Bootstrap 5 + FontAwesome 6）
│   ├── base.html
│   ├── index.html
│   ├── login.html
│   ├── register.html
│   ├── forgot_password.html
│   ├── change_password.html
│   ├── admin_users.html
│   └── admin_logs.html
├── app.py                     # 后端业务逻辑与路由控制器
├── models.py                  # 数据库模型 (User, GiftRecord, OperationLog)
├── main.py                    # 移动端启动入口与 Android 原生 WebView / 桌面容器适配
├── buildozer.spec             # Buildozer Android 打包配置文件
├── requirements.txt           # Python 依赖清单
├── .gitignore                 # Git 忽略配置
└── README.md                  # 项目说明文档
```

---

## 🚀 如何获取与安装 APK

### 方式一：GitHub Releases 页面直接下载（最推荐）
1. 访问本仓库的 **[Releases 页面](../../releases)**。
2. 找到 **Latest** 正式版发布。
3. 在 **Assets** 列表中点击 `.apk` 文件直接下载并安装到安卓手机上。

### 方式二：GitHub Actions 运行构建产物下载
1. 访问本仓库的 GitHub 页面中的 **Actions** 标签页。
2. 点击最新的运行记录，在 **Artifacts** 中下载打包好的 `GiftBookkeeping-APK`。
3. 解压后将 `.apk` 传输到 Android 手机上点击安装即可。

### 方式二：本地使用 Buildozer 打包（Linux / WSL2 环境）
```bash
# 1. 安装系统依赖
sudo apt update
sudo apt install -y git zip unzip openjdk-17-jdk autoconf libtool pkg-config zlib1g-dev libncurses5-dev libncursesw5-dev libtinfo5 cmake libffi-dev libssl-dev build-essential ccache

# 2. 安装 buildozer
pip install cython buildozer

# 3. 执行编译打包
buildozer android debug

# 4. 生成的 apk 位于 bin/ 目录下
```

---

## 💻 本地调试运行（桌面端）

在电脑上开发测试时，直接运行 `main.py` 或 `app.py`：

```bash
# 安装依赖
pip install -r requirements.txt

# 运行主程序（自动唤起窗口/浏览器）
python main.py

# 或直接运行 Flask Web 服务
python app.py
```
默认访问地址：`http://127.0.0.1:5000`

---

## 🔒 初始管理员与安全说明

- **管理员账号**：`admin`
- **初始密码**：`admin123`
- **提示**：首次登录后建议在个人资料或修改密码页面更新管理员密码。

---

## 📄 开源许可证

MIT License
