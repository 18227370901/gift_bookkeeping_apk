# -*- coding: utf-8 -*-
"""
Webhook 与长连接通知工具模块
使用企业微信官方 Python SDK (wecom-aibot-python-sdk) 实现长连接与智能机器人对接
同时使用 requests 库提供连接池与重试，支持钉钉、飞书、Bark、PushPlus、Server酱及通用 HTTP Webhook
用于实时推送礼金记账的新增、修改、删除、纪念日到期与安全提醒
"""

import json
import os
import logging
import urllib.parse
import threading
import time
import sqlite3
import asyncio
from datetime import datetime
import requests
import urllib3

# Webhook 模块日志器
logger = logging.getLogger('webhook_utils')
logger.setLevel(logging.DEBUG)

# 禁用 self-signed SSL 证书警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 延迟导入企业微信 aibot SDK（可选依赖）
# V10.10.16 性能优化：aibot SDK 间接拉入 aiohttp + websockets ~10-16MB，
# 移至首次长连接认证/发送/监听时才加载；普通 Webhook URL 模式永不触发
HAS_AIBOT_SDK = None  # None=未检测, True/False=已检测

def _import_aibot():
    """首次调用时延迟导入 aibot SDK，返回 (WSClient, WSClientOptions, generate_req_id) 或 None"""
    global HAS_AIBOT_SDK
    if HAS_AIBOT_SDK is True:
        from aibot import WSClient, WSClientOptions, generate_req_id
        return WSClient, WSClientOptions, generate_req_id
    if HAS_AIBOT_SDK is False:
        return None, None, None
    try:
        from aibot import WSClient, WSClientOptions, generate_req_id
        HAS_AIBOT_SDK = True
        return WSClient, WSClientOptions, generate_req_id
    except ImportError:
        HAS_AIBOT_SDK = False
        return None, None, None

# V10.10.15：监听线程通过 raw sqlite3 读取 bot_secret，需调用 decrypt_credential 解密
# （models.py 的 bot_secret 是 property，raw SQL 读取的是 AES-256-GCM 密文）
try:
    from models import decrypt_credential
except Exception:
    decrypt_credential = None

from _daemon_lock import try_acquire_daemon_lock


