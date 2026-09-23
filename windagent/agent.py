# -*- coding: utf-8 -*-
"""Агентный цикл: собрать погоду, проверить, посчитать, сверить с базовой линией, объяснить.

Числа считает только код. Языковая модель планирует объяснение и решает, на что смотреть человеку,
но ни одна цифра в отчёте не приходит от неё. Между расчётом и публикацией стоит шлюз качества:
если на последнем окне с фактом решение проигрывает персистентности, агент откатывается на
физическую кривую мощности и пишет об этом в журнал.

Бэктест февраля — тот же цикл, прокрученный 28 раз: на каждый день берётся ровно то, что было
известно за сутки и за двое.
"""
import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from . import backtest as bt
from . import llm
from . import model as mdl
from . import weather as wx

REFLECT_DAYS = 7          # окно свежего факта, по которому агент подкручивает смещение

ARCHIVE = ('2024-02-17', '2026-02-28')
VERIFY_DAYS = 14          # окно, на котором шлюз качества сверяет решение с базовой линией
RERUN_WIND_DELTA = 1.0    # м/с: насколько должен измениться прогноз ветра, чтобы пересчитывать
JOURNAL_PATH = Path(__file__).resolve().parent.parent / 'out' / 'agent_log.json'

_CONTEXT = None


def _note(journal, step, status, text, started):
    entry = {'ts': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
             'step': step, 'status': status, 'text': text,
             'duration_ms': int((time.perf_counter() - started) * 1000)}
    journal.append(entry)
    return entry


def context(refresh: bool = False) -> dict:
    """Данные, кривые, обученные модели и конформная поправка коридора. Считается один раз."""
    global _CONTEXT
    if _CONTEXT is not None and not refresh:
        return _CONTEXT
    hourly, table, curves = bt.prepare()
    good = (table.power.notna() & table.curve.notna() & (~table.curtailed.fillna(False))
            & table.lead_h.isin(bt.LEADS))
    train_all = table[(table.time <= bt.TRAIN_END) & good]
    calib_start = bt.TRAIN_END - pd.Timedelta(days=bt.CALIB_DAYS)
    train = train_all[train_all.time <= calib_start]
    calib = train_all[train_all.time > calib_start]
    models = mdl.fit(train)
    offsets = mdl.calibrate(models, calib, alpha=1 - bt.COVERAGE_TARGET)
    _CONTEXT = {'hourly': hourly, 'table': table, 'curves': curves,
                'train': train, 'calib': calib, 'models': models, 'offsets': offsets,
                'turbines': sorted({name for name, _ in curves})}
    return _CONTEXT


# ---------------------------------------------------------------- шаги цикла

def collect(issue_time: pd.Timestamp, journal: list, live: bool = False) -> pd.DataFrame:
    """Прогноз погоды, доступный на момент выпуска: сутки вперёд с горизонта 24 ч, ещё сутки с 48 ч."""
    started = time.perf_counter()
    source = 'архив прогнозов'
    if live:
        today = dt.date.today()
        try:
            frame = wx.fetch(today.isoformat(), (today + dt.timedelta(days=2)).isoformat())
            source = 'живой прогноз Open-Meteo'
        except Exception as err:  # сеть на площадке может лежать — не повод падать
            _note(journal, 'collect', 'degraded', f'живой прогноз недоступен ({err}), беру кеш', started)
            frame = wx.load(*ARCHIVE)
            source = 'кеш архива'
    else:
        frame = wx.load(*ARCHIVE)

    tall = wx.tidy(frame)
    day1 = issue_time.normalize() + pd.Timedelta(days=1)
    day2 = issue_time.normalize() + pd.Timedelta(days=2)
    window = pd.concat([
        tall[(tall.lead_h == 24) & (tall.time.dt.normalize() == day1)],
        tall[(tall.lead_h == 48) & (tall.time.dt.normalize() == day2)],
    ]).sort_values('time').reset_index(drop=True)
    _note(journal, 'collect', 'ok',
          f'{source}: {len(window)} часов на {day1.date()} — {day2.date()}, '
          f'горизонты {sorted(int(x) for x in window.lead_h.unique())} ч', started)
    return window


