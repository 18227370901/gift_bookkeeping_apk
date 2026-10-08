---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: 'f4856425-abc5-41c7-b4f0-b67d2dca82f9'
  PropagateID: 'f4856425-abc5-41c7-b4f0-b67d2dca82f9'
  ReservedCode1: 'ad0ff146-710e-468b-ae60-e9000b05680a'
  ReservedCode2: 'ad0ff146-710e-468b-ae60-e9000b05680a'
---

# 礼金记账簿 移动端 APK (Gift Bookkeeping Mobile App)

本项目是将【礼金记账簿】(Gift Bookkeeping App) 完整移植为 Android/iOS 手机端原生可安装运行的应用。通过**本地嵌入式 Flask 服务 + 原生 WebView 容器**技术，用户可以在手机上脱机离线使用完整的礼金记账、亲友管理、统计分析、AI 助手、导入导出等所有功能。

> **V2.0 重大更新**：完整同步源项目 `gift_bookkeeping_app` 全部功能（25 个页面、179 条路由、22+ 数据模型），并新增 iOS 构建支持。

---

## 📱 核心功能特性

### 完整包含 Web 版的所有功能

| 功能模块 | 说明 |
|:---|:---|
| 💰 **礼金收支记录** | 支持收礼/随礼双向记账、姓名/金额/事由/备注全字段管理 |
| 📊 **数据分析看板** | ECharts 月度/年度走势、亲友 TOP10、事由饼图、净现金流看板 |
| 👥 **家庭多成员协作** | 家庭组创建、成员邀请、主子账本切换与协作权限管理 |
| 📑 **批量导入** | Excel/CSV 批量导入映射 + 人情簿图片 OCR 智能识别录入 |
| 🖨️ **打印与海报导出** | 中式人情簿 A4 打印、对账单 PDF、手机分享长图海报 |
| 🔄 **人情对账** | 亲友往来对账计算、智能还礼金额建议、待补礼/待还礼状态 |
| 🍾 **专属宴席大账本** | 自动归集收礼记录、宴席盈亏分析、现场快速录入台账 |
| 📅 **纪念日备忘** | 亲友生日/结婚纪念日提醒、提前预警、Webhook 自动推送 |
| 🗑️ **回收站** | 跨模块软删除回收（礼金/宴席/纪念日），支持还原与彻底删除 |
| 🔐 **用户权限管理** | 4 级菜单权限（0~3）、权限工单申请审批、双密保问题 |
| 📋 **操作审计日志** | 全操作审计记录、模块级过滤、批量清理 |
| 📢 **系统广播** | 全员/管理员定向广播、已读未读标记 |
| 🔔 **Webhook 通知** | 多渠道推送（企微/钉钉/飞书/Bark/PushPlus/Server酱）、推送矩阵 |
| ☁️ **WebDAV 备份** | 远端自动定时备份、AES-256 加密、数据级合并恢复 |
| 🤖 **AI 智能助手** | 多配置管理、联网搜索、本地知识引擎兜底、OCR 图片识别 |
| 📝 **自然语言记账** | "收张三结婚礼金888" 一句话记账，支持多条复合语句 |
| 📤 **导入导出** | CSV/Excel 导入导出、模板下载、编码自动识别 |
| 🔗 **共享外链** | 大账本只读共享链接、可选访问密码、隐私脱敏 |
| 🌙 **黑夜模式** | 白天/黑夜主题切换、防偷窥遮罩模式 |

### 移动端特性
- 📱 **本地离线运行**：Flask 后端嵌入 APK，SQLite 数据库存储在手机私有空间，无需联网
- 📐 **响应式布局**：自动适配手机屏幕（360px~414px）、刘海屏安全区域、底部手势条
- 🎯 **触控优化**：所有可点击元素最小 44×44px，表格横向可滚动，卡片式布局
- 🔄 **横竖屏适配**：支持竖屏为主，横屏自动调整布局

---

## 🛠️ 项目架构

