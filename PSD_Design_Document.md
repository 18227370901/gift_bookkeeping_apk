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

# 礼金记账簿 移动端 APK — PSD 项目系统设计与重构决策文档

> **项目路径**：`gift_bookkeeping_apk/`  
> **文档版本**：V3.0（全量同步 + 构建修复版）  
> **生成日期**：2026-10-08  
> **架构基线**：本地嵌入式 Flask 服务 + 原生 WebView 容器  
> **代码规模**：12个Python文件 + 25个HTML模板 + 181条路由 + 22+数据模型

---

## 目录

1. **项目全局概览** — 业务定位 · 技术栈全景 · 架构拓扑
2. **V3.0 核心变更** — 构建修复 · 版本区分 · 全量同步
3. **功能模块全量清单** — 25页面 · 181条路由 · 22+数据模型
4. **技术栈全景** — 后端 · 前端 · 移动端 · CI/CD
5. **数据持久化设计** — ER关系 · 加密策略 · 迁移策略
6. **移动端适配设计** — 响应式布局 · 安全区域 · 触控优化
7. **构建与CI/CD** — Buildozer · GitHub Actions · Android+iOS双平台
8. **工程与安全保障** — 环境变量 · 认证鉴权 · 安全清单
9. **版本演进路线图** — V1.0 → V2.0 → V3.0

---

## 1. 项目全局概览

### 1.1 业务定位

本项目是「人情记账宝」产品矩阵的 **移动端交付形态**，定位为 Android/iOS 手机/平板上的全功能离线礼金记账工具。

| 形态 | 仓库 | 定位 | 功能覆盖 |
|:---|:---|:---|:---|
| Web 原生部署版 | `gift-bookkeeping-app` | 全功能基准版 | 100% 功能基准 |
| Docker Compose 版 | `gift-bookkeeping-app-docker` | 容器化部署版 | 100%（与传统版共享） |
| **移动端 APK 版** | **`gift_bookkeeping_apk`**（本仓库） | **Android/iOS 离线应用** | **100% 全量同步** |

### 1.2 架构拓扑图

```mermaid
graph TB
    subgraph device["📱 移动设备 (Android/iOS)"]
        APK["APK 应用 v3.0.0<br/>arm64-v8a<br/>API 21+"] --> WV["原生 WebView<br/>main.py 容器层<br/>jnius → WebView"]
        APK --> FLASK_LOCAL["本地 Flask 服务<br/>127.0.0.1:8765<br/>app.py (3376行, 181路由)"]
        FLASK_LOCAL --> DB_LOCAL["SQLite<br/>本地私有存储<br/>data/gift_bookkeeping.db"]
        FLASK_LOCAL -->|"Jinja2 SSR"| TPL_LOCAL["templates/ 25个HTML<br/>Bootstrap5 + FA6 + ECharts<br/>本地化引用"]
    end
    WV -->|"加载 http://127.0.0.1:8765"| FLASK_LOCAL
    TPL_LOCAL -->|"HTML/CSS/JS"| WV
```

> **架构关键特征**：
> - **完全离线**：所有业务逻辑在设备本地运行，无需互联网
> - **数据私有**：SQLite 数据库存储在手机私有空间
> - **资源内嵌**：25个HTML模板 + 静态资源全部打包进APK，不依赖CDN
> - **本地Flask**：Flask后端在设备后台线程运行，WebView加载本地页面

### 1.3 版本演进对比

| 指标 | V1.0 | V2.0 | **V3.0** |
|:---|:---|:---|:---|
| 架构模式 | 远程WebView壳 | 本地Flask+WebView | **本地Flask+WebView（修复构建）** |
| 代码文件 | 3个.py+8个.html | 12个.py+25个.html | **12个.py+25个.html** |
| 路由数量 | 32条 | 179条 | **181条** |
| 数据模型 | 3个 | 22+个 | **22+个** |
| 构建状态 | — | ❌失败 | **✅修复（p4a recipe分离）** |
| 版本号 | 1.0.0 | 2.0.0 | **3.0.0** |

