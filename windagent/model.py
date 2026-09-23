# -*- coding: utf-8 -*-
"""Прогноз выработки: физическая кривая мощности плюс поправка бустингом.

Кривая переводит прогнозную скорость ветра в мощность — это физика, она же работает как запасной
вариант, если модель окажется хуже. Бустинг учит только остаток: систематические ошибки прогноза
погоды, влияние плотности воздуха, направления и сезона.

Всё считается на архивных прогнозах (что было известно за 24 и 48 часов), поэтому метрики честные.

Признаки считаются из блока выпуска — 24 часа одного горизонта и одних календарных суток: ровно
то, что агент получает на руки в день выпуска. Скользящие окна не выходят за блок, поэтому в
обучении и в поставке признаки совпадают один в один, а ноукаст соседнего горизонта в них не
попадает.

Базовая погода — явно названная модель ICON; поверх неё ансамбль ICON/GFS/ECMWF: среднее по трём
ближе к измеренному ветру, чем любая одна, а разброс говорит, насколько прогнозу можно верить в
этот час. Кривая мощности снимается с ветра ICON. Что сравнивалось (checks/base_choice.py, честные
окна, nMAE 24/48 ч без простоев на ноябре 2025 — январе 2026):
  best_match, кривая на нём      0,166 / 0,182 — лучше всех, но какая модель под best_match, решает
                                                 Open-Meteo: 01.10.2025 он её сменил (weather.py);
  ICON, кривая на ICON           0,172 / 0,187 — выбран: источник назван и не подменяется;
  ICON, кривая на среднем        0,174 / 0,189 — бустинг почти не обгоняет кривую (0,175 / 0,189).
Смещение +0,07 на валидации у обоих явных вариантов — все три модели после октября 2025 дают ветер
выше прежнего относительно выработки; такой дрейф и ловит рефлексия агента.
"""
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

from . import weather as wx

R_DRY = 287.05  # газовая постоянная сухого воздуха, Дж/(кг·К)
QUANTILES = {'p10': 0.1, 'p90': 0.9}

# Ансамбль погодных моделей: скорость по каждой, среднее и разброс как мера неопределённости.
# Флаг оставлен, чтобы одной строкой вернуться к одиночной модели и повторить сравнение.
USE_ENSEMBLE = True
CURVE_WIND = 'ws_norm'   # какой ветер идёт в кривую мощности: базовая модель (ICON)


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


def block_mean(frame: pd.DataFrame, column: str, window: int) -> pd.Series:
    """Скользящее среднее внутри блока выпуска: один горизонт × одни календарные сутки.

    Окно не должно выходить за блок. В таблице обучения соседняя строка того же часа — другой
    горизонт, и окно по строкам подмешивало в признак ноукаст (nMAE на валидации 0,156 вместо
    честных 0,17); соседние сутки агент в день выпуска ещё не видит. Блок — ровно то, что агент
    получает на руки, поэтому одна функция даёт одинаковые признаки в обучении и в поставке."""
    ordered = frame.sort_values(['lead_h', 'time'])
    rolled = (ordered.groupby([ordered.lead_h, ordered.time.dt.normalize()], sort=False)[column]
              .rolling(window, center=True, min_periods=1).mean())
    return rolled.reset_index(level=[0, 1], drop=True).reindex(frame.index)


def features(weather: pd.DataFrame, turbine: str) -> pd.DataFrame:
    """Признаки из прогноза погоды. Ничего из будущего факта здесь нет."""
    f = weather.reset_index(drop=True)
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
        f[f'ws_roll{window}'] = block_mean(f, 'ws_norm', window)
    if USE_ENSEMBLE:
        for short in wx.MEMBERS.values():
            rho = air_density(f[f'temperature_2m_{short}'], f[f'surface_pressure_{short}'])
            f[f'ws_{short}'] = f[f'wind_speed_100m_{short}'] * (rho / 1.225) ** (1 / 3)
        members = f[[f'ws_{short}' for short in wx.MEMBERS.values()]]
        # среднее по моделям ближе к факту, чем любая из них; разброс — готовая мера неопределённости
        # для квантилей. Сами члены по отдельности в признаках не нужны: только шумят
        f['ws_ens'] = members.mean(axis=1)
        f['ws_spread'] = members.std(axis=1, ddof=0)
        for window in (3, 6):
            f[f'ws_ens_roll{window}'] = block_mean(f, 'ws_ens', window)
    return f


