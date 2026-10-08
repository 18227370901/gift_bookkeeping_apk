# -*- coding: utf-8 -*-
import os
import json
import base64
import hashlib
from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
# V10.10.16 性能优化：cryptography AESGCM 含 Rust 扩展 ~12-18MB，
# 延迟至首次加密/解密时才导入
_AESGCM = None

def _get_aesgcm():
    """首次调用时延迟导入 AESGCM，后续从 sys.modules 缓存获取"""
    global _AESGCM
    if _AESGCM is not None:
        return _AESGCM
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM as _cls
    _AESGCM = _cls
    return _AESGCM

DEFAULT_SECRET_KEY = 'gift-bookkeeping-secret-key-2026-prod-secure'

def get_aes_key(secret_key=None):
    if not secret_key:
        try:
            from flask import current_app
            secret_key = current_app.config.get('AES_SECRET_KEY') or current_app.config.get('SECRET_KEY')
        except Exception:
            secret_key = None
        if not secret_key:
            secret_key = os.environ.get('AES_SECRET_KEY') or os.environ.get('SECRET_KEY') or DEFAULT_SECRET_KEY
    if not isinstance(secret_key, str):
        secret_key = str(secret_key or DEFAULT_SECRET_KEY)
    return hashlib.sha256(secret_key.encode('utf-8')).digest()

def encrypt_credential(plain_text, secret_key=None):
    """使用 AES-256-GCM 对称算法对凭证进行加密"""
    if plain_text is None:
        return None
    plain_str = str(plain_text)
    if plain_str == '':
        return ''
    try:
        key = get_aes_key(secret_key)
        AESGCM = _get_aesgcm()
        aesgcm = AESGCM(key)
        nonce = os.urandom(12)  # 96-bit nonce
        cipher_bytes = aesgcm.encrypt(nonce, plain_str.encode('utf-8'), None)
        encrypted_raw = nonce + cipher_bytes
        return base64.b64encode(encrypted_raw).decode('utf-8')
    except Exception as e:
        print(f"[AES Encrypt Error] {e}")
        return None

def decrypt_credential(cipher_text, secret_key=None, fallback_plain=False):
    """使用 AES-256-GCM 对称算法对凭证进行解密，支持旧明文平滑回退"""
    if not cipher_text:
        return None
    try:
        AESGCM = _get_aesgcm()
        key = get_aes_key(secret_key)
        aesgcm = AESGCM(key)
        encrypted_raw = base64.b64decode(cipher_text.encode('utf-8'))
        if len(encrypted_raw) < 13:
            return cipher_text if fallback_plain else None
        nonce = encrypted_raw[:12]
        cipher_bytes = encrypted_raw[12:]
        decrypted_bytes = aesgcm.decrypt(nonce, cipher_bytes, None)
        return decrypted_bytes.decode('utf-8')
    except Exception:
        return cipher_text if fallback_plain else None
    try:
        AESGCM = _get_aesgcm()
        key = get_aes_key(secret_key)
        aesgcm = AESGCM(key)
        encrypted_raw = base64.b64decode(cipher_text.encode('utf-8'))
        if len(encrypted_raw) < 13:
            return None
        nonce = encrypted_raw[:12]
        cipher_bytes = encrypted_raw[12:]
        decrypted_bytes = aesgcm.decrypt(nonce, cipher_bytes, None)
    except Exception as e:
        print(f"[AES Decrypt Error] {e}")
        return None

db = SQLAlchemy()

