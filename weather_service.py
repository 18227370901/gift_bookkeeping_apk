# -*- coding: utf-8 -*-
"""
天气查询服务模块（数据源：Open-Meteo）
负责：
  1. 城市名 → 经纬度/时区（Geocoding API）
  2. 实时天气 + 每日预报（Forecast API，V10.11.7 起默认 15 天）
  3. WMO weather_code → 中文描述映射
  4. 统一错误处理（网络失败 / API 返回 error / 城市未找到）
  5. V10.11.7 级联选择支持：省/市/区县 → 候选城市智能匹配

API 参考：
  - 地理编码: GET https://geocoding-api.open-meteo.com/v1/search?name={城市}&count=10&language=zh&format=json
  - 天气预报: GET https://api.open-meteo.com/v1/forecast
              ?latitude=&longitude=&current=...&daily=...&timezone=auto&forecast_days=15
  Open-Meteo 错误返回格式: {"error": true, "reason": "..."}
"""
import requests

# ===================== 常量 =====================
GEOCODING_URL = 'https://geocoding-api.open-meteo.com/v1/search'
FORECAST_URL = 'https://api.open-meteo.com/v1/forecast'
# 连接超时 5 秒 / 读取超时 8 秒，避免天气服务异常拖慢页面（实际网络策略见 _http_get）
TIMEOUT = (5, 8)
# 城市名长度上限（前端与后端双重校验）
CITY_NAME_MAX_LEN = 30
# 默认预报天数（V10.11.7：3 → 15）
DEFAULT_FORECAST_DAYS = 15
# 预报天数上限（Open-Meteo 免费档最大 16 天）
MAX_FORECAST_DAYS = 16
# 级联匹配：地理编码返回的候选数量
GEOCODE_CANDIDATES = 10

# ===================== WMO weather_code → 中文 映射表 =====================
# 参照 WMO 标准天气码（Open-Meteo 使用该标准），覆盖全部常见取值
WMO_CODE_CN = {
    0: '晴',
    1: '基本晴朗',
    2: '局部多云',
    3: '阴天',
    4: '阴天',
    45: '雾',
    48: '冻雾',
    51: '毛毛雨',
    53: '毛毛雨',
    55: '毛毛雨',
    56: '冻毛毛雨',
    57: '冻毛毛雨',
    61: '小雨',
    63: '中雨',
    65: '大雨',
    66: '冻雨',
    67: '冻雨',
    71: '小雪',
    73: '中雪',
    75: '大雪',
    77: '米雪',
    80: '阵雨',
    81: '阵雨',
    82: '阵雨',
    85: '阵雪',
    86: '阵雪',
    95: '雷阵雨',
    96: '雷暴伴小冰雹',
    99: '雷暴伴大冰雹',
}

# 天气图标映射（FontAwesome 6 类名，供前端渲染天气图标使用）
WEATHER_ICONS = {
    0: 'fa-sun',
    1: 'fa-sun',
    2: 'fa-cloud-sun',
    3: 'fa-cloud',
    4: 'fa-cloud',
    45: 'fa-smog',
    48: 'fa-smog',
    51: 'fa-cloud-rain',
    53: 'fa-cloud-rain',
    55: 'fa-cloud-rain',
    56: 'fa-cloud-rain',
    57: 'fa-cloud-rain',
    61: 'fa-cloud-showers-heavy',
    63: 'fa-cloud-showers-heavy',
    65: 'fa-cloud-showers-heavy',
    66: 'fa-cloud-rain',
    67: 'fa-cloud-rain',
    71: 'fa-snowflake',
    73: 'fa-snowflake',
    75: 'fa-snowflake',
    77: 'fa-snowflake',
    80: 'fa-cloud-showers-heavy',
    81: 'fa-cloud-showers-heavy',
    82: 'fa-cloud-showers-heavy',
    85: 'fa-snowflake',
    86: 'fa-snowflake',
    95: 'fa-cloud-bolt',
    96: 'fa-cloud-bolt',
    99: 'fa-cloud-bolt',
}


class WeatherError(Exception):
    """天气服务统一异常：message 为可直接展示给用户的中文提示"""

    def __init__(self, message, status_code=500):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


# ===================== 工具函数 =====================