```
gift_bookkeeping_apk/
├── .github/
│   └── workflows/
│       └── build.yml              # GitHub Actions 构建 workflow (Android APK + iOS IPA)
├── templates/                     # 页面 UI 模板（25 个，Bootstrap 5 + FontAwesome 6）
│   ├── base.html                  # 基础模板（含移动端响应式 CSS、安全区域适配）
│   ├── index.html                 # 礼金账本首页
│   ├── login.html / register.html # 登录注册
│   ├── dashboard.html             # 数据分析看板
│   ├── family.html                # 家庭多成员记账
│   ├── banquets.html / banquet_detail.html  # 专属宴席
│   ├── reconciliation.html        # 人情对账
│   ├── reminders.html             # 纪念日备忘
│   ├── recycle_bin.html           # 回收站
│   ├── ai_assistant.html          # AI 助手
│   ├── admin_*.html               # 管理后台页面（用户/日志/广播/Webhook/备份/AI配置）
│   ├── permission_tickets.html    # 权限工单
│   ├── print_giftbook.html        # 人情簿打印
│   ├── poster_template.html       # 海报模板
│   └── shared_ledger.html         # 共享外链
├── static/                        # 静态资源（本地化，不依赖 CDN）
│   ├── vendor/                    # 第三方库
│   │   ├── bootstrap/5.3.0/      # Bootstrap 5.3 CSS + JS
│   │   ├── font-awesome/6.4.0/   # FontAwesome 图标
│   │   ├── bootstrap-icons/      # Bootstrap Icons
│   │   ├── echarts/5.5.0/        # ECharts 图表库
│   │   └── html2canvas/1.4.1/    # 海报导出
│   ├── manifest.json              # PWA Manifest
│   └── sw.js                      # Service Worker
├── app.py                         # 后端业务逻辑与路由控制器（3291 行，179 条路由）
├── models.py                      # 数据库模型（22+ 个模型，1129 行）
├── routes_ext.py                  # 扩展路由（回收站/对账/宴席/提醒/备份/Webhook/广播/打印/OCR）
├── routes_ai.py                   # AI 助手路由
├── ai_service.py                  # AI 核心服务层
├── pdf_generator.py               # PDF 对账单生成（ReportLab）
├── gift_utils.py                  # 人情业务工具（NLP解析/还礼建议/对账计算）
├── webhook_utils.py               # Webhook 多渠道推送
├── webdav_utils.py                # WebDAV 备份工具
├── web_search.py                  # AI 联网搜索（DuckDuckGo）
├── _daemon_lock.py                # 守护线程单实例锁
├── main.py                        # 移动端启动入口（Flask 本地服务 + WebView 容器）
├── buildozer.spec                 # Buildozer Android 打包配置
├── requirements.txt               # Python 依赖清单
├── .gitignore
└── README.md
```

---

## 🚀 如何获取与安装

### 方式一：GitHub Releases 下载（最推荐）

1. 访问本仓库的 **[Releases 页面](../../releases)**
2. 找到 **Latest** 正式版发布
3. 在 **Assets** 列表中下载：
   - `GiftBookkeeping-Android-APK/*.apk` → Android 安装包
   - `GiftBookkeeping-iOS-IPA-Unsigned/*.ipa` → iOS 未签名包

### 方式二：GitHub Actions 构建产物下载

1. 访问仓库的 **Actions** 标签页
2. 点击最新的运行记录
3. 在 **Artifacts** 中下载：
   - `GiftBookkeeping-Android-APK` → Android APK
   - `GiftBookkeeping-iOS-IPA-Unsigned` → iOS IPA（未签名）

### 方式三：本地构建

#### Android APK 本地构建（Linux / WSL2）

```bash
# 1. 安装系统依赖
sudo apt update
sudo apt install -y git zip unzip openjdk-17-jdk autoconf libtool pkg-config \
    zlib1g-dev libncurses5-dev libncursesw5-dev libtinfo5 cmake libffi-dev \
    libssl-dev build-essential ccache libsdl2-dev libsdl2-image-dev \
    libsdl2-mixer-dev libsdl2-ttf-dev

# 2. 安装 buildozer
pip install cython "buildozer>=1.5.0" virtualenv

# 3. 执行编译打包
buildozer android debug

# 4. 生成的 APK 位于 bin/ 目录下
ls bin/*.apk
```

#### iOS IPA 本地构建（macOS）

