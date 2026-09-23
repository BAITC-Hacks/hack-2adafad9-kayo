# -*- coding: utf-8 -*-
"""Валидация решения и прогноз на тестовый февраль.

Февраля 2026 в данных нет, поэтому никакого «бэктеста февраля» не существует: февраль — это режим
поставки. Себя проверяем на отложенном окне ноябрь 2025 — январь 2026: модель его не видела, зима —
тот же режим, что и в феврале, а прогнозы погоды взяты те, что были известны за сутки и за двое.

Лестница базовых линий, без которой цифры ничего не значат:
  * климатология — среднее по месяцу и часу;
  * персистентность — «как в тот же час сутки назад»;
  * смешанная персистентность — оптимальная смесь двух предыдущих;
  * кривая мощности на сыром прогнозном ветре, без обучения, — главный ориентир: она показывает,
    что именно добавляет бустинг поверх физики.

Отдельно считается «цена утечки»: тот же пайплайн на ноукасте (горизонт 0 ч) — это то, что получится,
если взять historical-forecast-api вместо честного архива прогнозов.
"""
import time

import numpy as np
import pandas as pd

from . import data as turbines
from . import model as mdl
from . import weather as wx

TRAIN_END = pd.Timestamp('2025-10-31 23:00')
CALIB_WINDOW = (pd.Timestamp('2024-11-01'), pd.Timestamp('2025-02-28 23:00'))  # тот же сезон год назад
VALID = (pd.Timestamp('2025-11-01'), pd.Timestamp('2026-01-31 23:00'))
TEST = (pd.Timestamp('2026-02-01'), pd.Timestamp('2026-02-28 23:00'))
LEADS = (24, 48)
COVERAGE_TARGET = 0.80                                       # P10–P90 должен покрывать ~80 % фактов


def metrics(actual: pd.Series, predicted: pd.Series, reference: pd.Series | None = None) -> dict:
    ok = actual.notna() & predicted.notna()
    a, p = actual[ok], predicted[ok]
    if not len(a):
        return {}
    rmse = float(np.sqrt(((a - p) ** 2).mean()))
    out = {'nmae': float((a - p).abs().mean()), 'nrmse': rmse, 'bias': float((p - a).mean()), 'n': int(len(a))}
    if reference is not None:
        r = reference[ok]
        mae_ref = float((a - r).abs().mean())
        out['skill'] = float(1 - out['nmae'] / mae_ref) if mae_ref else 0.0
    return out


def add_baselines(frame: pd.DataFrame, hourly: pd.DataFrame, train: pd.DataFrame) -> pd.DataFrame:
    """Приклеить к таблице все базовые линии."""
    clim = (train.assign(month=train.time.dt.month, hour=train.time.dt.hour)
            .groupby(['turbine', 'month', 'hour']).power.mean().rename('climatology').reset_index())
    out = frame.copy()
    out['month_'] = out.time.dt.month
    out['hour_'] = out.time.dt.hour
    out = out.merge(clim, left_on=['turbine', 'month_', 'hour_'],
                    right_on=['turbine', 'month', 'hour'], how='left', suffixes=('', '_c'))

    past = {name: group.set_index('time').power for name, group in hourly.groupby('turbine')}
    lag = []
    for row_turbine, row_time, row_lead in zip(out.turbine, out.time, out.lead_h):
        series = past.get(row_turbine)
        lag.append(series.get(row_time - pd.Timedelta(hours=int(row_lead)), np.nan) if series is not None else np.nan)
    out['persistence'] = lag
    # смесь персистентности с климатологией: на сутки вперёд она заметно сильнее сырого лага
    out['persistence_mix'] = 0.35 * out.persistence.fillna(out.climatology) + 0.65 * out.climatology
    return out.drop(columns=['month_', 'hour_'])


def prepare(leads=LEADS):
    hourly = turbines.load()
    weather = wx.ensemble('2024-02-17', '2026-02-28')
    parts = []
    for turbine, group in hourly.groupby('turbine'):
        joined = weather.merge(group[['time', 'power', 'curtailed', 'n_samples']], on='time', how='left')
        # после merge колонка object (bool + NaN): pandas 3 её к bool не приводит, и `~` инвертирует биты.
        # eq(True) даёт честный bool без FutureWarning про downcasting, которым шумит fillna на 2.x
        joined['curtailed'] = joined.curtailed.eq(True)
        table = mdl.features(joined, turbine)
        table['turbine'] = turbine
        parts.append(table)
    table = pd.concat(parts, ignore_index=True)
    table = table[table.lead_h.isin(list(leads) + [0])].reset_index(drop=True)

    # кривые снимаем только с обучающего периода и отдельно для каждого горизонта
    curves = {}
    train_mask = (table.time <= TRAIN_END) & table.power.notna() & ~table.curtailed
    for (turbine, lead), group in table[train_mask].groupby(['turbine', 'lead_h']):
        curves[(turbine, int(lead))] = mdl.power_curve(group, mdl.CURVE_WIND)
    table['curve'] = [mdl.apply_curve(curves[(t, int(l))], pd.Series([w]))[0]
                      for t, l, w in zip(table.turbine, table.lead_h, table[mdl.CURVE_WIND])]
    return hourly, table, curves


