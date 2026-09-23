# -*- coding: utf-8 -*-
"""Единая точка входа решения.

    python -m windagent.run --backtest   обучение, валидация и прогноз на февраль 2026 агентным циклом
    python -m windagent.run --forecast   прогноз на ближайшие 24–48 часов по свежей погоде
    python -m windagent.run --export     пересобрать web/data.json (тот же прогон без выгрузок в forecasts/)

Прогон рассчитан на чистую машину: архив прогнозов погоды лежит в репозитории, интернет нужен только
режиму `--forecast`.
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from . import agent
from . import asof
from . import backtest
from . import data as turbines

ROOT = Path(__file__).resolve().parent.parent
FORECASTS = ROOT / 'forecasts'
WEB = ROOT / 'web'

SITE = {'name': 'ВЭС, Шелекский коридор', 'lat': 43.645150, 'lon': 78.535604}
# допущения для оценки порядка величин, не данные станции: 2,5 МВт на турбину и штраф за небаланс
CAPACITY_MW = 2.5
PENALTY_PER_MWH = 12000
BASELINE = 'персистентность'


def build() -> dict:
    """Полный прогон: обучение один раз, валидация модели и агентного цикла, февраль агентом."""
    started = time.time()
    ctx = agent.context()
    result = backtest.run(fitted=ctx)
    checked = agent.validate(ctx)
    forecast, journal = agent.run_backtest_month(ctx=ctx)
    return {**result, 'scores': pd.concat([checked['scores'], result['scores']], ignore_index=True),
            'model_coverage': result['coverage'], 'model_width': result['width'],
            'coverage': checked['coverage'], 'width': checked['width'],
            'forecast': forecast, 'journal': journal, 'seconds': time.time() - started}


def _scores(result: dict) -> pd.DataFrame:
    """Метрики для витрины: часы без простоев."""
    scores = result['scores']
    return scores[scores.scope == 'без простоев']


def write_forecast_csv(forecast: pd.DataFrame) -> list[Path]:
    """Поставка: на турбину 672 часа × 2 горизонта. Время в UTC, рядом местное (Asia/Almaty,
    UTC+5) — в таком же местном времени организаторы дали факт."""
    out = FORECASTS / '2026-02'
    out.mkdir(parents=True, exist_ok=True)
    written = []
    local = forecast.time + pd.Timedelta(hours=turbines.OFFSET_AFTER)
    for turbine, group in forecast.assign(time_local=local).groupby('turbine'):
        path = out / f'forecast_{turbine}.csv'
        (group[['issue_time', 'time', 'time_local', 'lead_h', 'p10', 'p50', 'p90',
                'effective_lead_h', 'weather_lead_h', 'weather_reference_time']]
         .sort_values(['lead_h', 'time']).round(4)
         .to_csv(path, index=False, date_format='%Y-%m-%d %H:%M'))
        written.append(path)
    return written


def economics(scores: pd.DataFrame) -> dict:
    """Во что обходится ошибка прогноза за месяц: nMAE переводим в мегаватт-часы и в деньги."""
    day_ahead = scores[scores.lead_h == 24]
    ours = day_ahead[day_ahead.model == agent.OURS].nmae.mean()
    base = day_ahead[day_ahead.model == BASELINE].nmae.mean()
    if pd.isna(ours) or pd.isna(base):
        return {}
    to_money = CAPACITY_MW * 28 * 24 * 2 * PENALTY_PER_MWH   # две турбины × часы февраля
    return {'penalty_per_mwh': PENALTY_PER_MWH, 'capacity_mw': CAPACITY_MW,
            'monthly_loss': round(float(ours) * to_money),
            'baseline_loss': round(float(base) * to_money), 'currency': '₸'}


def export_json(result: dict) -> Path:
    """Собрать web/data.json по контракту дашборда."""
    forecast = result['forecast']
    actual = result['hourly'][['time', 'turbine', 'power']].rename(columns={'power': 'actual'})
    rows = forecast.merge(actual, on=['time', 'turbine'], how='left')

    payload_forecast = [
        {'time': r.time.strftime('%Y-%m-%dT%H:%M'), 'turbine': r.turbine, 'lead_h': int(r.lead_h),
         'issue_time': r.issue_time.strftime('%Y-%m-%dT%H:%M'),
         'effective_lead_h': int(r.effective_lead_h), 'weather_lead_h': int(r.weather_lead_h),
         'weather_reference_time': r.weather_reference_time.strftime('%Y-%m-%dT%H:%M'),
         'weather_source': r.weather_source,
         'p10': round(float(r.p10), 4), 'p50': round(float(r.p50), 4), 'p90': round(float(r.p90), 4),
         'actual': None if pd.isna(r.actual) else round(float(r.actual), 4),
         'curve': round(float(r.curve), 4),
         'wind_speed_100m': None if pd.isna(r.wind_speed_100m) else round(float(r.wind_speed_100m), 2),
         'wind_direction_100m': None if pd.isna(r.wind_direction_100m) else round(float(r.wind_direction_100m), 1)}
        for r in rows.itertuples(index=False)
    ]
    scores = _scores(result)
    payload_metrics = [
        {'model': r.model, 'lead_h': int(r.lead_h), 'nmae': round(float(r.nmae), 4),
         'nrmse': round(float(r.nrmse), 4), 'bias': round(float(r.bias), 4),
         'skill': None if pd.isna(r.skill) else round(float(r.skill), 4)}
        for r in scores.itertuples(index=False)
    ]
    payload = {
        'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'site': SITE,
        'forecast_protocol': {
            'timezone': 'UTC', 'issue_hour_utc': asof.ISSUE_HOUR_UTC,
            'effective_lead_hours': [1, 48], 'lead_h_meaning': 'корзины 1–24 и 25–48 часов',
            'weather_sources': {'24': 'previous_day2', '48': 'previous_day3'},
            'publication_buffer_hours': asof.PUBLICATION_BUFFER_HOURS,
            'availability_assumption': 'ДОПУЩЕНИЕ: задержка публикации прогноза не превышала 24 часа.',
            'reference_time_meaning': 'Номинальное valid time минус fixed lead; не точный init одного запуска.',
            'training_end_utc': str(backtest.TRAIN_END),
            'calibration_targets_excluded_from_curve': True,
        },
        'period': {'start': forecast.time.min().strftime('%Y-%m-%dT%H:%M'),
                   'end': forecast.time.max().strftime('%Y-%m-%dT%H:%M')},
        'validation_period': {'start': result['valid'].time.min().strftime('%Y-%m-%dT%H:%M'),
                              'end': result['valid'].time.max().strftime('%Y-%m-%dT%H:%M')},
        'forecast': payload_forecast,
        'metrics': payload_metrics,
        'agent_log': result['journal'],
        'economics': economics(scores),
        'coverage': result['coverage'],
        'width': result['width'],
    }
    WEB.mkdir(parents=True, exist_ok=True)
    path = WEB / 'data.json'
    text = json.dumps(payload, ensure_ascii=False, indent=1)
    path.write_text(text, encoding='utf-8')
    # без data.js страница, открытая двойным кликом, не читает соседний json (политика file://)
    (WEB / 'data.js').write_text('window.DASHBOARD_DATA = ' + text + ';\n', encoding='utf-8')
    return path


def do_backtest() -> dict:
    result = build()

    print('\nВАЛИДАЦИЯ: ноябрь 2025 — январь 2026 (этих данных модель не видела)')
    print(result['scores'].pivot_table(index=['scope', 'model'], columns='lead_h',
                                       values=['nmae', 'skill']).round(3).to_string())
    print(f'\nпокрытие коридора P10–P90: агентный цикл {result["coverage"]:.1%} при ширине {result["width"]:.3f}, '
          f'модель без агента {result["model_coverage"]:.1%} при ширине {result["model_width"]:.3f}')

    forecast, journal = result['forecast'], result['journal']
    files = write_forecast_csv(forecast)
    metrics_path = FORECASTS / 'metrics.csv'
    result['scores'].round(4).to_csv(metrics_path, index=False)
    agent.save_journal(journal)
    export_json(result)

    hours = forecast.groupby(['turbine', 'lead_h']).size().to_dict()
    print(f'\nпрогноз на февраль 2026 агентным циклом: часов по (турбина, горизонт) {hours}, '
          f'выпусков {sum(1 for e in journal if e["step"] == "forecast")}, '
          f'откатов на кривую {sum(1 for e in journal if e["status"] == "rollback")}')
    print('выгрузки: ' + ', '.join(str(p.relative_to(ROOT)) for p in files + [metrics_path, agent.JOURNAL_PATH]))
    print(f'время прогона: {result["seconds"]:.1f} с')
    return result


def do_forecast() -> pd.DataFrame | None:
    """Прогноз «как сейчас»: свежая погода, та же обученная модель, тот же цикл агента."""
    issued = pd.Timestamp.now(tz='UTC').tz_localize(None).floor('h')
    predicted, journal, _ = agent.run_cycle(issued, live=True)
    agent.print_journal(journal)
    if predicted is None:
        print(f'\nвыпуск {issued:%Y-%m-%d %H:%M} не состоялся — причина в журнале выше')
        return None
    print(predicted.round(3).to_string(index=False))
    return predicted


def main():
    parser = argparse.ArgumentParser(description='Прогноз выработки ВЭС')
    parser.add_argument('--backtest', action='store_true', help='обучение, валидация и прогноз на февраль')
    parser.add_argument('--forecast', action='store_true', help='прогноз на ближайшие 24–48 часов')
    parser.add_argument('--export', action='store_true', help='пересобрать web/data.json')
    args = parser.parse_args()

    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if args.forecast:
        do_forecast()
    elif args.export:
        export_json(build())
        print('web/data.json пересобран')
    else:
        do_backtest()


if __name__ == '__main__':
    main()