class User(UserMixin, db.Model):
    __tablename__ = 'users'
    
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    encrypted_password = db.Column(db.String(512), nullable=True)  # AES-256 可逆凭证密文
    role = db.Column(db.String(20), default='user')  # 'admin' or 'user'
    is_admin = db.Column(db.Boolean, default=False)
    session_token = db.Column(db.String(64), nullable=True)
    
    # 旧单密保兼容字段
    security_question = db.Column(db.String(200), nullable=True)
    security_answer_hash = db.Column(db.String(256), nullable=True)
    encrypted_security_answer = db.Column(db.String(512), nullable=True)
    
    # 双密保字段（至少 2 个密保问题与答案）
    security_question_1 = db.Column(db.String(200), nullable=True)
    security_answer_hash_1 = db.Column(db.String(256), nullable=True)
    encrypted_security_answer_1 = db.Column(db.String(512), nullable=True)
    
    security_question_2 = db.Column(db.String(200), nullable=True)
    security_answer_hash_2 = db.Column(db.String(256), nullable=True)
    encrypted_security_answer_2 = db.Column(db.String(512), nullable=True)

    is_active = db.Column(db.Boolean, default=True)
    can_view_others = db.Column(db.Boolean, default=False)
    can_edit_others = db.Column(db.Boolean, default=False)
    can_delete_others = db.Column(db.Boolean, default=False)
    allowed_menus = db.Column(db.String(256), default='')  # 允许访问的菜单列表（如 ledger,banquets 等），新注册用户默认为空
    
    # --- AI 助手相关字段 ---
    # 旧版单配置（向后兼容）
    _ai_api_key = db.Column('ai_api_key', db.String(512), nullable=True)  # AES-256-GCM 密文存储
    ai_base_url = db.Column(db.String(255), nullable=True, default='')
    ai_model = db.Column(db.String(100), nullable=True, default='')
    # 多配置列表（JSON 数组，结构: [{"name","api_key","base_url","model","enabled"}]）
    ai_configs = db.Column(db.Text, default='[]')
    # AI 授权标记（管理员可授权普通用户使用 AI）
    ai_authorized = db.Column(db.Boolean, default=False)
    # 备份授权标记（管理员可授权普通用户使用备份功能）
    backup_authorized = db.Column(db.Boolean, default=False)
    # 定时任务授权标记（管理员可授权普通用户使用定时任务功能）
    scheduled_task_authorized = db.Column(db.Boolean, default=False)
    menu_permissions = db.Column(db.Text, default='{}')  # 各菜单独立数据权限配置 JSON (如 {'ledger':0,'banquets':1,'backups':0})
    created_at = db.Column(db.DateTime, default=datetime.now)
    
    records = db.relationship('GiftRecord', backref='owner', lazy=True, cascade='all, delete-orphan')
    banquets = db.relationship('Banquet', backref='owner', lazy=True, cascade='all, delete-orphan')
    reminders = db.relationship('AnniversaryReminder', backref='owner', lazy=True, cascade='all, delete-orphan')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
        self.encrypted_password = encrypt_credential(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def get_allowed_menus(self):
        if getattr(self, 'is_admin', False):
            return ['ledger', 'dashboard', 'family', 'banquets', 'reconciliation', 'reminders', 'recycle_bin', 'admin_users', 'admin_logs', 'admin_broadcasts', 'admin_webhooks', 'admin_backups']
        raw = getattr(self, 'allowed_menus', '') or ''
        return [m.strip() for m in raw.split(',') if m.strip()]

    def can_access_menu(self, menu_key):
        if getattr(self, 'is_admin', False):
            return True
        return menu_key in self.get_allowed_menus()

    def get_menu_permissions(self):
        """获取用户各菜单独立权限配置字典 { menu_key: perm_level_int }"""
        raw = getattr(self, 'menu_permissions', None)
        res = {}
        if raw:
            try:
                res = json.loads(raw)
            except Exception:
                res = {}
        ALL_MENUS = ['ledger', 'dashboard', 'family', 'banquets', 'reconciliation', 'reminders', 'recycle_bin', 'backups']
        final_perms = {}
        for m in ALL_MENUS:
            val = res.get(m)
            if val is not None:
                try:
                    final_perms[m] = int(val)
                except (ValueError, TypeError):
                    final_perms[m] = 0
            else:
                if m == 'ledger' and not raw:
                    if getattr(self, 'can_delete_others', False):
                        final_perms[m] = 3
                    elif getattr(self, 'can_edit_others', False):
                        final_perms[m] = 2
                    elif getattr(self, 'can_view_others', False):
                        final_perms[m] = 1
                    else:
                        final_perms[m] = 0
                else:
                    final_perms[m] = 0
        return final_perms

    def set_menu_permissions(self, perms_dict):
        """设置各菜单独立数据权限配置"""
        clean_perms = {}
        ALL_MENUS = ['ledger', 'dashboard', 'family', 'banquets', 'reconciliation', 'reminders', 'recycle_bin', 'backups']
        for m in ALL_MENUS:
            val = perms_dict.get(m, 0) if isinstance(perms_dict, dict) else 0
            try:
                clean_perms[m] = int(val)
            except (ValueError, TypeError):
                clean_perms[m] = 0
        self.menu_permissions = json.dumps(clean_perms, ensure_ascii=False)

    def get_menu_perm(self, menu_key):
        """获取指定菜单的数据权限级别 (0~3)，管理员始终返回 3"""
        if getattr(self, 'is_admin', False):
            return 3
        perms = self.get_menu_permissions()
        return perms.get(menu_key, 0)

    def can_view_others_for(self, menu_key):
        if getattr(self, 'is_admin', False):
            return True
        return self.get_menu_perm(menu_key) >= 1

    def can_edit_others_for(self, menu_key):
        if getattr(self, 'is_admin', False):
            return True
        return self.get_menu_perm(menu_key) >= 2

    def can_delete_others_for(self, menu_key):
        if getattr(self, 'is_admin', False):
            return True
        return self.get_menu_perm(menu_key) >= 3

    def get_decrypted_password(self):
        return decrypt_credential(self.encrypted_password)

    def set_security_answers(self, q1, a1, q2=None, a2=None):
        """设置双密保（同时兼容旧单密保字段）"""
        if q1 and a1:
            clean_a1 = a1.strip().lower()
            self.security_question_1 = q1.strip()
            self.security_answer_hash_1 = generate_password_hash(clean_a1)
            self.encrypted_security_answer_1 = encrypt_credential(clean_a1)
            # 兼容旧字段
            self.security_question = self.security_question_1
            self.security_answer_hash = self.security_answer_hash_1
            self.encrypted_security_answer = self.encrypted_security_answer_1

        if q2 and a2:
            clean_a2 = a2.strip().lower()
            self.security_question_2 = q2.strip()
            self.security_answer_hash_2 = generate_password_hash(clean_a2)
            self.encrypted_security_answer_2 = encrypt_credential(clean_a2)

    def check_security_answers(self, a1, a2=None):
        """校验密保答案：如果配置了第2个密保则要求两者均正确"""
        if not self.security_answer_hash_1 and not self.security_answer_hash:
            return False

        hash1 = self.security_answer_hash_1 or self.security_answer_hash
        if not a1 or not check_password_hash(hash1, a1.strip().lower()):
            return False

        if self.security_question_2 and self.security_answer_hash_2:
            if not a2 or not check_password_hash(self.security_answer_hash_2, a2.strip().lower()):
                return False

        return True

    def check_any_security_answer(self, ans):
        """校验任意一个密保答案（只要答对第1个或第2个密保答案中的任意一个即可通过）"""
        if not ans:
            return False
        clean_ans = ans.strip().lower()
        hash1 = self.security_answer_hash_1 or self.security_answer_hash
        if hash1 and check_password_hash(hash1, clean_ans):
            return True
        if self.security_answer_hash_2 and check_password_hash(self.security_answer_hash_2, clean_ans):
            return True
        return False

    def check_security_answer(self, answer):
        """兼容单密保校验调用"""
        return self.check_any_security_answer(answer)

    def set_security_answer(self, answer):
        """兼容单密保设置调用"""
        q = self.security_question_1 or self.security_question or '默认安全问题'
        self.set_security_answers(q, answer)

    def get_decrypted_security_answers(self):
        """获取解密后的明文密保问答（用于超级管理员查看凭证）"""
        ans1 = decrypt_credential(self.encrypted_security_answer_1 or self.encrypted_security_answer)
        ans2 = decrypt_credential(self.encrypted_security_answer_2)
        q1 = self.security_question_1 or self.security_question or '未设置'
        q2 = self.security_question_2 or '未设置'
        return {
            'q1': q1,
            'a1': ans1 or '无',
            'q2': q2,
            'a2': ans2 or '无'
        }

    # ==================== AI 助手相关方法 ====================

    @property
    def ai_api_key(self):
        """解密获取 AI API Key 明文"""
        if not self._ai_api_key:
            return ''
        return decrypt_credential(self._ai_api_key, fallback_plain=True) or ''

    @ai_api_key.setter
    def ai_api_key(self, value):
        """加密存储 AI API Key"""
        if not value or str(value).strip() == '':
            self._ai_api_key = None
        else:
            self._ai_api_key = encrypt_credential(str(value).strip())

    def get_ai_configs(self):
        """获取解密后的 AI 多配置列表"""
        raw = getattr(self, 'ai_configs', None) or '[]'
        try:
            configs = json.loads(raw)
        except Exception:
            configs = []
        # 解密每个配置的 api_key
        for cfg in configs:
            cipher_key = cfg.get('api_key', '')
            if cipher_key:
                try:
                    cfg['api_key'] = decrypt_credential(cipher_key, fallback_plain=True) or ''
                except Exception:
                    cfg['api_key'] = ''
        return configs

    def set_ai_configs(self, configs_list):
        """加密保存 AI 多配置列表（api_key 字段加密存储）"""
        clean = []
        for cfg in configs_list:
            if not isinstance(cfg, dict):
                continue
            api_key = str(cfg.get('api_key', '') or '').strip()
            if not api_key:
                continue  # 空 key 的配置跳过
            clean.append({
                'name': str(cfg.get('name', ''))[:50].strip() or f'配置{len(clean)+1}',
                'api_key': encrypt_credential(api_key) if api_key else '',
                'base_url': str(cfg.get('base_url', '') or '').strip(),
                'model': str(cfg.get('model', '') or '').strip(),
                'enabled': bool(cfg.get('enabled', True))
            })
        self.ai_configs = json.dumps(clean, ensure_ascii=False)
        # 同步旧版字段
        if clean:
            first_enabled = next((c for c in clean if c.get('enabled', True)), clean[0])
            self._ai_api_key = first_enabled.get('api_key', '')
            self.ai_base_url = first_enabled.get('base_url', '')
            self.ai_model = first_enabled.get('model', '')
        else:
            self._ai_api_key = None
            self.ai_base_url = ''
            self.ai_model = ''

    def can_use_ai(self):
        """检查用户是否有权使用 AI 助手"""
        if getattr(self, 'is_admin', False):
            return True
        return bool(getattr(self, 'ai_authorized', False))

    def can_use_backup(self):
        """检查用户是否有权使用备份功能"""
        if getattr(self, 'is_admin', False):
            return True
        return bool(getattr(self, 'backup_authorized', False))

    def can_use_scheduled_tasks(self):
        """检查用户是否有权使用定时任务功能"""
        if getattr(self, 'is_admin', False):
            return True
        return bool(getattr(self, 'scheduled_task_authorized', False))

    def can_view_others_backup(self):
        """检查用户是否可查看其他用户的备份数据（menu_permissions['backups'] >= 1）"""
        if getattr(self, 'is_admin', False):
            return True
        return self.get_menu_perm('backups') >= 1

    def can_edit_others_backup(self):
        """检查用户是否可修改/恢复其他用户的备份数据（menu_permissions['backups'] >= 2）"""
        if getattr(self, 'is_admin', False):
            return True
        return self.get_menu_perm('backups') >= 2

    def can_delete_others_backup(self):
        """检查用户是否可删除其他用户的备份数据（menu_permissions['backups'] >= 3）"""
        if getattr(self, 'is_admin', False):
            return True
        return self.get_menu_perm('backups') >= 3

    # V10.2 新增：定时任务操作权限方法
    def can_view_others_scheduled_tasks(self):
        """检查用户是否可查看其他用户的定时任务"""
        if getattr(self, 'is_admin', False):
            return True
        cfg = BackupConfig.get_config(None)
        return bool(getattr(cfg, 'allow_view_others_tasks', False)) if cfg else False

    def can_edit_others_scheduled_tasks(self):
        """检查用户是否可编辑其他用户的定时任务"""
        if getattr(self, 'is_admin', False):
            return True
        cfg = BackupConfig.get_config(None)
        return bool(getattr(cfg, 'allow_edit_others_tasks', False)) if cfg else False

    def can_delete_others_scheduled_tasks(self):
        """检查用户是否可删除其他用户的定时任务"""
        if getattr(self, 'is_admin', False):
            return True
        cfg = BackupConfig.get_config(None)
        return bool(getattr(cfg, 'allow_delete_others_tasks', False)) if cfg else False


class ChatSession(db.Model):
    """AI 助手聊天会话"""
    __tablename__ = 'chat_sessions'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    title = db.Column(db.String(100), default='新会话')
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)

    messages = db.relationship('ChatMessage', backref='session', lazy=True, cascade='all, delete-orphan')
    user = db.relationship('User', backref=db.backref('chat_sessions', lazy=True, cascade='all, delete-orphan'))


