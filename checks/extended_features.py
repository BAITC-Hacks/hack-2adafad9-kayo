"""Проверка изоляции расширенных признаков: python checks/extended_features.py."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
from pandas.testing import assert_frame_equal

from windagent import extended, model as mdl, weather as wx


def check():
    hours = pd.date_range('2025-01-01', periods=48, freq='h')
    weather = pd.DataFrame({'time': np.repeat(hours, 3), 'lead_h': np.tile([0, 24, 48], len(hours))})
    weather['wind_speed_100m'] = 5 + np.sin(np.arange(len(weather)))
    weather['wind_speed_10m'] = 3.0
    weather['wind_direction_100m'] = np.arange(len(weather)) * 17 % 360
    weather['temperature_2m'] = -5.0
    weather['surface_pressure'] = 950.0
    for member in wx.MEMBERS.values():
        for variable in wx.MEMBER_VARS:
            weather[f'{variable}_{member}'] = weather[variable]
    weather['power'] = np.arange(len(weather)) / len(weather)
    expected = extended.features(weather, 'T1')
    target = (weather.lead_h == 24) & (weather.time.dt.day == 1)
    perturbed = weather.copy()
    columns = [column for column in wx.BASE if column != 'time']
    columns += [f'{variable}_{member}' for member in wx.MEMBERS.values() for variable in wx.MEMBER_VARS]
    perturbed.loc[~target, columns] += 100
    perturbed['power'] = 999
    actual = extended.features(perturbed, 'T1')
    compared = list(extended.EXTRA_COLUMNS) + [name for name in extended.BASE_COLUMNS if name != 'curve']
    assert_frame_equal(expected.loc[target, compared], actual.loc[target, compared])
    block = extended.features(weather.loc[target].reset_index(drop=True), 'T1')
    assert_frame_equal(expected.loc[target, compared].reset_index(drop=True), block[compared])
    original_features, original_columns = mdl.features, mdl.FEATURE_COLUMNS
    try:
        with extended.enabled():
            assert len(mdl.FEATURE_COLUMNS) == 36
            raise RuntimeError('restore check')
    except RuntimeError:
        pass
    assert mdl.features is original_features and mdl.FEATURE_COLUMNS is original_columns
    assert np.isfinite(expected[list(extended.EXTRA_COLUMNS)]).all().all()
    print('PASS: no nowcast/other-day/other-lead/actual contamination; batch=issue; restoration on error')


if __name__ == '__main__':
    check()