def weather_code_cn(code):
    """weather_code → 中文描述；未知码兜底显示（不抛异常）"""
    if code is None:
        return '未知'
    try:
        c = int(code)
    except (TypeError, ValueError):
        return '未知天气({0})'.format(code)
    return WMO_CODE_CN.get(c, '未知天气({0})'.format(c))


def weather_icon(code):
    """weather_code → FontAwesome 图标类；未知码使用默认多云图标"""
    if code is None:
        return 'fa-cloud'
    try:
        c = int(code)
    except (TypeError, ValueError):
        return 'fa-cloud'
    return WEATHER_ICONS.get(c, 'fa-cloud')


# 风向角度 → 中文方位（V10.11.7 新增，用于级联选择天气卡片展示）
def wind_direction_cn(deg):
    """角度转中文方位；缺值返回 None"""
    if deg is None:
        return None
    try:
        d = float(deg) % 360
    except (TypeError, ValueError):
        return None
    names = ['北', '东北', '东', '东南', '南', '西南', '西', '西北']
    # 每 45° 一个方位，22.5° 为分界偏移
    idx = int((d + 22.5) // 45) % 8
    return names[idx]


# 级联匹配辅助：行政区划名称归一化（去掉常见后缀，便于宽松匹配）
def _norm_region(name):
    if not name:
        return ''
    s = str(name).strip()
    # 依次去掉省/市/区/县/自治州/盟/旗等行政区划后缀词
    for suf in ('特别行政区', '维吾尔自治区', '壮族自治区', '回族自治区', '自治区',
                '省辖县级行政区划', '自治区直辖县级行政区划', '自治州', '地区',
                '自治县', '自治旗', '林区', '矿区', '新区', '县级市',
                '市辖区', '省', '市', '区', '县', '盟', '旗'):
        if s.endswith(suf) and len(s) > len(suf):
            s = s[:-len(suf)]
            break
    return s


def _region_match(candidate_val, region_name):
    """候选字段与区划名宽松匹配（归一化后包含或相等）"""
    if not region_name:
        return False
    a = _norm_region(candidate_val)
    b = _norm_region(region_name)
    if not a or not b:
        return False
    return (a == b) or (a in b) or (b in a)


# ===================== Open-Meteo API 封装 =====================

# V10.11.7 网络策略：直连优先 + 系统代理兑底
# 背景：用户机器常开启梯子代理（IE/注册表级），代理不稳时 requests 默认走代理会持续超时；
#       而 Open-Meteo 国内直连通常可达。故先禁代理直连，失败再回落系统代理重试一次，
#       两种网络环境（直连可达 / 仅代理可达）均能覆盖。
HTTP_TIMEOUT = (5, 8)


def _http_get(url, params):
    """Open-Meteo GET 请求：① 禁代理直连；② 失败回落系统代理"""
    try:
        return requests.get(url, params=params, timeout=HTTP_TIMEOUT,
                             proxies={'http': None, 'https': None})
    except (requests.Timeout, requests.RequestException):
        pass
    # 回落：读系统代理/环境变量（默认 trust_env 行为）
    return requests.get(url, params=params, timeout=HTTP_TIMEOUT)


def geocode_city(city_name, province=None, district_city=None):
    """
    根据城市名解析经纬度与时区（Geocoding API）
    V10.11.7 级联模式：city_name=区县名，province/district_city=省/市名时，
    拉取多个候选并按 admin1/admin2 匹配度打分选最佳，避免同名区县错配
    返回: {name, latitude, longitude, timezone, country, admin1, admin2}
    失败: 抛出 WeatherError（未找到城市 / 服务异常 / 超时）
    """
    cascade = bool(province or district_city)
    count = GEOCODE_CANDIDATES if cascade else 1

    def _search(name):
        """单次地理编码请求，返回候选列表（无候选返回空列表；错误抛 WeatherError）"""
        params = {
            'name': name,
            'count': count,
            'language': 'zh',
            'format': 'json',
        }
        try:
            resp = _http_get(GEOCODING_URL, params)
        except requests.Timeout:
            raise WeatherError('城市解析服务超时，请稍后重试', 503)
        except requests.RequestException:
            raise WeatherError('城市解析服务暂时不可用，请稍后重试', 503)

        try:
            data = resp.json()
        except ValueError:
            raise WeatherError('城市解析服务返回数据异常', 502)

        # Open-Meteo 错误返回格式: {"error": true, "reason": "..."}
        if data.get('error'):
            reason = data.get('reason') or '城市解析服务返回错误'
            raise WeatherError(reason, 502)
        return data.get('results') or []

    results = _search(city_name)

    # 级联模式 404 兜底：GeoNames 中文库部分区县需去掉「区/县/市」等后缀才能命中
    # （如「双流区」→「双流」），仅级联选择自动降级，自由文本输入保持原样提示
    if not results and cascade:
        stripped = _norm_region(city_name)
        if stripped and stripped != city_name:
            results = _search(stripped)

    if not results:
        raise WeatherError('未找到该城市，请检查城市名称是否正确', 404)

    # 级联模式：按省/市匹配度选最佳候选
    if cascade:
        def score(r):
            s = 0
            if _region_match(r.get('admin1'), province):
                s += 2
            if _region_match(r.get('admin2'), district_city):
                s += 2
            elif district_city and _region_match(r.get('name'), district_city):
                s += 1
            # 同名候选中优先人口多的（Open-Meteo 按 population 降序返回，保持原序即可）
            return -s
        results = sorted(results, key=score)
        if score(results[0]) >= 0:
            # 无任何匹配：仍取第一个候选，但允许前端通过 city 展示实际定位
            pass

    r = results[0]
    # latitude / longitude 为必填字段，缺失视为数据异常
    if 'latitude' not in r or 'longitude' not in r:
        raise WeatherError('城市解析结果缺少坐标信息', 502)
    return {
        'name': r.get('name') or city_name,
        'latitude': r['latitude'],
        'longitude': r['longitude'],
        # 时区优先使用地理编码返回值；缺失时由 Forecast 调用方回落 timezone=auto
        'timezone': r.get('timezone') or 'auto',
        'country': r.get('country') or '',
        'admin1': r.get('admin1') or '',
        'admin2': r.get('admin2') or '',
    }


def fetch_forecast(latitude, longitude, timezone='auto', days=DEFAULT_FORECAST_DAYS):
    """
    查询实时天气 + 每日预报（Forecast API，V10.11.7 起默认 15 天）
    注意: 请求 daily 数据时必须携带 timezone（auto 或地理编码返回值），否则接口报错
    返回: Open-Meteo 原始 JSON（已校验非错误响应）
    """
    # 天数规范化：默认 15，合法范围 [1, 16]
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = DEFAULT_FORECAST_DAYS
    days = max(1, min(MAX_FORECAST_DAYS, days))

    params = {
        'latitude': latitude,
        'longitude': longitude,
        # 实时天气字段：温度/湿度/体感/天气码/风速/气压/云量/风向/阵风（V10.11.7 扩充）
        'current': (
            'temperature_2m,relative_humidity_2m,'
            'apparent_temperature,weather_code,wind_speed_10m,'
            'surface_pressure,cloud_cover,wind_direction_10m,wind_gusts_10m'
        ),
        # 每日预报字段：天气码 / 最高温 / 最低温 / 降水概率 / 日出日落 / 紫外线 / 降水总量（V10.11.7 扩充）
        'daily': (
            'weather_code,temperature_2m_max,'
            'temperature_2m_min,precipitation_probability_max,'
            'sunrise,sunset,uv_index_max,precipitation_sum'
        ),
        'timezone': timezone or 'auto',
        'forecast_days': days,
    }
    try:
        resp = _http_get(FORECAST_URL, params)
    except requests.Timeout:
        raise WeatherError('天气服务超时，请稍后重试', 503)
    except requests.RequestException:
        raise WeatherError('天气服务暂时不可用，请稍后重试', 503)

    try:
        data = resp.json()
    except ValueError:
        raise WeatherError('天气服务返回数据异常', 502)

    # Open-Meteo 错误响应格式: {"error": true, "reason": "..."}
    if data.get('error'):
        reason = data.get('reason') or '天气服务返回错误'
        raise WeatherError(reason, 502)
    return data


def _build_result(city, data):
    """把 Open-Meteo forecast 原始 JSON 组装为前端结构化结果（V10.11.7 扩充指标）"""
    current = data.get('current') or {}
    daily = data.get('daily') or {}
    dates = daily.get('time') or []
    day_count = len(dates)
    # 与日期数组等长的空位占位，防止某字段缺失导致索引越界
    daily_codes = daily.get('weather_code') or [None] * day_count
    daily_max = daily.get('temperature_2m_max') or [None] * day_count
    daily_min = daily.get('temperature_2m_min') or [None] * day_count
    daily_precip = daily.get('precipitation_probability_max') or [None] * day_count
    daily_sunrise = daily.get('sunrise') or [None] * day_count
    daily_sunset = daily.get('sunset') or [None] * day_count
    daily_uv = daily.get('uv_index_max') or [None] * day_count
    daily_precip_sum = daily.get('precipitation_sum') or [None] * day_count

    daily_list = []
    for i in range(day_count):
        code = daily_codes[i]
        daily_list.append({
            'date': dates[i],
            'weather_code': code,
            'weather_text': weather_code_cn(code),
            'icon': weather_icon(code),
            'temp_max': daily_max[i],
            'temp_min': daily_min[i],
            'precip_prob': daily_precip[i],
            'sunrise': daily_sunrise[i],
            'sunset': daily_sunset[i],
            'uv_index': daily_uv[i],
            'precipitation': daily_precip_sum[i],
        })

    cur_code = current.get('weather_code')
    return {
        'city': city,
        'current': {
            'temperature': current.get('temperature_2m'),
            'apparent_temperature': current.get('apparent_temperature'),
            'humidity': current.get('relative_humidity_2m'),
            'weather_code': cur_code,
            'weather_text': weather_code_cn(cur_code),
            'icon': weather_icon(cur_code),
            'wind_speed': current.get('wind_speed_10m'),
            'wind_direction': current.get('wind_direction_10m'),
            'wind_direction_text': wind_direction_cn(current.get('wind_direction_10m')),
            'wind_gusts': current.get('wind_gusts_10m'),
            'pressure': current.get('surface_pressure'),
            'cloud_cover': current.get('cloud_cover'),
            'time': current.get('time'),
        },
        'daily': daily_list,
    }


def query_weather(city_name, province=None, district_city=None, days=DEFAULT_FORECAST_DAYS):
    """
    对外主入口 1：根据城市名查询天气（自由文本输入）
    （V10.11.7：15 天预报 + 级联匹配消歧 + 扩充指标）
    失败: 抛出 WeatherError（message 为中文提示，status_code 为建议 HTTP 状态码）
    """
    # 1. 城市名 → 经纬度/时区（级联模式下按省/市智能匹配候选）
    city = geocode_city(city_name, province=province, district_city=district_city)
    # 2. 查询实时 + 每日预报（必须传 timezone）
    data = fetch_forecast(city['latitude'], city['longitude'], city['timezone'], days=days)
    return _build_result(city, data)


def query_weather_by_coords(latitude, longitude, days=DEFAULT_FORECAST_DAYS, label=''):
    """
    对外主入口 2：按经纬度直接查询天气（V10.11.7 级联选择专用）
    背景：GeoNames 中文库缺中国区县级地名（如「双流区」0 候选），
         级联选择改用「行政区划数据内置坐标」方案，选中区县后按坐标直查，
         不再依赖城市名搜索，零错配且省一次地理编码请求。
    参数：
      latitude / longitude  区县行政中心坐标（前端级联数据自带）
      label                 展示名称，如「四川省成都市双流区」
    """
    try:
        lat = float(latitude)
        lon = float(longitude)
    except (TypeError, ValueError):
        raise WeatherError('坐标参数格式不正确', 400)
    # 中国陆域及周边粗校验：纬度 [3, 54]、经度 [73, 136]（含港澳台）
    if not (3 <= lat <= 54 and 73 <= lon <= 136):
        raise WeatherError('坐标超出中国行政区划范围', 400)

    city = {
        'name': label or '选定区域',
        'latitude': lat,
        'longitude': lon,
        'timezone': 'auto',
        'country': '中国',
        'admin1': '',
        'admin2': '',
    }
    data = fetch_forecast(lat, lon, 'auto', days=days)
    return _build_result(city, data)