class ChatMessage(db.Model):
    """AI 助手聊天消息"""
    __tablename__ = 'chat_messages'

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey('chat_sessions.id', ondelete='CASCADE'), nullable=False)
    role = db.Column(db.String(10), nullable=False)  # 'user' 或 'ai'
    content = db.Column(db.Text, nullable=False)
    used_config_name = db.Column(db.String(100), nullable=True, default='')
    used_search = db.Column(db.Boolean, default=False)
    error_hint = db.Column(db.Text, nullable=True, default='')
    created_at = db.Column(db.DateTime, default=datetime.now)


class AIQueryLog(db.Model):
    """AI 查询日志（旧版兼容）"""
    __tablename__ = 'ai_query_logs'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    session_id = db.Column(db.String(100), nullable=True, default='')
    query_type = db.Column(db.String(20), default='qa')  # qa, price_compare, ...
    query_text = db.Column(db.Text, nullable=True)
    response_text = db.Column(db.Text, nullable=True)
    response_time_ms = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.now)

    user = db.relationship('User', backref=db.backref('ai_query_logs', lazy=True))


class Banquet(db.Model):
    """专属宴席 / 活动大账本（如婚礼、百日宴、乔迁宴等）"""
    __tablename__ = 'banquets'
    
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(128), nullable=False)          # 宴席大账本名称（如“张伟 & 李娜 婚礼喜宴”）
    event_type = db.Column(db.String(64), nullable=False)       # 宴席类型（婚礼 / 百日宴 / 乔迁 / 寿宴 / 其它）
    event_date = db.Column(db.String(32), nullable=True)        # 举办日期 (YYYY-MM-DD)
    venue = db.Column(db.String(256), nullable=True)            # 举办地点 / 酒店
    budget = db.Column(db.Float, default=0.0)                   # 预算金额
    banquet_cost = db.Column(db.Float, default=0.0)             # 办宴总成本（酒席/烟酒/婚庆/物料支出）
    notes = db.Column(db.Text, nullable=True)                   # 备注说明
    created_at = db.Column(db.DateTime, default=datetime.now)
    deleted_at = db.Column(db.DateTime, nullable=True)          # 软删除标记
    creator_type = db.Column(db.String(32), default='manual')  # 'manual' (手动创建) | 'auto' (自动归集生成)
    creator_id = db.Column(db.Integer, nullable=True)          # 关联创建人/归集所属用户 ID
    source_username = db.Column(db.String(64), nullable=True)  # 来源用户名
    
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    records = db.relationship('GiftRecord', backref='banquet', lazy=True)

    @property
    def is_auto_created(self):
        if self.creator_type == 'auto':
            return True
        if self.notes and ('由系统自动归集' in self.notes or '自动归集' in self.notes):
            return True
        return False

    @property
    def display_source_user(self):
        if self.source_username:
            return self.source_username
        if hasattr(self, 'owner') and self.owner:
            return self.owner.username
        return '系统'