def _run_async(coro):
    """在同步线程中安全执行异步协程并返回结果"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _send_payload(url, payload_dict, headers=None, timeout=12):
    """底层 HTTP POST 数据投递，使用 requests 连接池，严格解析响应状态与平台错误码"""
    try:
        session = requests.Session()
        req_headers = {
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "GiftBookkeepingWebhook/2.0"
        }
        if headers:
            req_headers.update(headers)

        resp = session.post(url, json=payload_dict, headers=req_headers, timeout=timeout, verify=False)
        status_code = resp.status_code
        body = resp.text

        try:
            res_data = resp.json()
            if isinstance(res_data, dict):
                if "errcode" in res_data and res_data["errcode"] != 0:
                    errmsg = res_data.get("errmsg", "未知错误")
                    return False, status_code, f"平台返回错误 [errcode: {res_data['errcode']}]: {errmsg}"
                if "code" in res_data and res_data["code"] not in (0, 200) and "msg" in res_data:
                    return False, status_code, f"平台返回错误 [code: {res_data['code']}]: {res_data['msg']}"
                if "StatusCode" in res_data and res_data["StatusCode"] != 0:
                    return False, status_code, f"平台返回错误: {res_data.get('StatusMessage', '')}"
        except Exception:
            pass

        if 200 <= status_code < 300:
            return True, status_code, body
        else:
            return False, status_code, f"HTTP {status_code}: {body[:200]}"
    except requests.exceptions.Timeout:
        return False, 504, "连接超时：无法在规定时间内连接到 Webhook 目标地址，请检查网络或目标 URL"
    except requests.exceptions.ConnectionError as ce:
        return False, 502, f"无法连接到 Webhook 目标地址（网络不可达或连接失败）：{str(ce)}"
    except Exception as e:
        return False, 0, str(e)


def _sanitize_log_data(data):
    """递归脱敏日志中的敏感字段（密码、Token、Secret等）"""
    if isinstance(data, dict):
        res = {}
        for k, v in data.items():
            kl = str(k).lower()
            if any(s in kl for s in ['secret', 'token', 'pass', 'key', 'credential', 'auth', 'webhook_url']):
                res[k] = '***MASKED***'
            else:
                res[k] = _sanitize_log_data(v)
        return res
    elif isinstance(data, list):
        return [_sanitize_log_data(x) for x in data]
    return data

def _resolve_db_file():
    """动态获取 SQLite 数据库文件路径（适配 Docker /app/data 挂载与原生环境）"""
    base = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(base, 'data', 'gift_bookkeeping.db'),
        os.path.join(base, 'gift_bookkeeping.db'),
        'gift_bookkeeping.db'
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return candidates[0] if os.path.isdir(os.path.join(base, 'data')) else candidates[1]

# V10.10.17: 统一数据库连接辅助函数，支持 SQLite 与 PostgreSQL 双模式
# 返回 (connection, placeholder)：SQLite 用 '?'，PG 用 '%s'
# 不依赖 Flask app context，与监听线程/推送线程完全兼容
def _get_db_conn():
    """获取原生数据库连接与占位符，PG 用 psycopg2，SQLite 用 sqlite3"""
    _db_url = os.environ.get('DATABASE_URL', '').strip()
    if _db_url.startswith('postgresql://'):
        import psycopg2
        return psycopg2.connect(_db_url), '%s'
    else:
        return sqlite3.connect(_resolve_db_file(), timeout=10), '?'

def record_webhook_log(user_id, webhook_id, event_type, payload, status_code, response_body, is_success, operator_id=None):
    """线程安全写入 Webhook 推送日志表（V10.10.17: 支持 PG）"""
    try:
        conn, ph = _get_db_conn()
        c = conn.cursor()
        c.execute(
            f"""INSERT INTO webhook_logs
               (user_id, operator_id, webhook_id, event_type, payload, status_code, response_body, is_success, created_at)
               VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})""",
            (
                user_id or 1,
                operator_id,
                webhook_id,
                event_type or "notify",
                json.dumps(_sanitize_log_data(payload), ensure_ascii=False)[:2000] if isinstance(payload, (dict, list)) else str(_sanitize_log_data(payload))[:2000],
                status_code or 0,
                str(response_body)[:2000],
                1 if is_success else 0,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            )
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error("[Webhook] 写入推送日志失败: webhook_id=%s, event_type=%s, 错误: %s", webhook_id, event_type, e, exc_info=True)


def validate_wecom_credentials(bot_id, bot_secret):
    """验证企业微信机器人凭证格式有效性与官方 aibot SDK 长连接认证"""
    if not bot_id or not bot_secret:
        return False, "Bot ID 与 Secret 均不能为空"
    b_id = str(bot_id).strip()
    b_sec = str(bot_secret).strip()
    if len(b_id) < 8 or len(b_sec) < 8:
        return False, "Bot ID 或 Secret 长度过短，不符合企业微信机器人凭证规范（至少8位）"
    invalid_patterns = ["test", "error", "123456", "undefined", "null", "bot_id", "secret"]
    if b_id.lower() in invalid_patterns or b_sec.lower() in invalid_patterns:
        return False, "检测到测试/无效占位符，请输入真实有效的企业微信机器人 Bot ID 与 Secret"

    WSClient, WSClientOptions, _ = _import_aibot()
    if not HAS_AIBOT_SDK:
        return False, "Python 环境缺少 wecom-aibot-python-sdk 库"

    async def _test_auth():
        options = WSClientOptions(bot_id=b_id, secret=b_sec)
        client = WSClient(options)
        auth_future = asyncio.get_event_loop().create_future()

        @client.on("authenticated")
        def on_auth():
            if not auth_future.done():
                auth_future.set_result(True)

        @client.on("error")
        def on_err(e):
            if not auth_future.done():
                auth_future.set_exception(e)

        await client.connect()
        try:
            await asyncio.wait_for(auth_future, timeout=8.0)
            return True, "企业微信智能机器人凭证通过官方 aibot SDK 握手认证！"
        except asyncio.TimeoutError:
            return False, "连接企业微信官方长连接服务器超时，请检查网络或稍后重试"
        except Exception as e:
            return False, f"企业微信官方 SDK 认证失败: {str(e)}"
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    try:
        return _run_async(_test_auth())
    except Exception as e:
        return False, f"官方 SDK 认证握手异常: {str(e)}"


def extract_chatid_from_url(url):
    """从存储的 webhook_url 中解析可能携带的目标会话 chatid"""
    if not url:
        return ""
    if "chatid=" in url:
        try:
            parsed = urllib.parse.urlparse(url)
            qs = urllib.parse.parse_qs(parsed.query)
            val = qs.get("chatid", [""])[0].strip()
            if val:
                return val
        except Exception:
            pass
    return ""


# 全局内存缓存，记录已捕获到的最新 chatid (bot_id -> chatid)
_cached_chatids = {}


def test_wecom_long_connection(bot_id, bot_secret, chatid=None):
    """通过官方 wecom-aibot-python-sdk 测试智能机器人凭证与长连接推送"""
    is_v, msg_v = validate_wecom_credentials(bot_id, bot_secret)
    if not is_v:
        return False, 400, f"凭证校验失败: {msg_v}"

    target_chat = chatid or _cached_chatids.get(bot_id)
    if not target_chat:
        try:
            conn = sqlite3.connect(_resolve_db_file(), timeout=5)
            c = conn.cursor()
            row = c.execute("SELECT webhook_url FROM webhook_configs WHERE bot_id = ? AND is_enabled = 1", (bot_id,)).fetchone()
            if row and row[0]:
                cid = extract_chatid_from_url(row[0])
                if cid:
                    target_chat = cid
                    _cached_chatids[bot_id] = cid
            conn.close()
        except Exception:
            pass

    if target_chat:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return send_wecom_long_connection_message(
            bot_id, bot_secret,
            title="【人情礼金记账】智能机器人长连接测试",
            details="这是一条通过企业微信官方 SDK (wecom-aibot-python-sdk) 发送的长连接测试消息。",
            now_str=now_str,
            chatid=target_chat
        )

    return True, 200, "企业微信智能机器人官方 SDK 认证成功！Bot ID 与 Secret 凭证有效，已成功建立 WebSocket 长连接。提示：如需接收通知消息，建议在【Webhook 配置】填写目标群聊 chatid，或直接在企微群内 @机器人 一次即可自动绑定会话！"


def send_wecom_long_connection_message(bot_id, bot_secret, title, details=None, now_str=None, event_type="notify", chatid=None):
    """使用企业微信官方 Python SDK (wecom-aibot-python-sdk) 向指定群聊/会话主动发送 Markdown 消息"""
    is_valid, val_msg = validate_wecom_credentials(bot_id, bot_secret)
    if not is_valid:
        return False, 400, f"凭证校验失败: {val_msg}"

    WSClient, WSClientOptions, _ = _import_aibot()
    if not HAS_AIBOT_SDK:
        return False, 500, "Python 环境缺少 wecom-aibot-python-sdk 库"

    if not now_str:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    target_chat = chatid or _cached_chatids.get(bot_id)
    if not target_chat:
        try:
            conn = sqlite3.connect(_resolve_db_file(), timeout=5)
            c = conn.cursor()
            row = c.execute("SELECT webhook_url FROM webhook_configs WHERE bot_id = ? AND is_enabled = 1", (bot_id,)).fetchone()
            if row and row[0]:
                cid = extract_chatid_from_url(row[0])
                if cid:
                    target_chat = cid
                    _cached_chatids[bot_id] = cid
            conn.close()
        except Exception:
            pass

    if not target_chat:
        return False, 400, "企业微信智能机器人已完成长连接认证，但尚未配置目标群聊/会话 ID (chatid)。请在【Webhook 配置】中填入群聊 chatid，或在企微群内 @机器人 一次即可自动绑定会话！"

    if details and ("\\n" in details or chr(10) in details):
        content = f"### {title}\n> **接入方式**：<font color=\"info\">企业微信智能机器人 (aibot SDK)</font>\n> **时间**：<font color=\"comment\">{now_str}</font>\n\n{details}"
    else:
        content = f"### {title}\n> **接入方式**：<font color=\"info\">企业微信智能机器人 (aibot SDK)</font>\n> **时间**：<font color=\"comment\">{now_str}</font>\n> **详情**：<font color=\"info\">{details or '无额外说明'}</font>"

    async def _async_send():
        options = WSClientOptions(bot_id=bot_id.strip(), secret=bot_secret.strip())
        client = WSClient(options)
        auth_future = asyncio.get_event_loop().create_future()

        @client.on("authenticated")
        def on_auth():
            if not auth_future.done():
                auth_future.set_result(True)

        @client.on("error")
        def on_err(e):
            if not auth_future.done():
                auth_future.set_exception(e)

        await client.connect()
        try:
            await asyncio.wait_for(auth_future, timeout=8.0)
            res = await client.send_message(
                chatid=target_chat,
                body={
                    "msgtype": "markdown",
                    "markdown": {"content": content}
                }
            )
            return True, 200, f"企业微信官方 SDK 成功投递消息至会话 [{target_chat}]！"
        except Exception as e:
            err_str = str(e)
            if "93006" in err_str or "invalid chatid" in err_str:
                return False, 400, f"企微 SDK 推送失败: 目标会话 ID [{target_chat}] 无效或机器人不在该群内。请重新核对群聊 ID 或在群内 @机器人 一次。"
            return False, 400, f"企业微信官方 SDK 消息投递失败: {err_str}"
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    try:
        return _run_async(_async_send())
    except Exception as e:
        return False, 500, f"企业微信 SDK 发送调用异常: {str(e)}"


def test_single_webhook(wh, sender_name="admin"):
    """同步测试单条 Webhook，返回 (success, status_code, message) 并记录日志"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn_type = getattr(wh, "connection_type", "webhook_url") or "webhook_url"
    bot_id = (getattr(wh, "bot_id", "") or "").strip()
    bot_secret = (getattr(wh, "bot_secret", "") or "").strip()
    url = (getattr(wh, "webhook_url", "") or "").strip()
    title = "【Webhook 测试推送】连接验证"
    details = f"测试触发人: {sender_name} | 测试时间: {now_str}"

    if conn_type == "long_connection":
        if not bot_id or not bot_secret:
            return False, 400, "长连接模式下 Bot ID 与 Secret 不能为空"
        chatid = extract_chatid_from_url(url)
        success, code, msg = test_wecom_long_connection(bot_id, bot_secret, chatid=chatid)
        payload = {"mode": "long_connection", "bot_id": bot_id, "title": title, "chatid": chatid}
        record_webhook_log(wh.user_id, wh.id, "test", payload, code, msg, success)
        return success, code, msg

    if not url:
        return False, 400, "Webhook 目标 URL 不能为空"

    # 特殊协议与本地回调兼容
    if url.startswith("wecom://"):
        b_id = bot_id or (url.split("wecom://bot/")[1].split("?")[0] if "wecom://bot/" in url else "")
        c_id = extract_chatid_from_url(url) or _cached_chatids.get(b_id)
        if b_id and bot_secret:
            success, code, msg = test_wecom_long_connection(b_id, bot_secret, chatid=c_id)
            record_webhook_log(wh.user_id, wh.id, "test", {"mode": "wecom_url", "bot_id": b_id, "chatid": c_id}, code, msg, success)
            return success, code, msg

    if any(h in url for h in ["indevs.in", "localhost", "127.0.0.1:11443", ":15001"]) and not ("/send?" in url or "key=" in url):
        msg = "提示：当前填写的 URL 属于本系统的回调接收地址（用于在企微群内 @机器人 时接收事件并捕获 chatid）。若需向企微群推送通知，请填入标准企微群机器人 Webhook 地址（形如 https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...），或选用长连接模式！"
        record_webhook_log(wh.user_id, wh.id, "test", {"url": url}, 400, msg, False)
        return False, 400, msg

    # 标准 HTTP Webhook
    payload = {
        "event": "test",
        "title": title,
        "time": now_str,
        "details": details,
        "source": "gift_bookkeeping_app"
    }
    headers = {}
    if wh.secret_token:
        headers["Authorization"] = f"Bearer {wh.secret_token}"

    if "dingtalk.com" in url:
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": title,
                "text": f"### {title}\n\n- **时间**: {now_str}\n- **说明**: {details}\n\n> 礼金记账系统通知"
            }
        }
    elif "feishu.cn" in url or "larksuite.com" in url:
        payload = {
            "msg_type": "text",
            "content": {"text": f"{title}\n时间: {now_str}\n详情: {details}"}
        }
    elif "qyapi.weixin.qq.com" in url:
        if details and ("\\n" in details or chr(10) in details):
            md_cnt = f"### {title}\n> 时间：<font color=\"comment\">{now_str}</font>\n\n{details}"
        else:
            md_cnt = f"### {title}\n> 时间：<font color=\"comment\">{now_str}</font>\n> 详情：<font color=\"info\">{details or '无'}</font>"
        payload = {
            "msgtype": "markdown",
            "markdown": {"content": md_cnt}
        }
    elif "pushplus.plus" in url:
        token = wh.secret_token
        if not token and "token=" in url:
            parsed = urllib.parse.urlparse(url)
            qs = urllib.parse.parse_qs(parsed.query)
            token = qs.get("token", [""])[0]
        payload = {
            "token": token,
            "title": title,
            "content": f"<h3>{title}</h3><p>时间：{now_str}</p><p>详情：{details}</p>",
            "template": "html"
        }
    elif "ftqq.com" in url:
        payload = {
            "title": title,
            "desp": f"### {title}\n\n- **时间**: {now_str}\n- **说明**: {details}"
        }
    elif "api.day.app" in url:
        payload = {
            "title": title,
            "body": f"{details}\n时间: {now_str}",
            "group": "礼金记账"
        }

    success, code, body = _send_payload(url, payload, headers, timeout=5)
    record_webhook_log(wh.user_id, wh.id, "test", payload, code, body, success)
    return success, code, body


