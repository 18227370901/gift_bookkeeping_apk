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

> **V3.1 更新**：根治 APK 安装后页面空白问题——修复 buildozer.spec 无效 `python_depends` 键（导致 Flask 扩展库未进包）、新增 WebView 内嵌启动诊断页（就绪自动跳转/失败展示报错）、注入 `usesCleartextTraffic` 保障本地 HTTP 可达。全量同步源项目最新功能（181条路由）。

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
├── .github/workflows/build.yml     # GitHub Actions (Android APK + iOS IPA)
├── templates/                       # 页面 UI 模板（25 个）
│   ├── base.html                    # 基础模板（含移动端响应式 CSS）
│   ├── index.html                   # 礼金账本首页
│   ├── login.html / register.html   # 登录注册
│   ├── dashboard.html               # 数据分析看板
│   ├── family.html                  # 家庭多成员记账
│   ├── banquets.html / banquet_detail.html  # 专属宴席
│   ├── reconciliation.html          # 人情对账
│   ├── reminders.html               # 纪念日备忘
│   ├── recycle_bin.html             # 回收站
│   ├── ai_assistant.html            # AI 助手
│   ├── admin_*.html                 # 管理后台页面
│   ├── permission_tickets.html      # 权限工单
│   ├── print_giftbook.html          # 人情簿打印
│   ├── poster_template.html         # 海报模板
│   └── shared_ledger.html           # 共享外链
├── static/                          # 静态资源（本地化）
│   └── vendor/                      # Bootstrap/FA/ECharts/html2canvas
├── app.py                           # 后端业务逻辑（3376行, 181条路由）
├── models.py                        # 数据模型（22+个）
├── routes_ext.py                    # 扩展路由
├── routes_ai.py                     # AI 助手路由
├── ai_service.py                    # AI 核心服务层
├── pdf_generator.py                 # PDF 对账单生成
├── gift_utils.py                    # 人情业务工具
├── webhook_utils.py                 # Webhook 多渠道推送
├── webdav_utils.py                  # WebDAV 备份工具
├── web_search.py                    # AI 联网搜索
├── _daemon_lock.py                  # 守护线程单实例锁
├── main.py                          # 移动端启动入口（Flask + WebView）
├── buildozer.spec                   # Buildozer 打包配置 (v3.0.0)
├── requirements.txt                 # Python 依赖清单
└── README.md
```

---

## 🚀 如何获取与安装

### 方式一：GitHub Releases 下载

1. 访问本仓库的 **[Releases 页面](../../releases)**
2. 下载 **v3.1** 版本中的：
   - `GiftBookkeeping-Android-APK-v3.1/*.apk` → Android 安装包
   - `GiftBookkeeping-iOS-IPA-v3.1-Unsigned/*.ipa` → iOS 未签名包

### 方式二：GitHub Actions 构建产物下载

1. 访问仓库的 **Actions** 标签页
2. 点击最新的运行记录
3. 在 **Artifacts** 中下载：
   - `GiftBookkeeping-Android-APK-v3.1` → Android APK
   - `GiftBookkeeping-iOS-IPA-v3.1-Unsigned` → iOS IPA

### 方式三：本地构建

#### Android APK（Linux / WSL2）

```bash
sudo apt update && sudo apt install -y git zip unzip openjdk-17-jdk autoconf libtool \
    pkg-config zlib1g-dev cmake libffi-dev libssl-dev build-essential ccache \
    libsdl2-dev libsdl2-image-dev libsdl2-mixer-dev libsdl2-ttf-dev
pip install cython "buildozer>=1.5.2" virtualenv
buildozer android debug
ls bin/*.apk
```

#### iOS IPA（macOS）

```bash
brew install python@3.11
pip3 install flask flask-sqlalchemy flask-wtf flask-login werkzeug requests
# 使用 Xcode 构建未签名 IPA（详见 GitHub Actions workflow）
```

---

## 💻 本地调试运行（桌面端）

```bash
pip install -r requirements.txt

# 方式一：直接运行 Flask
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

- **push 到 main/master**：自动构建 Android APK
- **打 tag（v* 格式）**：自动构建 Android APK + iOS IPA，并发布到 GitHub Releases
- **手动触发**：在 Actions 页面点击 "Run workflow"

### Workflow 文件

路径：`.github/workflows/build.yml`

| Job | Runner | 产物 | Artifact名称 |
|:---|:---|:---|:---|
| `build-android` | ubuntu-22.04 | `bin/*.apk` | GiftBookkeeping-Android-APK-v3.1 |
| `build-ios` | macos-14 | `*.ipa`（未签名） | GiftBookkeeping-iOS-IPA-v3.1-Unsigned |

### V3.1 空白页面真实根因修复

V3.0/V3.0.1 安装后页面空白（如魅族20 / Android 16 实测）的真实根因与修复：

| 根因 | 修复 |
|:---|:---|
| `python_depends` 不是有效 buildozer 键被静默忽略，flask-sqlalchemy/flask-wtf/flask-login 从未进包，`from app import app` 即 ImportError | requirements 直接列出全部包，p4a 对无 recipe 的纯 Python 包自动 pip 安装 |
| Android 9+ 明文 HTTP 默认禁用，WebView 可能拒载 `http://127.0.0.1` | `extra_manifest_args.txt` 注入 `usesCleartextTraffic="true"` |
| 启动时序依赖固定延迟，首次建表可能 >10 秒 | WebView 首屏加载内嵌诊断页，JS 每 700ms 轮询，就绪后自动跳转 |
| 启动失败用户只能看到白屏，无法定位 | 失败时 12 秒后将 traceback 渲染到页面，可直接截图反馈 |
| 无现场日志可提取 | 双通道日志：logcat + `files/app_debug.log`（可 adb pull） |

### V3.0 构建修复要点

| 问题 | V2.0 | V3.0 |
|:---|:---|:---|
| requirements | cryptography 需Rust工具链编译失败 | 移除，代码内 try/except 自动降级 |
| cython | `<3.0` 限制过时 | 无限制 |
| android.api | 34 | 33 |

---

## 📱 移动端适配说明

### 响应式布局
- `viewport-fit=cover` + `maximum-scale=5.0`
- `rem`/`em`/`vw`/`vh`/百分比相对单位
- 断点：`@media 768px` + `@media 360px` + 横屏

### 安全区域适配
```css
body {
    padding-top: env(safe-area-inset-top);
    padding-bottom: env(safe-area-inset-bottom);
}
```

### 触控优化
- 按钮最小 44×44px（Apple HIG / Material Design）
- 禁用长按选择菜单和点击高亮
- 表格横向滚动，卡片式布局

### 已验证分辨率
360×640 / 390×844 / 414×896 / 412×915

---

## 🔒 初始管理员与安全说明

- **管理员账号**：`admin`
- **初始密码**：`admin123`
- **建议**：首次登录后在个人安全设置页面修改密码与密保问题

---

## 🌐 项目多形态交付与仓库矩阵

| 形态 | GitHub 仓库 | 适用场景 |
|:---|:---|:---|
| 🖥️ **Web 原生部署版** | [gift-bookkeeping-app](https://github.com/18227370901/gift-bookkeeping-app.git) | 本地 Python 环境、VPS 单机运行 |
| 🐳 **Docker Compose 版** | [gift-bookkeeping-app-docker](https://github.com/18227370901/gift-bookkeeping-app-docker.git) | 企业生产服务器、容器编排 |
| 📱 **移动端 APK 版** | [gift_bookkeeping_apk](https://github.com/18227370901/gift_bookkeeping_apk.git) | Android/iOS 手机脱机离线使用 |

---

## 📄 开源许可证

MIT License
