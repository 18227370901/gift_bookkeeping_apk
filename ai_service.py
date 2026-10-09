# -*- coding: utf-8 -*-
"""
AI 助手核心服务层
负责：配置优先级管理、System Prompt 构建、OpenAI 调用、本地兜底
"""
import os
import re
import time
from datetime import datetime

# 延迟导入 OpenAI SDK（可选依赖，未安装时降级为本地引擎）
# V10.10.16 性能优化：openai SDK 含 pydantic v2 Rust 扩展 + httpx ~40-55MB，
# 移至首次实际调用时才加载，进程启动不再预载
OpenAI = None
_HAS_OPENAI = None  # None=未检测, True/False=已检测

def _ensure_openai():
    """首次调用时延迟导入 OpenAI SDK，后续从 sys.modules 缓存获取"""
    global OpenAI, _HAS_OPENAI
    if _HAS_OPENAI is not None:
        return OpenAI
    try:
        from openai import OpenAI as _OpenAI
        OpenAI = _OpenAI
        _HAS_OPENAI = True
    except ImportError:
        _HAS_OPENAI = False
    return OpenAI

from models import db, User, ChatSession, ChatMessage, AIQueryLog
from models import encrypt_credential, decrypt_credential
from web_search import needs_search, search_and_summarize


# ==================== System Prompt ====================

GENERAL_CHAT_PROMPT = """你是一个乐于助人的智能助手，名字叫"礼小宝"。

## 当前日期
今天是 {today}（{weekday}）。
当用户询问日期/时间/星期/节假日等问题时，以此为准。

## 网络搜索结果
{search_context}
如果有搜索结果，优先基于搜索结果回答，末尾标注信息来源。
如显示"（无网络搜索结果）"，则按自身知识回答。

## 用户问题
{user_query}

## 输出要求
1. 语气温暖、耐心，像朋友聊天
2. 回答简洁明了，直击重点
3. 涉及人情往来、礼金、婚宴等话题时，结合中国文化习惯给出建议
4. 有搜索结果时末尾标注"（信息来源：网络搜索）"
"""

# ==================== 配置优先级体系 ====================

def _build_config_list(user=None):
    """
    构建 AI 配置优先级列表
    返回 [{name, api_key, base_url, model}] 列表（已去重）
    """
    configs = []
    seen_keys = set()

    def _add_if_unique(name, api_key, base_url, model):
        if not api_key or api_key in seen_keys:
            return
        seen_keys.add(api_key)
        configs.append({
            'name': name,
            'api_key': api_key,
            'base_url': base_url or '',
            'model': model or 'gpt-4o-mini'
        })

    # 第1优先级：用户多配置 (user.ai_configs)
    if user:
        user_configs = user.get_ai_configs()
        for cfg in user_configs:
            if cfg.get('enabled', True):
                _add_if_unique(
                    cfg.get('name', '用户配置'),
                    cfg.get('api_key', ''),
                    cfg.get('base_url', ''),
                    cfg.get('model', '')
                )

        # 第2优先级：用户旧版单配置
        if user.ai_api_key:
            _add_if_unique(
                '旧版配置',
                user.ai_api_key,
                getattr(user, 'ai_base_url', ''),
                getattr(user, 'ai_model', '')
            )

    # 第3优先级：全局配置（环境变量）
    global_key = os.environ.get('OPENAI_API_KEY', '')
    if global_key:
        _add_if_unique(
            '全局配置',
            global_key,
            os.environ.get('OPENAI_BASE_URL', ''),
            os.environ.get('OPENAI_MODEL', 'gpt-4o-mini')
        )

    # 第4优先级：管理员共享配置
    if user and not configs:
        if not getattr(user, 'is_admin', False) and getattr(user, 'ai_authorized', False):
            admin = User.query.filter_by(is_admin=True).first()
            if admin:
                admin_configs = admin.get_ai_configs()
                for cfg in admin_configs:
                    if cfg.get('enabled', True):
                        _add_if_unique(
                            '管理员共享配置',
                            cfg.get('api_key', ''),
                            cfg.get('base_url', ''),
                            cfg.get('model', '')
                        )
                # 兼容管理员旧版字段
                if not configs and admin.ai_api_key:
                    _add_if_unique(
                        '管理员共享配置',
                        admin.ai_api_key,
                        getattr(admin, 'ai_base_url', ''),
                        getattr(admin, 'ai_model', '')
                    )

    return configs