# === 页面标识与事件类型映射 (V10.1 矩阵重构) ===

PAGE_NAMES = {
    'ledger': '礼金账本', 'banquets': '专属宴席', 'reminders': '纪念日备忘',
    'reconciliation': '人情对账', 'recycle_bin': '回收站', 'admin_users': '用户管理',
    'admin_logs': '操作审计日志', 'admin_broadcasts': '系统广播', 'admin_webhooks': 'Webhook通知',
    'admin_backups': 'WebDAV备份', 'ai_assistant': 'AI助手', 'ai_config': 'AI助手配置',
    'security': '系统安全', 'invites': '邀请链接', 'permission_tickets': '权限工单',
    'dashboard': '数据分析', 'family': '家庭记账',
}

ALL_PAGES = list(PAGE_NAMES.keys())

# 矩阵列定义：事件类型 → 显示名称
EVENT_COLUMNS = {
    'create': '新增', 'update': '修改/编辑', 'delete': '删除',
    'batch_delete': '批量删除', 'clear': '清空', 'sync': '同步',
    'restore': '还原', 'status_change': '状态变更',
    'reminder': '提醒', 'broadcast': '广播',
    'security': '安全风控', 'system': '系统配置',
}

# 页面 × 事件适用矩阵（每页面支持哪些事件列）
PAGE_EVENT_MATRIX = {
    'ledger':            ['create', 'update', 'delete', 'batch_delete', 'clear', 'security'],
    'banquets':          ['create', 'update', 'delete', 'batch_delete', 'sync'],
    'reminders':         ['create', 'update', 'delete', 'batch_delete', 'reminder'],
    'reconciliation':    ['sync'],
    'recycle_bin':       ['restore', 'delete', 'batch_delete', 'clear'],
    'admin_users':       ['create', 'update', 'delete', 'batch_delete', 'status_change', 'security'],
    'admin_logs':        ['delete', 'clear', 'security'],
    'admin_broadcasts':  ['create', 'delete', 'status_change', 'broadcast'],
    'admin_webhooks':    ['create', 'update', 'delete', 'clear', 'status_change', 'system'],
    'admin_backups':     ['create', 'update', 'delete', 'clear', 'status_change', 'system', 'security', 'restore'],
    'ai_assistant':      ['create'],
    'ai_config':         ['update', 'system', 'status_change'],
    'security':          ['security'],
    'invites':           ['create', 'delete', 'status_change'],
    'permission_tickets': ['create', 'status_change', 'delete'],
    'family':            ['create', 'delete', 'status_change', 'security'],
}

