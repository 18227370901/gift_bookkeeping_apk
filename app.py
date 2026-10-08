import os
import re
import sys
import shutil
import random
import string
import time
import uuid
import secrets
from datetime import datetime, timedelta
import webbrowser
from threading import Timer
from flask import Flask, render_template, request, redirect, url_for, flash, session, current_app, abort, make_response, jsonify
from flask_wtf.csrf import CSRFProtect
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from models import (
    db, User, GiftRecord, OperationLog, SystemSetting, RegistrationToken,
    LoginRisk, SecurityRisk, Broadcast, BroadcastRead, WebhookConfig, WebhookLog,
    SharedLedgerLink, BackupConfig, Banquet, AnniversaryReminder,
    ChatSession, ChatMessage, AIQueryLog,
    ScheduledBackupTask, BackupAttachment, PermissionTicket,
    FamilyGroup, FamilyMember
)
from webhook_utils import trigger_webhook_event
from webdav_utils import (
    test_connection as test_webdav_connection,
    upload_backup as upload_backup_webdav,
    list_backups as list_webdav_backups,
    download_backup as restore_webdav_backup
)
from routes_ext import register_routes_ext

# Determine bundle directory for PyInstaller / PyBuild
if getattr(sys, 'frozen', False):
    BUNDLE_DIR = getattr(sys, '_MEIPASS', os.path.abspath(os.path.dirname(__file__)))
else:
    BUNDLE_DIR = os.path.abspath(os.path.dirname(__file__))

template_folder = os.path.join(BUNDLE_DIR, 'templates')
app = Flask(__name__, template_folder=template_folder)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY') or 'gift-bookkeeping-secret-key-2026-prod-secure'

# 服务/容器启动时间戳：用于在服务重启时强制失效所有旧用户会话
APP_START_TIME = time.time()



app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
# 如果通过 HTTPS 部署或设置环境变量 SESSION_COOKIE_SECURE=true，启用 Cookie 仅在 HTTPS 下传输
if os.environ.get('SESSION_COOKIE_SECURE', 'false').lower() in ('true', '1'):
    app.config['SESSION_COOKIE_SECURE'] = True

# 支持 Nginx 自定义 HTTPS 端口反向代理 (感知 X-Forwarded-Proto / Port / Host)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1)

# Handle Database Location (If frozen, write to user-writable directory or local directory)
if getattr(sys, 'frozen', False):
    USER_DATA_DIR = os.path.join(os.path.expanduser('~'), '.gift_bookkeeping')
    os.makedirs(USER_DATA_DIR, exist_ok=True)
    db_path = os.path.join(USER_DATA_DIR, 'gift_bookkeeping.db')
    bundled_db = os.path.join(BUNDLE_DIR, 'gift_bookkeeping.db')
    if not os.path.exists(db_path) and os.path.exists(bundled_db):
        shutil.copy2(bundled_db, db_path)
else:
    # 始终使用持久化目录 data 下的 SQLite 数据库（目录不存在时自动创建）。
    # 保证初次部署即为纯净空库 + 单一管理员（与 Docker 版行为一致），
    # 仓库根目录样例库不再作为运行库使用，避免演示数据污染运行库
    data_dir = os.path.join(BUNDLE_DIR, 'data')
    os.makedirs(data_dir, exist_ok=True)
    db_path = os.path.join(data_dir, 'gift_bookkeeping.db')

# 过滤 DATABASE_URL，当未设置/已注释/为空时无缝降级默认使用 SQLite 数据库
db_url = os.environ.get('DATABASE_URL', '').strip()
if not db_url:
    db_url = f'sqlite:///{db_path}'

app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
if db_url.startswith('sqlite:'):
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        'connect_args': {'timeout': 30}
    }
elif db_url.startswith('postgresql:'):
    # V10.10.20: PG 模式连接与语句级超时保护
    # 背景：PG 引擎此前无任何超时配置，web↔PG 之间出现连接黑洞（网络策略/TCP 半开/
    # 容器网络异常）时，db.create_all() 等启动流程会无限期挂起——表现为容器零日志、
    # 页面 504（gunicorn worker 永远无法完成 app 加载）。
    # 现配置 connect_timeout=10s（建连黑洞 10 秒断开）+ statement_timeout=120s
    #（单条语句最多 2 分钟，建表/迁移足够），超时异常由启动层 try/except 捕获并打印，
    # 保证任何启动故障都有日志可查、且不会无限卡死。
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        'connect_args': {
            'connect_timeout': 10,
            'options': '-c statement_timeout=120000'
        }
    }

db.init_app(app)


login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = '请先登录后再访问系统。'
login_manager.login_message_category = 'warning'



def get_session_timeout_minutes():
    """获取当前设置的用户超时时间（分钟），默认60分钟"""
    try:
        val = SystemSetting.get_val('session_timeout_minutes', '60')
        return max(1, int(val))
    except (ValueError, TypeError):
        return 60

def get_max_login_attempts():
    """获取当前设置的允许最大密码错误次数（触发验证码与锁定），默认5次"""
    try:
        val = SystemSetting.get_val('max_login_attempts', '5')
        return max(1, int(val))
    except (ValueError, TypeError):
        return 5

def get_login_lockout_seconds():
    """获取当前设置的连续密码错误锁定等待时长（秒），默认60秒"""
    try:
        val = SystemSetting.get_val('login_lockout_seconds', '60')
        return max(0, int(val))
    except (ValueError, TypeError):
        return 60

def get_max_security_attempts():
    """获取当前设置的找回密码密保问题最大错误尝试次数（触发锁定），默认3次"""
    try:
        val = SystemSetting.get_val('max_security_attempts', '3')
        return max(1, int(val))
    except (ValueError, TypeError):
        return 3

def get_forgot_security_risk_status(username):
    """获取指定用户名找回密码密保验证的风控状态：(cur_fail_count, is_locked, lock_wait_seconds)"""
    if not username:
        return 0, False, 0
    now = datetime.now().timestamp()
    cur_fail_count = FORGOT_SECURITY_FAIL_COUNTS.get(username, 0)
    target_lock_until = FORGOT_SECURITY_LOCK_UNTILS.get(username, 0)

    is_locked = False
    lock_wait = 0
    if now < target_lock_until:
        is_locked = True
        lock_wait = int(target_lock_until - now)

    return cur_fail_count, is_locked, lock_wait

def check_password_complexity(password):
    """检查密码复杂度：至少6位，且同时包含字母和数字"""
    if not password or len(password) < 6:
        return False, '密码长度至少为6位！'
    if not (re.search(r'[a-zA-Z]', password) and re.search(r'\d', password)):
        return False, '密码必须同时包含字母和数字！'
    return True, ''


@app.before_request
def check_session_timeout():
    public_endpoints = {
        'static', 'logout', 'login', 'register', 'forgot_password',
        'shared_ledger_view', 'pwa_manifest', 'pwa_sw', 'captcha'
    }
    if request.endpoint in public_endpoints:
        return

    if current_user.is_authenticated:
        now = datetime.now().timestamp()

        # 1. 服务/容器重启强制所有用户重新登录机制
        login_time = session.get('login_time')
        if not login_time or login_time < APP_START_TIME:
            user_to_logout = current_user
            if hasattr(user_to_logout, 'session_token'):
                user_to_logout.session_token = None
                try:
                    db.session.commit()
                except Exception:
                    pass
            logout_user()
            session.clear()
            if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'code': 401, 'message': '服务已重启，请重新登录！', 'redirect': url_for('login')}), 401
            flash('服务已重启，请重新登录！', 'warning')
            return redirect(url_for('login'))

        # 2. 超时时间检查
        last_activity = session.get('last_activity')
        timeout_minutes = get_session_timeout_minutes()

        if last_activity:
            elapsed = now - last_activity
            if elapsed > timeout_minutes * 60:
                user_to_logout = current_user
                if hasattr(user_to_logout, 'session_token'):
                    user_to_logout.session_token = None
                    try:
                        db.session.commit()
                    except Exception:
                        pass
                logout_user()
                session.clear()
                if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    return jsonify({'code': 401, 'message': f'由于您超过 {timeout_minutes} 分钟未操作，登录已超时，请重新登录！', 'redirect': url_for('login')}), 401
                flash(f'由于您超过 {timeout_minutes} 分钟未操作，登录已超时，请重新登录！', 'warning')
                return redirect(url_for('login'))

        session['last_activity'] = now

@app.after_request
def add_header(response):
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

@app.before_request
def csrf_protect():
    if app.config.get('TESTING') and not app.config.get('WTF_CSRF_ENABLED', True):
        return
    # 对企业微信回调与外部 Webhook 接口免除 CSRF 检查
    if (request.path.startswith('/api/wecom') or 
        request.path.startswith('/webhook') or 
        request.path.startswith('/wecom') or 
        request.path.startswith('/callback') or 
        request.args.get('echostr') or
        request.args.get('msg_signature') or
        (request.path == '/' and (request.args.get('echostr') or request.args.get('msg_signature') or (request.data and (b'<xml' in request.data.lower() or b'chatid' in request.data.lower())) or (request.is_json and any(k in (request.get_json(silent=True) or {}) for k in ['chatid', 'ChatId', 'from', 'aibot_id', 'bot_id', 'encrypt', 'msgtype']))))):
        return
    if 'csrf_token' not in session:
        session['csrf_token'] = secrets.token_hex(32)
    if request.method in ['POST', 'PUT', 'PATCH', 'DELETE']:
        token = session.get('csrf_token')
        request_token = (
            request.form.get('csrf_token') or
            request.headers.get('X-CSRF-Token') or
            request.headers.get('X-CSRFToken') or
            (request.is_json and isinstance(request.get_json(silent=True), dict) and request.get_json(silent=True).get('csrf_token'))
        )
        if not token or not request_token or token != request_token:
            abort(403, description="CSRF Token 验证失败，页面凭证已失效或请求非法，请重试！")

@app.before_request
def handle_wecom_root_callback():
    """当外髨企业澮信回调打到系统根阵时，自动分发给 wecom_http_callback 处理"""
    if request.path == '/':
        is_wecom = (
            request.args.get('echostr') or
            request.args.get('msg_signature') or
            (request.data and (b'<xml' in request.data.lower() or b'chatid' in request.data.lower())) or
            (request.is_json and any(k in (request.get_json(silent=True) or {}) for k in ['chatid', 'ChatId', 'from', 'aibot_id', 'bot_id', 'encrypt', 'msgtype']))
        )
        if is_wecom:
            view_func = app.view_functions.get('wecom_http_callback')
            if view_func:
                return view_func()

@app.errorhandler(403)
def handle_403(e):
    msg = str(e.description) if hasattr(e, 'description') and e.description else 'CSRF Token 验证失败或请求非法！'
    if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'code': 403, 'message': msg}), 403
    flash(msg, 'danger')
    return redirect(url_for('login'))

class CSRFTokenStr(str):
    def __call__(self):
        return str(self)

@app.context_processor
def inject_globals():
    if 'csrf_token' not in session:
        session['csrf_token'] = secrets.token_hex(32)
    token_str = CSRFTokenStr(session['csrf_token'])

    active_broadcasts = []
    if current_user and current_user.is_authenticated:
        try:
            read_bc_ids = [
                row[0] for row in db.session.query(BroadcastRead.broadcast_id).filter_by(user_id=current_user.id).all()
            ]
            all_active = Broadcast.query.filter_by(is_active=True).order_by(Broadcast.created_at.desc()).all()
            for bc in all_active:
                if bc.id in read_bc_ids:
                    continue
                if bc.scope == 'admin' and not current_user.is_admin:
                    continue
                active_broadcasts.append(bc)
        except Exception:
            pass

    return dict(
        csrf_token=token_str,
        active_broadcasts=active_broadcasts,
        can_user_view_record=can_user_view_record,
        can_user_edit_record=can_user_edit_record,
        can_user_delete_record=can_user_delete_record,
        can_user_view_banquet=can_user_view_banquet,
        can_user_edit_banquet=can_user_edit_banquet,
        can_user_delete_banquet=can_user_delete_banquet,
        can_user_view_reminder=can_user_view_reminder,
        can_user_edit_reminder=can_user_edit_reminder,
        can_user_delete_reminder=can_user_delete_reminder,
        can_user_view_entity=can_user_view_entity,
        can_user_edit_entity=can_user_edit_entity,
        can_user_delete_entity=can_user_delete_entity,
        num2cn=num2cn
    )

def purge_expired_logs(days=90):
    """自动批量清理超过保存时效（默认3个月/90天）的操作日志"""
    try:
        if not current_app:
            return
        cutoff = datetime.now() - timedelta(days=days)
        deleted_count = OperationLog.query.filter(OperationLog.created_at < cutoff).delete(synchronize_session=False)
        if deleted_count > 0:
            db.session.commit()
    except Exception as e:
        try:
            db.session.rollback()
        except Exception:
            pass
        print(f"[Purge Logs Error] 自动清理失效日志异常: {e}")

import json as _json_module

# V10: 审计日志模块过滤映射——按 action 关键词归组
AUDIT_MODULE_MAP = {
    'ledger': ['记录', '极简记账', '导入', '导出', '查询记录'],
    'banquets': ['大账本', '宴席', '分享链接'],
    'reconciliation': ['对账'],
    'reminders': ['纪念日', '提醒'],
    'recycle_bin': ['回收站'],
    'admin_users': ['用户', '权限', '账号', '密码', '密保'],
    'webhooks': ['Webhook', 'webhook', '推送日志'],
    'backups': ['WebDAV', '备份', '恢复', '定时任务', '附件'],
    'permission_tickets': ['工单', '权限申请'],
    'broadcasts': ['广播'],
    'ai_assistant': ['AI', '会话', '配置更新', '授权管理'],
    'auth': ['登录', '注册', '退出', '找回', '风控', '锁定'],
    'system': ['系统配置', '注册策略', '邀请', '日志'],
}

def _get_audit_module(action_text):
    """根据 action 文本判断所属模块，返回模块标识或 None"""
    for module_key, keywords in AUDIT_MODULE_MAP.items():
        for kw in keywords:
            if kw in action_text:
                return module_key
    return 'system'  # 未匹配的默认归入 system

def _should_log_module(action_text):
    """检查当前模块是否在管理员配置的审计日志白名单中"""
    try:
        config_val = SystemSetting.get_val('audit_log_modules', None)
        if not config_val:
            return True  # 未配置 = 默认全部记录
        modules = _json_module.loads(config_val)
        if not modules or 'all' in modules:
            return True  # 空列表或包含 'all' = 全部记录
        module_key = _get_audit_module(action_text)
        return module_key in modules
    except Exception:
        return True  # 出错时默认记录

def log_action(action, detail="", user=None):
    # 每次写入日志时顺便触发清理超过3个月的超期日志
    purge_expired_logs(days=90)
    try:
        # 兼容 (user_id, action, detail) 与 (action, detail, user) 两种调用签名
        if isinstance(action, int) and isinstance(detail, str):
            u_id = action
            actual_action = detail
            actual_detail = str(user) if user is not None else ""
            actual_user = User.query.get(u_id) if u_id else None
            u_name = actual_user.username if actual_user else f"用户#{u_id}"
        else:
            actual_action = action
            actual_detail = detail
            actual_user = user
            if actual_user:
                u_id = getattr(actual_user, "id", None)
                u_name = getattr(actual_user, "username", str(actual_user))
            elif current_user and current_user.is_authenticated:
                u_id = current_user.id
                u_name = current_user.username
            else:
                u_id = None
                u_name = "未登录/系统"

        # V10: 模块级过滤——若该模块未在白名单中则跳过写入
        if not _should_log_module(actual_action):
            return

        ip_addr = request.remote_addr if request else ""
        if request and request.headers.get("X-Forwarded-For"):
            ip_addr = request.headers.get("X-Forwarded-For").split(",")[0].strip()

        log_entry = OperationLog(
            user_id=u_id,
            username=u_name,
            action=actual_action,
            detail=actual_detail,
            ip_address=ip_addr
        )
        db.session.add(log_entry)
        db.session.commit()
    except Exception as e:
        # 日志记录失败切勿回滚主业务事务
        print(f"[Log Error] 写入日志失败: {e}")

@login_manager.user_loader
def load_user(user_id):
    user = User.query.get(int(user_id))
    if user:
        if not user.is_active:
            return None
        if not user.session_token:
            return None
        current_token = session.get('session_token')
        if current_token and current_token != user.session_token:
            return None
        if not current_token:
            session['session_token'] = user.session_token
    return user

def cn2num(s):
    if not s:
        return 0.0
    s = str(s).strip()
    s = s.replace('元', '').replace('圆', '').replace('正', '').replace('整', '').strip()
    try:
        return float(s)
    except ValueError:
        pass
    
    num_map = {'零':0, '壹':1, '贰':2, '叁':3, '肆':4, '伍':5, '陆':6, '柒':7, '捌':8, '玖':9,
               '一':1, '二':2, '三':3, '四':4, '五':5, '六':6, '七':7, '八':8, '九':9, '两':2}
    unit_map = {'拾':10, '十':10, '佰':100, '百':100, '仟':1000, '千':1000, '万':10000, '亿':100000000}
    
    total = 0
    section = 0
    number = 0
    has_digit = False
    
    for char in s:
        if char in num_map:
            number = num_map[char]
            has_digit = True
        elif char in unit_map:
            unit = unit_map[char]
            has_digit = True
            if unit == 10000 or unit == 100000000:
                section = (section + (number if number != 0 or not section else 0)) * unit
                total += section
                section = 0
                number = 0
            else:
                if number == 0:
                    number = 1
                section += number * unit
                number = 0
    total += section + number
    return float(total) if has_digit else 0.0