# ==================== OpenAI 调用 ====================

def _call_openai(prompt, api_key, base_url, model):
    """
    调用 OpenAI API
    返回 (success: bool, response: str, error: str)
    """
    _ensure_openai()
    if not _HAS_OPENAI:
        return False, '', 'OpenAI SDK 未安装'

    try:
        client_kwargs = {'api_key': api_key}
        if base_url:
            client_kwargs['base_url'] = base_url
        client = OpenAI(**client_kwargs)

        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "你是一个乐于助人的智能助手，名叫礼小宝。"},
                {"role": "user", "content": prompt}
            ],
            max_tokens=2000,
            temperature=0.7,
            timeout=30
        )
        content = resp.choices[0].message.content.strip()
        if content:
            return True, content, ''
        return False, '', 'API 返回空内容'
    except Exception as e:
        return False, '', str(e)


# ==================== 配置可用性测试 ====================

def test_ai_config(api_key, base_url, model):
    """
    测试单个 AI 配置是否可用
    发送一条简短测试消息，验证连通性、鉴权、接口返回是否正常
    返回 dict: { success: bool, message: str, latency_ms: int, detail: str }
    """
    if not api_key:
        return {'success': False, 'message': 'API Key 为空，请先填写', 'latency_ms': 0, 'detail': ''}

    if not model:
        model = 'gpt-4o-mini'  # 未填模型时使用默认值

    _ensure_openai()
    if not _HAS_OPENAI:
        return {'success': False, 'message': 'OpenAI SDK 未安装，无法测试', 'latency_ms': 0,
                'detail': '请在服务器上执行 pip install openai 安装 SDK'}

    start_time = time.time()
    try:
        client_kwargs = {'api_key': api_key}
        if base_url:
            client_kwargs['base_url'] = base_url
        client = OpenAI(**client_kwargs)

        # 发送极简测试消息，max_tokens 限制为 20 以快速返回
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "你是一个测试助手，请简短回复。"},
                {"role": "user", "content": "请回复'测试成功'四个字"}
            ],
            max_tokens=20,
            temperature=0,
            timeout=15  # 测试用较短超时，快速反馈
        )
        content = resp.choices[0].message.content.strip()
        latency_ms = int((time.time() - start_time) * 1000)

        if content:
            return {
                'success': True,
                'message': '配置可用，连接正常',
                'latency_ms': latency_ms,
                'detail': f'模型 {model} 回复: {content[:100]}'
            }
        return {
            'success': False,
            'message': 'API 返回空内容',
            'latency_ms': latency_ms,
            'detail': '接口连通正常但返回内容为空，请检查模型名称是否正确'
        }

    except Exception as e:
        latency_ms = int((time.time() - start_time) * 1000)
        err_str = str(e)

        # 常见错误自动归类提示
        err_lower = err_str.lower()
        if 'authentication' in err_lower or 'api key' in err_lower or '401' in err_lower:
            reason = 'API Key 无效或已过期'
        elif 'connection' in err_lower or 'connect' in err_lower or 'timeout' in err_lower or 'refused' in err_lower:
            reason = '无法连接到 API 地址，请检查 Base URL 是否正确'
        elif 'not found' in err_lower or '404' in err_lower or 'model' in err_lower:
            reason = '模型名称不存在，请检查 Model 参数'
        elif 'rate limit' in err_lower or '429' in err_lower:
            reason = 'API 调用频率超限，Key 额度可能用尽'
        elif 'insufficient' in err_lower or 'quota' in err_lower or 'billing' in err_lower:
            reason = 'API 账户余额不足或额度已用尽'
        else:
            reason = '未知错误'

        return {
            'success': False,
            'message': reason,
            'latency_ms': latency_ms,
            'detail': err_str[:500]
        }