---

## 2. V3.0 核心变更

### 2.1 V2.0 构建失败根因与修复

V2.0 的 GitHub Actions 构建在 `Build APK with Buildozer` 步骤失败（exit code 1，运行12分钟）。根因：

| # | 根因 | V2.0配置 | V3.0修复 |
|:--|:---|:---|:---|
| 1 | requirements混入非p4a recipe包名 | `flask_sqlalchemy,flask_wtf,flask_login,werkzeug,requests,cryptography` | 改为p4a原生recipe: `flask,sqlalchemy,markupsafe,sqlite3` + `python_depends` 纯Python包 |
| 2 | cryptography需Rust工具链 | 包含在requirements中 | 移除，代码中try/except延迟导入自动降级 |
| 3 | cython<3.0限制过时 | `"cython<3.0"` | 改为无限制 `"cython"` |
| 4 | android.api=34兼容性 | `android.api = 34` | 降为 `android.api = 33` |

### 2.2 buildozer.spec V3.0 配置

```
version = 3.0.0
requirements = python3,hostpython3,openssl,sqlite3,pyjnius,kivy,flask,sqlalchemy,markupsafe
python_depends = flask-sqlalchemy,flask-wtf,flask-login,werkzeug,requests,itsdangerous,click,blinker,jinja2
android.api = 33
android.archs = arm64-v8a
```

**关键设计**：
- `requirements` 仅包含 p4a 原生 recipe（需C交叉编译的包）
- `python_depends` 安装纯Python包（pip安装，无需C编译）
- 移除 `cryptography`（需Rust工具链），代码中AES加密通过try/except自动降级

### 2.3 全量功能同步

从源项目 `gift_bookkeeping_app` 同步到本仓库的完整文件：

| 文件 | 功能 | 行数 | 同步状态 |
|:---|:---|:---|:---|
| app.py | 后端业务逻辑 | 3376行 | ✅ |
| models.py | 数据模型 | 1129行 | ✅ |
| routes_ext.py | 扩展路由 | ~5000行 | ✅ |
| routes_ai.py | AI助手路由 | ~400行 | ✅ |
| ai_service.py | AI核心服务 | ~600行 | ✅ |
| pdf_generator.py | PDF生成 | ~250行 | ✅ |
| gift_utils.py | 人情业务工具 | ~350行 | ✅ |
| webhook_utils.py | Webhook推送 | ~1200行 | ✅ |
| webdav_utils.py | WebDAV备份 | ~700行 | ✅ |
| web_search.py | AI联网搜索 | ~80行 | ✅ |
| _daemon_lock.py | 守护线程锁 | ~45行 | ✅ |
| templates/ (25个) | 全部页面模板 | — | ✅ |
| static/ | 全部静态资源 | — | ✅ |

---

## 3. 功能模块全量清单

### 3.1 页面/路由清单（25个页面，181条路由）

