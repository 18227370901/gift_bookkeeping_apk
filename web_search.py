# -*- coding: utf-8 -*-
"""
AI 助手联网搜索模块
基于 DuckDuckGo Search 实现，用于判断是否需要联网搜索以及搜索结果注入 prompt
"""
import re

# 延迟导入 DuckDuckGo Search（可选依赖，未安装时降级）
# V10.10.16 性能优化：duckduckgo_search 含 primp Rust 扩展 + lxml ~12-20MB，
# 移至首次实际搜索时才加载
DDGS = None
_HAS_DDS = None  # None=未检测, True/False=已检测

def _ensure_dds():
    """首次调用时延迟导入 DDGS，后续从 sys.modules 缓存获取"""
    global DDGS, _HAS_DDS
    if _HAS_DDS is not None:
        return DDGS
    try:
        from duckduckgo_search import DDGS as _DDGS
        DDGS = _DDGS
        _HAS_DDS = True
    except ImportError:
        try:
            from ddgs import DDGS as _DDGS
            DDGS = _DDGS
            _HAS_DDS = True
        except ImportError:
            _HAS_DDS = False
    return DDGS

# 需要联网搜索的关键词正则模式
_SEARCH_PATTERNS = [
    r"天气|气温|温度|下雨|下雪|台风|雾霾|空气质量",
    r"新闻|最新|最近|今天.*发生|热点|事件|时事",
    r"现在|目前|当前|实时|今天|今日|本月|近期",
    r"搜一下|搜索|查一下|帮我查|查询",
    r"股价|汇率|油价|金价|比特币|基金|股票|理财|利率",
    r"是谁|谁.*说|谁.*做|发生了什么",
    r"几号|星期几|节假日|放假|调休|农历|阴历|阳历|节气",
]


def needs_search(query):
    """判断用户问题是否需要联网搜索"""
    if not query:
        return False
    for pattern in _SEARCH_PATTERNS:
        if re.search(pattern, query, re.IGNORECASE):
            return True
    return False


def web_search(query, max_results=5):
    """
    DuckDuckGo 搜索
    返回 [{title, body, href}] 列表
    """
    _ensure_dds()
    if not _HAS_DDS or not query:
        return []
    try:
        results = []
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=max_results):
                results.append({
                    'title': r.get('title', ''),
                    'body': r.get('body', ''),
                    'href': r.get('href', r.get('link', ''))
                })
        return results
    except Exception as e:
        print(f"[Web Search Error] {e}")
        return []


def search_and_summarize(query):
    """
    搜索并格式化为 prompt 注入文本
    返回格式化字符串，搜索失败返回"无网络搜索结果"占位
    """
    _ensure_dds()
    if not _HAS_DDS:
        return "（无网络搜索结果）"

    results = web_search(query, max_results=5)
    if not results:
        return "（无网络搜索结果）"

    lines = []
    for i, r in enumerate(results, 1):
        title = r.get('title', '')
        body = r.get('body', '')
        lines.append(f"{i}. {title}\n   {body}")

    return "\n".join(lines)
