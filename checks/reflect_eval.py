# -*- coding: utf-8 -*-
"""Честный замер шага рефлексии: помогает ли сдвиг медианы на недавнее смещение.

Для каждого дня выпуска d из валидационного окна берём прогноз на сутки d+1 (горизонт 24 ч) и на
d+2 (48 ч). Смещение считаем строго по факту до момента выпуска: средняя ошибка медианы на
горизонте 24 ч за последние N суток, без единого часа из будущего. Сравниваем nMAE без поправки и
с ней: окна 3/7/14 суток, усадка 1 и 0,5, плюс шлюз — сдвигать, только когда |смещение| больше двух
стандартных ошибок среднего. Ошибки соседних часов сильно скоррелированы, поэтому стандартную
ошибку считаем и по часам, и по суточным средним: второй вариант честнее.

Первые дни валидации пропускаем, чтобы окно смещения не залезало в обучающий период — там ошибка
модели занижена, и оценка вышла бы оптимистичной.

Итог на честных признаках (окна внутри блока выпуска, база ICON): без поправки 0,1754/0,1920,
окно 14 сут ×1 со шлюзом 0,1633/0,1802, ×0,5 — 0,1659/0,1829. Смещение системное (−0,08 при
стандартной ошибке ~0,012), поэтому в agent.reflect стоит полный сдвиг; шлюз открыт в 93 %
выпусков. Та же пара вариантов прокручена полным циклом в agent.validate — цифры в его докстринге.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from windagent import backtest as bt, model as mdl

WINDOWS = (3, 7, 14)
SHRINKS = (1.0, 0.5)
GATE_SE = 2.0
CHOSEN = (14, 1.0)      # что в итоге стоит в agent.reflect: окно и усадка, шлюз по часовой ошибке

hourly, table, curves = bt.prepare()
fitted = bt.train_models(table)
valid = table[table.time.between(*bt.VALID) & table.lead_h.isin(bt.LEADS)]
pred = mdl.apply_bounds(mdl.predict(fitted['models'], valid), fitted['offsets'])
valid = valid[['time', 'turbine', 'lead_h', 'power', 'curtailed']].merge(
    pred[['time', 'turbine', 'lead_h', 'p10', 'p50', 'p90']], on=['time', 'turbine', 'lead_h'])
valid['err'] = valid.power - valid.p50          # факт минус медиана: плюс — модель занижает

# то, что агент видит при рефлексии: ошибка на горизонте 24 ч, простои исключены
seen = valid[(valid.lead_h == 24) & valid.power.notna() & ~valid.curtailed]
by_day = valid.assign(day=valid.time.dt.normalize())

first_issue = bt.VALID[0] + pd.Timedelta(days=max(WINDOWS))
issues = pd.date_range(first_issue, bt.VALID[1].normalize() - pd.Timedelta(days=1), freq='D')


def bias_before(issue: pd.Timestamp, window: int) -> pd.DataFrame:
    recent = seen[(seen.time < issue) & (seen.time >= issue - pd.Timedelta(days=window))]
    hourly_stats = recent.groupby('turbine').err.agg(['mean', 'std', 'count'])
    daily = recent.groupby(['turbine', recent.time.dt.normalize()]).err.mean()
    daily_stats = daily.groupby('turbine').agg(['std', 'count'])
    return pd.DataFrame({
        'bias': hourly_stats['mean'],
        'se_hour': hourly_stats['std'] / np.sqrt(hourly_stats['count']),
        'se_day': daily_stats['std'] / np.sqrt(daily_stats['count']),
    })


trials = []
for issue in issues:
    targets = pd.concat([
        by_day[(by_day.lead_h == 24) & (by_day.day == issue + pd.Timedelta(days=1))],
        by_day[(by_day.lead_h == 48) & (by_day.day == issue + pd.Timedelta(days=2))],
    ])
    for window in WINDOWS:
        stats = bias_before(issue, window)
        trials.append(targets.join(stats, on='turbine').assign(window=window, issue=issue))
trials = pd.concat(trials, ignore_index=True)
trials = trials[trials.power.notna()]


def nmae(frame: pd.DataFrame, p50: pd.Series) -> float:
    return float((frame.power - p50).abs().mean())


def variants(frame: pd.DataFrame) -> dict:
    out = {'без': frame.p50}
    for shrink in SHRINKS:
        shifted = np.clip(frame.p50 + shrink * frame.bias, 0, 1)
        out[f'×{shrink:g}'] = shifted
        for label, se in (('шлюз-ч', frame.se_hour), ('шлюз-д', frame.se_day)):
            out[f'×{shrink:g} {label}'] = shifted.where(frame.bias.abs() > GATE_SE * se, frame.p50)
    return out


for scope, subset in (('без простоев', trials[~trials.curtailed]), ('все часы', trials)):
    rows = []
    for (window, lead), group in subset.groupby(['window', 'lead_h']):
        row = {'окно': window, 'горизонт': int(lead)}
        row.update({name: nmae(group, p50) for name, p50 in variants(group).items()})
        rows.append(row)
    scores = pd.DataFrame(rows).set_index(['окно', 'горизонт'])
    print(f'\n{scope}: nMAE по {len(issues)} дням выпуска с {issues[0].date()} по {issues[-1].date()}')
    print(scores.round(4).to_string())

gate_share = (trials.assign(open_h=trials.bias.abs() > GATE_SE * trials.se_hour,
                            open_d=trials.bias.abs() > GATE_SE * trials.se_day)
              .groupby('window')[['open_h', 'open_d']].mean())
print('\nдоля выпусков, где шлюз открыт (по часам / по суткам):')
print(gate_share.round(2).to_string())

print('\nсмещение по окнам, доля номинала:')
print(trials.groupby(['window', 'turbine']).bias.describe()[['mean', 'std', 'min', 'max']].round(3).to_string())

# Куда девать коридор при сдвиге медианы. Пятая часть фактов — ровно ноль, ещё часть — полка, и
# P10/P90 после конформной калибровки сидят на этих массах: сдвиг границ даже на 0,02 их выкидывает.
window, shrink = CHOSEN
chosen = trials[(trials.window == window) & ~trials.curtailed].copy()
shift = np.where(chosen.bias.abs() > GATE_SE * chosen.se_hour, shrink * chosen.bias, 0.0)
mid = np.clip(chosen.p50 + shift, 0, 1)
print(f'\nокно {window}, ×{shrink:g}, шлюз {GATE_SE:g} SE — куда сдвигать коридор:')
for label, low, p50, high in (
        ('без поправки', chosen.p10, chosen.p50, chosen.p90),
        ('сдвиг всей тройки', np.clip(chosen.p10 + shift, 0, 1), mid, np.clip(chosen.p90 + shift, 0, 1)),
        ('только медиана', chosen.p10, mid, chosen.p90),
        ('медиана в пределах коридора', chosen.p10, np.clip(mid, chosen.p10, chosen.p90), chosen.p90)):
    covered = ((chosen.power >= low) & (chosen.power <= high)).groupby(chosen.lead_h).mean()
    error = (chosen.power - p50).abs().groupby(chosen.lead_h).mean()
    print(f'  {label:<28} nMAE ' + '/'.join(f'{e:.4f}' for e in error)
          + '   покрытие ' + '/'.join(f'{c:.1%}' for c in covered))