# 事件类型 → 开关字段名映射
# batch_delete/clear 归 delete 组开关；sync 归 system 组开关；restore 归 status_change 组开关
EVENT_SWITCH_MAP = {
    'create': 'notify_on_add', 'record_create': 'notify_on_add',
    'update': 'notify_on_update', 'record_update': 'notify_on_update',
    'delete': 'notify_on_delete', 'record_delete': 'notify_on_delete',
    'batch_delete': 'notify_on_delete',
    'clear': 'notify_on_delete',
    'sync': 'notify_on_system',
    'restore': 'notify_on_status_change',
    'status_change': 'notify_on_status_change',
    'reminder': 'notify_on_reminder', 'auto_reminder': 'notify_on_reminder',
    'broadcast': 'notify_on_broadcast',
    'security': 'notify_on_security',
    'system': 'notify_on_system',
}

# 需要脱敏的页面（推送消息中不包含敏感数据详情）
SENSITIVE_PAGES = {'ai_assistant', 'ai_config', 'security', 'admin_webhooks', 'admin_backups'}

# 场景化默认提示词模板（支持占位符: {user}/{page}/{action}/{title}/{detail}/{time}/{count}）
DEFAULT_MESSAGE_TEMPLATES = {
    'create':        '【{page}·新增】操作人 {user} 在{page}新增了「{title}」',
    'update':        '【{page}·修改】操作人 {user} 更新了「{title}」的信息',
    'delete':        '【{page}·删除】操作人 {user} 删除了「{title}」（已移入回收站）',
    'batch_delete':  '【{page}·批量删除】操作人 {user} 批量删除了 {count} 条记录',
    'clear':         '【{page}·清空】操作人 {user} 清空了{page}数据（共 {count} 条）',
    'sync':          '【{page}·同步】操作人 {user} 执行了同步操作：{detail}',
    'restore':       '【{page}·还原】操作人 {user} 还原了「{title}」',
    'status_change': '【{page}·状态变更】操作人 {user} 变更了「{title}」的状态',
    'reminder':      '【{page}·提醒】{detail}',
    'broadcast':     '【{page}·广播】{detail}',
    'security':      '【{page}·安全】操作人 {user} 触发了安全操作',
    'system':        '【{page}·系统配置】操作人 {user} 更新了系统配置',
}


