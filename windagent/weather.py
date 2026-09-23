# -*- coding: utf-8 -*-
"""Архивные прогнозы погоды — то, что было известно за 24 и 48 часов до момента.

Кейс требует прогнозировать на архивных ПРОГНОЗАХ, а не на фактической погоде. У Open-Meteo для
этого есть Previous Runs API: колонки `*_previous_day1` и `*_previous_day2` — значения из прогонов,
выпущенных за сутки и за двое до валидного времени.

Соседний historical-forecast-api для этого не годится: он склеен из первых часов каждого прогона
(горизонт 0–3 ч), то есть почти факт. Модель, обученная на нём, покажет красивые метрики и провалится
на реальном горизонте.

Обе турбины стоят в 338 м друг от друга и попадают в одну ячейку погодной сетки, поэтому погода
качается один раз на площадку.
"""
import io
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pandas as pd

API = 'https://previous-runs-api.open-meteo.com/v1/forecast'
SITE = {'latitude': 43.645150, 'longitude': 78.535604}  # Шелекский коридор, Алматинская область

# базовые переменные и их версии «как предсказывали за N суток»
BASE = ['wind_speed_100m', 'wind_direction_100m', 'wind_speed_10m', 'temperature_2m', 'surface_pressure']
HORIZONS = ('', '_previous_day1', '_previous_day2')
HOURLY = [name + suffix for name in BASE for suffix in HORIZONS]

# Базовая модель названа явно. Запрос без `models` отдаёт best_match, а какая модель под ним, решает
# Open-Meteo: в архиве она сменилась 01.10.2025 — до этой даты best_match совпадал с ICON час в час,
# после не совпадает ни разу. Валидация и февраль оказались бы в новом режиме, а кривая мощности
# снята в старом. От ICON берутся направление, ветер на 10 м, температура и давление; скорость для
# кривой и модели — среднее по трём членам ансамбля (model.py).
BASE_MODEL = 'icon_seamless'
# Члены ансамбля: три независимых глобальных модели, их среднее и разброс идут в признаки. У ECMWF
# архив начинается на три недели позже остальных — в эти часы его колонки пустые.
MEMBERS = {'icon_seamless': 'icon', 'gfs_seamless': 'gfs', 'ecmwf_ifs025': 'ecmwf'}
MEMBER_VARS = ['wind_speed_100m', 'temperature_2m', 'surface_pressure']

CACHE = Path(__file__).resolve().parent.parent / 'data' / 'weather'


def _get(params, tries=3):
    url = API + '?' + urllib.parse.urlencode(params)
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                return json.load(response)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            if attempt == tries - 1:
                raise
            time.sleep(2 * (attempt + 1))


def fetch(start: str, end: str, models: str = '') -> pd.DataFrame:
    """Часовой ряд прогнозов за период. models — пусто (лучшая модель) или список через запятую."""
    params = dict(SITE, start_date=start, end_date=end, hourly=','.join(HOURLY),
                  wind_speed_unit='ms', timezone='UTC')
    if models:
        params['models'] = models
    hourly = _get(params)['hourly']
    frame = pd.DataFrame(hourly)
    frame['time'] = pd.to_datetime(frame['time'])
    return frame


def load(start: str, end: str, models: str = '', refresh: bool = False) -> pd.DataFrame:
    """То же, но через кеш в parquet: после первой выкачки всё работает без сети."""
    CACHE.mkdir(parents=True, exist_ok=True)
    name = f'prev_runs_{start}_{end}' + (f'_{models.replace(",", "-")}' if models else '') + '.parquet'
    path = CACHE / name
    if path.exists() and not refresh:
        return pd.read_parquet(path)
    frame = fetch(start, end, models)
    frame.to_parquet(path, index=False)
    return frame


def tidy(frame: pd.DataFrame) -> pd.DataFrame:
    """Разложить широкую таблицу в длинную: одна строка — час и горизонт прогноза (0, 24, 48)."""
    parts = []
    for suffix, lead in zip(HORIZONS, (0, 24, 48)):
        columns = {name + suffix: name for name in BASE if name + suffix in frame.columns}
        if len(columns) < len(BASE):
            continue
        part = frame[['time'] + list(columns)].rename(columns=columns)
        part['lead_h'] = lead
        parts.append(part)
    tall = pd.concat(parts, ignore_index=True)
    return tall.sort_values(['time', 'lead_h']).reset_index(drop=True)


def ensemble(start: str, end: str, live: bool = False) -> pd.DataFrame:
    """Длинная таблица базовой модели плюс колонки членов ансамбля: `wind_speed_100m_icon` и т. п.

    live — тянуть свежий прогноз мимо кеша (для выпуска на ближайшие сутки)."""
    source = fetch if live else load
    tall = tidy(source(start, end, BASE_MODEL))
    for name, short in MEMBERS.items():
        member = tidy(source(start, end, name))[['time', 'lead_h'] + MEMBER_VARS]
        member = member.rename(columns={var: f'{var}_{short}' for var in MEMBER_VARS})
        tall = tall.merge(member, on=['time', 'lead_h'], how='left')
    return tall


if __name__ == '__main__':
    import sys

    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    # весь нужный период: с начала архива Previous Runs до конца тестового февраля
    whole = load('2024-02-17', '2026-02-28', BASE_MODEL)
    print(f'часов: {len(whole)}, период: {whole.time.min()} — {whole.time.max()}')
    tall = ensemble('2024-02-17', '2026-02-28')
    for lead, group in tall.groupby('lead_h'):
        filled = {'база': group.wind_speed_100m.notna().sum()}
        filled.update({short: group[f'wind_speed_100m_{short}'].notna().sum() for short in MEMBERS.values()})
        print(f'  горизонт {lead:>2} ч из {len(group)}: заполнено ' + ', '.join(f'{k} {v}' for k, v in filled.items()))
