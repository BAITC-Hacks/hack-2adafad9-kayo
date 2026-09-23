# -*- coding: utf-8 -*-
"""Насколько выбор окна конформной калибровки — подгонка под валидацию.

Окно в backtest.CALIB_WINDOW выбрано правилом «тот же сезон год назад», без перебора. Здесь для
справки покрытие и ширина коридора на ноябре-январе при соседних окнах: последний месяц перед
валидацией и три варианта прошлой зимы. Квантильные модели каждый раз переучиваются без окна,
медиана — на всём ряду, как в train_models.
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from windagent import backtest as bt, data as turbines, model as mdl

WINDOWS = {
    'октябрь 2025': ('2025-10-01', '2025-10-31 23:00'),
    'ноябрь–январь 2024/25': ('2024-11-01', '2025-01-31 23:00'),
    'ноябрь–февраль 2024/25': ('2024-11-01', '2025-02-28 23:00'),
    'декабрь–февраль 2024/25': ('2024-12-01', '2025-02-28 23:00'),
}

hourly, table, curves = bt.prepare()
train_all = turbines.clean_for_training(
    table[(table.time <= bt.TRAIN_END) & table.curve.notna() & table.lead_h.isin(bt.LEADS)])
valid = table[table.time.between(*bt.VALID) & table.lead_h.isin(bt.LEADS)]

for label, (start, end) in WINDOWS.items():
    in_calib = train_all.time.between(pd.Timestamp(start), pd.Timestamp(end))
    models = mdl.fit(train_all[~in_calib], median_train=train_all)
    offsets = mdl.calibrate(models, train_all[in_calib], alpha=1 - bt.COVERAGE_TARGET)
    predicted = mdl.apply_bounds(mdl.predict(models, valid), offsets)
    joined = valid[['time', 'turbine', 'lead_h', 'power']].merge(predicted, on=['time', 'turbine', 'lead_h'])
    coverage, width = bt.corridor(joined)
    chosen = ' <- CALIB_WINDOW' if (pd.Timestamp(start), pd.Timestamp(end)) == bt.CALIB_WINDOW else ''
    print(f'{label:<26} покрытие {coverage:.1%}, ширина {width:.3f}, '
          f'nMAE {(joined.power - joined.p50).abs().mean():.4f}{chosen}')
