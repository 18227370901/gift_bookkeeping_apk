# -*- coding: utf-8 -*-
import os
import io
import time
import shutil
import urllib.parse
import threading
from datetime import datetime, timedelta
from flask import render_template, request, redirect, url_for, flash, jsonify, send_from_directory, make_response, Response, session, current_app
from flask_login import login_required, current_user
from models import (
    db, User, GiftRecord, Banquet, AnniversaryReminder, Broadcast, BroadcastRead,
    WebhookConfig, WebhookLog, SharedLedgerLink, BackupConfig, LoginRisk, SecurityRisk,
    SystemSetting, ScheduledBackupTask, BackupAttachment, PermissionTicket,
    ScheduledTaskExecutionLog, FamilyGroup, FamilyMember
)
from webhook_utils import trigger_webhook_event, test_single_webhook, validate_wecom_credentials, extract_chatid_from_url, start_wecom_long_connection_listener, _cached_chatids, record_webhook_log, _send_payload, send_wecom_long_connection_message
from webdav_utils import (
    test_connection as test_webdav_connection,
    upload_backup as upload_backup_webdav,
    list_backups as list_webdav_backups,
    download_backup as download_webdav_backup,
    download_and_decrypt_backup,
    upload_encrypted_backup,
    upload_file_to_webdav,
    delete_webdav_backup,
    # V10.10.22: 改用检测函数而非模块级变量快照——
    # webdav_utils.HAS_PYZIPPER 初值为 None（延迟检测），import 时捕获快照后永远不更新，
    # 页面 has_pyzipper 收到 None（falsy）会导致 pyzipper 实际已安装也误报"未安装"
    _ensure_pyzipper
)
from gift_utils import (
    parse_gift_nlp, parse_gift_nlp_multi, split_gift_nlp_text,
    get_gift_suggestion, calculate_reconciliation, cn2num
)
from _daemon_lock import try_acquire_daemon_lock


