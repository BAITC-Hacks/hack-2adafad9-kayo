"""Сетка выпуска и возраст всех используемых погодных значений."""
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
from pandas.testing import assert_series_equal

from windagent import agent, asof, backtest as bt, weather as wx


def check():
    # Большая ошибка часа без baseline не должна менять skill на общей выборке.
    actual = pd.Series([0.0, 1.0, float('nan'), 1.0], index=[10, 20, 30, 40])
    predicted = pd.Series([0.25, 0.0, 0.0, float('nan')], index=actual.index)
    reference = pd.Series([1.0, float('nan'), 0.0, 0.0], index=actual.index)
    measured = bt.metrics(actual, predicted, reference)
    assert measured['skill'] == 0.75
    assert measured['nmae'] == 0.625 and measured['n'] == 2
    archive = asof.daily_ensemble(*asof.ARCHIVE, diagnostic=True)
    issued = archive[archive.lead_h.isin([24, 48])].copy()
    issued['issue_time'] = asof.issue_times(issued)
    lead = (issued.time - issued.issue_time) / pd.Timedelta(hours=1)
    assert lead[issued.lead_h == 24].between(1, 24).all()
    assert lead[issued.lead_h == 48].between(25, 48).all()
    reserve = (issued.issue_time - issued.weather_reference_time) / pd.Timedelta(hours=1)
    assert reserve.between(24, 47).all()
    assert (issued.weather_lead_h == issued.lead_h + 24).all()
    assert (archive.loc[archive.lead_h == 0, 'weather_source'] == 'diagnostic_nowcast').all()
    keys = ['time', 'lead_h']
    assert not archive.duplicated(keys).any()
    for model, member in wx.MEMBERS.items():
        old = wx.load(*asof.ARCHIVE, model).sort_values('time').reset_index(drop=True)
        day3 = asof.load_day3(*asof.ARCHIVE, model).sort_values('time').reset_index(drop=True)
        for lead, source, suffix in [(24, old, '_previous_day2'), (48, day3, '_previous_day3')]:
            target = archive[archive.lead_h == lead].sort_values('time').reset_index(drop=True)
            for variable in wx.MEMBER_VARS:
                assert_series_equal(target[f'{variable}_{member}'], source[f'{variable}{suffix}'], check_names=False)
    evaluation = issued[issued.time >= '2025-11-01']
    assert evaluation.wind_speed_100m.notna().all()
    first_release = asof.issue_times(pd.DataFrame({'time': [bt.VALID[0]], 'lead_h': [48]})).iloc[0]
    assert bt.TRAIN_END + pd.Timedelta(hours=1) <= first_release
    hourly = pd.DataFrame({'time': pd.date_range('2025-10-28', periods=120, freq='h'),
                           'power': 0.5, 'turbine': 'T1'})
    targets = issued[issued.time.between('2025-11-01', '2025-11-01 23:00')].assign(turbine='T1')
    baseline = bt.add_baselines(targets, hourly, hourly)
    assert (baseline.persistence_time < asof.issue_times(baseline)).all()
    for hour in (0, 12, 23):
        release = pd.Timestamp('2026-02-01') + pd.Timedelta(hours=hour)
        current = archive[archive.lead_h == 0]
        with patch.object(wx, 'ensemble', return_value=current):
            window = agent._live_window(release)
        assert len(window) == 48
        assert list((window.time - release) / pd.Timedelta(hours=1)) == list(range(1, 49))
        assert window.groupby('lead_h').size().to_dict() == {24: 24, 48: 24}
        assert window.weather_reference_time.eq(release).all()
    broken = archive.copy()
    broken.loc[broken.lead_h == 24, 'wind_speed_100m'] = float('nan')
    forecast, journal, _ = agent.run_cycle(pd.Timestamp('2026-02-01 23:00'),
                                          ctx={'weather': broken}, with_explain=False)
    assert forecast is None and journal[-1]['step'] == 'prepare' and journal[-1]['status'] == 'error'
    print(f'PASS: {len(issued)} rows; target 1..48h; reference reserve 24..47h; exact day2/day3 values')


if __name__ == '__main__':
    check()