def get_accessible_records_query(user, menu_key='ledger'):
    """获取指定用户有权查看的记录查询对象（默认排除回收站记录，支持按菜单独立权限校验）"""
    if not user or not user.is_authenticated:
        return GiftRecord.query.filter(db.false())
    base_q = GiftRecord.query.filter(GiftRecord.deleted_at.is_(None))
    if getattr(user, 'is_admin', False):
        return base_q
    if hasattr(user, 'can_view_others_for') and user.can_view_others_for(menu_key):
        return base_q
    if getattr(user, 'can_view_others', False) and menu_key == 'ledger':
        return base_q
    return base_q.filter(GiftRecord.user_id == user.id)


def is_entity_owner_admin(entity):
    """判断实体（礼金记录、专属宴席、纪念日等）创建者是否为管理员"""
    if not entity:
        return False
    if getattr(entity, 'owner', None):
        return bool(getattr(entity.owner, 'is_admin', False))
    if getattr(entity, 'user_id', None):
        owner = db.session.get(User, entity.user_id)
        return bool(getattr(owner, 'is_admin', False)) if owner else False
    return False

is_record_owner_admin = is_entity_owner_admin

def _infer_entity_menu(entity, menu_key=None):
    if menu_key:
        return menu_key
    if isinstance(entity, Banquet):
        return 'banquets'
    if isinstance(entity, AnniversaryReminder):
        return 'reminders'
    return 'ledger'

def can_user_view_entity(user, entity, menu_key=None):
    """判断用户是否有权查看指定实体（账本/宴席/纪念日/回收站）"""
    if not user or not user.is_authenticated:
        return False
    if getattr(user, 'is_admin', False):
        return True
    mk = _infer_entity_menu(entity, menu_key)
    # 实体属于用户自身
    if getattr(entity, 'user_id', None) == user.id:
        return True
    # 他人实体：需要级别 >= 1 (仅查看他人数据及以上)
    if hasattr(user, 'can_view_others_for'):
        return user.can_view_others_for(mk)
    return bool(getattr(user, 'can_view_others', False))

def can_user_edit_entity(user, entity, menu_key=None):
    """判断用户是否有权修改指定实体（级别1：自身可改、他人只读）"""
    if not user or not user.is_authenticated:
        return False
    if getattr(user, 'is_admin', False):
        return True
    if is_entity_owner_admin(entity):
        return False
    mk = _infer_entity_menu(entity, menu_key)
    perm = user.get_menu_perm(mk) if hasattr(user, 'get_menu_perm') else 0
    # 实体属于自身：级别 0(自管)、1(查他人)、2(查+改)、3(查+改+删) 均可修改
    if getattr(entity, 'user_id', None) == user.id:
        return True
    # 他人实体：需要级别 >= 2 (查看+修改他人数据)
    return perm >= 2

def can_user_delete_entity(user, entity, menu_key=None):
    """判断用户是否有权删除指定实体（级别1自身可删、他人不可删；级别2严禁任何删除）"""
    if not user or not user.is_authenticated:
        return False
    if getattr(user, 'is_admin', False):
        return True
    if is_entity_owner_admin(entity):
        return False
    mk = _infer_entity_menu(entity, menu_key)
    perm = user.get_menu_perm(mk) if hasattr(user, 'get_menu_perm') else 0
    # 实体属于自身：级别 0(自管)、1(查他人)、3(查+改+删) 可删除；级别 2 不可删
    if getattr(entity, 'user_id', None) == user.id:
        return perm != 2
    # 他人实体：需要级别 >= 3 (查看+修改+删除他人数据)
    return perm >= 3

def can_user_view_record(user, record):
    return can_user_view_entity(user, record, 'ledger')

def can_user_edit_record(user, record):
    return can_user_edit_entity(user, record, 'ledger')

def can_user_delete_record(user, record):
    return can_user_delete_entity(user, record, 'ledger')

def can_user_view_banquet(user, banquet):
    return can_user_view_entity(user, banquet, 'banquets')

def can_user_edit_banquet(user, banquet):
    return can_user_edit_entity(user, banquet, 'banquets')

def can_user_delete_banquet(user, banquet):
    return can_user_delete_entity(user, banquet, 'banquets')

def can_user_view_reminder(user, reminder):
    return can_user_view_entity(user, reminder, 'reminders')

def can_user_edit_reminder(user, reminder):
    return can_user_edit_entity(user, reminder, 'reminders')

def can_user_delete_reminder(user, reminder):
    return can_user_delete_entity(user, reminder, 'reminders')

def get_accessible_banquets_query(user):
    """获取指定用户有权查看的专属宴席查询对象"""
    if not user or not user.is_authenticated:
        return Banquet.query.filter(db.false())
    base_q = Banquet.query.filter(Banquet.deleted_at.is_(None))
    if getattr(user, 'is_admin', False):
        return base_q
    if hasattr(user, 'can_view_others_for') and user.can_view_others_for('banquets'):
        return base_q
    return base_q.filter(Banquet.user_id == user.id)

def get_accessible_reminders_query(user):
    """获取指定用户有权查看的纪念日备忘查询对象"""
    if not user or not user.is_authenticated:
        return AnniversaryReminder.query.filter(db.false())
    base_q = AnniversaryReminder.query.filter(AnniversaryReminder.deleted_at.is_(None))
    if getattr(user, 'is_admin', False):
        return base_q
    if hasattr(user, 'can_view_others_for') and user.can_view_others_for('reminders'):
        return base_q
    return base_q.filter(AnniversaryReminder.user_id == user.id)

def num2cn(num):
    if num is None:
        return '零元整'
    try:
        num_float = float(num)
    except (ValueError, TypeError):
        return '零元整'
    
    if num_float == 0:
        return '零元整'
        
    digits = ['零', '壹', '贰', '叁', '肆', '伍', '陆', '柒', '捌', '玖']
    units = ['', '拾', '佰', '仟']
    big_units = ['', '万', '亿']
    
    integer_part = int(round(num_float))
    str_val = str(integer_part)
    
    if len(str_val) > 12:
        return f'{num_float:.2f}元'
        
    groups = []
    while str_val:
        groups.insert(0, str_val[-4:])
        str_val = str_val[:-4]
        
    group_count = len(groups)
    result = ''
    
    for i, group in enumerate(groups):
        g_len = len(group)
        g_res = ''
        g_zero = False
        for j, char in enumerate(group):
            d = int(char)
            unit_idx = g_len - 1 - j
            if d != 0:
                if g_zero:
                    g_res += '零'
                    g_zero = False
                g_res += digits[d] + units[unit_idx]
            else:
                g_zero = True
        
        big_unit_idx = group_count - 1 - i
        if g_res:
            result += g_res + big_units[big_unit_idx]
        elif big_unit_idx > 0 and result and not result.endswith('零'):
            result += '零'

    return result + '元整'

@app.template_filter('num2cn')
def num2cn_filter(num):
    return num2cn(num)

# V10.10.16: 数据库结构版本标记，用于幂等快速跳过迁移流程
# V10.10.17: 支持 PostgreSQL（用 schema_version 表替代 PRAGMA user_version）
_SCHEMA_VERSION = 1018

def _is_db_schema_current():
    """检查数据库结构版本是否已标记为当前版本（SQLite 用 PRAGMA user_version，PG 用 schema_version 表）"""
    try:
        if db_url.startswith('sqlite:'):
            _p = db_url.replace('sqlite:///', '')
            if _p and os.path.exists(_p):
                import sqlite3 as _vc
                _cn = _vc.connect(_p, timeout=5)
                _ver = _cn.execute("PRAGMA user_version").fetchone()[0]
                _cn.close()
                return _ver == _SCHEMA_VERSION
        elif db_url.startswith('postgresql://'):
            with db.engine.connect() as _conn:
                _row = _conn.execute(db.text("SELECT version FROM schema_version WHERE id = 1")).fetchone()
                return _row is not None and _row[0] == _SCHEMA_VERSION
    except Exception:
        pass
    return False

def _mark_db_schema_current():
    """迁移完成后标记数据库结构版本（SQLite 用 PRAGMA user_version，PG 用 schema_version 表）"""
    try:
        if db_url.startswith('sqlite:'):
            _p = db_url.replace('sqlite:///', '')
            if _p:
                import sqlite3 as _mc
                _cn = _mc.connect(_p)
                _cn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
                _cn.commit()
                _cn.close()
        elif db_url.startswith('postgresql://'):
            with db.engine.connect() as _conn:
                _conn.execute(db.text("CREATE TABLE IF NOT EXISTS schema_version (id INT PRIMARY KEY DEFAULT 1, version INT NOT NULL, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"))
                _conn.execute(db.text("INSERT INTO schema_version (id, version) VALUES (1, :v) ON CONFLICT (id) DO UPDATE SET version = :v, updated_at = CURRENT_TIMESTAMP"), {'v': _SCHEMA_VERSION})
                _conn.commit()
    except Exception:
        pass

def _do_startup_sync():
    """每次容器/服务重启都需执行的轻量同步：风控重置 + 管理员账号同步"""
    try:
        LOGIN_FAIL_COUNTS.clear()
        LOGIN_LOCK_UNTILS.clear()
        FORGOT_SECURITY_FAIL_COUNTS.clear()
        FORGOT_SECURITY_LOCK_UNTILS.clear()
    except Exception:
        pass

    try:
        reg_setting = SystemSetting.query.filter_by(key='registration_mode').first()
        if not reg_setting:
            SystemSetting.set_val('registration_mode', 'invite_only')
    except Exception:
        pass
    admin = User.query.filter_by(is_admin=True).first()
    initial_user = os.environ.get('ADMIN_USER', 'admin').strip()
    initial_pass = os.environ.get('ADMIN_PASS', 'admin123').strip()
    if not admin:
        admin = User.query.filter_by(username=initial_user).first()

    if not admin:
        admin = User(
            username=initial_user,
            security_question='系统默认安全问题：您的默认备用验证码是？',
            is_admin=True,
            is_active=True
        )
        admin.set_password(initial_pass)
        admin.set_security_answer('admin')
        db.session.add(admin)
        db.session.commit()
        print(f"[Init] 已创建初始管理员账号: {initial_user}")
    else:
        if admin.username != initial_user:
            existing = User.query.filter_by(username=initial_user).first()
            if existing and existing.id != admin.id:
                print(f"[Init] 警告: 用户名 '{initial_user}' 已被其他用户(id={existing.id})占用，"
                      f"保留当前管理员用户名 '{admin.username}'")
            else:
                admin.username = initial_user
        admin.set_password(initial_pass)
        admin.is_admin = True
        admin.is_active = True
        db.session.commit()
        print(f"[Init] 已同步更新管理员账号 [{admin.username}] 密码为最新配置并确保处于激活状态")

