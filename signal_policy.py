"""Trust metadata for legacy signals; no indicators, data fetching or trade rules.

All input times are timezone-aware ISO-8601 (or aware datetime). observed_at is
when this observation was made, bar_time is the source bar's label (NOT an
inferred close time). confirmed_at is producer-reported: it is authoritative for
an event only when status=confirmed; on retrospective markers it describes the
extremum confirmation, never trade approval. Markers additionally carry
extremum_at and marker_appeared_at, independently.

Only P0-02 verified default-profile CROSS_GC/DC may become confirmed technical
facts. They are not trade approval. Producers attest revision/profile/closure;
this module cannot verify market data or completion by looking at the clock.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
from threading import RLock
from typing import Mapping
from zoneinfo import ZoneInfo

STRATEGY_REVISION = '7f7c7ae092d5db7c6cc0c08691eae780474b4b8b9c0fc27398081279b8b6cd35'
DEFAULT_PROFILE = 'KdjMacdStrategy/default/fixed-history'
VALIDATION_REFERENCE = 'P0-02:crosses_causal_and_nonvacuous+cross_equal_boundary_and_first_row'
INVALID_SIGNALS = frozenset({'BOTTOM_FISHING', 'TOP_ESCAPE', 'STRONG_BOTTOM', 'STRONG_TOP'})
REPAINT_SIGNALS = frozenset({'BUY_B', 'SELL_S', 'BOTTOM_DIV', 'TOP_DIV',
                             'INNER_BOTTOM_DIV', 'INNER_TOP_DIV', 'BACKSET'})
MOMENTUM_SIGNALS = frozenset({'BUY_JB', 'SELL_JS', 'WAVE_PEAK', 'WAVE_TROUGH', 'MACD_ZIG'})
DIVERGENCE_SIGNALS = frozenset({'TWO_ROUND_BOTTOM_DIV', 'TWO_ROUND_TOP_DIV',
                                'LINE_BOTTOM', 'LINE_TOP'})
CONFIRMABLE_FACTS = frozenset({'CROSS_GC', 'CROSS_DC'})


def _canonical(name: str) -> str:
    name = name.upper().removesuffix('_RAW')
    return {'BUY': 'BUY_B', 'SELL': 'SELL_S'}.get(name, name)


def _time(value) -> str | None:
    """Never guess a timezone or replace a missing time with now()."""
    try:
        dt = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        if dt.tzinfo is None or dt.utcoffset() is None:
            return None
        return dt.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


@dataclass(frozen=True)
class SignalRecord:
    signal_type: str
    source_strategy: str
    observed_at: str | None
    bar_time: str | None
    bar_closed: bool | None
    status: str
    reason: str
    can_use_for_trade_plan: bool
    active: bool
    instrument: str
    timeframe: str
    scope: str
    strategy_revision: str
    calculation_profile: str
    validation_reference: str | None
    record_kind: str
    confirmed_at: str | None
    extremum_at: str | None
    marker_appeared_at: str | None
    payload_json: str

    @property
    def key(self) -> tuple:
        # Stable identity of the source event, not of an observation/recalculation.
        return (self.instrument, self.timeframe, self.scope, self.source_strategy,
                self.strategy_revision, self.calculation_profile,
                _canonical(self.signal_type), self.bar_time)

    @property
    def payload(self) -> dict:
        # Returning a decoded copy prevents nested mutation of recorded evidence.
        return json.loads(self.payload_json)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping) -> SignalRecord:
        """Reclassify stored inputs, rejecting forged or inconsistent metadata."""
        data = dict(data)
        expected = classify_signal(**{k: v for k, v in data.items()
                                      if k not in {'status', 'reason', 'can_use_for_trade_plan', 'payload_json'}},
                                   payload=json.loads(data['payload_json']))
        if expected.to_dict() != data:
            raise ValueError('Stored record does not match signal policy')
        return expected


def classify_signal(signal_type: str, *, source_strategy: str = 'KdjMacdStrategy',
                    observed_at=None, bar_time=None, bar_closed=None, active: bool = True,
                    instrument: str = '', timeframe: str = '', scope: str = '',
                    strategy_revision: str = '', calculation_profile: str = '',
                    validation_reference: str | None = None, record_kind: str = 'observation',
                    confirmed_at=None, extremum_at=None, marker_appeared_at=None,
                    payload: Mapping | None = None) -> SignalRecord:
    """Pure classification; no type, time or user flag can approve a trade plan.

    record_kind must explicitly distinguish actual observations from recomputed
    historical markers. A closed bar or supplied confirmed_at cannot promote
    BACKSET/B/S, momentum candidates or area/line divergences.
    """
    if not isinstance(signal_type, str) or not signal_type.strip():
        raise ValueError('signal_type must be a nonempty string')
    if type(active) is not bool:
        raise TypeError('active must be a Python bool, not truthy text or NaN')
    if record_kind not in {'observation', 'historical_marker'}:
        raise ValueError('Unknown record_kind')
    closed = bar_closed if type(bar_closed) is bool else None
    observed, bar, confirmed, extreme, appeared = map(
        _time, [observed_at, bar_time, confirmed_at, extremum_at, marker_appeared_at])
    payload_json = json.dumps(dict(payload or {}), sort_keys=True, ensure_ascii=False, allow_nan=False)
    issues = []
    if observed is None:
        issues.append('缺少带时区的观察时间')
    if bar is None:
        issues.append('缺少带时区的K线时间')
    if closed is not True:
        issues.append('K线未完成或收盘状态未知')
    if confirmed is None:
        issues.append('缺少明确的确认时间')
    if observed and bar and datetime.fromisoformat(bar) > datetime.fromisoformat(observed):
        issues.append('K线时间晚于观察时间')
    if confirmed and (not observed or not bar or not
                      (datetime.fromisoformat(bar) <= datetime.fromisoformat(confirmed)
                       <= datetime.fromisoformat(observed))):
        issues.append('确认时间不在K线标签与观察时间之间')
    if extreme and appeared and datetime.fromisoformat(extreme) > datetime.fromisoformat(appeared):
        issues.append('极值时间晚于标记出现时间')
    if appeared and observed and datetime.fromisoformat(appeared) > datetime.fromisoformat(observed):
        issues.append('标记出现时间晚于观察时间')
    # Old journals without a sidecar keep their original producer-attestation
    # contract. New market-aware observations must also pass the quality gate.
    quality = (payload or {}).get('market_data')
    if quality is not None:
        if not isinstance(quality, dict) or quality.get('freshness') != 'fresh':
            issues.append('行情过期、不可用或新鲜度未知，不允许确认')
        elif quality.get('bar_state') != 'closed':
            issues.append('缺少可靠的K线完成证据')
    name = _canonical(signal_type)
    if name in INVALID_SIGNALS:
        status, reason = 'invalid', 'P0-02已证明条件互斥或强背离同根组合不可达；不可用于交易计划'
    elif record_kind == 'historical_marker':
        status, reason = 'retrospective', '历史重算/回填标记；极值时间不代表当时可交易或已确认'
        if not extreme or not appeared:
            issues.append('回顾标记的极值时间或首次出现时间不完整')
    elif name in REPAINT_SIGNALS:
        status, reason = 'candidate', '存在BACKSET历史回填/重绘风险；收盘及后续交叉均不自动确认该交易信号'
    elif name in MOMENTUM_SIGNALS:
        status, reason = 'candidate', '动量或运行中新极值候选；不代表已确认趋势反转'
    elif name in DIVERGENCE_SIGNALS:
        status, reason = 'candidate', '样本因果性通过不等于策略有效；面积/DIF线背离暂不升级为交易确认'
    elif name in CONFIRMABLE_FACTS:
        validated = (source_strategy == 'KdjMacdStrategy' and strategy_revision == STRATEGY_REVISION
                     and calculation_profile == DEFAULT_PROFILE and validation_reference == VALIDATION_REFERENCE)
        if active and validated and not issues and instrument and timeframe:
            status, reason = 'confirmed', '经P0-02因果验证的已完成K线交叉技术事件；不是买卖计划授权'
        else:
            status, reason = 'candidate', '交叉事件缺少完整收盘/时点/对应版本因果验证证据'
    else:
        status, reason = 'candidate', '指标快照或未登记信号；没有对应确认与交易计划授权'
    if not active:
        issues.append('本次观察原始条件为False；保留此前观察，不覆盖历史记录')
    if issues:
        reason += '；' + '；'.join(issues)
    return SignalRecord(signal_type, source_strategy, observed, bar, closed, status, reason, False,
                        active, instrument, timeframe, scope, strategy_revision, calculation_profile,
                        validation_reference, record_kind, confirmed, extreme, appeared, payload_json)


class ConfirmedEventConflict(ValueError):
    """A later recomputation must not silently amend an existing confirmed fact."""


def append_record(history: tuple[SignalRecord, ...], record: SignalRecord) -> tuple[SignalRecord, ...]:
    """Pure append. Cancellation is a new active=False observation, never deletion.

    Confirmed facts are immutable within their original identity. Explicit source
    corrections need a separately specified workflow, not overwriting this log.
    """
    SignalRecord.from_dict(record.to_dict())
    if record.observed_at is None or record.bar_time is None or not record.instrument or not record.timeframe:
        raise ValueError('Journal needs known observation/bar times, instrument and timeframe')
    same = [r for r in history if r.key == record.key and r.record_kind == record.record_kind]
    if same and datetime.fromisoformat(record.observed_at) < datetime.fromisoformat(same[-1].observed_at):
        raise ValueError('Backdated observation cannot replace a later observation')
    for old in same:
        if old.status == 'confirmed' and replace(record, observed_at=old.observed_at) != old:
            raise ConfirmedEventConflict('Confirmed event differs from later recomputation; explicit review required')
    return history + (record,)


class SignalJournal:
    """Small append-only JSONL adapter, separate live and retrospective files.

    Single-process/single-writer ownership; thread safe in that process. Invalid
    or truncated files raise on load; they are never silently reset or repaired.
    No files/directories are created until record() is called.
    """
    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self._lock = RLock()
        self._streams = {'observation': (), 'historical_marker': ()}
        for kind in self._streams:
            path = self._path(kind)
            if path.exists():
                history = ()
                for line in path.read_text(encoding='utf-8').splitlines():
                    record = SignalRecord.from_dict(json.loads(line))
                    if record.record_kind != kind:
                        raise ValueError('Observation and historical marker streams must be separate')
                    history = append_record(history, record)
                self._streams[kind] = history

    def _path(self, kind):
        return self.directory / ('observations.jsonl' if kind == 'observation' else 'historical_markers.jsonl')

    def records(self, kind='observation') -> tuple[SignalRecord, ...]:
        with self._lock:
            return self._streams[kind]

    def record(self, record: SignalRecord) -> None:
        with self._lock:
            kind = record.record_kind
            history = append_record(self._streams[kind], record)  # validate before touching disk
            self.directory.mkdir(parents=True, exist_ok=True)
            with self._path(kind).open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(record.to_dict(), ensure_ascii=False, allow_nan=False) + '\n')
                stream.flush()
                os.fsync(stream.fileno())
            self._streams[kind] = history


def classify_market_signal(signal_type: str, *, market_state, **kwargs) -> SignalRecord:
    """Quality-gated producer boundary, including bar/series/time identity.

    Use the atomic snapshot paired with the exact input, never a later fetch's
    metadata. Closure does not replace the existing causal/version/profile gate.
    No freshness check can authorize B/S or JB/JS or a trading plan.
    """
    from market_data_state import QualitySnapshot, label_time
    payload = dict(kwargs.pop('payload', {}) or {})
    payload['market_data'] = {'freshness': 'unknown', 'bar_state': 'unknown'}
    kwargs['bar_closed'] = None
    kwargs['confirmed_at'] = None
    if isinstance(market_state, QualitySnapshot):
        observed = _time(kwargs.get('observed_at'))
        bar = _time(kwargs.get('bar_time'))
        matching = (market_state.instrument == kwargs.get('instrument')
                    and market_state.timeframe == kwargs.get('timeframe')
                    and observed is not None
                    and _time(market_state.observed_at) == observed)
        target = next((b for b in market_state.bars if _time(label_time(b.bar_time)) == bar), None)
        payload['market_data'].update(source_status=market_state.source_status,
                                      latest_bar_time=market_state.latest_bar_time,
                                      last_success_at=market_state.last_success_at,
                                      last_request_at=market_state.last_request_at,
                                      last_error=market_state.last_error, reason=market_state.reason)
        if matching and target is not None:
            payload['market_data'].update(freshness=market_state.freshness,
                                          observation_freshness=market_state.observation_freshness,
                                          observation_reason=market_state.observation_reason,
                                          last_progress_at=market_state.last_progress_at,
                                          unresolved_conflict=market_state.unresolved_conflict,
                                          bar_state=target.state, fingerprint=target.fingerprint,
                                          revision=target.revision, closure_reason=target.reason)
            kwargs['bar_closed'] = {'closed': True, 'forming': False}.get(target.state)
            if market_state.freshness == 'fresh' and target.state == 'closed':
                kwargs['confirmed_at'] = target.confirmed_at
    return classify_signal(signal_type, payload=payload, **kwargs)


def candidate_notice_allowed(record: SignalRecord, market_state) -> bool:
    """Notification permission is separate from signal confirmation/plan rights.

    A real latest observation and consistent sidecar are mandatory. Legacy
    verified-contract fresh snapshots remain accepted; closure-unknown PyTDX
    uses independent continuous-observation evidence, never closure inference.
    """
    from market_data_state import QualitySnapshot, label_time
    if not isinstance(market_state, QualitySnapshot):
        return False
    quality = record.payload.get('market_data', {})
    return (record.status == 'candidate' and record.active and record.record_kind == 'observation'
            and not record.can_use_for_trade_plan
            and record.instrument == market_state.instrument and record.timeframe == market_state.timeframe
            and record.observed_at == _time(market_state.observed_at)
            and market_state.latest_bar_time is not None
            and record.bar_time == _time(label_time(market_state.latest_bar_time))
            and market_state.source_status == 'ok' and not market_state.unresolved_conflict
            and market_state.freshness not in {'stale', 'unavailable'}
            and market_state.observation_freshness not in {'stale', 'unavailable'}
            and quality.get('source_status') == 'ok'
            and quality.get('freshness') == market_state.freshness
            and (market_state.freshness == 'fresh'
                 or (market_state.observation_freshness == 'fresh'
                     and quality.get('observation_freshness') == 'fresh')))


def record_latest_signal(journal: SignalJournal, signal, *, observed_at: datetime,
                         timeframe: str, scope: str, strategy_revision: str = '',
                         source_timezone='Asia/Shanghai', market_state=None) -> tuple[SignalRecord, ...]:
    """Minimal adapter for SignalResult; completion only comes from its sidecar.

    Native TDX bar labels are explicitly interpreted in Asia/Shanghai only here.
    Persist active candidates and same-bar withdrawals. A later bar does not
    retroactively retract earlier events; no full-history strategy is re-run.
    """
    bar = datetime.fromisoformat(signal.bar_time)
    if bar.tzinfo is None:
        bar = bar.replace(tzinfo=ZoneInfo(source_timezone))
    attrs = {'BUY_B': 'buy_b', 'SELL_S': 'sell_s', 'BOTTOM_FISHING': 'bottom_fishing',
             'TOP_ESCAPE': 'top_escape', 'BUY_JB': 'buy_jb', 'SELL_JS': 'sell_js'}
    # SignalResult may contain warm-up NaN; JSON null retains missingness, not a numeric replacement.
    payload = {name: (float(getattr(signal, name)) if math.isfinite(float(getattr(signal, name))) else None)
               for name in ['close', 'j', 'dif', 'dea', 'macd', 'cci']}
    written = []
    previous = {r.key: r for r in journal.records()}
    for name, attr in attrs.items():
        record = classify_market_signal(name, market_state=market_state, observed_at=observed_at, bar_time=bar,
                                 active=bool(getattr(signal, attr)), instrument=signal.ts_code,
                                 timeframe=timeframe, scope=scope, strategy_revision=strategy_revision,
                                 calculation_profile=DEFAULT_PROFILE, payload=payload)
        prior = previous.get(record.key)
        if record.active or (prior is not None and prior.active):
            journal.record(record)
            written.append(record)
    return tuple(written)


def format_signal_notice(signal_type: str) -> str:
    """Shared conservative wording for existing message bodies; never a buy label."""
    record = classify_signal(signal_type)
    if record.status == 'invalid':
        return '[invalid 不可用：条件互斥，不可用于交易计划]'
    if _canonical(signal_type) in MOMENTUM_SIGNALS:
        return '[candidate 动量候选：K线未确认，非已确认反转，不可用于交易计划]'
    return '[candidate 候选：存在重绘风险，收盘状态未知，不可用于交易计划]'
