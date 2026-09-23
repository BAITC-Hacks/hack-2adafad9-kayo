# -*- coding: utf-8 -*-
"""Проверка выравнивания по времени: метки турбин против прогноза погоды.

Если SCADA пишет местное время, а погода отдаётся в UTC, всё решение молча съедет на час-два.
Отдельная тонкость: Казахстан в марте 2024 перешёл на единый UTC+5, Алматы до этого был UTC+6 —
значит сдвиг может быть разным до и после перехода.

Считаем взаимную корреляцию измеренной скорости ветра с прогнозной на сдвигах −6…+6 часов.
Пик обязан быть на нуле.
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from windagent import data as turbines, weather as wx

hourly = turbines.load()
weather = wx.tidy(wx.load('2024-02-17', '2026-02-28'))
nowcast = weather[weather.lead_h == 0][['time', 'wind_speed_100m']]


def peak(measured: pd.DataFrame, label: str):
    joined = measured.merge(nowcast, on='time', how='inner')
    if len(joined) < 100:
        print(f'{label}: мало общих часов ({len(joined)})')
        return
    best = []
    for shift in range(-6, 7):
        corr = joined.ws_meas.corr(joined.wind_speed_100m.shift(shift))
        best.append((shift, corr))
    top = max(best, key=lambda x: x[1])
    line = '  '.join(f'{s:+d}:{c:.3f}' for s, c in best)
    print(f'{label}: пик на {top[0]:+d} ч (r={top[1]:.3f})\n    {line}')


for turbine, group in hourly.groupby('turbine'):
    series = group[['time', 'ws_meas']].dropna()
    peak(series, f'{turbine}, весь период')
    peak(series[series.time < '2024-03-01'], f'{turbine}, до перехода на UTC+5')
    peak(series[series.time >= '2024-03-01'], f'{turbine}, после перехода')
