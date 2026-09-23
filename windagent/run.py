# -*- coding: utf-8 -*-
"""Единая точка входа решения.

    python -m windagent.run --backtest   обучение, валидация и прогноз на февраль 2026
    python -m windagent.run --forecast   прогноз на ближайшие 24–48 часов по свежей погоде
    python -m windagent.run --export     пересобрать web/data.json для дашборда

Прогон рассчитан на чистую машину: архив прогнозов погоды лежит в репозитории, интернет нужен только
режиму `--forecast`.
"""
import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from . import backtest
from . import model as mdl
from . import weather as wx

ROOT = Path(__file__).resolve().parent.parent
FORECASTS = ROOT / 'forecasts'
WEB = ROOT / 'web'

SITE = {'name': 'ВЭС, Шелекский коридор', 'lat': 43.645150, 'lon': 78.535604}
# 2.5 МВт на турбину — типовая машина парка в Шелекском коридоре, штраф за небаланс взят справочно
CAPACITY_MW = 2.5
PENALTY_PER_MWH = 12000
OURS, BASELINE = 'наше решение', 'персистентность'


def _agent():
    """Агентный слой подключается, когда он готов: без него прогон всё равно работает."""
    try:
        from . import agent
        return agent
    except ImportError:
        return None


def _forecast_frame(result: dict) -> pd.DataFrame:
    return result.get('forecast', result.get('test_pred'))


def _scores(result: dict) -> pd.DataFrame:
    """Метрики для витрины: если считались два режима, берём часы без простоев."""
    scores = result['scores']
    if 'scope' in scores.columns and (scores.scope == 'без простоев').any():
        return scores[scores.scope == 'без простоев']
    return scores


def write_forecast_csv(forecast: pd.DataFrame) -> list[Path]:
    out = FORECASTS / '2026-02'
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for turbine, group in forecast.groupby('turbine'):
        path = out / f'forecast_{turbine}.csv'
        (group[['time', 'lead_h', 'p10', 'p50', 'p90']]
         .sort_values(['lead_h', 'time']).round(4).to_csv(path, index=False))
        written.append(path)
    return written


def economics(scores: pd.DataFrame) -> dict:
    """Во что обходится ошибка прогноза за месяц: nMAE переводим в мегаватт-часы и в деньги."""
    day_ahead = scores[scores.lead_h == 24]
    ours = day_ahead[day_ahead.model == OURS].nmae.mean()
    base = day_ahead[day_ahead.model == BASELINE].nmae.mean()
    if pd.isna(ours) or pd.isna(base):
        return {}
    to_money = CAPACITY_MW * 28 * 24 * 2 * PENALTY_PER_MWH   # два месяца турбин за февраль
    return {'penalty_per_mwh': PENALTY_PER_MWH, 'capacity_mw': CAPACITY_MW,
            'monthly_loss': round(float(ours) * to_money),
            'baseline_loss': round(float(base) * to_money), 'currency': '₸'}


def export_json(result: dict, agent_log: list | None = None) -> Path:
    """Собрать web/data.json по контракту дашборда."""
    forecast = _forecast_frame(result).copy()
    actual = result['hourly'][['time', 'turbine', 'power']].rename(columns={'power': 'actual'})
    rows = forecast.merge(actual, on=['time', 'turbine'], how='left')
    if 'actual' not in rows.columns:
        rows['actual'] = None

    payload_forecast = [
        {'time': r.time.strftime('%Y-%m-%dT%H:%M'), 'turbine': r.turbine, 'lead_h': int(r.lead_h),
         'p10': round(float(r.p10), 4), 'p50': round(float(r.p50), 4), 'p90': round(float(r.p90), 4),
         'actual': None if pd.isna(r.actual) else round(float(r.actual), 4),
         'curve': round(float(r.curve), 4)}
        for r in rows.itertuples(index=False)
    ]
    scores = _scores(result)
    payload_metrics = [
        {'model': r.model, 'lead_h': int(r.lead_h), 'nmae': round(float(r.nmae), 4),
         'nrmse': round(float(r.nrmse), 4), 'bias': round(float(r.bias), 4),
         'skill': None if pd.isna(getattr(r, 'skill', None)) else round(float(r.skill), 4)}
        for r in scores.itertuples(index=False)
    ]
    payload = {
        'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'site': SITE,
        'period': {'start': '2026-02-01T00:00', 'end': '2026-02-28T23:00'},
        'forecast': payload_forecast,
        'metrics': payload_metrics,
        'agent_log': agent_log or [],
        'economics': economics(scores),
        'coverage': result.get('coverage'),
    }
    WEB.mkdir(parents=True, exist_ok=True)
    path = WEB / 'data.json'
    text = json.dumps(payload, ensure_ascii=False, indent=1)
    path.write_text(text, encoding='utf-8')
    # без data.js страница, открытая двойным кликом, не читает соседний json (политика file://)
    (WEB / 'data.js').write_text('window.DASHBOARD_DATA = ' + text + ';\n', encoding='utf-8')
    return path