# ==================== 本地兜底引擎 ====================

_LOCAL_INTENTS = [
    ("礼金", "关于礼金金额建议：\n1. 了解当地行情：不同地区礼金标准不同，一般200-1000元不等\n2. 考虑关系亲疏：至亲好友多给，普通同事少给\n3. 参考收礼记录：如有往来记录，按对等或略高原则回礼\n4. 注意双数吉利：尽量选200、600、800等吉利数字，避开单数和含4的数字"),
    ("婚宴", "婚宴礼金建议：\n1. 普通同事/朋友：200-500元\n2. 较好朋友：500-1000元\n3. 至亲好友：1000元以上\n4. 注意当地风俗和酒店档次"),
    ("满月酒", "满月酒礼金建议：\n1. 普通朋友：200-500元\n2. 较好朋友：500-800元\n3. 亲戚：500-1000元\n4. 可搭配婴儿用品作为礼物"),
    ("寿宴", "寿宴礼金建议：\n1. 普通亲友：200-500元\n2. 较亲的亲戚：500-1000元\n3. 至亲：1000元以上\n4. 寿宴讲究吉利数字，如666、888等"),
    ("记账", "人情记账建议：\n1. 及时记录：每次收支后立即记录，避免遗忘\n2. 分类清晰：标注收礼/送礼、事由、金额、日期\n3. 关联宴席：办宴时关联到专属宴席大账本\n4. 定期对账：利用人情对账功能查看往来结余"),
    ("宴席", "办宴预算建议：\n1. 确定桌数：根据预计来宾人数确定\n2. 酒席费用：每桌单价 × 桌数\n3. 附加费用：烟酒、婚庆、场地布置等\n4. 预留弹性：总预算增加10%-15%作为机动支出"),
    ("回礼", "回礼原则：\n1. 对等原则：对方送多少回多少\n2. 略高原则：可比对方多100-200元表示诚意\n3. 及时性：对方有事时及时回礼\n4. 记录查询：查看人情对账了解往来明细"),
    ("乔迁", "乔迁之喜礼金建议：\n1. 普通朋友：200-500元\n2. 较好朋友：500-800元\n3. 亲戚：500-1000元\n4. 也可送实用家电或装饰品"),
    ("升学宴", "升学宴礼金建议：\n1. 普通朋友：200-500元\n2. 较好朋友：500-800元\n3. 亲戚：500-1000元\n4. 可搭配学习用品或红包"),
    ("白事", "白事人情建议：\n1. 金额单数：白事礼金一般用单数（如301、501）\n2. 普通朋友：300-500元\n3. 亲戚：500-1000元\n4. 不用红色信封，用白色或素色信封"),
    ("份子钱", "份子钱建议：\n1. 了解当地行情\n2. 参考往来记录\n3. 关系越近金额越高\n4. 双数为佳（200、600、800等）"),
    ("人情", "人情往来建议：\n1. 有来有往：保持收支平衡\n2. 及时记录：每次收支都记录在账\n3. 定期盘点：查看人情对账，了解结余\n4. 提前规划：看到纪念日提醒即可准备"),
]


def _local_question(query):
    """
    本地兜底回复引擎（无 API Key 时使用）
    关键词匹配 → 命中则返回对应预设回答
    未命中 → 返回通用引导消息
    """
    if not query:
        return "请输入您的问题。"

    query_lower = query.lower()
    for keyword, answer in _LOCAL_INTENTS:
        if keyword in query_lower:
            return answer

    return (
        "您好！我是礼小宝，目前在离线模式下运行。\n\n"
        "您可以问我关于礼金金额、人情往来、宴席预算等方面的问题。\n\n"
        "如需更智能的回答，请联系管理员配置 AI API Key。"
    )


# ==================== 核心 AI 聊天函数 ====================

