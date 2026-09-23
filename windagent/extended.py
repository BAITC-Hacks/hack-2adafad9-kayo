"""Отдельный эксперимент расширенных признаков: python -m windagent.extended.

Пакет v1 фиксируется целиком до замера. Алгоритм, параметры, кривая, окна и агент
совпадают с базой. Эксперимент не меняет официальные CSV и метрики.
"""
from contextlib import contextmanager
import json
from pathlib import Path
import time

import numpy as np
import pandas as pd

from . import agent, backtest as bt, model as mdl, weather as wx

BASE_COLUMNS = tuple(mdl.FEATURE_COLUMNS)
BASE_FEATURES = mdl.features
EXTRA_COLUMNS = (
    'wind_u', 'wind_v', 'dir_sin2', 'dir_cos2', 'hour_sin', 'hour_cos',
    'season_sin', 'season_cos', 'wind_ramp', 'direction_turn', 'forecast_variability',
    'pressure_ramp', 'icon_minus_ensemble', 'ensemble_range', 'temperature_spread',
    'pressure_spread', 'wind_shear_interaction', 'spread_power_interaction',
)


def features(weather: pd.DataFrame, turbine: str) -> pd.DataFrame:
    """Все производные используют только прогноз того же горизонта и суток."""
    frame = BASE_FEATURES(weather, turbine)
    frame['wind_u'] = -frame.ws_norm * frame.dir_sin
    frame['wind_v'] = -frame.ws_norm * frame.dir_cos
    radians = np.radians(frame.wind_direction_100m)
    frame['dir_sin2'] = np.sin(2 * radians)
    frame['dir_cos2'] = np.cos(2 * radians)
    frame['hour_sin'] = np.sin(2 * np.pi * frame.hour / 24)
    frame['hour_cos'] = np.cos(2 * np.pi * frame.hour / 24)
    frame['season_sin'] = np.sin(2 * np.pi * (frame.time.dt.dayofyear - 1) / 365.25)
    frame['season_cos'] = np.cos(2 * np.pi * (frame.time.dt.dayofyear - 1) / 365.25)
    ordered = frame.sort_values(['lead_h', 'time'])
    blocks = ordered.groupby([ordered.lead_h, ordered.time.dt.normalize()], sort=False)
    frame['wind_ramp'] = blocks.ws_norm.diff().reindex(frame.index).fillna(0)
    turn = blocks.wind_direction_100m.diff().reindex(frame.index)
    frame['direction_turn'] = ((turn + 180) % 360 - 180).fillna(0) / 180
    frame['pressure_ramp'] = blocks.surface_pressure.diff().reindex(frame.index).fillna(0)
    frame['_wind_squared'] = frame.ws_norm ** 2
    # Разброс почасового прогноза — не измеренная турбулентность и не настоящие порывы.
    variance = mdl.block_mean(frame, '_wind_squared', 3) - mdl.block_mean(frame, 'ws_norm', 3) ** 2
    frame['forecast_variability'] = np.sqrt(variance.clip(lower=0))
    frame.drop(columns='_wind_squared', inplace=True)
    speeds = frame[[f'ws_{member}' for member in wx.MEMBERS.values()]]
    frame['icon_minus_ensemble'] = frame.ws_icon - frame.ws_ens
    frame['ensemble_range'] = speeds.max(axis=1) - speeds.min(axis=1)
    for variable, name in [('temperature_2m', 'temperature_spread'),
                           ('surface_pressure', 'pressure_spread')]:
        frame[name] = frame[[f'{variable}_{member}' for member in wx.MEMBERS.values()]].std(axis=1, ddof=0)
    frame['wind_shear_interaction'] = frame.ws_norm * frame.shear
    frame['spread_power_interaction'] = frame.ws_spread * frame.ws_ens ** 2
    return frame


@contextmanager
def enabled():
    """Адаптер для повторного использования неизменённого агента в отдельном процессе.

    Не применять в многопоточном сервере: на время эксперимента меняются ссылки модуля.
    Восстановление выполняется и при исключении; файлы production не меняются.
    """
    original_features, original_columns, original_ctx = mdl.features, mdl.FEATURE_COLUMNS, agent._CONTEXT
    try:
        mdl.features = features
        mdl.FEATURE_COLUMNS = list(BASE_COLUMNS + EXTRA_COLUMNS)
        agent._CONTEXT = None
        yield
    finally:
        mdl.features, mdl.FEATURE_COLUMNS, agent._CONTEXT = original_features, original_columns, original_ctx