def init_database():
    with app.app_context():
        # V10.10.16: 幂等快速跳过——已标记最新版本的库跳过完整迁移流程
        if _is_db_schema_current():
            print(f"[Init] 数据库结构已最新 (v{_SCHEMA_VERSION})，跳过迁移流程")
            _do_startup_sync()
            return

        # V5: 在 db.create_all() 之前，先用原生 sqlite3 修复可能存在的 orphan index 问题
        # 这个问题在恢复旧版备份后尤为常见：malformed database schema (sqlite_autoindex_xxx) - orphan index
        # V10.10.17: PG 模式跳过 SQLite 专有的 orphan index 修复
        if db_url.startswith('sqlite:'):
            try:
                import sqlite3 as _sqlite3_raw
                _raw_db_path = app.config.get('SQLALCHEMY_DATABASE_URI', '').replace('sqlite:///', '')
                if _raw_db_path and os.path.exists(_raw_db_path):
                    _fix_conn = _sqlite3_raw.connect(_raw_db_path)
                    _fix_conn.execute("PRAGMA writable_schema=1")
                    _orphan_indexes = _fix_conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'sqlite_autoindex_%' AND tbl_name NOT IN (SELECT name FROM sqlite_master WHERE type='table')"
                    ).fetchall()
                    for (_idx_name,) in _orphan_indexes:
                        try:
                            _fix_conn.execute(f"DROP INDEX IF EXISTS \"{_idx_name}\"")
                            print(f"[V5-Fix] 已删除孤儿索引: {_idx_name}")
                        except Exception:
                            pass
                    _fix_conn.execute("PRAGMA writable_schema=0")
                    _fix_conn.commit()
                    _fix_conn.close()
            except Exception as _fix_err:
                print(f"[V5-Fix] orphan index 修复跳过: {_fix_err}")

        # V10.10.20: 启动阶段标记——建表与迁移是 PG 模式下的主要耗时/卡点，
        # 输出阶段标记保证任何卡点在容器日志中可见（此前卡在 create_all 时容器零日志）
        print(f"[Init] 数据库模式: {'PostgreSQL' if db_url.startswith('postgresql:') else 'SQLite'}，正在创建/校验表结构...")
        db.create_all()
        print("[Init] 表结构创建/校验完成，正在执行历史迁移 SQL...")
        # 自动迁移检查缺失字段
        migration_sqls = [
            "ALTER TABLE users ADD COLUMN role VARCHAR(20) DEFAULT 'user'",
            "ALTER TABLE users ADD COLUMN encrypted_password VARCHAR(512)",
            "ALTER TABLE users ADD COLUMN encrypted_security_answer VARCHAR(512)",
            "ALTER TABLE users ADD COLUMN security_question_1 VARCHAR(200)",
            "ALTER TABLE users ADD COLUMN security_answer_hash_1 VARCHAR(256)",
            "ALTER TABLE users ADD COLUMN encrypted_security_answer_1 VARCHAR(512)",
            "ALTER TABLE users ADD COLUMN security_question_2 VARCHAR(200)",
            "ALTER TABLE users ADD COLUMN security_answer_hash_2 VARCHAR(256)",
            "ALTER TABLE users ADD COLUMN encrypted_security_answer_2 VARCHAR(512)",
            "ALTER TABLE users ADD COLUMN is_active BOOLEAN DEFAULT TRUE",
            "ALTER TABLE users ADD COLUMN session_token VARCHAR(64)",
            "ALTER TABLE users ADD COLUMN can_view_others BOOLEAN DEFAULT 0",
            "ALTER TABLE users ADD COLUMN can_edit_others BOOLEAN DEFAULT 0",
            "ALTER TABLE users ADD COLUMN can_delete_others BOOLEAN DEFAULT 0",
            "ALTER TABLE gift_records ADD COLUMN deleted_at DATETIME",
            "ALTER TABLE gift_records ADD COLUMN record_type VARCHAR(20) DEFAULT 'receive'",
            "ALTER TABLE gift_records ADD COLUMN banquet_id INTEGER",
            "ALTER TABLE shared_ledger_links ADD COLUMN banquet_id INTEGER",
            "ALTER TABLE shared_ledger_links ADD COLUMN access_password VARCHAR(64)",
            "ALTER TABLE shared_ledger_links ADD COLUMN hide_notes BOOLEAN DEFAULT 0",
            "ALTER TABLE shared_ledger_links ADD COLUMN hide_amount BOOLEAN DEFAULT 0",
            "ALTER TABLE broadcasts ADD COLUMN scope VARCHAR(20) DEFAULT 'all'",
            "ALTER TABLE webhook_configs ADD COLUMN notify_on_reminder BOOLEAN DEFAULT 1",
            "ALTER TABLE webhook_configs ADD COLUMN notify_on_broadcast BOOLEAN DEFAULT 1",
            "ALTER TABLE webhook_configs ADD COLUMN connection_type VARCHAR(30) DEFAULT 'webhook_url'",
            "ALTER TABLE webhook_configs ADD COLUMN bot_platform VARCHAR(50) DEFAULT 'wecom'",
            "ALTER TABLE webhook_configs ADD COLUMN bot_id VARCHAR(100)",
            "ALTER TABLE webhook_configs ADD COLUMN bot_secret VARCHAR(256)",
            "ALTER TABLE registration_tokens ADD COLUMN max_uses INTEGER DEFAULT 1",
            "ALTER TABLE registration_tokens ADD COLUMN use_count INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN allowed_menus VARCHAR(256) DEFAULT 'ledger'",
            "ALTER TABLE users ADD COLUMN menu_permissions TEXT DEFAULT '{}'",
            "ALTER TABLE anniversary_reminders ADD COLUMN deleted_at DATETIME",
            "ALTER TABLE anniversary_reminders ADD COLUMN last_notified_target VARCHAR(32)",
            "ALTER TABLE banquets ADD COLUMN creator_type VARCHAR(32) DEFAULT 'manual'",
            "ALTER TABLE banquets ADD COLUMN creator_id INTEGER",
            "ALTER TABLE banquets ADD COLUMN source_username VARCHAR(64)",
            "UPDATE users SET allowed_menus = 'ledger,banquets,reconciliation,reminders,recycle_bin' WHERE is_admin = 1",
            "UPDATE users SET allowed_menus = 'ledger' WHERE allowed_menus IS NULL",
            "UPDATE users SET security_question_1 = security_question, security_answer_hash_1 = security_answer_hash WHERE security_question_1 IS NULL AND security_question IS NOT NULL",
            "UPDATE gift_records SET record_type = 'receive' WHERE record_type IS NULL",
            # --- AI 助手模块迁移 ---
            "ALTER TABLE users ADD COLUMN ai_api_key VARCHAR(512)",
            "ALTER TABLE users ADD COLUMN ai_base_url VARCHAR(255) DEFAULT ''",
            "ALTER TABLE users ADD COLUMN ai_model VARCHAR(100) DEFAULT ''",
            "ALTER TABLE users ADD COLUMN ai_configs TEXT DEFAULT '[]'",
            "ALTER TABLE users ADD COLUMN ai_authorized BOOLEAN DEFAULT 0",
            # --- Webhook 推送矩阵新字段 ---
            "ALTER TABLE webhook_configs ADD COLUMN notify_on_update BOOLEAN DEFAULT 0",
            "ALTER TABLE webhook_configs ADD COLUMN notify_on_security BOOLEAN DEFAULT 0",
            "ALTER TABLE webhook_configs ADD COLUMN notify_on_system BOOLEAN DEFAULT 1",
            "ALTER TABLE webhook_configs ADD COLUMN notify_on_status_change BOOLEAN DEFAULT 0",
            "ALTER TABLE webhook_configs ADD COLUMN notify_pages TEXT DEFAULT '{}'",
            "ALTER TABLE webhook_configs ADD COLUMN message_templates TEXT DEFAULT '{}'",
            # --- 备份加密与授权字段 ---
            "ALTER TABLE users ADD COLUMN backup_authorized BOOLEAN DEFAULT 0",
            "ALTER TABLE users ADD COLUMN scheduled_task_authorized BOOLEAN DEFAULT 0",
            "ALTER TABLE backup_configs ADD COLUMN backup_encrypt_password VARCHAR(512)",
            # --- 新模型建表 ---
            """CREATE TABLE IF NOT EXISTS scheduled_backup_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(100) NOT NULL DEFAULT '定时备份',
                cron_expr VARCHAR(50) NOT NULL DEFAULT '0 2 * * *',
                is_enabled BOOLEAN DEFAULT 0,
                encrypt_enabled BOOLEAN DEFAULT 0,
                last_run_time DATETIME,
                last_run_status VARCHAR(255),
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )""",
            """CREATE TABLE IF NOT EXISTS backup_attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename VARCHAR(255) NOT NULL,
                file_size INTEGER DEFAULT 0,
                file_type VARCHAR(50) DEFAULT 'application/octet-stream',
                is_encrypted BOOLEAN DEFAULT 0,
                uploaded_by INTEGER REFERENCES users(id),
                storage_path VARCHAR(500),
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )""",
            """CREATE TABLE IF NOT EXISTS permission_tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                requested_menus VARCHAR(256) NOT NULL,
                reason TEXT,
                status VARCHAR(20) DEFAULT 'pending',
                reviewed_by INTEGER REFERENCES users(id),
                reviewed_at DATETIME,
                review_comment TEXT,
                granted_menus VARCHAR(256),
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )"""
        ]
        # --- V2 修复：新增字段迁移 ---
        migration_sqls.extend([
            "ALTER TABLE webhook_logs ADD COLUMN operator_id INTEGER REFERENCES users(id)",
            "ALTER TABLE backup_configs ADD COLUMN backup_subdir VARCHAR(100) DEFAULT 'gift_backups'",
            "ALTER TABLE scheduled_backup_tasks ADD COLUMN task_type VARCHAR(20) DEFAULT 'db_backup'",
            "ALTER TABLE scheduled_backup_tasks ADD COLUMN target_files TEXT",
            "ALTER TABLE scheduled_backup_tasks ADD COLUMN custom_script TEXT",
        ])
        # --- V3 修复：新增字段与新表迁移 ---
        migration_sqls.extend([
            # BackupConfig 支持多用户隔离
            "ALTER TABLE backup_configs ADD COLUMN user_id INTEGER",
            "ALTER TABLE backup_configs ADD COLUMN allow_view_others_tasks BOOLEAN DEFAULT 0",
            # ScheduledBackupTask 新增创建者
            "ALTER TABLE scheduled_backup_tasks ADD COLUMN created_by INTEGER REFERENCES users(id)",
            # 定时任务执行历史日志表
            """CREATE TABLE IF NOT EXISTS scheduled_task_execution_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL REFERENCES scheduled_backup_tasks(id) ON DELETE CASCADE,
                start_time DATETIME,
                end_time DATETIME,
                status VARCHAR(20) DEFAULT 'running',
                output_log TEXT,
                executed_by VARCHAR(100),
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )""",
        ])
        # --- V9 修复：WebDAV 配置别称与引用标记 ---
        migration_sqls.extend([
            "ALTER TABLE backup_configs ADD COLUMN config_alias VARCHAR(100)",
            "ALTER TABLE backup_configs ADD COLUMN adopted_from_admin BOOLEAN DEFAULT 0",
        ])
        # --- V10.2 新增：定时任务操作权限细化字段 ---
        migration_sqls.extend([
            "ALTER TABLE backup_configs ADD COLUMN allow_edit_others_tasks BOOLEAN DEFAULT 0",
            "ALTER TABLE backup_configs ADD COLUMN allow_delete_others_tasks BOOLEAN DEFAULT 0",
        ])
        # --- V10.3 新增：Webhook 用户级监控过滤字段 ---
        migration_sqls.extend([
            "ALTER TABLE webhook_configs ADD COLUMN monitor_user_ids TEXT DEFAULT '[]'",
            "ALTER TABLE webhook_configs ADD COLUMN monitor_event_types TEXT DEFAULT '[]'",
        ])

        # --- V10.11 新增：家庭多成员协作记账 + 数据分析看板 ---
        migration_sqls.extend([
            # 家庭组表
            """CREATE TABLE IF NOT EXISTS family_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(100) NOT NULL,
                owner_id INTEGER NOT NULL REFERENCES users(id),
                description VARCHAR(256),
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )""",
            # 家庭成员关联表
            """CREATE TABLE IF NOT EXISTS family_members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER NOT NULL REFERENCES family_groups(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                role VARCHAR(20) DEFAULT 'member',
                nickname VARCHAR(64),
                joined_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )""",
        ])

        # V10.9: 数据迁移 — 将现有 Webhook 的空监控范围预填为全选
        # 逻辑变更：空列表从"不限制=全部放行"改为"不推送"，需保证现有通道不受影响
        try:
            import json as _migrate_json
            all_user_ids = [u.id for u in User.query.with_entities(User.id).all()]
            from webhook_utils import EVENT_COLUMNS as _migrate_events
            all_event_types = list(_migrate_events.keys())
            hooks_to_migrate = WebhookConfig.query.all()
            # V10.10.20: changed 必须在循环外初始化——原实现存在两个缺陷：
            # 1) WebhookConfig 表为空（全新部署/纯净空库）时循环不执行，循环外 if changed 直接抛
            #    UnboundLocalError（日志表现为"[V10.9 Migration] 监控范围迁移跳过: cannot access
            #    local variable 'changed'..."），且导致 db.session.commit() 被跳过；
            # 2) changed 每轮循环重置，循环外判断只反映最后一轮，前几轮的修改会丢失提交。
            changed = False
            for wh in hooks_to_migrate:
                # monitor_user_ids 空列表 → 填全部用户
                raw_uids = getattr(wh, 'monitor_user_ids', None) or '[]'
                try:
                    uids = _migrate_json.loads(raw_uids) if isinstance(raw_uids, str) else raw_uids
                    if not uids:
                        wh.monitor_user_ids = _migrate_json.dumps(all_user_ids)
                        changed = True
                except Exception:
                    pass
                # monitor_event_types 空列表 → 填全部事件
                raw_types = getattr(wh, 'monitor_event_types', None) or '[]'
                try:
                    types = _migrate_json.loads(raw_types) if isinstance(raw_types, str) else raw_types
                    if not types:
                        wh.monitor_event_types = _migrate_json.dumps(all_event_types)
                        changed = True
                except Exception:
                    pass
                # V10.9.1: 修复 notify_pages 矩阵遗漏 — batch_delete 大类补 admin_webhooks 页面
                raw_pages = getattr(wh, 'notify_pages', None) or '{}'
                try:
                    pages_dict = _migrate_json.loads(raw_pages) if isinstance(raw_pages, str) else raw_pages
                    if isinstance(pages_dict, dict):
                        bd_pages = pages_dict.get('batch_delete', [])
                        if isinstance(bd_pages, list) and 'admin_webhooks' not in bd_pages:
                            bd_pages.append('admin_webhooks')
                            pages_dict['batch_delete'] = bd_pages
                            wh.notify_pages = _migrate_json.dumps(pages_dict)
                            changed = True
                except Exception:
                    pass
            if changed:
                db.session.commit()
                print(f"[V10.9 Migration] 已将 {sum(1 for h in hooks_to_migrate if getattr(h, '_sa_instance_state', None) and h in db.session.dirty)} 个 Webhook 通道的空监控范围/矩阵遗漏修复")
        except Exception as e:
            print(f"[V10.9 Migration] 监控范围迁移跳过: {e}")

        with db.engine.connect() as conn:
            for sql in migration_sqls:
                try:
                    conn.execute(db.text(sql))
                    conn.commit()
                except Exception:
                    pass
        print("[Init] 历史 SQL 迁移执行完成，进入 V10.9 数据迁移...")

        # 开启 SQLite WAL 模式并设置繁忙等待超时，彻底消除并发读写排他锁与请求卡死
        if db_url.startswith('sqlite:'):
            try:
                with db.engine.connect() as conn:
                    conn.execute(db.text("PRAGMA journal_mode=WAL;"))
                    conn.execute(db.text("PRAGMA busy_timeout=30000;"))
                    conn.commit()
            except Exception:
                pass

        # V10.10.16: 迁移完成，标记数据库结构版本
        _mark_db_schema_current()

        # V10.10.16: 每次重启都需执行的轻量同步（风控重置 + 管理员账号同步）
        _do_startup_sync()

# 应用加载时自动执行数据库初始化与版本迁移（支持 Gunicorn / WSGI / App 启动）
try:
    init_database()
except Exception as _e:
    print(f"[Warning] 应用启动自动初始化数据库提示: {_e}")


@app.route('/')
@login_required
def index():
    # V7 修复：无礼金账本权限的用户直接访问首页（/）时，
    # 跳转权限工单页引导申请权限，避免在无权限状态下渲染账本页面
    if not getattr(current_user, 'is_admin', False) and hasattr(current_user, 'can_access_menu'):
        if not current_user.can_access_menu('ledger'):
            flash('您暂无权限访问【礼金账本】功能模块，请在下方提交权限申请工单，待管理员审批通过后即可使用。', 'warning')
            return redirect(url_for('permission_tickets_view'))
    query_str = request.args.get('search', '').strip() or request.args.get('q', '').strip()
    reason_filter = request.args.get('reason', '').strip()
    sort_by = request.args.get('sort', 'created_at_desc').strip()
    
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 10, type=int)
    if per_page not in [5, 10, 20, 50, 100]:
        per_page = 10

    query = get_accessible_records_query(current_user)

    if query_str:
        search_pattern = f"%{query_str}%"
        num_val = None
        try:
            num_val = float(query_str)
        except ValueError:
            parsed = cn2num(query_str)
            if parsed > 0:
                num_val = parsed
        except Exception:
            pass

        or_conditions = [
            GiftRecord.name.ilike(search_pattern),
            GiftRecord.address.ilike(search_pattern),
            GiftRecord.phone.ilike(search_pattern),
            GiftRecord.event_reason.ilike(search_pattern),
            GiftRecord.notes.ilike(search_pattern),
            db.cast(GiftRecord.amount, db.String).ilike(search_pattern),
            db.cast(GiftRecord.age, db.String).ilike(search_pattern)
        ]
        if num_val is not None:
            or_conditions.append(GiftRecord.amount == num_val)

        query = query.filter(db.or_(*or_conditions))

    if reason_filter:
        query = query.filter(GiftRecord.event_reason == reason_filter)

    type_filter = request.args.get('type', '').strip()
    if type_filter in ('receive', 'send'):
        query = query.filter(GiftRecord.record_type == type_filter)

    if sort_by == 'amount_desc':
        query = query.order_by(GiftRecord.amount.desc())
    elif sort_by == 'amount_asc':
        query = query.order_by(GiftRecord.amount.asc())
    elif sort_by in ['created_at_asc', 'oldest']:
        query = query.order_by(GiftRecord.created_at.asc())
    else:
        query = query.order_by(GiftRecord.created_at.desc())

    # Get total statistics before pagination
    all_filtered_records = query.all()
    total_count = len(all_filtered_records)
    total_amount = sum(r.amount for r in all_filtered_records) if all_filtered_records else 0.0
    avg_amount = round(total_amount / total_count, 2) if total_count > 0 else 0.0
    max_amount = max((r.amount for r in all_filtered_records), default=0.0)

    # Apply pagination
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    records = pagination.items

    if current_user.is_admin or current_user.can_view_others_for('ledger'):
        all_reasons_query = db.session.query(GiftRecord.event_reason).distinct().all()
    else:
        all_reasons_query = db.session.query(GiftRecord.event_reason).filter_by(user_id=current_user.id).distinct().all()
    reasons_list = [r[0] for r in all_reasons_query if r[0]]

    default_reasons = ['婚宴', '满月酒', '周岁宴', '寿宴', '升学宴', '乔迁宴', '白事人情', '其它']
    for dr in default_reasons:
        if dr not in reasons_list:
            reasons_list.append(dr)

    # 审计日志：记录查询与浏览操作
    if query_str or reason_filter:
        details = []
        if query_str:
            details.append(f"检索词: [{query_str}]")
        if reason_filter:
            details.append(f"筛选事由: [{reason_filter}]")
        log_action('查询记录', "，".join(details))
    else:
        log_action('查询记录', f'浏览明细列表（第 {page} 页）')

    active_banquets = (
        Banquet.query.filter(Banquet.deleted_at.is_(None)).order_by(Banquet.created_at.desc()).all()
        if current_user.is_admin
        else Banquet.query.filter_by(user_id=current_user.id).filter(Banquet.deleted_at.is_(None)).order_by(Banquet.created_at.desc()).all()
    )

    return render_template(
        'index.html',
        records=records,
        pagination=pagination,
        per_page=per_page,
        total_count=total_count,
        total_amount=total_amount,
        avg_amount=avg_amount,
        max_amount=max_amount,
        reasons_list=reasons_list,
        active_banquets=active_banquets,
        query_str=query_str,
        reason_filter=reason_filter,
        type_filter=type_filter,
        sort_by=sort_by,
        num2cn=num2cn
    )

# ---------------- 风控助手函数与缓存 ----------------
LOGIN_FAIL_COUNTS = {}
LOGIN_LOCK_UNTILS = {}
FORGOT_SECURITY_FAIL_COUNTS = {}
FORGOT_SECURITY_LOCK_UNTILS = {}

def get_login_risk_status(user_or_username):
    """获取用户登录风控状态：(is_locked, lock_wait_seconds, fail_count)"""
    if not user_or_username:
        return False, 0, 0
    username = getattr(user_or_username, 'username', str(user_or_username))
    now = time.time()
    fail_count, lock_until = LoginRisk.get_risk(username)
    # 同时同步内存字典状态
    mem_fail = LOGIN_FAIL_COUNTS.get(username, 0)
    mem_lock = LOGIN_LOCK_UNTILS.get(username, 0)
    final_fail = max(fail_count, mem_fail)
    final_lock = max(lock_until, mem_lock)

    if final_lock and final_lock > now:
        wait = int(final_lock - now)
        return True, wait, final_fail
    return False, 0, final_fail

def record_login_failure(user_or_username):
    """记录用户登录失败"""
    if not user_or_username:
        return
    username = getattr(user_or_username, 'username', str(user_or_username))
    now = time.time()
    fail_count = LoginRisk.record_fail(username)
    LOGIN_FAIL_COUNTS[username] = fail_count

    max_attempts = get_max_login_attempts()
    if fail_count >= max_attempts:
        lockout_seconds = get_login_lockout_seconds()
        lock_until = now + lockout_seconds
        LoginRisk.set_lock_until(username, lock_until)
        LOGIN_LOCK_UNTILS[username] = lock_until
        log_action('登录风控锁定', f'用户 [{username}] 登录连续失败 {fail_count} 次，触发风控锁定 {lockout_seconds} 秒')

def clear_login_risk(user_or_username):
    """清除登录风控"""
    if not user_or_username:
        return
    username = getattr(user_or_username, 'username', str(user_or_username))
    LoginRisk.clear_risk(username)
    LOGIN_FAIL_COUNTS.pop(username, None)
    LOGIN_LOCK_UNTILS.pop(username, None)

