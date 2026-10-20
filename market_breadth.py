"""Pure full-A snapshot validation/statistics. No network, disk or current clock.

Counts describe the supplied, deduplicated eligible sample. Provider pagination
completeness is NOT an independently verified all-A universe coverage measure.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, time
from decimal import Decimal
import math
import re

from market_data_state import aware

SCOPE = ('SH', 'SZ', 'BJ')
PATTERNS = {'SH': r'(600|601|603|605|688)\d{3}',
            'SZ': r'(000|001|002|003|300|301)\d{3}', 'BJ': r'920\d{3}'}
# Exact identity evidence: SZSE A-share list captured 2026-10-19, archived at
# docs/phases/P1-02A-1_evidence/szse_a.json (A股代码=302132, 中航成飞).
# This is NOT authorization for the entire 302xxx range or a complete universe.
VERIFIED_EXACT_A_CODES = {'302132': 'SZ'}
BINS = (('lt_neg5', '< -5%'), ('neg5_neg3', '[-5%, -3%)'),
        ('neg3_zero', '[-3%, 0%)'), ('flat', '0%'), ('zero_3', '(0%, 3%]'),
        ('3_5', '(3%, 5%]'), ('gt5', '> 5%'))
MISSING = (None, '', '-', '--')


class BreadthError(ValueError):
    def __init__(self, message, details=None):
        super().__init__(message)
        self.details = dict(details or {})


def a_share_exchange(code):
    """Central code guard for statistics and adapters, not a listing registry.

    Preserve existing prefix guards; add only officially evidenced exact codes.
    Source security type and source market still require separate validation.
    """
    if not isinstance(code, str) or not re.fullmatch(r'\d{6}', code):
        return None
    if code in VERIFIED_EXACT_A_CODES:
        return VERIFIED_EXACT_A_CODES[code]
    return next((ex for ex, pattern in PATTERNS.items() if re.fullmatch(pattern, code)), None)


def security_identity(row):
    """Explicit source type + market + code guard; never infer omitted market.

    Current BSE 920 codes are supported. Old 43/83/87 codes need an official
    mapping, not a blanket prefix rule which would also admit NEEQ securities.
    ST and new listings are NOT excluded from an all-A universe.
    """
    if not isinstance(row, dict):
        raise BreadthError('行情记录不是对象')
    if row.get('security_type') != 'A_STOCK':
        return None
    code, exchange = row.get('code'), row.get('exchange')
    if exchange not in SCOPE or not isinstance(code, str) or not re.fullmatch(r'\d{6}', code):
        raise BreadthError('A股代码或交易所身份缺失/异常')
    actual = a_share_exchange(code)
    if actual is None:
        return None
    if actual != exchange:
        raise BreadthError(f'A股身份冲突: {code}.{exchange}')
    return code+'.'+exchange


def number_or_none(value):
    if value in MISSING:
        return None
    if isinstance(value, bool):
        raise BreadthError('布尔值不能作为行情数值')
    try:
        result = float(value)
    except (ValueError, TypeError) as exc:
        raise BreadthError('行情数值无法解析') from exc
    if not math.isfinite(result):
        raise BreadthError('行情包含NaN/Inf')
    return result


def normalize_quotes(rows, amount_unit):
    if amount_unit not in {'CNY', 'CNY_10K'}:
        raise BreadthError('成交额单位未核验，拒绝汇总')
    if not isinstance(rows, list) or not rows:
        raise BreadthError('快照没有股票记录')
    unique, filtered, duplicates = {}, 0, 0
    for raw in rows:
        identity = security_identity(raw)
        if identity is None:
            filtered += 1
            continue
        suspended = raw.get('suspended')
        if suspended is not None and type(suspended) is not bool:
            raise BreadthError('停牌状态必须为明确布尔或未知')
        values = {key: number_or_none(raw.get(key)) for key in
                  ('price', 'previous_close', 'change_pct', 'volume', 'amount')}
        if any(values[key] is not None and values[key] < 0 for key in ('price','previous_close','volume','amount')):
            raise BreadthError(f'{identity}存在负价格、成交量或成交额')
        if values['amount'] is not None and amount_unit == 'CNY_10K':
            values['amount'] = float(Decimal(str(values['amount']))*10000)
            if not math.isfinite(values['amount']):
                raise BreadthError('成交额换算溢出')
        stamp = raw.get('quote_at')
        try:
            stamp = aware(stamp).isoformat() if stamp is not None else None
        except (ValueError, TypeError) as exc:
            raise BreadthError(f'{identity}来源时间格式异常') from exc
        row = {'identity':identity, 'exchange':raw['exchange'], 'code':raw['code'],
               'name':str(raw.get('name') or raw['code']), **values,
               'suspended':suspended, 'quote_at':stamp}
        if identity in unique:
            if unique[identity] != row:
                raise BreadthError(f'{identity}存在重复修订冲突')
            duplicates += 1
        unique[identity] = row
    return list(unique.values()), {'raw_count':len(rows), 'non_a_filtered':filtered,
                                   'exact_duplicates':duplicates, 'a_share_count':len(unique)}


def distribution_index(change_pct):
    if change_pct < -5: return 0
    if change_pct < -3: return 1
    if change_pct < 0: return 2
    if change_pct == 0: return 3
    if change_pct <= 3: return 4
    if change_pct <= 5: return 5
    return 6


def coverage_report(identities, valid_identities, trading_day, universe=None):
    result = {'expected_scope':list(SCOPE), 'reference_count':None,
              'quote_coverage_pct':None, 'valid_change_coverage_pct':None,
              'missing_count':None, 'unexpected_count':None, 'verified_complete':False,
              'reason':'未接入同交易日、经独立核验的沪深北A股基准名单，覆盖率未知'}
    if universe is None:
        return result
    if not isinstance(universe, dict) or universe.get('trade_date') != trading_day:
        result['reason'] = '基准名单日期不匹配，不能作为本快照覆盖率分母'
        return result
    entries = universe.get('identities')
    if (not universe.get('source') or not universe.get('verification_reference')
            or not isinstance(entries, list) or not entries or len(set(entries)) != len(entries)):
        raise BreadthError('基准名单缺少独立来源、核验引用或唯一证券名单')
    exchanges = set()
    for identity in entries:
        if not isinstance(identity, str) or not re.fullmatch(r'\d{6}\.(SH|SZ|BJ)', identity):
            raise BreadthError('基准名单证券身份不合法')
        code, exchange = identity.split('.')
        if security_identity({'code':code, 'exchange':exchange, 'security_type':'A_STOCK'}) != identity:
            raise BreadthError('基准名单包含非A股')
        exchanges.add(exchange)
    if exchanges != set(SCOPE):
        raise BreadthError('基准名单未覆盖沪深北三交易所')
    expected, observed = set(entries), set(identities)
    result.update(reference_count=len(expected), quote_coverage_pct=100*len(expected & observed)/len(expected),
                  valid_change_coverage_pct=100*len(expected & set(valid_identities))/len(expected),
                  missing_count=len(expected-observed), unexpected_count=len(observed-expected),
                  verified_complete=expected==observed, reference_source=universe['source'],
                  reason='独立基准名单逐证券对比；有效涨跌幅覆盖率与行情记录覆盖率分列')
    return result


def calculate_breadth(packet, calendar, *, universe=None, max_collection_seconds=20,
                      max_quote_skew_seconds=30, pct_tolerance=0.025):
    """Validate a SINGLE source collection window, then count mutually exclusive bins.

    No current time or trading-day guessing. Unknown/no-trade/suspension rows
    are not flat. Positive amounts can count even if the percentage is missing;
    amount coverage and percentage coverage therefore have separate denominators.
    """
    if not isinstance(packet, dict) or packet.get('schema') != 1:
        raise BreadthError('市场广度快照协议不符')
    if set(packet.get('scope', [])) != set(SCOPE) or packet.get('change_unit') != 'percent':
        raise BreadthError('非沪深北全A范围或涨跌幅百分比口径不符')
    if not packet.get('source') or not packet.get('source_url'):
        raise BreadthError('快照来源缺失')
    started, fetched = aware(packet['requested_at']), aware(packet['fetched_at'])
    span = (fetched-started).total_seconds()
    if span < 0 or span > max_collection_seconds:
        raise BreadthError('采集窗口倒置或过长，不能拼接为当前全市场快照')
    raw = packet.get('rows')
    total = packet.get('provider_total')
    if type(total) is not int or total <= 0 or not isinstance(raw,list) or len(raw) != total:
        raise BreadthError('接口分页不完整，不发布部分页统计',
                           {'provider_total':total, 'collected_rows':len(raw) if isinstance(raw,list) else 0})
    rows, quality = normalize_quotes(raw, packet.get('amount_unit'))
    if not rows:
        raise BreadthError('过滤后没有A股股票')
    by_exchange = dict(Counter(r['exchange'] for r in rows))
    if set(by_exchange) != set(SCOPE):
        raise BreadthError('快照缺少沪/深/北交易所样本，不能标为全A', {'by_exchange':by_exchange, **quality})
    excluded = Counter()
    valid, amount_count, amount_missing, amount_values, stamps = [], 0, 0, [], []
    for row in rows:
        stamp = aware(row['quote_at']) if row['quote_at'] else None
        if stamp and stamp > fetched:
            raise BreadthError('来源时间晚于获取时间，拒绝未来数据')
        if row['suspended'] is True:
            excluded['suspended'] += 1
            # Explicit halts may occur after trading. A dated positive amount
            # still belongs to that session; old/missing counters cannot be
            # silently converted to a current-session zero.
            if row['amount'] == 0:
                amount_count += 1
            elif (row['amount'] is not None and stamp is not None
                    and stamp.date()==fetched.date()
                    and calendar.days.get(stamp.date()) is True and stamp.time()>=time(9,30)):
                stamps.append(stamp);amount_count += 1
                amount_values.append(Decimal(str(row['amount'])))
            else:
                amount_missing += 1
            continue
        volume, amount = row['volume'], row['amount']
        if volume == 0 and amount == 0:
            excluded['no_trade'] += 1
            amount_count += 1  # known zero amount, NOT a flat stock
            continue
        if stamp is None:
            raise BreadthError('非明确停牌/零成交记录缺少来源日期时间，不能用请求日期代替')
        if calendar.days.get(stamp.date()) is not True:
            raise BreadthError('来源日期不是已知交易日或日历覆盖不足')
        if stamp.time() < time(9,30):
            raise BreadthError('来源为盘前更新时间，无法证明计数器属于当日交易')
        stamps.append(stamp)
        if amount is None:
            amount_missing += 1
        else:
            amount_count += 1
            amount_values.append(Decimal(str(amount)))
        # Exclusion reasons are deliberately mutually exclusive (priority below).
        if row['previous_close'] is None or row['previous_close'] <= 0:
            excluded['no_previous_close'] += 1
        elif row['change_pct'] is None:
            excluded['missing_change'] += 1
        elif row['price'] is None or row['price'] <= 0:
            excluded['invalid_price'] += 1
        elif volume is None or amount is None:
            excluded['missing_activity'] += 1
        elif volume == 0 or amount == 0:
            excluded['activity_conflict'] += 1
        elif abs((row['price']/row['previous_close']-1)*100-row['change_pct']) > pct_tolerance:
            excluded['change_conflict'] += 1
        else:
            valid.append(row)
    if not stamps:
        raise BreadthError('只有停牌/零成交记录，无法核验行情所属交易日', {'excluded':dict(excluded), **quality})
    dates = {stamp.date() for stamp in stamps}
    if len(dates) != 1:
        raise BreadthError('来源日期不一致，禁止混合交易日统计')
    oldest,newest = min(stamps),max(stamps)
    if (newest-oldest).total_seconds() > max_quote_skew_seconds:
        raise BreadthError('股票来源时间跨度过大，禁止混合不同时点行情')
    trading_day = next(iter(dates)).isoformat()
    bins = [0]*len(BINS)
    for row in valid:
        bins[distribution_index(row['change_pct'])] += 1
    down,flat,up = sum(bins[:3]),bins[3],sum(bins[4:])
    count = len(valid)
    assert up+down+flat == count == sum(bins)
    assert count+sum(excluded.values()) == len(rows)
    coverage = coverage_report([r['identity'] for r in rows], [r['identity'] for r in valid], trading_day, universe)
    warnings = []
    if not coverage['verified_complete']:
        warnings.append(coverage['reason'])
    if excluded:
        warnings.append('停牌、无成交及无有效涨跌幅等记录已单列，不计为平盘')
    if amount_missing:
        warnings.append(f'{amount_missing}只股票缺少可归属本交易日的成交额，不能称为完整市场成交额')
    if quality['non_a_filtered']:
        warnings.append(f"剔除{quality['non_a_filtered']}条非A股/未支持证券身份；未当作股票计数")
    if not count:
        warnings.append('没有可用于涨跌统计的有效交易样本')
    bad_activity = any(excluded[key] for key in
                       ('invalid_price','missing_activity','activity_conflict','change_conflict','missing_change'))
    market_complete = coverage['verified_complete'] and amount_missing == 0 and count > 0 and not bad_activity
    observed_amount = float(sum(amount_values, Decimal(0)))
    if not math.isfinite(observed_amount):
        raise BreadthError('成交额汇总溢出')
    return {
        'trade_date':trading_day, 'quote_time_min':oldest.isoformat(), 'quote_time_max':newest.isoformat(),
        'completion_state':'unknown',  # endpoint has no final daily completion flag
        'snapshot_kind':'intraday' if fetched.date()==oldest.date() and fetched.time()<time(15) else 'unknown',
        'temporal_basis':'single_source_bounded_collection_not_atomic',
        'date_basis':packet.get('date_basis','explicit_quote_timestamp'),
        'requested_at':started.isoformat(), 'fetched_at':fetched.isoformat(),
        'source':packet['source'], 'source_url':packet['source_url'],
        'availability':'fresh' if market_complete else 'degraded',
        'coverage':coverage, 'quality':{**quality, 'by_exchange':by_exchange,
            'valid_change_count':count, 'excluded':dict(excluded), 'amount_count':amount_count,
            'amount_missing_count':amount_missing, 'provider_total':total,
            'provider_pagination_pct':100., 'collection_seconds':span},
        'stats':{'up':up, 'down':down, 'flat':flat, 'valid_count':count,
                 'advance_pct':100*up/count if count else None,
                 'up_down_ratio':up/down if down else None, 'up_down_text':f'{up} : {down}',
                 'observed_amount_cny':observed_amount,
                 'all_a_amount_cny':observed_amount if market_complete else None,
                 'amount_change_pct':None,
                 'amount_change_reason':'未接入可比的两日同口径完整快照，不估算成交额变化',
                 'up_gt3':bins[5]+bins[6], 'up_gt5':bins[6],
                 'down_lt_neg3':bins[0]+bins[1], 'down_lt_neg5':bins[0]},
        'direction_distribution':[{'key':key,'label':label,'count':value,
                                   'pct':100*value/count if count else None}
                                  for key,label,value in [('up','上涨',up),('flat','平盘',flat),('down','下跌',down)]],
        'change_distribution':[{'key':key, 'label':label, 'count':bins[i],
                                'pct':100*bins[i]/count if count else None}
                               for i,(key,label) in enumerate(BINS)],
        'warnings':warnings,
    }