# ===== 金额转中文大写辅助函数 =====
def _amount_to_cn(num):
    """金额转中文大写（用于打印/PDF）"""
    if num is None:
        num = 0
    num = round(float(num), 2)
    int_part = int(num)
    dec_part = round((num - int_part) * 100)
    digit_map = '零壹贰叁肆伍陆柒捌玖'
    unit_map = ['', '拾', '佰', '仟', '万', '拾', '佰', '仟', '亿']
    if int_part == 0:
        int_cn = '零'
    else:
        int_cn = ''
        s = str(int_part)
        for i, d in enumerate(s):
            d = int(d)
            pos = len(s) - i - 1
            if d == 0:
                if not int_cn.endswith('零') and pos not in (4, 8):
                    int_cn += '零'
                elif pos == 4 and not int_cn.endswith('万'):
                    int_cn += '万'
                elif pos == 8 and not int_cn.endswith('亿'):
                    int_cn += '亿'
            else:
                int_cn += digit_map[d] + unit_map[pos]
    result = int_cn + '元'
    if dec_part == 0:
        result += '整'
    else:
        jiao = int(dec_part // 10)
        fen = int(dec_part % 10)
        if jiao > 0:
            result += digit_map[jiao] + '角'
        elif int_part > 0 or dec_part > 0:
            result += '零'
        if fen > 0:
            result += digit_map[fen] + '分'
    return result



import sqlite3 as _sqlite3
import tempfile as _tempfile
import json as _backup_json


# ===================== V10.10.17 PostgreSQL 备份/恢复兼容层 =====================

def _is_pg_mode():
    """检查当前是否为 PostgreSQL 模式"""
    return os.environ.get('DATABASE_URL', '').strip().startswith('postgresql://')

# PG 业务表清单（与 SQLite 完全一致，22 张表）
_PG_ALL_TABLES = [
    'users', 'gift_records', 'operation_logs', 'system_settings',
    'registration_tokens', 'login_risks', 'security_risks',
    'broadcasts', 'broadcast_reads', 'webhook_configs', 'webhook_logs',
    'shared_ledger_links', 'backup_configs', 'banquets',
    'anniversary_reminders', 'chat_sessions', 'chat_messages',
    'ai_query_logs', 'scheduled_backup_tasks', 'backup_attachments',
    'permission_tickets', 'scheduled_task_execution_logs'
]
_PG_USER_TABLES = ['gift_records', 'banquets', 'anniversary_reminders']
_PG_GLOBAL_TABLES = [t for t in _PG_ALL_TABLES if t not in _PG_USER_TABLES]

def _pg_export_database(output_path, user_filter=None):
    """
    PG 模式：使用 psycopg2 导出数据库到 JSON 文件
    user_filter: 非 None 时仅导出该用户的 3 张业务表
    返回 output_path
    """
    import psycopg2
    from datetime import date as _date, decimal as _decimal
    db_url = os.environ.get('DATABASE_URL', '').strip()
    conn = psycopg2.connect(db_url)
    cursor = conn.cursor()
    result = {}
    tables_to_export = _PG_USER_TABLES if user_filter else _PG_ALL_TABLES
    for table in tables_to_export:
        query = f'SELECT * FROM "{table}"'
        if user_filter and table in _PG_USER_TABLES:
            query += f' WHERE user_id = {int(user_filter)}'
        try:
            cursor.execute(query)
            colnames = [desc[0] for desc in cursor.description]
            rows = cursor.fetchall()
            result[table] = {'columns': colnames, 'rows': [list(r) for r in rows]}
        except Exception:
            result[table] = {'columns': [], 'rows': []}
    conn.close()
    def _default(o):
        if isinstance(o, (datetime, _date)):
            return o.isoformat()
        if isinstance(o, _decimal.Decimal):
            return float(o)
        if isinstance(o, bytes):
            return o.decode('utf-8', errors='replace')
        return str(o)
    with open(output_path, 'w', encoding='utf-8') as f:
        _backup_json.dump(result, f, ensure_ascii=False, default=_default)
    return output_path

def _pg_import_database(input_path, user_filter=None):
    """
    PG 模式：从 JSON 文件恢复数据库
    user_filter: 非 None 时仅恢复该用户的 3 张业务表（数据级合并）
    返回 (success, message, stats)
    """
    import psycopg2
    db_url = os.environ.get('DATABASE_URL', '').strip()
    try:
        with open(input_path, 'r', encoding='utf-8') as f:
            data = _backup_json.load(f)
        conn = psycopg2.connect(db_url)
        cursor = conn.cursor()
        stats = {}
        tables_to_import = _PG_USER_TABLES if user_filter else list(data.keys())
        for table_name, table_data in data.items():
            if user_filter and table_name not in _PG_USER_TABLES:
                continue
            columns = table_data.get('columns', [])
            rows = table_data.get('rows', [])
            if not columns or not rows:
                continue
            # 合并模式：先删除该用户旧数据
            if user_filter and table_name in _PG_USER_TABLES:
                cursor.execute(f'DELETE FROM "{table_name}" WHERE user_id = %s', (int(user_filter),))
            col_str = ', '.join(f'"{c}"' for c in columns)
            ph_str = ', '.join(['%s'] * len(columns))
            for row in rows:
                try:
                    cursor.execute(f'INSERT INTO "{table_name}" ({col_str}) VALUES ({ph_str})', row)
                except Exception:
                    pass  # 跳过类型不符或约束冲突的行
            stats[table_name] = len(rows)
        conn.commit()
        conn.close()
        return True, f'恢复完成: {stats}', stats
    except Exception as e:
        return False, f'恢复失败: {e}', {}



# V7 修复：普通用户备份越权漏洞
# 原逻辑直接复制完整数据库文件上传/下载，普通用户可获取他人数据。
# 本函数将主库复制到临时文件后用 SQL 删除非本人数据，仅保留本人礼金/宴席/纪念日。
# 管理员或拥有跨用户查看权限的用户返回原始库路径，无需过滤。
# V8 增强：普通用户备份时额外 DROP 所有系统全局表（users、WebDAV 配置、定时任务、
# Webhook、AI 配置、日志等），仅保留本人业务数据，杜绝全局配置泄露。
def build_user_scoped_backup_db(db_path, user):
    """
    为普通用户生成仅含本人数据的临时备份数据库。
    - 管理员 / can_view_others_for('ledger') 用户：直接返回原始 db_path（有权查看全库）
    - 普通用户：复制主库到临时文件，删除非本人数据 + 删除系统全局表后返回临时文件路径
    返回 (temp_db_path, is_temp) —— is_temp=True 表示调用方用完需自行删除临时文件
    """
    # V10.10.17: PG 模式用 JSON 导出替代 SQLite 文件复制
    if _is_pg_mode():
        if not user or not hasattr(user, 'is_admin'):
            return db_path, False
        if getattr(user, 'is_admin', False):
            return db_path, False
        if hasattr(user, 'can_view_others_for') and user.can_view_others_for('ledger'):
            return db_path, False
        # 普通用户：导出本人数据到临时 .sql 文件
        tmp_dir = _tempfile.mkdtemp(prefix='gift_pg_scope_')
        tmp_sql = os.path.join(tmp_dir, 'scoped_backup.sql')
        _pg_export_database(tmp_sql, user_filter=user.id)
        return tmp_sql, True

    if not user or not hasattr(user, 'is_admin'):
        return db_path, False
    if getattr(user, 'is_admin', False):
        return db_path, False
    # 有跨用户查看权限的用户也能看到全库，无需过滤
    if hasattr(user, 'can_view_others_for') and user.can_view_others_for('ledger'):
        return db_path, False
    # 普通用户：生成仅含本人数据的临时库
    tmp_dir = _tempfile.mkdtemp(prefix='gift_user_scope_')
    tmp_db = os.path.join(tmp_dir, 'scoped_backup.db')
    # V9 修复：主库为 WAL 模式时，shutil.copy2 只复制主文件会丢失 -wal 日志中未落盘的最新数据。
    #       改用 SQLite 在线备份 API（Connection.backup），获得包含 WAL 数据的一致性快照，
    #       不受主文件 LastWriteTime 滞后影响。
    _src_conn = _sqlite3.connect(db_path)
    _dst_conn = _sqlite3.connect(tmp_db)
    with _dst_conn:
        _src_conn.backup(_dst_conn)
    _src_conn.close()
    _dst_conn.close()
    try:
        conn = _sqlite3.connect(tmp_db)
        c = conn.cursor()
        # 1) 用户业务数据按 user_id 过滤：仅保留本人礼金/宴席/纪念日
        c.execute("DELETE FROM gift_records WHERE user_id != ?", (user.id,))
        c.execute("DELETE FROM banquets WHERE user_id != ?", (user.id,))
        c.execute("DELETE FROM anniversary_reminders WHERE user_id != ?", (user.id,))
        conn.commit()
        # 2) 系统全局敏感表：直接 DROP，杜绝普通用户备份中携带其他用户/全局配置数据
        #    （含用户表、WebDAV 配置、定时任务、Webhook、AI 会话、操作日志、广播等）
        _global_tables = [
            'users', 'backup_configs', 'scheduled_backup_tasks',
            'scheduled_task_execution_logs', 'webhook_configs', 'webhook_logs',
            'operation_logs', 'login_risks', 'security_risks', 'system_settings',
            'registration_tokens', 'broadcasts', 'broadcast_reads',
            'shared_ledger_links', 'backup_attachments', 'permission_tickets',
            'chat_sessions', 'chat_messages', 'ai_query_logs',
        ]
        for tbl in _global_tables:
            try:
                c.execute(f'DROP TABLE IF EXISTS "{tbl}"')
            except Exception:
                pass
        conn.commit()
        conn.close()
        return tmp_db, True
    except Exception:
        # 出错时回退到原始库（宁可功能不可用也不暴露数据）
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass
        raise


# V9 修复：普通用户恢复 .db 备份导致系统崩溃
# 根因：普通用户备份是"过滤库"（19 张全局表被 DROP、仅含本人 3 张业务表数据），
#       但恢复流程却用该文件"文件级替换"整个主库 → users 等核心表丢失 → 全站 500。
# 方案：普通用户恢复改为"数据级合并"——只把备份中本人三张业务表的数据合回主库，
#       不触碰主库文件结构与其他用户/全局数据；管理员保持原文件级替换。
def merge_user_scoped_backup(db_path, backup_db_path, user):
    """
    将普通用户的过滤备份（仅含 gift_records/banquets/anniversary_reminders 三表）
    以数据级合并方式恢复到主库：仅覆盖该用户本人的三张业务表数据。
    返回 (success: bool, message: str, stats: dict)
    """
    # V10.10.17: PG 模式用 JSON 导入替代 SQLite ATTACH 合并
    if _is_pg_mode():
        if not user or not hasattr(user, 'is_admin'):
            return False, '无效用户', {}
        return _pg_import_database(backup_db_path, user_filter=user.id)

    if not user or not hasattr(user, 'is_admin'):
        return False, '无效用户', {}
    user_id = user.id
    user_tables = ['gift_records', 'banquets', 'anniversary_reminders']
    stats = {}
    try:
        # 防锁：写主库前先把 Flask 的 SQLAlchemy session 里的未提交事务落盘
        # （如操作日志等），避免主库写锁冲突
        try:
            from flask import has_app_context
            if has_app_context():
                from flask_sqlalchemy import SQLAlchemy  # noqa: F401
                from flask import current_app as _cur_app
                _ext = _cur_app.extensions.get('sqlalchemy')
                if _ext is not None:
                    _ext.session.commit()  # 提交/结束当前请求未提交事务
        except Exception:
            pass
        src = _sqlite3.connect(backup_db_path)
        src.row_factory = _sqlite3.Row
        # 防锁：给合并连接设置忙等待，遇主库瞬时写锁时重试而非立即报错
        src.execute('PRAGMA busy_timeout = 8000')
        # 校验备份中实际存在的业务表
        existing = {r[0] for r in src.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        # V10.7：完整库拦截——非管理员上传的备份若含 users 等全局表（说明是完整库/他人库），
        # 明确拒绝，防止用他人或管理员完整备份覆盖本人数据
        if not getattr(user, 'is_admin', False) and 'users' in existing:
            src.close()
            return False, '检测到上传的是完整备份文件（含全局用户表），普通用户仅可恢复本人生成的过滤备份，请联系管理员处理', {}
        missing = [t for t in user_tables if t not in existing]
        if missing:
            src.close()
            return False, f'备份文件中缺少业务表: {", ".join(missing)}，无法恢复', {}

        # V10.7：结构兼容性校验——三张业务表必须包含 user_id 列（旧版/异构表结构直接报错，避免 SQL 异常导致 500）
        for tbl in user_tables:
            _tbl_cols = {r[1] for r in src.execute(f'PRAGMA table_info("{tbl}")').fetchall()}
            if 'user_id' not in _tbl_cols:
                src.close()
                return False, f'备份文件 [{tbl}] 表缺少 user_id 列，数据库结构不兼容，无法恢复', {}

        # ATTACH 主库，在同一连接内完成数据合并（自动处理跨库约束）
        src.execute("ATTACH DATABASE ? AS main_db", (db_path,))
        for tbl in user_tables:
            # V10: 空库保护——DELETE 前先检查备份库中该用户是否有数据，若为 0 则跳过该表
            # 避免上传空库导致本人现有数据被清空且无新数据写入
            backup_count = src.execute(
                f'SELECT COUNT(*) FROM "{tbl}" WHERE user_id = ?', (user_id,)
            ).fetchone()[0]
            if backup_count == 0:
                stats[tbl] = 0
                continue  # 跳过该表，保留本人现有数据不动
            # 1) 删除主库中该用户本人的旧数据
            src.execute(f'DELETE FROM main_db."{tbl}" WHERE user_id = ?', (user_id,))
            # 2) 从备份导入该用户数据（列对齐：取备份表与主表共有列，排除 id 由 SQLite 重新分配主键）
            # 注意：PRAGMA 的 schema 前缀格式为 PRAGMA main_db.table_info("表名")，不能写成 PRAGMA table_info(main_db."表名")
            main_cols = {r[1] for r in src.execute(f'PRAGMA main_db.table_info("{tbl}")').fetchall()}
            _backup_cols = [r[1] for r in src.execute(f'PRAGMA table_info("{tbl}")').fetchall()]
            src_cols = [c for c in _backup_cols if c in main_cols and c != 'id']
            col_list = ', '.join(f'"{c}"' for c in src_cols)
            placeholders = ', '.join('?' for _ in src_cols)
            # V10.7：SELECT 前置 user_id 过滤（数据隔离双保险，不再全表扫描再逐行过滤）
            rows = src.execute(
                f'SELECT {col_list} FROM "{tbl}" WHERE user_id = ?', (user_id,)
            ).fetchall()
            inserted = 0
            for row in rows:
                # 备份按 user_id 过滤生成，这里再防御性校验一次
                d = dict(row)
                if d.get('user_id') != user_id:
                    continue
                src.execute(
                    f'INSERT INTO main_db."{tbl}" ({col_list}) VALUES ({placeholders})',
                    tuple(d[c] for c in src_cols)
                )
                inserted += 1
            stats[tbl] = inserted
        # 修复：必须先 commit 结束写事务，再 DETACH——
        # SQLite 不允许 DETACH 一个存在未提交事务的数据库（会报 database is locked）
        src.commit()
        src.execute('DETACH DATABASE main_db')
        src.close()
        total = sum(stats.values())
        return True, f'数据级合并完成，共恢复 {total} 条本人数据', stats
    except Exception as e:
        return False, f'数据级合并失败: {str(e)}', {}


def parse_target_date_obj(date_val, today=None):
    if not date_val:
        return None, 9999
    if today is None:
        today = datetime.now().date()

    month, day = None, None
    if hasattr(date_val, 'month') and hasattr(date_val, 'day'):
        month, day = date_val.month, date_val.day
    else:
        s = str(date_val).strip()
        for fmt in ('%Y-%m-%d', '%Y/%m/%d', '%m-%d', '%m/%d'):
            try:
                dt = datetime.strptime(s, fmt)
                month, day = dt.month, dt.day
                break
            except Exception:
                continue

    if not month or not day:
        return None, 9999

    try:
        this_year_date = datetime(today.year, month, day).date()
    except ValueError:
        this_year_date = datetime(today.year, month, 28).date()

    if this_year_date < today:
        try:
            this_year_date = datetime(today.year + 1, month, day).date()
        except ValueError:
            this_year_date = datetime(today.year + 1, month, 28).date()

    days_left = (this_year_date - today).days
    return this_year_date, days_left


def format_reminder_notification_content(reminders_with_days):
    """
    格式化亲友纪念日提醒通知内容（准确展示所有详细内容，消除【其它】这类模糊信息）
    reminders_with_days: list of (reminder_obj, days_left, next_date_obj)
    """
    if not reminders_with_days:
        return "【亲友重要纪念日提醒】", "当前暂无临近的亲友纪念日。", []

    count = len(reminders_with_days)
    title = f"【亲友重要纪念日提醒】 近期有 {count} 位亲友重要纪念日临近"

    md_blocks = []
    text_lines = []

    for r, days_left, next_date in reminders_with_days:
        raw_type = (r.anniversary_type or '').strip()
        type_map = {
            'birthday': '生日',
            'wedding': '结婚纪念日',
            'anniversary': '重要纪念日',
            'other': '重要纪念日'
        }
        type_cn = type_map.get(raw_type.lower(), raw_type) if raw_type else '纪念日'
        notes = (r.notes or '').strip()

        # 智能提炼具体事件名称，坚决消除【其它】这类模糊信息：
        # 如果分类是其它且有备注，直接采用备注作为事件名称；若无备注则使用亲友重要纪念日
        if type_cn in ('其它', '其他', 'other', ''):
            event_name = notes if notes else '亲友重要纪念日'
        else:
            if notes and notes != type_cn and notes not in ('生日', '结婚纪念日'):
                event_name = f"{type_cn} ({notes})"
            else:
                event_name = type_cn

        # 倒计时文案
        if days_left == 0:
            status_desc = f"🎉 <font color=\"warning\">**就是今天** ({next_date.strftime('%m-%d')})！</font>"
            short_status = f"今天({next_date.strftime('%m-%d')})！"
        else:
            status_desc = f"还有 <font color=\"warning\">**{days_left}**</font> 天 ({next_date.strftime('%m-%d')})"
            short_status = f"还有 {days_left} 天({next_date.strftime('%m-%d')})"

        relation_str = f"({r.relation})" if r.relation else ""
        adv_days = r.advance_days or 3

        # Markdown 模块排版（全要素展示：姓名、关系、具体事项、倒计时、目标日期、下次公历、预警天数、备忘说明、联系电话）
        detail_items = [
            f"  > 目标日期：{r.target_date}（下次公历：{next_date.strftime('%Y-%m-%d')}）",
            f"  > 预警提醒：已设置提前 {adv_days} 天预警"
        ]
        if notes:
            detail_items.append(f"  > 备忘说明：{notes}")
        if r.phone:
            detail_items.append(f"  > 联系电话：{r.phone}")

        block = (
            f"• **{r.name}**{relation_str} 的【{event_name}】{status_desc}\n" +
            "\n".join(detail_items)
        )
        md_blocks.append(block)

        # 纯文本摘要信息（全要素展示）
        text_parts = [
            f"目标日期: {r.target_date}",
            f"预警: 提前{adv_days}天"
        ]
        if notes:
            text_parts.append(f"备忘: {notes}")
        if r.phone:
            text_parts.append(f"电话: {r.phone}")
        text_lines.append(f"• {r.name}{relation_str} 的【{event_name}】{short_status} | " + " | ".join(text_parts))

    md_detail = "\n\n".join(md_blocks)
    return title, md_detail, text_lines


_reminder_scheduler_thread = None
_reminder_scheduler_running = False

def check_and_trigger_due_reminders(app_obj=None, specific_reminder=None):
    """检查到达设置提醒天数的纪念日并自动触发 Webhook 推送"""
    try:
        from datetime import datetime
        today = datetime.now().date()
        webhooks = WebhookConfig.query.filter_by(is_enabled=True).all()
        active_hooks = [w for w in webhooks if getattr(w, 'notify_on_reminder', True) not in (False, 0, '0', 'false')]
        if not active_hooks:
            return 0

        if specific_reminder:
            rems = [specific_reminder] if (specific_reminder.is_active and not specific_reminder.deleted_at) else []
        else:
            rems = AnniversaryReminder.query.filter(
                AnniversaryReminder.deleted_at.is_(None),
                AnniversaryReminder.is_active == True
            ).all()

        due_list = []
        for r in rems:
            next_date, days_left = parse_target_date_obj(r.target_date, today)
            if not next_date:
                continue
            adv = r.advance_days or 3
            if 0 <= days_left <= adv:
                target_cycle_str = next_date.strftime('%Y-%m-%d')
                if not specific_reminder:
                    # 到达设置阈值后默认仅推送一次（本周期内不再重复自动推送，彻底消除高频刷屏）
                    if getattr(r, 'last_notified_target', None) == target_cycle_str:
                        continue
                due_list.append((r, days_left, next_date, target_cycle_str))

        if not due_list:
            return 0

        # 即刻更新已通知目标周期，持久化到数据库
        for item in due_list:
            r_obj = item[0]
            cycle_str = item[3]
            r_obj.last_notified_target = cycle_str
        try:
            db.session.commit()
        except Exception as _ce:
            db.session.rollback()
            print(f"[Anniversary Worker Commit Warning]: {_ce}")

        title, md_detail, text_summary = format_reminder_notification_content([(x[0], x[1], x[2]) for x in due_list])
        trigger_webhook_event(active_hooks, 'auto_reminder', f"近期有 {len(due_list)} 位亲友重要纪念日临近", md_detail, force_channels=True)
        print(f"[Anniversary Worker] 发现 {len(due_list)} 条临近纪念日，已触发自动推送通知: {[x[0].name for x in due_list]}")
        return len(due_list)
    except Exception as e:
        print(f"[check_and_trigger_due_reminders error]: {e}")
        return 0


def _anniversary_reminder_worker(flask_app):
    """后台常驻守护线程：定期巡检到达提醒天数的亲友纪念日并自动推送"""
    global _reminder_scheduler_running
    time.sleep(3)
    while _reminder_scheduler_running:
        # V10.10.16: 跨进程单实例锁，多 Worker 环境下仅一个进程执行巡检
        if not try_acquire_daemon_lock('anniversary_reminder'):
            count = 0
            while _reminder_scheduler_running and count < 6:
                time.sleep(10)
                count += 1
            continue
        try:
            with flask_app.app_context():
                check_and_trigger_due_reminders(flask_app)
        except Exception as e:
            print(f"[Anniversary Worker Error]: {e}")
        count = 0
        while _reminder_scheduler_running and count < 6:
            time.sleep(10)
            count += 1


def start_anniversary_reminder_scheduler(flask_app):
    global _reminder_scheduler_thread, _reminder_scheduler_running
    if _reminder_scheduler_thread and _reminder_scheduler_thread.is_alive():
        return
    _reminder_scheduler_running = True
    _reminder_scheduler_thread = threading.Thread(target=_anniversary_reminder_worker, args=(flask_app,), daemon=True)
    _reminder_scheduler_thread.start()
    print("[Scheduler] 亲友纪念日自动提醒后台守护线程已成功启动")


# ===================== 定时备份调度器 =====================

_backup_scheduler_running = False
_backup_scheduler_thread = None


def _parse_cron_field(expr, field_type):
    """
    解析 cron 表达式的单个字段，返回该字段应匹配的数值集合。
    支持格式：* / 数字 / 逗号列表 / 范围 / 步长（*/n）
    field_type: 'minute' | 'hour' | 'day' | 'month' | 'weekday'
    """
    ranges = {
        'minute': (0, 59),
        'hour': (0, 23),
        'day': (1, 31),
        'month': (1, 12),
        'weekday': (0, 6),  # 0=周日, 6=周六
    }
    min_val, max_val = ranges.get(field_type, (0, 59))
    expr = expr.strip()
    result = set()

    # 处理 */n 步长
    if '/' in expr:
        parts = expr.split('/', 1)
        base = parts[0].strip()
        step = int(parts[1].strip())
        if base == '*':
            for i in range(min_val, max_val + 1, step):
                result.add(i)
            return result
        elif '-' in base:
            lo, hi = base.split('-', 1)
            for i in range(int(lo), int(hi) + 1, step):
                result.add(i)
            return result

    # 处理 * 通配符
    if expr == '*':
        for i in range(min_val, max_val + 1):
            result.add(i)
        return result

    # 处理逗号分隔列表
    if ',' in expr:
        for part in expr.split(','):
            sub = part.strip()
            if '-' in sub:
                lo, hi = sub.split('-', 1)
                for i in range(int(lo), int(hi) + 1):
                    result.add(i)
            else:
                result.add(int(sub))
        return result

    # 处理范围 a-b
    if '-' in expr:
        lo, hi = expr.split('-', 1)
        for i in range(int(lo), int(hi) + 1):
            result.add(i)
        return result

    # 处理单个数字
    result.add(int(expr))
    return result


def _cron_match(cron_expr, dt):
    """检查给定时间是否匹配 cron 表达式"""
    try:
        parts = cron_expr.strip().split()
        if len(parts) != 5:
            return False
        minute_set = _parse_cron_field(parts[0], 'minute')
        hour_set = _parse_cron_field(parts[1], 'hour')
        day_set = _parse_cron_field(parts[2], 'day')
        month_set = _parse_cron_field(parts[3], 'month')
        weekday_set = _parse_cron_field(parts[4], 'weekday')

        # Python weekday: 0=Monday ... 6=Sunday
        # Cron weekday: 0=Sunday ... 6=Saturday
        cron_wday = (dt.weekday() + 1) % 7  # 转换为 cron 格式

        return (dt.minute in minute_set and
                dt.hour in hour_set and
                dt.day in day_set and
                dt.month in month_set and
                cron_wday in weekday_set)
    except Exception:
        return False


def _backup_scheduler_worker(flask_app):
    """后台常驻守护线程：定期检查 ScheduledBackupTask 表中启用的任务，按 cron 表达式执行加密备份上传"""
    global _backup_scheduler_running
    time.sleep(5)  # 等待应用完全启动
    while _backup_scheduler_running:
        # V10.10.16: 跨进程单实例锁，多 Worker 环境下仅一个进程执行备份调度
        if not try_acquire_daemon_lock('backup_scheduler'):
            count = 0
            while _backup_scheduler_running and count < 6:
                time.sleep(10)
                count += 1
            continue
        try:
            with flask_app.app_context():
                now = datetime.now()
                tasks = ScheduledBackupTask.query.filter_by(is_enabled=True).all()
                for task in tasks:
                    # 检查是否匹配当前时间（精确到分钟）
                    if not _cron_match(task.cron_expr, now):
                        continue
                    # 避免同一分钟内重复执行
                    if task.last_run_time:
                        delta = now - task.last_run_time
                        if delta.total_seconds() < 120:
                            continue

                    # 执行备份
                    print(f"[Backup Scheduler] 执行定时任务: {task.name} (cron: {task.cron_expr}, type: {task.task_type})")
                    # 创建执行日志记录
                    exec_log = ScheduledTaskExecutionLog(
                        task_id=task.id,
                        start_time=now,
                        status='running',
                        executed_by='system'
                    )
                    db.session.add(exec_log)
                    db.session.commit()
                    try:
                        config = BackupConfig.get_config(task.created_by if task.created_by else None)
                        if not config.server_url or not config.username:
                            task.last_run_time = now
                            task.last_run_status = '失败: WebDAV 未配置'
                            exec_log.status = 'failed'
                            exec_log.end_time = datetime.now()
                            exec_log.output_log = '失败: WebDAV 未配置'
                            db.session.commit()
                            continue

                        db_path = flask_app.config.get('SQLALCHEMY_DATABASE_URI', '').replace('sqlite:///', '')
                        task_type = getattr(task, 'task_type', 'db_backup') or 'db_backup'

                        if task_type == 'custom':
                            # 自定义脚本执行
                            script = getattr(task, 'custom_script', None)
                            if not script:
                                task.last_run_time = now
                                task.last_run_status = '失败: 未配置自定义脚本'
                                exec_log.status = 'failed'
                                exec_log.end_time = datetime.now()
                                exec_log.output_log = '失败: 未配置自定义脚本'
                                db.session.commit()
                                continue
                            import subprocess
                            try:
                                result = subprocess.run(
                                    script, shell=True, capture_output=True, text=True, timeout=300
                                )
                                if result.returncode == 0:
                                    task.last_run_time = now
                                    task.last_run_status = f'成功: 脚本执行完成'
                                    exec_log.status = 'success'
                                    exec_log.end_time = datetime.now()
                                    exec_log.output_log = result.stdout[:500]
                                    db.session.commit()
                                else:
                                    task.last_run_time = now
                                    task.last_run_status = f'失败: 脚本返回码 {result.returncode}, {result.stderr[:200]}'
                                    exec_log.status = 'failed'
                                    exec_log.end_time = datetime.now()
                                    exec_log.output_log = f'返回码 {result.returncode}: {result.stderr[:500]}'
                                    db.session.commit()
                            except Exception as se:
                                task.last_run_time = now
                                task.last_run_status = f'异常: {str(se)[:200]}'
                                exec_log.status = 'failed'
                                exec_log.end_time = datetime.now()
                                exec_log.output_log = f'异常: {str(se)[:500]}'
                                db.session.commit()

                        elif task_type == 'file_backup':
                            # 文件备份：打包指定文件上传
                            target_files_str = getattr(task, 'target_files', None)
                            if not target_files_str:
                                task.last_run_time = now
                                task.last_run_status = '失败: 未指定备份文件'
                                exec_log.status = 'failed'
                                exec_log.end_time = datetime.now()
                                exec_log.output_log = '失败: 未指定备份文件'
                                db.session.commit()
                                continue
                            import json as _json
                            try:
                                file_list = _json.loads(target_files_str)
                            except Exception:
                                file_list = [f.strip() for f in target_files_str.split(',') if f.strip()]
                            success, msg = upload_encrypted_backup(config, local_file_path=file_list, encrypt_password=None)
                            if success:
                                task.last_run_time = now
                                task.last_run_status = f'成功: {msg}'
                                exec_log.status = 'success'
                                exec_log.end_time = datetime.now()
                                exec_log.output_log = msg[:500]
                                db.session.commit()
                            else:
                                task.last_run_time = now
                                task.last_run_status = f'失败: {msg}'
                                exec_log.status = 'failed'
                                exec_log.end_time = datetime.now()
                                exec_log.output_log = f'失败: {msg[:500]}'
                                db.session.commit()

                        else:
                            # 默认：数据库备份
                            # 确定加密密码
                            encrypt_pwd = None
                            if task.encrypt_enabled:
                                encrypt_pwd = config.backup_encrypt_password
                                if not encrypt_pwd:
                                    task.last_run_time = now
                                    task.last_run_status = '失败: 未配置加密密码'
                                    db.session.commit()
                                    continue

                            # V7 修复：普通用户创建的定时任务，执行时生成仅含本人数据的临时库
                            _task_creator = db.session.get(User, task.created_by) if task.created_by else None
                            _scoped_path = db_path
                            _is_temp = False
                            # V10.10.17: PG 模式用 JSON 导出替代文件复制
                            if _is_pg_mode():
                                _tmp_dir = _tempfile.mkdtemp(prefix='gift_pg_sched_')
                                _scoped_path = os.path.join(_tmp_dir, 'pg_backup.sql')
                                _is_temp = True
                                if _task_creator and not getattr(_task_creator, 'is_admin', False):
                                    if not (hasattr(_task_creator, 'can_view_others_for') and _task_creator.can_view_others_for('ledger')):
                                        _pg_export_database(_scoped_path, user_filter=_task_creator.id)
                                    else:
                                        _pg_export_database(_scoped_path)
                                else:
                                    _pg_export_database(_scoped_path)
                            elif _task_creator and not getattr(_task_creator, 'is_admin', False):
                                if not (hasattr(_task_creator, 'can_view_others_for') and _task_creator.can_view_others_for('ledger')):
                                    try:
                                        _scoped_path, _is_temp = build_user_scoped_backup_db(db_path, _task_creator)
                                    except Exception as se:
                                        task.last_run_time = now
                                        task.last_run_status = f'失败: 生成用户备份数据异常: {str(se)[:200]}'
                                        exec_log.status = 'failed'
                                        exec_log.end_time = datetime.now()
                                        exec_log.output_log = task.last_run_status
                                        db.session.commit()
                                        continue

                            success, msg = upload_encrypted_backup(config, local_file_path=_scoped_path, encrypt_password=encrypt_pwd)
                            # V7: 清理临时库
                            if _is_temp:
                                try:
                                    shutil.rmtree(os.path.dirname(_scoped_path), ignore_errors=True)
                                except Exception:
                                    pass
                            if success:
                                task.last_run_time = now
                                task.last_run_status = f'成功: {msg}'
                                config.last_backup_time = now
                                config.last_status = '定时备份成功'
                                exec_log.status = 'success'
                                exec_log.end_time = datetime.now()
                                exec_log.output_log = msg[:500]
                                db.session.commit()
                                print(f"[Backup Scheduler] 定时备份成功: {msg}")
                            else:
                                task.last_run_time = now
                                task.last_run_status = f'失败: {msg}'
                                config.last_status = f'定时备份失败: {msg}'
                                exec_log.status = 'failed'
                                exec_log.end_time = datetime.now()
                                exec_log.output_log = f'失败: {msg[:500]}'
                                db.session.commit()
                                print(f"[Backup Scheduler] 定时备份失败: {msg}")
                    except Exception as e:
                        task.last_run_time = now
                        task.last_run_status = f'异常: {str(e)}'
                        exec_log.status = 'failed'
                        exec_log.end_time = datetime.now()
                        exec_log.output_log = f'异常: {str(e)[:500]}'
                        db.session.commit()
                        print(f"[Backup Scheduler Error] 任务 {task.name}: {e}")
        except Exception as e:
            print(f"[Backup Scheduler Worker Error]: {e}")

        # 每 60 秒检查一次
        count = 0
        while _backup_scheduler_running and count < 6:
            time.sleep(10)
            count += 1


def start_backup_scheduler(flask_app):
    """启动定时备份调度器守护线程"""
    global _backup_scheduler_thread, _backup_scheduler_running
    if _backup_scheduler_thread and _backup_scheduler_thread.is_alive():
        return
    _backup_scheduler_running = True
    _backup_scheduler_thread = threading.Thread(target=_backup_scheduler_worker, args=(flask_app,), daemon=True)
    _backup_scheduler_thread.start()
    print("[Scheduler] 定时备份后台守护线程已成功启动")

def register_routes_ext(app, log_operation=None, get_accessible_records_query=None, get_accessible_banquets_query=None, get_accessible_reminders_query=None, can_user_view_entity=None, can_user_edit_entity=None, can_user_delete_entity=None, clear_login_risk=None, clear_forgot_security_risk=None, app_start_time=None, **kwargs):

    def safe_log(action, detail="", user=None):
        if not log_operation:
            return
        try:
            log_operation(action, detail, user=user)
        except TypeError:
            try:
                log_operation(action, detail)
            except Exception:
                pass
        except Exception:
            pass

    # --- 统一菜单访问权限控制拦截 ---
    @app.before_request
    def check_menu_permissions():
        if not current_user.is_authenticated:
            return
        if getattr(current_user, 'is_admin', False):
            return
        endpoint = request.endpoint or ''
        menu_map = {
            # V7 修复：移除 'index': 'ledger' 映射。
            # 原逻辑无权限访问 index 时重定向回 index，造成无限重定向循环；
            # index 页的权限检查改由 app.py 的 index() 视图函数内实现，
            # 无权限时跳转权限工单页申请权限，不再重定向回 index。
            'add_record': 'ledger',
            'edit_record': 'ledger',
            'delete_record': 'ledger',
            'batch_delete_records': 'ledger',
            'delete_all_records': 'ledger',
            'export_csv': 'ledger',
            'import_csv': 'ledger',
            'banquets_view': 'banquets',
            'banquet_detail_view': 'banquets',
            'banquet_quick_add': 'banquets',
            'banquet_edit': 'banquets',
            'banquet_delete': 'banquets',
            'banquet_export_excel': 'banquets',
            'banquets_sync': 'banquets',
            'banquet_import_records': 'banquets',
            'banquet_unlink_record': 'banquets',
            'banquet_share': 'banquets',
            'banquet_share_delete': 'banquets',
            'banquets_batch_delete': 'banquets',
            'banquet_record_delete': 'banquets',
            'banquet_records_batch_delete': 'banquets',
            'banquet_records_batch_unlink': 'banquets',
            'reconciliation_view': 'reconciliation',
            'reconciliation_sync': 'reconciliation',
            'api_person_ledger': 'reconciliation',
            'reminders_view': 'reminders',
            'reminder_edit': 'reminders',
            'reminder_delete': 'reminders',
            'reminders_batch_delete': 'reminders',
            'api_trigger_reminder_push': 'reminders',
            'api_upcoming_reminders': 'reminders',
            'recycle_bin_view': 'recycle_bin',
            'restore_record': 'recycle_bin',
            'purge_record': 'recycle_bin',
            'batch_restore_records': 'recycle_bin',
            'batch_purge_records': 'recycle_bin',
            'clear_recycle_bin': 'recycle_bin',
            'dashboard_view': 'dashboard',
            'api_dashboard_stats': 'dashboard',
            'family_view': 'family',
            'api_family_my_groups': 'family',
            'api_family_create': 'family',
            'api_family_invite': 'family',
            'api_family_dissolve': 'family',
            'api_family_invitable_users': 'family',
            'api_family_my_perspective_users': 'family',
            'export_print_giftbook': 'ledger',
            'export_pdf_statement': 'ledger',
            'poster_view': 'ledger',
            'api_poster_data': 'ledger',
        }
        required_menu = menu_map.get(endpoint)
        if required_menu and hasattr(current_user, 'can_access_menu'):
            if not current_user.can_access_menu(required_menu):
                menu_names = {
                    'banquets': '专属宴席',
                    'reconciliation': '人情对账',
                    'reminders': '纪念日备忘',
                    'recycle_bin': '回收站',
                    'ledger': '礼金账本',
                    'dashboard': '数据分析',
                    'family': '家庭记账'
                }
                m_name = menu_names.get(required_menu, '该功能')
                if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    return jsonify({'code': 403, 'message': f'您暂无权限访问【{m_name}】功能模块，请联系管理员分配权限！'}), 403
                flash(f'您暂无权限访问【{m_name}】功能模块，请联系管理员为您分配访问权限！', 'warning')
                # V7 修复：无权限时跳转权限工单页申请权限，而不是重定向回 index
                # （避免用户被取消全部权限后陷入首页无限重定向循环）
                return redirect(url_for('permission_tickets_view'))

    # --- 回收站过期自动清理机制 ---
    def cleanup_expired_recycle_items():
        try:
            days_str = SystemSetting.get_val('recycle_bin_retention_days', '30')
            days_val = int(days_str) if days_str and str(days_str).isdigit() else 30
            if days_val > 0:
                cutoff = datetime.now() - timedelta(days=days_val)
                GiftRecord.query.filter(GiftRecord.deleted_at.isnot(None), GiftRecord.deleted_at < cutoff).delete(synchronize_session=False)
                Banquet.query.filter(Banquet.deleted_at.isnot(None), Banquet.deleted_at < cutoff).delete(synchronize_session=False)
                AnniversaryReminder.query.filter(AnniversaryReminder.deleted_at.isnot(None), AnniversaryReminder.deleted_at < cutoff).delete(synchronize_session=False)
                db.session.commit()
        except Exception as e:
            db.session.rollback()

    @app.route('/recycle_bin')
    @login_required
    def recycle_bin_view():
        """
        全系统统一回收站视图：
        支持跨模块软删除回收（礼金账本、专属宴席、纪念日备忘），
        显示所属标签页/模块标签，支持保留时长设置、搜索与分页。
        """
        cleanup_expired_recycle_items()

        # 1. 查询各模块软删除数据（严格遵守跨用户数据权限）
        can_view_all = current_user.is_admin or (hasattr(current_user, 'can_view_others_for') and current_user.can_view_others_for('recycle_bin'))

        q_records = GiftRecord.query.filter(GiftRecord.deleted_at.isnot(None))
        q_banquets = Banquet.query.filter(Banquet.deleted_at.isnot(None))
        q_reminders = AnniversaryReminder.query.filter(AnniversaryReminder.deleted_at.isnot(None))

        if not can_view_all:
            q_records = q_records.filter_by(user_id=current_user.id)
            q_banquets = q_banquets.filter_by(user_id=current_user.id)
            q_reminders = q_reminders.filter_by(user_id=current_user.id)

        raw_records = q_records.all()
        raw_banquets = q_banquets.all()
        raw_reminders = q_reminders.all()

        all_items = []
        for r in raw_records:
            is_send = getattr(r, 'record_type', 'receive') in ('send', 'give')
            all_items.append({
                'id': r.id,
                'entity_type': 'record',
                'module_key': 'ledger',
                'module_name': '礼金账本',
                'module_badge': 'bg-success-subtle text-success border border-success-subtle',
                'title': r.name,
                'category': r.event_reason or '礼金记录',
                'amount': r.amount,
                'type_label': '送礼' if is_send else '收礼',
                'type_badge': 'bg-danger-subtle text-danger' if is_send else 'bg-success-subtle text-success',
                'notes': r.notes or '',
                'phone': r.phone or '',
                'deleted_at': r.deleted_at,
                'owner_name': r.owner.username if r.owner else '',
                'can_delete': can_user_delete_entity(current_user, r, 'recycle_bin') if can_user_delete_entity else (current_user.is_admin or r.user_id == current_user.id),
                'can_edit': can_user_edit_entity(current_user, r, 'recycle_bin') if can_user_edit_entity else (current_user.is_admin or r.user_id == current_user.id)
            })

        for b in raw_banquets:
            all_items.append({
                'id': b.id,
                'entity_type': 'banquet',
                'module_key': 'banquets',
                'module_name': '专属宴席',
                'module_badge': 'bg-warning-subtle text-warning-emphasis border border-warning-subtle',
                'title': b.title,
                'category': b.event_type or '宴席大账本',
                'amount': b.banquet_cost,
                'type_label': '办宴成本',
                'type_badge': 'bg-warning-subtle text-dark',
                'notes': b.notes or '',
                'phone': b.venue or '',
                'deleted_at': b.deleted_at,
                'owner_name': b.owner.username if b.owner else '',
                'can_delete': can_user_delete_entity(current_user, b, 'recycle_bin') if can_user_delete_entity else (current_user.is_admin or b.user_id == current_user.id),
                'can_edit': can_user_edit_entity(current_user, b, 'recycle_bin') if can_user_edit_entity else (current_user.is_admin or b.user_id == current_user.id)
            })

        for rem in raw_reminders:
            all_items.append({
                'id': rem.id,
                'entity_type': 'reminder',
                'module_key': 'reminders',
                'module_name': '纪念日备忘',
                'module_badge': 'bg-info-subtle text-info-emphasis border border-info-subtle',
                'title': rem.name,
                'category': rem.anniversary_type or '纪念日',
                'amount': None,
                'type_label': rem.relation or '亲友',
                'type_badge': 'bg-secondary-subtle text-secondary',
                'notes': rem.notes or '',
                'phone': rem.phone or '',
                'deleted_at': rem.deleted_at,
                'owner_name': rem.owner.username if getattr(rem, 'owner', None) else '',
                'can_delete': can_user_delete_entity(current_user, rem, 'recycle_bin') if can_user_delete_entity else (current_user.is_admin or rem.user_id == current_user.id),
                'can_edit': can_user_edit_entity(current_user, rem, 'recycle_bin') if can_user_edit_entity else (current_user.is_admin or rem.user_id == current_user.id)
            })

        # 统计各模块总数
        counts = {
            'all': len(all_items),
            'ledger': len([x for x in all_items if x['module_key'] == 'ledger']),
            'banquets': len([x for x in all_items if x['module_key'] == 'banquets']),
            'reminders': len([x for x in all_items if x['module_key'] == 'reminders']),
        }

        # 2. 模块筛选 (module: all, ledger, banquets, reminders)
        module_filter = request.args.get('module', 'all').strip()
        if module_filter in ['ledger', 'banquets', 'reminders']:
            filtered_items = [x for x in all_items if x['module_key'] == module_filter]
        else:
            module_filter = 'all'
            filtered_items = all_items

        # 3. 搜索过滤
        search = request.args.get('search', '').strip()
        if search:
            s_low = search.lower()
            filtered_items = [
                x for x in filtered_items
                if (
                    s_low in x['title'].lower() or
                    s_low in x['category'].lower() or
                    s_low in x['notes'].lower() or
                    s_low in x['phone'].lower() or
                    s_low in x['owner_name'].lower()
                )
            ]

        # 4. 排序（默认按删除时间倒序）
        sort_by = request.args.get('sort', 'deleted_desc').strip()
        if sort_by == 'deleted_asc':
            filtered_items.sort(key=lambda x: x['deleted_at'] or datetime.min)
        elif sort_by == 'amount_desc':
            filtered_items.sort(key=lambda x: x['amount'] or 0.0, reverse=True)
        elif sort_by == 'amount_asc':
            filtered_items.sort(key=lambda x: x['amount'] or 0.0)
        else:
            filtered_items.sort(key=lambda x: x['deleted_at'] or datetime.min, reverse=True)

        # 5. 分页
        try:
            per_page = int(request.args.get('per_page', 10))
            if per_page not in [5, 10, 20, 50, 100]:
                per_page = 10
        except (ValueError, TypeError):
            per_page = 10

        total_count = len(filtered_items)
        total_pages = max(1, (total_count + per_page - 1) // per_page)

        try:
            page = int(request.args.get('page', 1))
            if page < 1:
                page = 1
            elif page > total_pages:
                page = total_pages
        except (ValueError, TypeError):
            page = 1

        start_idx = (page - 1) * per_page
        paged_items = filtered_items[start_idx : start_idx + per_page]

        # 读取管理员配置的保留天数
        retention_days = int(SystemSetting.get_val('recycle_bin_retention_days', '30') or 30)

        can_clear_recycle = current_user.is_admin or (hasattr(current_user, 'can_delete_others_for') and current_user.can_delete_others_for('recycle_bin'))
        can_batch_restore = current_user.is_admin or any(i.get('can_edit') for i in paged_items)
        can_batch_purge = current_user.is_admin or any(i.get('can_delete') for i in paged_items)

        return render_template(
            'recycle_bin.html',
            items=paged_items,
            counts=counts,
            module_filter=module_filter,
            search=search,
            sort_by=sort_by,
            page=page,
            per_page=per_page,
            total_count=total_count,
            total_pages=total_pages,
            retention_days=retention_days,
            can_clear_recycle=can_clear_recycle,
            can_batch_restore=can_batch_restore,
            can_batch_purge=can_batch_purge
        )

    # --- 统一单项/批量还原与删除接口 ---
    @app.route('/recycle_bin/restore/<string:entity_type>/<int:item_id>', methods=['POST'])
    @app.route('/record/restore/<int:item_id>', methods=['POST'])
    @app.route('/restore_record/<int:item_id>', methods=['POST'])
    @app.route('/restore_record', methods=['POST'])
    @login_required
    def restore_item(entity_type='record', item_id=None):
        """还原单条记录（支持礼金记录、专属宴席、纪念日）"""
        target_type = request.form.get('entity_type') or entity_type
        target_id = item_id or request.args.get('id', type=int) or request.form.get('id', type=int)

        if not target_id:
            flash('未指定需要还原的记录ID', 'warning')
            return redirect(url_for('recycle_bin_view'))

        item = None
        title = ''
        if target_type == 'banquet':
            item = db.session.get(Banquet, target_id)
            title = item.title if item else ''
        elif target_type == 'reminder':
            item = db.session.get(AnniversaryReminder, target_id)
            title = item.name if item else ''
        else:
            item = db.session.get(GiftRecord, target_id)
            title = item.name if item else ''

        if not item:
            flash('未找到指定记录或已被彻底清理！', 'warning')
            return redirect(url_for('recycle_bin_view'))

        if can_user_edit_entity and not can_user_edit_entity(current_user, item, 'recycle_bin'):
            flash('您没有权限还原此数据！', 'danger')
            return redirect(url_for('recycle_bin_view'))

        item.deleted_at = None
        db.session.commit()
        safe_log('还原回收站数据', f"还原了 [{target_type}] ID #{target_id}: [{title}]", user=current_user)
        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(), 'restore',
                f'还原回收站数据 [{title}]',
                f'操作人：{current_user.username} | 页面：回收站 | 类型：{target_type} | 名称：{title}',
                page_key='recycle_bin', user_name=current_user.username,
                operator_id=current_user.id
            )
        except Exception:
            pass
        flash(f'已成功还原 [{title}]！', 'success')
        return redirect(url_for('recycle_bin_view'))

    @app.route('/recycle_bin/purge/<string:entity_type>/<int:item_id>', methods=['POST'])
    @app.route('/record/permanent_delete/<int:item_id>', methods=['POST'])
    @app.route('/purge_record/<int:item_id>', methods=['POST'])
    @app.route('/purge_record', methods=['POST'])
    @login_required
    def purge_item(entity_type='record', item_id=None):
        """彻底删除单条记录"""
        target_type = request.form.get('entity_type') or entity_type
        target_id = item_id or request.args.get('id', type=int) or request.form.get('id', type=int)

        if not target_id:
            flash('未指定需要删除的记录ID', 'warning')
            return redirect(url_for('recycle_bin_view'))

        item = None
        title = ''
        if target_type == 'banquet':
            item = db.session.get(Banquet, target_id)
            title = item.title if item else ''
        elif target_type == 'reminder':
            item = db.session.get(AnniversaryReminder, target_id)
            title = item.name if item else ''
        else:
            item = db.session.get(GiftRecord, target_id)
            title = item.name if item else ''

        if not item:
            flash('未找到指定记录或已彻底删除！', 'warning')
            return redirect(url_for('recycle_bin_view'))

        if can_user_delete_entity and not can_user_delete_entity(current_user, item, 'recycle_bin'):
            flash('您没有权限彻底删除此数据！', 'danger')
            return redirect(url_for('recycle_bin_view'))

        db.session.delete(item)
        db.session.commit()
        safe_log('彻底删除数据', f"彻底删除了 [{target_type}] ID #{target_id}: [{title}]", user=current_user)
        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(), 'record_delete',
                f'彻底删除 [{title}]',
                f'操作人：{current_user.username} | 页面：回收站 | 类型：{target_type} | 名称：{title}',
                page_key='recycle_bin', user_name=current_user.username,
                operator_id=current_user.id
            )
        except Exception:
            pass
        flash(f'已彻底删除 [{title}]，无法恢复！', 'success')
        return redirect(url_for('recycle_bin_view'))

    @app.route('/recycle_bin/batch_restore', methods=['POST'])
    @app.route('/records/batch_restore', methods=['POST'])
    @app.route('/batch_restore_records', methods=['POST'])
    @login_required
    def batch_restore_items():
        """批量还原选中项（支持跨模块 items 混合参数如 record:12 或单独 record_ids）"""
        # V10.4: 级别1可还原自身数据，移除 == 1 拦截
        raw_items = request.form.getlist('selected_items') or request.form.getlist('selected_items[]') or request.form.getlist('record_ids') or request.form.getlist('record_ids[]')
        if not raw_items:
            flash('未选择任何记录！', 'warning')
            return redirect(url_for('recycle_bin_view'))

        count = 0
        for token in raw_items:
            try:
                if ':' in str(token):
                    etype, iid_str = str(token).split(':', 1)
                    iid = int(iid_str)
                else:
                    etype = request.form.get('entity_type', 'record')
                    iid = int(token)

                item = None
                if etype == 'banquet':
                    item = db.session.get(Banquet, iid)
                elif etype == 'reminder':
                    item = db.session.get(AnniversaryReminder, iid)
                else:
                    item = db.session.get(GiftRecord, iid)

                if item and item.deleted_at:
                    if not can_user_edit_entity or can_user_edit_entity(current_user, item, 'recycle_bin'):
                        item.deleted_at = None
                        count += 1
            except Exception:
                continue

        if count > 0:
            db.session.commit()
            safe_log('批量还原数据', f"批量还原了 {count} 条回收站记录", user=current_user)
            try:
                trigger_webhook_event(
                    WebhookConfig.query.filter_by(is_enabled=True).all(), 'restore',
                    f'批量还原 {count} 条回收站数据',
                    f'操作人：{current_user.username} | 页面：回收站 | 数量：{count}',
                    page_key='recycle_bin', user_name=current_user.username,
                    operator_id=current_user.id
                )
            except Exception:
                pass
            flash(f'成功还原了 {count} 条记录！', 'success')
        else:
            flash('未找到可还原的记录！', 'warning')

        return redirect(url_for('recycle_bin_view'))

    @app.route('/recycle_bin/batch_purge', methods=['POST'])
    @app.route('/records/batch_permanent_delete', methods=['POST'])
    @app.route('/batch_purge_records', methods=['POST'])
    @login_required
    def batch_purge_items():
        """批量彻底删除选中项"""
        if hasattr(current_user, 'get_menu_perm') and current_user.get_menu_perm('recycle_bin') in (2,):
            flash('当前页面权限不允许彻底删除数据！', 'danger')
            return redirect(url_for('recycle_bin_view'))
        raw_items = request.form.getlist('selected_items') or request.form.getlist('selected_items[]') or request.form.getlist('record_ids') or request.form.getlist('record_ids[]')
        if not raw_items:
            flash('未选择任何记录！', 'warning')
            return redirect(url_for('recycle_bin_view'))

        count = 0
        for token in raw_items:
            try:
                if ':' in str(token):
                    etype, iid_str = str(token).split(':', 1)
                    iid = int(iid_str)
                else:
                    etype = request.form.get('entity_type', 'record')
                    iid = int(token)

                item = None
                if etype == 'banquet':
                    item = db.session.get(Banquet, iid)
                elif etype == 'reminder':
                    item = db.session.get(AnniversaryReminder, iid)
                else:
                    item = db.session.get(GiftRecord, iid)

                if item:
                    if not can_user_delete_entity or can_user_delete_entity(current_user, item, 'recycle_bin'):
                        db.session.delete(item)
                        count += 1
            except Exception:
                continue

        if count > 0:
            db.session.commit()
            safe_log('批量彻底删除', f"批量彻底删除了 {count} 条回收站数据", user=current_user)
            try:
                trigger_webhook_event(
                    WebhookConfig.query.filter_by(is_enabled=True).all(), 'batch_delete',
                    f'批量彻底删除 {count} 条回收站数据',
                    f'操作人：{current_user.username} | 页面：回收站 | 数量：{count}',
                    page_key='recycle_bin', user_name=current_user.username,
                    operator_id=current_user.id
                )
            except Exception:
                pass
            flash(f'已彻底删除 {count} 条数据，不可恢复！', 'success')
        else:
            flash('未找到可删除的记录！', 'warning')

        return redirect(url_for('recycle_bin_view'))

    @app.route('/recycle_bin/clear', methods=['POST'])
    @app.route('/records/clear_recycle_bin', methods=['POST'])
    @app.route('/clear_recycle_bin', methods=['POST'])
    @login_required
    def clear_recycle_bin():
        """清空回收站中用户有权删除的所有数据"""
        if hasattr(current_user, 'get_menu_perm') and current_user.get_menu_perm('recycle_bin') in (2,):
            flash('当前页面权限不允许清空回收站！', 'danger')
            return redirect(url_for('recycle_bin_view'))
        if not (current_user.is_admin or (hasattr(current_user, 'can_delete_others_for') and current_user.can_delete_others_for('recycle_bin'))):
            flash('您没有权限清空回收站！', 'danger')
            return redirect(url_for('recycle_bin_view'))
        can_view_all = current_user.is_admin or (hasattr(current_user, 'can_view_others_for') and current_user.can_view_others_for('recycle_bin'))

        q_records = GiftRecord.query.filter(GiftRecord.deleted_at.isnot(None))
        q_banquets = Banquet.query.filter(Banquet.deleted_at.isnot(None))
        q_reminders = AnniversaryReminder.query.filter(AnniversaryReminder.deleted_at.isnot(None))

        if not can_view_all:
            q_records = q_records.filter_by(user_id=current_user.id)
            q_banquets = q_banquets.filter_by(user_id=current_user.id)
            q_reminders = q_reminders.filter_by(user_id=current_user.id)

        count = 0
        for r in q_records.all():
            if not can_user_delete_entity or can_user_delete_entity(current_user, r):
                db.session.delete(r)
                count += 1
        for b in q_banquets.all():
            if not can_user_delete_entity or can_user_delete_entity(current_user, b):
                db.session.delete(b)
                count += 1
        for rem in q_reminders.all():
            if not can_user_delete_entity or can_user_delete_entity(current_user, rem):
                db.session.delete(rem)
                count += 1

        if count > 0:
            db.session.commit()
            safe_log('清空回收站', f"清空回收站数据共 {count} 条", user=current_user)
            try:
                trigger_webhook_event(
                    WebhookConfig.query.filter_by(is_enabled=True).all(), 'clear',
                    f'清空回收站 {count} 条数据',
                    f'操作人：{current_user.username} | 页面：回收站 | 数量：{count}',
                    page_key='recycle_bin', user_name=current_user.username,
                    operator_id=current_user.id
                )
            except Exception:
                pass
            flash(f'回收站已清空，共彻底删除 {count} 条数据！', 'success')
        else:
            flash('回收站当前为空！', 'info')

        return redirect(url_for('recycle_bin_view'))

    @app.route('/admin/recycle_bin/set_retention', methods=['POST'])
    @login_required
    def admin_set_recycle_retention():
        """管理员设置回收站数据保留时长（天数）"""
        if not current_user.is_admin:
            flash('权限不足！', 'danger')
            return redirect(url_for('recycle_bin_view'))

        days_val = request.form.get('retention_days', '30').strip()
        try:
            days = max(0, int(days_val))
        except (ValueError, TypeError):
            days = 30

        SystemSetting.set_val('recycle_bin_retention_days', str(days))
        cleanup_expired_recycle_items()
        safe_log('设置回收站策略', f"管理员将回收站数据保留时长设置为 {days} 天")
        # V10.3: 补充回收站策略设置推送
        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(), 'system',
                f'{current_user.username} 设置回收站策略',
                f'操作人：{current_user.username} | 页面：回收站 | 保留天数：{days}',
                page_key='recycle_bin', user_name=current_user.username, operator_id=current_user.id
            )
        except Exception:
            pass
        desc = f"保留 {days} 天（超出自动彻底清理）" if days > 0 else "永久保留（不自动清理）"
        flash(f'回收站保留时长已设置为：【{desc}】！', 'success')
        return redirect(url_for('recycle_bin_view'))

    @app.route('/recycle_bin/cleanup_expired', methods=['POST'])
    @login_required
    def manual_cleanup_expired_recycle_bin():
        """手动一键清理超出时限的回收站过期数据"""
        if not current_user.is_admin:
            flash('权限不足！', 'danger')
            return redirect(url_for('recycle_bin_view'))

        cleanup_expired_recycle_items()
        safe_log('手动清理过期数据', '手动清理了回收站过期数据', user=current_user)
        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(), 'clear',
                f'手动清理回收站过期数据',
                f'操作人：{current_user.username} | 页面：回收站 | 操作：手动清理过期数据',
                page_key='recycle_bin', user_name=current_user.username,
                operator_id=current_user.id
            )
        except Exception:
            pass
        flash('已执行回收站过期数据清理！', 'success')
        return redirect(url_for('recycle_bin_view'))

    @app.route('/reconciliation')
    @login_required
    def reconciliation_view():
        """人情对账汇总与往来明细（支持搜索、状态筛选、排序与自定义分页）"""
        import json
        query = get_accessible_records_query(current_user, menu_key='reconciliation')
        # 排除回收站软删除数据
        records = query.filter(GiftRecord.deleted_at.is_(None)).all()
        balance_list, total_received, total_given = calculate_reconciliation(records)

        # 1. 搜索查询（亲友姓名模糊搜索）
        search = request.args.get('search', '').strip()
        if search:
            balance_list = [item for item in balance_list if search.lower() in item['person_name'].lower()]

        # 2. 状态筛选 (status)
        status = request.args.get('status', '').strip()
        if status == 'need_return':  # 待还礼 → 对方欠我方 → net_balance < 0
            balance_list = [item for item in balance_list if item['net_balance'] < 0]
        elif status == 'need_pay':   # 待补礼 → 我方欠对方 → net_balance > 0
            balance_list = [item for item in balance_list if item['net_balance'] > 0]
        elif status == 'balanced':   # 已平账 (net_balance == 0)
            balance_list = [item for item in balance_list if item['net_balance'] == 0]

        # 3. 排序 (sort)
        sort_by = request.args.get('sort', 'diff_abs_desc').strip()
        if sort_by == 'need_return_first':
            balance_list.sort(key=lambda x: x['net_balance'])
        elif sort_by == 'need_pay_first':
            balance_list.sort(key=lambda x: x['net_balance'], reverse=True)
        elif sort_by == 'received_desc':
            balance_list.sort(key=lambda x: x['received_amount'], reverse=True)
        elif sort_by == 'given_desc':
            balance_list.sort(key=lambda x: x['given_amount'], reverse=True)
        elif sort_by == 'name_asc':
            balance_list.sort(key=lambda x: x['person_name'])
        else:
            balance_list.sort(key=lambda x: abs(x['net_balance']), reverse=True)

        # 4. 分页分条数 (默认 10 条，支持设置 10, 20, 50, 100)
        try:
            per_page = int(request.args.get('per_page', 10))
            if per_page not in [5, 10, 20, 50, 100]:
                per_page = 10
        except (ValueError, TypeError):
            per_page = 10

        total_count = len(balance_list)
        total_pages = max(1, (total_count + per_page - 1) // per_page)

        try:
            page = int(request.args.get('page', 1))
            if page < 1:
                page = 1
            if page > total_pages:
                page = total_pages
        except (ValueError, TypeError):
            page = 1

        start_idx = (page - 1) * per_page
        end_idx = start_idx + per_page
        paged_list = balance_list[start_idx:end_idx]

        # 构造姓名对应的明细列表数据字典，供弹窗即时查阅
        records_map = {}
        for item in balance_list:
            records_map[item['person_name']] = item.get('records', [])

        return render_template(
            'reconciliation.html',
            balance_list=paged_list,
            total_count=total_count,
            total_pages=total_pages,
            page=page,
            per_page=per_page,
            search=search,
            status=status,
            sort_by=sort_by,
            total_received=total_received,
            total_given=total_given,
            records_map_json=json.dumps(records_map, ensure_ascii=False)
        )

    @app.route('/reconciliation/sync', methods=['GET', 'POST'])
    @login_required
    def reconciliation_sync():
        """手动从礼金账本拉取最新数据同步人情对账"""
        try:
            query = get_accessible_records_query(current_user, menu_key='reconciliation') if get_accessible_records_query else GiftRecord.query
            records = query.filter(GiftRecord.deleted_at.is_(None)).all()
            balance_list, total_received, total_given = calculate_reconciliation(records)
            safe_log('同步对账数据', f"从礼金账本拉取最新数据同步人情对账，共核对 {len(balance_list)} 位亲友往来")
            # V10: 补充对账同步 webhook 推送
            try:
                trigger_webhook_event(
                    WebhookConfig.query.filter_by(is_enabled=True).all(),
                    'system',
                    f'用户 {current_user.username} 同步了人情对账数据，共核对 {len(balance_list)} 位亲友往来',
                    page_key='reconciliation',
                    user_name=current_user.username,
                    operator_id=current_user.id
                )
            except Exception:
                pass
            flash(f'已成功从礼金账本拉取最新数据完成同步！共核对 {len(balance_list)} 位亲友的人情往来。', 'success')
        except Exception as e:
            flash(f'同步人情对账数据失败：{str(e)}', 'danger')
        return redirect(url_for('reconciliation_view'))

    @app.route('/api/person_ledger/<name>')
    @login_required
    def api_person_ledger(name):
        """获取指定亲友的所有往来礼金明细（完整字段兼容）"""
        clean_name = (name or '').strip()
        query = get_accessible_records_query(current_user)
        records = query.filter(
            GiftRecord.name == clean_name,
            GiftRecord.deleted_at.is_(None)
        ).order_by(GiftRecord.created_at.desc()).all()
        data = []
        for r in records:
            r_type = getattr(r, 'record_type', 'receive')
            is_send = (r_type in ('send', 'give'))
            amt = float(r.amount) if r.amount else 0.0
            dt_str = r.created_at.strftime('%Y-%m-%d %H:%M') if r.created_at else ''
            data.append({
                'id': r.id,
                'name': r.name,
                'type_label': '送礼(我方出)' if is_send else '收礼(对方来)',
                'gift_type': 'sent' if is_send else 'received',
                'record_type': 'send' if is_send else 'receive',
                'amount': amt,
                'event_type': r.event_reason or '礼金',
                'event_category': r.event_reason or '礼金',
                'event_reason': r.event_reason or '礼金',
                'event_date': dt_str,
                'created_at': dt_str,
                'remarks': r.notes or '',
                'notes': r.notes or ''
            })
        return jsonify({'code': 200, 'name': clean_name, 'records': data})

    @app.route('/api/gift-suggestion')
    @login_required
    def api_gift_suggestion():
        """智能还礼金额建议 API"""
        name = request.args.get('name', '').strip()
        all_records = get_accessible_records_query(current_user).all()
        result = get_gift_suggestion(all_records, name)
        return jsonify({'code': 200, **result})

    # --- 自然语言极简记账（支持分隔符多条自动拆分入库） ---
    @app.route('/api/record/parse_nlp', methods=['POST'])
    @login_required
    def api_parse_nlp():
        """自然语言解析记账信息（支持单条及多条复合语句）"""
        data = request.get_json() or {}
        text = data.get('text', '').strip()
        parsed_items = parse_gift_nlp_multi(text)
        single = parsed_items[0] if parsed_items else parse_gift_nlp(text)
        return jsonify({'code': 200, 'data': single, 'items': parsed_items, 'count': len(parsed_items)})

    @app.route('/api/record/nlp_quick_add', methods=['POST'])
    @login_required
    def api_nlp_quick_add():
        """自然语言极简一键记账（常见分割词自动分隔并生成多条记录）"""
        data = request.get_json() or {}
        text = data.get('text', '').strip()
        if not text:
            return jsonify({'code': 400, 'message': '输入文本不能为空'}), 400

        parsed_items = parse_gift_nlp_multi(text)
        if not parsed_items:
            return jsonify({'code': 400, 'message': '未能有效识别出亲友姓名或有效金额，请核对后重试'}), 400

        added_records = []
        for p in parsed_items:
            if not p.get('name') or p.get('amount', 0) <= 0:
                continue
            record = GiftRecord(
                name=p['name'],
                amount=p['amount'],
                event_reason=p.get('event_reason', '其它'),
                record_type=p.get('record_type', 'receive'),
                notes=p.get('notes', text),
                user_id=current_user.id
            )
            db.session.add(record)
            added_records.append(record)

        if not added_records:
            return jsonify({'code': 400, 'message': '未能有效识别出亲友姓名或有效金额，请核对后重试'}), 400

        db.session.commit()
        desc_list = [f"[{r.name}] {'送礼' if r.record_type == 'send' else '收礼'} {r.amount:.2f}元({r.event_reason})" for r in added_records]
        msg = f"成功入库 {len(added_records)} 条礼金记录：" + "、".join(desc_list)
        safe_log('自然语言极简记账', f"通过文本 [{text}] 批量录入 {len(added_records)} 条: " + "，".join(desc_list))
        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(), 'record_create',
                f'NLP极简记账录入 {len(added_records)} 条',
                f'操作人：{current_user.username} | 页面：礼金账本 | ' + "、".join(desc_list),
                page_key='ledger', user_name=current_user.username,
                operator_id=current_user.id
            )
        except Exception:
            pass

        return jsonify({
            'code': 200,
            'message': msg,
            'count': len(added_records),
            'records': [
                {
                    'id': r.id,
                    'name': r.name,
                    'amount': r.amount,
                    'event_reason': r.event_reason,
                    'record_type': r.record_type
                }
                for r in added_records
            ]
        })

    # --- 专属宴席 / 活动大账本与盈亏分析 ---
    def sync_banquets_from_ledger(user, force_restore=False):
        """
        专属宴席与礼金账本映射规范：
        原则一：专属宴席仅归集 record_type == 'receive'（收礼），彻底排除 send（送礼/随礼）
        原则二：清理历史误挂在宴席上的 send 记录（置 banquet_id = None）
        原则三：多维归集升级，按 (user_id, event_reason, 年份) 智能匹配或自动新建专属台账
        原则四：已手动关联宴席的有效收礼记录保持不变
        原则五：常规访问防自动复活；当用户显式点击【从礼金账本同步数据】(force_restore=True) 时，
               系统响应主动诉求，将已删除但在账本中仍有收礼明细的台账恢复/重新归集，并打上来源用户标识！
        """
        if not user or not user.is_authenticated:
            return 0, 0

        # 1. 彻底清理历史脏数据：送礼记录绝不可关联专属宴席
        dirty_send_records = GiftRecord.query.filter(
            GiftRecord.record_type != 'receive',
            GiftRecord.banquet_id.isnot(None)
        ).all()
        for ds in dirty_send_records:
            ds.banquet_id = None

        # 2. 检索当前用户有权访问的有效未删除收礼记录
        if get_accessible_records_query:
            records = get_accessible_records_query(user, menu_key='banquets').filter(
                GiftRecord.deleted_at.is_(None),
                GiftRecord.record_type == 'receive'
            ).all()
        else:
            base_q = GiftRecord.query if user.is_admin else GiftRecord.query.filter_by(user_id=user.id)
            records = base_q.filter(
                GiftRecord.deleted_at.is_(None),
                GiftRecord.record_type == 'receive'
            ).all()

        if not records:
            db.session.commit()
            return 0, 0

        all_banquets = Banquet.query.filter(Banquet.deleted_at.is_(None)).all()
        deleted_banquets = Banquet.query.filter(Banquet.deleted_at.isnot(None)).all()

        groups = {}
        current_year = str(datetime.now().year)

        linked_count = 0
        created_count = 0

        # 缓存用户信息避免重复查询
        user_cache = {}
        def _get_username(uid):
            if uid not in user_cache:
                u_obj = db.session.get(User, uid)
                user_cache[uid] = u_obj.username if u_obj else 'admin'
            return user_cache[uid]

        for r in records:
            if r.banquet_id:
                matched_b = next((b for b in all_banquets if b.id == r.banquet_id), None)
                if matched_b:
                    continue
                matched_deleted_b = next((b for b in deleted_banquets if b.id == r.banquet_id), None)
                if matched_deleted_b:
                    if force_restore:
                        # 主动手动同步：恢复被软删除的台账
                        matched_deleted_b.deleted_at = None
                        matched_deleted_b.creator_type = 'auto'
                        matched_deleted_b.creator_id = matched_deleted_b.user_id
                        matched_deleted_b.source_username = _get_username(matched_deleted_b.user_id)
                        all_banquets.append(matched_deleted_b)
                        deleted_banquets.remove(matched_deleted_b)
                        created_count += 1
                        continue
                    else:
                        continue
                else:
                    r.banquet_id = None

            owner_id = r.user_id or (user.id if user else 1)
            reason = (r.event_reason or '其它喜宴').strip()

            rec_year = current_year
            if r.created_at:
                rec_year = str(r.created_at.year)
            elif hasattr(r, 'date') and r.date:
                d_str = str(r.date).strip()
                if len(d_str) >= 4 and d_str[:4].isdigit():
                    rec_year = d_str[:4]

            key = (owner_id, reason, rec_year)
            if key not in groups:
                groups[key] = []
            groups[key].append(r)

        for (owner_id, reason, year_str), r_list in groups.items():
            target_b = None
            for b in all_banquets:
                if b.user_id == owner_id:
                    if b.event_type == reason or reason in (b.title or ''):
                        target_b = b
                        break

            if not target_b and deleted_banquets:
                deleted_match = None
                for db_item in deleted_banquets:
                    if db_item.user_id == owner_id:
                        if db_item.event_type == reason or reason in (db_item.title or '') or f"{year_str}年 {reason}" in (db_item.title or ''):
                            deleted_match = db_item
                            break
                if deleted_match:
                    if force_restore:
                        # 显式手动同步触发：恢复该专属台账
                        deleted_match.deleted_at = None
                        deleted_match.creator_type = 'auto'
                        deleted_match.creator_id = owner_id
                        deleted_match.source_username = _get_username(owner_id)
                        target_b = deleted_match
                        all_banquets.append(target_b)
                        deleted_banquets.remove(deleted_match)
                        created_count += 1
                    else:
                        continue

            if not target_b:
                source_user = _get_username(owner_id)
                target_b = Banquet(
                    title=f"{year_str}年 {reason}专属台账",
                    event_type=reason,
                    event_date=f"{year_str}-01-01",
                    venue="礼金账本收礼汇总",
                    budget=0.0,
                    banquet_cost=0.0,
                    notes=f"由系统自动归集【{year_str}年 {reason}】收礼记录生成",
                    user_id=owner_id,
                    creator_type='auto',
                    creator_id=owner_id,
                    source_username=source_user
                )
                db.session.add(target_b)
                db.session.flush()
                all_banquets.append(target_b)
                created_count += 1

            for r in r_list:
                if r.banquet_id != target_b.id:
                    r.banquet_id = target_b.id
                    linked_count += 1

        db.session.commit()
        return created_count, linked_count

    @app.route('/banquets', methods=['GET', 'POST'])
    @login_required
    def banquets_view():
        """宴席大账本列表与创建（自动从礼金账本获取并按办席原因生成）"""
        if request.method == 'POST':
            # V10.4: 级别1可创建自身宴席，移除 == 1 拦截
            title = request.form.get('title', '').strip()
            event_type = request.form.get('event_type', '婚宴').strip()
            event_date_str = request.form.get('event_date', '').strip()
            venue = request.form.get('venue', '').strip()
            budget = float(request.form.get('budget', 0) or 0)
            banquet_cost = float(request.form.get('banquet_cost', 0) or 0)
            notes = request.form.get('notes', '').strip()

            if not title:
                flash('宴席/活动名称不能为空！', 'danger')
                return redirect(url_for('banquets_view'))


            event_date = None
            if event_date_str:
                try:
                    event_date = datetime.strptime(event_date_str, '%Y-%m-%d').date()
                except Exception:
                    pass

            b = Banquet(
                title=title,
                event_type=event_type,
                event_date=event_date,
                venue=venue,
                budget=budget,
                banquet_cost=banquet_cost,
                notes=notes,
                user_id=current_user.id,
                creator_type='manual',
                creator_id=current_user.id,
                source_username=current_user.username
            )
            db.session.add(b)
            db.session.commit()
            safe_log('创建大账本', f"创建了专属宴席账本 [{title}]，办宴成本: {banquet_cost}元")
            try:
                trigger_webhook_event(
                    WebhookConfig.query.filter_by(is_enabled=True).all(), 'record_create',
                    f'创建宴席账本 [{title}]',
                    f'操作人：{current_user.username} | 页面：专属宴席 | 名称：{title} | 办宴成本：{banquet_cost}元',
                    page_key='banquets', user_name=current_user.username,
                    operator_id=current_user.id
                )
            except Exception:
                pass
            flash(f'成功创建宴席账本 [{title}]！', 'success')
            return redirect(url_for('banquets_view'))

        # 页面加载时自动从礼金账本获取数据并按办席原因自动生成/更新大账本
        try:
            sync_banquets_from_ledger(current_user)
        except Exception as e:
            print(f"[Banquet Auto Sync]: {e}")

        if get_accessible_banquets_query:
            query = get_accessible_banquets_query(current_user)
        else:
            query = Banquet.query.filter(Banquet.deleted_at.is_(None))
            if not (current_user.is_admin or getattr(current_user, 'can_view_others', False)):
                query = query.filter_by(user_id=current_user.id)

        banquets = query.order_by(Banquet.created_at.desc()).all()

        # 计算每个账本的收礼总额与净盈亏（基于用户有权访问的记录）
        banquet_stats = []
        for b in banquets:
            if get_accessible_records_query:
                recs = get_accessible_records_query(current_user, menu_key='banquets').filter(GiftRecord.banquet_id == b.id, GiftRecord.deleted_at.is_(None)).all()
            else:
                recs = GiftRecord.query.filter_by(banquet_id=b.id).filter(GiftRecord.deleted_at.is_(None)).all()
            total_income = sum(r.amount for r in recs if getattr(r, 'record_type', 'receive') != 'send')
            cost = b.banquet_cost or 0.0
            net_profit = total_income - cost
            banquet_stats.append({
                'banquet': b,
                'record_count': len(recs),
                'total_income': total_income,
                'banquet_cost': cost,
                'net_profit': net_profit
            })

        return render_template('banquets.html', banquet_stats=banquet_stats)

    @app.route('/banquets/sync', methods=['GET', 'POST'])
    @login_required
    def banquets_sync():
        """管理员或用户手动从礼金账本拉取数据同步专属大账本"""
        # V10.4: 级别1可同步自身宴席，移除 == 1 拦截
        try:
            created, linked = sync_banquets_from_ledger(current_user, force_restore=True)
            safe_log('同步专属大账本', f"从礼金账本手动拉取数据：自动同步/生成 {created} 个大账本，关联更新 {linked} 条记录")
            try:
                trigger_webhook_event(
                    WebhookConfig.query.filter_by(is_enabled=True).all(), 'sync',
                    f'同步宴席大账本',
                    f'操作人：{current_user.username} | 页面：专属宴席 | 新增/恢复 {created} 个大账本，关联更新 {linked} 条记录',
                    page_key='banquets', user_name=current_user.username,
                    operator_id=current_user.id
                )
            except Exception:
                pass
            flash(f'成功从礼金账本拉取数据同步！新增/恢复了 {created} 个专属大账本，关联更新 {linked} 条礼金记录。', 'success')
        except Exception as e:
            db.session.rollback()
            flash(f'同步专属大账本失败：{str(e)}', 'danger')
        return redirect(url_for('banquets_view'))

    @app.route('/banquet/<int:banquet_id>')
    @login_required
    def banquet_detail_view(banquet_id):
        """专属宴席大账本详情、现场快速录入台账与盈亏分析"""
        b = db.session.get(Banquet, banquet_id)
        if not b or b.deleted_at:
            flash('该宴席账本不存在或已被删除！', 'warning')
            return redirect(url_for('banquets_view'))

        if can_user_view_entity and not can_user_view_entity(current_user, b, 'banquets'):
            flash('您没有权限查看该专属宴席！', 'danger')
            return redirect(url_for('banquets_view'))

        if get_accessible_records_query:
            records = get_accessible_records_query(current_user).filter(GiftRecord.banquet_id == b.id, GiftRecord.deleted_at.is_(None)).order_by(GiftRecord.created_at.desc()).all()
        else:
            records = GiftRecord.query.filter_by(banquet_id=b.id).filter(GiftRecord.deleted_at.is_(None)).order_by(GiftRecord.created_at.desc()).all()
        total_income = sum(r.amount for r in records if getattr(r, 'record_type', 'receive') != 'send')
        cost = b.banquet_cost or 0.0
        net_profit = total_income - cost
        recovery_rate = (total_income / cost * 100) if cost > 0 else 0

        # 候选收礼记录（仅未删除、收礼类型、当前未归属于本宴席的记录供引入）
        if get_accessible_records_query:
            cand_query = get_accessible_records_query(current_user)
        else:
            cand_query = GiftRecord.query.filter(GiftRecord.deleted_at.is_(None))
            if not (current_user.is_admin or getattr(current_user, 'can_view_others', False)):
                cand_query = cand_query.filter_by(user_id=current_user.id)
        
        available_records = cand_query.filter(
            GiftRecord.record_type == 'receive',
            GiftRecord.deleted_at.is_(None),
            db.or_(GiftRecord.banquet_id.is_(None), GiftRecord.banquet_id != b.id)
        ).order_by(GiftRecord.created_at.desc()).all()

        # 分享链接信息
        share = SharedLedgerLink.query.filter_by(banquet_id=b.id, is_active=True).first()

        return render_template(
            'banquet_detail.html',
            banquet=b,
            records=records,
            available_records=available_records,
            total_income=total_income,
            banquet_cost=cost,
            net_profit=net_profit,
            recovery_rate=recovery_rate,
            share=share
        )

    @app.route('/banquet/edit/<int:banquet_id>', methods=['POST'])
    @login_required
    def banquet_edit(banquet_id):
        """编辑宴席账本基本信息与成本"""
        b = db.session.get(Banquet, banquet_id)
        if not b or b.deleted_at:
            flash('账本不存在', 'danger')
            return redirect(url_for('banquets_view'))

        if can_user_edit_entity and not can_user_edit_entity(current_user, b, 'banquets'):
            flash('您没有权限修改该专属宴席！', 'danger')
            return redirect(url_for('banquets_view'))

        b.title = request.form.get('title', b.title).strip()
        b.event_type = request.form.get('event_type', b.event_type).strip()
        b.venue = request.form.get('venue', b.venue).strip()
        try:
            b.budget = float(request.form.get('budget', b.budget) or 0)
            b.banquet_cost = float(request.form.get('banquet_cost', b.banquet_cost) or 0)
        except ValueError:
            pass
        b.notes = request.form.get('notes', b.notes).strip()
        db.session.commit()
        safe_log('修改大账本', f"更新了宴席账本 [{b.title}] 的信息与成本支出")
        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(), 'record_update',
                f'修改宴席账本 [{b.title}]',
                f'操作人：{current_user.username} | 页面：专属宴席 | 账本：{b.title}',
                page_key='banquets', user_name=current_user.username,
                operator_id=current_user.id
            )
        except Exception:
            pass
        flash('宴席账本已更新！', 'success')
        return redirect(url_for('banquet_detail_view', banquet_id=b.id))

    @app.route('/banquet/delete/<int:banquet_id>', methods=['POST'])
    @login_required
    def banquet_delete(banquet_id):
        """删除专属宴席大账本（软删除至回收站）"""
        b = db.session.get(Banquet, banquet_id)
        is_ajax = request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest' or 'application/json' in request.headers.get('Accept', '')
        if not b or b.deleted_at:
            if is_ajax:
                return jsonify({'code': 404, 'message': '宴席账本不存在或已被删除！'}), 404
            flash('宴席账本不存在或已被删除！', 'warning')
            return redirect(url_for('banquets_view'))

        if can_user_delete_entity and not can_user_delete_entity(current_user, b, 'banquets'):
            if is_ajax:
                return jsonify({'code': 403, 'message': '您没有权限删除该专属宴席！'}), 403
            flash('您没有权限删除该专属宴席！', 'danger')
            return redirect(url_for('banquets_view'))

        b_title = b.title
        b.deleted_at = datetime.now()
        db.session.commit()
        safe_log('删除大账本', f"软删除了宴席账本 ID #{banquet_id}: [{b_title}]")
        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(), 'record_delete',
                f'删除宴席账本 [{b_title}]',
                f'操作人：{current_user.username} | 页面：专属宴席 | 账本：{b_title}',
                page_key='banquets', user_name=current_user.username,
                operator_id=current_user.id
            )
        except Exception:
            pass
        if is_ajax:
            return jsonify({'code': 200, 'message': f'宴席账本 [{b_title}] 已成功移入回收站！'})
        flash(f'宴席账本 [{b_title}] 已成功移入回收站！', 'success')
        return redirect(url_for('banquets_view'))

    @app.route('/banquets/batch_delete', methods=['POST'])
    @login_required
    def banquets_batch_delete():
        """批量软删除专属宴席大账本至回收站"""
        is_ajax = request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest' or 'application/json' in request.headers.get('Accept', '')
        if hasattr(current_user, 'get_menu_perm') and current_user.get_menu_perm('banquets') in (2,):
            if is_ajax:
                return jsonify({'code': 403, 'message': '当前页面权限不允许删除专属宴席！'}), 403
            flash('当前页面权限不允许删除专属宴席！', 'danger')
            return redirect(url_for('banquets_view'))

        if request.is_json:
            data = request.get_json(silent=True) or {}
            banquet_ids = data.get('banquet_ids', [])
        else:
            banquet_ids = request.form.getlist('banquet_ids') or request.form.getlist('banquet_ids[]')

        if not banquet_ids:
            if is_ajax:
                return jsonify({'code': 400, 'message': '请至少选择一个要删除的专属宴席！'}), 400
            flash('请至少选择一个要删除的专属宴席！', 'warning')
            return redirect(url_for('banquets_view'))

        count = 0
        for b_id in banquet_ids:
            try:
                b_id_int = int(b_id)
                b = db.session.get(Banquet, b_id_int)
                if b and not b.deleted_at:
                    if can_user_delete_entity and not can_user_delete_entity(current_user, b, 'banquets'):
                        continue
                    b.deleted_at = datetime.now()
                    count += 1
            except (ValueError, TypeError):
                continue

        db.session.commit()
        safe_log('批量删除大账本', f"批量移入回收站了 {count} 个专属宴席")
        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(), 'batch_delete',
                f'批量删除 {count} 个宴席账本',
                f'操作人：{current_user.username} | 页面：专属宴席 | 数量：{count}',
                page_key='banquets', user_name=current_user.username,
                operator_id=current_user.id
            )
        except Exception:
            pass
        if is_ajax:
            return jsonify({'code': 200, 'message': f'成功将 {count} 个专属宴席移入回收站！', 'count': count})
        flash(f'成功将 {count} 个专属宴席移入回收站！', 'success')
        return redirect(url_for('banquets_view'))

    @app.route('/banquet/<int:banquet_id>/record/delete/<int:record_id>', methods=['POST'])
    @login_required
    def banquet_record_delete(banquet_id, record_id):
        """删除专属宴席中的单笔礼金明细（软删除至回收站）"""
        b = db.session.get(Banquet, banquet_id)
        is_ajax = request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest' or 'application/json' in request.headers.get('Accept', '')
        if not b or b.deleted_at:
            if is_ajax:
                return jsonify({'code': 404, 'message': '宴席账本不存在！'}), 404
            flash('专属宴席不存在或已被删除！', 'warning')
            return redirect(url_for('banquets_view'))

        r = db.session.get(GiftRecord, record_id)
        if not r or r.deleted_at:
            if is_ajax:
                return jsonify({'code': 404, 'message': '明细记录不存在或已被删除！'}), 404
            flash('该明细记录不存在或已被删除！', 'warning')
            return redirect(url_for('banquet_detail_view', banquet_id=banquet_id))

        if can_user_delete_entity and not can_user_delete_entity(current_user, r, 'banquets'):
            if is_ajax:
                return jsonify({'code': 403, 'message': '您没有权限删除该记录！'}), 403
            flash('您没有权限删除该记录！', 'danger')
            return redirect(url_for('banquet_detail_view', banquet_id=banquet_id))

        rec_name = r.name
        r.deleted_at = datetime.now()
        db.session.commit()
        safe_log('删除宴席明细', f"在专属宴席 [{b.title}] 中软删除了记录 ID #{record_id}: [{rec_name}]")
        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(), 'record_delete',
                f'删除宴席明细 [{rec_name}]',
                f'操作人：{current_user.username} | 页面：专属宴席 | 宴席：{b.title} | 客人：{rec_name}',
                page_key='banquets', user_name=current_user.username,
                operator_id=current_user.id
            )
        except Exception:
            pass
        if is_ajax:
            return jsonify({'code': 200, 'message': f'客人 [{rec_name}] 的记录已移入回收站！'})
        flash(f'客人 [{rec_name}] 的记录已成功移入回收站！', 'success')
        return redirect(url_for('banquet_detail_view', banquet_id=banquet_id))

    @app.route('/banquet/<int:banquet_id>/records/batch_delete', methods=['POST'])
    @login_required
    def banquet_records_batch_delete(banquet_id):
        """在专属宴席详情页批量软删除明细记录（移入回收站）"""
        b = db.session.get(Banquet, banquet_id)
        is_ajax = request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest' or 'application/json' in request.headers.get('Accept', '')
        if not b or b.deleted_at:
            if is_ajax:
                return jsonify({'code': 404, 'message': '宴席账本不存在！'}), 404
            flash('专属宴席不存在或已被删除！', 'warning')
            return redirect(url_for('banquets_view'))

        if hasattr(current_user, 'get_menu_perm') and current_user.get_menu_perm('banquets') in (2,):
            if is_ajax:
                return jsonify({'code': 403, 'message': '当前页面权限不允许删除记录！'}), 403
            flash('当前页面权限不允许删除记录！', 'danger')
            return redirect(url_for('banquet_detail_view', banquet_id=banquet_id))

        if request.is_json:
            data = request.get_json(silent=True) or {}
            record_ids = data.get('record_ids', [])
        else:
            record_ids = request.form.getlist('record_ids') or request.form.getlist('record_ids[]')

        if not record_ids:
            if is_ajax:
                return jsonify({'code': 400, 'message': '请至少选择一条要删除的明细记录！'}), 400
            flash('请至少选择一条要删除的明细记录！', 'warning')
            return redirect(url_for('banquet_detail_view', banquet_id=banquet_id))

        count = 0
        for r_id in record_ids:
            try:
                r_id_int = int(r_id)
                r = db.session.get(GiftRecord, r_id_int)
                if r and not r.deleted_at and r.banquet_id == b.id:
                    if can_user_delete_entity and not can_user_delete_entity(current_user, r, 'banquets'):
                        continue
                    r.deleted_at = datetime.now()
                    count += 1
            except (ValueError, TypeError):
                continue

        db.session.commit()
        safe_log('批量删除宴席明细', f"在专属宴席 [{b.title}] 中批量删除了 {count} 条明细至回收站")
        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(), 'batch_delete',
                f'批量删除宴席明细 {count} 条',
                f'操作人：{current_user.username} | 页面：专属宴席 | 宴席：{b.title} | 数量：{count}',
                page_key='banquets', user_name=current_user.username,
                operator_id=current_user.id
            )
        except Exception:
            pass
        if is_ajax:
            return jsonify({'code': 200, 'message': f'成功将选中的 {count} 笔明细移入回收站！', 'count': count})
        flash(f'成功将选中的 {count} 笔明细移入回收站！', 'success')
        return redirect(url_for('banquet_detail_view', banquet_id=banquet_id))

    @app.route('/banquet/<int:banquet_id>/records/batch_unlink', methods=['POST'])
    @login_required
    def banquet_records_batch_unlink(banquet_id):
        """在专属宴席详情页批量移出明细记录（解除关联，保留在主账本中）"""
        b = db.session.get(Banquet, banquet_id)
        is_ajax = request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest' or 'application/json' in request.headers.get('Accept', '')
        if not b or b.deleted_at:
            if is_ajax:
                return jsonify({'code': 404, 'message': '宴席账本不存在！'}), 404
            flash('专属宴席不存在或已被删除！', 'warning')
            return redirect(url_for('banquets_view'))

        if (can_user_edit_entity and not can_user_edit_entity(current_user, b, 'banquets')):
            if is_ajax:
                return jsonify({'code': 403, 'message': '当前页面权限不允许此操作！'}), 403
            flash('当前页面权限不允许此操作！', 'danger')
            return redirect(url_for('banquet_detail_view', banquet_id=banquet_id))

        if request.is_json:
            data = request.get_json(silent=True) or {}
            record_ids = data.get('record_ids', [])
        else:
            record_ids = request.form.getlist('record_ids') or request.form.getlist('record_ids[]')

        if not record_ids:
            if is_ajax:
                return jsonify({'code': 400, 'message': '请至少选择一条要移出的明细记录！'}), 400
            flash('请至少选择一条要移出的明细记录！', 'warning')
            return redirect(url_for('banquet_detail_view', banquet_id=banquet_id))

        count = 0
        for r_id in record_ids:
            try:
                r_id_int = int(r_id)
                r = db.session.get(GiftRecord, r_id_int)
                if r and not r.deleted_at and r.banquet_id == b.id:
                    r.banquet_id = None
                    count += 1
            except (ValueError, TypeError):
                continue

        db.session.commit()
        safe_log('批量移出宴席明细', f"从专属宴席 [{b.title}] 中批量移出了 {count} 笔记录（保留在主账本中）")
        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(), 'update',
                f'批量移出宴席明细 {count} 笔',
                f'操作人：{current_user.username} | 页面：专属宴席 | 宴席：{b.title} | 移出数量：{count}',
                page_key='banquets', user_name=current_user.username,
                operator_id=current_user.id
            )
        except Exception:
            pass
        if is_ajax:
            return jsonify({'code': 200, 'message': f'成功将选中的 {count} 笔明细移出此专属宴席（数据仍保留在主账本）！', 'count': count})
        flash(f'成功将选中的 {count} 笔明细移出此专属宴席（数据仍保留在主账本）！', 'success')
        return redirect(url_for('banquet_detail_view', banquet_id=banquet_id))

    @app.route('/banquet/<int:banquet_id>/quick_add', methods=['POST'])
    @login_required
    def banquet_quick_add(banquet_id):
        """现场快速录入收礼台账（支持 AJAX 与原生表单无缝双向兼容）"""
        b = db.session.get(Banquet, banquet_id)
        is_ajax = request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest' or 'application/json' in request.headers.get('Accept', '')
        if not b or b.deleted_at:
            if is_ajax:
                return jsonify({'code': 404, 'message': '宴席账本不存在'}), 404
            flash('宴席账本不存在或已被删除！', 'danger')
            return redirect(url_for('banquets_view'))

        if (can_user_edit_entity and not can_user_edit_entity(current_user, b, 'banquets')):
            if is_ajax:
                return jsonify({'code': 403, 'message': '当前页面权限不允许向此专属宴席登记收礼！'}), 403
            flash('当前页面权限不允许向此专属宴席登记收礼！', 'danger')
            return redirect(url_for('banquet_detail_view', banquet_id=banquet_id))

        name = request.form.get('name', '').strip()
        amount_str = request.form.get('amount', '').strip()
        phone = request.form.get('phone', '').strip()
        address = request.form.get('address', '').strip()
        notes = request.form.get('notes', '').strip()

        if not name or not amount_str:
            if is_ajax:
                return jsonify({'code': 400, 'message': '客人姓名与礼金金额为必填项！'}), 400
            flash('客人姓名与礼金金额为必填项！', 'danger')
            return redirect(url_for('banquet_detail_view', banquet_id=b.id))

        try:
            amount = cn2num(amount_str)
            if amount <= 0:
                raise ValueError
        except Exception:
            if is_ajax:
                return jsonify({'code': 400, 'message': '请输入有效的礼金金额！'}), 400
            flash('请输入有效的礼金金额！', 'danger')
            return redirect(url_for('banquet_detail_view', banquet_id=b.id))

        record = GiftRecord(
            name=name,
            amount=amount,
            phone=phone,
            address=address,
            event_reason=b.event_type or b.title,
            record_type='receive',
            notes=notes,
            banquet_id=b.id,
            user_id=current_user.id
        )
        db.session.add(record)
        db.session.commit()
        safe_log('现场快速录入', f"在宴席 [{b.title}] 中录入: [{name}] 金额: {amount}元")
        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(), 'record_create',
                f'宴席快速录入 [{name}] ¥{amount:.2f}',
                f'操作人：{current_user.username} | 页面：专属宴席 | 宴席：{b.title} | 客人：{name} | 金额：{amount}元',
                page_key='banquets', user_name=current_user.username,
                operator_id=current_user.id
            )
        except Exception:
            pass
        if not is_ajax:
            flash(f"成功登记客人 [{name}] 礼金 ¥{amount:.2f}！", 'success')
            return redirect(url_for('banquet_detail_view', banquet_id=b.id))
        return jsonify({
            'code': 200,
            'message': f"成功录入 [{name}] 礼金 ¥{amount:.2f}！",
            'record': {
                'id': record.id,
                'name': record.name,
                'amount': record.amount,
                'phone': record.phone or '',
                'address': record.address or '',
                'notes': record.notes or '',
                'created_at': record.created_at.strftime('%Y-%m-%d %H:%M')
            }
        })

    @app.route('/banquet/<int:banquet_id>/export_excel')
    @login_required
    def banquet_export_excel(banquet_id):
        """专属宴席一键导出台账 Excel / CSV"""
        b = db.session.get(Banquet, banquet_id)
        if not b or b.deleted_at:
            flash('账本不存在', 'danger')
            return redirect(url_for('banquets_view'))

        if can_user_edit_entity and not can_user_edit_entity(current_user, b, 'banquets'):
            flash('您没有权限修改该专属宴席！', 'danger')
            return redirect(url_for('banquets_view'))

        records = GiftRecord.query.filter_by(banquet_id=b.id).filter(GiftRecord.deleted_at.is_(None)).order_by(GiftRecord.created_at.asc()).all()
        total_income = sum(r.amount for r in records if getattr(r, 'record_type', 'receive') != 'send')
        cost = b.banquet_cost or 0.0

        export_format = request.args.get('format', 'xlsx').strip().lower()

        if export_format == 'csv':
            # CSV 导出
            output = io.StringIO()
            output.write('\ufeff')
            import csv as _csv_mod
            writer = _csv_mod.writer(output)
            writer.writerow([f"【{b.title}】收礼台账与盈亏简报"])
            writer.writerow([f"活动类型: {b.event_type or '宴席'}", f"举办日期: {b.event_date or '未定'}", f"地点: {b.venue or '无'}"])
            writer.writerow([f"总收礼金额: ¥{total_income:.2f}", f"办宴成本: ¥{cost:.2f}", f"净盈亏: ¥{total_income - cost:.2f}"])
            writer.writerow([])
            writer.writerow(['序号', '客人姓名', '礼金金额(元)', '联系电话', '联系地址', '备注说明', '登记时间'])
            for idx, r in enumerate(records, start=1):
                writer.writerow([idx, r.name, f"{r.amount:.2f}", r.phone or '', r.address or '', r.notes or '', r.created_at.strftime('%Y-%m-%d %H:%M') if r.created_at else ''])
            response = Response(output.getvalue(), mimetype='text/csv; charset=utf-8')
            filename = f"banquet_{b.id}_{b.title}_ledger.csv"
            response.headers['Content-Disposition'] = f"attachment; filename={urllib.parse.quote(filename)}"
        else:
            # Excel 导出（openpyxl 真实 .xlsx）
            try:
                from openpyxl import Workbook
                from openpyxl.utils import get_column_letter
            except ImportError:
                flash('Excel 导出需要 openpyxl 库。', 'danger')
                return redirect(url_for('banquet_detail_view', banquet_id=banquet_id))

            wb = Workbook()
            ws = wb.active
            ws.title = b.title[:31] if b.title else '台账'
            # 标题行
            ws.append([f"【{b.title}】收礼台账与盈亏简报"])
            ws.append([f"活动类型: {b.event_type or '宴席'}", f"举办日期: {b.event_date or '未定'}", f"地点: {b.venue or '无'}"])
            ws.append([f"总收礼金额: ¥{total_income:.2f}", f"办宴成本: ¥{cost:.2f}", f"净盈亏: ¥{total_income - cost:.2f}"])
            ws.append([])
            # 表头
            headers = ['序号', '客人姓名', '礼金金额(元)', '联系电话', '联系地址', '备注说明', '登记时间']
            ws.append(headers)
            for idx, r in enumerate(records, start=1):
                ws.append([idx, r.name, float(r.amount), r.phone or '', r.address or '', r.notes or '', r.created_at.strftime('%Y-%m-%d %H:%M') if r.created_at else ''])
            # 自动列宽
            for col_idx in range(1, len(headers) + 1):
                max_len = len(str(headers[col_idx - 1]))
                for row_idx in range(5, 5 + len(records)):
                    cell = ws.cell(row=row_idx, column=col_idx)
                    cell_len = len(str(cell.value)) if cell.value else 0
                    if cell_len > max_len:
                        max_len = cell_len
                ws.column_dimensions[get_column_letter(col_idx)].width = min(max_len + 4, 40)

            output = io.BytesIO()
            wb.save(output)
            output.seek(0)
            response = Response(output.getvalue(), mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
            filename = f"banquet_{b.id}_{b.title}_ledger.xlsx"
            response.headers['Content-Disposition'] = f"attachment; filename={urllib.parse.quote(filename)}"

        safe_log('导出宴席台账', f"导出了宴席 [{b.title}] 的全部礼金记录（{export_format.upper()}）")
        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(), 'security',
                f'导出宴席台账',
                f'操作人：{current_user.username} | 页面：专属宴席 | 宴席：{b.title} | 记录数：{len(records)} | 格式：{export_format.upper()}',
                page_key='banquets', user_name=current_user.username,
                operator_id=current_user.id
            )
        except Exception:
            pass
        return response

    @app.route('/banquet/<int:banquet_id>/import_records', methods=['POST'])
    @login_required
    def banquet_import_records(banquet_id):
        """从礼金账本批量引入收礼明细记录到当前宴席"""
        b = db.session.get(Banquet, banquet_id)
        if not b or b.deleted_at:
            flash('专属宴席不存在或已被删除！', 'warning')
            return redirect(url_for('banquets_view'))
        if (can_user_edit_entity and not can_user_edit_entity(current_user, b, 'banquets')):
            flash('当前页面权限不允许引入记录！', 'danger')
            return redirect(url_for('banquet_detail_view', banquet_id=banquet_id))

        record_ids = request.form.getlist('record_ids') or request.form.getlist('record_ids[]')
        if not record_ids:
            flash('请勾选需要引入的礼金记录！', 'warning')
            return redirect(url_for('banquet_detail_view', banquet_id=banquet_id))

        count = 0
        for rid_str in record_ids:
            try:
                rid = int(rid_str)
                r = db.session.get(GiftRecord, rid)
                if r and not r.deleted_at and getattr(r, 'record_type', 'receive') == 'receive':
                    if can_user_edit_entity and not can_user_edit_entity(current_user, r, 'ledger'):
                        continue
                    r.banquet_id = b.id
                    count += 1
            except Exception:
                continue

        if count > 0:
            db.session.commit()
            safe_log('引入宴席明细', f"为专属宴席 [{b.title}] 引入了 {count} 笔收礼记录")
            try:
                trigger_webhook_event(
                    WebhookConfig.query.filter_by(is_enabled=True).all(), 'record_create',
                    f'引入宴席明细 {count} 笔',
                    f'操作人：{current_user.username} | 页面：专属宴席 | 宴席：{b.title} | 引入数量：{count}',
                    page_key='banquets', user_name=current_user.username,
                    operator_id=current_user.id
                )
            except Exception:
                pass
            flash(f'成功从礼金账本引入 {count} 笔收礼明细！', 'success')
        else:
            flash('未成功引入任何记录（仅收礼记录且有编辑权限的记录支持引入）！', 'warning')
        return redirect(url_for('banquet_detail_view', banquet_id=banquet_id))

    @app.route('/banquet/<int:banquet_id>/unlink_record/<int:record_id>', methods=['POST'])
    @login_required
    def banquet_unlink_record(banquet_id, record_id):
        """将明细记录移出当前宴席（解除关联，置 banquet_id=None，保留在主账本中）"""
        b = db.session.get(Banquet, banquet_id)
        if not b or b.deleted_at:
            flash('专属宴席不存在！', 'warning')
            return redirect(url_for('banquets_view'))
        if (can_user_edit_entity and not can_user_edit_entity(current_user, b, 'banquets')):
            flash('当前页面权限不允许移出记录！', 'danger')
            return redirect(url_for('banquet_detail_view', banquet_id=banquet_id))

        r = db.session.get(GiftRecord, record_id)
        if r and r.banquet_id == b.id:
            r.banquet_id = None
            db.session.commit()
            safe_log('移出宴席明细', f"将客人 [{r.name}] 的记录移出专属宴席 [{b.title}]（保留在主账本中）")
            # V10.3: 补充移出宴席明细推送
            try:
                trigger_webhook_event(
                    WebhookConfig.query.filter_by(is_enabled=True).all(), 'update',
                    f'{current_user.username} 移出宴席明细',
                    f'操作人：{current_user.username} | 页面：宴席管理 | 客人：{r.name} | 宴席：{b.title} | 操作：移出',
                    page_key='banquets', user_name=current_user.username,
                    operator_id=current_user.id
                )
            except Exception:
                pass
            flash(f'已将客人 [{r.name}] 的记录移出专属宴席（主账本数据仍完好保留）！', 'success')
        return redirect(url_for('banquet_detail_view', banquet_id=banquet_id))

    @app.route('/banquet/<int:banquet_id>/share', methods=['POST'])
    @login_required
    def banquet_share(banquet_id):
        """生成或配置宴席账本免登录只读分享链接（支持自定义有效期、自动更新新链接、停留在当前窗口）"""
        b = db.session.get(Banquet, banquet_id)
        is_ajax = request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest' or 'application/json' in request.headers.get('Accept', '')
        if not b or b.deleted_at:
            if is_ajax:
                return jsonify({'code': 404, 'success': False, 'message': '宴席不存在'}), 404
            flash('宴席不存在', 'danger')
            return redirect(url_for('banquets_view'))

        if (can_user_edit_entity and not can_user_edit_entity(current_user, b, 'banquets')):
            if is_ajax:
                return jsonify({'code': 403, 'success': False, 'message': '当前页面权限不允许配置分享！'}), 403
            flash('当前页面权限不允许配置分享！', 'danger')
            return redirect(url_for('banquet_detail_view', banquet_id=banquet_id))

        import secrets
        password = request.form.get('access_password', '').strip()
        expire_days = request.form.get('expire_days', 'permanent').strip()
        hide_notes = bool(request.form.get('hide_notes'))
        hide_amount = bool(request.form.get('hide_amount'))

        expires_at = None
        expire_label = '永久有效'
        if expire_days == '1':
            expires_at = datetime.now() + timedelta(days=1)
            expire_label = '1天内有效'
        elif expire_days == '7':
            expires_at = datetime.now() + timedelta(days=7)
            expire_label = '7天内有效'
        elif expire_days == '30':
            expires_at = datetime.now() + timedelta(days=30)
            expire_label = '30天内有效'

        # 点击生成/更新时，重新生成全新的 token，确保链接地址真正更新发生改变
        new_token = secrets.token_urlsafe(16)
        share = SharedLedgerLink.query.filter_by(banquet_id=b.id).first()
        if not share:
            share = SharedLedgerLink(
                share_token=new_token,
                title=f"{b.title} 只读礼金单",
                user_id=current_user.id,
                banquet_id=b.id,
                access_password=password,
                hide_notes=hide_notes,
                hide_amount=hide_amount,
                expires_at=expires_at,
                is_active=True
            )
            db.session.add(share)
        else:
            share.share_token = new_token
            share.access_password = password
            share.hide_notes = hide_notes
            share.hide_amount = hide_amount
            share.expires_at = expires_at
            share.is_active = True

        db.session.commit()
        share_url = url_for('shared_ledger_view', token=share.share_token, _external=True)
        safe_log('配置分享链接', f"更新了宴席 [{b.title}] 的专属分享链接 (有效期: {expire_label})")
        # V10.3: 补充宴席分享链接配置推送
        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(), 'update',
                f'{current_user.username} 配置分享链接',
                f'操作人：{current_user.username} | 页面：宴席管理 | 宴席：{b.title} | 操作：配置分享链接 | 有效期：{expire_label}',
                page_key='banquets', user_name=current_user.username, operator_id=current_user.id
            )
        except Exception:
            pass

        if not is_ajax:
            flash(f'专属免登录只读分享链接已更新并生效（有效期: {expire_label}）！', 'success')
            return redirect(url_for('banquet_detail_view', banquet_id=b.id))
        return jsonify({
            'code': 200,
            'success': True,
            'message': f'专属免登录只读分享链接已成功生成并更新（有效期: {expire_label}）！',
            'share_url': share_url,
            'token': share.share_token,
            'expire_label': expire_label,
            'expires_at_str': expires_at.strftime('%Y-%m-%d %H:%M') if expires_at else '永久有效',
            'has_password': bool(password)
        })

    @app.route('/banquet/<int:banquet_id>/share/delete', methods=['POST'])
    @login_required
    def banquet_share_delete(banquet_id):
        """删除/作废宴席账本免登录只读分享链接"""
        b = db.session.get(Banquet, banquet_id)
        is_ajax = request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest' or 'application/json' in request.headers.get('Accept', '')
        if not b or b.deleted_at:
            if is_ajax:
                return jsonify({'code': 404, 'success': False, 'message': '宴席不存在'}), 404
            flash('宴席不存在', 'danger')
            return redirect(url_for('banquets_view'))

        if (can_user_edit_entity and not can_user_edit_entity(current_user, b, 'banquets')):
            if is_ajax:
                return jsonify({'code': 403, 'success': False, 'message': '当前页面权限不允许删除分享！'}), 403
            flash('当前页面权限不允许删除分享！', 'danger')
            return redirect(url_for('banquet_detail_view', banquet_id=banquet_id))

        share = SharedLedgerLink.query.filter_by(banquet_id=b.id).first()
        if share:
            db.session.delete(share)
            db.session.commit()
            safe_log('删除分享链接', f"删除了宴席 [{b.title}] 的免登录分享链接")
            # V10.3: 补充删除分享链接推送
            try:
                trigger_webhook_event(
                    WebhookConfig.query.filter_by(is_enabled=True).all(), 'delete',
                    f'{current_user.username} 删除分享链接',
                    f'操作人：{current_user.username} | 页面：宴席管理 | 宴席：{b.title} | 操作：删除分享链接',
                    page_key='banquets', user_name=current_user.username, operator_id=current_user.id
                )
            except Exception:
                pass

        if not is_ajax:
            flash('专属分享链接已成功删除作废！', 'success')
            return redirect(url_for('banquet_detail_view', banquet_id=b.id))
        return jsonify({'code': 200, 'success': True, 'message': '专属分享链接已成功删除作废！'})




    @app.route('/reminders', methods=['GET', 'POST'])
    @login_required
    def reminders_view():
        """纪念日备忘列表与添加"""
        if request.method == 'POST':
            # V10.4: 级别1可创建自身纪念日，移除 == 1 拦截
            name = request.form.get('name', '').strip()
            relation = request.form.get('relation', '').strip()
            phone = request.form.get('phone', '').strip()
            target_date_str = request.form.get('target_date', '').strip()
            anniversary_type = request.form.get('anniversary_type', '生日').strip()
            advance_days = int(request.form.get('advance_days', 3) or 3)
            notes = request.form.get('notes', '').strip()

            if not name or not target_date_str:
                flash('姓名与纪念日日期不能为空！', 'danger')
                return redirect(url_for('reminders_view'))

            rem = AnniversaryReminder(
                name=name,
                relation=relation,
                phone=phone,
                target_date=target_date_str,
                anniversary_type=anniversary_type,
                advance_days=advance_days,
                notes=notes,
                user_id=current_user.id
            )
            db.session.add(rem)
            db.session.commit()
            safe_log('添加纪念日', f"添加了 [{name}] 的 {anniversary_type} 提醒（提前 {advance_days} 天）")
            try:
                trigger_webhook_event(
                    WebhookConfig.query.filter_by(is_enabled=True).all(), 'record_create',
                    f'添加纪念日 [{name}]',
                    f'操作人：{current_user.username} | 页面：纪念日备忘 | 姓名：{name} | 类型：{anniversary_type} | 提前 {advance_days} 天',
                    page_key='reminders', user_name=current_user.username,
                    operator_id=current_user.id
                )
            except Exception:
                pass
            try:
                triggered = check_and_trigger_due_reminders(current_app._get_current_object(), specific_reminder=rem)
                if triggered:
                    flash(f'成功添加 [{name}] 的纪念日提醒，该纪念日已进入预警期，系统已自动向 Webhook 机器人推送提醒通知！', 'success')
                else:
                    flash(f'成功添加 [{name}] 的纪念日提醒！', 'success')
            except Exception as e:
                flash(f'成功添加 [{name}] 的纪念日提醒！', 'success')
            return redirect(url_for('reminders_view'))

        query = get_accessible_reminders_query(current_user) if get_accessible_reminders_query else AnniversaryReminder.query.filter_by(user_id=current_user.id)
        query = query.filter(AnniversaryReminder.deleted_at.is_(None), AnniversaryReminder.is_active == True)
        search = request.args.get('search', '').strip()
        if search:
            search_pat = f"%{search}%"
            query = query.filter(db.or_(
                AnniversaryReminder.name.ilike(search_pat),
                AnniversaryReminder.relation.ilike(search_pat),
                AnniversaryReminder.anniversary_type.ilike(search_pat),
                AnniversaryReminder.notes.ilike(search_pat),
                AnniversaryReminder.phone.ilike(search_pat)
            ))
        reminders = query.all()
        today = datetime.now().date()
        reminders_data = []
        for r in reminders:
            next_date, days_left = parse_target_date_obj(r.target_date, today)
            if not next_date:
                continue
            adv_days = r.advance_days or 3
            is_upcoming = (0 <= days_left <= adv_days)
            reminders_data.append({
                'reminder': r,
                'next_date': next_date,
                'days_left': days_left,
                'is_upcoming': is_upcoming
            })
        reminders_data.sort(key=lambda x: x['days_left'])
        available_webhooks = WebhookConfig.query.filter_by(is_enabled=True).all()
        return render_template('reminders.html', reminders_data=reminders_data, reminders=reminders, search=search, available_webhooks=available_webhooks)

    @app.route('/reminder/edit/<int:reminder_id>', methods=['POST'])
    @login_required
    def reminder_edit(reminder_id):
        """编辑纪念日备忘"""
        rem = db.session.get(AnniversaryReminder, reminder_id)
        if not rem:
            flash('纪念日记录不存在！', 'danger')
            return redirect(url_for('reminders_view'))

        if can_user_edit_entity and not can_user_edit_entity(current_user, rem, 'reminders'):
            flash('您没有权限修改该纪念日！', 'danger')
            return redirect(url_for('reminders_view'))

        rem.name = request.form.get('name', rem.name).strip()
        rem.relation = request.form.get('relation', rem.relation).strip()
        rem.phone = request.form.get('phone', rem.phone).strip()
        target_date_str = request.form.get('target_date', '').strip()
        if target_date_str and target_date_str != rem.target_date:
            rem.target_date = target_date_str
            rem.last_notified_target = None
        rem.anniversary_type = request.form.get('anniversary_type', rem.anniversary_type).strip()
        try:
            rem.advance_days = int(request.form.get('advance_days', rem.advance_days) or 3)
        except Exception:
            pass
        rem.notes = request.form.get('notes', rem.notes).strip()
        db.session.commit()
        safe_log('修改纪念日', f"修改了亲友 [{rem.name}] 的纪念日信息")
        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(), 'record_update',
                f'修改纪念日 [{rem.name}]',
                f'操作人：{current_user.username} | 页面：纪念日备忘 | 姓名：{rem.name}',
                page_key='reminders', user_name=current_user.username,
                operator_id=current_user.id
            )
        except Exception:
            pass
        try:
            triggered = check_and_trigger_due_reminders(current_app._get_current_object(), specific_reminder=rem)
            if triggered:
                flash(f'亲友 [{rem.name}] 的纪念日提醒已更新，且已处于预警期内，系统已自动向 Webhook 机器人推送提醒通知！', 'success')
            else:
                flash(f'亲友 [{rem.name}] 的纪念日提醒已更新！', 'success')
        except Exception:
            flash(f'亲友 [{rem.name}] 的纪念日提醒已更新！', 'success')
        return redirect(url_for('reminders_view'))

    @app.route('/reminder/delete/<int:reminder_id>', methods=['POST'])
    @login_required
    def reminder_delete(reminder_id):
        """删除纪念日（移入回收站）"""
        rem = db.session.get(AnniversaryReminder, reminder_id)
        if not rem:
            flash('纪念日记录不存在！', 'warning')
            return redirect(url_for('reminders_view'))
        if can_user_delete_entity and not can_user_delete_entity(current_user, rem, 'reminders'):
            flash('您没有权限删除此纪念日！', 'danger')
            return redirect(url_for('reminders_view'))
        rem.deleted_at = datetime.now()
        db.session.commit()
        safe_log('移入回收站', f"软删除了亲友 [{rem.name}] 的纪念日 (ID #{reminder_id})", user=current_user)
        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(), 'record_delete',
                f'删除纪念日 [{rem.name}]',
                f'操作人：{current_user.username} | 页面：纪念日备忘 | 姓名：{rem.name}',
                page_key='reminders', user_name=current_user.username,
                operator_id=current_user.id
            )
        except Exception:
            pass
        flash(f'亲友 [{rem.name}] 的纪念日已移入回收站！', 'success')
        return redirect(url_for('reminders_view'))

    @app.route('/reminders/batch_delete', methods=['POST'])
    @login_required
    def reminders_batch_delete():
        """批量软删除纪念日"""
        if hasattr(current_user, 'get_menu_perm') and current_user.get_menu_perm('reminders') in (2,):
            flash('当前页面权限不允许删除纪念日提醒！', 'danger')
            return redirect(url_for('reminders_view'))
        reminder_ids = request.form.getlist('reminder_ids') or request.form.getlist('reminder_ids[]')
        if not reminder_ids:
            flash('请勾选需要删除的纪念日！', 'warning')
            return redirect(url_for('reminders_view'))
        count = 0
        now = datetime.now()
        for rid_str in reminder_ids:
            try:
                rid = int(rid_str)
                rem = db.session.get(AnniversaryReminder, rid)
                if rem and (not can_user_delete_entity or can_user_delete_entity(current_user, rem, 'reminders')):
                    rem.deleted_at = now
                    count += 1
            except Exception:
                continue
        if count > 0:
            db.session.commit()
            safe_log('批量移入回收站', f"批量软删除了 {count} 条纪念日记录", user=current_user)
            try:
                trigger_webhook_event(
                    WebhookConfig.query.filter_by(is_enabled=True).all(), 'batch_delete',
                f'批量删除 {count} 条纪念日',
                f'操作人：{current_user.username} | 页面：纪念日备忘 | 数量：{count}',
                page_key='reminders', user_name=current_user.username,
                operator_id=current_user.id
            )
            except Exception:
                pass
            flash(f'已成功将选中的 {count} 条纪念日移入回收站！', 'success')
        else:
            flash('未找到可删除的纪念日记录！', 'warning')
        return redirect(url_for('reminders_view'))

    @app.route('/api/reminders/trigger_push', methods=['POST'])
    @login_required
    def api_trigger_reminder_push():
        """手动或外部触发即将到期的亲友纪念日 Webhook 推送"""
        # V10.4: 级别1可发起推送提醒，移除 == 1 拦截
        query = get_accessible_reminders_query(current_user) if get_accessible_reminders_query else AnniversaryReminder.query.filter_by(user_id=current_user.id)
        rems = query.filter(AnniversaryReminder.deleted_at.is_(None), AnniversaryReminder.is_active == True).all()
        today = datetime.now().date()
        upcoming = []
        for r in rems:
            this_year_date, days_left = parse_target_date_obj(r.target_date, today)
            if not this_year_date:
                continue
            adv_days = r.advance_days or 3
            if 0 <= days_left <= adv_days:
                upcoming.append((r, days_left, this_year_date))

        webhooks = WebhookConfig.query.filter_by(is_enabled=True).all()
        if not webhooks:
            return jsonify({
                'code': 400,
                'message': '当前未启用任何 Webhook 机器人，请先在【系统管理 -> Webhook 通知】中配置并启用企微/钉钉/飞书/Server酱通道！'
            }), 400

        if not upcoming:
            if rems:
                all_with_days = []
                for r in rems:
                    this_year_date, days_left = parse_target_date_obj(r.target_date, today)
                    if this_year_date:
                        all_with_days.append((r, days_left, this_year_date))
                all_with_days.sort(key=lambda x: x[1])
                nearest = all_with_days[:3]
                title, detail_msg, lines = format_reminder_notification_content(nearest)
                trigger_webhook_event(webhooks, 'reminder', f"{title}（测试推送最近 {len(nearest)} 条）", detail_msg, page_key='reminders', operator_id=current_user.id)
                safe_log('推送纪念日提醒', f"Webhook 测试推送了最近 {len(nearest)} 条纪念日", user=current_user)
                return jsonify({
                    'code': 200,
                    'message': f'当前无预警期内的临近纪念日，已向启用的 Webhook 成功测试推送最近 {len(nearest)} 条亲友纪念日！',
                    'count': len(nearest),
                    'details': lines
                })
            return jsonify({'code': 200, 'message': '当前暂无任何有效的亲友纪念日记录，请先添加纪念日！', 'count': 0})

        title, detail_msg, lines = format_reminder_notification_content(upcoming)
        trigger_webhook_event(webhooks, 'reminder', title, detail_msg, page_key='reminders', operator_id=current_user.id)
        safe_log('推送纪念日提醒', f"向 Webhook 推送了 {len(upcoming)} 条即将到期的纪念日", user=current_user)

        unbound_names = []
        for wh in webhooks:
            if wh.connection_type == 'long_connection':
                cid = extract_chatid_from_url(wh.webhook_url) or _cached_chatids.get(wh.bot_id)
                if not cid:
                    unbound_names.append(wh.channel_name)

        push_msg = f'成功向 Webhook 通道推送 {len(upcoming)} 条即将到期的亲友纪念日提醒！'
        if unbound_names:
            push_msg += f'【提示：检测到长连接机器人 [{", ".join(unbound_names)}] 尚未配置目标会话 chatid，请在管理后台填入群聊 chatid 或在企微群内 @机器人 一次完成绑定】'

        return jsonify({
            'code': 200,
            'message': push_msg,
            'count': len(upcoming),
            'details': lines
        })

    @app.route('/api/reminders/custom_push', methods=['POST'])
    @login_required
    def api_custom_reminder_push():
        """用户对单条或多条记录发起自定义手动推送提醒（可设置提醒次数与间隔时长）"""
        if hasattr(current_user, 'can_view_menu') and not current_user.can_view_menu('reminders'):
            return jsonify({'code': 403, 'message': '您没有【纪念日备忘】页面的访问权限！'}), 403
        # V10.4: 级别1可发起自定义推送，移除 == 1 拦截
        data = request.get_json(silent=True) or {}
        reminder_ids = data.get('reminder_ids') or []
        if not reminder_ids and request.form.get('reminder_ids'):
            try:
                reminder_ids = [int(x.strip()) for x in request.form.get('reminder_ids').split(',') if x.strip().isdigit()]
            except Exception:
                reminder_ids = []

        repeat_count = int(data.get('repeat_count') or request.form.get('repeat_count') or 1)
        interval_seconds = int(data.get('interval_seconds') or request.form.get('interval_seconds') or 0)
        custom_note = (data.get('custom_note') or request.form.get('custom_note') or '').strip()

        repeat_count = max(1, min(5, repeat_count))
        interval_seconds = max(0, min(300, interval_seconds))

        query = get_accessible_reminders_query(current_user) if get_accessible_reminders_query else AnniversaryReminder.query.filter_by(user_id=current_user.id)
        query = query.filter(AnniversaryReminder.deleted_at.is_(None), AnniversaryReminder.is_active == True)

        if reminder_ids:
            rems = query.filter(AnniversaryReminder.id.in_(reminder_ids)).all()
        else:
            rems = query.all()

        if not rems:
            return jsonify({'code': 400, 'message': '未找到有效的纪念日记录，请确认记录是否存在或已被删除！'}), 400

        webhook_ids = data.get('webhook_ids') or []
        if not webhook_ids and request.form.get('webhook_ids'):
            try:
                webhook_ids = [int(x.strip()) for x in request.form.get('webhook_ids').split(',') if x.strip().isdigit()]
            except Exception:
                webhook_ids = []

        if webhook_ids:
            webhooks = WebhookConfig.query.filter(WebhookConfig.id.in_(webhook_ids), WebhookConfig.is_enabled == True).all()
        else:
            webhooks = WebhookConfig.query.filter_by(is_enabled=True).all()

        if not webhooks:
            return jsonify({'code': 400, 'message': '未选择或未找到启用的 Webhook 通知通道，请在弹窗中勾选至少一个有效通道！'}), 400

        today = datetime.now().date()
        rem_with_days = []
        for r in rems:
            next_date, days_left = parse_target_date_obj(r.target_date, today)
            if next_date:
                rem_with_days.append((r, days_left, next_date))

        if not rem_with_days:
            return jsonify({'code': 400, 'message': '所选纪念日的日期格式无效，无法计算到期时间！'}), 400

        custom_content = (data.get('custom_content') or request.form.get('custom_content') or '').strip()
        title, default_md, text_summary = format_reminder_notification_content(rem_with_days)
        if custom_content:
            md_detail = custom_content
        else:
            md_detail = default_md
            if custom_note:
                md_detail += "\n\n> 💡 **发起人特别附言**：" + custom_note
        if custom_note and custom_note not in md_detail:
            md_detail += "\n\n> 💡 **发起人特别附言**：" + custom_note

        channel_names = [w.channel_name for w in webhooks]
        channel_str = '、'.join(channel_names)

        # V10.9: 重构为通过 trigger_webhook_event 统一推送管道
        # 保留多轮推送与间隔逻辑，每轮调用 trigger_webhook_event
        def _execute_push_round(round_idx, total_rounds):
            sub_title = title if total_rounds == 1 else f"{title} (第{round_idx}/{total_rounds}次提醒)"
            final_detail = md_detail
            if total_rounds > 1:
                final_detail = f"（第{round_idx}/{total_rounds}次提醒）\n{md_detail}"
            trigger_webhook_event(
                webhooks, 'reminder', sub_title, final_detail,
                page_key='reminders', user_name=current_user.username,
                operator_id=current_user.id
            )

        if repeat_count > 1:
            def _background_repeat_task():
                for r_idx in range(1, repeat_count + 1):
                    _execute_push_round(r_idx, repeat_count)
                    if r_idx < repeat_count:
                        time.sleep(max(1, interval_seconds))
            threading.Thread(target=_background_repeat_task, daemon=True).start()
            msg = f"已启动多轮定时推送任务：将累计向 [{channel_str}] 推送 {repeat_count} 次（每隔 {interval_seconds} 秒一次），共涵盖 {len(rem_with_days)} 条纪念日！"
        else:
            _execute_push_round(1, 1)
            msg = f"成功向 [{channel_str}] 推送 {len(rem_with_days)} 条亲友重要纪念日提醒！"
        safe_log('手动推送纪念日', f"推送了 {len(rem_with_days)} 条纪念日，次数: {repeat_count}, 间隔: {interval_seconds}秒", user=current_user)
        return jsonify({
            'code': 200,
            'message': msg,
            'count': len(rem_with_days),
            'repeat_count': repeat_count,
            'interval_seconds': interval_seconds,
            'details': text_summary
        })

    @app.route('/api/reminders/upcoming')
    @login_required
    def api_upcoming_reminders():
        """获取近期（3~7天内）的纪念日提醒"""
        rems = AnniversaryReminder.query.filter_by(user_id=current_user.id, is_active=True).all()
        today = datetime.now().date()
        upcoming = []

        for r in rems:
            this_year_date, days_left = parse_target_date_obj(r.target_date, today)
            if not this_year_date:
                continue
            adv_days = r.advance_days or 3
            if 0 <= days_left <= adv_days:
                upcoming.append({
                    'id': r.id,
                    'name': r.name,
                    'relation': r.relation or '',
                    'anniversary_type': r.anniversary_type or '纪念日',
                    'date': this_year_date.strftime('%m月%d日'),
                    'days_left': days_left,
                    'notes': r.notes or ''
                })

        return jsonify({'code': 200, 'reminders': upcoming})

    @app.route('/share/toggle', methods=['POST'])
    @login_required
    def toggle_share_ledger():
        """生成或切换免登录共享账本链接"""
        data = request.get_json() or {}
        share = SharedLedgerLink.query.filter_by(user_id=current_user.id).first()
        
        if not share:
            share = SharedLedgerLink.generate_new(current_user.id)
            db.session.add(share)
        else:
            if 'is_active' in data:
                share.is_active = bool(data['is_active'])
            else:
                share.is_active = not share.is_active
                
        db.session.commit()
        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(), 'status_change',
                f'切换共享链接',
                f'操作人：{current_user.username} | 页面：礼金账本 | 状态：{"启用" if share.is_active else "禁用"}',
                page_key='ledger', user_name=current_user.username,
                operator_id=current_user.id
            )
        except Exception:
            pass
        return jsonify({
            'success': True,
            'is_active': share.is_active,
            'token': share.token,
            'share_url': url_for('shared_ledger_view', token=share.token, _external=True)
        })


    @app.route('/admin/broadcasts', methods=['GET', 'POST'])
    @login_required
    def admin_broadcasts():
        """管理员系统广播管理"""
        if not current_user.is_admin:
            flash('权限不足', 'danger')
            return redirect(url_for('index'))
            
        if request.method == 'POST':
            title = request.form.get('title', '').strip()
            content = request.form.get('content', '').strip()
            level = request.form.get('level', 'info').strip()
            scope = request.form.get('scope', 'all').strip() or 'all'
            
            # V10.10.14 敏感内容防护：广播内容含管理员账号/密码等敏感关键词时，
            # 强制限定为「仅管理员可见」，并跳过 Webhook 推送（防止敏感信息流出到群聊）
            SENSITIVE_KEYWORDS = ('默认管理员', '初始密码', '管理员账号', 'admin123', '默认账号', '超级管理员账号')
            content_lower = (content or '').lower()
            is_sensitive = any(kw in content_lower for kw in SENSITIVE_KEYWORDS)
            if is_sensitive:
                scope = 'admin'

            is_active_val = request.form.get('is_active')
            if is_active_val is not None:
                is_active = is_active_val in ('1', 'on', 'true', 'True')
            else:
                is_active = True  # 新增广播默认上线发布
            
            if not content:
                flash('广播内容不能为空', 'warning')
            else:
                bc = Broadcast(
                    title=title or '系统公告',
                    content=content,
                    level=level,
                    scope=scope,
                    is_active=is_active,
                    created_by_user_id=current_user.id
                )
                db.session.add(bc)
                db.session.commit()
                safe_log('创建系统广播', f"标题: {title or '无标题'}，等级: {level}，范围: {scope}，状态: {'上线' if is_active else '下线'}", user=current_user)
                # V10.10.14：仅管理员可见（scope=admin）的广播不推送 Webhook，避免敏感内容流出
                if scope != 'admin':
                    try:
                        trigger_webhook_event(
                            WebhookConfig.query.filter_by(is_enabled=True).all(), 'broadcast',
                            f'{current_user.username} 发布广播：「{title or "系统公告"}」，等级：{level}',
                            f'操作人：{current_user.username} | 页面：系统广播 | 标题：{title or "系统公告"} | 等级：{level} | 内容：{content[:100]}',
                            page_key='admin_broadcasts', user_name=current_user.username,
                            operator_id=current_user.id
                        )
                    except Exception:
                        pass
                if is_sensitive:
                    flash('检测到广播内容含管理员账号/密码等敏感信息，已自动限定为「仅管理员可见」且不推送外部通知！', 'warning')
                else:
                    flash('系统广播已成功发布！', 'success')
                return redirect(url_for('admin_broadcasts'))
                
        broadcasts = Broadcast.query.order_by(Broadcast.created_at.desc()).all()
        return render_template('admin_broadcasts.html', broadcasts=broadcasts)

    @app.route('/admin/broadcast/toggle/<int:broadcast_id>', methods=['GET', 'POST'])
    @login_required
    def admin_toggle_broadcast(broadcast_id):
        """切换广播状态（支持 AJAX 与普通表单重定向，杜绝裸露出 JSON）"""
        is_ajax = (request.headers.get('X-Requested-With') == 'XMLHttpRequest') or (request.is_json)
        if not current_user.is_admin:
            if is_ajax:
                return jsonify({'success': False, 'message': '权限不足'}), 403
            flash('权限不足', 'danger')
            return redirect(url_for('admin_broadcasts'))
        bc = db.session.get(Broadcast, broadcast_id)
        if not bc:
            if is_ajax:
                return jsonify({'success': False, 'message': '广播不存在'}), 404
            flash('广播不存在', 'warning')
            return redirect(url_for('admin_broadcasts'))
        bc.is_active = not bc.is_active
        db.session.commit()
        safe_log('切换系统广播状态', f"ID: {broadcast_id} -> {'上线' if bc.is_active else '下线'}", user=current_user)
        # V10.3: 补充广播状态切换推送
        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(), 'status_change',
                f'{current_user.username} 切换广播状态',
                f'操作人：{current_user.username} | 页面：系统广播 | 状态：{"上线" if bc.is_active else "下线"}',
                page_key='admin_broadcasts', user_name=current_user.username, operator_id=current_user.id
            )
        except Exception:
            pass
        if is_ajax:
            return jsonify({'success': True, 'is_active': bc.is_active})
        flash(f"系统广播已{'上线展示' if bc.is_active else '下线暂不展示'}！", 'success')
        return redirect(url_for('admin_broadcasts'))

    @app.route('/admin/broadcast/delete/<int:broadcast_id>', methods=['POST'])
    @login_required
    def admin_delete_broadcast(broadcast_id):
        """删除广播"""
        if not current_user.is_admin:
            flash('权限不足', 'danger')
            return redirect(url_for('index'))
        bc = db.session.get(Broadcast, broadcast_id)
        if bc:
            db.session.delete(bc)
            db.session.commit()
            safe_log('删除系统广播', f"ID: {broadcast_id}", user=current_user)
            # V10.3: 补充删除广播推送
            try:
                trigger_webhook_event(
                    WebhookConfig.query.filter_by(is_enabled=True).all(), 'delete',
                    f'{current_user.username} 删除广播',
                    f'操作人：{current_user.username} | 页面：系统广播 | ID: {broadcast_id}',
                    page_key='admin_broadcasts', user_name=current_user.username, operator_id=current_user.id
                )
            except Exception:
                pass
            flash('广播已彻底删除', 'success')
        return redirect(url_for('admin_broadcasts'))

    @app.route('/api/broadcasts')
    @app.route('/api/broadcasts/active')
    @login_required
    def api_active_broadcasts():
        """获取当前所有有效系统广播及当前用户的已读/未读状态"""
        active_bcs = Broadcast.query.filter_by(is_active=True).order_by(Broadcast.created_at.desc()).all()
        read_bc_ids = set(
            row[0] for row in db.session.query(BroadcastRead.broadcast_id).filter_by(user_id=current_user.id).all()
        )
        data = []
        unread_count = 0
        for bc in active_bcs:
            if bc.scope == 'admin' and not current_user.is_admin:
                continue
            if bc.scope == 'user' and current_user.is_admin:
                continue
            is_read = (bc.id in read_bc_ids)
            if not is_read:
                unread_count += 1
            data.append({
                'id': bc.id,
                'title': bc.title or '系统通知',
                'content': bc.content,
                'level': bc.level or 'info',
                'scope': bc.scope or 'all',
                'created_at': bc.created_at.strftime('%Y-%m-%d %H:%M') if bc.created_at else '',
                'is_read': is_read
            })
        return jsonify({'code': 200, 'broadcasts': data, 'unread_count': unread_count})

    @app.route('/api/broadcast/mark_read/<int:bc_id>', methods=['POST'])
    @login_required
    def api_mark_broadcast_read(bc_id):
        """标记单条广播为当前用户已读"""
        existing = BroadcastRead.query.filter_by(broadcast_id=bc_id, user_id=current_user.id).first()
        if not existing:
            br = BroadcastRead(broadcast_id=bc_id, user_id=current_user.id)
            db.session.add(br)
            db.session.commit()
        # V10.9: 补充推送
        try:
            bc = Broadcast.query.get(bc_id)
            bc_title = bc.title if bc else f'广播#{bc_id}'
            webhooks = WebhookConfig.query.filter_by(is_enabled=True).all()
            trigger_webhook_event(
                webhooks, 'status_change',
                f'标记广播已读',
                f'操作人：{current_user.username} | 页面：系统广播 | 标记「{bc_title}」为已读',
                page_key='admin_broadcasts', user_name=current_user.username,
                operator_id=current_user.id
            )
        except Exception:
            pass
        return jsonify({'code': 200, 'message': '已标记为已读'})

    @app.route('/api/broadcast/mark_all_read', methods=['POST'])
    @login_required
    def api_mark_all_broadcast_read():
        """一键将所有有效广播标记为当前用户已读"""
        active_bcs = Broadcast.query.filter_by(is_active=True).all()
        read_bc_ids = set(
            row[0] for row in db.session.query(BroadcastRead.broadcast_id).filter_by(user_id=current_user.id).all()
        )
        marked_count = 0
        for bc in active_bcs:
            if bc.id not in read_bc_ids:
                db.session.add(BroadcastRead(broadcast_id=bc.id, user_id=current_user.id))
                marked_count += 1
        db.session.commit()
        # V10.9: 补充推送
        if marked_count > 0:
            try:
                webhooks = WebhookConfig.query.filter_by(is_enabled=True).all()
                trigger_webhook_event(
                    webhooks, 'status_change',
                    f'全部标记广播已读',
                    f'操作人：{current_user.username} | 页面：系统广播 | 一键标记 {marked_count} 条广播为已读',
                    page_key='admin_broadcasts', user_name=current_user.username,
                    operator_id=current_user.id
                )
            except Exception:
                pass
        return jsonify({'code': 200, 'message': '所有通知已全部标记为已读'})

    @app.route('/admin/webhooks', methods=['GET'])
    @login_required
    def admin_webhooks():
        """管理员 Webhook 与长连接机器人配置及推送日志"""
        if not current_user.is_admin:
            flash('权限不足', 'danger')
            return redirect(url_for('index'))

        webhooks = WebhookConfig.query.order_by(WebhookConfig.created_at.desc()).all()
        logs = WebhookLog.query.order_by(WebhookLog.created_at.desc()).limit(500).all()
        # V10.1: 注入矩阵数据到模板
        from webhook_utils import PAGE_NAMES as _PAGE_NAMES, EVENT_COLUMNS as _EVENT_COLUMNS, PAGE_EVENT_MATRIX as _PAGE_EVENT_MATRIX, DEFAULT_MESSAGE_TEMPLATES as _DEFAULT_TEMPLATES
        return render_template('admin_webhooks.html', webhooks=webhooks, logs=logs,
                               ALL_PAGES=_PAGE_NAMES, EVENT_COLUMNS=_EVENT_COLUMNS,
                               PAGE_EVENT_MATRIX=_PAGE_EVENT_MATRIX,
                               DEFAULT_TEMPLATES=_DEFAULT_TEMPLATES,
                               all_users=User.query.order_by(User.is_admin.desc(), User.id).all())

    @app.route('/admin/webhooks/create', methods=['POST'])
    @login_required
    def admin_create_webhook():
        """添加 Webhook 或企业微信长连接机器人"""
        if not current_user.is_admin:
            # V3: 兼容 AJAX 和传统 form 提交
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.is_json:
                return jsonify({'success': False, 'message': '权限不足'}), 403
            flash('权限不足', 'danger')
            return redirect(url_for('index'))

        connection_type = request.form.get('connection_type', 'webhook_url').strip()
        bot_platform = request.form.get('bot_platform', 'wecom').strip()
        name = request.form.get('name', '').strip()
        url = request.form.get('url', '').strip()
        secret = request.form.get('secret', '').strip()
        bot_id = request.form.get('bot_id', '').strip()
        bot_secret = request.form.get('bot_secret', '').strip()
        chatid = request.form.get('chatid', '').strip()

        notify_on_add = bool(request.form.get('notify_on_add'))
        notify_on_delete = bool(request.form.get('notify_on_delete'))
        notify_on_reminder = bool(request.form.get('notify_on_reminder'))
        notify_on_broadcast = bool(request.form.get('notify_on_broadcast'))
        notify_on_update = bool(request.form.get('notify_on_update'))
        notify_on_security = bool(request.form.get('notify_on_security'))
        notify_on_system = bool(request.form.get('notify_on_system'))
        notify_on_status_change = bool(request.form.get('notify_on_status_change'))
        notify_pages = request.form.get('notify_pages', '{}').strip()
        message_templates = request.form.get('message_templates', '{}').strip()
        # V10.3: 用户级监控过滤
        # V10.9: 空列表 = 不推送，新建时如未勾选则默认全选
        monitor_user_ids = request.form.get('monitor_user_ids', '').strip()
        monitor_event_types = request.form.get('monitor_event_types', '').strip()
        # 解析检查，如果为空列表则填入全选
        import json as _wh_json
        try:
            uids_list = _wh_json.loads(monitor_user_ids) if monitor_user_ids else []
            if not uids_list:
                uids_list = [u.id for u in User.query.with_entities(User.id).all()]
                monitor_user_ids = _wh_json.dumps(uids_list)
        except Exception:
            monitor_user_ids = _wh_json.dumps([u.id for u in User.query.with_entities(User.id).all()])
        try:
            types_list = _wh_json.loads(monitor_event_types) if monitor_event_types else []
            if not types_list:
                from webhook_utils import EVENT_COLUMNS as _ev_cols
                types_list = list(_ev_cols.keys())
                monitor_event_types = _wh_json.dumps(types_list)
        except Exception:
            from webhook_utils import EVENT_COLUMNS as _ev_cols
            monitor_event_types = _wh_json.dumps(list(_ev_cols.keys()))

        is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

        if not name:
            if is_ajax:
                return jsonify({'success': False, 'message': '渠道名称不能为空！'})
            flash('渠道名称不能为空！', 'warning')
            return redirect(url_for('admin_webhooks'))

        if connection_type == 'long_connection':
            if not bot_id or not bot_secret:
                if is_ajax:
                    return jsonify({'success': False, 'message': '长连接模式必须填写 Bot ID 和 Secret 两个凭证！'})
                flash('长连接模式必须填写 Bot ID 和 Secret 两个凭证！', 'warning')
                return redirect(url_for('admin_webhooks'))
            # V7: 保存不再进行真实凭证校验（原逻辑需连接企微服务器最长 8 秒，
            # 前端无加载反馈导致"保存无响应"感知）。凭证有效性由「测试」按钮验证。
            if not url or url.startswith('wecom://bot/'):
                url = f"wecom://bot/{bot_id}?chatid={chatid}" if chatid else f"wecom://bot/{bot_id}"
        else:
            if not url:
                if is_ajax:
                    return jsonify({'success': False, 'message': '标准 Webhook 模式的目标 URL 不能为空！'})
                flash('标准 Webhook 模式的目标 URL 不能为空！', 'warning')
                return redirect(url_for('admin_webhooks'))

        hook = WebhookConfig(
            user_id=current_user.id,
            channel_name=name,
            connection_type=connection_type,
            bot_platform=bot_platform,
            bot_id=bot_id if connection_type == 'long_connection' else None,
            bot_secret=bot_secret if connection_type == 'long_connection' else None,
            webhook_url=url,
            secret_token=secret if connection_type == 'webhook_url' else None,
            notify_on_add=notify_on_add,
            notify_on_delete=notify_on_delete,
            notify_on_reminder=notify_on_reminder,
            notify_on_broadcast=notify_on_broadcast,
            notify_on_update=notify_on_update,
            notify_on_security=notify_on_security,
            notify_on_system=notify_on_system,
            notify_on_status_change=notify_on_status_change,
            notify_pages=notify_pages,
            message_templates=message_templates,
            monitor_user_ids=monitor_user_ids,
            monitor_event_types=monitor_event_types,
            is_enabled=True
        )
        db.session.add(hook)
        db.session.commit()
        safe_log('添加Webhook', f"名称: {name}, 连接方式: {connection_type}")
        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(), 'system',
                f'新增Webhook通道 [{name}]',
                f'操作人：{current_user.username} | 页面：Webhook通知 | 名称：{name} | 连接方式：{connection_type}',
                page_key='admin_webhooks', user_name=current_user.username,
                operator_id=current_user.id
            )
        except Exception:
            pass
        if is_ajax:
            return jsonify({'success': True, 'message': f'Webhook / 机器人通道 [{name}] 配置添加成功！'})
        flash(f'Webhook / 机器人通道 [{name}] 配置添加成功！', 'success')
        return redirect(url_for('admin_webhooks'))

    @app.route('/admin/webhooks/edit/<int:webhook_id>', methods=['POST'])
    @login_required
    def admin_edit_webhook(webhook_id):
        """编辑 Webhook 或企业微信长连接机器人配置"""
        if not current_user.is_admin:
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.is_json:
                return jsonify({'success': False, 'message': '权限不足'}), 403
            flash('权限不足', 'danger')
            return redirect(url_for('index'))

        hook = db.session.get(WebhookConfig, webhook_id)
        if not hook:
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'success': False, 'message': 'Webhook 配置不存在'}), 404
            flash('Webhook 配置不存在', 'danger')
            return redirect(url_for('admin_webhooks'))

        is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        connection_type = request.form.get('connection_type', 'webhook_url').strip()
        bot_platform = request.form.get('bot_platform', 'wecom').strip()
        name = request.form.get('name', '').strip()
        url = request.form.get('url', '').strip()
        secret = request.form.get('secret', '').strip()
        bot_id = request.form.get('bot_id', '').strip()
        bot_secret = request.form.get('bot_secret', '').strip()
        chatid = request.form.get('chatid', '').strip()

        if not name:
            if is_ajax:
                return jsonify({'success': False, 'message': '渠道名称不能为空！'})
            flash('渠道名称不能为空！', 'warning')
            return redirect(url_for('admin_webhooks'))

        if connection_type == 'long_connection':
            if not bot_id or not bot_secret:
                if is_ajax:
                    return jsonify({'success': False, 'message': '长连接模式必须填写 Bot ID 和 Secret 两个凭证！'})
                flash('长连接模式必须填写 Bot ID 和 Secret 两个凭证！', 'warning')
                return redirect(url_for('admin_webhooks'))
            # V7 修复：保存时不再进行真实凭证校验（原校验需连接企微服务器最长 8 秒且无反馈，
            # 导致「保存无响应」）；凭证有效性由列表中的「测试」按钮负责验证
            # V10.10.14：移除「chatid 为空时从旧 webhook_url 回填」逻辑——
            # 用户清空目标会话输入框保存即真正清空，等待企微群内 @机器人 由后台守护线程自动捕获绑定
            url = f"wecom://bot/{bot_id}?chatid={chatid}" if chatid else f"wecom://bot/{bot_id}"
            hook.bot_id = bot_id
            hook.bot_secret = bot_secret
            hook.secret_token = None
        else:
            if not url:
                flash('标准 Webhook 模式的目标 URL 不能为空！', 'warning')
                return redirect(url_for('admin_webhooks'))
            hook.secret_token = secret
            hook.bot_id = None
            hook.bot_secret = None

        hook.channel_name = name
        hook.connection_type = connection_type
        hook.bot_platform = bot_platform
        hook.webhook_url = url
        hook.notify_on_add = bool(request.form.get('notify_on_add'))
        hook.notify_on_delete = bool(request.form.get('notify_on_delete'))
        hook.notify_on_reminder = bool(request.form.get('notify_on_reminder'))
        hook.notify_on_broadcast = bool(request.form.get('notify_on_broadcast'))
        hook.notify_on_update = bool(request.form.get('notify_on_update'))
        hook.notify_on_security = bool(request.form.get('notify_on_security'))
        hook.notify_on_system = bool(request.form.get('notify_on_system'))
        hook.notify_on_status_change = bool(request.form.get('notify_on_status_change'))
        hook.notify_pages = request.form.get('notify_pages', '{}').strip()
        hook.message_templates = request.form.get('message_templates', '{}').strip()
        # V10.3: 用户级监控过滤
        hook.monitor_user_ids = request.form.get('monitor_user_ids', '[]').strip()
        hook.monitor_event_types = request.form.get('monitor_event_types', '[]').strip()

        db.session.commit()
        safe_log('编辑Webhook', f"ID: {webhook_id}, 名称: {name}, 连接方式: {connection_type}")
        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(), 'system',
                f'编辑Webhook通道 [{name}]',
                f'操作人：{current_user.username} | 页面：Webhook通知 | ID：{webhook_id} | 名称：{name}',
                page_key='admin_webhooks', user_name=current_user.username,
                operator_id=current_user.id
            )
        except Exception:
            pass
        if is_ajax:
            return jsonify({'success': True, 'message': f'Webhook [{name}] 配置已成功更新！'})
        flash(f'Webhook [{name}] 配置已成功更新！', 'success')
        return redirect(url_for('admin_webhooks'))

    @app.route('/admin/webhooks/toggle/<int:webhook_id>', methods=['POST'])
    @app.route('/admin/webhook/toggle/<int:webhook_id>', methods=['POST'])
    @login_required
    def admin_toggle_webhook(webhook_id):
        """切换 Webhook 激活状态"""
        if not current_user.is_admin:
            return jsonify({'success': False, 'message': '权限不足'}), 403
        hook = db.session.get(WebhookConfig, webhook_id)
        if not hook:
            flash('Webhook不存在', 'danger')
            return redirect(url_for('admin_webhooks'))
        hook.is_enabled = not hook.is_enabled
        db.session.commit()
        safe_log('切换Webhook状态', f"通道: {hook.channel_name}, 状态: {'启用' if hook.is_enabled else '停用'}")
        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(), 'status_change',
                f'切换Webhook状态 [{hook.channel_name}]',
                f'操作人：{current_user.username} | 页面：Webhook通知 | 通道：{hook.channel_name} | 状态：{"启用" if hook.is_enabled else "停用"}',
                page_key='admin_webhooks', user_name=current_user.username,
                operator_id=current_user.id
            )
        except Exception:
            pass
        flash(f"Webhook [{hook.channel_name}] 状态已更新！", 'success')
        return redirect(url_for('admin_webhooks'))

    @app.route('/admin/webhooks/delete/<int:webhook_id>', methods=['POST'])
    @app.route('/admin/webhook/delete/<int:webhook_id>', methods=['POST'])
    @login_required
    def admin_delete_webhook(webhook_id):
        """删除 Webhook 配置"""
        if not current_user.is_admin:
            flash('权限不足', 'danger')
            return redirect(url_for('index'))
        hook = db.session.get(WebhookConfig, webhook_id)
        if hook:
            hook_name = hook.channel_name
            db.session.delete(hook)
            safe_log('删除Webhook', f"ID: {webhook_id}")
            db.session.commit()
            try:
                trigger_webhook_event(
                    WebhookConfig.query.filter_by(is_enabled=True).all(), 'record_delete',
                f'删除Webhook通道 [{hook_name}]',
                f'操作人：{current_user.username} | 页面：Webhook通知 | ID：{webhook_id} | 名称：{hook_name}',
                page_key='admin_webhooks', user_name=current_user.username,
                operator_id=current_user.id
                )
            except Exception:
                pass
            flash('Webhook 配置已删除', 'success')
        return redirect(url_for('admin_webhooks'))

    @app.route('/admin/webhooks/test/<int:webhook_id>', methods=['POST'])
    @app.route('/admin/webhook/test/<int:webhook_id>', methods=['POST'])
    @login_required
    def admin_test_webhook(webhook_id):
        """测试 Webhook 或长连接机器人发送"""
        if not current_user.is_admin:
            return jsonify({'success': False, 'message': '权限不足'}), 403
        hook = db.session.get(WebhookConfig, webhook_id)
        if not hook:
            return jsonify({'success': False, 'message': 'Webhook不存在'}), 404

        try:
            success, code, msg = test_single_webhook(hook, sender_name=current_user.username)
            safe_log('测试Webhook推送', f"通道: {hook.channel_name}, 结果: {'成功' if success else '失败'}, 状态码: {code}")
            # V10.9: 补充推送通知
            try:
                webhooks = WebhookConfig.query.filter_by(is_enabled=True).all()
                trigger_webhook_event(
                    webhooks, 'system',
                    f'测试Webhook通道',
                    f'操作人：{current_user.username} | 页面：Webhook通知 | 测试通道「{hook.channel_name}」结果: {"成功" if success else "失败"}',
                    page_key='admin_webhooks', user_name=current_user.username,
                    operator_id=current_user.id
                )
            except Exception:
                pass
            return jsonify({
                'success': bool(success),
                'code': 200 if success else 500,
                'status_code': code,
                'message': msg
            })
        except Exception as e:
            return jsonify({'success': False, 'code': 500, 'message': str(e)}), 500

    @app.route('/admin/webhook/logs/delete/<int:log_id>', methods=['POST'])
    @login_required
    def admin_delete_webhook_log(log_id):
        """删除单条 Webhook 推送日志"""
        if not current_user.is_admin:
            return jsonify({'code': 403, 'message': '权限不足'}), 403
        log_item = db.session.get(WebhookLog, log_id)
        if not log_item:
            return jsonify({'code': 404, 'message': '日志不存在或已被删除'}), 404
        db.session.delete(log_item)
        db.session.commit()
        safe_log('删除推送日志', f"日志ID: {log_id}")
        # V10.3: 补充删除推送日志推送
        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(), 'delete',
                f'{current_user.username} 删除推送日志',
                f'操作人：{current_user.username} | 页面：Webhook管理 | 日志ID: {log_id}',
                page_key='admin_webhooks', user_name=current_user.username, operator_id=current_user.id
            )
        except Exception:
            pass
        return jsonify({'code': 200, 'message': '推送日志已成功删除！'})

    @app.route('/admin/webhook/logs/batch_delete', methods=['POST'])
    @login_required
    def admin_batch_delete_webhook_logs():
        """批量删除 Webhook 推送日志"""
        if not current_user.is_admin:
            return jsonify({'code': 403, 'message': '权限不足'}), 403
        data = request.get_json(silent=True) or {}
        ids = data.get('log_ids') or []
        if not ids and request.form.get('log_ids'):
            try:
                ids = [int(x.strip()) for x in request.form.get('log_ids').split(',') if x.strip().isdigit()]
            except Exception:
                ids = []
        if not ids:
            return jsonify({'code': 400, 'message': '请先勾选需要删除的日志记录！'}), 400

        deleted_count = WebhookLog.query.filter(WebhookLog.id.in_(ids)).delete(synchronize_session=False)
        db.session.commit()
        # V10.3: 补充批量删除推送日志推送
        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(), 'batch_delete',
                f'{current_user.username} 批量删除推送日志',
                f'操作人：{current_user.username} | 页面：Webhook管理 | 数量：{deleted_count} 条',
                page_key='admin_webhooks', user_name=current_user.username, operator_id=current_user.id
            )
        except Exception:
            pass
        return jsonify({'code': 200, 'message': f'已成功删除 {deleted_count} 条推送日志！', 'deleted_count': deleted_count})

    @app.route('/admin/webhook/logs/clear', methods=['POST'])
    @login_required
    def admin_clear_webhook_logs():
        """清空全部 Webhook 推送日志"""
        if not current_user.is_admin:
            return jsonify({'code': 403, 'message': '权限不足'}), 403
        count = WebhookLog.query.delete(synchronize_session=False)
        db.session.commit()
        # V10.3: 补充清空推送日志推送
        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(), 'clear',
                f'{current_user.username} 清空推送日志',
                f'操作人：{current_user.username} | 页面：Webhook管理 | 数量：全部 {count} 条',
                page_key='admin_webhooks', user_name=current_user.username, operator_id=current_user.id
            )
        except Exception:
            pass
        return jsonify({'code': 200, 'message': f'已成功清空全部 {count} 条推送日志！'})

    @app.route('/api/wecom/callback', methods=['GET', 'POST'])
    @app.route('/webhook/callback', methods=['GET', 'POST'])
    @app.route('/wecom/callback', methods=['GET', 'POST'])
    @app.route('/api/wecom', methods=['GET', 'POST'])
    @app.route('/webhook', methods=['GET', 'POST'])
    @app.route('/wecom', methods=['GET', 'POST'])
    @app.route('/callback', methods=['GET', 'POST'])
    @app.route('/api/wecom/webhook', methods=['GET', 'POST'])
    @app.route('/wechat/callback', methods=['GET', 'POST'])
    def wecom_http_callback():
        """
        企业微信群机器人 / 智能机器人 HTTP 回调接口
        支持 GET 验证 echostr，支持 POST 接收群内 @机器人 消息并提取目标 chatid
        """
        # 1. 企微 GET 回调验证
        echostr = request.args.get('echostr', '').strip()
        if request.method == 'GET':
            if echostr:
                return echostr, 200, {'Content-Type': 'text/plain; charset=utf-8'}
            return jsonify({'code': 200, 'status': 'ok', 'message': 'WeCom callback endpoint is active'}), 200

        # 2. 企微 POST 回调消息
        try:
            raw_body = request.get_data(as_text=True)
            chatid = None
            sender = None
            content = None
            bot_id = request.args.get('bot_id', '')

            # 尝试 JSON 解析
            if request.is_json or (raw_body and raw_body.strip().startswith('{')):
                try:
                    data = json.loads(raw_body)
                    chatid = data.get('chatid') or data.get('ChatId')
                    if not chatid and isinstance(data.get('from'), dict):
                        chatid = data['from'].get('userid') or data['from'].get('id')
                    sender = (data.get('from', {}) if isinstance(data.get('from'), dict) else {}).get('name') or '用户'
                    content = (data.get('text', {}) if isinstance(data.get('text'), dict) else {}).get('content') or ''
                    if not bot_id:
                        bot_id = data.get('aibot_id') or data.get('bot_id') or ''
                except Exception:
                    pass

            # 尝试 XML 解析
            elif raw_body and '<xml>' in raw_body:
                import re
                cid_match = re.search(r'<ChatId><!\[CDATA\[(.*?)\]\]></ChatId>', raw_body) or re.search(r'<ChatId>(.*?)</ChatId>', raw_body)
                if cid_match:
                    chatid = cid_match.group(1).strip()
                from_match = re.search(r'<FromUserName><!\[CDATA\[(.*?)\]\]></FromUserName>', raw_body) or re.search(r'<FromUserName>(.*?)</FromUserName>', raw_body)
                if from_match:
                    sender = from_match.group(1).strip()
                cnt_match = re.search(r'<Content><!\[CDATA\[(.*?)\]\]></Content>', raw_body) or re.search(r'<Content>(.*?)</Content>', raw_body)
                if cnt_match:
                    content = cnt_match.group(1).strip()

            # 正则兜底解析
            if not chatid and raw_body:
                import re
                m = re.search(r'["\']chatid["\']\s*:\s*["\']([^"\']+)["\']', raw_body, re.IGNORECASE)
                if m:
                    chatid = m.group(1).strip()

            if chatid:
                if bot_id:
                    _cached_chatids[bot_id] = chatid
                wh_list = WebhookConfig.query.filter_by(is_enabled=True).all()
                bound_names = []
                for wh in wh_list:
                    if wh.bot_platform == 'wecom' or (bot_id and wh.bot_id == bot_id) or (wh.webhook_url and 'wecom' in wh.webhook_url):
                        if wh.bot_id:
                            _cached_chatids[wh.bot_id] = chatid
                        if not wh.webhook_url or wh.webhook_url.startswith('wecom://') or wh.connection_type == 'long_connection':
                            wh.webhook_url = f"wecom://bot/{wh.bot_id or 'aibot'}?chatid={chatid}"
                        elif 'chatid=' in wh.webhook_url:
                            wh.webhook_url = re.sub(r'chatid=[^&]+', f'chatid={chatid}', wh.webhook_url)
                        else:
                            sep = '&' if '?' in wh.webhook_url else '?'
                            wh.webhook_url = f"{wh.webhook_url}{sep}chatid={chatid}"
                        bound_names.append(wh.channel_name)
                db.session.commit()

                record_webhook_log(
                    1,
                    wh_list[0].id if wh_list else 1,
                    'receive_chatid',
                    {"chatid": chatid, "sender": sender, "content": content},
                    200,
                    f"成功从企业微信 HTTP 回调中捕获群聊会话 chatid [{chatid}]，已自动绑定渠道: {', '.join(bound_names) or '企微渠道'}",
                    True
                )
                # V10.9: 补充推送通知 - 企微回调自动绑定 chatid
                try:
                    webhooks_for_notify = WebhookConfig.query.filter_by(is_enabled=True).all()
                    trigger_webhook_event(
                        webhooks_for_notify, 'update',
                        f'企微回调自动绑定chatid',
                        f'系统自动 | 页面：Webhook通知 | 捕获群聊会话 chatid [{chatid}]，已绑定渠道: {", ".join(bound_names) or "企微渠道"}',
                        page_key='admin_webhooks', user_name='系统自动'
                    )
                except Exception:
                    pass

                if raw_body and ('<xml' in raw_body.lower()):
                    reply_xml = f"""<xml>
<ToUserName><![CDATA[{sender or ''}]]></ToUserName>
<FromUserName><![CDATA[bot]]></FromUserName>
<CreateTime>{int(time.time())}</CreateTime>
<MsgType><![CDATA[text]]></MsgType>
<Content><![CDATA[【人情礼金记账系统】已成功捕获并绑定本群聊会话(chatid: {chatid})！后续重要纪念日与记账提醒将自动推送到本群。]]></Content>
</xml>"""
                    return reply_xml, 200, {'Content-Type': 'application/xml; charset=utf-8'}
                return jsonify({'errcode': 0, 'errmsg': 'ok', 'chatid': chatid, 'message': f'已成功绑定群聊会话 [{chatid}]'}), 200

            return jsonify({'errcode': 0, 'errmsg': 'received'}), 200
        except Exception as e:
            return jsonify({'errcode': 0, 'errmsg': str(e)}), 200


    @app.route('/admin/backups', methods=['GET'])
    @login_required
    def admin_backups():
        """WebDAV 备份与恢复管理（瞬间响应，列表通过前端异步拉取）"""
        if not current_user.is_admin and not current_user.can_use_backup():
            flash('权限不足', 'danger')
            return redirect(url_for('index'))

        # V3: 按用户获取配置（管理员获取全局配置，普通用户获取自己的私有配置）
        config = BackupConfig.get_config(None if current_user.is_admin else current_user.id)
        # V10.1: 定时任务查看权限解耦——"允许查看他人定时任务"开关仅控制查看，与备份数据权限无关
        if current_user.is_admin:
            scheduled_tasks = ScheduledBackupTask.query.order_by(ScheduledBackupTask.created_at.desc()).all()
        else:
            _global_cfg = BackupConfig.get_config(None)
            _allow_view = getattr(_global_cfg, 'allow_view_others_tasks', False)
            if _allow_view:
                # 开关开启：普通用户可查看全部定时任务（只读），编辑/删除仍由后端权限隔离
                scheduled_tasks = ScheduledBackupTask.query.order_by(ScheduledBackupTask.created_at.desc()).all()
            else:
                scheduled_tasks = ScheduledBackupTask.query.filter_by(created_by=current_user.id).order_by(ScheduledBackupTask.created_at.desc()).all()
        # 获取有备份权限的普通用户列表
        authorized_users = User.query.filter_by(backup_authorized=True, is_admin=False).all() if hasattr(User, 'backup_authorized') else []
        # V6: 获取有定时任务权限的普通用户列表
        task_authorized_users = User.query.filter_by(scheduled_task_authorized=True, is_admin=False).all() if hasattr(User, 'scheduled_task_authorized') else []
        all_users = User.query.order_by(User.is_admin.desc(), User.id).all()
        # V3: 检查加密密码是否已配置
        has_encrypt_password = bool(config.backup_encrypt_password)
        # V8: 加密密码明文回显（用户要求：未勾选"清除"时回显已输入密码）
        config_encrypt_pwd_plain = config.backup_encrypt_password or ''
        # V3: 获取管理员全局配置中的 allow_view_others_tasks
        global_config = BackupConfig.get_config(None)
        allow_view_others = getattr(global_config, 'allow_view_others_tasks', False)
        # V9: 普通用户引用管理员配置时，页面展示管理员设置的配置别称（而非用户自己的空别称）
        admin_config_alias = ''
        if not current_user.is_admin:
            admin_config_alias = getattr(global_config, 'config_alias', '') or '管理员配置'
        return render_template(
            'admin_backups.html',
            config=config,
            config_data=config,
            backups=[],
            backup_files=[],
            scheduled_tasks=scheduled_tasks,
            authorized_users=authorized_users,
            task_authorized_users=task_authorized_users,
            all_users=all_users,
            # V10.10.22: 渲染时实时检测（首次访问触发一次导入，此后返回缓存值）
            has_pyzipper=_ensure_pyzipper(),
            has_encrypt_password=has_encrypt_password,
            config_encrypt_pwd_plain=config_encrypt_pwd_plain,
            allow_view_others=allow_view_others,
            admin_config_alias=admin_config_alias,
            can_use_scheduled_tasks=current_user.can_use_scheduled_tasks(),
            # V10.2 新增：定时任务操作权限
            allow_edit_others_tasks=getattr(global_config, 'allow_edit_others_tasks', False),
            allow_delete_others_tasks=getattr(global_config, 'allow_delete_others_tasks', False),
            can_edit_others_tasks=current_user.can_edit_others_scheduled_tasks(),
            can_delete_others_tasks=current_user.can_delete_others_scheduled_tasks()
        )

    @app.route('/admin/backups/reference_admin_config', methods=['GET'])
    @login_required
    def admin_reference_admin_config():
        """V9 优化：一键引用只展示管理员配置别称，不返回地址/账号/密码任何敏感信息。
        返回：别称、是否已配置、是否设置密码、当前用户引用状态"""
        if current_user.is_admin:
            return jsonify({'success': False, 'message': '管理员无需引用自己的配置'}), 403

        global_config = BackupConfig.get_config(None)
        user_config = BackupConfig.get_config(current_user.id)
        # 管理员未配置服务器地址时视为未配置
        if not global_config.webdav_url:
            return jsonify({'success': False, 'message': '管理员尚未配置 WebDAV，请等待管理员配置后重试'})
        data = {
            'success': True,
            # V9: 只返回别称，地址/账号/子目录/密码一概不返回
            'config_alias': global_config.config_alias or '管理员配置',
            'has_password': bool(global_config.webdav_password),
            'adopted': bool(getattr(user_config, 'adopted_from_admin', False)),
        }
        return jsonify(data)

    @app.route('/admin/backups/adopt_admin_config', methods=['POST'])
    @login_required
    def admin_adopt_admin_config():
        """V9 新增：普通用户"一键采用"管理员 WebDAV 配置（后端加密复制，前端不接触任何凭证）
        - 服务端直接把管理员的 URL/账号/密码/子目录复制到当前用户私有配置（密码密文直传）
        - 标记 adopted_from_admin=True，此后页面不再回显地址/账号（防泄露）
        - 若管理员配置更新后用户再次点"一键更新"，重新拉取最新配置覆盖
        """
        if current_user.is_admin:
            return jsonify({'success': False, 'message': '管理员无需引用自己的配置'}), 403

        global_config = BackupConfig.get_config(None)
        if not global_config.webdav_url:
            return jsonify({'success': False, 'message': '管理员尚未配置 WebDAV，请等待管理员配置后重试'})

        user_config = BackupConfig.get_config(current_user.id)
        # 服务端直传密文，前端全程接触不到明文或密文凭证
        user_config.webdav_url = global_config.webdav_url
        user_config.webdav_username = global_config.webdav_username
        user_config.webdav_password = global_config.webdav_password
        user_config.backup_path = global_config.backup_path
        user_config.backup_subdir = global_config.backup_subdir or 'gift_backups'
        user_config.adopted_from_admin = True
        db.session.commit()

        alias = global_config.config_alias or '管理员配置'
        safe_log('引用WebDAV配置', f"用户 {current_user.username} 一键采用管理员配置「{alias}」（凭证服务端加密复制，页面不回显）")
        # V10.3: 补充采用管理员配置推送
        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(), 'system',
                f'{current_user.username} 采用管理员配置',
                f'操作人：{current_user.username} | 页面：WebDAV备份 | 配置：{alias}',
                page_key='admin_backups', user_name=current_user.username, operator_id=current_user.id
            )
        except Exception:
            pass
        return jsonify({'success': True, 'config_alias': alias, 'message': f'已采用管理员配置「{alias}」，可直接执行备份操作'})

    @app.route('/admin/backups/list_ajax', methods=['GET'])
    @login_required
    def admin_backups_list_ajax():
        """异步拉取远端 WebDAV 备份文件列表，带5秒超时与安全容灾"""
        if not current_user.is_admin and not current_user.can_use_backup():
            return jsonify({'success': False, 'message': '权限不足'}), 403
        # V3: 按用户获取配置
        config = BackupConfig.get_config(None if current_user.is_admin else current_user.id)
        if not config.server_url or not config.username:
            return jsonify({'success': True, 'configured': False, 'backups': [], 'message': '未配置 WebDAV'})
        safe_log('查看备份列表', f"WebDAV: {config.server_url}")
        try:
            ok, res = list_webdav_backups(config)
            if ok:
                # V3: 解析备份文件名中的创建者标识，并标记当前用户是否有操作权限
                import re as _re
                # V6: 查询实际管理员用户名列表，不硬编码
                _admin_usernames = set()
                if hasattr(current_user, 'is_admin'):
                    _admin_users = User.query.filter_by(is_admin=True).all()
                    _admin_usernames = {u.username for u in _admin_users}
                for item in res:
                    fn = item.get('filename', '') or item.get('name', '')
                    # 解析文件名格式: {timestamp}_{username}_{type}.db 或旧格式 gift_bookkeeping_backup_*.db
                    m = _re.match(r'(\d{8}_\d{6})_(.+?)_(db_backup|file_backup|custom)\.(db|zip)', fn)
                    if m:
                        item['created_by'] = m.group(2)
                    else:
                        # V6: 旧格式备份——查找实际管理员用户名，不硬编码 'admin'
                        item['created_by'] = next(iter(_admin_usernames)) if _admin_usernames else 'unknown'
                    # V6: 三级权限判断 + 管理员数据保护
                    _creator = item['created_by']
                    _is_creator_admin = _creator in _admin_usernames
                    _is_own = (_creator == current_user.username)
                    # 管理员创建的备份：只有管理员可删除/恢复
                    if _is_creator_admin:
                        item['can_restore'] = current_user.is_admin
                        item['can_delete'] = current_user.is_admin
                    else:
                        item['can_restore'] = current_user.is_admin or _is_own or current_user.can_edit_others_backup()
                        item['can_delete'] = current_user.is_admin or _is_own or current_user.can_delete_others_backup()
                return jsonify({'success': True, 'configured': True, 'backups': res, 'current_user': current_user.username, 'is_admin': current_user.is_admin})
            else:
                return jsonify({'success': False, 'configured': True, 'message': str(res), 'backups': []})
        except Exception as e:
            return jsonify({'success': False, 'configured': True, 'message': f'拉取备份异常: {str(e)}', 'backups': []})

    @app.route('/admin/backups/save_config', methods=['POST'])
    @app.route('/admin/backup/config', methods=['POST'])
    @login_required
    def admin_save_webdav_config():
        """保存 WebDAV 配置（管理员可编辑全局配置，普通用户编辑自己的私有配置）"""
        if not current_user.is_admin and not current_user.can_use_backup():
            flash('权限不足', 'danger')
            return redirect(url_for('admin_backups'))

        # V3: 按用户获取配置
        config = BackupConfig.get_config(None if current_user.is_admin else current_user.id)
        server_url = request.form.get('webdav_url', '').strip() or request.form.get('server_url', '').strip()
        username = request.form.get('webdav_username', '').strip() or request.form.get('username', '').strip()
        password = request.form.get('webdav_password', '').strip() or request.form.get('password', '').strip()
        remote_dir = request.form.get('remote_dir', '').strip() or request.form.get('backup_path', '').strip()
        backup_subdir = request.form.get('backup_subdir', '').strip() or 'gift_backups'
        # V9 新增：配置别称（仅管理员全局配置保存；供普通用户引用时展示）
        config_alias = request.form.get('config_alias', '').strip()
        # 加密密码（管理员预设的自动备份加密密码）
        encrypt_pwd = request.form.get('backup_encrypt_password', '').strip()
        # V3: 管理员全局配置项 - 是否允许普通用户查看他人任务
        allow_view_others = request.form.get('allow_view_others_tasks', '') == 'on'
        # V10.2 新增：定时任务操作权限细化
        allow_edit_others = request.form.get('allow_edit_others_tasks', '') == 'on'
        allow_delete_others = request.form.get('allow_delete_others_tasks', '') == 'on'

        # V9: 普通用户手动保存自己的配置时，视为脱离管理员引用（地址/账号将正常回显）
        if not current_user.is_admin:
            config.adopted_from_admin = False

        # V10.4: 移除空值校验——允许普通用户保存空配置（停用引用、自行配置场景）
        config.webdav_url = server_url
        config.webdav_username = username
        if password:
            config.set_webdav_password(password)
        if remote_dir:
            config.backup_path = remote_dir
        config.backup_subdir = backup_subdir
        # V9: 仅管理员全局配置保存别称
        if current_user.is_admin:
            config.config_alias = config_alias or None
        # V3: 仅管理员全局配置才保存 allow_view_others_tasks
        if current_user.is_admin:
            config.allow_view_others_tasks = allow_view_others
            # V10.2 新增：保存定时任务编辑/删除他人权限
            config.allow_edit_others_tasks = allow_edit_others
            config.allow_delete_others_tasks = allow_delete_others
        # 保存或清除加密密码
        # V7 修复：勾选「清除」时优先执行清除（原逻辑先判断新密码分支，
        # 勾选清除+密码框留空时清除动作会被吞掉，导致清除失效）
        if 'backup_encrypt_password_clear' in request.form and request.form.get('backup_encrypt_password_clear'):
            config.backup_encrypt_password = None
        elif encrypt_pwd:
            config.backup_encrypt_password = encrypt_pwd

        db.session.commit()
        safe_log('更新WebDAV配置', f"服务器: {server_url}, 子目录: {backup_subdir}, 用户: {current_user.username}")
        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(), 'system',
                f'更新WebDAV配置',
                f'操作人：{current_user.username} | 页面：WebDAV备份 | 服务器：{server_url}',
                page_key='admin_backups', user_name=current_user.username,
                operator_id=current_user.id
            )
        except Exception:
            pass
        flash('WebDAV 备份配置已保存！', 'success')
        return redirect(url_for('admin_backups'))

    @app.route('/admin/backups/trigger', methods=['POST'])
    @app.route('/admin/backup/create', methods=['POST'])
    @login_required
    def admin_trigger_webdav_backup():
        """手动触发创建 WebDAV 备份（支持加密）"""
        if not current_user.is_admin and not current_user.can_use_backup():
            flash('权限不足', 'danger')
            return redirect(url_for('admin_backups'))

        # V3: 按用户获取配置
        config = BackupConfig.get_config(None if current_user.is_admin else current_user.id)
        if not config.server_url or not config.username:
            flash('请先完善 WebDAV 配置！', 'warning')
            return redirect(url_for('admin_backups'))

        db_path = app.config.get('SQLALCHEMY_DATABASE_URI', '').replace('sqlite:///', '')

        # V10.10.17: PG 模式用 JSON 导出替代文件复制
        if _is_pg_mode():
            tmp_dir = _tempfile.mkdtemp(prefix='gift_pg_backup_')
            _scoped_db_path = os.path.join(tmp_dir, 'pg_backup.sql')
            _is_temp_db = True
            if not current_user.is_admin:
                _pg_export_database(_scoped_db_path, user_filter=current_user.id)
            else:
                _pg_export_database(_scoped_db_path)
        else:
            # V7 修复：普通用户备份时生成仅含本人数据的临时库，防止越权获取他人数据
            _scoped_db_path = db_path
            _is_temp_db = False
            if not current_user.is_admin:
                try:
                    _scoped_db_path, _is_temp_db = build_user_scoped_backup_db(db_path, current_user)
                except Exception as se:
                    flash(f'生成用户备份数据失败: {str(se)}', 'danger')
                    return redirect(url_for('admin_backups'))

        # 是否加密：手动操作时用户可选择是否加密及密码
        encrypt_password = None
        use_encrypt = request.form.get('use_encrypt', '') == 'on'
        manual_pwd = request.form.get('manual_encrypt_password', '').strip()
        if use_encrypt:
            if manual_pwd:
                encrypt_password = manual_pwd
            else:
                # 使用管理员预设的加密密码
                encrypt_password = config.backup_encrypt_password
            if not encrypt_password:
                flash('已选择加密备份，但未提供加密密码！请输入密码或在配置中预设。', 'warning')
                return redirect(url_for('admin_backups'))

        # V3: 备份文件名中嵌入用户标识
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        username_tag = current_user.username
        _backup_ext = '.sql' if _is_pg_mode() else '.db'
        remote_filename = f"{timestamp}_{username_tag}_db_backup{_backup_ext}"
        if encrypt_password:
            remote_filename = f"{timestamp}_{username_tag}_db_backup.zip"

        # 使用 upload_encrypted_backup 但指定 remote_filename
        if encrypt_password:
            success, msg = upload_encrypted_backup(config, local_file_path=_scoped_db_path, encrypt_password=encrypt_password, remote_filename=remote_filename)
        else:
            success, msg = upload_backup_webdav(config, local_file_path=_scoped_db_path, remote_filename=remote_filename)
        # V7: 清理临时库
        if _is_temp_db:
            try:
                shutil.rmtree(os.path.dirname(_scoped_db_path), ignore_errors=True)
            except Exception:
                pass
        if success:
            config.last_backup_time = datetime.now()
            config.last_status = '备份成功'
            db.session.commit()
            safe_log('创建WebDAV备份', f"文件名: {msg}")
            try:
                trigger_webhook_event(
                    WebhookConfig.query.filter_by(is_enabled=True).all(), 'system',
                f'WebDAV备份成功',
                f'操作人：{current_user.username} | 页面：WebDAV备份 | 文件：{msg}',
                page_key='admin_backups', user_name=current_user.username,
                operator_id=current_user.id
                )
            except Exception:
                pass
            flash(f'备份成功上传至 WebDAV: {msg}', 'success')
        else:
            config.last_status = f'失败: {msg}'
            db.session.commit()
            # V10.9: 备份失败也推送通知
            try:
                trigger_webhook_event(
                    WebhookConfig.query.filter_by(is_enabled=True).all(), 'security',
                    f'WebDAV备份失败',
                    f'操作人：{current_user.username} | 页面：WebDAV备份 | 失败原因：{msg}',
                    page_key='admin_backups', user_name=current_user.username,
                    operator_id=current_user.id
                )
            except Exception:
                pass
            flash(f'备份失败: {msg}', 'danger')
        return redirect(url_for('admin_backups'))

    @app.route('/admin/backup/download_local')
    @login_required
    def admin_download_local_backup():
        """一键下载当前本地数据库备份文件（离线备份）"""
        if not current_user.is_admin and not current_user.can_use_backup():
            flash('权限不足', 'danger')
            return redirect(url_for('index'))

        # V10.10.17: PG 模式用 JSON 导出替代文件下载
        if _is_pg_mode():
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f"gift_bookkeeping_backup_{timestamp}.sql"
            safe_log('下载本地备份', f"下载了当前数据库备份文件: {filename}")
            tmp_dir = _tempfile.mkdtemp(prefix='gift_pg_dl_')
            tmp_sql = os.path.join(tmp_dir, filename)
            if not current_user.is_admin:
                _pg_export_database(tmp_sql, user_filter=current_user.id)
            else:
                _pg_export_database(tmp_sql)
            from flask import after_this_request
            @after_this_request
            def _cleanup_pg_download(response):
                try:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                except Exception:
                    pass
                return response
            return send_from_directory(tmp_dir, filename, as_attachment=True)

        db_path = app.config.get('SQLALCHEMY_DATABASE_URI', '').replace('sqlite:///', '')
        if not os.path.exists(db_path):
            flash('数据库文件不存在！', 'danger')
            return redirect(url_for('admin_backups'))

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"gift_bookkeeping_backup_{timestamp}.db"
        safe_log('下载本地备份', f"下载了当前数据库备份文件: {filename}")
        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(), 'security',
                f'下载本地备份',
                f'操作人：{current_user.username} | 页面：WebDAV备份 | 文件：{filename}',
                page_key='admin_backups', user_name=current_user.username,
                operator_id=current_user.id
            )
        except Exception:
            pass

        # V7 修复：普通用户下载时生成仅含本人数据的临时库，防止越权获取他人数据
        if not current_user.is_admin:
            try:
                scoped_path, is_temp = build_user_scoped_backup_db(db_path, current_user)
            except Exception as se:
                flash(f'生成用户备份数据失败: {str(se)}', 'danger')
                return redirect(url_for('admin_backups'))
            if is_temp:
                # 用临时文件发送；使用 after_this_request 在响应发送完毕后清理临时目录
                tmp_dir = os.path.dirname(scoped_path)
                from flask import after_this_request

                @after_this_request
                def _cleanup_scoped_backup(response):
                    try:
                        shutil.rmtree(tmp_dir, ignore_errors=True)
                    except Exception:
                        pass
                    return response

                return send_from_directory(
                    tmp_dir, os.path.basename(scoped_path),
                    as_attachment=True, download_name=filename
                )

        return send_from_directory(
            os.path.dirname(os.path.abspath(db_path)),
            os.path.basename(db_path),
            as_attachment=True,
            download_name=filename
        )

    @app.route('/admin/backup/upload_local', methods=['POST'])
    @login_required
    def admin_upload_local_backup():
        """上传本地 .db 备份文件并恢复"""
        if not current_user.is_admin and not current_user.can_use_backup():
            flash('权限不足', 'danger')
            return redirect(url_for('index'))

        file = request.files.get('backup_file')
        if not file or not file.filename:
            flash('请选择需要上传恢复的备份文件！', 'warning')
            return redirect(url_for('admin_backups'))

        # V10.10.17: PG 模式接受 .sql 文件
        is_pg = _is_pg_mode()
        _valid_ext = '.sql' if is_pg else '.db'
        if not file.filename.endswith(_valid_ext):
            flash(f'仅支持恢复 {_valid_ext} 格式的备份文件！', 'danger')
            return redirect(url_for('admin_backups'))

        # V10.10.17: PG 模式恢复分支——使用 _pg_import_database 替代文件级替换
        if is_pg:
            import tempfile
            tmp_dir = tempfile.mkdtemp(prefix='gift_pg_restore_')
            tmp_sql = os.path.join(tmp_dir, 'uploaded.sql')
            file.save(tmp_sql)
            try:
                if current_user.is_admin:
                    ok, msg, stats = _pg_import_database(tmp_sql)
                else:
                    ok, msg, stats = _pg_import_database(tmp_sql, user_filter=current_user.id)
                if ok:
                    safe_log('上传恢复备份', f'PG恢复成功: {msg}')
                    flash(f'恢复成功: {msg}', 'success')
                else:
                    flash(f'恢复失败: {msg}', 'danger')
            except Exception as e:
                flash(f'恢复失败: {str(e)}', 'danger')
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            return redirect(url_for('admin_backups'))

        db_path = app.config.get('SQLALCHEMY_DATABASE_URI', '').replace('sqlite:///', '')
        try:
            # V9 修复：普通用户的过滤备份不能文件级替换主库（会导致 users 表丢失、全站 500），
            # 改为数据级合并：只把备份中本人三张业务表数据合回主库
            # V10.7 收紧：文件级替换仅限管理员；拥有他人查看权限（ledger level>=1）的普通用户
            # 也必须走数据级合并，防止上传他人/完整/结构不一致的 .db 文件级覆盖主库导致全站 500
            _is_full_restore = current_user.is_admin
            if not _is_full_restore:
                import tempfile
                tmp_dir = tempfile.mkdtemp(prefix='gift_upload_')
                tmp_db_path = os.path.join(tmp_dir, 'uploaded.db')
                file.save(tmp_db_path)

                # V10.7：文件归属校验——仅允许上传本人系统生成的备份文件
                # 兼容两种系统命名格式：
                #   1) WebDAV/定时备份格式: YYYYMMDD_HHMMSS_用户名_db_backup.db
                #   2) 本地下载格式:       gift_bookkeeping_backup_YYYYMMDD_HHMMSS.db（不含用户名，跳过归属比对）
                import re as _re_upload
                _fn = file.filename or ''
                _fn_m = _re_upload.match(r'(\d{8}_\d{6})_(.+?)_(db_backup|file_backup|custom)\.(db|zip)', _fn)
                _fn_local = _re_upload.match(r'gift_bookkeeping_backup_(\d{8}_\d{6})\.db$', _fn)
                if not _fn_m and not _fn_local:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                    flash('恢复失败：上传文件不符合系统备份命名规则，仅支持上传本人在本系统生成的备份文件！', 'danger')
                    return redirect(url_for('admin_backups'))
                if _fn_m and _fn_m.group(2) != current_user.username:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                    flash('恢复失败：该备份文件不属于当前账号，仅能恢复本人的备份文件！', 'danger')
                    return redirect(url_for('admin_backups'))

                # 校验上传的数据库文件完整性
                import sqlite3 as _sqlite3
                try:
                    test_conn = _sqlite3.connect(tmp_db_path)
                    integrity = test_conn.execute('PRAGMA integrity_check').fetchone()[0]
                    test_conn.close()
                    if integrity != 'ok':
                        shutil.rmtree(tmp_dir, ignore_errors=True)
                        flash(f'恢复失败：上传的数据库文件完整性检查未通过 ({integrity})，可能文件已损坏', 'danger')
                        return redirect(url_for('admin_backups'))
                except Exception as ie:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                    flash(f'恢复失败：无法读取上传的数据库文件 ({str(ie)})', 'danger')
                    return redirect(url_for('admin_backups'))

                # 防锁：合并前先提交请求内未提交事务并释放连接池，确保主库写锁可获取
                try:
                    db.session.commit()
                except Exception:
                    pass
                db.engine.dispose()

                ok, msg, stats = merge_user_scoped_backup(db_path, tmp_db_path, current_user)
                shutil.rmtree(tmp_dir, ignore_errors=True)
                if ok:
                    safe_log('上传恢复本地备份', f"普通用户 {current_user.username} 数据级合并恢复成功: {msg}")
                    # V10.3: 补充普通用户恢复推送
                    try:
                        trigger_webhook_event(
                            WebhookConfig.query.filter_by(is_enabled=True).all(), 'restore',
                            f'{current_user.username} 恢复本地备份',
                            f'操作人：{current_user.username} | 页面：WebDAV备份 | 操作：上传恢复 | 方式：数据级合并 | 结果：成功',
                            page_key='admin_backups', user_name=current_user.username, operator_id=current_user.id
                        )
                    except Exception:
                        pass
                    flash(f'已成功恢复您的个人数据（{msg}），系统与其他用户数据不受影响。', 'success')
                else:
                    flash(f'恢复失败：{msg}', 'danger')
                return redirect(url_for('admin_backups'))

            # 管理员：保持原文件级替换逻辑（V5: 改用 sqlite3.backup() 原子操作，避免 WAL 不一致）
            # 第1步：释放数据库连接池，防止覆盖正在使用的文件导致损坏
            db.engine.dispose()
            import time as _time
            _time.sleep(0.5)

            # 第2步：保存上传文件到临时路径，验证完整性后再替换
            import tempfile
            tmp_dir = tempfile.mkdtemp(prefix='gift_upload_')
            tmp_db_path = os.path.join(tmp_dir, 'uploaded.db')
            file.save(tmp_db_path)

            # 第3步：验证上传的数据库文件完整性
            import sqlite3 as _sqlite3
            try:
                test_conn = _sqlite3.connect(tmp_db_path)
                integrity = test_conn.execute('PRAGMA integrity_check').fetchone()[0]
                test_conn.close()
                if integrity != 'ok':
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                    flash(f'恢复失败：上传的数据库文件完整性检查未通过 ({integrity})，可能文件已损坏', 'danger')
                    return redirect(url_for('admin_backups'))
            except Exception as ie:
                shutil.rmtree(tmp_dir, ignore_errors=True)
                flash(f'恢复失败：无法读取上传的数据库文件 ({str(ie)})', 'danger')
                return redirect(url_for('admin_backups'))

            # 第4步：备份现有数据库防止损坏
            if os.path.exists(db_path):
                shutil.copy2(db_path, db_path + f".bak_{int(time.time())}")

            # 第5步：用 sqlite3.backup() 原子性替换数据库内容（不操作文件，避免 WAL 不一致）
            try:
                # 先删除 WAL/SHM 文件，确保干净状态
                for suffix in ('-wal', '-shm'):
                    p = db_path + suffix
                    if os.path.exists(p):
                        try:
                            os.remove(p)
                        except Exception:
                            pass
                src_conn = _sqlite3.connect(tmp_db_path)
                dst_conn = _sqlite3.connect(db_path)
                src_conn.backup(dst_conn)
                dst_conn.close()
                src_conn.close()
            except Exception as backup_err:
                shutil.rmtree(tmp_dir, ignore_errors=True)
                flash(f'恢复失败：数据库原子复制失败 ({str(backup_err)})', 'danger')
                return redirect(url_for('admin_backups'))

            # 第6步：清理 WAL/SHM 文件（此时连接已关闭，文件句柄已释放）
            for suffix in ('-wal', '-shm'):
                wal_path = db_path + suffix
                if os.path.exists(wal_path):
                    try:
                        os.remove(wal_path)
                    except Exception:
                        pass

            shutil.rmtree(tmp_dir, ignore_errors=True)
            
            # 重新执行数据库初始化（补建缺失的表/字段）
            try:
                from app import init_database
                init_database()
            except Exception as e:
                safe_log('数据库迁移', f"init_database 执行失败: {e}")
            
            safe_log('上传恢复本地备份', f"成功恢复了上传的数据库文件: {file.filename}")
            # V10.3: 补充管理员恢复推送
            try:
                trigger_webhook_event(
                    WebhookConfig.query.filter_by(is_enabled=True).all(), 'restore',
                    f'{current_user.username} 恢复本地备份',
                    f'操作人：{current_user.username} | 页面：WebDAV备份 | 操作：上传恢复 | 方式：文件级替换 | 文件：{file.filename} | 结果：成功',
                    page_key='admin_backups', user_name=current_user.username, operator_id=current_user.id
                )
            except Exception:
                pass
            flash('本地数据库已成功恢复，请刷新页面确认数据更新。', 'success')
        except Exception as e:
            flash(f'恢复数据库文件失败: {e}', 'danger')

        return redirect(url_for('admin_backups'))

    @app.route('/admin/backup/upload_attachment', methods=['POST'])
    @login_required
    def admin_upload_attachment():
        """上传附件文件（非 .db 的其他备份文件）"""
        if not current_user.is_admin and not current_user.can_use_backup():
            flash('权限不足', 'danger')
            return redirect(url_for('index'))

        file = request.files.get('attachment_file')
        if not file or not file.filename:
            flash('请选择需要上传的附件文件！', 'warning')
            return redirect(url_for('admin_backups'))

        allowed_extensions = ('.json', '.csv', '.txt', '.xlsx', '.docx', '.zip')
        if not file.filename.lower().endswith(allowed_extensions):
            flash('仅支持 JSON、CSV、TXT、XLSX、DOCX、ZIP 格式文件！', 'danger')
            return redirect(url_for('admin_backups'))

        # 保存到 data/attachments 目录
        attachments_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'attachments')
        os.makedirs(attachments_dir, exist_ok=True)
        filename = file.filename
        save_path = os.path.join(attachments_dir, filename)
        try:
            file.save(save_path)
            safe_log('上传附件文件', f"文件: {filename}")
            try:
                trigger_webhook_event(
                    WebhookConfig.query.filter_by(is_enabled=True).all(), 'system',
                    f'上传附件文件',
                    f'操作人：{current_user.username} | 页面：WebDAV备份 | 文件：{filename}',
                    page_key='admin_backups', user_name=current_user.username,
                    operator_id=current_user.id
                )
            except Exception:
                pass

            # V3: 尝试上传到 WebDAV
            try:
                config = BackupConfig.get_config(None if current_user.is_admin else current_user.id)
                if config.server_url and config.username:
                    # 修复：附件直接放到备份根目录下，不嵌套子目录，避免 409 错误
                    remote_name = filename
                    ok_wd, msg_wd = upload_file_to_webdav(config, local_file_path=save_path, remote_filename=remote_name)
                    if ok_wd:
                        flash(f'附件文件「{filename}」已成功上传至本地和 WebDAV！', 'success')
                    else:
                        flash(f'附件文件「{filename}」已保存到本地，WebDAV 上传失败: {msg_wd}', 'warning')
                else:
                    flash(f'附件文件「{filename}」已成功上传至本地！（未配置 WebDAV，跳过远端上传）', 'success')
            except Exception as wd_err:
                flash(f'附件文件「{filename}」已保存到本地，WebDAV 上传异常: {wd_err}', 'warning')
        except Exception as e:
            flash(f'上传附件文件失败: {e}', 'danger')

        return redirect(url_for('admin_backups'))

    @app.route('/admin/backups/restore', methods=['POST'])
    @app.route('/admin/backup/restore', methods=['POST'])
    @app.route('/admin/backups/restore/<path:filename>', methods=['POST'])
    @login_required
    def admin_restore_webdav_backup(filename=None):
        """从 WebDAV 恢复备份"""
        if not current_user.is_admin and not current_user.can_use_backup():
            flash('权限不足', 'danger')
            return redirect(url_for('index'))

        target_filename = filename or request.form.get('filename', '').strip() or request.args.get('filename', '').strip()
        if not target_filename:
            flash('未指定备份文件', 'warning')
            return redirect(url_for('admin_backups'))

        # V6: 三级权限判断 - 管理员数据保护 + 级别权限
        import re as _re
        m = _re.match(r'(\d{8}_\d{6})_(.+?)_(db_backup|file_backup|custom)\.(db|zip)', target_filename)
        if m:
            created_by = m.group(2)
            # V6: 查询管理员用户名列表
            _admin_usernames = {u.username for u in User.query.filter_by(is_admin=True).all()}
            _is_creator_admin = created_by in _admin_usernames
            if _is_creator_admin and not current_user.is_admin:
                flash('权限不足：管理员创建的备份只有管理员可恢复', 'danger')
                return redirect(url_for('admin_backups'))
            if not current_user.is_admin and created_by != current_user.username:
                if not current_user.can_edit_others_backup():
                    flash('权限不足：只能恢复自己创建的备份文件', 'danger')
                    return redirect(url_for('admin_backups'))

        # V3: 按用户获取配置
        config = BackupConfig.get_config(None if current_user.is_admin else current_user.id)
        db_path = app.config.get('SQLALCHEMY_DATABASE_URI', '').replace('sqlite:///', '')
        
        # 判断是否是加密 zip 文件
        is_encrypted_zip = target_filename.lower().endswith('.zip')
        decrypt_password = request.form.get('decrypt_password', '').strip() if is_encrypted_zip else None
        
        if is_encrypted_zip and not decrypt_password:
            # 加密文件但未提供密码，重定向回页面并提示需要密码
            flash(f'备份文件「{target_filename}」是加密文件，请输入加密密码后恢复。', 'warning')
            return redirect(url_for('admin_backups'))
        
        try:
            # 修复：先释放数据库连接池，防止覆盖正在使用的文件导致损坏
            db.engine.dispose()
            
            # 下载到临时文件，验证完整性后再替换
            import tempfile
            tmp_dir = tempfile.mkdtemp(prefix='gift_restore_')

            # V10.10.17: PG 模式恢复分支
            _is_pg = _is_pg_mode()
            if _is_pg:
                tmp_db_path = os.path.join(tmp_dir, 'restored.sql')
            else:
                tmp_db_path = os.path.join(tmp_dir, 'restored.db')

            if is_encrypted_zip:
                # 加密 zip：下载并解密到临时文件
                success, msg = download_and_decrypt_backup(config, remote_filename=target_filename, save_path=tmp_db_path, decrypt_password=decrypt_password)
            else:
                # 普通 .db/.sql：直接下载到临时文件
                success, msg = download_webdav_backup(config, remote_filename=target_filename, save_path=tmp_db_path)

            if success:
                # V10.10.17: PG 模式恢复
                if _is_pg:
                    try:
                        if current_user.is_admin:
                            ok, msg, stats = _pg_import_database(tmp_db_path)
                        else:
                            ok, msg, stats = _pg_import_database(tmp_db_path, user_filter=current_user.id)
                        shutil.rmtree(tmp_dir, ignore_errors=True)
                        if ok:
                            safe_log('恢复WebDAV备份', f"PG恢复成功（文件: {target_filename}）: {msg}")
                            flash(f'恢复成功: {msg}', 'success')
                        else:
                            flash(f'恢复失败: {msg}', 'danger')
                    except Exception as e:
                        shutil.rmtree(tmp_dir, ignore_errors=True)
                        flash(f'恢复失败: {str(e)}', 'danger')
                    return redirect(url_for('admin_backups'))

                # 验证下载数据库的完整性
                import sqlite3 as _sqlite3
                try:
                    test_conn = _sqlite3.connect(tmp_db_path)
                    integrity = test_conn.execute('PRAGMA integrity_check').fetchone()[0]
                    test_conn.close()
                    if integrity != 'ok':
                        shutil.rmtree(tmp_dir, ignore_errors=True)
                        flash(f'恢复失败：下载数据库文件完整性检查未通过 ({integrity})，可能文件已损坏', 'danger')
                        return redirect(url_for('admin_backups'))
                except Exception as ie:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                    flash(f'恢复失败：无法读取下载数据库文件 ({str(ie)})', 'danger')
                    return redirect(url_for('admin_backups'))

                # V9 修复：普通用户的过滤备份不能文件级替换主库（users 表丢失 → 全站 500），
                # 改为数据级合并：只把备份中本人三张业务表数据合回主库
                # V10.7：文件级恢复仅限管理员（含他人查看权限的普通用户也必须数据级合并）
                _is_full_restore = current_user.is_admin
                if not _is_full_restore:
                    # 防锁：合并前先提交请求内未提交事务并释放连接池，确保主库写锁可获取
                    try:
                        db.session.commit()
                    except Exception:
                        pass
                    db.engine.dispose()

                    ok, msg, stats = merge_user_scoped_backup(db_path, tmp_db_path, current_user)
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                    if ok:
                        safe_log('恢复WebDAV备份', f"普通用户 {current_user.username} 数据级合并恢复成功（文件: {target_filename}）: {msg}")
                        try:
                            trigger_webhook_event(
                                WebhookConfig.query.filter_by(is_enabled=True).all(), 'restore',
                                f'恢复WebDAV备份',
                                f'操作人：{current_user.username} | 页面：WebDAV备份 | 文件：{target_filename} | 方式：数据级合并',
                                page_key='admin_backups', user_name=current_user.username,
                                operator_id=current_user.id
                            )
                        except Exception:
                            pass
                        flash(f'已成功恢复您的个人数据（{msg}），系统与其他用户数据不受影响。', 'success')
                    else:
                        flash(f'恢复失败：{msg}', 'danger')
                    return redirect(url_for('admin_backups'))

                # 管理员：保持原文件级替换逻辑（V5: 改用 sqlite3.backup() 原子操作，避免 WAL 不一致）
                # 第4步：备份当前数据库防止恢复失败
                if os.path.exists(db_path):
                    shutil.copy2(db_path, db_path + f".bak_{int(time.time())}")

                # 第5步：用 sqlite3.backup() 原子性替换数据库内容（不操作文件，避免 WAL 不一致）
                try:
                    # 先删除 WAL/SHM 文件，确保干净状态
                    for suffix in ('-wal', '-shm'):
                        p = db_path + suffix
                        if os.path.exists(p):
                            try:
                                os.remove(p)
                            except Exception:
                                pass
                    src_conn = _sqlite3.connect(tmp_db_path)
                    dst_conn = _sqlite3.connect(db_path)
                    src_conn.backup(dst_conn)
                    dst_conn.close()
                    src_conn.close()
                except Exception as backup_err:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                    flash(f'恢复失败：数据库原子复制失败 ({str(backup_err)})', 'danger')
                    return redirect(url_for('admin_backups'))

                # 第6步：清理 WAL/SHM 文件（连接已关闭，文件句柄已释放）
                for suffix in ('-wal', '-shm'):
                    wal_path = db_path + suffix
                    if os.path.exists(wal_path):
                        try:
                            os.remove(wal_path)
                        except Exception:
                            pass

                shutil.rmtree(tmp_dir, ignore_errors=True)
                
                # 重新执行数据库初始化（补建缺失的表/字段）
                try:
                    from app import init_database
                    init_database()
                except Exception as e:
                    safe_log('数据库迁移', f"init_database 执行失败: {e}")
                
                safe_log('恢复WebDAV备份', f"文件名: {target_filename}")
                try:
                    trigger_webhook_event(
                        WebhookConfig.query.filter_by(is_enabled=True).all(), 'system',
                    f'恢复WebDAV备份',
                    f'操作人：{current_user.username} | 页面：WebDAV备份 | 文件：{target_filename}',
                    page_key='admin_backups', user_name=current_user.username,
                    operator_id=current_user.id
                    )
                except Exception:
                    pass
                flash('备份已成功恢复，请刷新页面确认数据更新。', 'success')
            else:
                shutil.rmtree(tmp_dir, ignore_errors=True)
                flash(f'恢复失败: {msg}', 'danger')
        except Exception as e:
            flash(f'恢复备份时发生异常: {str(e)}', 'danger')
            safe_log('恢复WebDAV备份失败', f"文件名: {target_filename}, 错误: {str(e)}")
        return redirect(url_for('admin_backups'))

    @app.route('/admin/backups/test_connection', methods=['POST'])
    @login_required
    def admin_test_webdav():
        """测试 WebDAV 连接（兼容 JSON 与 Form 表单格式）"""
        if not current_user.is_admin and not current_user.can_use_backup():
            return jsonify({'success': False, 'message': '权限不足'}), 403
        # V3: 按用户获取配置
        config = BackupConfig.get_config(None if current_user.is_admin else current_user.id)
        if request.is_json:
            data = request.get_json() or {}
        else:
            data = request.form
        server_url = (data.get('webdav_url') or data.get('server_url') or '').strip() or config.server_url
        username = (data.get('webdav_username') or data.get('username') or '').strip() or config.webdav_username
        password = (data.get('webdav_password') or data.get('password') or '').strip() or config.password
        backup_path = (data.get('backup_path') or data.get('remote_dir') or '').strip() or getattr(config, 'backup_path', '')
        ok, msg = test_webdav_connection(server_url, username, password, backup_path=backup_path)
        safe_log('测试WebDAV连接', f"结果: {'成功' if ok else '失败'}, 消息: {msg}")
        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(), 'system',
                f'测试WebDAV连接',
                f'操作人：{current_user.username} | 页面：WebDAV备份 | 结果：{"成功" if ok else "失败"}',
                page_key='admin_backups', user_name=current_user.username,
                operator_id=current_user.id
            )
        except Exception:
            pass
        return jsonify({'success': ok, 'message': msg})

    @app.route('/admin/backups/delete', methods=['POST'])
    @login_required
    def admin_delete_webdav_backup():
        """删除 WebDAV 远端备份文件（支持批量删除）"""
        if not current_user.is_admin and not current_user.can_use_backup():
            return jsonify({'success': False, 'message': '权限不足'}), 403

        # V3: 权限判断 - 仅管理员或备份创建者本人可删除
        filenames_str = request.form.get('filenames', '').strip()
        if not filenames_str and request.is_json:
            json_data = request.get_json(silent=True) or {}
            filenames_str = json_data.get('filenames', '')
        filenames = [f.strip() for f in filenames_str.split(',') if f.strip()] if filenames_str else []
        if not filenames:
            # 也支持单个 filename 参数
            single_fn = request.form.get('filename', '').strip()
            if single_fn:
                filenames = [single_fn]
        if not filenames:
            return jsonify({'success': False, 'message': '未指定要删除的文件'}), 400

        import re as _re
        config = BackupConfig.get_config(None if current_user.is_admin else current_user.id)
        if not config.server_url or not config.username:
            return jsonify({'success': False, 'message': '未配置 WebDAV'}), 400

        results = []
        all_success = True
        # V6: 查询实际管理员用户名列表，不硬编码
        _admin_usernames = set()
        _admin_users = User.query.filter_by(is_admin=True).all()
        _admin_usernames = {u.username for u in _admin_users}
        for fn in filenames:
            # V6: 三级权限判断 + 管理员数据保护
            m = _re.match(r'(\d{8}_\d{6})_(.+?)_(db_backup|file_backup|custom)\.(db|zip)', fn)
            if m:
                created_by = m.group(2)
                _is_creator_admin = created_by in _admin_usernames
                if _is_creator_admin and not current_user.is_admin:
                    results.append({'filename': fn, 'success': False, 'message': '权限不足：管理员创建的备份只有管理员可删除'})
                    all_success = False
                    continue
                if not current_user.is_admin and created_by != current_user.username:
                    if not current_user.can_delete_others_backup():
                        results.append({'filename': fn, 'success': False, 'message': '权限不足：只能删除自己创建的备份'})
                        all_success = False
                        continue
            ok, msg = delete_webdav_backup(config, remote_filename=fn)
            results.append({'filename': fn, 'success': ok, 'message': msg})
            if not ok:
                all_success = False

        safe_log('删除WebDAV备份', f"文件: {', '.join(filenames)}, 结果: {'全部成功' if all_success else '部分失败'}")
        # V10.3: 补充删除WebDAV备份推送
        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(), 'delete',
                f'{current_user.username} 删除WebDAV备份',
                f"操作人：{current_user.username} | 页面：WebDAV备份 | 文件：{', '.join(filenames)} | 结果：{'全部成功' if all_success else '部分失败'}",
                page_key='admin_backups', user_name=current_user.username, operator_id=current_user.id
            )
        except Exception:
            pass
        return jsonify({'success': all_success, 'results': results})

    # ===================== 定时备份任务管理 =====================

    @app.route('/admin/backups/scheduled_tasks', methods=['POST'])
    @login_required
    def admin_save_scheduled_task():
        """创建或更新定时备份任务"""
        if not current_user.is_admin and not current_user.can_use_scheduled_tasks():
            return jsonify({'success': False, 'message': '权限不足'}), 403

        task_id = request.form.get('task_id', '').strip()
        name = request.form.get('task_name', '').strip() or '定时任务'
        cron_expr = request.form.get('cron_expr', '').strip() or '0 2 * * *'
        is_enabled = request.form.get('is_enabled', '') == 'on'
        task_type = request.form.get('task_type', 'db_backup').strip()
        target_files = request.form.get('target_files', '').strip()
        custom_script = request.form.get('custom_script', '').strip()
        # V3: 读取加密复选框
        encrypt_enabled = request.form.get('encrypt_enabled', '') == 'on'

        if task_type not in ('db_backup', 'file_backup', 'custom'):
            task_type = 'db_backup'

        if task_id:
            task = db.session.get(ScheduledBackupTask, int(task_id))
            if not task:
                flash('定时任务不存在', 'danger')
                return redirect(url_for('admin_backups'))
            # V10.2: 权限细化 - 普通用户可编辑自己的任务，或被授权编辑他人任务
            if not current_user.is_admin and task.created_by != current_user.id and not current_user.can_edit_others_scheduled_tasks():
                flash('权限不足：只能编辑自己的定时任务', 'danger')
                return redirect(url_for('admin_backups'))
            task.name = name
            task.cron_expr = cron_expr
            task.is_enabled = is_enabled
            task.task_type = task_type
            task.target_files = target_files if task_type == 'file_backup' else None
            task.custom_script = custom_script if task_type == 'custom' else None
            task.encrypt_enabled = encrypt_enabled
        else:
            task = ScheduledBackupTask(
                name=name,
                cron_expr=cron_expr,
                is_enabled=is_enabled,
                task_type=task_type,
                target_files=target_files if task_type == 'file_backup' else None,
                custom_script=custom_script if task_type == 'custom' else None,
                encrypt_enabled=encrypt_enabled,
                created_by=current_user.id  # V3: 记录创建者
            )
            db.session.add(task)

        db.session.commit()
        safe_log('保存定时备份任务', f'任务: {name}, Cron: {cron_expr}, 启用: {is_enabled}, 加密: {encrypt_enabled}', user=current_user)
        try:
            _evt = 'create' if not task_id else 'update'
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(), _evt,
                f'定时任务「{name}」',
                f'操作人：{current_user.username} | 页面：WebDAV备份 | 任务：{name} | Cron：{cron_expr} | 启用：{"是" if is_enabled else "否"}',
                page_key='admin_backups', user_name=current_user.username,
                operator_id=current_user.id
            )
        except Exception:
            pass
        flash(f'定时备份任务「{name}」已保存', 'success')
        return redirect(url_for('admin_backups'))

    @app.route('/admin/backups/scheduled_tasks/<int:task_id>/delete', methods=['POST'])
    @login_required
    def admin_delete_scheduled_task(task_id):
        """删除定时备份任务"""
        if not current_user.is_admin and not current_user.can_use_scheduled_tasks():
            return jsonify({'success': False, 'message': '权限不足'}), 403

        task = db.session.get(ScheduledBackupTask, task_id)
        if not task:
            flash('定时任务不存在', 'danger')
            return redirect(url_for('admin_backups'))

        # V10.2: 权限细化 - 普通用户可删除自己的任务，或被授权删除他人任务
        if not current_user.is_admin and task.created_by != current_user.id and not current_user.can_delete_others_scheduled_tasks():
            flash('权限不足：只能删除自己的定时任务', 'danger')
            return redirect(url_for('admin_backups'))

        name = task.name
        db.session.delete(task)
        db.session.commit()
        safe_log('删除定时备份任务', f'任务: {name}', user=current_user)
        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(), 'delete',
                f'删除定时任务「{name}」',
                f'操作人：{current_user.username} | 页面：WebDAV备份 | 任务：{name}',
                page_key='admin_backups', user_name=current_user.username,
                operator_id=current_user.id
            )
        except Exception:
            pass
        flash(f'定时备份任务「{name}」已删除', 'info')
        return redirect(url_for('admin_backups'))

    @app.route('/admin/backups/scheduled_tasks/<int:task_id>/toggle', methods=['POST'])
    @login_required
    def admin_toggle_scheduled_task(task_id):
        """启用/禁用定时备份任务"""
        if not current_user.is_admin and not current_user.can_use_scheduled_tasks():
            return jsonify({'success': False, 'message': '权限不足'}), 403

        task = db.session.get(ScheduledBackupTask, task_id)
        if not task:
            return jsonify({'success': False, 'message': '任务不存在'}), 404

        # V10.2: 权限细化 - 普通用户可操作自己的任务，或被授权编辑他人任务
        if not current_user.is_admin and task.created_by != current_user.id and not current_user.can_edit_others_scheduled_tasks():
            return jsonify({'success': False, 'message': '权限不足：只能操作自己的定时任务'}), 403

        task.is_enabled = not task.is_enabled
        db.session.commit()
        safe_log('切换定时备份任务状态', f'任务: {task.name}, 状态: {"启用" if task.is_enabled else "禁用"}', user=current_user)
        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(), 'status_change',
                f'定时任务状态变更「{task.name}」',
                f'操作人：{current_user.username} | 页面：WebDAV备份 | 任务：{task.name} | 状态：{"启用" if task.is_enabled else "禁用"}',
                page_key='admin_backups', user_name=current_user.username,
                operator_id=current_user.id
            )
        except Exception:
            pass
        return jsonify({'success': True, 'is_enabled': task.is_enabled})

    @app.route('/admin/backups/scheduled_tasks/<int:task_id>/execution_logs', methods=['GET'])
    @login_required
    def admin_get_execution_logs(task_id):
        """获取定时任务的执行历史日志"""
        if not current_user.is_admin and not current_user.can_use_scheduled_tasks():
            return jsonify({'success': False, 'message': '权限不足'}), 403

        task = db.session.get(ScheduledBackupTask, task_id)
        if not task:
            return jsonify({'success': False, 'message': '任务不存在'}), 404

        # V10.2: 权限细化 - 普通用户可查看自己的任务，或被授权查看他人任务
        if not current_user.is_admin and task.created_by != current_user.id and not current_user.can_view_others_scheduled_tasks():
            return jsonify({'success': False, 'message': '权限不足'}), 403

        logs = ScheduledTaskExecutionLog.query.filter_by(task_id=task_id).order_by(ScheduledTaskExecutionLog.created_at.desc()).limit(50).all()
        log_list = []
        for log in logs:
            log_list.append({
                'id': log.id,
                'start_time': log.start_time.strftime('%Y-%m-%d %H:%M:%S') if log.start_time else '',
                'end_time': log.end_time.strftime('%Y-%m-%d %H:%M:%S') if log.end_time else '',
                'status': log.status,
                'output_log': (log.output_log or '')[:500],
                'executed_by': log.executed_by or 'system'
            })
        return jsonify({'success': True, 'logs': log_list, 'task_name': task.name})

    # ===================== 备份授权管理 =====================

    @app.route('/admin/backups/authorize/<int:user_id>', methods=['POST'])
    @login_required
    def admin_toggle_backup_auth(user_id):
        """切换用户的备份功能授权"""
        if not current_user.is_admin:
            return jsonify({'success': False, 'message': '权限不足'}), 403

        user = db.session.get(User, user_id)
        if not user:
            return jsonify({'success': False, 'message': '用户不存在'}), 404

        if user.is_admin:
            return jsonify({'success': False, 'message': '管理员默认拥有备份权限'}), 400

        user.backup_authorized = not user.backup_authorized
        db.session.commit()
        safe_log('切换备份授权', f'用户: {user.username}, 授权: {"是" if user.backup_authorized else "否"}', user=current_user)

        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(),
                'status_change',
                f'用户 {user.username} 的备份权限已{"授权" if user.backup_authorized else "撤销"}',
                page_key='admin_backups',
                user_name=current_user.username,
                operator_id=current_user.id
            )
        except Exception:
            pass

        return jsonify({'success': True, 'backup_authorized': user.backup_authorized})

    @app.route('/admin/backups/authorize_task/<int:user_id>', methods=['POST'])
    @login_required
    def admin_toggle_task_auth(user_id):
        """切换用户的定时任务功能授权"""
        if not current_user.is_admin:
            return jsonify({'success': False, 'message': '权限不足'}), 403

        user = db.session.get(User, user_id)
        if not user:
            return jsonify({'success': False, 'message': '用户不存在'}), 404

        if user.is_admin:
            return jsonify({'success': False, 'message': '管理员默认拥有定时任务权限'}), 400

        user.scheduled_task_authorized = not user.scheduled_task_authorized
        db.session.commit()
        safe_log('切换定时任务授权', f'用户: {user.username}, 授权: {"是" if user.scheduled_task_authorized else "否"}', user=current_user)

        # V10.2: 补充缺失的 Webhook 推送（与 admin_toggle_backup_auth 对齐）
        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(),
                'status_change',
                f'用户 {user.username} 的定时任务权限已{"授权" if user.scheduled_task_authorized else "撤销"}',
                page_key='admin_backups',
                user_name=current_user.username,
                operator_id=current_user.id
            )
        except Exception:
            pass

        return jsonify({'success': True, 'scheduled_task_authorized': user.scheduled_task_authorized})

    @app.route('/admin/backups/save_task_permissions', methods=['POST'])
    @login_required
    def admin_save_task_permissions():
        """V10.2: 保存定时任务全局权限开关（仅管理员）"""
        if not current_user.is_admin:
            return jsonify({'success': False, 'message': '权限不足'}), 403
        data = request.get_json(silent=True) or {}
        config = BackupConfig.get_config(None)
        config.allow_view_others_tasks = bool(data.get('allow_view_others_tasks', False))
        config.allow_edit_others_tasks = bool(data.get('allow_edit_others_tasks', False))
        config.allow_delete_others_tasks = bool(data.get('allow_delete_others_tasks', False))
        db.session.commit()
        safe_log('更新定时任务权限', f'查看他人: {config.allow_view_others_tasks}, 编辑他人: {config.allow_edit_others_tasks}, 删除他人: {config.allow_delete_others_tasks}', user=current_user)
        # V10.3: 补充定时任务权限设置推送
        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(), 'system',
                f'{current_user.username} 更新定时任务权限',
                f'操作人：{current_user.username} | 页面：WebDAV备份 | 查看：{config.allow_view_others_tasks} | 编辑：{config.allow_edit_others_tasks} | 删除：{config.allow_delete_others_tasks}',
                page_key='admin_backups', user_name=current_user.username, operator_id=current_user.id
            )
        except Exception:
            pass
        return jsonify({'success': True})

    @app.route('/manifest.json')
    def pwa_manifest():
        """PWA 清单文件"""
        return send_from_directory('static', 'manifest.json', mimetype='application/manifest+json')

    @app.route('/sw.js')
    def pwa_sw():
        """Service Worker 脚本"""
        response = make_response(send_from_directory('static', 'sw.js', mimetype='application/javascript'))
        response.headers['Service-Worker-Allowed'] = '/'
        return response

    @app.route('/shared/<token>', methods=['GET', 'POST'])
    def shared_ledger_view(token):
        """免登录只读共享视图"""
        share = SharedLedgerLink.query.filter_by(share_token=token, is_active=True).first()

        if not share:
            flash('共享链接已失效或不存在', 'danger')
            return redirect(url_for('login'))

        if share.expires_at and share.expires_at < datetime.now():
            flash('该分享链接已超时失效', 'warning')
            return redirect(url_for('login'))

        # 校验访问密码
        needs_password = False
        if share.access_password:
            auth_key = f'shared_auth_{token}'
            if request.method == 'POST':
                pwd = request.form.get('access_password', '').strip()
                if pwd == share.access_password:
                    session[auth_key] = True
                else:
                    flash('访问密码错误，请重新输入！', 'danger')
            if not session.get(auth_key):
                needs_password = True
                return render_template(
                    'shared_ledger.html',
                    share=share,
                    share_title=share.title or '专属共享账本',
                    needs_password=True,
                    records=[],
                    owner_name='',
                    total_received=0,
                    total_given=0
                )

        if share.banquet_id:
            b = db.session.get(Banquet, share.banquet_id)
            records = GiftRecord.query.filter_by(banquet_id=b.id).filter(GiftRecord.deleted_at.is_(None)).order_by(GiftRecord.created_at.asc()).all()
            owner = getattr(share, 'owner', getattr(share, 'user', None))
            share_title = b.title
        else:
            owner = db.session.get(User, share.user_id)
            records = GiftRecord.query.filter_by(user_id=owner.id).filter(GiftRecord.deleted_at.is_(None)).order_by(GiftRecord.created_at.desc()).all()
            share_title = share.title or f"{owner.username} 的礼金账本"

        total_received = sum(r.amount for r in records if getattr(r, 'record_type', 'receive') != 'send')
        total_given = sum(r.amount for r in records if getattr(r, 'record_type', 'receive') == 'send')

        return render_template(
            'shared_ledger.html',
            owner=owner,
            owner_name=owner.username if owner else '系统用户',
            records=records,
            share=share,
            share_title=share_title,
            total_received=total_received,
            total_given=total_given,
            needs_password=False
        )

    # ===================== 权限申请工单 =====================

    # 可申请的菜单列表
    TICKET_MENU_OPTIONS = [
        ('ledger', '礼金账本'),
        ('dashboard', '数据分析'),
        ('family', '家庭记账'),
        ('banquets', '专属宴席'),
        ('reconciliation', '人情对账'),
        ('reminders', '纪念日备忘'),
        ('recycle_bin', '回收站'),
    ]

    @app.route('/permission_tickets')
    @login_required
    def permission_tickets_view():
        """工单管理页面：普通用户看自己的工单，管理员看全部工单
        V8 增强：排序（sort/order）、筛选（含 revoked）、分页（page/per_page，默认10条/页）
        V10 增强：关键词搜索（q）、申请模块筛选（module）、扩展排序列（applicant/reason）、多选批量删除"""
        # ---- V8/V10 排序参数 ----
        sort_map = {
            'created_at': PermissionTicket.created_at,
            'updated_at': PermissionTicket.updated_at,
            'status': PermissionTicket.status,
            'id': PermissionTicket.id,
            'applicant': User.username,       # V10: 按申请人排序
            'reason': PermissionTicket.reason, # V10: 按申请理由排序
        }
        sort_key = request.args.get('sort', 'created_at').strip()
        if sort_key not in sort_map:
            sort_key = 'created_at'
        order = request.args.get('order', 'desc').strip().lower()
        if order not in ('asc', 'desc'):
            order = 'desc'
        sort_col = sort_map[sort_key]

        # ---- V8/V10 分页参数 ----
        try:
            page = max(1, int(request.args.get('page', 1)))
        except (TypeError, ValueError):
            page = 1
        try:
            per_page = int(request.args.get('per_page', 10))
            if per_page not in (5, 10, 20, 50, 100):
                per_page = 10
        except (TypeError, ValueError):
            per_page = 10

        # ---- V10: 关键词搜索 & 模块筛选（仅管理员） ----
        keyword = request.args.get('q', '').strip()
        module_filter = request.args.get('module', '').strip()

        query = PermissionTicket.query
        if current_user.is_admin:
            status_filter = request.args.get('status', '').strip()
            if status_filter in ('pending', 'approved', 'rejected', 'revoked'):
                query = query.filter(PermissionTicket.status == status_filter)
            # V10: 关键词搜索（申请人用户名 + 申请理由模糊匹配）
            if keyword:
                query = query.join(User, PermissionTicket.user_id == User.id, isouter=True)
                query = query.filter(
                    db.or_(
                        User.username.ilike(f'%{keyword}%'),
                        PermissionTicket.reason.ilike(f'%{keyword}%')
                    )
                )
            # V10: 申请模块筛选
            if module_filter:
                query = query.filter(PermissionTicket.requested_menus.ilike(f'%{module_filter}%'))
        else:
            query = query.filter(PermissionTicket.user_id == current_user.id)

        # 排序 + 分页
        if order == 'asc':
            query = query.order_by(sort_col.asc())
        else:
            query = query.order_by(sort_col.desc())
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)
        tickets = pagination.items

        # 收集当前用户已申请(pending)和已拥有的菜单，用于前端禁用重复勾选
        already_requested = set()
        if not current_user.is_admin:
            pending_tickets = PermissionTicket.query.filter_by(user_id=current_user.id, status='pending').all()
            for t in pending_tickets:
                if t.requested_menus:
                    already_requested.update(t.requested_menus.split(','))
            # 已拥有的菜单权限也要标记
            if current_user.allowed_menus:
                already_requested.update(current_user.allowed_menus.split(','))
        return render_template(
            'permission_tickets.html',
            tickets=tickets,
            menu_options=TICKET_MENU_OPTIONS,
            already_requested=already_requested,
            pagination=pagination,
            sort_key=sort_key,
            order=order,
            per_page=per_page,
            status_filter=request.args.get('status', '') if current_user.is_admin else '',
            keyword=keyword if current_user.is_admin else '',
            module_filter=module_filter if current_user.is_admin else ''
        )

    @app.route('/permission_tickets/create', methods=['POST'])
    @login_required
    def permission_ticket_create():
        """用户提交权限申请工单"""
        requested_menus = request.form.getlist('requested_menus')
        reason = request.form.get('reason', '').strip()

        if not requested_menus:
            flash('请至少选择一个需要申请的菜单模块。', 'warning')
            return redirect(url_for('permission_tickets_view'))

        # 过滤合法菜单项
        valid_keys = [k for k, _ in TICKET_MENU_OPTIONS]
        requested_menus = [m for m in requested_menus if m in valid_keys]
        if not requested_menus:
            flash('选择的菜单模块无效。', 'warning')
            return redirect(url_for('permission_tickets_view'))

        # 检查是否已有 pending 工单
        existing = PermissionTicket.query.filter_by(
            user_id=current_user.id, status='pending'
        ).first()
        if existing:
            flash('您已有一个待审批的工单，请等待管理员处理后再提交新工单。', 'info')
            return redirect(url_for('permission_tickets_view'))

        # 检查提交的菜单是否已有权限或已在 pending 工单中
        already_set = set()
        if current_user.allowed_menus:
            already_set.update(current_user.allowed_menus.split(','))
        for t in PermissionTicket.query.filter_by(user_id=current_user.id, status='pending').all():
            if t.requested_menus:
                already_set.update(t.requested_menus.split(','))
        dup_menus = [m for m in requested_menus if m in already_set]
        if dup_menus:
            flash(f'以下菜单已申请或已有权限，无需重复申请：{", ".join(dup_menus)}', 'warning')
            return redirect(url_for('permission_tickets_view'))

        ticket = PermissionTicket(
            user_id=current_user.id,
            requested_menus=','.join(requested_menus),
            reason=reason if reason else None,
            status='pending'
        )
        db.session.add(ticket)
        db.session.commit()

        safe_log('提交权限申请', f'工单#{ticket.id}，申请菜单: {", ".join(requested_menus)}', user=current_user)

        # Webhook 推送
        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(),
                'system',
                f'用户 {current_user.username} 提交了权限申请工单#{ticket.id}，申请菜单: {", ".join(requested_menus)}',
                page_key='permission_tickets',
                user_name=current_user.username,
                operator_id=current_user.id
            )
        except Exception:
            pass

        flash('权限申请已提交，请耐心等待管理员审批。', 'success')
        return redirect(url_for('permission_tickets_view'))

    @app.route('/permission_tickets/<int:ticket_id>/approve', methods=['POST'])
    @login_required
    def permission_ticket_approve(ticket_id):
        """管理员批准工单"""
        if not current_user.is_admin:
            flash('无权限执行此操作。', 'danger')
            return redirect(url_for('index'))

        ticket = db.session.get(PermissionTicket, ticket_id)
        if not ticket:
            flash('工单不存在。', 'danger')
            return redirect(url_for('permission_tickets_view'))

        if ticket.status != 'pending':
            flash('该工单已处理，无法重复操作。', 'warning')
            return redirect(url_for('permission_tickets_view'))

        # 管理员可调整实际授予的菜单
        granted_menus = request.form.getlist('granted_menus')
        valid_keys = [k for k, _ in TICKET_MENU_OPTIONS]
        granted_menus = [m for m in granted_menus if m in valid_keys]

        review_comment = request.form.get('review_comment', '').strip()

        ticket.status = 'approved'
        ticket.reviewed_by = current_user.id
        ticket.reviewed_at = datetime.now()
        ticket.review_comment = review_comment if review_comment else None
        ticket.granted_menus = ','.join(granted_menus) if granted_menus else ticket.requested_menus

        # 将批准的菜单合并到用户的 allowed_menus
        user = db.session.get(User, ticket.user_id)
        if user:
            current_menus = set(user.get_allowed_menus())
            granted_set = set(granted_menus if granted_menus else ticket.requested_menus.split(','))
            merged = current_menus | granted_set
            # 移除空字符串
            merged.discard('')
            user.allowed_menus = ','.join(sorted(merged))

        db.session.commit()

        safe_log('批准权限工单', f'工单#{ticket.id}，用户: {user.username if user else "?"}，授予菜单: {ticket.granted_menus}', user=current_user)

        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(),
                'status_change',
                f'管理员 {current_user.username} 批准了工单#{ticket.id}，用户 {user.username if user else "?"} 获得菜单权限: {ticket.granted_menus}',
                page_key='permission_tickets',
                user_name=current_user.username,
                operator_id=current_user.id
            )
        except Exception:
            pass

        flash(f'工单#{ticket_id} 已批准，用户权限已更新。', 'success')
        return redirect(url_for('permission_tickets_view'))

    @app.route('/permission_tickets/<int:ticket_id>/reject', methods=['POST'])
    @login_required
    def permission_ticket_reject(ticket_id):
        """管理员驳回工单"""
        if not current_user.is_admin:
            flash('无权限执行此操作。', 'danger')
            return redirect(url_for('index'))

        ticket = db.session.get(PermissionTicket, ticket_id)
        if not ticket:
            flash('工单不存在。', 'danger')
            return redirect(url_for('permission_tickets_view'))

        if ticket.status != 'pending':
            flash('该工单已处理，无法重复操作。', 'warning')
            return redirect(url_for('permission_tickets_view'))

        review_comment = request.form.get('review_comment', '').strip()

        ticket.status = 'rejected'
        ticket.reviewed_by = current_user.id
        ticket.reviewed_at = datetime.now()
        ticket.review_comment = review_comment if review_comment else None

        db.session.commit()

        user = db.session.get(User, ticket.user_id)
        safe_log('驳回权限工单', f'工单#{ticket.id}，用户: {user.username if user else "?"}，理由: {review_comment or "无"}', user=current_user)

        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(),
                'status_change',
                f'管理员 {current_user.username} 驳回了工单#{ticket.id}，用户 {user.username if user else "?"} 的权限申请',
                page_key='permission_tickets',
                user_name=current_user.username,
                operator_id=current_user.id
            )
        except Exception:
            pass

        flash(f'工单#{ticket_id} 已驳回。', 'info')
        return redirect(url_for('permission_tickets_view'))

    @app.route('/permission_tickets/<int:ticket_id>/revoke', methods=['POST'])
    @login_required
    def permission_ticket_revoke(ticket_id):
        """V3: 管理员撤销已批准的权限工单"""
        if not current_user.is_admin:
            flash('无权限执行此操作。', 'danger')
            return redirect(url_for('index'))

        ticket = db.session.get(PermissionTicket, ticket_id)
        if not ticket:
            flash('工单不存在。', 'danger')
            return redirect(url_for('permission_tickets_view'))

        if ticket.status != 'approved':
            flash('只能撤销已批准的工单。', 'warning')
            return redirect(url_for('permission_tickets_view'))

        review_comment = request.form.get('review_comment', '').strip()

        # 从用户的 allowed_menus 中移除该工单授予的菜单
        user = db.session.get(User, ticket.user_id)
        if user and ticket.granted_menus:
            granted_set = set(ticket.granted_menus.split(','))
            current_menus = set(user.get_allowed_menus())
            revoked = current_menus - granted_set
            revoked.discard('')
            user.allowed_menus = ','.join(sorted(revoked)) if revoked else ''

        ticket.status = 'revoked'
        ticket.reviewed_by = current_user.id
        ticket.reviewed_at = datetime.now()
        ticket.review_comment = review_comment if review_comment else '管理员撤销授权'

        db.session.commit()
        safe_log('撤销权限工单', f'工单#{ticket.id}，用户: {user.username if user else "?"}，撤销菜单: {ticket.granted_menus}', user=current_user)

        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(),
                'status_change',
                f'管理员 {current_user.username} 撤销了工单#{ticket.id}，用户 {user.username if user else "?"} 的菜单权限已被收回',
                page_key='permission_tickets',
                user_name=current_user.username,
                operator_id=current_user.id
            )
        except Exception:
            pass

        flash(f'工单#{ticket_id} 已撤销，用户权限已更新。', 'warning')
        return redirect(url_for('permission_tickets_view'))

    @app.route('/permission_tickets/<int:ticket_id>/delete', methods=['POST'])
    @login_required
    def permission_ticket_delete(ticket_id):
        """V8 新增：管理员删除工单（普通用户无权限）"""
        if not current_user.is_admin:
            flash('无权限执行此操作。', 'danger')
            return redirect(url_for('index'))

        ticket = db.session.get(PermissionTicket, ticket_id)
        if not ticket:
            flash('工单不存在。', 'danger')
            return redirect(url_for('permission_tickets_view'))

        applicant = ticket.user.username if ticket.user else '已删除用户'
        ticket_id_val = ticket.id
        db.session.delete(ticket)
        db.session.commit()

        safe_log('删除权限工单', f'工单#{ticket_id_val}，申请人: {applicant}', user=current_user)

        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(),
                'status_change',
                f'管理员 {current_user.username} 删除了工单#{ticket_id_val}（申请人: {applicant}）',
                page_key='permission_tickets',
                user_name=current_user.username,
                operator_id=current_user.id
            )
        except Exception:
            pass

        flash(f'工单#{ticket_id_val} 已删除。', 'info')
        return redirect(url_for('permission_tickets_view'))

    @app.route('/permission_tickets/batch_delete', methods=['POST'])
    @login_required
    def permission_ticket_batch_delete():
        """V10 新增：管理员批量删除工单"""
        if not current_user.is_admin:
            flash('无权限执行此操作。', 'danger')
            return redirect(url_for('index'))

        ticket_ids = request.form.getlist('ticket_ids')
        if not ticket_ids:
            flash('请至少选择一个工单。', 'warning')
            return redirect(url_for('permission_tickets_view'))

        # 转换为整数并过滤无效值
        valid_ids = []
        for tid in ticket_ids:
            try:
                valid_ids.append(int(tid))
            except (TypeError, ValueError):
                pass
        if not valid_ids:
            flash('未选择有效工单。', 'warning')
            return redirect(url_for('permission_tickets_view'))

        tickets = PermissionTicket.query.filter(PermissionTicket.id.in_(valid_ids)).all()
        count = len(tickets)
        for t in tickets:
            db.session.delete(t)
        db.session.commit()

        safe_log('批量删除权限工单', f'删除 {count} 条工单（ID: {", ".join(str(t.id) for t in tickets)}）', user=current_user)

        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(),
                'status_change',
                f'管理员 {current_user.username} 批量删除了 {count} 条权限工单',
                page_key='permission_tickets',
                user_name=current_user.username,
                operator_id=current_user.id
            )
        except Exception:
            pass

        flash(f'已批量删除 {count} 条工单。', 'info')
        return redirect(url_for('permission_tickets_view'))

    # ==================== 功能一：数据分析看板 ====================

    @app.route('/dashboard')
    @login_required
    def dashboard_view():
        """数据分析看板页面"""
        if not current_user.is_admin and not current_user.can_access_menu('dashboard'):
            return render_template('dashboard.html')
        return render_template('dashboard.html')

    @app.route('/api/dashboard/stats')
    @login_required
    def api_dashboard_stats():
        """数据分析看板 API — 返回汇总/月度/年度/事由分布/TOP10 数据，支持日期范围筛选"""
        try:
            # 获取当前用户可访问的记录
            if current_user.is_admin or current_user.can_view_others_for('ledger'):
                query = GiftRecord.query.filter(GiftRecord.deleted_at.is_(None))
            else:
                query = GiftRecord.query.filter(GiftRecord.deleted_at.is_(None), GiftRecord.user_id == current_user.id)

            # V10.11.1 日期范围筛选参数
            start_month = request.args.get('start_month', '').strip()
            end_month = request.args.get('end_month', '').strip()
            start_year = request.args.get('start_year', '').strip()
            end_year = request.args.get('end_year', '').strip()

            # 根据月度范围筛选
            if start_month and end_month:
                try:
                    start_date = datetime.strptime(start_month + '-01', '%Y-%m-%d')
                    end_date = datetime.strptime(end_month + '-01', '%Y-%m-%d')
                    # end_month 取当月最后一天
                    if end_date.month == 12:
                        end_date = end_date.replace(day=31)
                    else:
                        end_date = end_date.replace(month=end_date.month + 1, day=1) - timedelta(days=1)
                    query = query.filter(GiftRecord.created_at >= start_date, GiftRecord.created_at <= end_date)
                except Exception:
                    pass
            elif start_year and end_year:
                try:
                    start_date = datetime(int(start_year), 1, 1)
                    end_date = datetime(int(end_year), 12, 31, 23, 59, 59)
                    query = query.filter(GiftRecord.created_at >= start_date, GiftRecord.created_at <= end_date)
                except Exception:
                    pass

            records = query.order_by(GiftRecord.created_at.desc()).all()

            # 汇总统计
            total_received = sum(r.amount for r in records if r.record_type not in ('send', 'give'))
            total_sent = sum(r.amount for r in records if r.record_type in ('send', 'give'))
            net_amount = total_received - total_sent
            total_count = len(records)

            # 月度趋势
            monthly_map = {}
            for r in records:
                month_key = r.created_at.strftime('%Y-%m') if r.created_at else '未知'
                if month_key not in monthly_map:
                    monthly_map[month_key] = {'month': month_key, 'received': 0, 'sent': 0}
                if r.record_type in ('send', 'give'):
                    monthly_map[month_key]['sent'] += r.amount
                else:
                    monthly_map[month_key]['received'] += r.amount
            monthly_trend = sorted(monthly_map.values(), key=lambda x: x['month'])

            # 年度趋势
            yearly_map = {}
            for r in records:
                year_key = str(r.created_at.year) if r.created_at else '未知'
                if year_key not in yearly_map:
                    yearly_map[year_key] = {'year': year_key, 'received': 0, 'sent': 0}
                if r.record_type in ('send', 'give'):
                    yearly_map[year_key]['sent'] += r.amount
                else:
                    yearly_map[year_key]['received'] += r.amount
            yearly_trend = sorted(yearly_map.values(), key=lambda x: x['year'])

            # 事由分布
            reason_map = {}
            for r in records:
                reason = r.event_reason or '其它'
                if reason not in reason_map:
                    reason_map[reason] = {'reason': reason, 'count': 0, 'amount': 0}
                reason_map[reason]['count'] += 1
                reason_map[reason]['amount'] += r.amount
            reason_distribution = sorted(reason_map.values(), key=lambda x: x['amount'], reverse=True)

            # TOP10 亲友往来
            contact_map = {}
            for r in records:
                name = (r.name or '').strip()
                if not name:
                    continue
                if name not in contact_map:
                    contact_map[name] = {'name': name, 'received': 0, 'sent': 0, 'count': 0}
                if r.record_type in ('send', 'give'):
                    contact_map[name]['sent'] += r.amount
                else:
                    contact_map[name]['received'] += r.amount
                contact_map[name]['count'] += 1
            for v in contact_map.values():
                v['total'] = v['received'] + v['sent']
            top10_contacts = sorted(contact_map.values(), key=lambda x: x['total'], reverse=True)[:10]

            return jsonify({
                'code': 200,
                'data': {
                    'summary': {
                        'total_received': round(total_received, 2),
                        'total_sent': round(total_sent, 2),
                        'net_amount': round(net_amount, 2),
                        'total_count': total_count,
                        'avg_per_record': round(total_received / total_count, 2) if total_count > 0 else 0
                    },
                    'monthly_trend': monthly_trend,
                    'yearly_trend': yearly_trend,
                    'reason_distribution': reason_distribution,
                    'top10_contacts': top10_contacts
                }
            })
        except Exception as e:
            return jsonify({'code': 500, 'message': f'获取看板数据失败: {str(e)}'}), 500

    # ==================== 功能二：家庭多成员协作记账 ====================

    @app.route('/family')
    @login_required
    def family_view():
        """家庭协作记账管理页面"""
        return render_template('family.html')

    @app.route('/api/family/my-groups')
    @login_required
    def api_family_my_groups():
        """获取当前用户所在的家庭组列表"""
        try:
            # 查找用户所在的所有家庭组
            memberships = FamilyMember.query.filter_by(user_id=current_user.id).all()
            group_ids = [m.group_id for m in memberships]
            groups = FamilyGroup.query.filter(FamilyGroup.id.in_(group_ids)).all() if group_ids else []
            result = []
            for g in groups:
                members = FamilyMember.query.filter_by(group_id=g.id).all()
                member_list = []
                for m in members:
                    u = User.query.get(m.user_id)
                    member_list.append({
                        'id': m.id,
                        'user_id': m.user_id,
                        'username': u.username if u else '未知',
                        'role': m.role,
                        'nickname': m.nickname or ''
                    })
                result.append({
                    'id': g.id,
                    'name': g.name,
                    'description': g.description or '',
                    'owner_id': g.owner_id,
                    'members': member_list
                })
            return jsonify({'code': 200, 'groups': result})
        except Exception as e:
            return jsonify({'code': 500, 'message': str(e)}), 500

    @app.route('/api/family/create', methods=['POST'])
    @login_required
    def api_family_create():
        """创建家庭组"""
        try:
            data = request.get_json(force=True)
            name = (data.get('name') or '').strip()
            if not name:
                return jsonify({'code': 400, 'message': '请输入家庭名称'}), 400
            group = FamilyGroup(name=name, owner_id=current_user.id, description=data.get('description', ''))
            db.session.add(group)
            db.session.flush()
            # 创建者自动成为家长成员
            member = FamilyMember(group_id=group.id, user_id=current_user.id, role='head', nickname='家长')
            db.session.add(member)
            db.session.commit()
            safe_log('创建家庭组', f'家庭名称: {name}', user=current_user)
            try:
                trigger_webhook_event(
                    WebhookConfig.query.filter_by(is_enabled=True).all(),
                    'create', f'家庭组 [{name}] 已创建',
                    page_key='family', user_name=current_user.username, operator_id=current_user.id
                )
            except Exception:
                pass
            return jsonify({'code': 200, 'message': '家庭组创建成功', 'group_id': group.id})
        except Exception as e:
            db.session.rollback()
            return jsonify({'code': 500, 'message': str(e)}), 500

    @app.route('/api/family/<int:group_id>/invitable-users')
    @login_required
    def api_family_invitable_users(group_id):
        """获取可邀请的用户列表（排除已在组内的）"""
        try:
            group = FamilyGroup.query.get(group_id)
            if not group:
                return jsonify({'code': 404, 'message': '家庭组不存在'}), 404
            existing_ids = [m.user_id for m in FamilyMember.query.filter_by(group_id=group_id).all()]
            all_users = User.query.filter(User.is_active == True, ~User.id.in_(existing_ids) if existing_ids else True).all()
            return jsonify({'code': 200, 'users': [{'id': u.id, 'username': u.username} for u in all_users]})
        except Exception as e:
            return jsonify({'code': 500, 'message': str(e)}), 500

    @app.route('/api/family/<int:group_id>/invite', methods=['POST'])
    @login_required
    def api_family_invite(group_id):
        """邀请成员加入家庭组"""
        try:
            group = FamilyGroup.query.get(group_id)
            if not group:
                return jsonify({'code': 404, 'message': '家庭组不存在'}), 404
            # 仅家长或管理员可邀请
            if not current_user.is_admin and group.owner_id != current_user.id:
                my_membership = FamilyMember.query.filter_by(group_id=group_id, user_id=current_user.id, role='head').first()
                if not my_membership:
                    return jsonify({'code': 403, 'message': '仅家长可邀请成员'}), 403
            data = request.get_json(force=True)
            user_id = data.get('user_id')
            if not user_id:
                return jsonify({'code': 400, 'message': '请选择用户'}), 400
            # 检查是否已在组内
            existing = FamilyMember.query.filter_by(group_id=group_id, user_id=user_id).first()
            if existing:
                return jsonify({'code': 409, 'message': '该用户已在家庭组中'}), 409
            role = data.get('role', 'member')
            nickname = data.get('nickname', '')
            member = FamilyMember(group_id=group_id, user_id=user_id, role=role, nickname=nickname)
            db.session.add(member)
            db.session.commit()
            safe_log('邀请家庭成员', f'家庭组: {group.name}, 用户ID: {user_id}, 角色: {role}', user=current_user)
            try:
                trigger_webhook_event(
                    WebhookConfig.query.filter_by(is_enabled=True).all(),
                    'status_change', f'家庭组 [{group.name}] 新增成员',
                    page_key='family', user_name=current_user.username, operator_id=current_user.id
                )
            except Exception:
                pass
            return jsonify({'code': 200, 'message': '邀请成功'})
        except Exception as e:
            db.session.rollback()
            return jsonify({'code': 500, 'message': str(e)}), 500

    @app.route('/api/family/<int:group_id>/dissolve', methods=['DELETE'])
    @login_required
    def api_family_dissolve(group_id):
        """解散家庭组"""
        try:
            group = FamilyGroup.query.get(group_id)
            if not group:
                return jsonify({'code': 404, 'message': '家庭组不存在'}), 404
            if not current_user.is_admin and group.owner_id != current_user.id:
                return jsonify({'code': 403, 'message': '仅家长可解散家庭组'}), 403
            safe_log('解散家庭组', f'家庭名称: {group.name}', user=current_user)
            db.session.delete(group)  # 级联删除成员
            db.session.commit()
            try:
                trigger_webhook_event(
                    WebhookConfig.query.filter_by(is_enabled=True).all(),
                    'delete', f'家庭组 [{group.name}] 已解散',
                    page_key='family', user_name=current_user.username, operator_id=current_user.id
                )
            except Exception:
                pass
            return jsonify({'code': 200, 'message': '家庭组已解散'})
        except Exception as e:
            db.session.rollback()
            return jsonify({'code': 500, 'message': str(e)}), 500

    @app.route('/api/family/my-perspective-users')
    @login_required
    def api_family_my_perspective_users():
        """获取当前用户可查看的家庭成员视角列表（用于账本视角切换）"""
        try:
            memberships = FamilyMember.query.filter_by(user_id=current_user.id).all()
            perspective_users = [{'id': current_user.id, 'username': current_user.username, 'label': '我的账本'}]
            for m in memberships:
                group = FamilyGroup.query.get(m.group_id)
                if not group:
                    continue
                # 家长可看全部成员，member 可看自己，viewer 可看汇总
                if m.role == 'head' or current_user.is_admin:
                    group_members = FamilyMember.query.filter_by(group_id=m.group_id).all()
                    for gm in group_members:
                        if gm.user_id != current_user.id:
                            u = User.query.get(gm.user_id)
                            if u:
                                perspective_users.append({
                                    'id': u.id,
                                    'username': u.username,
                                    'label': f'{group.name} - {gm.nickname or u.username}'
                                })
            return jsonify({'code': 200, 'users': perspective_users})
        except Exception as e:
            return jsonify({'code': 500, 'message': str(e)}), 500

    # ==================== 功能三：批量导入与智能识别 ====================

    @app.route('/api/import/preview', methods=['POST'])
    @login_required
    def api_import_preview():
        """Excel/CSV 文件上传预览：解析表头与前5行数据"""
        try:
            file = request.files.get('file')
            if not file:
                return jsonify({'code': 400, 'message': '请选择文件'}), 400
            filename = file.filename or ''
            ext = os.path.splitext(filename)[1].lower()

            if ext == '.csv':
                import csv as _csv
                import io as _io
                content = file.read().decode('utf-8-sig')
                reader = _csv.reader(_io.StringIO(content))
                rows = list(reader)
            elif ext in ('.xlsx', '.xls'):
                try:
                    from openpyxl import load_workbook
                except ImportError:
                    return jsonify({'code': 503, 'message': 'openpyxl 未安装，无法解析 Excel 文件'}), 503
                wb = load_workbook(file, read_only=True, data_only=True)
                ws = wb.active
                rows = []
                for row in ws.iter_rows(values_only=True):
                    rows.append([str(cell) if cell is not None else '' for cell in row])
                wb.close()
            else:
                return jsonify({'code': 400, 'message': '仅支持 .csv / .xlsx 格式'}), 400

            if len(rows) < 2:
                return jsonify({'code': 400, 'message': '文件为空或只有表头'}), 400

            headers = [str(h).strip() for h in rows[0]]
            preview_rows = rows[1:6]  # 前5行预览

            # 智能列名匹配
            field_map = {
                'name': ['姓名', '客人姓名', '名字', 'name', '送礼人', '联系人'],
                'amount': ['金额', '礼金', '礼金金额', '数额', '金额(元)', '金额（元）', 'amount'],
                'event_reason': ['事由', '办席原因', '原因', '事由/原因', 'event_reason', 'reason'],
                'record_type': ['类型', '往来类型', '收送', '收礼/送礼', 'record_type', 'type'],
                'age': ['年龄', 'age'],
                'phone': ['电话', '联系电话', '手机', 'phone'],
                'address': ['地址', '联系地址', 'address'],
                'notes': ['备注', '说明', '备注说明', 'notes', 'remark']
            }
            auto_mapping = {}
            for field, keywords in field_map.items():
                for idx, header in enumerate(headers):
                    if header.lower() in [kw.lower() for kw in keywords]:
                        auto_mapping[field] = idx
                        break

            return jsonify({
                'code': 200,
                'headers': headers,
                'total_rows': len(rows) - 1,
                'preview': preview_rows,
                'auto_mapping': auto_mapping
            })
        except Exception as e:
            return jsonify({'code': 500, 'message': f'文件解析失败: {str(e)}'}), 500

    @app.route('/api/import/confirm', methods=['POST'])
    @login_required
    def api_import_confirm():
        """确认字段映射后批量导入"""
        try:
            data = request.get_json(force=True)
            file_path = data.get('file_path')
            mapping = data.get('mapping', {})  # {field_name: column_index}

            # 由于文件已在上传时读取，此处通过重新上传或临时文件处理
            # 这里接收 base64 文件数据
            file_b64 = data.get('file_data')
            if not file_b64:
                return jsonify({'code': 400, 'message': '缺少文件数据'}), 400

            import base64 as _b64
            file_bytes = _b64.b64decode(file_b64)

            ext = data.get('file_ext', '.csv')
            if ext == '.csv':
                import csv as _csv
                import io as _io
                content = file_bytes.decode('utf-8-sig')
                reader = _csv.reader(_io.StringIO(content))
                rows = list(reader)
            elif ext in ('.xlsx', '.xls'):
                from openpyxl import load_workbook
                import io as _io2
                wb = load_workbook(_io2.BytesIO(file_bytes), read_only=True, data_only=True)
                ws = wb.active
                rows = []
                for row in ws.iter_rows(values_only=True):
                    rows.append([str(cell) if cell is not None else '' for cell in row])
                wb.close()
            else:
                return jsonify({'code': 400, 'message': '不支持的文件格式'}), 400

            if len(rows) < 2:
                return jsonify({'code': 400, 'message': '文件为空'}), 400

            # 解析映射
            name_idx = mapping.get('name')
            amount_idx = mapping.get('amount')
            reason_idx = mapping.get('event_reason')
            type_idx = mapping.get('record_type')
            age_idx = mapping.get('age')
            phone_idx = mapping.get('phone')
            address_idx = mapping.get('address')
            notes_idx = mapping.get('notes')

            if name_idx is None or amount_idx is None:
                return jsonify({'code': 400, 'message': '必须映射姓名和金额列'}), 400

            imported = 0
            skipped = 0
            for row in rows[1:]:
                try:
                    if name_idx >= len(row) or amount_idx >= len(row):
                        skipped += 1
                        continue
                    name = str(row[name_idx]).strip()
                    if not name:
                        skipped += 1
                        continue
                    amount_str = str(row[amount_idx]).strip().replace('¥', '').replace('元', '').replace(',', '')
                    try:
                        amount = float(amount_str)
                    except ValueError:
                        amount = cn2num(amount_str)
                    if amount <= 0:
                        skipped += 1
                        continue

                    event_reason = '其它'
                    if reason_idx is not None and reason_idx < len(row):
                        event_reason = str(row[reason_idx]).strip() or '其它'

                    record_type = 'receive'
                    if type_idx is not None and type_idx < len(row):
                        type_val = str(row[type_idx]).strip().lower()
                        if type_val in ('send', '送礼', '随礼', '出', 'give'):
                            record_type = 'send'

                    age_val = None
                    if age_idx is not None and age_idx < len(row):
                        try:
                            age_val = int(str(row[age_idx]).strip()) if str(row[age_idx]).strip() else None
                        except ValueError:
                            age_val = None

                    phone_val = str(row[phone_idx]).strip() if phone_idx is not None and phone_idx < len(row) else None
                    address_val = str(row[address_idx]).strip() if address_idx is not None and address_idx < len(row) else None
                    notes_val = str(row[notes_idx]).strip() if notes_idx is not None and notes_idx < len(row) else None

                    record = GiftRecord(
                        name=name, amount=amount, event_reason=event_reason,
                        record_type=record_type, age=age_val, phone=phone_val,
                        address=address_val, notes=notes_val, user_id=current_user.id
                    )
                    db.session.add(record)
                    imported += 1
                except Exception:
                    skipped += 1
                    continue

            db.session.commit()
            safe_log('批量导入', f'成功导入 {imported} 条，跳过 {skipped} 条', user=current_user)
            try:
                trigger_webhook_event(
                    WebhookConfig.query.filter_by(is_enabled=True).all(),
                    'add', f'{current_user.username} 批量导入了 {imported} 条礼金记录',
                    page_key='ledger', user_name=current_user.username, operator_id=current_user.id
                )
            except Exception:
                pass
            return jsonify({'code': 200, 'message': f'成功导入 {imported} 条记录，跳过 {skipped} 条无效数据', 'imported': imported, 'skipped': skipped})
        except Exception as e:
            db.session.rollback()
            return jsonify({'code': 500, 'message': f'导入失败: {str(e)}'}), 500

    @app.route('/api/ocr/recognize', methods=['POST'])
    @login_required
    def api_ocr_recognize():
        """OCR 图片智能识别"""
        try:
            data = request.get_json(force=True)
            image_b64 = data.get('image')
            if not image_b64:
                return jsonify({'code': 400, 'message': '请上传图片'}), 400
            # 去除可能的 data:image/xxx;base64, 前缀
            if ',' in image_b64 and image_b64.startswith('data:'):
                image_b64 = image_b64.split(',', 1)[1]

            from ai_service import recognize_gift_image
            result = recognize_gift_image(image_b64, user=current_user)
            return jsonify(result)
        except Exception as e:
            return jsonify({'code': 500, 'message': f'OCR 识别失败: {str(e)}'}), 500

    @app.route('/api/ocr/batch-add', methods=['POST'])
    @login_required
    def api_ocr_batch_add():
        """OCR 识别结果批量入库"""
        try:
            data = request.get_json(force=True)
            records = data.get('records', [])
            if not records:
                return jsonify({'code': 400, 'message': '无可入库记录'}), 400
            added = 0
            for r in records:
                name = str(r.get('name', '')).strip()
                amount = float(r.get('amount', 0))
                if not name or amount <= 0:
                    continue
                record = GiftRecord(
                    name=name, amount=amount,
                    event_reason=str(r.get('event_reason', '其它')).strip() or '其它',
                    record_type='send' if str(r.get('record_type', 'receive')).lower() in ('send', 'give') else 'receive',
                    notes=str(r.get('notes', '')).strip() or None,
                    user_id=current_user.id
                )
                db.session.add(record)
                added += 1
            db.session.commit()
            safe_log('OCR批量录入', f'通过图片识别录入 {added} 条记录', user=current_user)
            try:
                trigger_webhook_event(
                    WebhookConfig.query.filter_by(is_enabled=True).all(),
                    'add', f'{current_user.username} 通过 OCR 识别录入了 {added} 条记录',
                    page_key='ledger', user_name=current_user.username, operator_id=current_user.id
                )
            except Exception:
                pass
            return jsonify({'code': 200, 'message': f'成功录入 {added} 条记录', 'added': added})
        except Exception as e:
            db.session.rollback()
            return jsonify({'code': 500, 'message': f'批量入库失败: {str(e)}'}), 500

    # ==================== 功能四：人情簿打印与海报导出 ====================

    @app.route('/export/print-giftbook')
    @login_required
    def export_print_giftbook():
        """人情簿 A4 打印预览页面"""
        try:
            # 获取记录
            if current_user.is_admin or current_user.can_view_others_for('ledger'):
                query = GiftRecord.query.filter(GiftRecord.deleted_at.is_(None))
            else:
                query = GiftRecord.query.filter(GiftRecord.deleted_at.is_(None), GiftRecord.user_id == current_user.id)
            records = query.order_by(GiftRecord.created_at.desc()).all()

            total_received = sum(r.amount for r in records if r.record_type not in ('send', 'give'))
            total_sent = sum(r.amount for r in records if r.record_type in ('send', 'give'))
            net_amount = total_received - total_sent

            # 金额大写转换
            for r in records:
                r.amount_cn = _amount_to_cn(r.amount)

            # 分页计算
            per_page = 20
            total_pages = (len(records) + per_page - 1) // per_page if records else 1

            return render_template('print_giftbook.html',
                                   title='人情礼金簿',
                                   subtitle=f'{datetime.now().strftime("%Y年度")} · 全部往来',
                                   records=records,
                                   total_count=len(records),
                                   total_received=total_received,
                                   total_sent=total_sent,
                                   net_amount=net_amount,
                                   total_pages=total_pages,
                                   generated_at=datetime.now().strftime('%Y-%m-%d %H:%M'))
        except Exception as e:
            flash(f'生成打印页面失败: {str(e)}', 'danger')
            return redirect(url_for('index'))

    @app.route('/export/pdf-statement')
    @login_required
    def export_pdf_statement():
        """生成 PDF 对账单"""
        try:
            # 获取记录
            if current_user.is_admin or current_user.can_view_others_for('ledger'):
                query = GiftRecord.query.filter(GiftRecord.deleted_at.is_(None))
            else:
                query = GiftRecord.query.filter(GiftRecord.deleted_at.is_(None), GiftRecord.user_id == current_user.id)
            records = query.order_by(GiftRecord.created_at.desc()).all()

            total_received = sum(r.amount for r in records if r.record_type not in ('send', 'give'))
            total_sent = sum(r.amount for r in records if r.record_type in ('send', 'give'))
            net_amount = total_received - total_sent

            # 构造 PDF 数据
            record_list = []
            for r in records:
                record_list.append({
                    'name': r.name,
                    'record_type': r.record_type,
                    'amount': r.amount,
                    'event_reason': r.event_reason,
                    'notes': r.notes or '',
                    'created_at': r.created_at.strftime('%Y-%m-%d') if r.created_at else ''
                })
            summary = {
                'total_received': total_received,
                'total_sent': total_sent,
                'net_amount': net_amount,
                'total_count': len(records)
            }

            from pdf_generator import generate_pdf_statement
            pdf_buffer = generate_pdf_statement(record_list, summary, title='礼金对账单')
            safe_log('导出PDF对账单', f'共 {len(records)} 条记录', user=current_user)

            response = make_response(pdf_buffer.getvalue())
            response.headers['Content-Type'] = 'application/pdf'
            response.headers['Content-Disposition'] = f'attachment; filename="礼金对账单_{datetime.now().strftime("%Y%m%d")}.pdf"'
            return response
        except Exception as e:
            flash(f'生成 PDF 失败: {str(e)}', 'danger')
            return redirect(url_for('index'))

    @app.route('/poster')
    @login_required
    def poster_view():
        """长图海报页面"""
        return render_template('poster_template.html')

    @app.route('/api/poster/data')
    @login_required
    def api_poster_data():
        """海报数据 API"""
        try:
            if current_user.is_admin or current_user.can_view_others_for('ledger'):
                query = GiftRecord.query.filter(GiftRecord.deleted_at.is_(None))
            else:
                query = GiftRecord.query.filter(GiftRecord.deleted_at.is_(None), GiftRecord.user_id == current_user.id)
            records = query.order_by(GiftRecord.created_at.desc()).all()

            total_received = sum(r.amount for r in records if r.record_type not in ('send', 'give'))
            total_sent = sum(r.amount for r in records if r.record_type in ('send', 'give'))

            # TOP5
            contact_map = {}
            for r in records:
                name = (r.name or '').strip()
                if not name:
                    continue
                if name not in contact_map:
                    contact_map[name] = {'name': name, 'received': 0, 'sent': 0}
                if r.record_type in ('send', 'give'):
                    contact_map[name]['sent'] += r.amount
                else:
                    contact_map[name]['received'] += r.amount
            for v in contact_map.values():
                v['total'] = v['received'] + v['sent']
            top5 = sorted(contact_map.values(), key=lambda x: x['total'], reverse=True)[:5]

            # 事由分布
            reason_map = {}
            total_reason_amount = 0
            for r in records:
                reason = r.event_reason or '其它'
                if reason not in reason_map:
                    reason_map[reason] = {'reason': reason, 'amount': 0}
                reason_map[reason]['amount'] += r.amount
                total_reason_amount += r.amount
            reasons = []
            for v in sorted(reason_map.values(), key=lambda x: x['amount'], reverse=True):
                v['percentage'] = round(v['amount'] / total_reason_amount * 100, 1) if total_reason_amount > 0 else 0
                reasons.append(v)

            # 海报二维码 URL（管理员可在系统设置中自定义注册链接）
            qr_url = SystemSetting.get_val('poster_qr_url', '')
            if not qr_url:
                # 默认使用当前站点注册页面
                qr_url = request.url_root.rstrip('/') + '/register'

            return jsonify({
                'code': 200,
                'data': {
                    'year': datetime.now().year,
                    'summary': {
                        'total_received': round(total_received, 2),
                        'total_sent': round(total_sent, 2),
                        'net_amount': round(total_received - total_sent, 2),
                        'total_count': len(records)
                    },
                    'top5': top5,
                    'reasons': reasons,
                    'qr_url': qr_url
                }
            })
        except Exception as e:
            return jsonify({'code': 500, 'message': str(e)}), 500

    # ==================== 海报二维码 URL 管理（管理员） ====================

    @app.route('/api/admin/poster-qr-url')
    @login_required
    def api_admin_poster_qr_url_get():
        """获取海报二维码 URL 设置"""
        if not current_user.is_admin:
            return jsonify({'code': 403, 'message': '仅管理员可操作'}), 403
        qr_url = SystemSetting.get_val('poster_qr_url', '')
        return jsonify({'code': 200, 'qr_url': qr_url or ''})

    @app.route('/api/admin/poster-qr-url', methods=['POST'])
    @login_required
    def api_admin_poster_qr_url_save():
        """保存海报二维码 URL 设置"""
        if not current_user.is_admin:
            return jsonify({'code': 403, 'message': '仅管理员可操作'}), 403
        try:
            data = request.get_json(force=True)
            qr_url = (data.get('qr_url') or '').strip()
            SystemSetting.set_val('poster_qr_url', qr_url)
            db.session.commit()
            safe_log('海报二维码设置', f'URL: {qr_url or "(空)"}', user=current_user)
            return jsonify({'code': 200, 'message': '保存成功'})
        except Exception as e:
            db.session.rollback()
            return jsonify({'code': 500, 'message': str(e)}), 500

    # 启动企业微信智能机器人长连接后台监听守护线程与亲友纪念日自动提醒后台调度器
    try:
        start_wecom_long_connection_listener()
    except Exception as e:
        print(f"[Init Warning] start_wecom_long_connection_listener: {e}")

    try:
        start_anniversary_reminder_scheduler(app)
    except Exception as e:
        print(f"[Init Warning] start_anniversary_reminder_scheduler: {e}")

    try:
        start_backup_scheduler(app)
    except Exception as e:
        print(f"[Init Warning] start_backup_scheduler: {e}")