def do_backtest() -> dict:
    started = time.time()
    result = backtest.run()

    print('\nВАЛИДАЦИЯ: ноябрь 2025 — январь 2026 (этих данных модель не видела)')
    scores = result['scores']
    index = ['scope', 'model'] if 'scope' in scores.columns else ['model']
    print(scores.pivot_table(index=index, columns='lead_h', values=['nmae', 'skill']).round(3).to_string())
    if result.get('coverage') is not None:
        print(f'\nпокрытие коридора P10–P90: {result["coverage"]:.1%}')

    forecast = _forecast_frame(result)
    files = write_forecast_csv(forecast)
    metrics_path = FORECASTS / 'metrics.csv'
    scores.round(4).to_csv(metrics_path, index=False)

    agent = _agent()
    log = None
    if agent and hasattr(agent, 'run_backtest_month'):
        try:
            _, log = agent.run_backtest_month()
            print(f'агент: {sum(1 for e in log if e["step"] == "forecast")} циклов, '
                  f'{sum(1 for e in log if e["status"] == "rollback")} откатов на базовую линию')
        except Exception as err:   # прогон и выгрузки не должны падать из-за агента
            print(f'агент не отработал ({err}); прогноз и метрики это не затрагивает')
    export_json(result, log)

    hours = forecast[forecast.lead_h == 24].groupby('turbine').size().to_dict()
    print(f'\nпрогноз на февраль 2026: {hours} часов на горизонте 24 ч')
    print('выгрузки: ' + ', '.join(str(p.relative_to(ROOT)) for p in files + [metrics_path]))
    print(f'время прогона: {time.time() - started:.1f} с')
    return result


def do_forecast() -> pd.DataFrame:
    """Прогноз «как сейчас»: свежая погода, та же обученная модель."""
    agent = _agent()
    if agent and hasattr(agent, 'run_cycle'):
        issued = pd.Timestamp.now(tz='UTC').tz_localize(None).floor('h')
        predicted, journal, _ = agent.run_cycle(issued, live=True)
        for entry in journal:
            print(f'{entry["step"]:<9} {entry["status"]:<8} {entry["text"]}')
        if predicted is not None:
            print(predicted.round(3).to_string(index=False))
        return predicted

    result = backtest.run()
    models, curves = result['models'], result['curves']
    today = datetime.now(timezone.utc).date()
    fresh = wx.tidy(wx.load(today.isoformat(), (today + timedelta(days=2)).isoformat(), refresh=True))

    parts = []
    for turbine in sorted(result['hourly'].turbine.unique()):
        piece = mdl.features(fresh, turbine)
        piece['turbine'] = turbine
        piece['curve'] = [mdl.apply_curve(curves[(turbine, int(lead))], pd.Series([wind]))[0]
                          if (turbine, int(lead)) in curves else float('nan')
                          for lead, wind in zip(piece.lead_h, piece.ws_norm)]
        parts.append(piece.dropna(subset=['curve']))
    live = mdl.predict(models, pd.concat(parts, ignore_index=True))
    upcoming = live[live.lead_h == 24].sort_values(['turbine', 'time'])
    print(upcoming.round(3).to_string(index=False))
    return upcoming


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
        export_json(backtest.run())
        print('web/data.json пересобран')
    else:
        do_backtest()


if __name__ == '__main__':
    main()