def get_forgot_security_risk_status(user_or_username):
    """获取指定用户找回密码密保验证的风控状态：(cur_fail_count, is_locked, lock_wait_seconds)"""
    if not user_or_username:
        return 0, False, 0
    username = getattr(user_or_username, 'username', str(user_or_username))
    now = time.time()
    fail_count, lock_until = SecurityRisk.get_risk(username)
    mem_fail = FORGOT_SECURITY_FAIL_COUNTS.get(username, 0)
    mem_lock = FORGOT_SECURITY_LOCK_UNTILS.get(username, 0)
    final_fail = max(fail_count, mem_fail)
    final_lock = max(lock_until, mem_lock)

    if final_lock and final_lock > now:
        wait = int(final_lock - now)
        return final_fail, True, wait
    return final_fail, False, 0

def record_forgot_security_failure(user_or_username):
    """记录密保验证失败"""
    if not user_or_username:
        return
    username = getattr(user_or_username, 'username', str(user_or_username))
    now = time.time()
    fail_count = SecurityRisk.record_fail(username)
    FORGOT_SECURITY_FAIL_COUNTS[username] = fail_count

    max_attempts = get_max_security_attempts()
    if fail_count >= max_attempts:
        lockout_seconds = get_login_lockout_seconds()
        lock_until = now + lockout_seconds
        SecurityRisk.set_lock_until(username, lock_until)
        FORGOT_SECURITY_LOCK_UNTILS[username] = lock_until
        log_action('密保风控锁定', f'用户 [{username}] 密保验证连续错误 {fail_count} 次，触发风控锁定 {lockout_seconds} 秒')

def clear_forgot_security_risk(user_or_username):
    """清除密保风控"""
    if not user_or_username:
        return
    username = getattr(user_or_username, 'username', str(user_or_username))
    SecurityRisk.clear_risk(username)
    FORGOT_SECURITY_FAIL_COUNTS.pop(username, None)
    FORGOT_SECURITY_LOCK_UNTILS.pop(username, None)

def get_user_risk_status(username):
    """获取指定用户名的风控状态：(cur_fail_count, is_locked, lock_wait_seconds, require_captcha)"""
    if not username:
        return 0, False, 0, False
    is_locked, lock_wait, cur_fail_count = get_login_risk_status(username)
    max_attempts = get_max_login_attempts()
    require_captcha = (cur_fail_count >= max_attempts) or is_locked
    return cur_fail_count, is_locked, lock_wait, require_captcha

@app.route('/login/check_risk')
def login_check_risk():
    username = request.args.get('username', '').strip()
    cur_fail_count, is_locked, lock_wait, require_captcha = get_user_risk_status(username)
    return jsonify({
        'username': username,
        'fail_count': cur_fail_count,
        'is_locked': is_locked,
        'lock_wait': lock_wait,
        'require_captcha': require_captcha
    })

@app.route('/login/captcha')
def login_captcha():
    num1 = random.randint(1, 20)
    num2 = random.randint(1, 20)
    op = random.choice(['+', '-'])
    if op == '-':
        if num1 < num2:
            num1, num2 = num2, num1
        ans = num1 - num2
    else:
        ans = num1 + num2
    session['login_captcha_ans'] = str(ans)
    expr = f"{num1} {op} {num2} = ?"
    return jsonify({'expr': expr})

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))

    now = datetime.now().timestamp()
    max_attempts = get_max_login_attempts()
    lockout_seconds = get_login_lockout_seconds()

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        remember = True if request.form.get('remember') else False
        user_captcha = request.form.get('captcha', '').strip()

        cur_fail_count, is_locked, lock_wait, require_captcha = get_user_risk_status(username)

        # 1. 检查锁定状态
        if is_locked:
            flash(f'账号 [{username}] 密码错误过多，触发安全保护！请等待 {lock_wait} 秒后再试。', 'danger')
            return render_template('login.html', require_captcha=True, lock_wait=lock_wait, username=username)

        # 2. 检查验证码（如果达到最大尝试次数，强制校验验证码）
        if require_captcha:
            real_captcha = session.get('login_captcha_ans')
            if not user_captcha or user_captcha != real_captcha:
                flash('验证码错误或未输入，请重新计算并输入！', 'danger')
                return render_template('login.html', require_captcha=True, username=username)

        # 3. 校验账号密码
        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            if not user.is_active:
                log_action('登录失败', f'已被禁用的账号 [{username}] 尝试登录', user=user)
                flash('该账号已被禁用/冻结，无法登录使用，请联系管理员处理！', 'danger')
                return render_template('login.html', require_captcha=require_captcha, username=username)
            
            # 登录成功，清除该账号在服务端的失败计数与锁定状态
            LOGIN_FAIL_COUNTS.pop(username, None)
            LOGIN_LOCK_UNTILS.pop(username, None)
            session.pop('login_captcha_ans', None)

            token = secrets.token_hex(16)
            user.session_token = token
            db.session.commit()
            login_user(user, remember=remember)
            session['session_token'] = token
            session['login_time'] = datetime.now().timestamp()
            session['last_activity'] = datetime.now().timestamp()
            log_action('用户登录', f'用户成功登录系统', user=user)
            try:
                trigger_webhook_event(
                    WebhookConfig.query.filter_by(is_enabled=True).all(), 'security',
                    f'用户登录',
                    f'操作人：{user.username} | 页面：系统安全 | 结果：登录成功',
                    page_key='security', user_name=user.username,
                    operator_id=user.id
                )
            except Exception:
                pass
            flash(f'欢迎回来，{user.username}！', 'success')
            # 登录时主动检测是否有当前用户未读的有效广播并提示
            try:
                read_bc_ids = [r[0] for r in db.session.query(BroadcastRead.broadcast_id).filter_by(user_id=user.id).all()]
                all_bcs = Broadcast.query.filter_by(is_active=True).all()
                unread_count = 0
                for b in all_bcs:
                    if b.id in read_bc_ids:
                        continue
                    if b.scope == "admin" and not user.is_admin:
                        continue
                    if b.scope == "user" and user.is_admin:
                        continue
                    unread_count += 1
                if unread_count > 0:
                    flash(f"系统通知：您有 {unread_count} 条未读广播，请查阅！", "info")
            except Exception as e:
                print(f"[Login Broadcast Check Error]: {e}")
            return redirect(url_for('index'))
        else:
            # 登录失败：更新服务端全局字典
            new_fail_count = cur_fail_count + 1
            LOGIN_FAIL_COUNTS[username] = new_fail_count
            log_action('登录失败', f'尝试登录用户名 [{username}] 失败（连续失败{new_fail_count}次）')
            try:
                trigger_webhook_event(
                    WebhookConfig.query.filter_by(is_enabled=True).all(), 'security',
                    f'登录失败',
                    f'操作人：{username} | 页面：系统安全 | 结果：登录失败（连续{new_fail_count}次）',
                    page_key='security', user_name=username,
                    operator_id=None
                )
            except Exception:
                pass

            if new_fail_count >= max_attempts:
                lock_duration = lockout_seconds
                if lock_duration > 0:
                    LOGIN_LOCK_UNTILS[username] = now + lock_duration
                    lock_desc = f"{lock_duration // 60} 分钟" if lock_duration >= 60 and lock_duration % 60 == 0 else f"{lock_duration} 秒"
                    flash(f'账号 [{username}] 密码错误次数达到 {new_fail_count} 次，需要验证码且必须等待 {lock_desc} 后才能再次尝试！', 'danger')
                    return render_template('login.html', require_captcha=True, lock_wait=lock_wait, username=username)
                else:
                    flash(f'账号 [{username}] 密码错误次数达到 {new_fail_count} 次，请输入下方安全验证码！', 'danger')
                    return render_template('login.html', require_captcha=True, username=username)
            else:
                remaining = max_attempts - new_fail_count
                flash(f'用户名或密码错误，请重试。（账号 [{username}] 连续错误 {new_fail_count} 次，再错 {remaining} 次将触发验证码）', 'danger')
                return render_template('login.html', require_captcha=require_captcha, username=username)

    # GET 请求处理
    username = request.args.get('username', '').strip()
    if username:
        cur_fail_count, is_locked, lock_wait, require_captcha = get_user_risk_status(username)
        return render_template('login.html', require_captcha=require_captcha, lock_wait=lock_wait if is_locked else None, username=username)
    return render_template('login.html', require_captcha=False)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('index'))

    reg_mode = SystemSetting.get_val('registration_mode', 'invite_only')
    token_str = request.args.get('token') or request.form.get('token', '').strip()
    reg_token = None

    if reg_mode == 'invite_only':
        if not token_str:
            flash('系统已开启注册邀请制，必须通过管理员生成的有效邀请链接才能进行注册！', 'warning')
            return render_template('register.html', invalid_token=True, registration_mode=reg_mode, token=token_str)

        reg_token = RegistrationToken.query.filter_by(token=token_str).first()

        if not reg_token:
            flash('无效的注册邀请链接，请联系管理员获取正确的注册链接！', 'danger')
            return render_template('register.html', invalid_token=True, registration_mode=reg_mode, token=token_str)

        if reg_token.used or (reg_token.max_uses and reg_token.use_count >= reg_token.max_uses):
            flash('该注册邀请链接使用次数已达上限或已被使用过，无法再次注册，请联系管理员重新生成！', 'danger')
            return render_template('register.html', invalid_token=True, registration_mode=reg_mode, token=token_str)

        if reg_token.expires_at < datetime.now():
            flash('该注册邀请链接已超时失效，请联系管理员生成新的注册链接！', 'danger')
            return render_template('register.html', invalid_token=True, registration_mode=reg_mode, token=token_str)
    else:
        # 自由注册模式：如果携带有效 token 则关联，未携带也可直接注册
        if token_str:
            reg_token = RegistrationToken.query.filter_by(token=token_str).first()

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        confirm_password = request.form.get('confirm_password', '').strip()
        q1 = request.form.get('security_question_1', '').strip() or request.form.get('security_question', '').strip()
        a1 = request.form.get('security_answer_1', '').strip() or request.form.get('security_answer', '').strip()
        q2 = request.form.get('security_question_2', '').strip()
        a2 = request.form.get('security_answer_2', '').strip()

        if not username or not password or not q1 or not a1 or not q2 or not a2:
            flash('所有必填字段均不能为空，且必须设置至少 2 个密保问题与答案！', 'danger')
            return render_template('register.html', token=token_str, invalid_token=False, registration_mode=reg_mode)

        if q1 == q2:
            flash('两个密保问题不能相同，请选择或输入不同的密保问题！', 'danger')
            return render_template('register.html', token=token_str, invalid_token=False, registration_mode=reg_mode)

        # 1. Username Format Validation (2-20 chars)
        if not re.match(r'^[a-zA-Z0-9_\u4e00-\u9fa5]{2,20}$', username):
            flash('用户名格式不符合要求！长度须为 2-20 位，仅允许汉字、字母、数字及下划线。', 'danger')
            return render_template('register.html', token=token_str, invalid_token=False, registration_mode=reg_mode)

        # 2. Password Strength Validation (min 6 chars, containing both letters and numbers)
        if len(password) < 6 or not re.search(r'[a-zA-Z]', password) or not re.search(r'\d', password):
            flash('密码强度不足！密码长度至少为 6 位，且必须包含字母和数字的组合。', 'danger')
            return render_template('register.html', token=token_str, invalid_token=False, registration_mode=reg_mode)

        if password != confirm_password:
            flash('两次输入的密码不一致！', 'danger')
            return render_template('register.html', token=token_str, invalid_token=False, registration_mode=reg_mode)

        existing_user = User.query.filter_by(username=username).first()
        if existing_user:
            flash('该用户名已被注册，请尝试其他名称。', 'warning')
            return render_template('register.html', token=token_str, invalid_token=False, registration_mode=reg_mode)

        user = User(username=username)
        user.set_password(password)
        user.set_security_answers(q1, a1, q2, a2)
        user.allowed_menus = ''  # 新注册用户默认无任何菜单权限，需通过工单申请

        # 增加 token 使用次数，达到上限标记为已使用（自由注册模式下 reg_token 可能为 None）
        if reg_token:
            reg_token.use_count = (reg_token.use_count or 0) + 1
            if reg_token.max_uses and reg_token.use_count >= reg_token.max_uses:
                reg_token.used = True
                reg_token.used_at = datetime.now()

        db.session.add(user)
        db.session.commit()

        log_action('用户注册', f'新用户 [{username}] 识别邀请码成功注册账号', user=user)
        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(), 'system',
                f'新用户注册',
                f'操作人：{username} | 页面：系统安全 | 新用户ID：{user.id}',
                page_key='security', user_name=username,
                operator_id=user.id
            )
        except Exception:
            pass
        flash('注册成功！当前账号暂无功能页面权限，登录后请在「权限申请」页面提交权限申请工单，等待管理员审批后即可使用各功能模块。', 'success')
        return redirect(url_for('login'))

    return render_template('register.html', token=token_str, invalid_token=False, registration_mode=reg_mode)

@app.route('/forgot-password/captcha')
def forgot_password_captcha():
    num1 = random.randint(1, 20)
    num2 = random.randint(1, 20)
    op = random.choice(['+', '-'])
    if op == '-':
        if num1 < num2:
            num1, num2 = num2, num1
        ans = num1 - num2
    else:
        ans = num1 + num2
    session['forgot_captcha_ans'] = str(ans)
    expr = f"{num1} {op} {num2} = ?"
    return jsonify({'expr': expr})