def prepare(window: pd.DataFrame, journal: list) -> pd.DataFrame:
    """Проверки ряда до расчёта: полнота, единицы, абсурдные значения."""
    started = time.perf_counter()
    problems = []
    wind = window.wind_speed_100m
    if wind.max() > 60:
        problems.append('ветер похож на км/ч, а не на м/с')
    if not window.temperature_2m.between(-60, 60).all():
        problems.append('температура вне разумных границ')
    if not window.surface_pressure.between(800, 1100).all():
        problems.append('давление вне разумных границ')

    gaps = window.wind_speed_100m.isna().sum()
    if gaps:
        # горизонт 48 ч у Open-Meteo иногда неполный — закрываем соседним часом
        window = window.sort_values('time').ffill().bfill()
        problems.append(f'заполнено {gaps} пропусков соседними часами')

    per_day = window.groupby(window.time.dt.normalize()).size()
    if (per_day < 24).any():
        problems.append(f'неполные сутки: {dict(per_day[per_day < 24])}')

    status = 'ok' if not problems else 'degraded'
    text = (f'ряд принят: {len(window)} часов, ветер {wind.min():.1f}–{wind.max():.1f} м/с'
            if not problems else 'ряд принят с оговорками: ' + '; '.join(problems))
    _note(journal, 'prepare', status, text, started)
    return window


def forecast(window: pd.DataFrame, journal: list, ctx: dict) -> pd.DataFrame:
    """Почасовой прогноз выработки по обеим турбинам. Считает модель, не языковая."""
    started = time.perf_counter()
    parts = []
    for turbine in ctx['turbines']:
        table = mdl.features(window, turbine)
        table['turbine'] = turbine
        for lead, rows in table.groupby('lead_h').groups.items():
            curve = ctx['curves'][(turbine, int(lead))]
            table.loc[rows, 'curve'] = mdl.apply_curve(curve, table.loc[rows, 'ws_norm'])
        parts.append(table)
    table = pd.concat(parts, ignore_index=True)
    predicted = mdl.apply_bounds(mdl.predict(ctx['models'], table), ctx.get('offsets', {}))
    mean_power = predicted.p50.mean()
    _note(journal, 'forecast', 'ok',
          f'посчитано {len(predicted)} строк, средняя мощность {mean_power:.2f} от номинала, '
          f'коридор P10–P90 шириной {(predicted.p90 - predicted.p10).mean():.2f}', started)
    return predicted


def reflect(predicted: pd.DataFrame, issue_time: pd.Timestamp, journal: list, ctx: dict) -> pd.DataFrame:
    """Рефлексия: свериться со свежим фактом и подкрутить смещение медианы под каждую турбину.

    Каждая ВЭС дрейфует по-своему из-за локальных эффектов, которых нет в общей модели: обледенение,
    затенение, тонкие настройки контроллера. Скользящее среднее ошибки прошедших семи суток —
    самая простая честная поправка."""
    started = time.perf_counter()
    table = ctx['table']
    end = min(issue_time, table[table.power.notna()].time.max())
    recent = table[(table.lead_h == 24) & (table.time <= end)
                   & (table.time > end - pd.Timedelta(days=REFLECT_DAYS))
                   & table.power.notna() & (~table.curtailed.fillna(False))]
    if recent.empty:
        _note(journal, 'reflect', 'skipped', 'нет свежего факта — прогноз идёт без поправки', started)
        return predicted
    check = mdl.predict(ctx['models'], recent)
    joined = recent[['time', 'turbine', 'lead_h', 'power']].merge(
        check[['time', 'turbine', 'lead_h', 'p50']], on=['time', 'turbine', 'lead_h'])
    bias = joined.groupby('turbine').apply(lambda g: float((g.power - g.p50).mean()), include_groups=False)
    predicted = predicted.copy()
    for turbine, shift in bias.items():
        mask = predicted.turbine == turbine
        for column in ('p10', 'p50', 'p90'):
            predicted.loc[mask, column] = np.clip(predicted.loc[mask, column] + shift, 0, 1)
    text = 'смещение за неделю: ' + ', '.join(f'{t} {shift:+.03f}' for t, shift in bias.items())
    _note(journal, 'reflect', 'ok', text, started)
    return predicted


