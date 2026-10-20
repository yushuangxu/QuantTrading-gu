"""Minute-bar quality sidecar. No network, indicators, cache writes or clock inference.

PyTDX 1.72 exposes labels, not a documented completion protocol. Its production
contract stays UNKNOWN. A verified contract below is an integration seam, NOT a
claim that PyTDX has start/end labels or that successors prove finality.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
import hashlib
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from bar_cache_merge import minute_frequency, minute_time, security_code, normalize_minute_bars

SHANGHAI = ZoneInfo('Asia/Shanghai')
SESSIONS = ((time(9, 30), time(11, 30)), (time(13), time(15)))


def aware(value):
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError('Quality observation times must be timezone-aware')
    return dt.astimezone(SHANGHAI)


def label_time(value):
    return datetime.fromisoformat(minute_time(value)).replace(tzinfo=SHANGHAI)


class TradingCalendar:
    """Explicit covered dates only. Missing/conflicting coverage is unknown."""
    def __init__(self, days=None):
        self.days = dict(days or {})
        if any(type(d) is not date or type(v) is not bool for d, v in self.days.items()):
            raise ValueError('Calendar requires date -> bool')

    @classmethod
    def from_cache(cls, directory):
        days, conflicts = {}, set()
        for path in sorted(Path(directory).glob('trade_cal_*.json')):
            match = re.fullmatch(r'trade_cal_(\d{8})_(\d{8})\.json', path.name)
            if not match:
                continue
            try:
                start, end = [datetime.strptime(s, '%Y%m%d').date() for s in match.groups()]
                if not 0 <= (end - start).days <= 3700:
                    continue
                raw = json.loads(path.read_text(encoding='utf-8'))['open_days']
                if not isinstance(raw, list):
                    continue
                opened = {datetime.strptime(s, '%Y%m%d').date() for s in raw}
                if not opened or any(not start <= d <= end for d in opened):
                    continue
            except (OSError, ValueError, TypeError, KeyError):
                continue
            for offset in range((end - start).days + 1):
                day = start + timedelta(days=offset)
                value = day in opened
                if day in days and days[day] != value:
                    conflicts.add(day)
                days[day] = value
        return cls({d: v for d, v in days.items() if d not in conflicts})

    def phase(self, now):
        now = aware(now)
        opened = self.days.get(now.date())
        if opened is None:
            return 'unknown'
        if not opened:
            return 'closed_day'
        t = now.time()
        if t < time(9, 30):
            return 'preopen'
        if time(11, 30) <= t < time(13):
            return 'lunch'
        if t >= time(15):
            return 'after_close'
        return 'trading'

    def active_seconds(self, start, end):
        start, end = aware(start), aware(end)
        if end < start:
            return None
        seconds = 0.
        for offset in range((end.date() - start.date()).days + 1):
            day = start.date() + timedelta(days=offset)
            if day not in self.days:
                return None
            if self.days[day]:
                for lo, hi in SESSIONS:
                    lo, hi = [datetime.combine(day, t, SHANGHAI) for t in (lo, hi)]
                    seconds += max(0., (min(end, hi) - max(start, lo)).total_seconds())
        return seconds


@dataclass(frozen=True)
class SourceContract:
    source: str = 'pytdx/get_security_bars'
    label: str = 'unknown'
    verification_reference: str | None = None
    successor_proves_closed: bool = False

    @property
    def verified(self):
        return self.label in {'start', 'end'} and bool(self.verification_reference)


PYTDX_CONTRACT = SourceContract()


def interval(bar_time, freq, calendar, contract):
    """Only an independently verified label contract can map a label to a slot."""
    if not contract.verified:
        return None
    label = label_time(bar_time)
    if calendar.days.get(label.date()) is not True:
        return None
    step = timedelta(minutes=int(minute_frequency(freq).removesuffix('MIN')))
    for lo, hi in SESSIONS:
        start, end = [datetime.combine(label.date(), t, SHANGHAI) for t in (lo, hi)]
        slot = label if contract.label == 'start' else label - step
        if start <= slot and slot + step <= end and (slot - start) % step == timedelta(0):
            return slot, slot + step
    return None


@dataclass(frozen=True)
class BarState:
    bar_time: str
    state: str
    reason: str
    confirmed_at: str | None = None
    fingerprint: str = ''
    revision: int = 1


def bar_state(bar_time, freq, *, observed_at, calendar, contract=PYTDX_CONTRACT,
              successor=None):
    """Elapsed wall time alone NEVER yields closed, even for verified labels."""
    now = aware(observed_at)
    label = minute_time(bar_time)
    bounds = interval(label, freq, calendar, contract)
    if bounds is None:
        return BarState(label, 'unknown', '时间标签/交易日/数据源收盘语义未经验证')
    start, end = bounds
    if start <= now < end:
        return BarState(label, 'forming', '已验证时间契约：当前处于该周期区间')
    if successor is not None and contract.successor_proves_closed and end <= now:
        following = interval(successor, freq, calendar, contract)
        if following and end <= following[0] <= now:
            # Confirmation is known when this batch was observed, not at a
            # guessed past close time. Cross-day/lunch successors are allowed
            # ONLY under the explicitly verified source guarantee.
            return BarState(label, 'closed', '已验证来源保证后继周期出现代表前根完成', now.isoformat())
    return BarState(label, 'unknown', '没有来源完成证据；时间经过不等于数据已最终完成')


def fingerprint(row):
    values = [minute_time(row['time'])] + [float(row[k]) for k in ('open', 'high', 'low', 'close', 'vol', 'amount')]
    return hashlib.sha256(json.dumps(values, allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class QualitySnapshot:
    instrument: str
    timeframe: str
    observed_at: str
    latest_bar_time: str | None
    last_success_at: str | None
    last_request_at: str | None
    source_status: str
    freshness: str
    is_stale: bool | None
    last_error: str | None
    last_error_at: str | None
    phase: str
    reason: str
    bars: tuple[BarState, ...]
    # Independent of the legacy completion-contract freshness field. These
    # fields authorize candidate OBSERVATION only, never closure/confirmation.
    observation_freshness: str = 'unknown'
    observation_reason: str = '尚无连续有效观察依据'
    last_progress_at: str | None = None
    unresolved_conflict: bool = False

    def to_dict(self):
        return asdict(self)


class MarketDataState:
    """Owned by fetcher's lock. Append-only in-memory revision observations.

    Transport success, returned label progress and same-bar value changes are
    independent. Repeated identical replies do not reset last_change_at.
    """
    def __init__(self, *, calendar=None, contract=PYTDX_CONTRACT,
                 request_timeout_seconds=180, bar_grace_seconds=120):
        if request_timeout_seconds <= 0 or bar_grace_seconds < 0:
            raise ValueError('Invalid quality timeout')
        self.calendar = calendar or TradingCalendar()
        self.contract = contract
        self.request_timeout = request_timeout_seconds
        self.bar_grace = bar_grace_seconds
        self._series = {}

    def _entry(self, code, freq):
        key = security_code(code), minute_frequency(freq)
        return self._series.setdefault(key, dict(bars={}, history=(), latest=None,
                                                last_success=None, last_request=None,
                                                last_change=None, source='not_requested',
                                                error=None, error_at=None, conflict=False,
                                                live_sample=None, progress_at=None))

    def request(self, code, freq, now):
        self._entry(code, freq)['last_request'] = aware(now)

    def failure(self, code, freq, now, error, status='error'):
        if status not in {'error', 'empty', 'disconnected'}:
            raise ValueError('Invalid source failure status')
        entry = self._entry(code, freq)
        entry.update(source=status, error=str(error), error_at=aware(now))
        # Recovery needs new live progress; do not inherit pre-outage proof.
        entry.update(live_sample=None, progress_at=None)

    def observe(self, code, freq, frame, now, *, cache_only=False):
        now = aware(now)
        try:
            frame = normalize_minute_bars(frame, ts_code=code, freq=freq)
        except ValueError as exc:
            self.failure(code, freq, now, str(exc))
            raise
        if frame.empty:
            self.failure(code, freq, now, 'Empty/filtered response', 'empty')
            return
        entry = self._entry(code, freq)
        states = dict(entry['bars'])
        additions = []
        labels = frame.time.tolist()
        for i, row in enumerate(frame.to_dict('records')):
            label, digest = row['time'], fingerprint(row)
            old = states.get(label)
            changed = old is not None and digest != old.fingerprint
            revised = (old.revision + int(changed)) if old else 1
            state = bar_state(label, freq, observed_at=now, calendar=self.calendar,
                              contract=self.contract,
                              successor=labels[i + 1] if i + 1 < len(labels) else None)
            if cache_only:
                state = BarState(label, 'unknown', '磁盘缓存没有原始收盘/获取时点证据')
            if changed and old.state == 'closed':
                entry['conflict'] = True
            if entry['conflict']:
                state = BarState(label, 'unknown', '已完成输入发生来源修订，待显式复核')
            confirmed = old.confirmed_at if old and old.fingerprint == digest and old.state == 'closed' else state.confirmed_at
            if old and old.state == 'closed' and old.fingerprint == digest and not entry['conflict']:
                state = old
            else:
                state = BarState(label, state.state, state.reason, confirmed, digest, revised)
            if old != state:
                additions.append((now.isoformat(), state))
            states[label] = state
        latest = max(states)
        old_latest = entry['bars'].get(latest)
        if old_latest is None or old_latest.fingerprint != states[latest].fingerprint:
            entry['last_change'] = now
        entry.update(bars=states, history=entry['history'] + tuple(additions), latest=latest)
        if cache_only:
            entry['source'] = 'cache_only'
        else:
            current = (labels[-1], fingerprint(frame.iloc[-1]), now)
            previous = entry['live_sample']
            # A cache load / first reply is not evidence of live progression.
            # Only a later valid live observation of this latest series counts.
            same_session = previous and any(
                datetime.combine(now.date(), lo, SHANGHAI) <= previous[2] < now
                < datetime.combine(now.date(), hi, SHANGHAI) for lo, hi in SESSIONS)
            if (previous and same_session
                    and (now - previous[2]).total_seconds() <= self.request_timeout
                    and current[0] >= previous[0] and current[:2] != previous[:2]
                    and current[0] == latest):
                entry['progress_at'] = now
            entry['live_sample'] = current
            entry.update(last_success=now, source='ok')

    def history(self, code, freq):
        return self._entry(code, freq)['history']

    def snapshot(self, code, freq, now):
        now = aware(now)
        entry = self._entry(code, freq)
        phase = self.calendar.phase(now)
        fresh, reason = 'unknown', '来源时间语义、交易日历或取数证据不完整'
        label = label_time(entry['latest']) if entry['latest'] else None
        bounds = interval(entry['latest'], freq, self.calendar, self.contract) if label else None
        # Verified end-labelled forming bars can carry a future end label. That
        # is not evidence of future data, nor permission to accept arbitrary
        # future labels from an unverified source.
        label_anchor = bounds[0] if bounds and bounds[0] <= now < bounds[1] else label
        ages = [self.calendar.active_seconds(t, now) if t else None
                for t in (entry['last_success'], entry['last_change'], label_anchor)]
        timeout = int(minute_frequency(freq).removesuffix('MIN')) * 60 + self.bar_grace
        if entry['source'] in {'error', 'empty', 'disconnected'}:
            fresh, reason = 'unavailable', '最近行情请求失败；旧数据保留但不是正常实时行情'
        elif entry['source'] == 'not_requested' and label is None:
            fresh, reason = 'unavailable', '尚未取得行情'
        elif entry['conflict']:
            fresh, reason = 'unknown', '已完成输入被修订，需复核来源'
        elif any(age is not None and age > limit
                 for age, limit in zip(ages, (self.request_timeout, timeout, timeout))):
            fresh, reason = 'stale', '交易时间内请求、末根时间或数据进展已超过容忍窗口'
        elif entry['source'] == 'ok' and all(age is not None for age in ages):
            bounds = interval(entry['latest'], freq, self.calendar, self.contract)
            if self.contract.verified and bounds and bounds[0] <= now:
                fresh, reason = 'fresh', '来源时间契约已验证，成功取数及行情进展在容忍窗口内'
        # A new request or an unchanged success cannot erase the latest error.
        iso = lambda t: t.isoformat() if t else None
        states = []
        for k in sorted(entry['bars']):
            state = entry['bars'][k]
            if state.state == 'forming':
                bounds = interval(k, freq, self.calendar, self.contract)
                if not bounds or not bounds[0] <= now < bounds[1]:
                    state = BarState(k, 'unknown', '形成区间已过但尚无来源完成证据',
                                     fingerprint=state.fingerprint, revision=state.revision)
            states.append(state)
        observation, observation_reason = self._observation_quality(entry, freq, now, fresh)
        return QualitySnapshot(security_code(code), minute_frequency(freq), now.isoformat(),
                               entry['latest'], iso(entry['last_success']), iso(entry['last_request']),
                               entry['source'], fresh, True if fresh == 'stale' else False if fresh == 'fresh' else None,
                               entry['error'], iso(entry['error_at']), phase, reason,
                               tuple(states), observation, observation_reason,
                               iso(entry['progress_at']), entry['conflict'])

    def _observation_quality(self, entry, freq, now, legacy_freshness):
        """Time/progress plausibility, NOT a choice of TDX start/end semantics.

        Require two ordered live replies with progress and recent request,
        success and progress in THIS continuous session. A plausible future end
        label is bounded to one period in the same session; it never closes a bar.
        """
        if entry['source'] in {'error', 'empty', 'disconnected'}:
            return 'unavailable', '最近获取失败或无有效返回'
        if entry['conflict']:
            return 'unknown', '已完成输入存在未解决的修订冲突'
        if legacy_freshness == 'stale':
            return 'stale', '已确定过期，不允许普通候选提醒'
        if self.calendar.phase(now) != 'trading' or entry['source'] != 'ok':
            return 'unknown', '非明确交易时段或尚无有效实时获取'
        request, success, progress = (entry[k] for k in ('last_request', 'last_success', 'progress_at'))
        if not all(t is not None for t in (request, success, progress)):
            return 'unknown', '缺少连续有效取数的数据进展依据，单次成功不放行'
        if not request <= success <= now or progress > success:
            return 'unknown', '请求、成功、进展与观察时点不一致'
        step = timedelta(minutes=int(minute_frequency(freq).removesuffix('MIN')))
        label = label_time(entry['latest'])
        for lo, hi in SESSIONS:
            lo, hi = [datetime.combine(now.date(), t, SHANGHAI) for t in (lo, hi)]
            if lo <= now < hi:
                if not all(lo <= t <= now for t in (request, success, progress)):
                    return 'unknown', '尚无本交易日当前连续时段的数据进展'
                if not lo <= label <= hi or (label - lo) % step != timedelta(0):
                    return 'unknown', '末根标签不属于当前时段的合理周期网格'
                if label > now + step:
                    return 'unknown', '未来标签超过一个周期的观察容忍范围'
                ages = ((now - request).total_seconds(), (now - success).total_seconds(),
                        (now - progress).total_seconds(), max(0., (now - label).total_seconds()))
                limits = (self.request_timeout, self.request_timeout,
                          step.total_seconds() + self.bar_grace, step.total_seconds() + self.bar_grace)
                if any(a > b for a, b in zip(ages, limits)):
                    return 'stale', '请求、获取、实际数据进展或标签时效超限'
                return 'fresh', '本时段连续有效获取且末根实际推进/修订；仅支持候选观察，不证明收盘'
        return 'unknown', '无法确定当前交易时段'