def _get_notify_pages(webhook):
    """获取 Webhook 配置的页面过滤列表，返回 {event_category: [page_keys]} 或空 dict"""
    import json as _json
    raw = getattr(webhook, 'notify_pages', None) or '{}'
    try:
        data = _json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(data, dict):
            return data
    except Exception as e:
        logger.warning("[Webhook] 解析 notify_pages 失败: %s, 原始值: %s", e, raw[:200])
    return {}


def _page_matches(webhook, event_type, page_key):
    """检查该 Webhook 通道是否配置了当前页面的当前事件类型
    V10.1: notify_pages 为空时不过滤（对全部页面放行）
    """
    if not page_key:
        return True
    notify_pages = _get_notify_pages(webhook)
    if not notify_pages:
        return True  # V10.1: 未配置矩阵 = 不过滤，对全部页面放行
    event_category = _get_event_category(event_type)
    pages_for_event = notify_pages.get(event_category, [])
    return page_key in pages_for_event


def _get_monitor_user_ids(webhook):
    """V10.3: 获取 Webhook 监控的用户 ID 列表，空列表=不限制"""
    import json as _json
    raw = getattr(webhook, 'monitor_user_ids', None) or '[]'
    try:
        data = _json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(data, list):
            return [int(uid) for uid in data if uid]
    except Exception as e:
        logger.warning("[Webhook] 解析 monitor_user_ids 失败: %s, 原始值: %s", e, raw[:200])
    return []


def _get_monitor_event_types(webhook):
    """V10.3: 获取 Webhook 监控的事件大类列表，空列表=不限制"""
    import json as _json
    raw = getattr(webhook, 'monitor_event_types', None) or '[]'
    try:
        data = _json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(data, list):
            return data
    except Exception as e:
        logger.warning("[Webhook] 解析 monitor_event_types 失败: %s, 原始值: %s", e, raw[:200])
    return []


def _monitor_matches(webhook, event_type, operator_id):
    """V10.9: 检查该 Webhook 通道的监控范围是否匹配当前操作
    - monitor_user_ids 为空 = 不推送（必须勾选至少一个用户）
    - monitor_event_types 为空 = 不推送（必须勾选至少一个事件类型）
    - 两者都勾选时需同时满足才推送
    """
    # 用户过滤：空列表 = 不推送
    monitor_uids = _get_monitor_user_ids(webhook)
    if not monitor_uids:
        return False
    if operator_id is not None and operator_id not in monitor_uids:
        return False
    # 事件类型过滤：空列表 = 不推送
    monitor_types = _get_monitor_event_types(webhook)
    if not monitor_types:
        return False
    event_cat = _get_event_category(event_type)
    if event_cat not in monitor_types and event_type not in monitor_types:
        return False
    return True


