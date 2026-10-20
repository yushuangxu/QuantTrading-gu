"""Pure, validated merge for a single explicitly identified TDX minute series.

No downloads, writes, retention limits, period inference or bar-closure inference.
Nonempty inputs must identify their period via attrs['freq'] or a 'freq' column.
Legacy CSVs are explicitly bound to their requested filename period by the caller.
Time labels are naive TDX market-local labels, serialized to second precision;
nonzero seconds, subsecond data and timezone-bearing inputs are not rounded.
"""
from __future__ import annotations

from datetime import datetime
import math
import re

import pandas as pd

BAR_COLUMNS = ['ts_code', 'time', 'open', 'high', 'low', 'close', 'vol', 'amount']
NUMERIC_COLUMNS = BAR_COLUMNS[2:]
MINUTE_FREQUENCIES = frozenset({'1MIN', '5MIN', '15MIN', '30MIN', '60MIN'})


class BarCacheValidationError(ValueError):
    """The entire merge is rejected; no existing history should be overwritten."""


def security_code(value) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{6}\.(SH|SZ|BJ)', value.upper()):
        raise BarCacheValidationError(f'Unrecognized security code: {value!r}; explicit exchange required')
    return value.upper()


def minute_frequency(value) -> str:
    if not isinstance(value, str) or value.upper() not in MINUTE_FREQUENCIES:
        raise BarCacheValidationError(f'Unsupported minute frequency: {value!r}')
    return value.upper()


def minute_time(value) -> str:
    if isinstance(value, str):
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}(:\d{2})?', value):
            raise BarCacheValidationError(f'Unrecognized minute time: {value!r}')
        try:
            dt = datetime.fromisoformat(value)
        except ValueError as exc:
            raise BarCacheValidationError(f'Invalid minute time: {value!r}') from exc
    elif isinstance(value, datetime) and not pd.isna(value):
        dt = value
    else:
        raise BarCacheValidationError(f'Unrecognized minute time: {value!r}')
    if dt.tzinfo is not None or dt.second != 0 or dt.microsecond != 0 or getattr(dt, 'nanosecond', 0) != 0:
        raise BarCacheValidationError(f'Expected naive, minute-aligned TDX label: {value!r}')
    return dt.strftime('%Y-%m-%d %H:%M:%S')


def normalize_minute_bars(frame: pd.DataFrame, *, ts_code: str, freq: str,
                          declared_freq: str | None = None) -> pd.DataFrame:
    """Validate and copy a batch; reject ambiguous conflicting duplicates.

    declared_freq is an explicit producer/path assertion, not inference from
    time spacing. Any actual attrs/column metadata must agree with it and freq.
    Optional freq column is represented in output attrs to retain the eight-column
    CSV schema. Unknown additional columns are rejected, not silently dropped.
    """
    code, frequency = security_code(ts_code), minute_frequency(freq)
    if not isinstance(frame, pd.DataFrame):
        raise BarCacheValidationError('Minute bars must be a DataFrame')
    if not frame.columns.is_unique:
        raise BarCacheValidationError('Duplicate column names are ambiguous')
    if declared_freq is not None and minute_frequency(declared_freq) != frequency:
        raise BarCacheValidationError('Declared frequency differs from target series')
    if 'freq' in frame.attrs and minute_frequency(frame.attrs['freq']) != frequency:
        raise BarCacheValidationError('Batch frequency differs from target series')
    if 'ts_code' in frame.attrs and security_code(frame.attrs['ts_code']) != code:
        raise BarCacheValidationError('Batch security differs from target series')
    if len(frame) == 0 and len(frame.columns) == 0:
        result = pd.DataFrame(columns=BAR_COLUMNS)
        result.attrs = {'ts_code': code, 'freq': frequency}
        return result
    missing = set(BAR_COLUMNS) - set(frame.columns)
    extra = set(frame.columns) - set(BAR_COLUMNS) - {'freq'}
    if missing or extra:
        raise BarCacheValidationError(f'Invalid bar columns: missing={sorted(missing)}, extra={sorted(extra)}')
    if len(frame) and 'freq' not in frame.attrs and 'freq' not in frame.columns and declared_freq is None:
        raise BarCacheValidationError('Batch has no explicit frequency identity')
    if 'freq' in frame.columns:
        if any(minute_frequency(v) != frequency for v in frame['freq']):
            raise BarCacheValidationError('Rows contain a different frequency')
    result = frame[BAR_COLUMNS].copy(deep=True)
    codes = result['ts_code'].map(security_code)
    if not codes.eq(code).all():
        raise BarCacheValidationError('Rows contain a different security')
    result['ts_code'] = codes
    result['time'] = result['time'].map(minute_time)
    for column in NUMERIC_COLUMNS:
        # Reject missing, booleans and nonfinite values before any deduplication.
        if result[column].map(lambda v: isinstance(v, bool)).any():
            raise BarCacheValidationError(f'Boolean is not numeric OHLCV: {column}')
        try:
            values = pd.to_numeric(result[column], errors='raise')
            if pd.api.types.is_bool_dtype(values.dtype):
                raise ValueError('boolean numeric column')
            if not values.map(lambda v: math.isfinite(float(v))).all():
                raise ValueError('nonfinite numeric value')
        except (ValueError, TypeError, OverflowError) as exc:
            raise BarCacheValidationError(f'Invalid numeric bar field: {column}') from exc
        result[column] = values
    if len(result):
        prices = result[['open', 'high', 'low', 'close']]
        if (prices <= 0).any().any() or (result[['vol', 'amount']] < 0).any().any():
            raise BarCacheValidationError('Prices must be positive; volume/amount must be nonnegative')
        if (result.high < prices.max(axis=1)).any() or (result.low > prices.min(axis=1)).any():
            raise BarCacheValidationError('Inconsistent OHLC high/low range')
    # Identical duplicate labels can be collapsed. Different values in the SAME
    # batch have no reliable revision sequence; do not guess by row order.
    result = result.drop_duplicates()
    if result.duplicated(['ts_code', 'time']).any():
        raise BarCacheValidationError('Conflicting duplicate bars within one batch; revision order unknown')
    result = result.sort_values('time', kind='stable').reset_index(drop=True)
    result.attrs = {'ts_code': code, 'freq': frequency}
    return result


def merge_minute_bars(history: pd.DataFrame, incoming: pd.DataFrame, *, ts_code: str, freq: str) -> pd.DataFrame:
    """Keep the full time union; a new batch replaces an old same-time whole row.

    Both inputs are validated in full before producing any replacement history.
    No field-wise fallback from partial rows and no rolling history truncation.
    """
    old = normalize_minute_bars(history, ts_code=ts_code, freq=freq)
    new = normalize_minute_bars(incoming, ts_code=ts_code, freq=freq)
    nonempty = [batch for batch in [old, new] if len(batch)]
    if not nonempty:
        return old
    result = pd.concat(nonempty, ignore_index=True)
    result = result.drop_duplicates(['ts_code', 'time'], keep='last')
    result = result.sort_values('time', kind='stable').reset_index(drop=True)
    result.attrs = {'ts_code': security_code(ts_code), 'freq': minute_frequency(freq)}
    return result