class GiftRecord(db.Model):
    __tablename__ = 'gift_records'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), nullable=False)           # 姓名 - 必填
    record_type = db.Column(db.String(20), default='receive') # 记录类型：'receive' (收礼) | 'send' (随礼/送礼)
    age = db.Column(db.Integer, nullable=True)                 # 年龄 - 可选
    address = db.Column(db.String(256), nullable=True)         # 地址 - 可选
    phone = db.Column(db.String(30), nullable=True)            # 联系电话 - 可选
    amount = db.Column(db.Float, nullable=False)               # 礼金数额 - 必填
    event_reason = db.Column(db.String(128), nullable=False)   # 办席事由/送礼原因 - 必填
    notes = db.Column(db.Text, nullable=True)                  # 备注 - 可选
    created_at = db.Column(db.DateTime, default=datetime.now)
    deleted_at = db.Column(db.DateTime, nullable=True)         # 软删除标记（回收站）
    
    banquet_id = db.Column(db.Integer, db.ForeignKey('banquets.id', ondelete='SET NULL'), nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)

    @property
    def person_name(self):
        return self.name

    @person_name.setter
    def person_name(self, val):
        self.name = val

    @property
    def gift_type(self):
        return 'received' if getattr(self, 'record_type', 'receive') != 'send' else 'sent'

    @property
    def event_category(self):
        return self.event_reason

    @property
    def event_date(self):
        return self.created_at


