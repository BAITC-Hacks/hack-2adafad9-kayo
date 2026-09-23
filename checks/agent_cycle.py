# -*- coding: utf-8 -*-
"""Проверки агентного цикла, которые не видно по метрикам.

1. Признаки поставки равны признакам обучения: для одного дня выпуска считаем признаки на окне
   агента (48 часов) и берём те же строки из таблицы бэктеста — скользящие окна внутри блока
   выпуска должны совпасть до последнего знака, иначе модель на поставке видит не то, на чём училась.
2. Решение о пересчёте: первый прогон, новые сутки, тот же выпуск, новый прогон с тем же ветром,
   новый прогон с другим ветром.
3. Выпуск за пределами архива: журнал получает запись error, цикл возвращает None без трейсбека.
4. При обрыве факта агент переносит поправку с последних доступных данных.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from windagent import agent, model as mdl

ctx = agent.context()
table = ctx['table']

print('1. признаки поставки против таблицы обучения')
issue = pd.Timestamp('2026-01-20 23:00')
journal = []
window = agent.prepare(agent.collect(issue, journal, ctx), journal, issue)
for turbine in ctx['turbines']:
    served = mdl.features(window, turbine)
    trained = table[(table.turbine == turbine) & table.time.isin(served.time) & table.lead_h.isin([24, 48])]
    both = served.merge(trained, on=['time', 'lead_h'], suffixes=('_agent', '_train'))
    worst = max(np.abs(both[f'{c}_agent'] - both[f'{c}_train']).max() for c in mdl.FEATURE_COLUMNS
                if c not in ('curve', 'turbine_id', 'lead_h'))
    assert len(both) == 48 and worst < 1e-10
    print(f'   {turbine}: {len(both)} строк, наибольшее расхождение по признакам {worst:.2e}')

print('\n2. решение о пересчёте')


def state(issue, day, wind, leads=(24, 48)):
    hours = pd.date_range(day, periods=48, freq='h')
    window = pd.DataFrame({'time': hours, 'lead_h': np.repeat(leads, 24), 'wind_speed_100m': wind})
    return {'issue_time': pd.Timestamp(issue), 'window': window}


first = state('2026-02-01', '2026-02-02', 5.0)
cases = (
    ('первый прогон', None, first),
    ('новые сутки, ветер тот же', first, state('2026-02-02', '2026-02-03', 5.0)),
    ('тот же выпуск', first, state('2026-02-01', '2026-02-02', 9.0)),
    ('новый прогон на те же сутки, ветер +0,3', first, state('2026-02-01 06:00', '2026-02-02', 5.3)),
    ('новый прогон на те же сутки, ветер +4', first, state('2026-02-01 06:00', '2026-02-02', 9.0)),
)
for label, previous, current in cases:
    needed, reason = agent.decide_rerun(previous, current)
    print(f'   {label:<40} -> {"пересчёт" if needed else "оставить"}: {reason}')
assert [agent.decide_rerun(previous, current)[0] for _, previous, current in cases] == [True, True, False, False, True]

print('\n3. выпуск за пределами архива')
predicted, journal, _ = agent.run_cycle(pd.Timestamp('2026-03-05 23:00'), ctx=ctx, with_explain=False)
assert predicted is None and journal[-1]['status'] == 'error'
print(f'   прогноз: {predicted}; журнал: {journal[-1]["step"]} {journal[-1]["status"]} — {journal[-1]["text"]}')

print('\n4. перенос поправки без свежего факта (26.02.2026, факт кончается 31.01)')
predicted, journal, _ = agent.run_cycle(pd.Timestamp('2026-02-26 23:00'), ctx=ctx, with_explain=False)
assert predicted is not None
assert predicted.effective_lead_h.between(1, 48).all()
assert pd.api.types.is_datetime64_any_dtype(predicted.weather_reference_time)
assert pd.api.types.is_integer_dtype(predicted.weather_lead_h)
for entry in journal:
    if entry['step'] in ('reflect', 'verify'):
        print(f'   {entry["step"]} {entry["status"]}: {entry["text"]}')
