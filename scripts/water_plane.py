#!/usr/bin/env python3
"""
水上飞机选股 v3.0 — 同花顺主源版
每个交易日 9:27 初筛 + 15:30 收盘验证
数据源: thsdk同花顺问财(主力) + mx-xuangu妙想(降级备选)
预估消耗: ~3K 输入 ≈ ¥0.002 (thsdk零API费用)

v3.0 改进:
  ① 主数据源切换为同花顺问财 — 竞价涨跌幅精度更高、更权威
  ② 一次查询拿到全字段 — 竞价涨幅/竞价金额/量比/换手率/流通市值
  ③ thsdk竞价异动类型识别 — 竞价抢筹/急速上涨等加分
  ④ 流通市值过滤 ≥10亿 — 排除庄股
  ⑤ 多维度综合评分 — 平线+成交+量比+异动+市值
  ⑥ mx-xuangu降级备选 — thsdk失败时自动切换
  ⑦ 统一脚本双模式 — --mode am|pm

用法:
  python3 water_plane.py --mode am   # 早盘初筛 (09:27)
  python3 water_plane.py --mode pm   # 收盘验证 (15:30)
"""
import csv
import os
import sys
import json
import argparse
import subprocess
from datetime import datetime, timezone, timedelta

TZ = timezone(timedelta(hours=8))
now = datetime.now(TZ)

# ======================== 工具函数 ========================
def parse_num(s):
    if not s: return 0
    s = str(s).strip()
    if '万亿' in s: return float(s.replace('万亿','')) * 1e12
    if '亿' in s: return float(s.replace('亿','')) * 1e8
    if '万' in s: return float(s.replace('万','')) * 1e4
    try: return float(s)
    except: return 0

def date_label(dt): return dt.strftime('%Y年%m月%d日')
def date_str(dt): return dt.strftime('%Y%m%d')

def is_trading_day(dt):
    """简易判断交易日（周一到周五）"""
    return dt.weekday() < 5

# ======================== 数据源 ========================
def src_mx_xuangu(query, output_dir):
    """数据源1: mx-xuangu 东方财富妙想选股（主力）"""
    mx_script = os.path.expanduser('~/.openclaw/skills/mx-xuangu/mx_xuangu.py')
    ret = os.system(
        f'cd ~/.openclaw/skills/mx-xuangu && '
        f'python3 mx_xuangu.py --query "{query}" --output-dir "{output_dir}" 2>/dev/null'
    )
    if ret != 0:
        return None
    # 按文件修改时间排序（字母序不可靠，中文文件名排在数字后面）
    csv_files = sorted([
        os.path.join(output_dir, f)
        for f in os.listdir(output_dir)
        if f.startswith('mx_xuangu_') and f.endswith('.csv')
    ], key=lambda p: os.path.getmtime(p), reverse=True)
    # 在前5个最新文件中找竞价相关的（容错其他脚本同时写入）
    for f in csv_files[:5]:
        fname = os.path.basename(f)
        if '集合竞价' in fname and '涨跌幅' in fname and '成交额' in fname:
            return f
    return csv_files[0] if csv_files else None

def src_thsdk_wencai(query):
    """数据源2: thsdk 问财自然语言（降级备选）"""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from thsdk import THS
        from ths_config import get_ths_ops
        with THS(get_ths_ops()) as ths:
            resp = ths.wencai_nlp(query)
            if resp and not resp.df.empty:
                return resp.df
    except Exception:
        # 游客模式降级
        try:
            from thsdk import THS
            with THS() as ths:
                resp = ths.wencai_nlp(query)
                if resp and not resp.df.empty:
                    return resp.df
        except Exception:
            pass
    return None