| # | 页面模板 | 路由 | 核心功能 |
|:---|:---|:---|:---|
| 1 | base.html | — | 全局布局（导航栏/Flash/主题/防偷窥/广播/移动端CSS） |
| 2 | index.html | `/` | 礼金账本首页（CRUD/搜索/筛选/排序/分页/批量/NLP/导入导出/打印/海报） |
| 3 | login.html | `/login` | 登录（风控/验证码/记住密码） |
| 4 | register.html | `/register` | 注册（双密保/邀请制） |
| 5 | forgot_password.html | `/forgot-password` | 找回密码（双密保/风控） |
| 6 | change_password.html | `/change-password` | 修改密码 |
| 7 | profile_security.html | `/profile/security` | 个人安全设置 |
| 8 | dashboard.html | `/dashboard` | 数据分析看板（ECharts） |
| 9 | family.html | `/family` | 家庭多成员协作记账 |
| 10 | banquets.html | `/banquets` | 专属宴席大账本列表 |
| 11 | banquet_detail.html | `/banquet/<id>` | 宴席详情（盈亏分析/现场录入） |
| 12 | reconciliation.html | `/reconciliation` | 人情对账（往来明细/还礼建议） |
| 13 | reminders.html | `/reminders` | 纪念日备忘（提醒/Webhook推送） |
| 14 | recycle_bin.html | `/recycle_bin` | 回收站（跨模块/还原/彻底删除） |
| 15 | ai_assistant.html | `/ai-assistant` | AI智能助手 |
| 16 | permission_tickets.html | `/permission_tickets` | 权限工单 |
| 17 | admin_users.html | `/admin/users` | 用户管理（4级权限/双密保） |
| 18 | admin_logs.html | `/admin/logs` | 操作审计日志 |
| 19 | admin_broadcasts.html | `/admin/broadcasts` | 系统广播发布 |
| 20 | admin_webhooks.html | `/admin/webhooks` | Webhook通知配置 |
| 21 | admin_backups.html | `/admin/backups` | WebDAV备份管理 |
| 22 | admin_ai_config.html | `/admin/ai-config` | AI配置管理 |
| 23 | print_giftbook.html | `/print_giftbook` | 人情簿A4打印 |
| 24 | poster_template.html | `/poster_template` | 海报模板 |
| 25 | shared_ledger.html | `/shared_ledger/<token>` | 共享外链 |

### 3.2 数据模型清单（22+个模型）

User, GiftRecord, Banquet, AnniversaryReminder, SystemSetting, RegistrationToken, LoginRisk, SecurityRisk, OperationLog, Broadcast, BroadcastRead, SharedLedgerLink, WebhookConfig, WebhookLog, BackupConfig, ScheduledBackupTask, ScheduledTaskExecutionLog, BackupAttachment, PermissionTicket, ChatSession, ChatMessage, AIQueryLog, FamilyGroup, FamilyMember

---

## 4. 技术栈全景

### 4.1 后端

| 技术 | 版本 | 说明 |
|:---|:---|:---|
| Flask | 3.0.3 | Web框架 |
| Flask-SQLAlchemy | 3.1.1 | ORM |
| Flask-WTF | 1.2.1 | CSRF防护 |
| Flask-Login | 0.6.3 | 身份认证 |
| Werkzeug | 3.0.3 | 密码哈希/ProxyFix |
| cryptography | 42.0.8 | AES-256-GCM（可选，延迟导入） |
| requests | >=2.31.0 | HTTP客户端 |

### 4.2 可选依赖（延迟导入，自动降级）

| 依赖 | 功能 | 降级行为 |
|:---|:---|:---|
| openai | AI聊天/OCR | 本地知识引擎兜底 |
| duckduckgo_search | AI联网搜索 | 跳过搜索 |
| reportlab | PDF对账单 | 跳过PDF功能 |
| openpyxl | Excel导入导出 | 跳过Excel功能 |
| pyzipper | 加密备份 | 跳过加密功能 |
| psycopg2-binary | PostgreSQL | 移动端使用SQLite |

### 4.3 前端（全部本地化）

| 技术 | 版本 | 引用方式 |
|:---|:---|:---|
| Bootstrap | 5.3.0 | static/vendor/ |
| FontAwesome | 6.4.0 | static/vendor/ |
| Bootstrap Icons | 1.11.3 | static/vendor/ |
| ECharts | 5.5.0 | static/vendor/ |
| html2canvas | 1.4.1 | static/vendor/ |

### 4.4 移动端

| 技术 | 版本 | 说明 |
|:---|:---|:---|
| Kivy | >=2.3.0 | Python原生UI框架 |
| pyjnius | — | Python→Java接口 |
| Buildozer | >=1.5.2 | APK编译 |
| 目标架构 | arm64-v8a | API 33/min 21 |

---

## 5. 数据持久化设计

- **引擎**：SQLite（WAL模式，busy_timeout=30s）
- **存储位置**：`data/gift_bookkeeping.db`（设备私有存储）
- **迁移策略**：`_SCHEMA_VERSION` 幂等迁移 + 历史SQL增量
- **表数量**：22+张业务表