class AnniversaryReminder(db.Model):
    """亲友重要纪念日提醒（生日、结婚纪念日等）"""
    __tablename__ = 'anniversary_reminders'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), nullable=False)           # 亲友姓名
    relation = db.Column(db.String(64), nullable=True)         # 关系标签（长辈/同学/朋友/同事/亲戚）
    phone = db.Column(db.String(30), nullable=True)            # 联系电话
    target_date = db.Column(db.String(32), nullable=False)     # 纪念日期（如 10-15 或 1995-10-15）
    anniversary_type = db.Column(db.String(32), default='birthday') # birthday(生日), wedding(结婚纪念), other(其它)
    advance_days = db.Column(db.Integer, default=3)            # 提前提醒天数（默认 3 天）
    notes = db.Column(db.String(256), nullable=True)           # 备忘备注
    is_active = db.Column(db.Boolean, default=True)
    last_notified_target = db.Column(db.String(32), nullable=True) # 已通知目标周期 YYYY-MM-DD，防周期内重复推送
    created_at = db.Column(db.DateTime, default=datetime.now)
    deleted_at = db.Column(db.DateTime, nullable=True)         # 软删除标记（回收站）
    
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False)


class SystemSetting(db.Model):
    __tablename__ = 'system_settings'
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(50), unique=True, nullable=False)
    value = db.Column(db.String(255), nullable=True)

    @classmethod
    def get_val(cls, key, default=None):
        setting = cls.query.filter_by(key=key).first()
        return setting.value if setting and setting.value is not None else default

    @classmethod
    def set_val(cls, key, value):
        setting = cls.query.filter_by(key=key).first()
        if not setting:
            setting = cls(key=key, value=str(value) if value is not None else '')
            db.session.add(setting)
        else:
            setting.value = str(value) if value is not None else ''
        db.session.commit()


class RegistrationToken(db.Model):
    __tablename__ = 'registration_tokens'
    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.String(64), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now)
    expires_at = db.Column(db.DateTime, nullable=False)
    max_uses = db.Column(db.Integer, default=1)
    use_count = db.Column(db.Integer, default=0)
    used = db.Column(db.Boolean, default=False)
    used_at = db.Column(db.DateTime, nullable=True)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)


class LoginRisk(db.Model):
    __tablename__ = 'login_risks'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    fail_count = db.Column(db.Integer, default=0, nullable=False)
    lock_until = db.Column(db.Float, default=0.0, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)

    @classmethod
    def get_risk(cls, username):
        if not username:
            return 0, 0.0
        try:
            risk = cls.query.filter_by(username=username).first()
            if not risk:
                return 0, 0.0
            return risk.fail_count, risk.lock_until
        except Exception:
            db.session.rollback()
            return 0, 0.0

    @classmethod
    def record_fail(cls, username):
        if not username:
            return 1
        try:
            risk = cls.query.filter_by(username=username).first()
            if not risk:
                risk = cls(username=username, fail_count=1, lock_until=0.0)
                db.session.add(risk)
            else:
                risk.fail_count += 1
            db.session.commit()
            return risk.fail_count
        except Exception:
            db.session.rollback()
            return 1

    @classmethod
    def set_lock_until(cls, username, lock_until):
        if not username:
            return
        try:
            risk = cls.query.filter_by(username=username).first()
            if risk:
                risk.lock_until = lock_until
                db.session.commit()
        except Exception:
            db.session.rollback()

    @classmethod
    def clear_risk(cls, username):
        if not username:
            return
        try:
            cls.query.filter_by(username=username).delete()
            db.session.commit()
        except Exception:
            db.session.rollback()


class SecurityRisk(db.Model):
    __tablename__ = 'security_risks'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    fail_count = db.Column(db.Integer, default=0, nullable=False)
    lock_until = db.Column(db.Float, default=0.0, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)

    @classmethod
    def get_risk(cls, username):
        if not username:
            return 0, 0.0
        try:
            risk = cls.query.filter_by(username=username).first()
            if not risk:
                return 0, 0.0
            return risk.fail_count, risk.lock_until
        except Exception:
            db.session.rollback()
            return 0, 0.0

    @classmethod
    def record_fail(cls, username):
        if not username:
            return 1
        try:
            risk = cls.query.filter_by(username=username).first()
            if not risk:
                risk = cls(username=username, fail_count=1, lock_until=0.0)
                db.session.add(risk)
            else:
                risk.fail_count += 1
            db.session.commit()
            return risk.fail_count
        except Exception:
            db.session.rollback()
            return 1

    @classmethod
    def set_lock_until(cls, username, lock_until):
        if not username:
            return
        try:
            risk = cls.query.filter_by(username=username).first()
            if risk:
                risk.lock_until = lock_until
                db.session.commit()
        except Exception:
            db.session.rollback()

    @classmethod
    def clear_risk(cls, username):
        if not username:
            return
        try:
            cls.query.filter_by(username=username).delete()
            db.session.commit()
        except Exception:
            db.session.rollback()