def src_thsdk_auction_anomaly():
    """数据源3: thsdk 竞价异动扫描（异动类型识别）"""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from thsdk import THS
        from ths_config import get_ths_ops
        results = {}
        with THS(get_ths_ops()) as ths:
            for market, label in [('USHA', '沪'), ('USZA', '深')]:
                resp = ths.call_auction_anomaly(market)
                if resp and not resp.df.empty:
                    # 建立代码→异动类型映射 (THSCODE格式: USHA600519 → 600519)
                    df = resp.df
                    for _, row in df.iterrows():
                        code = str(row.get('代码', ''))
                        atype = str(row.get('异动类型1', ''))
                        # 提取纯数字代码
                        pure_code = code[4:] if len(code) >= 6 else code
                        if pure_code and pure_code.isdigit():
                            if pure_code not in results:
                                results[pure_code] = []
                            results[pure_code].append(atype)
        return results
    except Exception:
        try:
            from thsdk import THS
            results = {}
            with THS() as ths:
                for market in ['USHA', 'USZA']:
                    resp = ths.call_auction_anomaly(market)
                    if resp and not resp.df.empty:
                        df = resp.df
                        for _, row in df.iterrows():
                            code = str(row.get('代码', ''))
                            atype = str(row.get('异动类型1', ''))
                            pure_code = code[4:] if len(code) >= 6 else code
                            if pure_code and pure_code.isdigit():
                                if pure_code not in results:
                                    results[pure_code] = []
                                results[pure_code].append(atype)
            return results
        except Exception:
            pass
    return {}

# ======================== 评分引擎 ========================
ANOMALY_BONUS = {
    '竞价抢筹': 25,
    '急速上涨': 20,
    '大幅高开': 10,
    '大买单试盘': 15,
    '涨停试盘': 5,
    '高开回落': -5,
    '跌停试盘': -20,
    '急速下跌': -20,
    '大幅低开': -10,
}

def score_stock(r, anomaly_map):
    """
    多维度综合评分 (v3.1)
    
    维度:
      1. 竞价平线度 (0-100分): 9:15-9:23价格序列波动率越小越平
      2. 竞价成交额 (0-50分): 成交额越高越好
      3. 量比修正 (-10~+15): 量比>1加分，<0.3减分
      4. 异动类型 (+5~+25): 竞价抢筹/急速上涨等
      5. 流通市值修正 (-5~+5): 中小市值弹性加分
    """
    auction_chg = abs(r['auction_chg'])
    auction_amt = r['auction_amt']
    vol_ratio = r.get('vol_ratio', 1.0)
    float_mv = r.get('float_mv', 0)
    code = r['code']

    # 1) 竞价平线度: 9:20-9:23价格波动率，0%=100分, 0.7%=0分
    flat_vol = r.get('flat_vol')
    if flat_vol is None:
        # 降级: 无价格序列时，用竞价涨跌幅近似
        flat_score = max(0, 100 - auction_chg / 3 * 100)
    else:
        flat_score = max(0, 100 - flat_vol / 0.7 * 100)  # 波动0.7%→0分

    # 2) 竞价成交额: 500万=0分, 5000万=25分, 5亿=50分
    amt_score = min(50, auction_amt / 1e6 * 1.0)  # 每100万1分, 上限50

    # 3) 量比修正
    if vol_ratio >= 5:
        vr_bonus = 15
    elif vol_ratio >= 3:
        vr_bonus = 12
    elif vol_ratio >= 2:
        vr_bonus = 8
    elif vol_ratio >= 1.2:
        vr_bonus = 4
    elif vol_ratio >= 0.5:
        vr_bonus = 0
    elif vol_ratio >= 0.3:
        vr_bonus = -3
    else:
        vr_bonus = -10

    # 4) 异动类型加分
    anomaly_bonus = 0
    if code in anomaly_map:
        for atype in anomaly_map[code]:
            anomaly_bonus = max(anomaly_bonus, ANOMALY_BONUS.get(atype, 0))

    # 5) 流通市值修正: 20-100亿弹性区间+5, 超大-3
    if float_mv > 0:
        if 10e8 <= float_mv < 50e8:
            mv_bonus = 5
        elif 50e8 <= float_mv < 100e8:
            mv_bonus = 3
        elif 100e8 <= float_mv < 500e8:
            mv_bonus = 0
        elif float_mv >= 2000e8:
            mv_bonus = -3
        else:
            mv_bonus = -1
    else:
        mv_bonus = 0

    total = flat_score + amt_score + vr_bonus + anomaly_bonus + mv_bonus
    return total, {
        'flat': flat_score, 'amt': amt_score,
        'vol_ratio': vr_bonus, 'anomaly': anomaly_bonus,
        'mv': mv_bonus
    }

