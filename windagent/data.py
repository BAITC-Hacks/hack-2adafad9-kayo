# -*- coding: utf-8 -*-
"""Данные турбин: часовая агрегация, простои, склейка двух машин.

Исходники — два CSV с шагом 10 минут за 11.03.2023 — 31.01.2026. Февраля 2026 в них нет: это и есть
горизонт прогноза. Часовой срез лежит в репозитории (`data/turbines_hourly.parquet`), поэтому сами
CSV нужны только для пересборки кеша: положить их в `data/raw/` или указать папку в `WIND_RAW_DIR`.

Две тонкости, которые видно только в данных:
  * «ноль» закодирован как 0.01, а не 0, и вся мощность квантована до сотых;
  * метки времени местные, а не UTC, причём сдвиг менялся вместе с часовым поясом Казахстана.
"""
import os
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = Path(os.environ.get('WIND_RAW_DIR', ROOT / 'data' / 'raw'))
RAW_FILES = {
    'T1': 'Dataset HackAlemAI для участников 11.03.2023-28.02.2026 - turbine 1.csv',
    'T2': 'Dataset HackAlemAI для участников 11.03.2023-28.02.2026 - turbine 2.csv',
}
CACHE = ROOT / 'data' / 'turbines_hourly.parquet'

COLUMNS = {
    'Статистическое время': 'time',
    'Средняя скорость ветра(m/s)': 'ws_meas',
    'Нормализованная активная мощность': 'power',
    'Средняя температура окружающей среды(°C)': 'temp_meas',
}
IDLE = 0.011   # всё, что ниже — простой: реальный ноль в данных записан как 0.01
WORKING_WIND = 4.0   # при таком ветре турбина обязана выдавать мощность
FULL_HOUR = 4        # минимум десятиминутных замеров, чтобы часовое среднее считалось надёжным

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
    missing = [name for name in RAW_FILES.values() if not (RAW_DIR / name).exists()]
    if missing:
        raise FileNotFoundError(
            f'нет кеша {CACHE} и исходных CSV организаторов: положите их в {RAW_DIR} '
            f'или задайте папку в WIND_RAW_DIR. Не найдены: ' + '; '.join(missing))
    parts = []
    for name, file in RAW_FILES.items():
        hourly = to_hourly(read_raw(RAW_DIR / file))
        hourly['turbine'] = name
        parts.append(hourly)
    both = pd.concat(parts, ignore_index=True)
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    both.to_parquet(CACHE, index=False)
    return both


def clean_for_training(frame: pd.DataFrame) -> pd.DataFrame:
    """Часы, на которых честно учить: полные (не меньше FULL_HOUR замеров) и без простоя.

    Штиль, когда машина стоит из-за слабого ветра, — не брак, а физика: такие часы остаются."""
    return frame[(frame.n_samples >= FULL_HOUR) & ~frame.curtailed]


if __name__ == '__main__':
    import sys

    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    hourly = load(refresh=True)
    print(f'часов всего: {len(hourly)}, период: {hourly.time.min()} — {hourly.time.max()}')
    for name, group in hourly.groupby('turbine'):
        clean = clean_for_training(group)
        print(f'{name}: {len(group)} ч, чистых для обучения {len(clean)}, простоев {group.curtailed.sum()}, '
              f'неполных часов {(group.n_samples < FULL_HOUR).sum()}, средняя мощность {group.power.mean():.3f}')
