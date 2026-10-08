# -*- coding: utf-8 -*-
"""
人情礼金业务工具模块：
1. 自然语言极简记账文本解析器（支持多条语句复合分割与批量解析）
2. 智能还礼金额建议计算器
3. 亲友往来对账计算器
"""

import re
from datetime import datetime


def cn2num(s):
    """支持汉字数字及金额转浮点数"""
    if not s:
        return 0.0
    s = str(s).strip()
    s = s.replace('元', '').replace('圆', '').replace('正', '').replace('整', '').strip()
    try:
        return float(s)
    except ValueError:
        pass

    num_map = {
        '零': 0, '壹': 1, '贰': 2, '叁': 3, '肆': 4, '伍': 5, '陆': 6, '柒': 7, '捌': 8, '玖': 9,
        '一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '两': 2
    }
    unit_map = {
        '拾': 10, '十': 10, '佰': 100, '百': 100, '仟': 1000, '千': 1000, '万': 10000, '亿': 100000000
    }

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


def split_gift_nlp_text(text):
    """
    将可能包含多条记账的复合文本智能拆分为单条语句。
    支持分隔符：
    - 顿号（、）
    - 分号（; ；）
    - 换行符（\r \n）
    - 竖线（|）
    - 逗号（, ，）及连词（以及、还有、并且，当片段内包含多个金额时）
    """
    if not text:
        return []
    raw = text.strip()
    primary_chunks = re.split(r'[\r\n;；、|]+', raw)
    results = []
    for chunk in primary_chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        amounts = re.findall(r'(?:\d+(?:\.\d+)?|[一二两三四五六七八九十百千万]+)\s*(?:元|块|RMB|rmb|￥|¥)?', chunk)
        if len(amounts) > 1 and re.search(r'[,，\s]|以及|还有|并且', chunk):
            sub_chunks = re.split(r'[,，\s]+|以及|还有|并且', chunk)
            for sub in sub_chunks:
                sub = sub.strip()
                if sub:
                    results.append(sub)
        else:
            results.append(chunk)
    return results


def parse_gift_nlp(text):
    """
    自然语言解析单条记账信息：
    例如：“昨天李四儿子满月微信随了600”、“收张三结婚礼金888”
    返回: {name, amount, event_reason, record_type, notes}
    """
    text = (text or '').strip()
    result = {
        'name': '',
        'amount': 0.0,
        'event_reason': '其它',
        'record_type': 'receive',
        'notes': text
    }
    if not text:
        return result

    # 1. 识别收支类型（优先判定最早出现的关键词位置）
    receive_keywords = ['收了', '收到', '收下', '收礼', '收', '入账', '入礼', '受礼', '接礼']
    send_keywords = ['随了', '随礼', '随给', '送了', '送去', '包了', '支出', '出礼', '给', '转账给', '红包给', '送', '随']

    first_send = min([text.find(kw) for kw in send_keywords if kw in text], default=999)
    first_receive = min([text.find(kw) for kw in receive_keywords if kw in text], default=999)

    if first_send < first_receive:
        result['record_type'] = 'send'
    elif first_receive < 999:
        result['record_type'] = 'receive'
    else:
        result['record_type'] = 'receive'

    # 2. 提取金额
    m_num = re.search(r'(\d+(?:\.\d+)?)\s*(?:元|块|RMB|rmb|￥|¥)?', text)
    if m_num:
        try:
            result['amount'] = float(m_num.group(1))
        except Exception:
            result['amount'] = cn2num(text)
    else:
        result['amount'] = cn2num(text)

    # 3. 提取事由
    reasons_map = {
        '结婚': '婚宴', '婚礼': '婚宴', '大婚': '婚宴', '新婚': '婚宴',
        '满月': '满月酒', '满月酒': '满月酒', '百天': '满月酒', '百日宴': '满月酒',
        '周岁': '周岁宴', '周岁宴': '周岁宴',
        '寿宴': '寿宴', '大寿': '寿宴', '过寿': '寿宴', '生日': '寿宴',
        '升学': '升学宴', '高考': '升学宴', '中考': '升学宴', '升学宴': '升学宴',
        '乔迁': '乔迁宴', '乔迁宴': '乔迁宴', '进家': '乔迁宴', '搬家': '乔迁宴',
        '白事': '白事人情', '白事人情': '白事人情', '丧事': '白事人情',
        '开业': '其它'
    }
    for kw, target_reason in reasons_map.items():
        if kw in text:
            result['event_reason'] = target_reason
            break

    # 4. 提取人名
    clean_text = text
    stop_words = [
        '昨天', '今天', '前天', '大前天', '明天', '上周', '上午', '下午', '晚上',
        '微信', '支付宝', '现金', '银行卡', '随了', '随礼', '送了', '收了', '收到',
        '收下', '收礼', '礼金', '份子钱', '份子', '贺礼', '出礼', '入礼', '转账给', '红包给',
        '元', '块', '儿子', '女儿', '孩子', '喜事', '办席', '人情', '红包',
        '结婚', '婚礼', '大婚', '新婚', '满月', '满月酒', '百天', '百日宴',
        '周岁', '周岁宴', '寿宴', '大寿', '过寿', '生日', '升学', '高考', '中考',
        '乔迁', '乔迁宴', '进家', '搬家', '白事', '白事人情', '丧事', '开业'
    ]
    for sw in stop_words:
        clean_text = clean_text.replace(sw, ' ')
    clean_text = re.sub(r'\d+(\.\d+)?', ' ', clean_text)
    clean_text = re.sub(r'[,，。！？、\\.!?~@#￥%&*()（）;；|]', ' ', clean_text)
    tokens = [t.strip() for t in clean_text.split() if t.strip()]

    if tokens:
        candidate = tokens[0]
        for t in tokens:
            stripped_t = re.sub(r'^(?:收|送|随|给)\s*', '', t)
            if 2 <= len(stripped_t) <= 4:
                candidate = stripped_t
                break
            elif 2 <= len(t) <= 4:
                candidate = t
                break
        else:
            candidate = re.sub(r'^(?:收|送|随|给)\s*', '', candidate)
        result['name'] = candidate

    return result


