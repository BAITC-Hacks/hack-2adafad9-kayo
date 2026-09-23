# -*- coding: utf-8 -*-
"""Стоит ли переносить поправку рефлексии, когда факт устарел.

Факт кончается 31.01.2026, а февраль прокручивается по дням: первые выпуски ещё видят свежий факт,
дальше агент живёт на том, что было. Метрика, снятая при ежедневном факте, этому режиму не
соответствует. Имитируем его на валидационном окне: в каждом из трёх месяцев факт обрывается в его
начале, агент прокручивается по дням месяца в трёх режимах:
  (а) поправка по последним 14 суткам имеющегося факта переносится на весь месяц;
  (б) при устаревшем факте рефлексия выключена, шлюз качества переносится как в (а);
  (в) для справки — факт подвозят каждый день.
Оговорка про ноябрь: его окно поправки 18.10–31.10 лежит в обучающем периоде, где остаток модели
занижен, поэтому перенос там даёт меньше, чем даст в феврале, чьё окно 18.01–31.01 модель не видела.
Решающее сравнение — декабрь и январь, но итог по всем трём месяцам тоже печатается: он идёт в
таблицу валидации.
"""
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from windagent import agent, backtest as bt

MONTHS = {11: 'ноябрь', 12: 'декабрь', 1: 'январь'}

ctx = agent.context()
first, last = bt.VALID
runs = {}

started = time.perf_counter()
default_stale = agent.REFLECT_STALE_DAYS
for label, stale_days in (('(а) перенос поправки', agent.VERIFY_STALE_DAYS), ('(б) без поправки', 0)):
    agent.REFLECT_STALE_DAYS = stale_days
    runs[label] = agent.deliver_months(first, last, ctx)
agent.REFLECT_STALE_DAYS = default_stale
daily, journal = agent.rollout(first - pd.Timedelta(days=2), last.normalize() - pd.Timedelta(days=1), ctx=ctx)
runs['(в) ежедневный факт'] = (daily[daily.time.between(first, last)], journal)
print(f'три прокрутки за {time.perf_counter() - started:.0f} с')


def by_month(forecasts: pd.DataFrame) -> pd.DataFrame:
    """nMAE и смещение без простоев по месяцам и в сумме, отдельно по горизонтам."""
    joined = agent.evaluate(forecasts, ctx, '')['forecasts']
    clean = joined[~joined.curtailed & joined.power.notna()].assign(month=lambda f: f.time.dt.month)
    groups = [(MONTHS[m], g) for m, g in clean.groupby('month', sort=False)]
    groups += [('дек–янв', clean[clean.month != 11]), ('ноя–янв', clean)]
    cells = {}
    for name, group in groups:
        for lead, part in group.groupby('lead_h'):
            score = bt.metrics(part.power, part.p50)
            cells[(name, int(lead))] = f'{score["nmae"]:.4f} ({score["bias"]:+.3f})'
    return pd.Series(cells)


table = pd.DataFrame({label: by_month(forecasts) for label, (forecasts, _) in runs.items()})
table.index = pd.MultiIndex.from_tuples(table.index, names=['месяц', 'горизонт'])
print('\nnMAE без простоев (в скобках смещение прогноз−факт):')
print(table.to_string())

print('\nрефлексия по режимам: сколько выпусков со сдвигом медианы / в пределах шума / пропущено')
for label, (_, entries) in runs.items():
    reflect = [e for e in entries if e['step'] == 'reflect']
    shifted = sum(e['status'] == 'ok' for e in reflect)
    quiet = sum('в пределах шума' in e['text'] for e in reflect if e['status'] == 'skipped')
    print(f'  {label:<22} {shifted:>3} / {quiet:>3} / {len(reflect) - shifted - quiet:>3}')

rollbacks = {label: sum(e['status'] == 'rollback' for e in entries) for label, (_, entries) in runs.items()}
print(f'откатов на кривую: {rollbacks}')


def total(label: str, lead: int) -> float:
    return float(table.loc[('ноя–янв', lead), label].split()[0])


carry = [total('(а) перенос поправки', lead) for lead in bt.LEADS]
plain = [total('(б) без поправки', lead) for lead in bt.LEADS]
better = all(c < p for c, p in zip(carry, plain))
print(f'\nитог ноя–янв, 24/48 ч: перенос {carry[0]:.4f}/{carry[1]:.4f} против без поправки '
      f'{plain[0]:.4f}/{plain[1]:.4f} — перенос {"лучше" if better else "НЕ лучше"}')
