# -*- coding: utf-8 -*-
"""Данные турбин: часовая агрегация, простои, склейка двух машин.

Исходники — два CSV с шагом 10 минут за 11.03.2023 — 31.01.2026. Февраля 2026 в них нет: это и есть
горизонт прогноза.

Две тонкости, которые видно только в данных:
  * «ноль» закодирован как 0.01, а не 0, и вся мощность квантована до сотых;
  * полка у турбин разная — T1 упирается в 0.99, T2 в 0.97, поэтому нормировать их надо порознь.
"""
from pathlib import Path

import numpy as np
import pandas as pd

RAW = {
    'T1': Path(r'C:\Users\perne\Downloads\Dataset HackAlemAI для участников 11.03.2023-28.02.2026 - turbine 1.csv'),
    'T2': Path(r'C:\Users\perne\Downloads\Dataset HackAlemAI для участников 11.03.2023-28.02.2026 - turbine 2.csv'),
}
CACHE = Path(__file__).resolve().parent.parent / 'data' / 'turbines_hourly.parquet'

COLUMNS = {
    'Статистическое время': 'time',
    'Средняя скорость ветра(m/s)': 'ws_meas',
    'Нормализованная активная мощность': 'power',
    'Средняя температура окружающей среды(°C)': 'temp_meas',
}
IDLE = 0.011   # всё, что ниже — простой: реальный ноль в данных записан как 0.01
WORKING_WIND = 4.0   # при таком ветре турбина обязана выдавать мощность

# Метки времени в данных — местные, а не UTC. Проверено взаимной корреляцией измеренного ветра с
# прогнозным (checks/timealign.py): до перехода Казахстана на единый пояс пик на +6 ч, после — на +5.
TZ_SWITCH = pd.Timestamp('2024-03-01')
OFFSET_BEFORE, OFFSET_AFTER = 6, 5


def read_raw(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path, usecols=list(COLUMNS)).rename(columns=COLUMNS)
    frame['time'] = pd.to_datetime(frame['time'])
    frame['time'] = frame.time - pd.to_timedelta(
        np.where(frame.time < TZ_SWITCH, OFFSET_BEFORE, OFFSET_AFTER), unit='h')
    # ноль записан как 0.01 — если это не выправить, прогноз систематически завышен на процент
    frame['power'] = frame.power.where(frame.power > IDLE, 0.0)
    return frame.sort_values('time').reset_index(drop=True)


def to_hourly(frame: pd.DataFrame) -> pd.DataFrame:
    """Часовое среднее плюс признаки качества часа: сколько замеров и был ли простой."""
    frame = frame.set_index('time')
    idle = frame.power <= IDLE
    stopped_in_wind = idle & (frame.ws_meas >= WORKING_WIND)
    hourly = frame.resample('h').agg(
        power=('power', 'mean'),
        ws_meas=('ws_meas', 'mean'),
        temp_meas=('temp_meas', 'mean'),
        n_samples=('power', 'size'),
    )
    hourly['idle_share'] = idle.resample('h').mean()
    hourly['curtailed'] = stopped_in_wind.resample('h').mean() > 0.5
    return hourly.dropna(subset=['power']).reset_index()


def load(refresh: bool = False) -> pd.DataFrame:
    """Длинная таблица: turbine, time, power и признаки часа. Кешируется в parquet."""
    if CACHE.exists() and not refresh:
        return pd.read_parquet(CACHE)
    parts = []
    for name, path in RAW.items():
        hourly = to_hourly(read_raw(path))
        hourly['turbine'] = name
        # своя полка у каждой машины: мощность приводим к её собственному максимуму
        hourly['rated'] = hourly.power.quantile(0.999)
        parts.append(hourly)
    both = pd.concat(parts, ignore_index=True)
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    both.to_parquet(CACHE, index=False)
    return both


def clean_for_training(frame: pd.DataFrame) -> pd.DataFrame:
    """Часы, на которых честно учить физику: полные, без простоя и без обрезанных замеров."""
    good = (frame.n_samples >= 4) & (~frame.curtailed) & (frame.idle_share < 0.5)
    return frame[good].copy()


def fill_from_twin(frame: pd.DataFrame) -> pd.DataFrame:
    """Дыры одной турбины закрываем второй: они стоят в 338 м, мощности коррелируют 0.96.

    Заполненные часы помечаются, чтобы не выдавать их за собственные измерения."""
    wide = frame.pivot(index='time', columns='turbine', values='power')
    filled = []
    for turbine in wide.columns:
        twin = [c for c in wide.columns if c != turbine]
        if not twin:
            continue
        ratio = (wide[turbine] / wide[twin[0]]).replace([np.inf, -np.inf], np.nan).median()
        gap = wide[turbine].isna() & wide[twin[0]].notna()
        patch = pd.DataFrame({
            'time': wide.index[gap],
            'turbine': turbine,
            'power': wide.loc[gap, twin[0]] * ratio,
            'from_twin': True,
        })
        filled.append(patch)
    if not filled:
        return frame.assign(from_twin=False)
    return pd.concat([frame.assign(from_twin=False), pd.concat(filled, ignore_index=True)], ignore_index=True)


if __name__ == '__main__':
    import sys

    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    hourly = load(refresh=True)
    print(f'часов всего: {len(hourly)}, период: {hourly.time.min()} — {hourly.time.max()}')
    for name, group in hourly.groupby('turbine'):
        clean = clean_for_training(group)
        print(f'{name}: {len(group)} ч, чистых для обучения {len(clean)}, '
              f'простоев {group.curtailed.sum()}, полка {group.rated.iloc[0]:.2f}, '
              f'средняя мощность {group.power.mean():.3f}')