@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        step = request.form.get('step')
        
        if step == 'find_user':
            username = request.form.get('username', '').strip()
            user = User.query.filter_by(username=username).first()
            if not user:
                flash('找不到该用户名对应的账号！', 'danger')
                return render_template('forgot_password.html', step='find_user', username=username)

            if not user.is_active:
                flash('该账号已被锁定或禁用，无法找回密码！请联系系统管理员解锁账号。', 'danger')
                return render_template('forgot_password.html', step='find_user', account_locked=True, locked_username=username)

            cur_fail_count, is_locked, lock_wait = get_forgot_security_risk_status(username)
            max_security_attempts = get_max_security_attempts()
            remaining_attempts = max(0, max_security_attempts - cur_fail_count)
            require_captcha = getattr(user, 'is_admin', False) and (cur_fail_count >= max_security_attempts or is_locked)
            return render_template('forgot_password.html', user=user, step='answer_question',
                                   cur_fail_count=cur_fail_count, is_locked=is_locked, lock_wait=lock_wait,
                                   max_security_attempts=max_security_attempts,
                                   remaining_attempts=remaining_attempts,
                                   require_captcha=require_captcha)

        elif step == 'reset_pass':
            username = request.form.get('username', '').strip()
            security_answer = request.form.get('security_answer', '').strip()
            security_answer_1 = request.form.get('security_answer_1', '').strip() or security_answer
            security_answer_2 = request.form.get('security_answer_2', '').strip()
            new_password = request.form.get('new_password', '').strip()
            confirm_password = request.form.get('confirm_password', '').strip()

            user = User.query.filter_by(username=username).first()
            if not user:
                flash('用户不存在！', 'danger')
                return render_template('forgot_password.html', step='find_user', username=username)

            if not user.is_active:
                flash('该账号已被锁定或禁用，无法重置密码！请联系系统管理员解锁账号。', 'danger')
                return render_template('forgot_password.html', step='find_user', account_locked=True, locked_username=username)

            max_security_attempts = get_max_security_attempts()
            lockout_seconds = get_login_lockout_seconds()
            cur_fail_count, is_locked, lock_wait = get_forgot_security_risk_status(username)
            user_captcha = request.form.get('captcha', '').strip()
            require_captcha = getattr(user, 'is_admin', False) and (cur_fail_count >= max_security_attempts or is_locked)

            # 1. 检查锁定状态（针对管理员用户防爆破锁定）
            if is_locked and lock_wait > 0:
                lock_desc = f"{lock_wait // 60} 分钟" if lock_wait >= 60 and lock_wait % 60 == 0 else f"{lock_wait} 秒"
                flash(f'该账号处于锁定保护中，请等待 {lock_desc} 后再重试。', 'danger')
                return render_template('forgot_password.html', user=user, step='answer_question',
                                       cur_fail_count=cur_fail_count, is_locked=True, lock_wait=lock_wait,
                                       max_security_attempts=max_security_attempts,
                                       remaining_attempts=0,
                                       require_captcha=require_captcha,
                                       security_answer_1=security_answer_1, security_answer_2=security_answer_2)

            # 2. 检查验证码
            if require_captcha:
                real_captcha = session.get('forgot_captcha_ans')
                if not user_captcha or user_captcha != real_captcha:
                    flash('验证码错误或未输入，请重新计算并输入！', 'danger')
                    return render_template('forgot_password.html', user=user, step='answer_question',
                                           cur_fail_count=cur_fail_count, is_locked=is_locked, lock_wait=lock_wait,
                                           max_security_attempts=max_security_attempts,
                                           remaining_attempts=max(0, max_security_attempts - cur_fail_count),
                                           require_captcha=True,
                                           security_answer_1=security_answer_1, security_answer_2=security_answer_2)

            if not user.check_security_answers(security_answer_1, security_answer_2):
                new_fail_count = FORGOT_SECURITY_FAIL_COUNTS.get(username, 0) + 1
                FORGOT_SECURITY_FAIL_COUNTS[username] = new_fail_count
                log_action('找回密码失败', f'尝试找回用户名 [{username}] 密保答案验证错误（连续错误{new_fail_count}次）')

                if not getattr(user, 'is_admin', False) and new_fail_count >= max_security_attempts:
                    user.is_active = False
                    user.session_token = None
                    db.session.commit()
                    FORGOT_SECURITY_FAIL_COUNTS.pop(username, None)
                    FORGOT_SECURITY_LOCK_UNTILS.pop(username, None)
                    log_action('账号自动锁定', f'用户 [{username}] 密保验证错误达到上限（{new_fail_count}次），账号已被系统自动锁定')
                    flash(f'密保答案连续错误达到 {max_security_attempts} 次上限，该账号已被系统锁定！请联系系统管理员解锁账号。', 'danger')
                    return render_template('forgot_password.html', step='find_user', account_locked=True, locked_username=username)
                elif getattr(user, 'is_admin', False) and new_fail_count >= max_security_attempts:
                    now = datetime.now().timestamp()
                    lock_duration = lockout_seconds
                    if lock_duration > 0:
                        FORGOT_SECURITY_LOCK_UNTILS[username] = now + lock_duration
                    lock_desc = f"{lock_duration // 60} 分钟" if lock_duration >= 60 and lock_duration % 60 == 0 else f"{lock_duration} 秒"
                    flash(f'密保答案验证错误！管理员账号错误已达 {new_fail_count} 次，需要验证码且必须等待 {lock_desc} 后方可重试。', 'danger')
                    return render_template('forgot_password.html', user=user, step='answer_question',
                                           cur_fail_count=new_fail_count, is_locked=True if lock_duration > 0 else False,
                                           lock_wait=lock_duration,
                                           max_security_attempts=max_security_attempts,
                                           remaining_attempts=0,
                                           require_captcha=True,
                                           security_answer_1=security_answer_1, security_answer_2=security_answer_2)
                else:
                    remaining = max(0, max_security_attempts - new_fail_count)
                    flash(f'密保问题答案验证错误！您还剩 {remaining} 次尝试机会。', 'danger')
                    return render_template('forgot_password.html', user=user, step='answer_question',
                                           cur_fail_count=new_fail_count, is_locked=False, lock_wait=0,
                                           max_security_attempts=max_security_attempts,
                                           remaining_attempts=remaining,
                                           require_captcha=False,
                                           security_answer_1=security_answer_1, security_answer_2=security_answer_2)

            if new_password != confirm_password:
                remaining = 0 if getattr(user, 'is_admin', False) and cur_fail_count >= max_security_attempts else max(0, max_security_attempts - cur_fail_count)
                flash('两次输入的新密码不一致！', 'danger')
                return render_template('forgot_password.html', user=user, step='answer_question',
                                       cur_fail_count=cur_fail_count, is_locked=False, lock_wait=0,
                                       max_security_attempts=max_security_attempts,
                                       remaining_attempts=remaining,
                                       require_captcha=require_captcha,
                                       security_answer_1=security_answer_1, security_answer_2=security_answer_2)

            # 验证新密码强度
            is_valid, msg = check_password_complexity(new_password)
            if not is_valid:
                remaining = 0 if getattr(user, 'is_admin', False) and cur_fail_count >= max_security_attempts else max(0, max_security_attempts - cur_fail_count)
                flash(msg, 'danger')
                return render_template('forgot_password.html', user=user, step='answer_question',
                                       cur_fail_count=cur_fail_count, is_locked=False, lock_wait=0,
                                       max_security_attempts=max_security_attempts,
                                       remaining_attempts=remaining,
                                       require_captcha=require_captcha,
                                       security_answer_1=security_answer_1, security_answer_2=security_answer_2)

            # 密保校验成功，清除该账号在找回密码服务端的失败计数与锁定状态
            FORGOT_SECURITY_FAIL_COUNTS.pop(username, None)
            FORGOT_SECURITY_LOCK_UNTILS.pop(username, None)

            user.set_password(new_password)
            db.session.commit()

            log_action('重置密码', f'用户成功重置个人密码', user=user)
            try:
                trigger_webhook_event(
                    WebhookConfig.query.filter_by(is_enabled=True).all(), 'security',
                    f'找回密码重置',
                    f'操作人：{user.username} | 页面：系统安全 | 结果：密码重置成功',
                    page_key='security', user_name=user.username,
                    operator_id=user.id
                )
            except Exception:
                pass
            flash('密码重置成功！请使用新密码重新登录。', 'success')
            return redirect(url_for('login'))

    return render_template('forgot_password.html', step='find_user')

@app.route('/logout')
def logout():
    if current_user.is_authenticated:
        log_action('退出登录', f'用户退出系统登录')
        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(), 'security',
                f'用户退出登录',
                f'操作人：{current_user.username} | 页面：系统安全',
                page_key='security', user_name=current_user.username,
                operator_id=current_user.id
            )
        except Exception:
            pass
        current_user.session_token = None
        db.session.commit()
    logout_user()
    session.clear()
    flash('您已成功退出登录。', 'info')
    resp = make_response(redirect(url_for('login')))
    remember_cookie = app.config.get('REMEMBER_COOKIE_NAME', 'remember_token')
    session_cookie = app.config.get('SESSION_COOKIE_NAME', 'session')
    resp.delete_cookie(remember_cookie)
    resp.delete_cookie(session_cookie)
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp

@app.route('/record/add', methods=['POST'])
@login_required
def add_record():
    is_ajax = request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    # V10.4: 级别1可正常新增自身记录，移除 == 1 拦截
    name = request.form.get('name', '').strip()
    age_str = request.form.get('age', '').strip()
    address = request.form.get('address', '').strip()
    phone = request.form.get('phone', '').strip()
    amount_str = request.form.get('amount', '').strip()
    event_reason = request.form.get('event_reason', '').strip()
    custom_reason = request.form.get('custom_reason', '').strip()
    notes = request.form.get('notes', '').strip()

    if event_reason == '其它' and custom_reason:
        event_reason = custom_reason

    if not name:
        if is_ajax:
            return jsonify({'code': 400, 'message': '客人姓名为必填字段！', 'field': 'name'}), 400
        flash('客人姓名为必填字段！', 'danger')
        return redirect(url_for('index'))

    if not amount_str:
        if is_ajax:
            return jsonify({'code': 400, 'message': '礼金数额为必填字段！', 'field': 'amount'}), 400
        flash('礼金数额为必填字段！', 'danger')
        return redirect(url_for('index'))

    if not event_reason:
        if is_ajax:
            return jsonify({'code': 400, 'message': '办席原因为必填字段！', 'field': 'event_reason'}), 400
        flash('办席原因为必填字段！', 'danger')
        return redirect(url_for('index'))

    try:
        amount = cn2num(amount_str)
        if amount <= 0:
            raise ValueError
    except Exception:
        if is_ajax:
            return jsonify({'code': 400, 'message': '礼金数额必须是大于0的有效数值或大写金额！', 'field': 'amount'}), 400
        flash('礼金数额必须是有效的大于0的数值或大写金额！', 'danger')
        return redirect(url_for('index'))

    age = int(age_str) if age_str and age_str.isdigit() else None
    raw_type = request.form.get('record_type', 'receive').strip()
    if raw_type in ('send', 'give', '送礼', '随礼', '出礼'):
        record_type = 'send'
    else:
        record_type = 'receive'

    banquet_id_val = None
    if record_type == 'receive':
        b_id_str = request.form.get('banquet_id', '').strip()
        if b_id_str and b_id_str.isdigit():
            b_cand = db.session.get(Banquet, int(b_id_str))
            if b_cand and not b_cand.deleted_at:
                banquet_id_val = b_cand.id

    record = GiftRecord(
        name=name,
        age=age,
        address=address,
        phone=phone,
        amount=amount,
        event_reason=event_reason,
        record_type=record_type,
        banquet_id=banquet_id_val,
        notes=notes,
        user_id=current_user.id
    )
    db.session.add(record)
    db.session.commit()

    type_str = "随礼(出礼)" if record_type == 'send' else "收礼(入礼)"
    log_action('新增记录', f'新增{type_str}: [{name}]，金额: {amount}元，事由: {event_reason}')
    try:
        trigger_webhook_event(
            WebhookConfig.query.filter_by(is_enabled=True).all(), 'record_create',
            f'{current_user.username} 新增{type_str}：客人「{name}」，金额 {amount:.2f}元，事由：{event_reason}',
            f'操作人：{current_user.username} | 页面：礼金账本 | 类型：{type_str} | 客人：{name} | 金额：{amount:.2f}元 | 事由：{event_reason}',
            page_key='ledger', user_name=current_user.username,
            operator_id=current_user.id
        )
    except Exception:
        pass
    if is_ajax:
        return jsonify({'code': 200, 'message': f'成功保存 [{name}] 的礼金记录！', 'record_id': record.id})
    flash(f'成功保存 [{name}] 的礼金记录！', 'success')
    return redirect(url_for('index'))

@app.route('/record/edit/<int:record_id>', methods=['POST'])
@login_required
def edit_record(record_id):
    is_ajax = request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    record = GiftRecord.query.get_or_404(record_id)

    if not can_user_edit_record(current_user, record):
        if is_ajax:
            return jsonify({'code': 403, 'message': '您没有权限修改此记录！'}), 403
        flash('您没有权限修改此记录！', 'danger')
        return redirect(url_for('index'))

    name = request.form.get('name', '').strip()
    age_str = request.form.get('age', '').strip()
    address = request.form.get('address', '').strip()
    phone = request.form.get('phone', '').strip()
    amount_str = request.form.get('amount', '').strip()
    event_reason = request.form.get('event_reason', '').strip()
    custom_reason = request.form.get('custom_reason', '').strip()
    notes = request.form.get('notes', '').strip()

    if event_reason == '其它' and custom_reason:
        event_reason = custom_reason

    if not name:
        if is_ajax:
            return jsonify({'code': 400, 'message': '客人姓名为必填字段！', 'field': 'name'}), 400
        flash('客人姓名为必填字段！', 'danger')
        return redirect(url_for('index'))

    if not amount_str:
        if is_ajax:
            return jsonify({'code': 400, 'message': '礼金数额为必填字段！', 'field': 'amount'}), 400
        flash('礼金数额为必填字段！', 'danger')
        return redirect(url_for('index'))

    if not event_reason:
        if is_ajax:
            return jsonify({'code': 400, 'message': '办席原因为必填字段！', 'field': 'event_reason'}), 400
        flash('办席原因为必填字段！', 'danger')
        return redirect(url_for('index'))

    try:
        amount = cn2num(amount_str)
        if amount <= 0:
            raise ValueError
    except Exception:
        if is_ajax:
            return jsonify({'code': 400, 'message': '礼金数额必须是大于0的有效数值或大写金额！', 'field': 'amount'}), 400
        flash('礼金数额必须是有效的大于0的数值或大写金额！', 'danger')
        return redirect(url_for('index'))

    raw_type = request.form.get('record_type', '').strip()
    if raw_type in ('send', 'give', '送礼', '随礼', '出礼'):
        record.record_type = 'send'
    elif raw_type in ('receive', '收礼', '入礼'):
        record.record_type = 'receive'

    record.name = name
    record.age = int(age_str) if age_str and age_str.isdigit() else None
    record.address = address
    record.phone = phone
    record.amount = amount
    record.event_reason = event_reason
    record.notes = notes

    if record.record_type == 'receive':
        b_id_str = request.form.get('banquet_id', '').strip()
        if b_id_str and b_id_str.isdigit():
            b_cand = db.session.get(Banquet, int(b_id_str))
            record.banquet_id = b_cand.id if (b_cand and not b_cand.deleted_at) else None
        else:
            record.banquet_id = None
    else:
        record.banquet_id = None

    db.session.commit()
    log_action('修改记录', f'修改记录 ID #{record_id}: 姓名 [{name}]，金额: {amount}元，事由: {event_reason}')
    try:
        trigger_webhook_event(
            WebhookConfig.query.filter_by(is_enabled=True).all(), 'record_update',
            f'{current_user.username} 修改记录：客人「{name}」，金额 {amount:.2f}元，事由：{event_reason}',
            f'操作人：{current_user.username} | 页面：礼金账本 | 客人：{name} | 金额：{amount:.2f}元 | 事由：{event_reason}',
            page_key='ledger', user_name=current_user.username,
            operator_id=current_user.id
        )
    except Exception:
        pass
    if is_ajax:
        return jsonify({'code': 200, 'message': f'记录 [{name}] 修改成功！'})
    flash(f'记录 [{name}] 修改成功！', 'success')
    return redirect(url_for('index'))

@app.route('/record/delete/<int:record_id>', methods=['POST'])
@login_required
def delete_record(record_id):
    is_ajax = request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    record = db.session.get(GiftRecord, record_id)
    if not record:
        if is_ajax:
            return jsonify({'code': 404, 'message': '未找到该记录或记录已被删除！'}), 404
        flash('未找到该记录或记录已被删除！', 'warning')
        return redirect(url_for('index'))

    if not can_user_delete_record(current_user, record):
        if is_ajax:
            return jsonify({'code': 403, 'message': '您没有权限删除此记录！'}), 403
        flash('您没有权限删除此记录！', 'danger')
        return redirect(url_for('index'))

    record_name = record.name
    record_amount = record.amount
    record.deleted_at = datetime.now()
    db.session.commit()
    try:
        trigger_webhook_event(
            WebhookConfig.query.filter_by(is_enabled=True).all(), 'record_delete',
            f'{current_user.username} 删除记录：客人「{record_name}」，金额 {record_amount:.2f}元',
            f'操作人：{current_user.username} | 页面：礼金账本 | 客人：{record_name} | 金额：{record_amount:.2f}元 | 状态：已移入回收站',
            page_key='ledger', user_name=current_user.username,
            operator_id=current_user.id
        )
    except Exception:
        pass
    log_action('删除记录至回收站', f'删除记录 ID #{record_id}: 姓名 [{record_name}] 进入回收站')
    if is_ajax:
        return jsonify({'code': 200, 'message': f'记录 [{record_name}] 已移入回收站！'})
    flash('记录已移入回收站！', 'success')
    return redirect(url_for('index'))

@app.route('/records/batch_delete', methods=['POST'])
@login_required
def batch_delete_records():
    is_ajax = request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    if hasattr(current_user, 'get_menu_perm') and current_user.get_menu_perm('ledger') in (2,):
        if is_ajax:
            return jsonify({'code': 403, 'message': '当前页面权限不允许删除记录！'}), 403
        flash('当前页面权限不允许删除记录！', 'danger')
        return redirect(url_for('index'))
    record_ids = request.form.getlist('record_ids')
    if not record_ids:
        if is_ajax:
            return jsonify({'code': 400, 'message': '请选择要删除的记录！'}), 400
        flash('请选择要删除的记录！', 'warning')
        return redirect(url_for('index'))

    deleted_count = 0
    now_time = datetime.now()
    for rid in record_ids:
        try:
            record_id = int(rid)
        except ValueError:
            continue
        record = db.session.get(GiftRecord, record_id)
        if record and can_user_delete_record(current_user, record):
            record.deleted_at = now_time
            deleted_count += 1

    db.session.commit()
    if deleted_count > 0:
        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(), 'batch_delete',
                f'{current_user.username} 批量删除 {deleted_count} 条记录',
                f'操作人：{current_user.username} | 页面：礼金账本 | 数量：{deleted_count} 条 | 状态：已移入回收站',
                page_key='ledger', user_name=current_user.username,
                operator_id=current_user.id
            )
        except Exception:
            pass
    log_action('批量删除记录至回收站', f'成功批量将 {deleted_count} 条礼金记录移入回收站')
    if is_ajax:
        return jsonify({'code': 200, 'message': f'成功将 {deleted_count} 条记录移入回收站！', 'count': deleted_count})
    flash(f'成功将 {deleted_count} 条记录移入回收站！', 'success')
    return redirect(url_for('index'))

@app.route('/records/delete_all', methods=['POST'])
@login_required
def delete_all_records():
    now_time = datetime.now()
    if current_user.is_admin:
        records = GiftRecord.query.filter(GiftRecord.deleted_at.is_(None)).all()
    elif getattr(current_user, 'can_delete_others', False):
        admin_user_ids = [u.id for u in User.query.filter_by(is_admin=True).all()]
        if admin_user_ids:
            records = GiftRecord.query.filter(GiftRecord.deleted_at.is_(None), ~GiftRecord.user_id.in_(admin_user_ids)).all()
        else:
            records = GiftRecord.query.filter(GiftRecord.deleted_at.is_(None)).all()
    else:
        records = GiftRecord.query.filter_by(user_id=current_user.id, deleted_at=None).all()
    
    deleted_count = len(records)
    for r in records:
        r.deleted_at = now_time
    
    db.session.commit()
    if deleted_count > 0:
        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(), 'clear',
                f'{current_user.username} 清空 {deleted_count} 条记录',
                f'操作人：{current_user.username} | 页面：礼金账本 | 数量：全部 {deleted_count} 条 | 状态：已移入回收站',
                page_key='ledger', user_name=current_user.username,
                operator_id=current_user.id
            )
        except Exception:
            pass
    log_action('全部删除记录至回收站', f'成功将 {deleted_count} 条礼金记录移入回收站')
    flash(f'已成功将 {deleted_count} 条礼金记录移入回收站！', 'success')
    return redirect(url_for('index'))

