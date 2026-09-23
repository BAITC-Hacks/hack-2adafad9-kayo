"""Консервативный архив для ежедневного выпуска в 23:00 UTC.

Previous Runs — фиксированный возраст относительно valid time, а не единый run.
Для следующих суток берём day2, для вторых — day3. Номинальный reference time
каждого значения отстаёт от выпуска минимум на 24 часа. Доступность предполагает,
что задержка публикации не превышала этот запас: API не отдаёт исторические
publication timestamps. Мы не называем reference time точным init модели.

Источник: https://open-meteo.com/en/docs/previous-runs-api
"""
import pandas as pd

from . import weather as wx

ISSUE_HOUR_UTC = 23
PUBLICATION_BUFFER_HOURS = 24
ARCHIVE = ('2024-02-17', '2026-02-28')


def issue_times(frame: pd.DataFrame) -> pd.Series:
    """Дата выпуска для корзин следующих суток (24) и вторых суток (48)."""
    return frame.time.dt.normalize() - pd.to_timedelta(frame.lead_h, unit='h') + pd.Timedelta(hours=ISSUE_HOUR_UTC)


def load_day3(start: str, end: str, model: str) -> pd.DataFrame:
    path = wx.CACHE / f'prev_runs_day3_{start}_{end}_{model}.parquet'
    if path.exists():
        return pd.read_parquet(path)
    columns = [f'{variable}_previous_day3' for variable in wx.BASE]
    params = dict(wx.SITE, start_date=start, end_date=end, models=model,
                  hourly=','.join(columns), wind_speed_unit='ms', timezone='UTC')
    print(f'Загрузка day3: {model}, {start} — {end}', flush=True)
    payload = wx._get(params, tries=2)
    frame = pd.DataFrame(payload['hourly'])
    frame['time'] = pd.to_datetime(frame.time)
    absent = [column for column in columns if column not in frame or frame[column].notna().sum() == 0]
    if absent:
        raise ValueError(f'{model}: day3 недоступен для {absent}; подмена горизонта запрещена')
    expected = pd.date_range(start, pd.Timestamp(end) + pd.Timedelta(hours=23), freq='h')
    if not pd.DatetimeIndex(frame.time).equals(expected):
        raise ValueError(f'{model}: неполная временная сетка day3')
    wx.CACHE.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path, index=False)
    return frame


def _model_days(start: str, end: str, model: str, diagnostic: bool) -> pd.DataFrame:
    cached = wx.load(start, end, model)
    day3 = load_day3(start, end, model)
    periods = [(cached, '_previous_day2', 24, 48), (day3, '_previous_day3', 48, 72)]
    if diagnostic:
        periods.insert(0, (cached, '', 0, 0))
    parts = []
    for source, suffix, lead, weather_lead in periods:
        columns = {f'{variable}{suffix}': variable for variable in wx.BASE}
        part = source[['time'] + list(columns)].rename(columns=columns)
        part['lead_h'] = lead
        part['weather_lead_h'] = weather_lead
        part['weather_reference_time'] = part.time - pd.Timedelta(hours=weather_lead)
        part['weather_source'] = 'diagnostic_nowcast' if lead == 0 else 'previous_runs_conservative'
        parts.append(part)
    return pd.concat(parts, ignore_index=True).sort_values(['time', 'lead_h']).reset_index(drop=True)


def daily_ensemble(start: str, end: str, diagnostic: bool = False) -> pd.DataFrame:
    """Тот же интерфейс признаков, но архив day2/day3 для корзин 24/48.

    lead_h обозначает конец корзины времени до цели, не возраст погодного прогноза.
    Строки lead_h=0 предназначены только для отдельной диагностики утечки.
    """
    base = _model_days(start, end, wx.BASE_MODEL, diagnostic)
    for model, member in wx.MEMBERS.items():
        forecasts = base if model == wx.BASE_MODEL else _model_days(start, end, model, diagnostic)
        forecasts = forecasts[['time', 'lead_h'] + wx.MEMBER_VARS].rename(
            columns={variable: f'{variable}_{member}' for variable in wx.MEMBER_VARS})
        base = base.merge(forecasts, on=['time', 'lead_h'], how='left', validate='one_to_one')
    issued = base[base.lead_h > 0]
    reserve = issue_times(issued) - issued.weather_reference_time
    if (reserve < pd.Timedelta(hours=PUBLICATION_BUFFER_HOURS)).any():
        raise ValueError('Погода нарушает запас на публикацию до выпуска')
    return base


if __name__ == '__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    forecasts = daily_ensemble(*ARCHIVE, diagnostic=True)
    print(forecasts.groupby('lead_h').wind_speed_100m.agg(['size', 'count']).to_string())