def ai_chat(query, user, session=None):
    """
    AI 聊天核心函数
    返回 dict: {
        response, used_openai, used_config_name, used_search,
        latency_ms, suggestions, error_hint, session_id, session_title
    }
    """
    start_time = time.time()

    # 1. 判断是否需要联网搜索
    search_context = "（无网络搜索结果）"
    used_search = False
    if needs_search(query):
        search_context = search_and_summarize(query)
        used_search = "无网络搜索结果" not in search_context

    # 2. 构建 prompt
    today = datetime.now().strftime('%Y年%m月%d日')
    weekday_cn = ['星期一', '星期二', '星期三', '星期四', '星期五', '星期六', '星期日'][datetime.now().weekday()]
    prompt = GENERAL_CHAT_PROMPT.format(
        today=today,
        weekday=weekday_cn,
        search_context=search_context,
        user_query=query
    )

    # 3. 构建配置列表并逐个尝试
    configs = _build_config_list(user)
    response = ''
    used_openai = False
    used_config_name = ''
    error_hint = ''

    for cfg in configs:
        success, resp, err = _call_openai(
            prompt, cfg['api_key'], cfg['base_url'], cfg['model']
        )
        if success:
            response = resp
            used_openai = True
            used_config_name = cfg['name']
            break
        else:
            error_hint = f"配置[{cfg['name']}]调用失败: {err}"

    # 4. 全部失败 → 本地兜底
    if not response:
        response = _local_question(query)
        used_config_name = ''
        if configs:
            error_hint = f"所有 AI 配置均调用失败，已使用本地知识引擎回复。最后错误: {error_hint}"
        else:
            error_hint = '当前未配置 AI API Key，已使用本地知识引擎回复。请联系管理员配置 AI 以获得更智能的回答。'

    latency_ms = int((time.time() - start_time) * 1000)

    # 5. 保存消息到数据库
    session_id = None
    session_title = '新会话'
    if session:
        session_id = session.id
        session_title = session.title
    elif user:
        # 自动创建新会话
        session = ChatSession(
            user_id=user.id,
            title=query[:30] + ('...' if len(query) > 30 else '')
        )
        db.session.add(session)
        db.session.flush()
        session_id = session.id
        session_title = session.title

    # 保存用户消息
    if session:
        user_msg = ChatMessage(
            session_id=session.id,
            role='user',
            content=query
        )
        db.session.add(user_msg)

        # 保存 AI 消息
        ai_msg = ChatMessage(
            session_id=session.id,
            role='ai',
            content=response,
            used_config_name=used_config_name,
            used_search=used_search,
            error_hint=error_hint
        )
        db.session.add(ai_msg)

        # 更新会话时间
        session.updated_at = datetime.now()

    # 写入旧版日志
    if user:
        log_entry = AIQueryLog(
            user_id=user.id,
            session_id=str(session_id) if session_id else '',
            query_type='qa',
            query_text=query,
            response_text=response,
            response_time_ms=latency_ms
        )
        db.session.add(log_entry)

    db.session.commit()

    # 6. 推荐问题
    suggestions = _get_suggestions()

    return {
        'query': query,
        'response': response,
        'used_openai': used_openai,
        'used_config_name': used_config_name,
        'used_search': used_search,
        'latency_ms': latency_ms,
        'suggestions': suggestions,
        'error_hint': error_hint,
        'session_id': session_id,
        'session_title': session_title
    }


# ==================== 推荐问题 ====================

_SUGGESTIONS = [
    "婚宴礼金一般给多少合适？",
    "满月酒和周岁宴礼金有什么区别？",
    "如何做好人情往来的记账？",
    "乔迁之喜送什么礼好？",
    "白事人情有哪些讲究？",
    "办婚宴预算怎么规划？",
    "人情对账怎么算？",
    "寿宴礼金有什么讲究？",
]


def _get_suggestions():
    """返回推荐问题列表"""
    return _SUGGESTIONS


# ==================== OCR 图片智能识别（功能三） ====================