def measure() -> tuple[dict, pd.DataFrame]:
    started = time.perf_counter()
    fitted = bt.run()
    ctx = agent.context(fitted)
    forecasts, journal = agent.deliver_months(*bt.VALID, ctx)
    delivered = agent.evaluate(forecasts, ctx, 'delivery')
    monthly = []
    for (month, lead), group in delivered['forecasts'].groupby(
            [delivered['forecasts'].time.dt.strftime('%Y-%m'), 'lead_h']):
        clean = group[~group.curtailed]
        monthly.append({'month': month, 'lead_h': int(lead), **bt.metrics(clean.power, clean.p50)})
    summary = {
        'feature_count': len(mdl.FEATURE_COLUMNS),
        'model_only': fitted['scores'][fitted['scores'].model == bt.MODEL_ONLY].to_dict('records'),
        'delivery': delivered['scores'].to_dict('records'),
        'delivery_monthly_clean': monthly,
        'coverage': delivered['coverage'], 'width': delivered['width'],
        'rollback_count': sum(entry['status'] == 'rollback' for entry in journal),
        'train_rows': len(fitted['train']), 'median_train_rows': len(fitted['train_all']),
        'calibration_rows': len(fitted['calib']), 'seconds': round(time.perf_counter() - started, 2),
    }
    return summary, delivered['forecasts']


def run(output: Path | None = None) -> dict:
    print('База: обучение, калибровка, агент в режиме поставки', flush=True)
    baseline, baseline_forecasts = measure()
    print('Расширенный пакет v1: те же окна и параметры', flush=True)
    with enabled():
        extended, extended_forecasts = measure()
    paired = baseline_forecasts[['time', 'turbine', 'lead_h', 'power', 'curtailed', 'p50']].merge(
        extended_forecasts[['time', 'turbine', 'lead_h', 'p50']],
        on=['time', 'turbine', 'lead_h'], suffixes=('_base', '_extended'), validate='one_to_one')
    assert len(paired) == len(baseline_forecasts) == len(extended_forecasts), 'Разные часы сравнения'
    deltas = []
    for lead, group in paired[~paired.curtailed & paired.power.notna()].groupby('lead_h'):
        improvement = (group.power - group.p50_base).abs() - (group.power - group.p50_extended).abs()
        deltas.append({'lead_h': int(lead), 'n': len(group),
                       'nmae_improvement': float(improvement.mean())})
    report = {
        'experiment': 'extended-weather-v1-conservative-day2-day3', 'status': 'exploratory',
        'protocol': 'Один пакет без подбора по valid; одинаковые параметры и окна; оба варианта через агент.',
        'train_end': str(bt.TRAIN_END), 'calibration_window': [str(t) for t in bt.CALIB_WINDOW],
        'validation_window': [str(t) for t in bt.VALID],
        'base_features': BASE_COLUMNS, 'extra_features': EXTRA_COLUMNS,
        'baseline': baseline, 'extended': extended, 'paired_delivery_clean': deltas,
        'limitations': [
            'Previous Runs задаёт fixed lead, не точный запуск; day2/day3 при выпуске23UTC даёт24–47ч запаса. Доступность предполагает задержку публикации≤24ч.',
            'Ноябрь–январь уже использовался в разработке базы; это исследовательский замер, а не новый holdout.',
            'Факта февраля нет; качество февральского прогноза здесь не измеряется.',
            'Вариативность почасового прогноза не заменяет прогноз порывов и турбулентности.',
            'Режим поставки обрывает доступный факт в начале каждого месяца.',
            'Кривая мощности и базовая калибровочная методика унаследованы без изменений.',
        ],
        'sources': ['https://open-meteo.com/en/docs/previous-runs-api',
                    'https://open-meteo.com/en/docs/historical-forecast-api'],
    }
    output = output or Path(__file__).resolve().parent.parent / 'out' / 'extended_experiment.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    for delta in deltas:
        print(f"{delta['lead_h']} ч: улучшение nMAE {delta['nmae_improvement']:+.6f} (плюс = лучше)")
    print(f'Отчёт: {output}')
    return report


if __name__ == '__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    run()
