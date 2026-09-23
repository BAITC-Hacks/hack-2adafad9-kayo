# -*- coding: utf-8 -*-
"""Прогноз выработки: физическая кривая мощности плюс поправка бустингом.

Кривая переводит прогнозную скорость ветра в мощность — это физика, она же работает как запасной
вариант, если модель окажется хуже. Бустинг учит только остаток: систематические ошибки прогноза
погоды, влияние плотности воздуха, направления и сезона.

Всё считается на архивных прогнозах (что было известно за 24 и 48 часов), поэтому метрики честные.
"""
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

R_DRY = 287.05  # газовая постоянная сухого воздуха, Дж/(кг·К)
QUANTILES = {'p10': 0.1, 'p90': 0.9}


def power_curve(clean: pd.DataFrame, wind_col: str = 'ws_norm', step: float = 0.5) -> pd.DataFrame:
    """Эмпирическая кривая: медиана мощности по бинам скорости.

    Строится на ПРОГНОЗНОЙ скорости, а не на измеренной. Прогноз сглажен по сравнению с показаниями
    анемометра, и если прогонять его через кривую, снятую с измерений, выпуклость кривой даёт
    систематическое смещение. Кривая по измерениям остаётся только для диагностики."""
    bins = np.arange(0, clean[wind_col].max() + step, step)
    binned = clean.assign(bin=pd.cut(clean[wind_col], bins, labels=bins[:-1] + step / 2))
    curve = binned.groupby('bin', observed=True).power.median().reset_index()
    curve['bin'] = curve['bin'].astype(float)
    curve = curve.dropna().sort_values('bin')
    # мощность по ветру не убывает — чиним редкие провалы от простоев, попавших в бин
    curve['power'] = np.maximum.accumulate(curve.power.values)
    return curve.rename(columns={'bin': 'ws'})


def apply_curve(curve: pd.DataFrame, wind: pd.Series) -> np.ndarray:
    return np.interp(wind, curve.ws, curve.power, left=curve.power.iloc[0], right=curve.power.iloc[-1])


def air_density(temp_c: pd.Series, pressure_hpa: pd.Series) -> pd.Series:
    return (pressure_hpa * 100) / (R_DRY * (temp_c + 273.15))


def features(weather: pd.DataFrame, turbine: str) -> pd.DataFrame:
    """Признаки из прогноза погоды. Ничего из будущего факта здесь нет."""
    f = weather.copy()
    f['rho'] = air_density(f.temperature_2m, f.surface_pressure)
    # нормировка скорости по плотности (IEC 61400-12-1): холодный воздух плотнее, мощность выше
    f['ws_norm'] = f.wind_speed_100m * (f.rho / 1.225) ** (1 / 3)
    f['ws3'] = f.ws_norm ** 3
    f['shear'] = (f.wind_speed_100m / f.wind_speed_10m.replace(0, np.nan)).fillna(1.0)
    f['dir_sin'] = np.sin(np.radians(f.wind_direction_100m))
    f['dir_cos'] = np.cos(np.radians(f.wind_direction_100m))
    f['hour'] = f.time.dt.hour
    f['month'] = f.time.dt.month
    f['turbine_id'] = 0 if turbine == 'T1' else 1
    # сглаживание гасит фазовый сдвиг прогноза: ветер часто приходит на час раньше или позже
    for window in (3, 6):
        f[f'ws_roll{window}'] = f.ws_norm.rolling(window, center=True, min_periods=1).mean()
    return f


FEATURE_COLUMNS = ['ws_norm', 'ws3', 'shear', 'dir_sin', 'dir_cos', 'rho', 'temperature_2m',
                   'hour', 'month', 'lead_h', 'turbine_id', 'ws_roll3', 'ws_roll6', 'curve']


def build_table(hourly: pd.DataFrame, weather_tall: pd.DataFrame, curves: dict) -> pd.DataFrame:
    """Собрать обучающую таблицу: час × турбина × горизонт прогноза."""
    parts = []
    for turbine, group in hourly.groupby('turbine'):
        joined = weather_tall.merge(group[['time', 'power', 'curtailed', 'n_samples']], on='time', how='left')
        table = features(joined, turbine)
        table['curve'] = apply_curve(curves[turbine], table.ws_norm)
        table['turbine'] = turbine
        parts.append(table)
    return pd.concat(parts, ignore_index=True)


def fit(train: pd.DataFrame) -> dict:
    """Бустинг на остаток кривой плюс две квантильные модели для коридора."""
    usable = train[train.power.notna() & train.curve.notna()]
    X = usable[FEATURE_COLUMNS]
    residual = usable.power - usable.curve
    models = {'mid': HistGradientBoostingRegressor(max_iter=400, learning_rate=0.06,
                                                   max_depth=6, random_state=42).fit(X, residual)}
    for name, q in QUANTILES.items():
        models[name] = HistGradientBoostingRegressor(loss='quantile', quantile=q, max_iter=250,
                                                     learning_rate=0.08, max_depth=6,
                                                     random_state=42).fit(X, residual)
    return models


def predict(models: dict, table: pd.DataFrame) -> pd.DataFrame:
    X = table[FEATURE_COLUMNS]
    out = table[['time', 'turbine', 'lead_h', 'curve']].copy()
    for name, model in models.items():
        out[name] = np.clip(table.curve + model.predict(X), 0, 1)
    return out.rename(columns={'mid': 'p50'})