def train_models(table: pd.DataFrame, leads=LEADS) -> dict:
    """Обучение и конформная калибровка на всём, что известно до TRAIN_END.

    Окно CALIB_WINDOW уходит под калибровку коридора: считать её на valid — утечка, а на
    самом train — оптимистичная оценка (квантильные модели эти часы уже видели). Медиана в
    калибровке не участвует, поэтому учится на всём ряду целиком."""
    train_all = table[(table.time <= TRAIN_END) & table.power.notna() & table.curve.notna()
                      & ~table.curtailed & table.lead_h.isin(leads)]
    # калибруем на прошлой зиме, а не на последнем месяце: зимний ветер порывистее осеннего, и отступ,
    # снятый с октября, на ноябре-январе недобирал покрытие (78,4 % против 79,8 %, коридор 0,451 → 0,431)
    in_calib = train_all.time.between(*CALIB_WINDOW)
    train = train_all[~in_calib]
    calib = train_all[in_calib]
    models = mdl.fit(train, median_train=train_all)
    offsets = mdl.calibrate(models, calib, alpha=1 - COVERAGE_TARGET)
    return {'train_all': train_all, 'train': train, 'calib': calib, 'models': models, 'offsets': offsets}


def run(leads=LEADS) -> dict:
    started = time.time()
    hourly, table, curves = prepare(leads)
    fitted = train_models(table, leads)
    models, offsets = fitted['models'], fitted['offsets']

    valid = table[table.time.between(*VALID) & table.lead_h.isin(leads)].copy()
    predicted = mdl.apply_bounds(mdl.predict(models, valid), offsets)
    valid = valid.merge(predicted[['time', 'turbine', 'lead_h', 'p50', 'p10', 'p90']],
                        on=['time', 'turbine', 'lead_h'])
    valid = add_baselines(valid, hourly, fitted['train_all'])

    rows = []
    for lead, group in valid.groupby('lead_h'):
        for scope, subset in (('все часы', group), ('без простоев', group[~group.curtailed])):
            for name, column in (('наше решение', 'p50'), ('кривая мощности', 'curve'),
                                 ('персистентность+климат', 'persistence_mix'),
                                 ('персистентность', 'persistence'), ('климатология', 'climatology')):
                row = metrics(subset.power, subset[column], subset.persistence)
                if row:
                    rows.append(dict(model=name, lead_h=int(lead), scope=scope, **row))
    scores = pd.DataFrame(rows)

    inside = valid[(valid.power >= valid.p10) & (valid.power <= valid.p90)].shape[0]
    coverage = inside / max(valid.power.notna().sum(), 1)
    # покрытие само по себе ничего не значит: коридор от 0 до 1 покроет всё. Смотрим и ширину
    width = float((valid.p90 - valid.p10).mean())

    test = table[table.time.between(*TEST) & table.lead_h.isin(leads)]
    forecast = mdl.apply_bounds(mdl.predict(models, test), offsets)

    return {'hourly': hourly, 'table': table, 'curves': curves, **fitted, 'valid': valid,
            'scores': scores, 'coverage': coverage, 'width': width, 'forecast': forecast,
            'seconds': time.time() - started}


def leakage_price(result: dict) -> pd.DataFrame:
    """Во что обошлась бы подмена честного архива прогнозов ноукастом (горизонт 0 ч)."""
    table = result['table']
    train = table[(table.time <= TRAIN_END) & table.power.notna()
                  & ~table.curtailed & (table.lead_h == 0)]
    if train.empty:
        return pd.DataFrame()
    models = mdl.fit(train)
    valid = table[table.time.between(*VALID) & (table.lead_h == 0)].copy()
    valid = valid.merge(mdl.predict(models, valid)[['time', 'turbine', 'lead_h', 'p50']],
                        on=['time', 'turbine', 'lead_h'])
    row = metrics(valid.power, valid.p50)
    row.update(model='ноукаст (утечка)', lead_h=0, scope='все часы')
    return pd.DataFrame([row])


if __name__ == '__main__':
    import sys

    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    result = run()
    print('\nВАЛИДАЦИЯ: ноябрь 2025 — январь 2026 (модель этих данных не видела)')
    table = result['scores'].pivot_table(index=['scope', 'model'], columns='lead_h',
                                         values=['nmae', 'skill']).round(3)
    print(table.to_string())
    print(f'\nпокрытие коридора P10–P90: {result["coverage"]:.1%}, средняя ширина {result["width"]:.3f}')
    print(f'прогноз на февраль: {len(result["forecast"])} строк')
    print(f'время прогона: {result["seconds"]:.1f} с')
    leak = leakage_price(result)
    if not leak.empty:
        honest = result['scores'].query('model == "наше решение" and scope == "все часы"').nmae.min()
        print(f'\nцена утечки: на ноукасте nMAE {leak.nmae.iloc[0]:.3f} против честных {honest:.3f}')
