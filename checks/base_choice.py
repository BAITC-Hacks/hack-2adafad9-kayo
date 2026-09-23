# -*- coding: utf-8 -*-
"""Выбор базовой модели погоды после того, как best_match сменил модель 01.10.2025.

Запрос к Open-Meteo без `models` отдаёт best_match — какую модель под ним подставить, решает
поставщик. В архиве до 01.10.2025 best_match совпадает с ICON час в час, после — ни разу, а
валидация и февраль целиком в новом режиме. Сравниваем три явных варианта на честных признаках
(скользящие окна внутри блока выпуска), nMAE без простоев на ноябре-январе:

  1. best_match как база, кривая на его ветре   — как было;
  2. ICON как база, кривая на ветре ICON;
  3. ICON как база, кривая на среднем по ансамблю ICON/GFS/ECMWF.

Выбор по стабильности источника, а не только по valid: у названной модели поставщик ничего не
подменяет. Итог прогона: best_match 0,166/0,182, ICON 0,172/0,187, ICON с кривой на среднем
0,174/0,189 (бустинг там почти не обгоняет кривую). Выбран ICON с кривой на ICON — второй по
valid, но единственный, где и база, и кривая сидят на одном названном источнике; среднее и разброс
ансамбля остаются признаками бустинга.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from windagent import backtest as bt, model as mdl, weather as wx

VARIANTS = (
    ('best_match, кривая на нём', '', 'ws_norm'),
    ('ICON, кривая на ICON', 'icon_seamless', 'ws_norm'),
    ('ICON, кривая на среднем', 'icon_seamless', 'ws_ens'),
)

for label, base, curve_wind in VARIANTS:
    wx.BASE_MODEL, mdl.CURVE_WIND = base, curve_wind
    result = bt.run()
    scores = result['scores'][result['scores'].scope == 'без простоев'].set_index(['model', 'lead_h'])
    ours, curve = scores.loc[bt.MODEL_ONLY], scores.loc['кривая мощности']
    print(f'{label:<28} nMAE 24/48 {ours.nmae[24]:.4f}/{ours.nmae[48]:.4f}, смещение {ours.bias[24]:+.3f}, '
          f'скилл {ours.skill[24]:.3f}/{ours.skill[48]:.3f}; кривая {curve.nmae[24]:.4f}/{curve.nmae[48]:.4f} '
          f'(смещение {curve.bias[24]:+.3f}); покрытие {result["coverage"]:.1%}, ширина {result["width"]:.3f}')