def verify(predicted: pd.DataFrame, issue_time: pd.Timestamp, journal: list, ctx: dict) -> pd.DataFrame:
    """Шлюз качества: сверяем решение с персистентностью на последнем окне с фактом."""
    started = time.perf_counter()
    table, hourly = ctx['table'], ctx['hourly']
    end = min(issue_time, table[table.power.notna()].time.max())
    recent = table[(table.lead_h == 24) & (table.time <= end)
                   & (table.time > end - pd.Timedelta(days=VERIFY_DAYS))
                   & table.power.notna() & (~table.curtailed.fillna(False))]
    if recent.empty:
        _note(journal, 'verify', 'skipped', 'нет факта для сверки — публикуем расчёт как есть', started)
        return predicted

    check = mdl.predict(ctx['models'], recent)
    merged = recent[['time', 'turbine', 'lead_h', 'power', 'curve']].merge(
        check[['time', 'turbine', 'lead_h', 'p50']], on=['time', 'turbine', 'lead_h'])
    reference = bt.add_baselines(merged, hourly, ctx['train'])
    ours = bt.metrics(reference.power, reference.p50, reference.persistence)
    curve_only = bt.metrics(reference.power, reference.curve, reference.persistence)

    if ours.get('skill', 0) <= 0:
        published = predicted.assign(p50=predicted.curve)
        _note(journal, 'verify', 'rollback',
              f'решение не обыграло персистентность (скилл {ours.get("skill", 0):+.2f}) — '
              f'откат на кривую мощности, её скилл {curve_only.get("skill", 0):+.2f}', started)
        return published

    _note(journal, 'verify', 'ok',
          f'проверка на {VERIFY_DAYS} днях до {end.date()}: nRMSE {ours["nrmse"]:.3f}, '
          f'скилл к персистентности {ours["skill"]:+.2f}, чистая кривая дала {curve_only["nrmse"]:.3f}',
          started)
    return predicted


def explain(predicted: pd.DataFrame, window: pd.DataFrame, journal: list) -> str:
    """Короткий разбор для диспетчера. Все числа посчитаны заранее, модель их только пересказывает."""
    started = time.perf_counter()
    day_ahead = predicted[predicted.lead_h == 24]
    facts = {
        'период': f'{day_ahead.time.min():%d.%m %H:%M} — {day_ahead.time.max():%d.%m %H:%M}',
        'средняя мощность, доля номинала': round(float(day_ahead.p50.mean()), 2),
        'минимум и максимум': [round(float(day_ahead.p50.min()), 2), round(float(day_ahead.p50.max()), 2)],
        'часов выше 0.8': int((day_ahead.p50 > 0.8).sum()),
        'часов ниже 0.1': int((day_ahead.p50 < 0.1).sum()),
        'ширина коридора P10–P90': round(float((day_ahead.p90 - day_ahead.p10).mean()), 2),
        'ветер на 100 м, м/с': [round(float(window.wind_speed_100m.min()), 1),
                                round(float(window.wind_speed_100m.max()), 1)],
        'температура, °C': [round(float(window.temperature_2m.min()), 1),
                            round(float(window.temperature_2m.max()), 1)],
    }
    answer = llm.ask(
        'Ты помощник диспетчера ветроэлектростанции. Пиши по-русски, три-четыре коротких предложения. '
        'Используй только переданные числа, ничего не придумывай и не добавляй рекомендаций, '
        'не следующих из них.',
        'Прогноз выработки на сутки вперёд, доли от установленной мощности.\n'
        + json.dumps(facts, ensure_ascii=False, indent=1)
        + '\nОбъясни, что ждёт станцию, где риск ошибки и на что смотреть диспетчеру.')

    if answer:
        _note(journal, 'explain', 'ok', answer, started)
        return answer

    text = (f'Сутки {facts["период"]}: средняя выработка {facts["средняя мощность, доля номинала"]:.2f} '
            f'от номинала, размах {facts["минимум и максимум"][0]:.2f}–{facts["минимум и максимум"][1]:.2f}. '
            f'Ветер {facts["ветер на 100 м, м/с"][0]}–{facts["ветер на 100 м, м/с"][1]} м/с, '
            f'часов на полке {facts["часов выше 0.8"]}, почти без выработки {facts["часов ниже 0.1"]}. '
            f'Коридор P10–P90 шириной {facts["ширина коридора P10–P90"]:.2f} — это и есть мера риска.')
    _note(journal, 'explain', 'fallback', text, started)
    return text


# ---------------------------------------------------------------- цикл и пересчёт