def _sniff_image_mime(image_base64):
    """根据 base64 头部嗅探图片真实 MIME 类型（部分网关严格校验 data URI 的 MIME）

    V3.1.1 修复：此前版本 b64decode 后仅截取前 8 字节再判断 prefix[8:12]，
    WEBP 魔数（RIFF....WEBP）的 'WEBP' 恰好位于第 8-12 字节，被截掉后
    永远嗅探不出 webp。现统一解码出 18 字节头部再判断。
    """
    try:
        # 取头部 24 个 base64 字符（解码后 18 字节），覆盖所有格式魔数 + RIFF size + WEBP
        head = str(image_base64)[:24].lstrip('\r\n')
        import base64 as _b64
        pad = -len(head) % 4
        prefix = _b64.b64decode(head + ('=' * pad))[:18]
        if prefix.startswith(b'\x89PNG'):
            return 'image/png'
        if prefix[:3] == b'\xff\xd8\xff':
            return 'image/jpeg'
        if prefix[:4] == b'RIFF' and prefix[8:12] == b'WEBP':
            return 'image/webp'
        if prefix[:2] == b'BM':
            return 'image/bmp'
        if prefix[:4] in (b'II*\x00', b'MM\x00*'):
            return 'image/tiff'
    except Exception:
        pass
    return 'image/jpeg'