@app.route('/change-password', methods=['GET', 'POST'])
@login_required
def change_password():
    if request.method == 'POST':
        old_password = request.form.get('old_password', '').strip()
        new_password = request.form.get('new_password', '').strip()
        confirm_password = request.form.get('confirm_password', '').strip()

        if not current_user.check_password(old_password):
            flash('原密码输入错误！', 'danger')
            return redirect(url_for('change_password'))

        if new_password != confirm_password:
            flash('新密码两次输入不一致！', 'danger')
            return redirect(url_for('change_password'))

        current_user.set_password(new_password)
        db.session.commit()
        logout_user()  # 修改密码后强制注销当前会话，确保必须用新密码重新登录

        log_action('修改密码', f'用户成功修改个人密码')
        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(), 'security',
                f'修改密码 [{current_user.username}]',
                f'操作人：{current_user.username} | 页面：系统安全',
                page_key='security', user_name=current_user.username,
                operator_id=current_user.id
            )
        except Exception:
            pass
        flash('密码修改成功，请使用新密码重新登录！', 'success')
        return redirect(url_for('login'))

    return render_template('change_password.html')

@app.route('/profile/security', methods=['GET', 'POST'])
@login_required
def profile_security():
    """普通用户个人安全设置页面：查看/修改自身密码与密保问题"""
    user = current_user._get_current_object()

    if request.method == 'POST':
        # ========== 修改密保问题 ==========
        old_pwd = request.form.get('old_password', '').strip()
        old_ans1 = request.form.get('old_security_answer_1', '').strip()
        old_ans2 = request.form.get('old_security_answer_2', '').strip()

        # 身份验证：旧密码 / 旧密保1 / 旧密保2，答对任一即可
        verified = False
        if old_pwd and user.check_password(old_pwd):
            verified = True
        elif old_ans1 and (user.check_any_security_answer(old_ans1) or user.check_security_answers(old_ans1, old_ans2)):
            verified = True

        if not verified:
            flash('身份验证失败！请输入正确的旧密码或原密保答案后再修改密保问题。', 'danger')
            return redirect(url_for('profile_security'))

        q1 = request.form.get('security_question_1', '').strip()
        a1 = request.form.get('security_answer_1', '').strip()
        q2 = request.form.get('security_question_2', '').strip()
        a2 = request.form.get('security_answer_2', '').strip()

        # 校验：密保问题 1 和答案 1 必填
        if not q1 or not a1:
            flash('密保问题 1 和答案 1 不能为空！', 'warning')
            return redirect(url_for('profile_security'))

        # 校验：密保问题 2 与答案 2 必须成对出现
        if (q2 and not a2) or (a2 and not q2):
            flash('密保问题 2 与答案 2 必须成对填写！', 'warning')
            return redirect(url_for('profile_security'))

        # 校验：两个新密保问题不能相同
        if q1 and q2 and q1 == q2:
            flash('两个新密保问题不能相同，请输入不同的问题！', 'warning')
            return redirect(url_for('profile_security'))

        user.set_security_answers(q1, a1, q2, a2)
        db.session.commit()

        log_action('修改密保问题', f'用户 [{user.username}] 自助修改了密保问题与答案')
        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(), 'security',
                f'修改密保 [{user.username}]',
                f'操作人：{user.username} | 页面：个人安全设置',
                page_key='security', user_name=user.username,
                operator_id=user.id
            )
        except Exception:
            pass
        flash('密保问题修改成功！' + ('新问题 1：' + q1 + '；新问题 2：' + q2 if q2 else '新问题：' + q1), 'success')
        return redirect(url_for('profile_security'))

    # GET：渲染页面
    return render_template('profile_security.html')

@app.route('/admin/users')
@login_required
def admin_users():
    if not current_user.is_admin:
        flash('只有超级管理员才能访问用户管理页面！', 'danger')
        return redirect(url_for('index'))

    users = User.query.order_by(User.id.asc()).all()
    # 获取所有注册邀请链接（最近创建的排前面）
    tokens = RegistrationToken.query.order_by(RegistrationToken.created_at.desc()).all()
    session_timeout_minutes = get_session_timeout_minutes()
    max_login_attempts = get_max_login_attempts()
    max_security_attempts = get_max_security_attempts()
    login_lockout_seconds = get_login_lockout_seconds()

    login_risks = {r.username: r for r in LoginRisk.query.all()}
    sec_risks = {r.username: r for r in SecurityRisk.query.all()}

    uptime_seconds = int(time.time() - APP_START_TIME)
    days = uptime_seconds // 86400
    hours = (uptime_seconds % 86400) // 3600
    minutes = (uptime_seconds % 3600) // 60
    secs = uptime_seconds % 60
    if days > 0:
        uptime_str = f"{days}天 {hours}时 {minutes}分"
    elif hours > 0:
        uptime_str = f"{hours}时 {minutes}分 {secs}秒"
    else:
        uptime_str = f"{minutes}分 {secs}秒"

    total_users_count = len(users)
    active_users_count = sum(1 for u in users if u.is_active)
    total_records_count = GiftRecord.query.count()

    return render_template(
        'admin_users.html',
        users=users,
        tokens=tokens,
        login_risks=login_risks,
        sec_risks=sec_risks,
        session_timeout_minutes=session_timeout_minutes,
        max_login_attempts=max_login_attempts,
        max_security_attempts=max_security_attempts,
        login_lockout_seconds=login_lockout_seconds,
        uptime_str=uptime_str,
        total_users_count=total_users_count,
        active_users_count=active_users_count,
        total_records_count=total_records_count,
        registration_mode=SystemSetting.get_val('registration_mode', 'invite_only'),
        now=datetime.now()
    )

@app.route('/admin/settings/timeout', methods=['POST'])
@login_required
def update_session_timeout():
    if not current_user.is_admin:
        flash('只有超级管理员才能更改系统设置！', 'danger')
        return redirect(url_for('index'))

    try:
        minutes = int(request.form.get('session_timeout_minutes', '60'))
        attempts = int(request.form.get('max_login_attempts', '5'))
        sec_attempts = int(request.form.get('max_security_attempts', '3'))
        lockout_sec = int(request.form.get('login_lockout_seconds', '60'))

        if minutes < 1 or minutes > 10080:
            flash('超时时间必须在 1 到 10080 分钟之间！', 'danger')
        elif attempts < 1 or attempts > 20:
            flash('允许密码最大尝试次数必须在 1 到 20 次之间！', 'danger')
        elif sec_attempts < 1 or sec_attempts > 20:
            flash('允许密保最大尝试次数必须在 1 到 20 次之间！', 'danger')
        elif lockout_sec < 0 or lockout_sec > 86400:
            flash('锁定时长必须在 0 到 86400 秒之间！', 'danger')
        else:
            SystemSetting.set_val('session_timeout_minutes', minutes)
            SystemSetting.set_val('max_login_attempts', attempts)
            SystemSetting.set_val('max_security_attempts', sec_attempts)
            SystemSetting.set_val('login_lockout_seconds', lockout_sec)
            log_action('更新系统配置', f'设置超时时间为 {minutes} 分钟，密码最大尝试次数为 {attempts} 次，密保最大尝试次数为 {sec_attempts} 次，锁定时长为 {lockout_sec} 秒')
            # V10.3: 补充系统安全配置推送
            try:
                trigger_webhook_event(
                    WebhookConfig.query.filter_by(is_enabled=True).all(), 'system',
                    f'{current_user.username} 更新安全配置',
                    f'操作人：{current_user.username} | 页面：用户管理 | 操作：更新系统安全配置',
                    page_key='admin_users', user_name=current_user.username,
                    operator_id=current_user.id
                )
            except Exception:
                pass
            flash('系统安全与登录设置修改成功！', 'success')
    except (ValueError, TypeError):
        flash('无效的配置参数！', 'danger')

    return redirect(url_for('admin_users'))
@app.route('/admin/logs')
@login_required
def admin_logs():
    if not current_user.is_admin:
        flash('只有超级管理员才能查看审计日志！', 'danger')
        return redirect(url_for('index'))

    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 30, type=int)
    if per_page not in [10, 20, 30, 50, 100]:
        per_page = 30

    search_q = request.args.get('q', '').strip()
    sort_by = request.args.get('sort_by', 'created_at').strip()
    sort_order = request.args.get('sort_order', 'desc').strip()

    query = OperationLog.query

    if search_q:
        search_filter = (
            OperationLog.username.ilike(f'%{search_q}%') |
            OperationLog.action.ilike(f'%{search_q}%') |
            OperationLog.detail.ilike(f'%{search_q}%') |
            OperationLog.ip_address.ilike(f'%{search_q}%')
        )
        query = query.filter(search_filter)

    sort_column_map = {
        'id': OperationLog.id,
        'username': OperationLog.username,
        'action': OperationLog.action,
        'created_at': OperationLog.created_at
    }
    col = sort_column_map.get(sort_by, OperationLog.created_at)

    if sort_order == 'asc':
        query = query.order_by(col.asc())
    else:
        query = query.order_by(col.desc())

    logs = query.paginate(page=page, per_page=per_page)

    # V10: 读取审计日志模块配置
    audit_modules_raw = SystemSetting.get_val('audit_log_modules', None)
    audit_modules_enabled = []
    audit_modules_all = [
        ('ledger', '礼金账本'), ('banquets', '专属宴席'), ('reconciliation', '人情对账'),
        ('reminders', '纪念日备忘'), ('recycle_bin', '回收站'), ('admin_users', '用户管理'),
        ('webhooks', 'Webhook'), ('backups', '备份管理'), ('permission_tickets', '权限工单'),
        ('broadcasts', '系统广播'), ('ai_assistant', 'AI助手'), ('auth', '认证安全'),
        ('system', '系统配置'),
    ]
    if not audit_modules_raw:
        audit_modules_enabled = [m[0] for m in audit_modules_all]  # 默认全部启用
    else:
        try:
            audit_modules_enabled = _json_module.loads(audit_modules_raw)
            if not audit_modules_enabled or 'all' in audit_modules_enabled:
                audit_modules_enabled = [m[0] for m in audit_modules_all]
        except Exception:
            audit_modules_enabled = [m[0] for m in audit_modules_all]

    return render_template(
        'admin_logs.html',
        logs=logs,
        q=search_q,
        sort_by=sort_by,
        sort_order=sort_order,
        per_page=per_page,
        audit_modules_all=audit_modules_all,
        audit_modules_enabled=audit_modules_enabled
    )

@app.route('/admin/logs/batch_delete', methods=['POST'])
@login_required
def admin_batch_delete_logs():
    if not current_user.is_admin:
        flash('权限不足！', 'danger')
        return redirect(url_for('index'))

    log_ids = request.form.getlist('log_ids')
    if not log_ids:
        flash('请先勾选需要删除的日志记录！', 'warning')
        return redirect(url_for('admin_logs'))

    try:
        log_ids_int = [int(i) for i in log_ids]
        deleted_count = OperationLog.query.filter(OperationLog.id.in_(log_ids_int)).delete(synchronize_session=False)
        db.session.commit()
        log_action('批量删除日志', f'管理员勾选删除了 {deleted_count} 条操作日志')
        # V10: 补充审计日志删除推送
        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(), 'security',
                f'管理员 {current_user.username} 批量删除了 {deleted_count} 条操作审计日志',
                page_key='admin_logs', user_name=current_user.username, operator_id=current_user.id
            )
        except Exception:
            pass
        flash(f'成功批量删除 {deleted_count} 条审计日志！', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'批量删除操作失败: {str(e)}', 'danger')

    return redirect(url_for('admin_logs'))

@app.route('/admin/generate_invite_link', methods=['POST'])
@login_required
def generate_invite_link():
    if not current_user.is_admin:
        flash('权限不足！', 'danger')
        return redirect(url_for('index'))

    try:
        expire_hours = int(request.form.get('expire_hours', 24))
    except (ValueError, TypeError):
        expire_hours = 24

    try:
        max_uses = int(request.form.get('max_uses', 1))
    except (ValueError, TypeError):
        max_uses = 1

    token_str = secrets.token_urlsafe(32)
    expires_at = datetime.now() + timedelta(hours=expire_hours)

    reg_token = RegistrationToken(
        token=token_str,
        expires_at=expires_at,
        max_uses=max_uses,
        use_count=0,
        created_by_user_id=current_user.id
    )
    db.session.add(reg_token)
    db.session.commit()

    log_action('生成注册邀请', f'生成有效时间为 {expire_hours} 小时、可用次数为 {max_uses} 次的注册链接')
    try:
        trigger_webhook_event(
            WebhookConfig.query.filter_by(is_enabled=True).all(), 'system',
            f'生成注册邀请链接',
            f'操作人：{current_user.username} | 页面：邀请链接 | 有效期：{expire_hours}小时 | 可用次数：{max_uses}',
            page_key='invites', user_name=current_user.username,
            operator_id=current_user.id
        )
    except Exception:
        pass
    flash(f'注册邀请链接已成功生成（可使用 {max_uses} 次）！', 'success')
    return redirect(url_for('admin_users'))

@app.route('/admin/invite_link/delete/<int:token_id>', methods=['POST'])
@login_required
def admin_delete_invite_link(token_id):
    if not current_user.is_admin:
        flash('权限不足！', 'danger')
        return redirect(url_for('index'))

    reg_token = RegistrationToken.query.get_or_404(token_id)
    token_val = reg_token.token[:8] + '...'
    db.session.delete(reg_token)
    db.session.commit()

    log_action('删除注册邀请链接', f'管理员删除了邀请链接前缀为 [{token_val}] 的链接')
    try:
        trigger_webhook_event(
            WebhookConfig.query.filter_by(is_enabled=True).all(), 'record_delete',
            f'删除注册邀请链接',
            f'操作人：{current_user.username} | 页面：邀请链接 | 前缀：{token_val}',
            page_key='invites', user_name=current_user.username,
            operator_id=current_user.id
        )
    except Exception:
        pass
    flash('邀请链接已成功删除！', 'success')
    return redirect(url_for('admin_users'))

@app.route('/admin/invite_link/batch_delete', methods=['POST'])
@login_required
def admin_batch_delete_invite_links():
    if not current_user.is_admin:
        flash('权限不足！', 'danger')
        return redirect(url_for('index'))

    token_ids = request.form.getlist('token_ids')
    if not token_ids:
        flash('请先勾选需要删除的注册邀请链接！', 'warning')
        return redirect(url_for('admin_users'))

    try:
        token_ids_int = [int(i) for i in token_ids]
        tokens = RegistrationToken.query.filter(RegistrationToken.id.in_(token_ids_int)).all()
        deleted_count = len(tokens)
        for t in tokens:
            db.session.delete(t)
        db.session.commit()
        log_action('批量删除注册邀请链接', f'管理员批量删除了 {deleted_count} 个注册邀请链接')
        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(), 'batch_delete',
                f'批量删除 {deleted_count} 个邀请链接',
                f'操作人：{current_user.username} | 页面：邀请链接 | 数量：{deleted_count}',
                page_key='invites', user_name=current_user.username,
                operator_id=current_user.id
            )
        except Exception:
            pass
        flash(f'成功批量删除 {deleted_count} 个注册邀请链接！', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'批量删除注册邀请链接失败: {str(e)}', 'danger')

    return redirect(url_for('admin_users'))

@app.route('/admin/log/delete/<int:log_id>', methods=['POST'])
@login_required
def admin_delete_log(log_id):
    if not current_user.is_admin:
        flash('权限不足！', 'danger')
        return redirect(url_for('index'))

    log_entry = OperationLog.query.get_or_404(log_id)
    action_info = f"{log_entry.username} - {log_entry.action}"
    db.session.delete(log_entry)
    db.session.commit()

    log_action('删除日志', f'管理员删除了操作日志ID #{log_id} ({action_info})')
    # V10: 补充审计日志删除推送
    try:
        trigger_webhook_event(
            WebhookConfig.query.filter_by(is_enabled=True).all(), 'security',
            f'管理员 {current_user.username} 删除了操作日志 #{log_id} ({action_info})',
            page_key='admin_logs', user_name=current_user.username, operator_id=current_user.id
        )
    except Exception:
        pass
    flash('日志记录已成功删除！', 'success')
    return redirect(url_for('admin_logs'))