def parse_gift_nlp_multi(text):
    """
    解析可能包含多条记账的文本，返回解析结果列表：
    [ {name, amount, event_reason, record_type, notes}, ... ]
    """
    chunks = split_gift_nlp_text(text)
    if not chunks:
        chunks = [text]
    results = []
    for chunk in chunks:
        p = parse_gift_nlp(chunk)
        if p.get('name') and p.get('amount', 0) > 0:
            results.append(p)
    if not results and text.strip():
        single = parse_gift_nlp(text)
        results.append(single)
    return results


def get_gift_suggestion(user_accessible_records, person_name):
    """
    智能还礼金额建议
    """
    name = (person_name or '').strip()
    if not name:
        return {
            'found': False,
            'suggestion': '请输入亲友姓名获取还礼建议'
        }

    history_records = [
        r for r in user_accessible_records
        if (r.name or '').strip() == name and getattr(r, 'record_type', 'receive') not in ('send', 'give')
    ]

    if not history_records:
        return {
            'found': False,
            'suggestion': f'暂未查询到 [{name}] 的历史来礼记录，建议根据关系亲疏随礼 200~800 元'
        }

    latest_rec = sorted(history_records, key=lambda x: x.created_at, reverse=True)[0]
    base_amt = float(latest_rec.amount)
    rec_year = latest_rec.created_at.year if latest_rec.created_at else datetime.now().year
    current_year = datetime.now().year
    years_diff = max(0, current_year - rec_year)

    inflation_rate = 1.0 + (0.06 * years_diff)
    suggest_min = int(round((base_amt * inflation_rate) / 100.0) * 100)
    suggest_max = int(round(((base_amt * inflation_rate) + 200) / 100.0) * 100)

    if suggest_min < base_amt:
        suggest_min = int(base_amt)
    if suggest_max <= suggest_min:
        suggest_max = suggest_min + 200

    return {
        'found': True,
        'base_amount': base_amt,
        'original_year': rec_year,
        'years_diff': years_diff,
        'suggest_min': suggest_min,
        'suggest_max': suggest_max,
        'suggestion': f'该亲友于 {rec_year} 年（{years_diff}年前）来礼 ¥{base_amt:.0f}元。综合人情往来与通胀，建议还礼区间：¥{suggest_min} ~ ¥{suggest_max} 元（整百为宜）'
    }


def calculate_reconciliation(records):
    """
    人情对账计算器
    """
    balance_map = {}
    total_received = 0.0
    total_given = 0.0

    for r in records:
        name = (r.name or '').strip()
        if not name:
            continue

        if name not in balance_map:
            balance_map[name] = {
                'person_name': name,
                'received_amount': 0.0,
                'given_amount': 0.0,
                'records_count': 0,
                'last_interaction_date': r.created_at,
                'records': []
            }

        amt = float(r.amount) if r.amount else 0.0
        r_type = getattr(r, 'record_type', 'receive')
        is_send = (r_type in ('send', 'give'))

        if is_send:
            balance_map[name]['given_amount'] += amt
            total_given += amt
        else:
            balance_map[name]['received_amount'] += amt
            total_received += amt

        balance_map[name]['records_count'] += 1
        if r.created_at and (not balance_map[name]['last_interaction_date'] or r.created_at > balance_map[name]['last_interaction_date']):
            balance_map[name]['last_interaction_date'] = r.created_at

        balance_map[name]['records'].append({
            'id': r.id,
            'amount': amt,
            'record_type': 'send' if is_send else 'receive',
            'type_label': '随礼(出)' if is_send else '收礼(入)',
            'event_reason': r.event_reason,
            'created_at': r.created_at.strftime('%Y-%m-%d') if r.created_at else ''
        })

    balance_list = []
    for name, item in balance_map.items():
        net = item['received_amount'] - item['given_amount']
        item['net_balance'] = round(net, 2)
        if net > 0:
            # 我方收 > 送 → 我方欠对方人情 → 待补礼（我方需回礼）
            item['status_text'] = '待补礼'
            item['status_class'] = 'text-danger fw-bold'
            item['badge_class'] = 'bg-danger-subtle text-danger border border-danger-subtle'
            item['status_desc'] = f'我欠对方 ¥{net:.2f}'
        elif net < 0:
            # 我方送 > 收 → 对方欠我方人情 → 待还礼（尚欠我方）
            item['status_text'] = '待还礼'
            item['status_class'] = 'text-success fw-bold'
            item['badge_class'] = 'bg-success-subtle text-success border border-success-subtle'
            item['status_desc'] = f'对方欠我 ¥{abs(net):.2f}'
        else:
            item['status_text'] = '已平账'
            item['status_class'] = 'text-success'
            item['badge_class'] = 'bg-success-subtle text-success border border-success-subtle'
            item['status_desc'] = '双方人情已对平'

        item['received_amount'] = round(item['received_amount'], 2)
        item['given_amount'] = round(item['given_amount'], 2)
        balance_list.append(item)

    balance_list.sort(key=lambda x: abs(x['net_balance']), reverse=True)

    return balance_list, round(total_received, 2), round(total_given, 2)