### 加密策略

| 数据类型 | 加密方式 |
|:---|:---|
| 用户密码 | Werkzeug pbkdf2_sha256 不可逆哈希 |
| 密保答案 | pbkdf2_sha256 哈希 + AES-256-GCM 密文 |
| AI API Key | AES-256-GCM 密文 |
| Webhook Secret | AES-256-GCM 密文 |
| WebDAV密码 | AES-256-GCM 密文 |
| 备份文件 | AES-256 Zip加密（pyzipper） |

> **移动端降级**：cryptography未编译进APK时，AES加密函数通过try/except返回明文回退，不影响核心记账功能。

---

## 6. 移动端适配设计

### 6.1 响应式布局

| 适配维度 | 实现方式 |
|:---|:---|
| 视口设置 | `viewport-fit=cover` + `maximum-scale=5.0` |
| 相对单位 | rem/em/vw/vh/百分比 |
| 断点 | `@media 768px` + `@media 360px` + 横屏 |
| 表格适配 | `.table-responsive` 横向滚动 |

### 6.2 安全区域适配

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

### 6.3 触控优化

| 优化项 | 规范 |
|:---|:---|
| 按钮最小尺寸 | 44×44px |
| 输入框最小高度 | 44px |
| 禁用长按菜单 | `-webkit-touch-callout: none` |
| 禁用高亮闪烁 | `-webkit-tap-highlight-color: transparent` |

### 6.4 已验证分辨率

360×640 / 390×844 / 414×896 / 412×915

---

## 7. 构建与CI/CD

### 7.1 GitHub Actions Workflow

路径：`.github/workflows/build.yml`

| Job | Runner | 产物 | Artifact名称 |
|:---|:---|:---|:---|
| build-android | ubuntu-22.04 | bin/*.apk | GiftBookkeeping-Android-APK-v3.0 |
| build-ios | macos-14 | *.ipa | GiftBookkeeping-iOS-IPA-v3.0-Unsigned |

### 7.2 触发条件

| 条件 | Android | iOS | Release |
|:---|:---|:---|:---|
| push到main/master | ✅ | ❌ | ✅ (tag: v3.0-latest) |
| 打tag（v*） | ✅ | ✅ | ✅ |
| 手动触发 | ✅ | ✅ | ❌ |

### 7.3 构建配置要点

- **Cython**：无版本限制（移除`<3.0`过时限制）
- **Buildozer**：`>=1.5.2`
- **ANDROID_HOME/ANDROID_NDK_HOME**：构建步骤中显式设置
- **构建失败诊断**：失败时打印最后200行日志 + 上传build_log.txt artifact
- **版本区分**：Artifact名称含`v3.0`，Release tag含`v3.0-latest`

---

## 8. 工程与安全保障

### 8.1 安全防护清单

| 维度 | 实现 | 有效性 |
|:---|:---|:---|
| CSRF防护 | session token + POST校验 | ✅ |
| XSS防护 | Jinja2自动转义 | ✅ |
| 密码哈希 | pbkdf2_sha256 + 16字节盐 | ✅ |
| 暴力破解防护 | 持久化风控 + 验证码 + 锁定 | ✅ |
| 会话安全 | HttpOnly + SameSite + 超时 | ✅ |
| 注册控制 | 邀请令牌制 | ✅ |
| 审计日志 | 全操作记录 + 90天清理 | ✅ |
| 数据隔离 | 普通用户备份仅含本人数据 | ✅ |

---

## 9. 版本演进路线图

| 版本 | 架构 | 路由 | 构建 | 状态 |
|:---|:---|:---|:---|:---|
| V1.0 | 远程WebView壳 | 32 | ✅ | 已废弃 |
| V2.0 | 本地Flask+WebView | 179 | ❌失败 | 已被V3.0替代 |
| **V3.0** | **本地Flask+WebView** | **181** | **✅修复** | **当前版本** |