class OperationLog(db.Model):
    __tablename__ = 'operation_logs'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    username = db.Column(db.String(50), nullable=False)
    action = db.Column(db.String(50), nullable=False)
    detail = db.Column(db.String(500), nullable=True)
    ip_address = db.Column(db.String(50), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
    user = db.relationship('User', backref=db.backref('operation_logs', lazy=True))


class Broadcast(db.Model):
    """系统广播通知体系"""
    __tablename__ = 'broadcasts'
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(100), nullable=False)
    content = db.Column(db.Text, nullable=False)
    level = db.Column(db.String(20), default='info')  # info, warning, danger, success
    scope = db.Column(db.String(20), default='all')   # all, admin, user
    is_active = db.Column(db.Boolean, default=True)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now)


class BroadcastRead(db.Model):
    """系统广播已读记录表"""
    __tablename__ = 'broadcast_reads'
    id = db.Column(db.Integer, primary_key=True)
    broadcast_id = db.Column(db.Integer, db.ForeignKey('broadcasts.id', ondelete='CASCADE'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    read_at = db.Column(db.DateTime, default=datetime.now)


class SharedLedgerLink(db.Model):
    """大账本只读共享外链模型"""
    __tablename__ = 'shared_ledger_links'
    id = db.Column(db.Integer, primary_key=True)
    share_token = db.Column(db.String(64), unique=True, nullable=False)
    title = db.Column(db.String(100), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    banquet_id = db.Column(db.Integer, db.ForeignKey('banquets.id', ondelete='SET NULL'), nullable=True)
    _access_password = db.Column('access_password', db.String(256), nullable=True)  # 可选只读访问密码（AES-256-GCM密文）
    hide_notes = db.Column(db.Boolean, default=False)          # 隐私信息脱敏：是否隐藏备注
    hide_amount = db.Column(db.Boolean, default=False)         # 隐私信息脱敏：是否隐藏具体金额
    expires_at = db.Column(db.DateTime, nullable=True)         # 有效期截止时间（None 表示永久有效）
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
    
    user = db.relationship('User', backref=db.backref('shared_links', lazy=True))
    banquet_rel = db.relationship('Banquet', backref=db.backref('shared_links', lazy=True))

    def __init__(self, **kwargs):
        pwd = kwargs.pop('access_password', None)
        super(SharedLedgerLink, self).__init__(**kwargs)
        if pwd is not None:
            self.access_password = pwd

    @property
    def access_password(self):
        if not self._access_password:
            return None
        return decrypt_credential(self._access_password, fallback_plain=True)

    @access_password.setter
    def access_password(self, val):
        if val is None or str(val).strip() == '':
            self._access_password = None
        else:
            self._access_password = encrypt_credential(str(val).strip())

    @property
    def owner(self):
        return self.user

    @property
    def token(self):
        return self.share_token


class WebhookConfig(db.Model):
    """多渠道 Webhook / 凭证长连接机器人消息推送配置"""
    __tablename__ = 'webhook_configs'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    channel_name = db.Column(db.String(50), nullable=False)  # 微信PushPlus / Server酱 / 钉钉 / 飞书 / 企微 / Bark / 智能机器人
    webhook_url = db.Column(db.String(500), nullable=True)
    _secret_token = db.Column('secret_token', db.String(512), nullable=True)  # AES-256-GCM 密文存储
    connection_type = db.Column(db.String(30), default='webhook_url')  # 'webhook_url' (标准 Webhook) | 'long_connection' (凭证长连接)
    bot_platform = db.Column(db.String(50), default='wecom')           # 'wecom' (企业微信机器人) | 'general' (通用长连接)
    bot_id = db.Column(db.String(100), nullable=True)                  # 机器人 Bot ID
    _bot_secret = db.Column('bot_secret', db.String(512), nullable=True)  # AES-256-GCM 密文存储，禁止明文落盘
    is_enabled = db.Column(db.Boolean, default=True)
    notify_on_add = db.Column(db.Boolean, default=True)
    notify_on_delete = db.Column(db.Boolean, default=True)
    notify_on_reminder = db.Column(db.Boolean, default=True)
    notify_on_broadcast = db.Column(db.Boolean, default=True)
    # 新增：推送矩阵与自定义模板字段
    notify_on_update = db.Column(db.Boolean, default=False)
    notify_on_security = db.Column(db.Boolean, default=False)
    notify_on_system = db.Column(db.Boolean, default=True)
    notify_on_status_change = db.Column(db.Boolean, default=False)
    notify_pages = db.Column(db.Text, default='{}')  # JSON: {event_category: [page_keys]}
    message_templates = db.Column(db.Text, default='{}')  # JSON: {event_type:page_key or event_type: template_str}
    # V10.3: 用户级 Webhook 监控过滤
    monitor_user_ids = db.Column(db.Text, default='[]')    # JSON: [user_id, ...], 空=不限制
    monitor_event_types = db.Column(db.Text, default='[]')  # JSON: [event_type, ...], 空=不限制
    created_at = db.Column(db.DateTime, default=datetime.now)
    
    user = db.relationship('User', backref=db.backref('webhooks', lazy=True))

    def __init__(self, **kwargs):
        sec = kwargs.pop('secret_token', None) or kwargs.pop('secret', None)
        b_sec = kwargs.pop('bot_secret', None)
        super(WebhookConfig, self).__init__(**kwargs)
        if sec is not None:
            self.secret_token = sec
        if b_sec is not None:
            self.bot_secret = b_sec

    @property
    def is_long_connection(self):
        return self.connection_type == 'long_connection'

    @property
    def name(self):
        return self.channel_name

    @name.setter
    def name(self, val):
        self.channel_name = val

    @property
    def url(self):
        return self.webhook_url

    @url.setter
    def url(self, val):
        self.webhook_url = val

    @property
    def secret_token(self):
        if not self._secret_token:
            return None
        return decrypt_credential(self._secret_token, fallback_plain=True)

    @secret_token.setter
    def secret_token(self, val):
        if val is None or str(val).strip() == '':
            self._secret_token = None
        else:
            self._secret_token = encrypt_credential(str(val).strip())

    @property
    def secret(self):
        return self.secret_token

    @secret.setter
    def secret(self, val):
        self.secret_token = val

    @property
    def bot_secret(self):
        if not self._bot_secret:
            return None
        return decrypt_credential(self._bot_secret, fallback_plain=True)

    @bot_secret.setter
    def bot_secret(self, val):
        if val is None or str(val).strip() == '':
            self._bot_secret = None
        else:
            self._bot_secret = encrypt_credential(str(val).strip())

    @property
    def is_active(self):
        return self.is_enabled

    @is_active.setter
    def is_active(self, val):
        self.is_enabled = val


class BackupConfig(db.Model):
    """云端 / WebDAV 自动定时备份配置（支持多用户隔离）"""
    __tablename__ = 'backup_configs'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=True)  # NULL=管理员全局配置，非NULL=用户私有配置
    allow_view_others_tasks = db.Column(db.Boolean, default=False)  # 管理员全局配置：是否允许普通用户查看他人任务
    # V10.2 新增：定时任务操作权限细化
    allow_edit_others_tasks = db.Column(db.Boolean, default=False)  # 是否允许普通用户编辑他人定时任务
    allow_delete_others_tasks = db.Column(db.Boolean, default=False)  # 是否允许普通用户删除他人定时任务
    webdav_url = db.Column(db.String(255), nullable=True)
    webdav_username = db.Column(db.String(100), nullable=True)
    webdav_password = db.Column(db.String(255), nullable=True)
    backup_path = db.Column(db.String(255), default='/gift_backups/')
    auto_backup_daily = db.Column(db.Boolean, default=False)
    _backup_encrypt_password = db.Column('backup_encrypt_password', db.String(512), nullable=True)  # AES-256-GCM 密文存储
    last_backup_time = db.Column(db.DateTime, nullable=True)
    last_status = db.Column(db.String(255), nullable=True)
    backup_subdir = db.Column(db.String(100), default='gift_backups')
    updated_at = db.Column(db.DateTime, default=datetime.now)
    # V9 新增：配置别称（管理员为全局 WebDAV 配置设置，供普通用户引用时展示，不暴露地址/账号/密码）
    config_alias = db.Column(db.String(100), nullable=True)
    # V9 新增：该用户配置是否引用自管理员（引用后地址/账号/密码在页面不回显）
    adopted_from_admin = db.Column(db.Boolean, default=False)

    @property
    def server_url(self):
        return self.webdav_url

    @server_url.setter
    def server_url(self, val):
        self.webdav_url = val

    @property
    def username(self):
        return self.webdav_username

    @username.setter
    def username(self, val):
        self.webdav_username = val

    @property
    def password(self):
        return self.get_webdav_password()

    @password.setter
    def password(self, val):
        self.set_webdav_password(val)

    @property
    def remote_dir(self):
        return self.backup_path

    @remote_dir.setter
    def remote_dir(self, val):
        self.backup_path = val

    @property
    def auto_backup(self):
        return self.auto_backup_daily

    @auto_backup.setter
    def auto_backup(self, val):
        self.auto_backup_daily = val

    def set_webdav_password(self, password):
        self.webdav_password = encrypt_credential(password)

    def get_webdav_password(self):
        return decrypt_credential(self.webdav_password)

    @classmethod
    def get_config(cls, user_id=None):
        """获取配置：user_id=None 返回管理员全局配置，否则返回用户私有配置
        V7 修复：普通用户首次访问创建空白配置，不再自动复制管理员全局配置（越权风险）"""
        if user_id is None:
            # 管理员全局配置（user_id 为 NULL 的记录）
            cfg = cls.query.filter(cls.user_id.is_(None)).first()
            if not cfg:
                cfg = cls()
                db.session.add(cfg)
                db.session.commit()
            return cfg
        else:
            # 用户私有配置（V7: 空白配置，不继承管理员凭证）
            cfg = cls.query.filter(cls.user_id == user_id).first()
            if not cfg:
                cfg = cls(user_id=user_id)
                db.session.add(cfg)
                db.session.commit()
            return cfg

    @property
    def backup_encrypt_password(self):
        if not self._backup_encrypt_password:
            return None
        return decrypt_credential(self._backup_encrypt_password, fallback_plain=True)

    @backup_encrypt_password.setter
    def backup_encrypt_password(self, val):
        if val is None or str(val).strip() == '':
            self._backup_encrypt_password = None
        else:
            self._backup_encrypt_password = encrypt_credential(str(val).strip())


class ScheduledBackupTask(db.Model):
    """定时任务（支持数据库备份、文件备份、自定义脚本）"""
    __tablename__ = 'scheduled_backup_tasks'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, default='定时任务')
    cron_expr = db.Column(db.String(50), nullable=False, default='0 2 * * *')  # 默认每天凌晨2点
    is_enabled = db.Column(db.Boolean, default=False)
    encrypt_enabled = db.Column(db.Boolean, default=False)
    task_type = db.Column(db.String(20), default='db_backup')  # db_backup / file_backup / custom
    target_files = db.Column(db.Text, nullable=True)  # JSON 数组，file_backup 类型用
    custom_script = db.Column(db.Text, nullable=True)  # 自定义脚本，custom 类型用
    last_run_time = db.Column(db.DateTime, nullable=True)
    last_run_status = db.Column(db.String(255), nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)  # 创建者ID
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)

    creator = db.relationship('User', foreign_keys=[created_by], backref=db.backref('scheduled_tasks', lazy=True))


