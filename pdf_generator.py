# -*- coding: utf-8 -*-
"""
PDF 对账单生成模块（基于 ReportLab）
功能四：生成礼金对账单 PDF，含封面、汇总页、明细页、对账页
"""
import os
import io
from datetime import datetime

# 延迟导入 ReportLab（可选依赖，未安装时优雅降级）
_reportlab = None

def _ensure_reportlab():
    """首次调用时延迟导入 ReportLab"""
    global _reportlab
    if _reportlab is not None:
        return _reportlab
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import mm
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.colors import HexColor
        from reportlab.lib.enums import TA_CENTER, TA_RIGHT
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        _reportlab = {
            'A4': A4, 'mm': mm, 'getSampleStyleSheet': getSampleStyleSheet,
            'ParagraphStyle': ParagraphStyle, 'HexColor': HexColor,
            'TA_CENTER': TA_CENTER, 'TA_RIGHT': TA_RIGHT,
            'SimpleDocTemplate': SimpleDocTemplate, 'Table': Table,
            'TableStyle': TableStyle, 'Paragraph': Paragraph, 'Spacer': Spacer,
            'PageBreak': PageBreak, 'pdfmetrics': pdfmetrics, 'TTFont': TTFont
        }
    except ImportError:
        _reportlab = False
    return _reportlab


def _register_chinese_font():
    """注册中文字体，按优先级尝试多个路径"""
    rl = _ensure_reportlab()
    if not rl:
        return None
    font_candidates = [
        ('NotoSansCJK', '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'),
        ('NotoSansCJK', '/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc'),
        ('NotoSansCJK', '/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc'),
        ('SimSun', 'C:/Windows/Fonts/simsun.ttc'),
        ('SimSun', 'C:/Windows/Fonts/simfang.ttf'),
        ('MSYH', 'C:/Windows/Fonts/msyh.ttc'),
    ]
    for font_name, font_path in font_candidates:
        try:
            if os.path.exists(font_path):
                rl['pdfmetrics'].registerFont(rl['TTFont'](font_name, font_path))
                return font_name
        except Exception:
            continue
    # 尝试 reportlab 内置 CID 字体（不一定可用）
    try:
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        rl['pdfmetrics'].registerFont(UnicodeCIDFont('STSong-Light'))
        return 'STSong-Light'
    except Exception:
        pass
    return None


def _num_to_cn(num):
    """金额转中文大写"""
    num = round(num, 2)
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


def generate_pdf_statement(records, summary, output_buffer=None, title='礼金对账单'):
    """
    生成 PDF 对账单
    records: 记录列表 [{name, record_type, amount, event_reason, notes, created_at}]
    summary: 汇总 {total_received, total_sent, net_amount, total_count}
    output_buffer: io.BytesIO 对象（不传则创建新的）
    返回: io.BytesIO 对象
    """
    rl = _ensure_reportlab()
    if not rl:
        raise RuntimeError('ReportLab 未安装，无法生成 PDF')

    font_name = _register_chinese_font() or 'Helvetica'

    if output_buffer is None:
        output_buffer = io.BytesIO()

    # 创建 PDF 文档
    doc = rl['SimpleDocTemplate'](
        output_buffer,
        pagesize=rl['A4'],
        leftMargin=20 * rl['mm'],
        rightMargin=20 * rl['mm'],
        topMargin=20 * rl['mm'],
        bottomMargin=20 * rl['mm']
    )

    # 样式定义
    styles = rl['getSampleStyleSheet']()
    title_style = rl['ParagraphStyle']('CTitle', parent=styles['Title'],
                                        fontSize=22, textColor=rl['HexColor']('#8B0000'),
                                        fontName=font_name, alignment=rl['TA_CENTER'], spaceAfter=10)
    subtitle_style = rl['ParagraphStyle']('CSubtitle', parent=styles['Normal'],
                                           fontSize=11, textColor=rl['HexColor']('#666666'),
                                           fontName=font_name, alignment=rl['TA_CENTER'], spaceAfter=20)
    section_style = rl['ParagraphStyle']('CSection', parent=styles['Heading2'],
                                           fontSize=14, textColor=rl['HexColor']('#8B0000'),
                                           fontName=font_name, spaceBefore=15, spaceAfter=8)
    normal_style = rl['ParagraphStyle']('CNormal', parent=styles['Normal'],
                                         fontSize=10, fontName=font_name, leading=16)

    elements = []

    # ===== 封面 =====
    elements.append(rl['Spacer'](1, 100))
    elements.append(rl['Paragraph'](title, title_style))
    elements.append(rl['Paragraph'](
        '生成日期：' + datetime.now().strftime('%Y-%m-%d %H:%M'),
        subtitle_style
    ))

    # ===== 汇总页 =====
    elements.append(rl['PageBreak']())
    elements.append(rl['Paragraph']('一、汇总统计', section_style))

    summary_data = [
        ['项目', '金额（元）', '说明'],
        ['收到礼金总额', '¥%.2f' % summary.get('total_received', 0), '对方送我'],
        ['送出礼金总额', '¥%.2f' % summary.get('total_sent', 0), '我送对方'],
        ['净现金流', '%s¥%.2f' % ('+' if summary.get('net_amount', 0) >= 0 else '-',
                            abs(summary.get('net_amount', 0))),
         '收礼-送礼'],
        ['记录总笔数', '%d 笔' % summary.get('total_count', 0), '—'],
    ]
    summary_table = rl['Table'](summary_data, colWidths=[120, 120, 160])
    summary_table.setStyle(rl['TableStyle']([
        ('FONTNAME', (0, 0), (-1, -1), font_name),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('BACKGROUND', (0, 0), (-1, 0), rl['HexColor']('#8B0000')),
        ('TEXTCOLOR', (0, 0), (-1, 0), rl['HexColor']('#ffffff')),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('GRID', (0, 0), (-1, -1), 0.5, rl['HexColor']('#cccccc')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [rl['HexColor']('#ffffff'), rl['HexColor']('#f9f5f0')]),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
    ]))
    elements.append(summary_table)

    # ===== 明细页 =====
    elements.append(rl['PageBreak']())
    elements.append(rl['Paragraph']('二、礼金明细', section_style))

    detail_header = ['序号', '姓名', '类型', '金额', '大写', '事由', '日期']
    detail_rows = [detail_header]
    for i, r in enumerate(records):
        detail_rows.append([
            str(i + 1),
            r.get('name', ''),
            '送礼' if r.get('record_type') in ('send', 'give') else '收礼',
            '¥%.2f' % r.get('amount', 0),
            _num_to_cn(r.get('amount', 0)),
            r.get('event_reason', ''),
            r.get('created_at', '')[:10] if r.get('created_at') else ''
        ])

    detail_table = rl['Table'](detail_rows, colWidths=[35, 60, 40, 65, 80, 70, 70])
    detail_table.setStyle(rl['TableStyle']([
        ('FONTNAME', (0, 0), (-1, -1), font_name),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
        ('BACKGROUND', (0, 0), (-1, 0), rl['HexColor']('#8B0000')),
        ('TEXTCOLOR', (0, 0), (-1, 0), rl['HexColor']('#ffffff')),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('GRID', (0, 0), (-1, -1), 0.3, rl['HexColor']('#dddddd')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [rl['HexColor']('#ffffff'), rl['HexColor']('#faf8f5')]),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))
    elements.append(detail_table)

    # 构建文档
    doc.build(elements)
    output_buffer.seek(0)
    return output_buffer