BASE_FEATURES = ['ws_norm', 'ws3', 'shear', 'dir_sin', 'dir_cos', 'rho', 'temperature_2m',
                 'hour', 'month', 'lead_h', 'turbine_id', 'ws_roll3', 'ws_roll6', 'curve']
ENSEMBLE_FEATURES = ['ws_ens', 'ws_spread', 'ws_ens_roll3', 'ws_ens_roll6']
FEATURE_COLUMNS = BASE_FEATURES + (ENSEMBLE_FEATURES if USE_ENSEMBLE else [])


def build_table(hourly: pd.DataFrame, weather_tall: pd.DataFrame, curves: dict) -> pd.DataFrame:
    """Собрать обучающую таблицу: час × турбина × горизонт прогноза."""
    parts = []
    for turbine, group in hourly.groupby('turbine'):
        joined = weather_tall.merge(group[['time', 'power', 'curtailed', 'n_samples']], on='time', how='left')
        joined['curtailed'] = joined.curtailed.eq(True)
        table = features(joined, turbine)
        table['curve'] = apply_curve(curves[turbine], table[CURVE_WIND])
        table['turbine'] = turbine
        parts.append(table)
    return pd.concat(parts, ignore_index=True)


def _usable(frame: pd.DataFrame):
    rows = frame[frame.power.notna() & frame.curve.notna()]
    return rows[FEATURE_COLUMNS], rows.power - rows.curve


def fit(train: pd.DataFrame, median_train: pd.DataFrame | None = None) -> dict:
    """Бустинг на остаток кривой плюс две квантильные модели для коридора.

    Квантили учатся на `train`, из которого вырезан калибровочный срез: иначе конформная поправка
    считается на часах, которые модель уже видела. Медиане калибровка не нужна, поэтому ей отдают
    весь ряд `median_train` — лишний месяц данных даёт около 0,005 nMAE на валидации."""
    X, residual = _usable(train)
    X_mid, residual_mid = _usable(median_train) if median_train is not None else (X, residual)
    models = {'mid': HistGradientBoostingRegressor(max_iter=400, learning_rate=0.06,
                                                   max_depth=6, random_state=42).fit(X_mid, residual_mid)}
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


def calibrate(models: dict, calib: pd.DataFrame, alpha: float = 0.2) -> dict:
    """Конформная поправка коридора: раздвинуть P10/P90 так, чтобы фактическое покрытие ≈ 1-α.

    Отдельно на пару (турбина, горизонт): у 48 ч разброс шире, чем у 24 ч, и одной поправкой обе
    группы не выровнять. CQR даёт покрытие с гарантией на будущем при условии обменяемости.
    """
    usable = calib[calib.power.notna() & calib.curve.notna()]
    if usable.empty:
        return {}
    pred = predict(models, usable)
    joined = usable[['time', 'turbine', 'lead_h', 'power']].merge(
        pred[['time', 'turbine', 'lead_h', 'p10', 'p90']], on=['time', 'turbine', 'lead_h'])
    offsets = {}
    for (turbine, lead), group in joined.groupby(['turbine', 'lead_h']):
        scores = np.maximum(group.p10 - group.power, group.power - group.p90).values
        if len(scores) < 30:
            offsets[(turbine, int(lead))] = 0.0
            continue
        offsets[(turbine, int(lead))] = float(np.quantile(scores, 1 - alpha))
    return offsets


def apply_bounds(pred: pd.DataFrame, offsets: dict) -> pd.DataFrame:
    """Раздвинуть квантили на конформную поправку и заодно починить порядок p10 ≤ p50 ≤ p90.

    Квантильные модели учатся независимо, монотонность не гарантирована — на границах диапазона
    (мощность около 0 или 1) тройка местами перехлёстывается. Пересортировка построчно —
    стандартный приём quantile crossing.

    Границы подрезаются по медиане, а не сортируются вместе с ней: отступ бывает отрицательным
    (коридор сужается), и сортировка тогда подменяла p50 значением границы — nMAE рос на 0,002."""
    out = pred.copy()
    if offsets:
        q = np.array([offsets.get((t, int(l)), 0.0) for t, l in zip(pred.turbine, pred.lead_h)])
        out['p10'] = np.clip(pred.p10 - q, 0, 1)
        out['p90'] = np.clip(pred.p90 + q, 0, 1)
    out['p10'] = np.minimum(out.p10, out.p50)
    out['p90'] = np.maximum(out.p90, out.p50)
    return out