def decide_rerun(previous: dict | None, current: dict) -> tuple[bool, str]:
    """Нужен ли пересчёт: вышел новый прогон погоды или ветер в прогнозе заметно поехал."""
    if previous is None:
        return True, 'первый прогон за сессию'
    if current['issue_time'] > previous['issue_time']:
        overlap = previous['wind'].reindex(current['wind'].index).dropna()
        if overlap.empty:
            return True, 'вышел новый прогон погоды, пересечения с прошлым нет'
        delta = float((current['wind'].reindex(overlap.index) - overlap).abs().mean())
        if delta >= RERUN_WIND_DELTA:
            return True, f'новый прогон погоды, ветер сдвинулся на {delta:.1f} м/с'
        return True, f'новый прогон погоды, ветер почти тот же ({delta:.1f} м/с)'
    return False, 'новых данных нет, пересчёт не нужен'


def run_cycle(issue_time, ctx: dict | None = None, live: bool = False,
              previous: dict | None = None, journal: list | None = None, with_explain: bool = True):
    """Один полный проход агента. Возвращает прогноз, журнал и состояние для следующего решения."""
    issue_time = pd.Timestamp(issue_time)
    journal = journal if journal is not None else []
    ctx = ctx or context()

    window = collect(issue_time, journal, live=live)
    window = prepare(window, journal)
    state = {'issue_time': issue_time, 'wind': window.set_index('time').wind_speed_100m}

    needed, reason = decide_rerun(previous, state)
    _note(journal, 'decide', 'ok' if needed else 'skipped', reason, time.perf_counter())
    if not needed and previous is not None:
        return previous.get('forecast'), journal, previous

    predicted = forecast(window, journal, ctx)
    predicted = reflect(predicted, issue_time, journal, ctx)
    predicted = verify(predicted, issue_time, journal, ctx)
    predicted['issue_time'] = issue_time
    if with_explain:
        explain(predicted, window, journal)
    state['forecast'] = predicted
    return predicted, journal, state


def run_backtest_month(start: str = '2026-02-01', end: str = '2026-02-28',
                       explain_every: int = 7, live: bool = False):
    """Прокрутка тестового месяца: на каждый день — то, что было известно накануне."""
    ctx = context()
    journal, state, parts = [], None, []
    days = pd.date_range(start, end, freq='D')
    for number, target_day in enumerate(days):
        issue_time = target_day - pd.Timedelta(days=1)
        predicted, journal, state = run_cycle(
            issue_time, ctx=ctx, live=live, previous=state, journal=journal,
            with_explain=bool(explain_every) and number % explain_every == 0)
        if predicted is not None:
            parts.append(predicted[predicted.lead_h == 24])
    forecasts = pd.concat(parts, ignore_index=True).drop_duplicates(['time', 'turbine'], keep='last')
    return forecasts, journal


def save_journal(journal: list, path: Path = JOURNAL_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(journal, ensure_ascii=False, indent=1), encoding='utf-8')
    return path


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description='Агент прогноза выработки ВЭС')
    parser.add_argument('--demo', action='store_true', help='один цикл на свежих данных архива')
    parser.add_argument('--live', action='store_true', help='тянуть живой прогноз погоды')
    parser.add_argument('--month', action='store_true', help='прокрутить тестовый февраль 2026')
    args = parser.parse_args()

    cfg = llm.settings()
    print(f'провайдер объяснений: {cfg["provider"]}, модель {cfg["model"]}, '
          f'ключ {"есть" if cfg["key"] else "не задан — объяснение соберётся шаблоном"}')

    if args.month:
        started = time.perf_counter()
        forecasts, journal = run_backtest_month(live=args.live)
        print(f'\nпрогноз на февраль: {len(forecasts)} строк, циклов в журнале '
              f'{sum(1 for e in journal if e["step"] == "forecast")}, '
              f'откатов {sum(1 for e in journal if e["status"] == "rollback")}, '
              f'время {time.perf_counter() - started:.1f} с')
        print(f'журнал: {save_journal(journal)}')
    else:
        if args.live:
            issue_time = pd.Timestamp.utcnow().normalize().tz_localize(None)
        else:
            # самый свежий момент, для которого архив ещё покрывает двое суток вперёд
            issue_time = (wx.load(*ARCHIVE).time.max() - pd.Timedelta(days=2)).normalize()
        predicted, journal, _ = run_cycle(issue_time, live=args.live)
        print(f'\nвыпуск {issue_time:%Y-%m-%d}, строк прогноза {len(predicted)}\n')
        for entry in journal:
            print(f'  [{entry["step"]:<8} {entry["status"]:<8} {entry["duration_ms"]:>5} мс] {entry["text"]}')