def _get_event_category(event_type):
    """将具体事件类型归入大类 — V10.1: batch_delete/clear/sync/restore 独立为大类"""
    if event_type in ('create', 'record_create'):
        return 'create'
    if event_type in ('update', 'record_update'):
        return 'update'
    if event_type in ('delete', 'record_delete'):
        return 'delete'
    if event_type in ('batch_delete',):
        return 'batch_delete'
    if event_type in ('clear',):
        return 'clear'
    if event_type in ('sync',):
        return 'sync'
    if event_type in ('restore',):
        return 'restore'
    if event_type in ('status_change',):
        return 'status_change'
    if event_type in ('reminder', 'auto_reminder'):
        return 'reminder'
    if event_type in ('broadcast',):
        return 'broadcast'
    if event_type in ('security',):
        return 'security'
    if event_type in ('system',):
        return 'system'
    return event_type


def _render_message(webhook, event_type, page_key, default_title, default_details, user_name):
    """渲染推送消息 — V10.1: 场景化默认提示词 + 页面×事件自定义模板"""
    import json as _json
    import re as _re
    page_name = PAGE_NAMES.get(page_key, page_key or '系统')
    event_cat = _get_event_category(event_type)
    action_label = EVENT_COLUMNS.get(event_cat, EVENT_COLUMNS.get(event_type, '操作'))
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # 从 default_details 中提取 count（如果有）
    count = ''
    count_match = _re.search(r'数量[：:]\s*(\d+)', default_details or '')
    if count_match:
        count = count_match.group(1)
    else:
        count_match2 = _re.search(r'(\d+)\s*(条|个|笔)', default_details or '')
        if count_match2:
            count = count_match2.group(1)

    # 占位符上下文
    fmt_ctx = {
        'user': user_name, 'page': page_name, 'action': action_label,
        'title': default_title or '', 'detail': default_details or '',
        'time': now_str, 'count': count,
    }

    # 先尝试自定义模板（页面×事件级别优先，再事件级别）
    raw_templates = getattr(webhook, 'message_templates', None) or '{}'
    try:
        templates = _json.loads(raw_templates) if isinstance(raw_templates, str) else raw_templates
        if isinstance(templates, dict):
            template_key = f'{event_cat}:{page_key}'
            tpl = templates.get(template_key) or templates.get(event_cat) or templates.get(event_type)
            if tpl and isinstance(tpl, str) and tpl.strip():
                title = tpl.format(**fmt_ctx)
                # V10.2 修复: 自定义模板仅覆盖标题，详情保留原始内容（敏感页面仍脱敏）
                details = default_details or ''
                if page_key in SENSITIVE_PAGES:
                    details = _sanitize_details(page_key, event_type, user_name, details)
                return title, details
    except Exception:
        pass

    # 使用场景化默认模板
    default_tpl = DEFAULT_MESSAGE_TEMPLATES.get(event_cat) or DEFAULT_MESSAGE_TEMPLATES.get(event_type)
    if default_tpl:
        try:
            title = default_tpl.format(**fmt_ctx)
        except Exception:
            title = f'【{page_name}·{action_label}】{default_title}'
    else:
        title = f'【{page_name}·{action_label}】{default_title}'

    details = default_details or ''

    # 敏感页面脱敏
    if page_key in SENSITIVE_PAGES:
        details = _sanitize_details(page_key, event_type, user_name, details)

    return title, details


def _sanitize_details(page_key, event_type, user_name, details):
    """对敏感页面的推送内容进行脱敏 — V10.1: 按事件类型细化描述"""
    event_cat = _get_event_category(event_type)
    action_label = EVENT_COLUMNS.get(event_cat, '操作')
    if page_key == 'ai_assistant':
        return f'{user_name} 在 AI 助手执行了{action_label}操作（内容已脱敏）'
    if page_key == 'ai_config':
        return f'{user_name} {action_label}了 AI 配置（密钥等敏感信息已脱敏）'
    if page_key == 'security':
        return f'{user_name} 触发了安全风控{action_label}操作（详细信息已脱敏）'
    if page_key == 'admin_webhooks':
        return f'{user_name} {action_label}了 Webhook 通道配置（Token等敏感信息已脱敏）'
    if page_key == 'admin_backups':
        return f'{user_name} 执行了备份{action_label}操作（密码等敏感信息已脱敏）'
    return details


