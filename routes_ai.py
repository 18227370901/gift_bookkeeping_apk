# -*- coding: utf-8 -*-
"""
AI 助手路由模块
负责：AI 聊天、会话管理、配置管理、授权管理、推荐问题、页面渲染
"""
import os
import json
from datetime import datetime
from flask import render_template, request, redirect, url_for, flash, jsonify, session, abort
from flask_login import login_required, current_user
from models import db, User, ChatSession, ChatMessage, AIQueryLog
from ai_service import ai_chat, _build_config_list, _get_suggestions, test_ai_config
from webhook_utils import trigger_webhook_event
from models import WebhookConfig


def register_ai_routes(app, log_action=None):
    """注册 AI 助手相关路由"""

    def safe_log(action, detail="", user=None):
        if not log_action:
            return
        try:
            log_action(action, detail, user=user)
        except TypeError:
            try:
                log_action(action, detail)
            except Exception:
                pass
        except Exception:
            pass

    def _ai_access_check():
        """检查当前用户是否有权使用 AI 助手"""
        if not current_user.is_authenticated:
            return False
        return current_user.can_use_ai()

    # ==================== 页面路由 ====================

    @app.route('/ai-assistant')
    @login_required
    def ai_assistant_page():
        """AI 助手聊天页面"""
        if not _ai_access_check():
            flash('您暂无权限使用 AI 助手功能，请联系管理员授权。', 'warning')
            return redirect(url_for('index'))
        return render_template('ai_assistant.html')

    @app.route('/admin/ai-config')
    @login_required
    def admin_ai_config_page():
        """管理员 AI 配置页面"""
        if not current_user.is_admin:
            flash('此页面仅管理员可访问。', 'danger')
            return redirect(url_for('index'))
        return render_template('admin_ai_config.html')

    # ==================== AI 聊天 API ====================

    @app.route('/api/ai/chat', methods=['POST'])
    @login_required
    def api_ai_chat():
        """AI 聊天核心接口"""
        if not _ai_access_check():
            return jsonify({'code': 403, 'message': '您暂无权限使用 AI 助手功能'}), 403

        data = request.get_json(silent=True) or {}
        query = (data.get('query') or '').strip()
        session_id = data.get('session_id')

        if not query:
            return jsonify({'code': 400, 'message': '问题内容不能为空'}), 400

        # 查找或创建会话
        chat_session = None
        if session_id:
            chat_session = ChatSession.query.filter_by(
                id=session_id, user_id=current_user.id
            ).first()
            if not chat_session:
                return jsonify({'code': 404, 'message': '会话不存在'}), 404

        try:
            result = ai_chat(query, current_user, chat_session)
            safe_log('AI 聊天', f'提问: [{query[:50]}]', user=current_user)
            return jsonify({'code': 200, 'data': result})
        except Exception as e:
            print(f"[AI Chat Error] {e}")
            return jsonify({'code': 500, 'message': f'AI 处理出错: {str(e)}'}), 500

    @app.route('/api/ai/suggestions')
    @login_required
    def api_ai_suggestions():
        """获取推荐问题"""
        if not _ai_access_check():
            return jsonify({'code': 403, 'message': '无权限'}), 403
        return jsonify({'code': 200, 'data': _get_suggestions()})

    # ==================== 会话管理 API ====================

    @app.route('/api/ai/sessions')
    @login_required
    def api_ai_sessions():
        """获取当前用户会话列表"""
        if not _ai_access_check():
            return jsonify({'code': 403, 'message': '无权限'}), 403

        sessions = ChatSession.query.filter_by(
            user_id=current_user.id
        ).order_by(ChatSession.updated_at.desc()).all()

        result = []
        for s in sessions:
            msg_count = ChatMessage.query.filter_by(session_id=s.id).count()
            last_msg = ChatMessage.query.filter_by(
                session_id=s.id
            ).order_by(ChatMessage.created_at.desc()).first()

            result.append({
                'id': s.id,
                'title': s.title,
                'message_count': msg_count,
                'last_message': last_msg.content[:100] if last_msg else '',
                'last_role': last_msg.role if last_msg else '',
                'created_at': s.created_at.strftime('%Y-%m-%d %H:%M') if s.created_at else '',
                'updated_at': s.updated_at.strftime('%Y-%m-%d %H:%M') if s.updated_at else ''
            })

        return jsonify({'code': 200, 'data': result})

    @app.route('/api/ai/sessions/<int:session_id>')
    @login_required
    def api_ai_session_detail(session_id):
        """获取会话详情（含消息列表）"""
        if not _ai_access_check():
            return jsonify({'code': 403, 'message': '无权限'}), 403

        chat_session = ChatSession.query.filter_by(
            id=session_id, user_id=current_user.id
        ).first()
        if not chat_session:
            return jsonify({'code': 404, 'message': '会话不存在'}), 404

        messages = ChatMessage.query.filter_by(
            session_id=session_id
        ).order_by(ChatMessage.created_at.asc()).all()

        result = {
            'id': chat_session.id,
            'title': chat_session.title,
            'created_at': chat_session.created_at.strftime('%Y-%m-%d %H:%M') if chat_session.created_at else '',
            'updated_at': chat_session.updated_at.strftime('%Y-%m-%d %H:%M') if chat_session.updated_at else '',
            'messages': [{
                'id': m.id,
                'role': m.role,
                'content': m.content,
                'used_config_name': m.used_config_name or '',
                'used_search': bool(m.used_search),
                'error_hint': m.error_hint or '',
                'created_at': m.created_at.strftime('%Y-%m-%d %H:%M:%S') if m.created_at else ''
            } for m in messages]
        }

        return jsonify({'code': 200, 'data': result})

    @app.route('/api/ai/sessions/create', methods=['POST'])
    @login_required
    def api_ai_session_create():
        """创建新会话"""
        if not _ai_access_check():
            return jsonify({'code': 403, 'message': '无权限'}), 403

        data = request.get_json(silent=True) or {}
        title = (data.get('title') or '新会话').strip()[:100]

        chat_session = ChatSession(
            user_id=current_user.id,
            title=title
        )
        db.session.add(chat_session)
        db.session.commit()

        safe_log('AI 新建会话', f'会话: [{title}]', user=current_user)
        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(), 'system',
                f'AI新建会话',
                f'操作人：{current_user.username} | 页面：AI助手 | 会话：[{title}]',
                page_key='ai_assistant', user_name=current_user.username,
                operator_id=current_user.id
            )
        except Exception:
            pass
        return jsonify({'code': 200, 'data': {
            'id': chat_session.id,
            'title': chat_session.title
        }})

    @app.route('/api/ai/sessions/<int:session_id>/rename', methods=['POST', 'PATCH'])
    @login_required
    def api_ai_session_rename(session_id):
        """重命名会话"""
        if not _ai_access_check():
            return jsonify({'code': 403, 'message': '无权限'}), 403

        chat_session = ChatSession.query.filter_by(
            id=session_id, user_id=current_user.id
        ).first()
        if not chat_session:
            return jsonify({'code': 404, 'message': '会话不存在'}), 404

        data = request.get_json(silent=True) or {}
        title = (data.get('title') or '').strip()[:100]
        if not title:
            return jsonify({'code': 400, 'message': '标题不能为空'}), 400

        chat_session.title = title
        db.session.commit()

        safe_log('AI 重命名会话', f'会话 #{session_id} → [{title}]', user=current_user)
        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(), 'system',
                f'AI重命名会话',
                f'操作人：{current_user.username} | 页面：AI助手 | 会话 #{session_id} → [{title}]',
                page_key='ai_assistant', user_name=current_user.username,
                operator_id=current_user.id
            )
        except Exception:
            pass
        return jsonify({'code': 200, 'data': {'id': chat_session.id, 'title': title}})

    @app.route('/api/ai/sessions/<int:session_id>/delete', methods=['POST', 'DELETE'])
    @login_required
    def api_ai_session_delete(session_id):
        """删除会话及所有消息"""
        if not _ai_access_check():
            return jsonify({'code': 403, 'message': '无权限'}), 403

        chat_session = ChatSession.query.filter_by(
            id=session_id, user_id=current_user.id
        ).first()
        if not chat_session:
            return jsonify({'code': 404, 'message': '会话不存在'}), 404

        title = chat_session.title
        db.session.delete(chat_session)
        db.session.commit()

        safe_log('AI 删除会话', f'会话 #{session_id} [{title}]', user=current_user)
        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(), 'record_delete',
                f'删除AI会话 [{title}]',
                f'操作人：{current_user.username} | 页面：AI助手 | 会话：{title}',
                page_key='ai_assistant', user_name=current_user.username,
                operator_id=current_user.id
            )
        except Exception:
            pass
        return jsonify({'code': 200, 'message': '会话已删除'})

    # ==================== AI 配置管理（仅管理员） ====================

    @app.route('/api/ai/config')
    @login_required
    def api_ai_config():
        """获取 AI 配置（仅管理员）"""
        if not current_user.is_admin:
            return jsonify({'code': 403, 'message': '此接口仅管理员可访问'}), 403

        configs = current_user.get_ai_configs()
        global_key = os.environ.get('OPENAI_API_KEY', '')

        return jsonify({'code': 200, 'data': {
            'ai_configs': configs,
            'ai_api_key': current_user.ai_api_key,
            'ai_base_url': current_user.ai_base_url or '',
            'ai_model': current_user.ai_model or '',
            'has_global_key': bool(global_key),
            'can_manage': True
        }})

    @app.route('/api/ai/config', methods=['PUT', 'POST'])
    @login_required
    def api_ai_config_update():
        """更新 AI 配置（仅管理员）"""
        if not current_user.is_admin:
            return jsonify({'code': 403, 'message': '此接口仅管理员可访问'}), 403

        data = request.get_json(silent=True) or {}
        configs_raw = data.get('ai_configs', [])

        if not isinstance(configs_raw, list):
            return jsonify({'code': 400, 'message': 'ai_configs 必须是数组'}), 400

        current_user.set_ai_configs(configs_raw)
        db.session.commit()

        safe_log('AI 配置更新', f'更新了 {len(configs_raw)} 个 AI 配置', user=current_user)
        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(), 'system',
                f'更新AI配置',
                f'操作人：{current_user.username} | 页面：AI助手配置 | 配置数：{len(configs_raw)}',
                page_key='ai_config', user_name=current_user.username,
                operator_id=current_user.id
            )
        except Exception:
            pass

        # 返回清洗后的配置（不暴露 key 明文）
        configs = current_user.get_ai_configs()
        return jsonify({'code': 200, 'data': {
            'ai_configs': configs,
            'message': 'AI 配置保存成功'
        }})

    # ==================== AI 配置可用性测试（仅管理员） ====================

    @app.route('/api/ai/config/test', methods=['POST'])
    @login_required
    def api_ai_config_test():
        """测试 AI 配置可用性（仅管理员）
        支持两种模式：
        1) 传 config_index: 从已保存的配置中取对应条目测试
        2) 传 api_key/base_url/model: 直接测试未保存的配置
        """
        if not current_user.is_admin:
            return jsonify({'code': 403, 'message': '此接口仅管理员可访问'}), 403

        data = request.get_json(silent=True) or {}

        # 模式1：按索引测试已保存的配置
        config_index = data.get('config_index')
        if config_index is not None:
            try:
                config_index = int(config_index)
            except (ValueError, TypeError):
                return jsonify({'code': 400, 'message': 'config_index 必须是整数'}), 400

            configs = current_user.get_ai_configs()
            if config_index < 0 or config_index >= len(configs):
                return jsonify({'code': 400, 'message': '配置索引超出范围'}), 400

            cfg = configs[config_index]
            result = test_ai_config(
                cfg.get('api_key', ''),
                cfg.get('base_url', ''),
                cfg.get('model', '')
            )
            return jsonify({'code': 200, 'data': result})

        # 模式2：直接传入配置参数测试
        api_key = (data.get('api_key') or '').strip()
        base_url = (data.get('base_url') or '').strip()
        model = (data.get('model') or '').strip()

        result = test_ai_config(api_key, base_url, model)
        return jsonify({'code': 200, 'data': result})

    # ==================== AI 授权管理（仅管理员） ====================

    @app.route('/api/ai/auth-list')
    @login_required
    def api_ai_auth_list():
        """获取用户 AI 授权列表（仅管理员）"""
        if not current_user.is_admin:
            return jsonify({'code': 403, 'message': '此接口仅管理员可访问'}), 403

        users = User.query.filter_by(is_admin=False, is_active=True).all()
        result = [{
            'id': u.id,
            'username': u.username,
            'ai_authorized': bool(u.ai_authorized),
            'has_own_config': bool(u.ai_api_key or u.get_ai_configs()),
            'created_at': u.created_at.strftime('%Y-%m-%d') if u.created_at else ''
        } for u in users]

        return jsonify({'code': 200, 'data': result})

    @app.route('/api/ai/auth-toggle', methods=['POST'])
    @login_required
    def api_ai_auth_toggle():
        """切换用户 AI 授权状态（仅管理员）"""
        if not current_user.is_admin:
            return jsonify({'code': 403, 'message': '此接口仅管理员可访问'}), 403

        data = request.get_json(silent=True) or {}
        user_id = data.get('user_id')
        authorized = data.get('ai_authorized')

        if not user_id:
            return jsonify({'code': 400, 'message': '缺少 user_id'}), 400

        target_user = User.query.get(int(user_id))
        if not target_user:
            return jsonify({'code': 404, 'message': '用户不存在'}), 404

        if target_user.is_admin:
            return jsonify({'code': 400, 'message': '不能修改管理员的授权'}), 400

        target_user.ai_authorized = bool(authorized)
        db.session.commit()

        status = '授权' if authorized else '取消授权'
        safe_log('AI 授权管理', f'用户 [{target_user.username}] {status}', user=current_user)
        try:
            trigger_webhook_event(
                WebhookConfig.query.filter_by(is_enabled=True).all(), 'status_change',
                f'AI授权{status} [{target_user.username}]',
                f'操作人：{current_user.username} | 页面：AI助手配置 | 用户：{target_user.username} | 操作：{status}',
                page_key='ai_config', user_name=current_user.username,
                operator_id=current_user.id
            )
        except Exception:
            pass
        return jsonify({'code': 200, 'data': {
            'user_id': target_user.id,
            'username': target_user.username,
            'ai_authorized': target_user.ai_authorized
        }})