class ScheduledTaskExecutionLog(db.Model):
    """定时任务执行历史日志"""
    __tablename__ = 'scheduled_task_execution_logs'
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey('scheduled_backup_tasks.id', ondelete='CASCADE'), nullable=False)
    start_time = db.Column(db.DateTime, nullable=True)
    end_time = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(20), default='running')  # running / success / failed
    output_log = db.Column(db.Text, nullable=True)  # 执行输出日志
    executed_by = db.Column(db.String(100), nullable=True)  # 执行人用户名（定时任务自动执行时为 'system'）
    created_at = db.Column(db.DateTime, default=datetime.now)

    task = db.relationship('ScheduledBackupTask', backref=db.backref('execution_logs', lazy=True, cascade='all, delete-orphan'))


class BackupAttachment(db.Model):
    """备份附件文件"""
    __tablename__ = 'backup_attachments'
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255), nullable=False)
    file_size = db.Column(db.Integer, default=0)
    file_type = db.Column(db.String(50), default='application/octet-stream')
    is_encrypted = db.Column(db.Boolean, default=False)
    uploaded_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    storage_path = db.Column(db.String(500), nullable=True)  # WebDAV 远端路径或本地路径
    created_at = db.Column(db.DateTime, default=datetime.now)

    user = db.relationship('User', backref=db.backref('backup_attachments', lazy=True))