def trigger_webhook_event(webhooks, event_type, record_title, details=None, force_channels=False, page_key=None, user_name=None, operator_id=None):
    """异步多线程触发 Webhook 与长连接机器人通知
    
    参数:
        webhooks: WebhookConfig 对象列表
        event_type: 事件类型（create/update/delete/batch_delete/reminder/broadcast/security/system/status_change）
        record_title: 推送标题（不含前缀）
        details: 详情内容
        force_channels: 是否强制推送（跳过开关过滤）
        page_key: 来源页面标识（ledger/banquets/reminders 等），用于页面级过滤
        user_name: 操作人用户名，用于消息内容
        operator_id: 操作发起人ID，用于推送日志记录
    """
    if not webhooks:
        return

    hook_data_list = []
    for w in webhooks:
        if not getattr(w, "is_enabled", True):
            continue
        if not force_channels:
            # 事件开关过滤
            switch_field = EVENT_SWITCH_MAP.get(event_type)
            if switch_field and not getattr(w, switch_field, False):
                continue
            # 页面级过滤
            if not _page_matches(w, event_type, page_key):
                continue
            # V10.3: 用户级监控过滤
            if not _monitor_matches(w, event_type, operator_id):
                continue

        hook_data_list.append({
            "id": w.id,
            "user_id": w.user_id,
            "url": getattr(w, "webhook_url", "") or "",
            "secret": getattr(w, "secret_token", None),
            "connection_type": getattr(w, "connection_type", "webhook_url") or "webhook_url",
            "bot_platform": getattr(w, "bot_platform", "wecom") or "wecom",
            "bot_id": getattr(w, "bot_id", None),
            "bot_secret": getattr(w, "bot_secret", None),
            "_webhook_obj": w  # 保留引用用于模板渲染
        })

    if not hook_data_list:
        return

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # 为每个通道渲染消息（可能各自有不同模板）
    rendered_items = []
    for item in hook_data_list:
        wh = item.pop("_webhook_obj", None)
        title, msg_details = _render_message(wh, event_type, page_key, record_title, details, user_name or '系统')
        rendered_items.append({**item, "title": title, "details": msg_details})

    def _worker():
        for item in rendered_items:
            try:
                conn_type = item.get("connection_type", "webhook_url")
                title = item.get("title", record_title)
                msg_details = item.get("details", details)
                if conn_type == "long_connection":
                    b_id = item.get("bot_id", "")
                    b_sec = item.get("bot_secret", "")
                    c_id = extract_chatid_from_url(item.get("url", "")) or _cached_chatids.get(b_id)
                    if b_id and b_sec:
                        s, c, b = send_wecom_long_connection_message(b_id, b_sec, title, msg_details, now_str, event_type, chatid=c_id)
                        record_webhook_log(item.get("user_id"), item.get("id"), event_type, {"title": title, "bot_id": b_id, "chatid": c_id, "details": msg_details}, c, b, s, operator_id=operator_id)
                    continue

                url = item.get("url", "")
                if not url:
                    continue

                if "dingtalk.com" in url:
                    payload = {
                        "msgtype": "markdown",
                        "markdown": {
                            "title": title,
                            "text": f"### {title}\n\n- **时间**: {now_str}\n- **说明**: {msg_details or '无'}\n\n> 礼金记账系统通知"
                        }
                    }
                elif "feishu.cn" in url or "larksuite.com" in url:
                    payload = {
                        "msg_type": "text",
                        "content": {
                            "text": f"{title}\n时间: {now_str}\n\n{msg_details or '无'}"
                        }
                    }
                elif "qyapi.weixin.qq.com" in url:
                    if msg_details and ("\\n" in msg_details or chr(10) in msg_details):
                        md_cnt = f"### {title}\n> 时间：<font color=\"comment\">{now_str}</font>\n\n{msg_details}"
                    else:
                        md_cnt = f"### {title}\n> 时间：<font color=\"comment\">{now_str}</font>\n> 详情：<font color=\"info\">{msg_details or '无'}</font>"
                    payload = {
                        "msgtype": "markdown",
                        "markdown": {"content": md_cnt}
                    }
                elif "pushplus.plus" in url:
                    token = item.get("secret")
                    if not token and "token=" in url:
                        parsed = urllib.parse.urlparse(url)
                        qs = urllib.parse.parse_qs(parsed.query)
                        token = qs.get("token", [""])[0]
                    html_details = msg_details.replace(chr(10), "<br>").replace("\\n", "<br>") if msg_details else "无"
                    payload = {
                        "token": token,
                        "title": title,
                        "content": f"<h3>{title}</h3><p>时间：{now_str}</p><div>{html_details}</div>",
                        "template": "html"
                    }
                elif "ftqq.com" in url:
                    payload = {
                        "title": title,
                        "desp": f"### {title}\n\n- **时间**: {now_str}\n- **说明**: {msg_details or '无'}"
                    }
                elif "api.day.app" in url:
                    payload = {
                        "title": title,
                        "body": f"{msg_details or '无'}\n时间: {now_str}",
                        "group": "礼金记账"
                    }
                else:
                    payload = {
                        "event": event_type,
                        "title": title,
                        "time": now_str,
                        "details": msg_details,
                        "source": "gift_bookkeeping_app"
                    }

                headers = {}
                if item.get("secret"):
                    headers["Authorization"] = f"Bearer {item.get('secret')}"

                s, c, b = _send_payload(url, payload, headers, timeout=12)
                record_webhook_log(item.get("user_id"), item.get("id"), event_type, payload, c, b, s, operator_id=operator_id)
            except Exception as e:
                logger.error("[Webhook] 推送失败: channel_id=%s, event_type=%s, url=%s, 错误: %s",
                             item.get("id"), event_type, item.get("url", "")[:100], e, exc_info=True)
                # 尝试记录失败日志到数据库
                try:
                    record_webhook_log(item.get("user_id"), item.get("id"), event_type,
                                       {"title": item.get("title", record_title), "error": str(e)},
                                       0, f"推送异常: {e}", False, operator_id=operator_id)
                except Exception:
                    pass

    t = threading.Thread(target=_worker, daemon=True)
    t.start()


