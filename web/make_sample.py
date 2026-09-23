# -*- coding: utf-8 -*-
"""Тестовые данные для дашборда: правдоподобный февраль по контракту data.json.

Нужен только для проверки страницы без прогона пайплайна. Результат — data.sample.json.
"""
import json
import math
import random
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
random.seed(7)

START = datetime(2026, 2, 1)
HOURS = 28 * 24
TURBINES = ('T1', 'T2')
LEADS = (24, 48)


def synoptic(hour: int) -> float:
    """Медленная погодная волна плюс слабый суточный ход — как ветер в феврале."""
    slow = 0.45 + 0.30 * math.sin(hour / 38.0) + 0.18 * math.sin(hour / 11.0 + 1.3)
    daily = 0.04 * math.sin((hour % 24) / 24 * 2 * math.pi - 1.0)
    return max(0.02, min(0.98, slow + daily))


def build():
    forecast = []
    for turbine in TURBINES:
        bias = 0.0 if turbine == 'T1' else -0.02   # вторая машина стабильно чуть ниже
        for lead in LEADS:
            spread = 0.11 if lead == 24 else 0.17  # на двое суток коридор шире
            for i in range(HOURS):
                t = START + timedelta(hours=i)
                base = synoptic(i) + bias
                p50 = max(0.0, min(1.0, base + random.gauss(0, 0.02)))
                lo = max(0.0, p50 - spread * (0.6 + 0.8 * random.random()))
                hi = min(1.0, p50 + spread * (0.6 + 0.8 * random.random()))
                # факт известен только за первые две недели — дальше горизонт прогноза
                actual = None
                if i < HOURS // 2:
                    actual = max(0.0, min(1.0, p50 + random.gauss(0, 0.06 if lead == 24 else 0.09)))
                    if random.random() < 0.01:
                        actual = 0.01   # простой
                forecast.append({
                    'time': t.strftime('%Y-%m-%dT%H:%M'),
                    'turbine': turbine,
                    'lead_h': lead,
                    'p10': round(lo, 3),
                    'p50': round(p50, 3),
                    'p90': round(hi, 3),
                    'actual': None if actual is None else round(actual, 3),
                    'curve': round(max(0.0, min(1.0, base + random.gauss(0, 0.03))), 3),
                })

    metrics = []
    for lead, k in ((24, 1.0), (48, 1.35)):
        metrics += [
            {'model': 'agent', 'lead_h': lead, 'nmae': round(0.089 * k, 3), 'nrmse': round(0.142 * k, 3),
             'bias': round(-0.004 * k, 3), 'skill': round(0.41 / k, 3)},
            {'model': 'curve', 'lead_h': lead, 'nmae': round(0.104 * k, 3), 'nrmse': round(0.163 * k, 3),
             'bias': round(0.012 * k, 3), 'skill': round(0.32 / k, 3)},
            {'model': 'climatology', 'lead_h': lead, 'nmae': round(0.171 * k, 3), 'nrmse': round(0.231 * k, 3),
             'bias': round(0.021 * k, 3), 'skill': round(0.05 / k, 3)},
            {'model': 'persistence', 'lead_h': lead, 'nmae': round(0.163 * k, 3), 'nrmse': round(0.241 * k, 3),
             'bias': round(0.002 * k, 3), 'skill': 0.0},
        ]

    steps = [
        ('weather', 'ok', 'архивный прогноз получен: 48 часов, модель icon_seamless'),
        ('prepare', 'ok', 'ряды полные, единицы сверены, пропусков нет'),
        ('forecast', 'ok', 'посчитан прогноз на 48 часов, две турбины'),
        ('verify', 'ok', 'скилл над персистентностью 0.41 — публикуем'),
        ('explain', 'ok', 'выработка растёт к вечеру: ветер 9–11 м/с с востока'),
    ]
    log = []
    for day in range(28):
        stamp = START + timedelta(days=day, hours=6)
        for n, (step, status, text) in enumerate(steps):
            entry_status, entry_text = status, text
            if day == 9 and step == 'weather':
                entry_status, entry_text = 'retry', 'источник не ответил, повтор через 2 с'
            if day == 9 and step == 'verify':
                entry_status, entry_text = 'fallback', 'скилл 0.02 ниже порога — откат на кривую мощности'
            if day == 17 and step == 'prepare':
                entry_status, entry_text = 'warn', 'дыра 3 часа в прогнозе, заполнено интерполяцией'
            log.append({
                'ts': (stamp + timedelta(minutes=n * 4)).strftime('%Y-%m-%dT%H:%M'),
                'step': step,
                'status': entry_status,
                'text': entry_text,
            })

    return {
        'generated_at': datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
        'site': {'name': 'ВЭС, Шелекский коридор', 'lat': 43.64515, 'lon': 78.535604},
        'period': {'start': '2026-02-01T00:00', 'end': '2026-02-28T23:00'},
        'forecast': forecast,
        'metrics': metrics,
        'agent_log': log,
        'economics': {'penalty_per_mwh': 12000, 'capacity_mw': 2.5, 'monthly_loss': 1840000,
                      'baseline_loss': 3120000, 'currency': '₸'},
    }


if __name__ == '__main__':
    data = build()
    (HERE / 'data.sample.json').write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    print(f'записано: {len(data["forecast"])} точек прогноза, {len(data["agent_log"])} шагов журнала')