def recognize_gift_image(image_base64, user=None):
    """
    调用 AI Vision 模型识别人情簿/礼金簿图片
    image_base64: base64 编码的图片数据（不含 data:image/ 前缀）
    user: 当前用户对象
    返回: {"code": 200, "records": [...], "message": "..."}
    
    V3.1.1 修复：不再按模型名关键词白名单擅自替换用户的模型。
    原逻辑缺陷：用户经自建网关（NewApi/OneAPI 等）配置的模型实际支持图片识别，
    但模型名不含 'gpt-4o'/'vision'/'vl'/'4v'/'claude-3' 关键词时被硬改为
    'gpt-4o-mini'，而该网关无此模型名 → 请求必然失败 → 提示
    "所有 AI 配置均无法识别图片"，误导用户以为配置的 AI 不支持图片。
    新逻辑：① 优先用用户配置的原始模型名直连调用（模型是否支持图片由服务端判定）；
             ② 仅当原始模型调用失败时，才以常见 vision 模型名兜底再试一次；
             ③ 收集每次失败的真实错误并在最终提示中透出，便于诊断。
    """
    OpenAI = _ensure_openai()
    if not OpenAI:
        return {'code': 503, 'message': 'AI 服务未安装 OpenAI SDK，无法识别图片', 'records': []}

    # 获取用户 AI 配置
    configs = []
    if user:
        configs = user.get_ai_configs()
    # 优先取第一个启用的配置
    enabled_configs = [c for c in configs if c.get('enabled', True)] if configs else []
    if not enabled_configs:
        return {'code': 403, 'message': '尚未配置或启用任何 AI 服务，请先在【AI 助手配置】中添加并启用', 'records': []}

    # 清理 base64 中的换行符（部分客户端编码会插入，严格网关会解码失败）
    image_base64 = ''.join(str(image_base64).split())
    image_mime = _sniff_image_mime(image_base64)

    # OCR 识别 Prompt
    ocr_prompt = (
        "请识别这张人情簿/礼金簿图片中的所有记录。"
        "每条记录包含：姓名、金额（元）、事由、收礼或送礼类型。"
        "请以 JSON 数组格式返回，每条记录格式如下：\n"
        '[{"name":"张三","amount":500,"event_reason":"婚宴","record_type":"receive"}]\n'
        "其中 record_type 只有两个值：receive（收礼）或 send（送礼）。\n"
        "如果无法识别某字段，对应值设为 null。\n"
        "请只返回 JSON 数组，不要添加其他文字说明。"
    )

    # 收集每次尝试的真实错误，全部失败时透出便于诊断
    attempts_errors = []

    for cfg in enabled_configs:
        api_key = cfg.get('api_key', '')
        base_url = cfg.get('base_url', '')
        model = (cfg.get('model', '') or '').strip()

        if not api_key:
            attempts_errors.append(f"[{cfg.get('name', '未命名')}] 该配置缺少 API Key，已跳过")
            continue

        # ① 用户配置的原始模型优先（客户端不做能力猜测，由服务端判定）
        # ② 原始模型失败后，才尝试常见 vision 模型名兜底（且不与原始模型重复）
        candidate_models = [model] if model else []
        fallbacks = [m for m in ('gpt-4o-mini', 'gpt-4o', 'gemini-2.0-flash')
                     if m and m != model]
        candidate_models += fallbacks

        for target_model in candidate_models:
            try:
                # V3.2.1：显式设置超时（大图 + 慢网关场景；SDK 默认虽为 600s，
                # 但部分网关/网络环境更早断连，明确超时便于错误信息定位）
                client = OpenAI(api_key=api_key,
                                base_url=base_url if base_url else None,
                                timeout=120)
                response = client.chat.completions.create(
                    model=target_model,
                    messages=[{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": ocr_prompt},
                            {"type": "image_url", "image_url": {
                                "url": f"data:{image_mime};base64,{image_base64}"
                            }}
                        ]
                    }],
                    max_tokens=4000,
                    temperature=0.1
                )
                content = response.choices[0].message.content.strip()

                # 尝试从返回内容中提取 JSON 数组
                import json
                # 去除可能的 markdown 代码块标记
                content = content.replace('```json', '').replace('```', '').strip()
                # 尝试找到 JSON 数组
                start = content.find('[')
                end = content.rfind(']')
                if start != -1 and end != -1:
                    json_str = content[start:end + 1]
                    records = json.loads(json_str)
                    # 清理和验证记录
                    cleaned = []
                    for r in records:
                        if not isinstance(r, dict):
                            continue
                        name = str(r.get('name', '')).strip()
                        amount = r.get('amount')
                        try:
                            amount = float(amount) if amount is not None else 0
                        except (ValueError, TypeError):
                            amount = 0
                        if not name or amount <= 0:
                            continue
                        cleaned.append({
                            'name': name,
                            'amount': amount,
                            # V3.1.1 修复：字段值为 null 时 r.get(...) 返回 None 而非默认值，
                            # str(None) 会变成 'None' 字符串，改用 or 兜底
                            'event_reason': (str(r.get('event_reason') or '其它').strip()) or '其它',
                            'record_type': 'send' if str(r.get('record_type', 'receive')).lower() in ('send', 'give') else 'receive',
                            'notes': str(r.get('notes') or '').strip()
                        })

                    return {
                        'code': 200,
                        'records': cleaned,
                        'message': f'成功识别 {len(cleaned)} 条记录（使用模型: {target_model}）',
                        'config_name': cfg.get('name', '')
                    }
                else:
                    # 原始模型调用成功（未抛异常）但返回内容不像 JSON 数组：
                    # 无需继续尝试兜底模型，直接返回让用户重试
                    return {'code': 422,
                            'message': f'模型 {target_model} 返回内容无法解析为记录列表（请确认为图片识别模型后重试）',
                            'records': []}

            except Exception as e:
                error_msg = str(e)
                # 连接类错误补充提示：base_url 未填写时 SDK 默认走官方域名，
                # 自定义网关场景必然失败——错误透出即可见 "Connection error" 等
                attempts_errors.append(
                    f"[{cfg.get('name', '未命名')} / {target_model}] {error_msg}")
                # 该模型失败，继续尝试下一个候选模型
                continue

    # 全部配置与候选模型均失败：透出最后 3 条真实错误，帮助用户定位
    detail = '；'.join(attempts_errors[-3:]) if attempts_errors else '无可用尝试'
    return {'code': 503,
            'message': f'图片识别失败（已尝试 {len(attempts_errors)} 次）。最后错误：{detail}。'
                       f'请检查 AI 配置的 API Key、接口地址与模型名是否正确，'
                       f'且所用模型需支持图片输入。',
            'records': []}