_listener_thread = None
_listener_running = False

def _wecom_listener_worker():
    global _listener_running
    while _listener_running:
        # V10.10.16: 跨进程单实例锁，多 Worker 环境下仅一个进程建立 WebSocket 长连接
        if not try_acquire_daemon_lock('wecom_listener'):
            time.sleep(10)
            continue
        try:
            conn, ph = _get_db_conn()
            c = conn.cursor()
            rows = c.execute("SELECT id, bot_id, bot_secret, webhook_url, connection_type FROM webhook_configs WHERE bot_id IS NOT NULL AND bot_id != '' AND is_enabled = 1").fetchall()
            conn.close()

            if not rows:
                time.sleep(10)
                continue

            for wh_id, bot_id, bot_secret, current_url, conn_type in rows:
                WSClient, WSClientOptions, _ = _import_aibot()
                if not bot_id or not bot_secret or not HAS_AIBOT_SDK:
                    continue

                # V10.10.15：raw sqlite3 读取的 bot_secret 是 AES-256-GCM 密文，需解密为明文后才能用于 SDK 认证
                if decrypt_credential:
                    bot_secret = decrypt_credential(bot_secret, fallback_plain=True) or bot_secret

                async def _run_bot_client(b_id, b_sec, w_id, c_type):
                    options = WSClientOptions(
                        bot_id=b_id.strip(),
                        secret=b_sec.strip(),
                        max_reconnect_attempts=3,
                        reconnect_interval=2000,
                        heartbeat_interval=30000
                    )
                    client = WSClient(options)

                    def _on_msg(frame):
                        try:
                            body = frame.get("body", {}) if isinstance(frame, dict) else {}
                            headers = frame.get("headers", {}) if isinstance(frame, dict) else {}
                            cid = body.get("chatid") or (body.get("from", {}) if isinstance(body.get("from"), dict) else {}).get("userid") or headers.get("chatid")
                            if cid:
                                _cached_chatids[b_id] = cid
                                conn_u, ph_u = _get_db_conn()
                                c_u = conn_u.cursor()
                                target_rows = c_u.execute(f"SELECT id, webhook_url, connection_type FROM webhook_configs WHERE (bot_id = {ph_u} OR id = {ph_u} OR bot_platform = 'wecom' OR webhook_url LIKE '%qyapi.weixin.qq.com%') AND is_enabled = 1", (b_id, w_id)).fetchall()
                                for tr_id, tr_url, tr_conn in target_rows:
                                    if tr_conn == "long_connection" or not tr_url or tr_url.startswith("wecom://"):
                                        new_url = f"wecom://bot/{b_id}?chatid={cid}"
                                    else:
                                        sep = "&" if "?" in tr_url else "?"
                                        new_url = tr_url if "chatid=" in tr_url else f"{tr_url}{sep}chatid={cid}"
                                    c_u.execute(f"UPDATE webhook_configs SET webhook_url = {ph_u} WHERE id = {ph_u}", (new_url, tr_id))
                                conn_u.commit()
                                conn_u.close()
                                record_webhook_log(1, w_id, "receive_chatid", {"bot_id": b_id, "chatid": cid}, 200, f"企微群内 @机器人 成功自动捕获群聊会话 chatid [{cid}] 并绑定到通道！", True)
                                print(f"[aibot SDK] 自动捕获群聊会话 chatid [{cid}] 并已持久化")
                        except Exception as e:
                            print(f"[aibot SDK error]: {e}")

                    client.on("message", _on_msg)
                    client.on("event", _on_msg)

                    await client.connect()
                    count = 0
                    while _listener_running and count < 60:
                        await asyncio.sleep(5)
                        count += 1
                    try:
                        await client.disconnect()
                    except Exception:
                        pass

                try:
                    _run_async(_run_bot_client(bot_id, bot_secret, wh_id, conn_type))
                except Exception as e:
                    logger.error("[Webhook] 企微长连接监听异常: wh_id=%s, bot_id=%s, 错误: %s", wh_id, bot_id, e, exc_info=True)

            time.sleep(5)
        except Exception as e:
            logger.error("[Webhook] 监听线程异常: %s", e, exc_info=True)
            time.sleep(10)


def start_wecom_long_connection_listener():
    """启动全局企微智能机器人长连接后台监听守护线程"""
    global _listener_thread, _listener_running
    if _listener_thread and _listener_thread.is_alive():
        return
    _listener_running = True
    _listener_thread = threading.Thread(target=_wecom_listener_worker, daemon=True)
    _listener_thread.start()