```bash
# 1. 安装 Xcode 15+ 和 Python 3.11+
brew install python@3.11

# 2. 安装依赖
pip3 install flask flask-sqlalchemy flask-wtf flask-login werkzeug cryptography requests

# 3. 使用 Xcode 构建未签名 IPA
# (详细步骤参见 GitHub Actions workflow 中的 iOS 构建配置)
```

---

## 💻 本地调试运行（桌面端）

```bash
# 安装依赖
pip install -r requirements.txt

# 方式一：直接运行 Flask Web 服务
python app.py
# 访问 http://127.0.0.1:11443

# 方式二：运行 main.py（自动启动 Flask + 打开浏览器）
python main.py
# 访问 http://127.0.0.1:8765
```

**默认管理员账号**：`admin` / **初始密码**：`admin123`

---

## 📋 构建 Workflow 说明

### 触发条件

- **push 到 main/master 分支**：自动构建 Android APK
- **打 tag（v* 格式）**：自动构建 Android APK + iOS IPA，并发布到 GitHub Releases
- **手动触发**：在 Actions 页面点击 "Run workflow" 手动触发构建

### Workflow 文件

路径：`.github/workflows/build.yml`

| Job | Runner | 产物 | 说明 |
|:---|:---|:---|:---|
| `build-android` | ubuntu-22.04 | `bin/*.apk` | Buildozer 编译 Android APK |
| `build-ios` | macos-14 | `*.ipa`（未签名） | Xcode 构建 iOS IPA（需自行签名） |

### 产物下载

- **Android**：Actions → 最新运行 → Artifacts → `GiftBookkeeping-Android-APK`
- **iOS**：Actions → 最新运行 → Artifacts → `GiftBookkeeping-iOS-IPA-Unsigned`
- **Release**：Releases 页面直接下载 `.apk` / `.ipa` 文件

---

## 📱 移动端适配说明

### 响应式布局
- 使用 `rem`/`em`/`vw`/`vh`/百分比等相对单位替代固定像素
- 最大宽度限制：`.container { max-width: 100% }`，移动端不留多余边距
- 表格自动横向滚动（`.table-responsive`），避免内容压缩

### 安全区域适配
```css
:root {
    --safe-area-top: env(safe-area-inset-top, 0px);
    --safe-area-bottom: env(safe-area-inset-bottom, 0px);
}
body {
    padding-top: env(safe-area-inset-top);
    padding-bottom: env(safe-area-inset-bottom);
}
```

### 触控优化
- 所有按钮最小 44×44px（符合 Apple HIG 和 Material Design 触控目标规范）
- 表单输入框最小高度 44px
- 禁用文本长按选择菜单（`-webkit-touch-callout: none`）
- 禁用点击高亮闪烁（`-webkit-tap-highlight-color: transparent`）

### 分辨率适配
已验证以下常见手机分辨率：
- 360×640（小屏手机）
- 390×844（iPhone 12/13/14）
- 414×896（iPhone 11/XR）
- 412×915（Android 大屏）

---

## 🔒 初始管理员与安全说明

- **管理员账号**：`admin`
- **初始密码**：`admin123`
- **默认密保问题**：系统默认安全问题：您的默认备用验证码是？
- **默认密保答案**：`admin`
- **建议**：首次登录后在个人安全设置页面修改密码与密保问题

---

## 📄 开源许可证

MIT License

---

## 🌐 项目多形态交付与仓库矩阵

| 形态 | GitHub 仓库 | 适用场景 |
|:---|:---|:---|
| 🖥️ **Web 原生部署版** | [gift-bookkeeping-app](https://github.com/18227370901/gift-bookkeeping-app.git) | 本地 Python 环境、虚拟主机、VPS 单机运行 |
| 🐳 **Docker Compose 版** | [gift-bookkeeping-app-docker](https://github.com/18227370901/gift-bookkeeping-app-docker.git) | 企业生产服务器、容器编排、Nginx 反代 |
| 📱 **移动端 APK 版** | [gift_bookkeeping_apk](https://github.com/18227370901/gift_bookkeeping_apk.git) | Android/iOS 手机脱机离线使用 |