class PermissionTicket(db.Model):
    """权限申请工单"""
    __tablename__ = 'permission_tickets'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    requested_menus = db.Column(db.String(256), nullable=False)  # 申请的菜单列表（逗号分隔）
    reason = db.Column(db.Text, nullable=True)  # 申请理由
    status = db.Column(db.String(20), default='pending')  # pending / approved / rejected / revoked
    reviewed_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    review_comment = db.Column(db.Text, nullable=True)
    granted_menus = db.Column(db.String(256), nullable=True)  # 实际批准的菜单列表
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)

    user = db.relationship('User', foreign_keys=[user_id], backref=db.backref('permission_tickets', lazy=True, cascade='all, delete-orphan'))
    reviewer = db.relationship('User', foreign_keys=[reviewed_by])


class WebhookLog(db.Model):
    __tablename__ = 'webhook_logs'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    operator_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    webhook_id = db.Column(db.Integer, db.ForeignKey('webhook_configs.id', ondelete='CASCADE'), nullable=True)
    event_type = db.Column(db.String(50), nullable=False)  # add, delete, test, reminder, broadcast
    payload = db.Column(db.Text, nullable=True)
    status_code = db.Column(db.Integer, nullable=True)
    response_body = db.Column(db.Text, nullable=True)
    is_success = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.now)
    user = db.relationship('User', foreign_keys=[user_id], backref=db.backref('webhook_logs', lazy=True))
    operator = db.relationship('User', foreign_keys=[operator_id])


class FamilyGroup(db.Model):
    """家庭多成员协作记账 - 家庭组模型"""
    __tablename__ = 'family_groups'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)           # 家庭名称，如"张家"
    owner_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)  # 创建者（家长）
    description = db.Column(db.String(256), nullable=True)     # 家庭描述
    created_at = db.Column(db.DateTime, default=datetime.now)

    owner = db.relationship('User', foreign_keys=[owner_id], backref=db.backref('owned_family_groups', lazy=True))
    members = db.relationship('FamilyMember', backref='group', lazy=True, cascade='all, delete-orphan')

    @property
    def member_count(self):
        return len(self.members) if self.members else 0

    def get_member_user_ids(self):
        """获取该家庭组所有成员的 user_id 列表"""
        return [m.user_id for m in self.members]

    def get_head_user_ids(self):
        """获取家长角色的 user_id 列表（含创建者）"""
        ids = [m.user_id for m in self.members if m.role == 'head']
        if self.owner_id and self.owner_id not in ids:
            ids.append(self.owner_id)
        return ids


class FamilyMember(db.Model):
    """家庭多成员协作记账 - 成员关联表"""
    __tablename__ = 'family_members'
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('family_groups.id', ondelete='CASCADE'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    role = db.Column(db.String(20), default='member')         # head（家长）/ member（成员）/ viewer（只读）
    nickname = db.Column(db.String(64), nullable=True)       # 家庭内昵称，如"爸爸"、"妈妈"
    joined_at = db.Column(db.DateTime, default=datetime.now)

    user = db.relationship('User', foreign_keys=[user_id], backref=db.backref('family_memberships', lazy=True))

    @property
    def is_head(self):
        return self.role == 'head'