@app.route('/admin/logs/clear', methods=['POST'])
@login_required
def admin_clear_logs():
    if not current_user.is_admin:
        flash('权限不足！', 'danger')
        return redirect(url_for('index'))

    deleted_count = OperationLog.query.delete()
    db.session.commit()

    log_action('清空日志', f'管理员清空了所有操作日志（共删除 {deleted_count} 条）')
    # V10: 补充审计日志清空推送
    try:
        trigger_webhook_event(
            WebhookConfig.query.filter_by(is_enabled=True).all(), 'clear',
            f'管理员 {current_user.username} 清空了所有操作审计日志（共 {deleted_count} 条）',
            page_key='admin_logs', user_name=current_user.username, operator_id=current_user.id
        )
    except Exception:
        pass
    flash(f'已成功清空所有审计日志（共 {deleted_count} 条）！', 'success')
    return redirect(url_for('admin_logs'))

# V10: 审计日志模块配置保存
@app.route('/admin/audit-log-config', methods=['POST'])
@login_required
def admin_save_audit_log_config():
    if not current_user.is_admin:
        flash('权限不足！', 'danger')
        return redirect(url_for('index'))
    modules = request.form.getlist('audit_modules')
    if not modules:
        SystemSetting.set_val('audit_log_modules', '[]')
    elif 'all' in modules:
        SystemSetting.set_val('audit_log_modules', '["all"]')
    else:
        SystemSetting.set_val('audit_log_modules', _json_module.dumps(modules))
    log_action('更新系统配置', f'管理员更新了审计日志记录模块配置: {", ".join(modules) if modules else "全部禁用"}')
    # V10.3: 补充审计日志配置推送
    try:
        trigger_webhook_event(
            WebhookConfig.query.filter_by(is_enabled=True).all(), 'system',
            f'{current_user.username} 更新审计日志配置',
            f'操作人：{current_user.username} | 页面：审计日志 | 操作：更新模块配置',
            page_key='admin_logs', user_name=current_user.username,
            operator_id=current_user.id
        )
    except Exception:
        pass
    flash('审计日志记录配置已保存成功！', 'success')
    return redirect(url_for('admin_logs'))

@app.route('/admin/settings/registration_mode', methods=['POST'])
@login_required
def admin_set_registration_mode():
    """管理员设置用户注册模式（自由注册 vs 仅邀请链接注册）"""
    if not current_user.is_admin:
        flash('权限不足！', 'danger')
        return redirect(url_for('index'))
    mode = request.form.get('registration_mode', 'invite_only').strip()
    if mode not in ('free', 'invite_only'):
        mode = 'invite_only'
    SystemSetting.set_val('registration_mode', mode)
    mode_name = '自由开放注册' if mode == 'free' else '仅邀请链接注册'
    log_action('修改注册策略', f"管理员将系统注册模式调整为: [{mode_name}]")
    # V10.3: 补充注册模式变更推送
    try:
        trigger_webhook_event(
            WebhookConfig.query.filter_by(is_enabled=True).all(), 'system',
            f'{current_user.username} 修改注册模式',
            f'操作人：{current_user.username} | 页面：用户管理 | 操作：修改注册模式 | 模式：{mode_name}',
            page_key='admin_users', user_name=current_user.username,
            operator_id=current_user.id
        )
    except Exception:
        pass
    flash(f'注册模式已成功调整为：【{mode_name}】！', 'success')
    return redirect(url_for('admin_users'))


@app.route('/admin/users/batch_permissions', methods=['POST'])
@login_required
def admin_batch_user_permissions():
    """管理员批量配置某一组用户的菜单权限与各菜单独立数据权限"""
    if not current_user.is_admin:
        flash('权限不足！', 'danger')
        return redirect(url_for('index'))

    user_ids = request.form.getlist('user_ids') or request.form.getlist('user_ids[]')
    if not user_ids:
        flash('请勾选需要批量配置权限的用户！', 'warning')
        return redirect(url_for('admin_users'))

    menus_list = request.form.getlist('allowed_menus') or request.form.getlist('allowed_menus[]')
    allowed_menus_str = ",".join(menus_list)

    ALL_MENUS = ['ledger', 'banquets', 'reconciliation', 'reminders', 'recycle_bin', 'backups']
    menu_perms = {}
    for m in ALL_MENUS:
        val = request.form.get(f'menu_perm_{m}')
        if val is None:
            val = request.form.get(f'menu_perms[{m}]')
        if val is not None and str(val).isdigit():
            menu_perms[m] = int(val)
        else:
            legacy_p = request.form.get('perm_level', '0')
            menu_perms[m] = int(legacy_p) if str(legacy_p).isdigit() else 0

    # V3: 批量 AI 助手授权
    ai_auth = request.form.get('ai_authorized', '')
    ai_auth_val = (ai_auth == '1' or ai_auth == 'on')

    # V6: 批量定时任务授权
    task_auth = request.form.get('scheduled_task_authorized', '')
    task_auth_val = (task_auth == '1' or task_auth == 'on')

    count = 0
    for uid_str in user_ids:
        try:
            uid = int(uid_str)
            u = db.session.get(User, uid)
            if u and not u.is_admin and u.id != current_user.id:
                u.allowed_menus = allowed_menus_str
                u.set_menu_permissions(menu_perms)
                ledger_p = menu_perms.get('ledger', 0)
                u.can_view_others = (ledger_p >= 1)
                u.can_edit_others = (ledger_p >= 2)
                u.can_delete_others = (ledger_p >= 3)
                # V3: 批量 AI 授权
                u.ai_authorized = ai_auth_val
                # V6: 批量定时任务授权
                u.scheduled_task_authorized = task_auth_val
                count += 1
        except Exception:
            continue

    if count > 0:
        db.session.commit()
        log_action('批量配置权限', f"管理员批量更新了 {count} 位用户的菜单与各菜单独立数据权限")
        # V10.3: 补充批量配置权限推送
        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(), 'update',
                f'{current_user.username} 批量配置 {count} 位用户权限',
                f'操作人：{current_user.username} | 页面：用户管理 | 操作：批量配置权限 | 数量：{count}',
                page_key='admin_users', user_name=current_user.username,
                operator_id=current_user.id
            )
        except Exception:
            pass
        flash(f'成功批量更新了 {count} 位用户的菜单访问与各菜单独立数据权限！', 'success')
    else:
        flash('未找到符合批量更新条件的普通用户！', 'warning')

    return redirect(url_for('admin_users'))

@app.route('/admin/user/permissions/<int:user_id>', methods=['POST'])
@login_required
def admin_update_user_permissions(user_id):
    if not current_user.is_admin:
        flash('权限不足！', 'danger')
        return redirect(url_for('index'))

    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash('无需为当前管理员账号设置数据权限！', 'warning')
        return redirect(url_for('admin_users'))

    # 1. 菜单权限更新
    menus_list = request.form.getlist('allowed_menus') or request.form.getlist('allowed_menus[]')
    if not menus_list:
        menus_raw = request.form.get('allowed_menus', '').strip()
        menus_list = [m.strip() for m in menus_raw.split(',') if m.strip()]
    user.allowed_menus = ",".join(menus_list)

    # 2. 各菜单独立数据权限更新
    ALL_MENUS = ['ledger', 'banquets', 'reconciliation', 'reminders', 'recycle_bin', 'backups']
    MENU_NAMES = {
        'ledger': '礼金账本',
        'banquets': '专属宴席',
        'reconciliation': '人情对账',
        'reminders': '纪念日备忘',
        'recycle_bin': '回收站',
        'backups': 'WebDAV备份'
    }
    PERM_LABELS = {
        0: '仅自身',
        1: '自身全权+查他人',
        2: '查改他人',
        3: '查改删他人'
    }

    menu_perms = {}
    for m in ALL_MENUS:
        val = request.form.get(f'menu_perm_{m}')
        if val is None:
            val = request.form.get(f'menu_perms[{m}]')
        if val is not None and str(val).isdigit():
            menu_perms[m] = int(val)
        else:
            legacy_p = request.form.get('perm_level')
            if legacy_p is not None and str(legacy_p).isdigit():
                menu_perms[m] = int(legacy_p)
            else:
                menu_perms[m] = 0

    user.set_menu_permissions(menu_perms)
    ledger_p = menu_perms.get('ledger', 0)
    user.can_view_others = (ledger_p >= 1)
    user.can_edit_others = (ledger_p >= 2)
    user.can_delete_others = (ledger_p >= 3)

    # 3. 备份功能授权
    backup_auth = request.form.get('backup_authorized', '')
    user.backup_authorized = (backup_auth == '1' or backup_auth == 'on')

    # V6: 定时任务功能授权
    task_auth = request.form.get('scheduled_task_authorized', '')
    user.scheduled_task_authorized = (task_auth == '1' or task_auth == 'on')

    # V3: AI 助手授权
    ai_auth = request.form.get('ai_authorized', '')
    user.ai_authorized = (ai_auth == '1' or ai_auth == 'on')

    db.session.commit()

    summary_parts = []
    for m in ALL_MENUS:
        p_val = menu_perms.get(m, 0)
        summary_parts.append(f"{MENU_NAMES.get(m, m)}:{PERM_LABELS.get(p_val, '仅自身')}")
    perm_summary = "，".join(summary_parts)

    log_action('修改用户权限', f'管理员修改了用户 [{user.username}] 的各菜单独立数据权限: {perm_summary}')
    try:
        trigger_webhook_event(
            WebhookConfig.query.filter_by(is_enabled=True).all(), 'status_change',
            f'修改用户权限 [{user.username}]',
            f'操作人：{current_user.username} | 页面：用户管理 | 用户：{user.username} | 权限：{perm_summary}',
            page_key='admin_users', user_name=current_user.username,
            operator_id=current_user.id
        )
    except Exception:
        pass
    flash(f'用户 [{user.username}] 的各菜单独立数据权限已更新：{perm_summary}', 'success')
    return redirect(url_for('admin_users'))


@app.route('/admin/user/reset_security/<int:user_id>', methods=['POST'])
@login_required
def admin_reset_user_security(user_id):
    if not current_user.is_admin:
        flash('权限不足！', 'danger')
        return redirect(url_for('index'))

    user = User.query.get_or_404(user_id)
    q1 = request.form.get('security_question_1', '').strip() or request.form.get('security_question', '').strip()
    a1 = request.form.get('security_answer_1', '').strip() or request.form.get('security_answer', '').strip()
    q2 = request.form.get('security_question_2', '').strip()
    a2 = request.form.get('security_answer_2', '').strip()

    # 校验：密保问题 2 与答案 2 必须成对出现（两个都填或两个都空）
    if (q2 and not a2) or (a2 and not q2):
        flash('新密保问题 2 与答案 2 必须成对填写！', 'warning')
        return redirect(url_for('admin_users'))
    # 校验：两个新密保问题不能相同
    if q1 and q2 and q1 == q2:
        flash('两个新密保问题不能相同，请输入不同的问题！', 'warning')
        return redirect(url_for('admin_users'))

    if user.is_admin:
        # 管理员账号重置密保必须先验证旧密码或者旧密保问题
        old_pwd = request.form.get('old_password', '').strip()
        old_ans1 = request.form.get('old_security_answer_1', '').strip() or request.form.get('old_security_answer', '').strip()
        old_ans2 = request.form.get('old_security_answer_2', '').strip()

        verified = False
        if old_pwd and user.check_password(old_pwd):
            verified = True
        elif old_ans1 and (user.check_any_security_answer(old_ans1) or user.check_security_answers(old_ans1, old_ans2)):
            verified = True

        if not verified:
            flash(f'重置管理员 [{user.username}] 密保失败：必须先验证原密码或原密保答案！', 'danger')
            return redirect(url_for('admin_users'))

    if q1 and a1:
        user.set_security_answers(q1, a1, q2, a2)
        # 管理员重置密保时清空找回密码风控计数
        FORGOT_SECURITY_FAIL_COUNTS.pop(user.username, None)
        FORGOT_SECURITY_LOCK_UNTILS.pop(user.username, None)
        db.session.commit()
        log_action('重置密保问题', f'管理员重置了用户 [{user.username}] 的密保问题与答案')
        # V10.3: 补充重置密保推送
        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(), 'security',
                f'{current_user.username} 重置用户密保',
                f'操作人：{current_user.username} | 页面：用户管理 | 用户：{user.username} | 操作：重置密保问题',
                page_key='admin_users', user_name=current_user.username,
                operator_id=current_user.id
            )
        except Exception:
            pass
        flash(f'用户 [{user.username}] 的密保问题已重置成功！新问题 1：{q1}' + (f'；新问题 2：{q2}' if q2 else '；密保 2 保留原设置'), 'success')
    else:
        flash('密保问题和密保答案均不能为空！', 'warning')

    return redirect(url_for('admin_users'))

@app.route('/admin/user/toggle_status/<int:user_id>', methods=['POST'])
@login_required
def admin_toggle_user_status(user_id):
    if not current_user.is_admin:
        flash('权限不足！', 'danger')
        return redirect(url_for('index'))

    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash('无法更改当前登录的管理员账号状态！', 'danger')
        return redirect(url_for('admin_users'))

    user.is_active = not user.is_active
    if not user.is_active:
        user.session_token = None  # 禁用时清空 session_token 强行踢下线
    else:
        # 解锁/启用账号时清除所有失败计数与锁定状态
        LOGIN_FAIL_COUNTS.pop(user.username, None)
        LOGIN_LOCK_UNTILS.pop(user.username, None)
        FORGOT_SECURITY_FAIL_COUNTS.pop(user.username, None)
        FORGOT_SECURITY_LOCK_UNTILS.pop(user.username, None)

    db.session.commit()

    status_str = "启用" if user.is_active else "禁用"
    log_action('修改账号状态', f'管理员{status_str}了用户账号 [{user.username}]')
    try:
        trigger_webhook_event(
            WebhookConfig.query.filter_by(is_enabled=True).all(), 'status_change',
            f'{status_str}用户 [{user.username}]',
            f'操作人：{current_user.username} | 页面：用户管理 | 用户：{user.username} | 状态：{status_str}',
            page_key='admin_users', user_name=current_user.username,
            operator_id=current_user.id
        )
    except Exception:
        pass
    flash(f'用户 [{user.username}] 已成功{status_str}！', 'success')
    return redirect(url_for('admin_users'))

@app.route('/admin/user/reset_pass/<int:user_id>', methods=['POST'])
@login_required
def admin_reset_user_pass(user_id):
    if not current_user.is_admin:
        flash('权限不足！', 'danger')
        return redirect(url_for('index'))

    user = User.query.get_or_404(user_id)
    new_password = request.form.get('new_password', '').strip()
    confirm_password = request.form.get('confirm_password', '').strip()

    if not new_password:
        flash('新密码不能为空！', 'warning')
        return redirect(url_for('admin_users'))

    if new_password != confirm_password:
        flash('两次输入的密码不一致，重置失败！', 'danger')
        return redirect(url_for('admin_users'))

    if user.is_admin:
        # 管理员账号重置密码必须先验证旧密码或者旧密保问题
        old_pwd = request.form.get('old_password', '').strip()
        old_ans1 = request.form.get('old_security_answer_1', '').strip() or request.form.get('old_security_answer', '').strip()
        old_ans2 = request.form.get('old_security_answer_2', '').strip()

        verified = False
        if old_pwd and user.check_password(old_pwd):
            verified = True
        elif old_ans1 and user.check_security_answers(old_ans1, old_ans2):
            verified = True

        if not verified:
            flash(f'重置管理员 [{user.username}] 密码失败：必须先验证原密码或原密保答案！', 'danger')
            return redirect(url_for('admin_users'))

    user.set_password(new_password)
    # 管理员重置密码时清空登录风控计数
    LOGIN_FAIL_COUNTS.pop(user.username, None)
    LOGIN_LOCK_UNTILS.pop(user.username, None)
    db.session.commit()
    log_action('重置用户密码', f'管理员重置了用户 [{user.username}] 的密码')
    try:
        trigger_webhook_event(
            WebhookConfig.query.filter_by(is_enabled=True).all(), 'security',
            f'重置用户密码 [{user.username}]',
            f'操作人：{current_user.username} | 页面：用户管理 | 用户：{user.username}',
            page_key='admin_users', user_name=current_user.username,
            operator_id=current_user.id
        )
    except Exception:
        pass
    flash(f'用户 [{user.username}] 的密码已重置成功！', 'success')

    return redirect(url_for('admin_users'))