# ======================== CSV解析 ========================
def parse_mx_csv(csv_path, anomaly_map):
    """解析mx-xuangu CSV，启用全量字段"""
    rows = []
    with open(csv_path, encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for r in reader:
            code = str(r.get('代码', '')).strip()
            name = str(r.get('名称', '')).strip()
            if 'ST' in name.upper() or '*' in name or not code:
                continue

            # === 列名动态匹配（带日期后缀如 "量比 2026.05.11"）===
            def _col(*keywords):
                for k in r.keys():
                    if all(kw in k for kw in keywords):
                        return k
                return None

            # 核心字段（注意：全日字段需排除'集合竞价'前缀）
            auction_chg_col = _col('集合竞价', '涨跌幅')
            auction_amt_col = _col('集合竞价', '成交额')
            # 全日涨跌幅: 含'涨跌幅'但不含'集合竞价'
            total_chg_col = None
            for k in r.keys():
                if '涨跌幅' in k and '集合竞价' not in k:
                    total_chg_col = k
                    break
            vol_ratio_col = _col('量比')
            turnover_col = _col('换手率')
            float_mv_col = _col('流通市值')
            # 全日成交量: 含'成交量'但不含'集合竞价'
            vol_col = None
            for k in r.keys():
                if '成交量' in k and '集合竞价' not in k:
                    vol_col = k
                    break
            # 全日成交额: 含'成交额'但不含'集合竞价'
            amt_col = None
            for k in r.keys():
                if '成交额' in k and '集合竞价' not in k:
                    amt_col = k
                    break

            if not auction_chg_col or not auction_amt_col:
                continue

            auction_chg = parse_num(r.get(auction_chg_col, '0'))
            auction_amt = parse_num(r.get(auction_amt_col, '0'))

            # 过滤
            if abs(auction_chg) > 1.0:
                continue
            if 300000 < auction_amt < 10000000:  # 竞价成交额中间区间排除（≥1000万或≤30万）
                continue

            # 流通市值过滤
            float_mv = parse_num(r.get(float_mv_col, '0')) if float_mv_col else 0
            if float_mv > 0 and float_mv < 10e8:  # <10亿小盘股排除
                continue

            vol_ratio = parse_num(r.get(vol_ratio_col, '1')) if vol_ratio_col else 1.0
            turnover = parse_num(r.get(turnover_col, '0')) if turnover_col else 0
            total_chg = parse_num(r.get(total_chg_col, '0')) if total_chg_col else 0
            total_vol = parse_num(r.get(vol_col, '0')) if vol_col else 0
            total_amt = parse_num(r.get(amt_col, '0')) if amt_col else 0

            # 补充: 竞价占全日比
            auction_pct = (auction_amt / total_amt * 100) if total_amt > 0 else 0

            r_data = {
                'code': code, 'name': name,
                'auction_chg': auction_chg,
                'auction_amt': auction_amt,
                'auction_pct': auction_pct,
                'total_chg': total_chg,
                'turnover': turnover,
                'vol_ratio': vol_ratio,
                'float_mv': float_mv,
                'total_vol': total_vol,
                'total_amt': total_amt,
            }

            # 评分
            total_score, detail = score_stock(r_data, anomaly_map)
            r_data['score'] = total_score
            r_data['score_detail'] = detail
            rows.append(r_data)

    return rows

def parse_thsdk_df(df, anomaly_map):
    """解析thsdk wencai返回的DataFrame（主源：同花顺竞价数据）
    字段名带日期后缀如 '竞价涨幅[20260817]'，需动态匹配
    只做初筛+字段解析，评分和平线度在后续步骤完成
    """
    def _col(*keywords):
        for k in df.columns:
            if all(kw in str(k) for kw in keywords):
                return k
        return None

    auction_chg_col = _col('竞价涨幅')
    auction_amt_col = _col('竞价金额', '集合竞价')
    if auction_amt_col is None:
        auction_amt_col = _col('竞价金额')
    vol_ratio_col = _col('量比')
    turnover_col = _col('换手率')
    float_mv_col = _col('a股市值')
    pre_close_col = _col('收盘价')

    rows = []
    for _, r in df.iterrows():
        code_raw = str(r.get('股票代码', ''))
        name = str(r.get('股票简称', ''))
        if 'ST' in name.upper() or '*' in name or not code_raw:
            continue

        # 代码格式: 000001.SZ → 000001
        code = code_raw.split('.')[0] if '.' in code_raw else code_raw

        auction_chg = parse_num(r.get(auction_chg_col, 0)) if auction_chg_col else 0
        auction_amt = parse_num(r.get(auction_amt_col, 0)) if auction_amt_col else 0
        total_chg = parse_num(r.get('最新涨跌幅', 0))
        vol_ratio = parse_num(r.get(vol_ratio_col, 1)) if vol_ratio_col else 1.0
        turnover = parse_num(r.get(turnover_col, 0)) if turnover_col else 0
        float_mv = parse_num(r.get(float_mv_col, 0)) if float_mv_col else 0
        pre_close = parse_num(r.get(pre_close_col, 0)) if pre_close_col else 0

        # 初筛: 开盘涨幅 -1%~+1% / 竞价成交额≥1000万或≤30万 / 流通市值≥10亿
        if abs(auction_chg) > 1.0:
            continue
        if 300000 < auction_amt < 10000000:
            continue
        if float_mv > 0 and float_mv < 10e8:
            continue

        r_data = {
            'code': code, 'name': name,
            'auction_chg': auction_chg,
            'auction_amt': auction_amt,
            'auction_pct': 0,
            'total_chg': total_chg,
            'turnover': turnover,
            'vol_ratio': vol_ratio,
            'float_mv': float_mv,
            'pre_close': pre_close,
            'total_vol': 0,
            'total_amt': 0,
            'flat_vol': None,  # 平线波动率，稍后由竞价价格序列计算
        }
        rows.append(r_data)
    return rows

# ======================== 竞价平线度（价格序列波动） ========================
def _market_prefix(code: str) -> str:
    """6位代码 → 同花顺市场前缀"""
    if code.startswith(('6', '9')):
        return 'USHA'   # 上海A股（含688科创）
    if code.startswith(('0', '3')):
        return 'USZA'   # 深圳A股（含300创业）
    if code.startswith(('4', '8', '920')):
        return 'USTM'   # 北交所
    return 'USHA'


def _auction_prices(ths, code: str):
    """拿竞价 9:20-9:23 价格序列，返回价格列表（无数据返回None）"""
    try:
        resp = ths.call_auction(_market_prefix(code) + code)
    except Exception:
        return None
    if not resp or not resp.data:
        return None
    prices = []
    for entry in resp.data:
        try:
            t = int(entry.get('时间', 0))
            p = float(entry.get('价格', 0))
        except (TypeError, ValueError):
            continue
        if p <= 0:
            continue
        tm = datetime.fromtimestamp(t, TZ)
        hms = tm.hour * 3600 + tm.minute * 60 + tm.second
        # 只看 9:20:00 - 9:23:59
        if 9 * 3600 + 20 * 60 <= hms <= 9 * 3600 + 23 * 60 + 59:
            prices.append(p)
    return prices if len(prices) >= 3 else None


def _flat_vol(prices):
    """价格序列波动率 = (max-min)/均价 × 100%，值越小越平"""
    if not prices:
        return None
    mean = sum(prices) / len(prices)
    if mean <= 0:
        return None
    return (max(prices) - min(prices)) / mean * 100


def enrich_flatline(rows):
    """逐只拿竞价价格序列，计算平线波动率，淘汰波动>0.7%的个股"""
    if not rows:
        return rows
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from thsdk import THS
        from ths_config import get_ths_ops
        ops = get_ths_ops()
    except Exception:
        ops = None

    try:
        ths = THS(ops) if ops else THS()
        ths.connect()
    except Exception:
        ths = None

    if ths is None:
        # 无法拿价格序列，降级：全部保留，平线度用竞价涨跌幅近似
        return rows

    kept = []
    try:
        for r in rows:
            # 缩量票（≤30万）：竞价金额极小，价格天然平，跳过 call_auction 用 fallback
            if r.get('auction_amt', 0) <= 300000:
                r['flat_vol'] = None
                r['line_chg'] = r.get('auction_chg', 0)
                kept.append(r)
                continue
            prices = _auction_prices(ths, r['code'])
            vol = _flat_vol(prices)
            if vol is None:
                # 拿不到序列数据，保留（平线度用涨跌幅近似），避免误杀
                r['flat_vol'] = None
                r['line_chg'] = None
                kept.append(r)
                continue
            r['flat_vol'] = vol
            # 9:20-9:23 竞价涨幅 = 竞价均价 相对 昨收
            pre_close = r.get('pre_close', 0)
            if pre_close > 0 and prices:
                mean_p = sum(prices) / len(prices)
                r['line_chg'] = (mean_p - pre_close) / pre_close * 100
            else:
                r['line_chg'] = r.get('auction_chg', 0)
            if vol > 0.7:
                continue  # 波动超0.7%，不是平线，淘汰
            if r['line_chg'] is not None and abs(r['line_chg']) > 3.0:
                continue  # 9:20-9:23竞价涨幅超±3%，淘汰
            kept.append(r)
    finally:
        try:
            ths.disconnect()
        except Exception:
            pass
    return kept

# ======================== 报告生成 ========================
def generate_report(rows, mode, top_n=10):
    """生成Markdown格式报告"""
    if not rows:
        return None

    label = date_label(now)
    ranked = sorted(rows, key=lambda x: x['score'], reverse=True)[:top_n]

    if mode == 'am':
        title = f'✈️ **水上飞机选股 v3.0 | {label} 09:27**'
        subtitle = f'>{len(rows)} 只符合条件，多维度综合评分 Top {top_n}'
        header = '| # | 代码 | 名称 | 平线波动 | 竞价涨幅 | 开盘涨幅 | 竞价成交 | 量比 | 流通市值 | 异动 | 评分 |'
        sep = '|---|------|------|---------|----------|----------|----------|------|---------|------|------|'
    else:
        title = f'✈️ **水上飞机·收盘验证 v3.0 | {label} 15:30**'
        subtitle = f'>{len(rows)} 只符合条件，全日验证 Top {top_n}'
        header = '| # | 代码 | 名称 | 平线波动 | 竞价涨幅 | 开盘涨幅 | 竞价成交 | 全日涨幅 | 量比 | 异动 | 评分 |'
        sep = '|---|------|------|---------|----------|----------|----------|---------|------|------|------|'

    lines = [f'💰 预估¥0.002 | {title}']
    lines.append(subtitle)
    lines.append('')

    # 数据源标注
    ds_label = 'thsdk同花顺 + 竞价异动识别'
    lines.append(f'> 📡 数据源: {ds_label}')
    lines.append('')

    lines.append(header)
    lines.append(sep)

    for i, r in enumerate(ranked, 1):
        chg = r.get('line_chg', r.get('auction_chg', 0))
        chg_str = '0.00%' if chg == 0 else f'{chg:+.2f}%'
        open_chg = r.get('auction_chg', 0)
        open_chg_str = '0.00%' if open_chg == 0 else f'{open_chg:+.2f}%'
        amt_str = f'{r["auction_amt"]/1e4:.0f}万'
        vol_r = r.get('vol_ratio', 1.0)
        vr_str = f'{vol_r:.1f}' if vol_r else '—'
        mv = r.get('float_mv', 0)
        mv_str = f'{mv/1e8:.0f}亿' if mv > 0 else '—'

        anomaly_map = anomaly_cache.get(r['code'], [])
        anomaly_str = ','.join(anomaly_map[:2]) if anomaly_map else '—'

        fv = r.get('flat_vol')
        fv_str = f'{fv:.2f}%' if fv is not None else '—'

        score = r['score']

        if mode == 'am':
            lines.append(
                f'| {i} | {r["code"]} | {r["name"]} | {fv_str} | {chg_str} | {open_chg_str} | {amt_str} | {vr_str} | {mv_str} | {anomaly_str} | {score:.0f} |'
            )
        else:
            total_chg = r.get('total_chg', 0)
            pct_str = f'{total_chg:+.2f}%'
            lines.append(
                f'| {i} | {r["code"]} | {r["name"]} | {fv_str} | {chg_str} | {open_chg_str} | {amt_str} | {pct_str} | {vr_str} | {anomaly_str} | {score:.0f} |'
            )

    lines.append('')
    if mode == 'am':
        lines.append('> 📌 评分维度: 平线波动率 + 竞价成交 + 量比 + 异动识别 + 流通市值修正')
        lines.append('> 📌 平线波动≤0.7% | 9:20-9:23竞价涨幅-3%~+3% | 开盘涨幅-1%~+1%')
        lines.append('> 📌 竞价成交额≥1000万 或 ≤30万')
        lines.append('> 📌 异动类型: 竞价抢筹+25 | 急速上涨+20 | 大买单试盘+15')
        lines.append('> 📌 9:27 初筛 → 15:30 收盘验证确认起飞')
    else:
        lines.append('> 📊 全日涨幅≥3%且早盘确认=标准水上飞机')
        lines.append('> 🚀 涨停=完美起飞 | ⭐ 涨幅≥5%+竞价确认=强势起飞')

    lines.append('')
    return '\n'.join(lines), ranked

# ======================== PDF发送 ========================
# ======================== 通达信自定义板块写入 ========================
def _code_to_tdx(code: str) -> str:
    """将6位股票代码转为通达信.blk格式前缀+6位码"""
    if code.startswith('6') or code.startswith('688'):
        prefix = '1'
    elif code.startswith('8') or code.startswith('4') or code.startswith('920'):
        prefix = '2'
    else:
        prefix = '0'
    return prefix + code


def tdx_write_all(rows, blk_name='XLXSSFJ', group_name='小龙虾水上飞机'):
    """全部结果写入通达信自定义板块，只保留当日筛选结果（不再累积）"""
    tdx_block = "/mnt/d/GP/通达信金融终端(开心果交易版)V2026/T0002/blocknew"
    fp = f"{tdx_block}/{blk_name}.blk"

    # 只写入当日结果，不再保留历史
    codes = sorted(set(_code_to_tdx(r['code']) for r in rows))
    content = "\r\n" + "\r\n".join(codes) + "\r\n"
    with open(fp, "wb") as f:
        f.write(content.encode("ascii"))
    print(f"  [板块] 已写入: {fp} （当日{len(codes)}只）")
    print(f"  [板块] [{group_name}] 已更新，不再积累历史数据")


def send_pdf(report, ranked, mode):
    try:
        from fpdf import FPDF
        WK = os.path.expanduser('~/.openclaw/workspace')
        FONT = os.path.expanduser('~/.fonts/msyh.ttc')
        ds = date_str(now)
        if mode == 'am':
            pdf_name = f'水上飞机早盘_{ds}.pdf'
            title_pdf = '✈️ 水上飞机选股 v3.0 · 早盘'
            subject = f'✈️ 水上飞机早盘 {ds}'
        else:
            pdf_name = f'水上飞机收盘_{ds}.pdf'
            title_pdf = '✈️ 水上飞机选股 v3.0 · 收盘验证'
            subject = f'✈️ 水上飞机收盘 {ds}'

        pdf_path = os.path.join(WK, pdf_name)
        pdf = FPDF()
        pdf.add_page()
        pdf.add_font('CN', '', FONT)
        pdf.add_font('CN', 'B', FONT)
        # 标题
        pdf.set_font('CN', '', 14)
        pdf.set_text_color(200, 30, 30)
        pdf.cell(0, 10, title_pdf, align='C', new_x='LMARGIN', new_y='NEXT')
        pdf.set_font('CN', '', 8)
        pdf.set_text_color(100, 100, 100)
        pdf.cell(0, 6, date_label(now), align='C', new_x='LMARGIN', new_y='NEXT')
        pdf.cell(0, 5, '数据源: thsdk同花顺 + 竞价异动识别', align='C', new_x='LMARGIN', new_y='NEXT')
        pdf.set_draw_color(200, 30, 30)
        pdf.line(60, pdf.get_y(), 150, pdf.get_y())
        pdf.ln(4)
        # 正文
        pdf.set_font('CN', '', 7)
        pdf.set_text_color(30, 30, 30)
        for line in report.split('\n'):
            if not line.strip():
                pdf.ln(2)
                continue
            if line.startswith('**'):
                pdf.set_font('CN', '', 8)
                pdf.set_text_color(50, 50, 50)
                pdf.cell(0, 5, line.replace('**', '')[:100], new_x='LMARGIN', new_y='NEXT')
                pdf.set_font('CN', '', 7)
            elif line.startswith('| '):
                cells = [c.strip() for c in line.split('|') if c.strip()]
                pdf.cell(0, 4.5, '  '.join(cells[:8])[:130], new_x='LMARGIN', new_y='NEXT')
            elif line.startswith('>'):
                pdf.set_text_color(100, 100, 100)
                pdf.cell(0, 4.5, line.replace('>', '').strip()[:130], new_x='LMARGIN', new_y='NEXT')
                pdf.set_text_color(30, 30, 30)
            else:
                pdf.cell(0, 4.5, line[:130], new_x='LMARGIN', new_y='NEXT')
        # 尾部
        pdf.ln(3)
        pdf.set_draw_color(200, 200, 200)
        pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
        pdf.set_font('CN', '', 6)
        pdf.set_text_color(150, 150, 150)
        pdf.cell(0, 4, '数据: 东方财富 + 同花顺 | 不构成投资建议', new_x='LMARGIN', new_y='NEXT')
        pdf.output(pdf_path)
        # 邮件
        subprocess.run([
            'node', '-e',
            f'require("{WK}/node_modules/nodemailer").createTransport('
            f'{{host:"smtp.qq.com",port:465,secure:true,'
            f'auth:{{user:"1628354330@qq.com",pass:"hqfvuyenniwjdcgi"}}}}'
            f').sendMail({{from:"1628354330@qq.com",to:"1628354330@qq.com",'
            f'subject:"{subject}",text:"报告详见附件PDF。",'
            f'attachments:[{{path:"{pdf_path}"}}]}})'
        ], capture_output=True, timeout=15)
        print(f'MEDIA: {pdf_path}')
    except Exception as e:
        pass

# ======================== 全局异动缓存 ========================
anomaly_cache = {}

# ======================== 主流程 ========================
def run(mode='am'):
    if not is_trading_day(now):
        return f'⏭️ 今天 {date_label(now)} 是周末，不执行水上飞机选股'

    label = date_label(now)
    ds = date_str(now)
    wd = os.path.expanduser('~/.openclaw/workspace')

    # 输出目录
    output_dir = os.path.join(wd, 'mx_data', 'output')
    os.makedirs(output_dir, exist_ok=True)

    # ========== Step 1: 获取竞价异动数据 (thsdk) ==========
    global anomaly_cache
    try:
        anomaly_cache = src_thsdk_auction_anomaly()
        ac = len(anomaly_cache)
    except Exception:
        anomaly_cache = {}
        ac = 0

    # ========== Step 2: 主数据源 - 同花顺问财（竞价数据更权威） ==========
    df = src_thsdk_wencai(
        f'{ds[:4]}年{int(ds[4:6])}月{int(ds[6:8])}日集合竞价涨幅，集合竞价成交额，量比，换手率，流通市值，昨收盘价，非ST，A股'
    )

    if df is not None and len(df) > 0:
        rows = parse_thsdk_df(df, anomaly_cache)
        data_src = 'thsdk(同花顺)'
        # 平线度: 逐只拿9:20-9:23竞价价格序列，淘汰波动>0.7%
        rows = enrich_flatline(rows)
        # 统一评分
        for r in rows:
            r['score'], r['score_detail'] = score_stock(r, anomaly_cache)
        # 两头分组: 放量≥1000万 与 缩量≤30万 各取评分top100
        big = [r for r in rows if r['auction_amt'] >= 10000000]
        small = [r for r in rows if r['auction_amt'] <= 300000]
        big.sort(key=lambda x: x['score'], reverse=True)
        small.sort(key=lambda x: x['score'], reverse=True)
        rows = big[:100] + small[:100]
    else:
        # ========== Step 3: 降级备选 - mx-xuangu 妙想 ==========
        query = f'{label}集合竞价涨跌幅在-1%到1%之间集合竞价成交额大于1000万或集合竞价成交额小于30万的A股'
        csv_path = src_mx_xuangu(query, output_dir)
        if csv_path:
            rows = parse_mx_csv(csv_path, anomaly_cache)
            data_src = 'mx-xuangu(降级)'
        else:
            return '❌ 水上飞机选股：所有数据源均查询失败'

    if not rows:
        return f'✈️ **水上飞机选股 | {label}**\n\n今日无符合条件的股票\n\n> 数据源: {data_src} | 异动扫描: {ac}只有记录'

    # ========== Step 4: 生成报告 ==========
    result = generate_report(rows, mode)
    if result is None:
        return f'✈️ **水上飞机选股 | {label}**\n\n生成报告失败'

    report, ranked = result

    # 保存文本报告
    prefix = 'water_plane_morning' if mode == 'am' else 'water_plane_evening'
    report_path = os.path.join(output_dir, f'{prefix}_{ds}.txt')
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(report)

    # PDF + 邮件
    try:
        send_pdf(report, ranked, mode)
    except Exception:
        pass

    # ========== Step 5: 全部结果写入通达信自定义板块 ==========
    try:
        tdx_write_all(rows, blk_name='XLXSSFJ', group_name='小龙虾水上飞机')
    except Exception as e:
        print(f'  [板块写入失败] {e}', file=sys.stderr)

    return report

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='水上飞机选股 v2.0')
    parser.add_argument('--mode', choices=['am', 'pm'], default='am',
                       help='am=早盘初筛(09:27), pm=收盘验证(15:30)')
    args = parser.parse_args()
    result = run(args.mode)
    print(result)