@app.route('/admin/user/<int:user_id>/credentials', methods=['GET', 'POST'])
@login_required
def admin_user_credentials(user_id):
    """管理员查看用户安全凭证（AES-256-GCM 解密明文密码及密保问答；管理员账号需先验证旧密码或旧密保）"""
    if not current_user.is_admin:
        return jsonify({'code': 403, 'message': '仅超级管理员有权查看安全凭证！'}), 403

    user = User.query.get_or_404(user_id)
    if user.is_admin:
        # 管理员账号可以查看当前密码或者密保，但必须先验证旧密码或者旧密保问题后方可查看
        req_json = request.get_json(silent=True) or {}
        verify_pwd = (request.args.get('verify_password') or request.form.get('verify_password') or req_json.get('verify_password') or '').strip()
        verify_ans1 = (request.args.get('verify_security_answer') or request.form.get('verify_security_answer') or req_json.get('verify_security_answer') or '').strip()
        verify_ans2 = (request.args.get('verify_security_answer_2') or request.form.get('verify_security_answer_2') or req_json.get('verify_security_answer_2') or '').strip()

        verified = False
        if verify_pwd and user.check_password(verify_pwd):
            verified = True
        elif verify_ans1 and (user.check_any_security_answer(verify_ans1) or user.check_security_answers(verify_ans1, verify_ans2)):
            verified = True

        if not verified:
            # V10.7 修复：未验证通过时 HTTP 状态码改为 200（JSON code 仍为 401），
            # 避免 base.html 全局 Fetch 拦截器把业务性 401 误判为登录失效而强制跳转首页
            return jsonify({
                'code': 401,
                'need_verify': True,
                'is_admin': True,
                'username': user.username,
                'q1': user.security_question_1 or user.security_question or '未设置',
                'q2': user.security_question_2 or '',
                'message': '管理员账号的安全凭证受保护，必须先验证当前账号的原密码或原密保答案！'
            }), 200

    plain_password = user.get_decrypted_password()
    security_qa = user.get_decrypted_security_answers()

    log_action('查看安全凭证', f'超级管理员查看了用户 [{user.username}] 的安全凭证详情')
    try:
        trigger_webhook_event(
            WebhookConfig.query.filter_by(is_enabled=True).all(), 'security',
            f'查看安全凭证',
            f'操作人：{current_user.username} | 页面：用户管理 | 目标用户：{user.username}',
            page_key='admin_users', user_name=current_user.username,
            operator_id=current_user.id
        )
    except Exception:
        pass
    return jsonify({
        'code': 200,
        'message': 'success',
        'data': {
            'id': user.id,
            'username': user.username,
            'is_admin': user.is_admin,
            'has_plain_password': bool(plain_password),
            'plain_password': plain_password or '（该账号创建于可逆加密启用前，修改/重置密码后可查看明文）',
            'security_qa': security_qa
        }
    })

@app.route('/admin/user/delete/<int:user_id>', methods=['POST'])
@login_required
def admin_delete_user(user_id):
    if not current_user.is_admin:
        flash('权限不足！', 'danger')
        return redirect(url_for('index'))

    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash('无法删除当前的管理员账号！', 'danger')
        return redirect(url_for('admin_users'))

    deleted_username = user.username
    db.session.delete(user)
    db.session.commit()
    log_action('删除用户', f'管理员删除了用户账号 [{deleted_username}]')
    try:
        trigger_webhook_event(
            WebhookConfig.query.filter_by(is_enabled=True).all(), 'record_delete',
            f'删除用户 [{deleted_username}]',
            f'操作人：{current_user.username} | 页面：用户管理 | 用户：{deleted_username}',
            page_key='admin_users', user_name=current_user.username,
            operator_id=current_user.id
        )
    except Exception:
        pass
    flash(f'用户 [{deleted_username}] 及其关联数据已成功删除！', 'success')
    return redirect(url_for('admin_users'))

@app.route('/admin/users/batch_delete', methods=['POST'])
@login_required
def admin_batch_delete_users():
    if not current_user.is_admin:
        flash('权限不足！', 'danger')
        return redirect(url_for('index'))

    user_ids = request.form.getlist('user_ids')
    if not user_ids:
        flash('请先勾选需要批量删除的用户！', 'warning')
        return redirect(url_for('admin_users'))

    try:
        user_ids_int = [int(i) for i in user_ids]
        # 严格过滤：禁止删除当前登录用户，且禁止删除管理员账号
        eligible_users = User.query.filter(
            User.id.in_(user_ids_int),
            User.id != current_user.id,
            User.is_admin.is_(False)
        ).all()

        if not eligible_users:
            flash('没有符合删除条件的普通用户（已保护管理员账号及当前登录账号）！', 'warning')
            return redirect(url_for('admin_users'))

        deleted_usernames = [u.username for u in eligible_users]
        deleted_count = len(eligible_users)

        for u in eligible_users:
            db.session.delete(u)
        db.session.commit()

        log_action('批量删除用户', f'管理员批量删除了 {deleted_count} 个用户: {", ".join(deleted_usernames)}')
        # V10.3: 补充批量删除用户推送
        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(), 'batch_delete',
                f'{current_user.username} 批量删除 {deleted_count} 个用户',
                f'操作人：{current_user.username} | 页面：用户管理 | 用户：{", ".join(deleted_usernames)} | 数量：{deleted_count}',
                page_key='admin_users', user_name=current_user.username,
                operator_id=current_user.id
            )
        except Exception:
            pass
        flash(f'成功批量删除 {deleted_count} 个用户及其关联数据：{", ".join(deleted_usernames)}', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'批量删除用户失败: {str(e)}', 'danger')

    return redirect(url_for('admin_users'))
import csv
import io
from flask import Response

@app.route('/export/csv')
@login_required
def export_csv():
    query = get_accessible_records_query(current_user)

    scope = request.args.get('scope', '').strip()  # 'all', 'filtered', 'page', 'selected'
    ids_param = request.args.get('ids', '').strip()

    if ids_param:
        id_list = [int(x) for x in ids_param.split(',') if x.strip().isdigit()]
        if id_list:
            query = query.filter(GiftRecord.id.in_(id_list))
        records = query.order_by(GiftRecord.created_at.desc()).all()
    elif scope == 'all':
        # 导出系统内全部礼金数据（忽略任何关键词及筛选条件）
        records = query.order_by(GiftRecord.created_at.desc()).all()
    else:
        # 默认或 scope in ('filtered', 'page')：严格继承当前页面的筛选与查询条件
        query_str = request.args.get('search', '').strip() or request.args.get('q', '').strip()
        reason_filter = request.args.get('reason', '').strip()
        type_filter = request.args.get('type', '').strip()
        sort_by = request.args.get('sort', 'created_at_desc').strip()

        if query_str:
            search_pattern = f"%{query_str}%"
            num_val = None
            try:
                num_val = float(query_str)
            except ValueError:
                parsed = cn2num(query_str)
                if parsed > 0:
                    num_val = parsed
            except Exception:
                pass

            or_conditions = [
                GiftRecord.name.ilike(search_pattern),
                GiftRecord.address.ilike(search_pattern),
                GiftRecord.phone.ilike(search_pattern),
                GiftRecord.event_reason.ilike(search_pattern),
                GiftRecord.notes.ilike(search_pattern),
                db.cast(GiftRecord.amount, db.String).ilike(search_pattern),
                db.cast(GiftRecord.age, db.String).ilike(search_pattern)
            ]
            if num_val is not None:
                or_conditions.append(GiftRecord.amount == num_val)

            query = query.filter(db.or_(*or_conditions))

        if reason_filter:
            query = query.filter(GiftRecord.event_reason == reason_filter)

        if type_filter in ('receive', 'send'):
            query = query.filter(GiftRecord.record_type == type_filter)

        if sort_by == 'amount_desc':
            query = query.order_by(GiftRecord.amount.desc())
        elif sort_by == 'amount_asc':
            query = query.order_by(GiftRecord.amount.asc())
        elif sort_by in ['created_at_asc', 'oldest']:
            query = query.order_by(GiftRecord.created_at.asc())
        else:
            query = query.order_by(GiftRecord.created_at.desc())

        if scope == 'page':
            page = request.args.get('page', 1, type=int)
            per_page = request.args.get('per_page', 10, type=int)
            if per_page not in [5, 10, 20, 50, 100]:
                per_page = 10
            pagination = query.paginate(page=page, per_page=per_page, error_out=False)
            records = pagination.items
        else:
            records = query.all()

    output = io.StringIO()
    output.write('\ufeff')
    writer = csv.writer(output)
    writer.writerow(['ID', '客人姓名', '往来类型', '年龄', '联系电话', '礼金金额(元)', '大写金额', '办席原因', '归属专属宴席', '联系地址', '备注说明', '登记时间', '录入用户'])

    for r in records:
        r_type_label = '送礼' if getattr(r, 'record_type', 'receive') in ('send', 'give') else '收礼'
        banquet_title = r.banquet.title if (r.banquet and not r.banquet.deleted_at) else ''
        writer.writerow([
            r.id,
            r.name,
            r_type_label,
            r.age if r.age else '',
            r.phone if r.phone else '',
            f"{r.amount:.2f}",
            num2cn(r.amount),
            r.event_reason,
            banquet_title,
            r.address if r.address else '',
            r.notes if r.notes else '',
            r.created_at.strftime('%Y-%m-%d %H:%M') if r.created_at else '',
            r.owner.username if r.owner else ''
        ])

    response = Response(output.getvalue(), mimetype='text/csv; charset=utf-8')
    response.headers['Content-Disposition'] = 'attachment; filename=gift_records.csv'
    log_action('导出数据', f'用户导出了 {len(records)} 条礼金记录 CSV 文件')
    try:
        trigger_webhook_event(
            WebhookConfig.query.filter_by(is_enabled=True).all(), 'security',
            f'导出礼金数据',
            f'操作人：{current_user.username} | 页面：礼金账本 | 导出记录数：{len(records)}',
            page_key='ledger', user_name=current_user.username,
            operator_id=current_user.id
        )
    except Exception:
        pass
    return response


@app.route('/import/template')
@login_required
def download_import_template():
    output = io.StringIO()
    output.write('\ufeff')
    writer = csv.writer(output)
    writer.writerow(['客人姓名(必填)', '往来类型(选填，收礼/随礼，默认收礼)', '年龄(选填)', '联系电话(选填)', '礼金金额(元)(必填)', '办席原因(必填)', '联系地址(选填)', '备注说明(选填)'])
    writer.writerow(['张三', '收礼', '30', '13800138000', '500', '婚礼', '北京市朝阳区', '新婚大吉'])
    writer.writerow(['李四', '随礼', '', '13900139000', '1000', '满月酒', '上海市浦东新区', '贺百天之喜'])

    response = Response(output.getvalue(), mimetype='text/csv')
    response.headers['Content-Disposition'] = 'attachment; filename=gift_records_template.csv'
    log_action('下载模板', '用户下载了批量导入样例模版 CSV 文件')
    return response


@app.route('/import/csv', methods=['POST'])
@login_required
def import_csv():
    # V10.4: 级别1可正常导入自身记录，移除 == 1 拦截
    file = request.files.get('file')
    if not file or file.filename == '':
        flash('请选择要导入的 CSV 文件！', 'danger')
        return redirect(url_for('index'))

    if not file.filename.endswith('.csv'):
        flash('只支持导入 CSV 格式的文件！', 'danger')
        return redirect(url_for('index'))

    try:
        raw_bytes = file.stream.read()
        content = None
        for enc in ['utf-8-sig', 'utf-8', 'gb18030', 'gbk', 'gb2312']:
            try:
                content = raw_bytes.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        
        if content is None:
            content = raw_bytes.decode('utf-8', errors='replace')

        csv_reader = csv.reader(io.StringIO(content))

        success_count = 0
        skip_count = 0
        header_map = {}

        for row in csv_reader:
            if not row or not any(row):
                skip_count += 1
                continue

            headers = [c.strip() for c in row]
            if any(h in headers for h in ['姓名', '客人姓名', '礼金金额', '礼金金额(元)', 'ID', '送礼人', '事由', '办席原因', '办事原因', '原因', '往来类型', '类型']) or any(any(k in h for k in ['姓名', '金额', '事由', '原因', '礼金', '客人']) for h in headers):
                col_map = {}
                for idx, col in enumerate(headers):
                    col_clean = col.replace('(元)', '').replace('(必填)', '').replace('(选填)', '').replace('*', '').strip()
                    if any(k in col_clean for k in ['姓名', '客人', '送礼人']):
                        col_map['name'] = idx
                    elif any(k in col_clean for k in ['往来', '类型', '收送']):
                        col_map['record_type'] = idx
                    elif '年龄' in col_clean:
                        col_map['age'] = idx
                    elif any(k in col_clean for k in ['地址', '住址', '联系地址']):
                        col_map['address'] = idx
                    elif any(k in col_clean for k in ['电话', '手机', '联系电话']):
                        col_map['phone'] = idx
                    elif any(k in col_clean for k in ['金额', '礼金', '钱']):
                        col_map['amount'] = idx
                    elif any(k in col_clean for k in ['事由', '原因', '办席', '来意', '办事']):
                        col_map['event_reason'] = idx
                    elif any(k in col_clean for k in ['备注', '说明']):
                        col_map['notes'] = idx
                header_map = col_map
                continue

            rec_type_val = 'receive'
            if header_map:
                name = row[header_map['name']].strip() if 'name' in header_map and len(row) > header_map['name'] else ''
                age_str = row[header_map['age']].strip() if 'age' in header_map and len(row) > header_map['age'] else ''
                address = row[header_map['address']].strip() if 'address' in header_map and len(row) > header_map['address'] else ''
                phone = row[header_map['phone']].strip() if 'phone' in header_map and len(row) > header_map['phone'] else ''
                amount_str = row[header_map['amount']].strip() if 'amount' in header_map and len(row) > header_map['amount'] else '0'
                raw_reason = row[header_map['event_reason']].strip() if 'event_reason' in header_map and len(row) > header_map['event_reason'] else ''
                event_reason = raw_reason if raw_reason else '其它'
                notes = row[header_map['notes']].strip() if 'notes' in header_map and len(row) > header_map['notes'] else ''
                if 'record_type' in header_map and len(row) > header_map['record_type']:
                    raw_type = row[header_map['record_type']].strip()
                    if any(w in raw_type for w in ['随', '送', '出', '支出', 'send', 'give']):
                        rec_type_val = 'send'
                    else:
                        rec_type_val = 'receive'
            else:
                name = row[0].strip() if len(row) > 0 else ''
                if not name or name in ['ID', '客人姓名', '姓名']:
                    continue
                if name.isdigit() and len(row) > 1:
                    name = row[1].strip()
                    age_str = row[2].strip() if len(row) > 2 else ''
                    phone = row[3].strip() if len(row) > 3 else ''
                    amount_str = row[4].strip() if len(row) > 4 else '0'
                    event_reason = row[5].strip() if len(row) > 5 and row[5].strip() else '其它'
                    address = row[6].strip() if len(row) > 6 else ''
                    notes = row[7].strip() if len(row) > 7 else ''
                else:
                    age_str = row[1].strip() if len(row) > 1 else ''
                    address = row[2].strip() if len(row) > 2 else ''
                    phone = row[3].strip() if len(row) > 3 else ''
                    amount_str = row[4].strip() if len(row) > 4 else '0'
                    event_reason = row[5].strip() if len(row) > 5 and row[5].strip() else '其它'
                    notes = row[6].strip() if len(row) > 6 else ''

            if not name:
                skip_count += 1
                continue

            try:
                age = int(age_str) if age_str.isdigit() else None
            except ValueError:
                age = None

            try:
                amount = cn2num(amount_str) if amount_str else 0.0
            except Exception:
                amount = 0.0

            record = GiftRecord(
                name=name,
                age=age,
                phone=phone,
                amount=amount,
                event_reason=event_reason,
                record_type=rec_type_val,
                address=address,
                notes=notes,
                user_id=current_user.id
            )
            db.session.add(record)
            success_count += 1

        db.session.commit()
        log_action('导入数据', f'成功导入 {success_count} 条礼金记录（忽略 {skip_count} 条）')
        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(), 'record_create',
                f'{current_user.username} 导入 {success_count} 条记录',
                f'操作人：{current_user.username} | 页面：礼金账本 | 导入成功：{success_count} 条 | 忽略：{skip_count} 条',
                page_key='ledger', user_name=current_user.username,
                operator_id=current_user.id
            )
        except Exception:
            pass
        flash(f'批量导入完成！成功导入 {success_count} 条记录' + (f'，忽略 {skip_count} 条无效数据。' if skip_count > 0 else '。'), 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'文件解析或导入失败：{str(e)}', 'danger')

    return redirect(url_for('index'))



register_routes_ext(
    app,
    log_operation=log_action,
    get_accessible_records_query=get_accessible_records_query,
    get_accessible_banquets_query=get_accessible_banquets_query,
    get_accessible_reminders_query=get_accessible_reminders_query,
    can_user_view_entity=can_user_view_entity,
    can_user_edit_entity=can_user_edit_entity,
    can_user_delete_entity=can_user_delete_entity,
    clear_login_risk=clear_login_risk,
    clear_forgot_security_risk=clear_forgot_security_risk
)

# 注册 AI 助手路由
from routes_ai import register_ai_routes
register_ai_routes(app, log_action=log_action)


if __name__ == '__main__':
    import argparse
    import os

    parser = argparse.ArgumentParser(description='礼金记账系统 Linux/云服务器启动脚本')
    parser.add_argument('--host', type=str, default=os.environ.get('HOST', '0.0.0.0'), help='监听 IP 地址 (默认: 0.0.0.0)')
    parser.add_argument('--port', type=int, default=int(os.environ.get('PORT', 11443)), help='服务端口 (默认: 11443)')
    args = parser.parse_args()

    # 管理员账号初始化统一由 init_database() 负责（模块级 L912 已自动执行）
    # 此处不再重复管理员创建/同步逻辑，避免双管理员与用户名冲突隐患

    app.run(host=args.host, port=args.port, debug=False)
