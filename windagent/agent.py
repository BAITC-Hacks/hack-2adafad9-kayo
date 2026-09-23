# -*- coding: utf-8 -*-
"""Агентный цикл: собрать погоду, проверить, посчитать, свериться с фактом, объяснить.

Числа считает только код. Языковая модель планирует объяснение и решает, на что смотреть человеку,
но ни одна цифра в отчёте не приходит от неё. Между расчётом и публикацией стоят два шага с фактом:
рефлексия сдвигает медиану на недавнее смещение, если оно отличимо от шума, а шлюз качества
откатывает решение на физическую кривую мощности, когда оно проигрывает персистентности.

Выпуск дня d отдаёт сутки d+1 с горизонта 24 ч и сутки d+2 с горизонта 48 ч — ровно то, что было
известно накануне. Факт кончается 31.01.2026, и февраль идёт на поправке, снятой с последних двух
недель января. Валидация на ноябре-январе повторяет этот режим: в каждом месяце факт обрывается в
его начале, — поэтому метрика «наше решение» измеряет именно то, что поставляется. Тот же цикл при
ежедневном факте, как в эксплуатации, стоит отдельной строкой.
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

REFLECT_DAYS = 14         # окно свежего факта, по которому агент подкручивает смещение
REFLECT_SHRINK = 1.0      # доля смещения, на которую сдвигаем медиану (см. reflect)
REFLECT_GATE_SE = 2.0     # сдвигать, только если |смещение| больше стольких стандартных ошибок
MIN_FACT_DAYS = 3         # меньше трёх суток факта — не судим ни о смещении, ни о качестве
VERIFY_DAYS = 14          # окно, на котором шлюз качества сверяет решение с базовой линией
# Факт может отставать от выпуска: в феврале история кончается 31.01. Пока отставание не больше
# порога, окно берётся полным до последнего факта, а не усыхает; старше — как будто факта нет
# (живой режим без подвоза факта). Порог рефлексии отдельный, чтобы перенос поправки можно было
# выключить и перемерить: checks/stale_reflect.py, цифры в докстринге reflect
VERIFY_STALE_DAYS = 30
REFLECT_STALE_DAYS = 30
RERUN_WIND_DELTA = 1.0    # м/с: насколько должен измениться прогноз ветра, чтобы пересчитывать

ARCHIVE = ('2024-02-17', '2026-02-28')
JOURNAL_PATH = Path(__file__).resolve().parent.parent / 'out' / 'agent_log.json'
OURS = 'наше решение'
DAILY = 'агент при ежедневном факте'

_CONTEXT = None


def _note(journal, issue_time, step, status, text, started):
    entry = {'ts': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
             'issue_time': issue_time.strftime('%Y-%m-%dT%H:%M'),
             'step': step, 'status': status, 'text': text,
             'duration_ms': int((time.perf_counter() - started) * 1000)}
    journal.append(entry)
    return entry


def context(fitted: dict | None = None) -> dict:
    """Данные, кривые, обученные модели и поправка коридора плюс архив погоды. Считается один раз;
    уже обученное из backtest.fit_all можно отдать снаружи, чтобы не учить дважды."""
    global _CONTEXT
    if fitted is None and _CONTEXT is not None:
        return _CONTEXT
    fitted = fitted or bt.fit_all()
    _CONTEXT = {**fitted, 'weather': wx.ensemble(*ARCHIVE),
                'turbines': sorted({name for name, _ in fitted['curves']})}
    return _CONTEXT


# ---------------------------------------------------------------- шаги цикла

def _live_window(issue_time: pd.Timestamp, day1: pd.Timestamp, day2: pd.Timestamp) -> pd.DataFrame:
    """Свежий прогон Open-Meteo на двое суток вперёд.

    У Previous Runs API «прошлые прогоны» на будущие часы совпадают с текущим прогоном, так что
    колонки previous_day там ничего не значат. Берём текущий прогон и назначаем горизонт по
    реальному сдвигу от момента выпуска: сутки d+1 идут как 24 ч, d+2 — как 48 ч."""
    fresh = wx.ensemble(day1.date().isoformat(), day2.date().isoformat(), live=True)
    latest = fresh[fresh.lead_h == 0].copy()
    latest['lead_h'] = np.where(latest.time < day2, 24, 48)
    return latest.sort_values('time').reset_index(drop=True)


def collect(issue_time: pd.Timestamp, journal: list, ctx: dict, live: bool = False) -> pd.DataFrame:
    """Прогноз погоды, доступный на момент выпуска: сутки d+1 с горизонта 24 ч, сутки d+2 с 48 ч."""
    started = time.perf_counter()
    day1 = issue_time.normalize() + pd.Timedelta(days=1)
    day2 = day1 + pd.Timedelta(days=1)
    if live:
        try:
            window = _live_window(issue_time, day1, day2)
        except Exception as err:  # сеть на площадке может лежать — не повод падать
            _note(journal, issue_time, 'collect', 'degraded',
                  f'живой прогноз недоступен ({err}), беру архив прогнозов', started)
            live = False
        else:
            shift = (window.time - issue_time) / pd.Timedelta(hours=1)
            source = (f'живой прогноз Open-Meteo, свежий прогон: сутки {day1.date()} идут горизонтом 24 ч, '
                      f'{day2.date()} — 48 ч, реальный сдвиг от выпуска {shift.min():.0f}–{shift.max():.0f} ч')
            gone = f'Open-Meteo не отдал часов на {day1.date()} — {day2.date()}'
    if not live:
        archive = ctx['weather']
        window = pd.concat([
            archive[(archive.lead_h == 24) & (archive.time.dt.normalize() == day1)],
            archive[(archive.lead_h == 48) & (archive.time.dt.normalize() == day2)],
        ]).sort_values('time').reset_index(drop=True)
        source = 'архив прогнозов'
        gone = (f'нет прогноза погоды на {day1.date()} — {day2.date()}: '
                f'архив кончается {archive.time.max():%d.%m.%Y}')
    if window.empty:
        _note(journal, issue_time, 'collect', 'error', gone + ' — выпуск невозможен', started)
        return window
    _note(journal, issue_time, 'collect', 'ok',
          f'{source}: {len(window)} часов на {day1.date()} — {day2.date()}, '
          f'горизонты {sorted(int(x) for x in window.lead_h.unique())} ч', started)
    return window


def prepare(window: pd.DataFrame, journal: list, issue_time: pd.Timestamp) -> pd.DataFrame:
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

    gaps = wind.isna().sum()
    if gaps:
        # горизонт 48 ч у Open-Meteo иногда неполный — закрываем соседним часом
        window = window.sort_values('time').ffill().bfill()
        problems.append(f'заполнено {gaps} пропусков соседними часами')

    per_day = window.groupby(window.time.dt.normalize()).size()
    if (per_day < 24).any():
        problems.append(f'неполные сутки: {dict(per_day[per_day < 24])}')
    for lead, offset in ((24, 1), (48, 2)):
        if lead not in set(window.lead_h):
            day = issue_time.normalize() + pd.Timedelta(days=offset)
            problems.append(f'нет суток {day.date()} на горизонте {lead} ч — выпуск короче на сутки')

    status = 'ok' if not problems else 'degraded'
    text = (f'ряд принят: {len(window)} часов, ветер {wind.min():.1f}–{wind.max():.1f} м/с'
            if not problems else 'ряд принят с оговорками: ' + '; '.join(problems))
    _note(journal, issue_time, 'prepare', status, text, started)
    return window


def forecast(window: pd.DataFrame, journal: list, ctx: dict, issue_time: pd.Timestamp) -> pd.DataFrame:
    """Почасовой прогноз выработки по обеим турбинам. Считает модель, не языковая."""
    started = time.perf_counter()
    parts = []
    for turbine in ctx['turbines']:
        table = mdl.features(window, turbine)
        table['turbine'] = turbine
        for lead, rows in table.groupby('lead_h').groups.items():
            curve = ctx['curves'][(turbine, int(lead))]
            table.loc[rows, 'curve'] = mdl.apply_curve(curve, table.loc[rows, mdl.CURVE_WIND])
        parts.append(table)
    table = pd.concat(parts, ignore_index=True)
    predicted = mdl.apply_bounds(mdl.predict(ctx['models'], table), ctx['offsets'])
    _note(journal, issue_time, 'forecast', 'ok',
          f'посчитано {len(predicted)} строк, средняя мощность {predicted.p50.mean():.2f} от номинала, '
          f'коридор P10–P90 шириной {(predicted.p90 - predicted.p10).mean():.2f}', started)
    return predicted


def _fact_window(ctx: dict, issue_time: pd.Timestamp, days: int, fact_until=None):
    """Последние `days` суток имеющегося факта на горизонте 24 ч до выпуска, без простоев, и час,
    которым факт кончается (None — факта нет). Час выпуска ещё не закрыт, поэтому граница строгая.
    `fact_until` обрывает факт искусственно — так прошлое прокручивается в режиме поставки."""
    table = ctx['table']
    cutoff = issue_time if fact_until is None else min(issue_time, pd.Timestamp(fact_until))
    known = table[(table.lead_h == 24) & (table.time < cutoff) & table.power.notna()]
    if known.empty:
        return known, None
    last = known.time.max()
    # целые календарные сутки: последний день факта бывает неполным (31.01 кончается в 18:00 UTC),
    # и отсчёт часами от него захватывал бы хвост лишнего дня
    since = last.normalize() - pd.Timedelta(days=days - 1)
    recent = known[(known.time >= since) & ~known.curtailed]
    return recent, last


def _fact_age(issue_time: pd.Timestamp, last: pd.Timestamp) -> int:
    """На сколько суток факт отстал от выпуска: 0 — есть факт за сутки перед выпуском."""
    return max(0, (issue_time.normalize() - last.normalize()).days - 1)


def _fact_gap(recent: pd.DataFrame, last, issue_time: pd.Timestamp, days: int, stale_days: int) -> str | None:
    """Почему по этому окну нельзя судить; None — можно."""
    if last is None:
        return 'факта нет вовсе'
    age = _fact_age(issue_time, last)
    if age > stale_days:
        return f'факт устарел: последний {last:%d.%m.%Y}, {age} сут назад при пороге {stale_days}'
    have = recent.time.dt.normalize().nunique()
    if have < MIN_FACT_DAYS:
        return f'мало факта: за {days} сут до {last:%d.%m.%Y} только {have} суток без простоев'
    return None


def _window_label(recent: pd.DataFrame, last: pd.Timestamp, issue_time: pd.Timestamp, what: str) -> str:
    """«проверка за 14 сут до 31.01» при свежем факте; когда факт отстал — сколько ему суток и что
    окно перенесено с последнего факта, а не усохло."""
    age = _fact_age(issue_time, last)
    if not age:
        return f'{what} за {recent.time.dt.normalize().nunique()} сут до {last:%d.%m}'
    return (f'последний факт {last:%d.%m} ({age} сут назад), '
            f'{what} по {recent.time.min():%d.%m}–{last:%d.%m} перенесена')


def reflect(predicted: pd.DataFrame, issue_time: pd.Timestamp, journal: list, ctx: dict,
            fact_until=None) -> pd.DataFrame:
    """Рефлексия: свериться со свежим фактом и решить, нужна ли поправка медианы по каждой турбине.

    Каждая ВЭС дрейфует по-своему из-за локальных эффектов, которых нет в общей модели: обледенение,
    затенение, настройки контроллера. Средняя ошибка медианы на горизонте 24 ч за последние
    REFLECT_DAYS суток — самая простая честная оценка; сдвигаем на неё, и только когда она выходит
    за REFLECT_GATE_SE стандартных ошибок, иначе это шум, и агент так и пишет в журнал. Коридор не
    трогаем: он откалиброван конформно и сидит на массах факта в нуле и на полке, сдвиг границ
    вместе с медианой роняет покрытие с 78 до 68 %. Медиана двигается внутри него.

    Замер checks/reflect_eval.py на выпусках 15.11.2025 — 30.01.2026, честные признаки, nMAE 24/48 ч
    без простоев: без поправки 0,1754/0,1920; окно 3 сут ×1 0,1678/0,1847, ×0,5 0,1670/0,1844;
    7 сут ×1 0,1638/0,1812, ×0,5 0,1656/0,1830; 14 сут ×1 0,1631/0,1800, ×0,5 0,1658/0,1828, то же
    со шлюзом 2 SE 0,1633/0,1802 и 0,1659/0,1829. Полной прокруткой цикла (agent.validate):
    ×0,5 — 0,1653/0,1803, ×1 — 0,1627/0,1781 против 0,1722/0,1866 у модели без агента.
    Полный сдвиг взят потому, что смещение системное: −0,08 ± 0,05 по окнам при стандартной
    ошибке около 0,012 — после смены режима погоды в октябре 2025 модель завышает, и половинный
    сдвиг просто недобирает; пока признаки подсматривали в ноукаст, это смещение было скрыто.

    Когда факт отстаёт от выпуска (февраль: история кончается 31.01), поправка переносится с
    последних REFLECT_DAYS суток имеющегося факта на весь месяц, пока отставание не больше
    REFLECT_STALE_DAYS. Замер checks/stale_reflect.py — валидация в режиме поставки, факт обрывается
    в начале каждого месяца, nMAE 24/48 ч без простоев за ноябрь-январь: перенос 0,1638/0,1789,
    без поправки 0,1719/0,1863, при ежедневном факте 0,1627/0,1781. Перенос почти не уступает
    свежему факту, потому что смещение держится месяцами, а не днями."""
    started = time.perf_counter()
    recent, last = _fact_window(ctx, issue_time, REFLECT_DAYS, fact_until)
    gap = _fact_gap(recent, last, issue_time, REFLECT_DAYS, REFLECT_STALE_DAYS)
    if gap:
        _note(journal, issue_time, 'reflect', 'skipped', gap + ' — прогноз идёт без поправки', started)
        return predicted
    check = mdl.predict(ctx['models'], recent)
    joined = recent[['time', 'turbine', 'lead_h', 'power']].merge(
        check[['time', 'turbine', 'lead_h', 'p50']], on=['time', 'turbine', 'lead_h'])
    error = (joined.power - joined.p50).groupby(joined.turbine).agg(['mean', 'std', 'count'])
    predicted = predicted.copy()
    notes, shifted = [], 0
    for turbine, row in error.iterrows():
        se = row['std'] / np.sqrt(row['count'])
        if abs(row['mean']) <= REFLECT_GATE_SE * se:
            notes.append(f'{turbine} {row["mean"]:+.3f}±{se:.3f} в пределах шума, поправка не нужна')
            continue
        shift = REFLECT_SHRINK * row['mean']
        rows = predicted.turbine == turbine
        predicted.loc[rows, 'p50'] = np.clip(predicted.loc[rows, 'p50'] + shift,
                                             predicted.loc[rows, 'p10'], predicted.loc[rows, 'p90'])
        notes.append(f'{turbine} {row["mean"]:+.3f}±{se:.3f}, сдвигаю медиану на {shift:+.3f}')
        shifted += 1
    _note(journal, issue_time, 'reflect', 'ok' if shifted else 'skipped',
          _window_label(recent, last, issue_time, 'поправка') + ': ' + '; '.join(notes), started)
    return predicted


def verify(predicted: pd.DataFrame, issue_time: pd.Timestamp, journal: list, ctx: dict,
           fact_until=None) -> pd.DataFrame:
    """Шлюз качества: сверяем решение с персистентностью на последнем окне с фактом."""
    started = time.perf_counter()
    recent, last = _fact_window(ctx, issue_time, VERIFY_DAYS, fact_until)
    gap = _fact_gap(recent, last, issue_time, VERIFY_DAYS, VERIFY_STALE_DAYS)
    if gap:
        _note(journal, issue_time, 'verify', 'skipped', gap + ' — публикуем расчёт как есть', started)
        return predicted

    check = mdl.predict(ctx['models'], recent)
    merged = recent[['time', 'turbine', 'lead_h', 'power', 'curve']].merge(
        check[['time', 'turbine', 'lead_h', 'p50']], on=['time', 'turbine', 'lead_h'])
    reference = bt.add_baselines(merged, ctx['hourly'], ctx['train_all'])
    ours = bt.metrics(reference.power, reference.p50, reference.persistence)
    curve_only = bt.metrics(reference.power, reference.curve, reference.persistence)

    if ours['skill'] <= 0:
        published = predicted.assign(p50=predicted.curve)
        _note(journal, issue_time, 'verify', 'rollback',
              f'решение не обыграло персистентность (скилл {ours["skill"]:+.2f}) — '
              f'откат на кривую мощности, её скилл {curve_only["skill"]:+.2f}', started)
        return published

    _note(journal, issue_time, 'verify', 'ok',
          _window_label(recent, last, issue_time, 'проверка')
          + f': nRMSE {ours["nrmse"]:.3f}, скилл к персистентности {ours["skill"]:+.2f}, '
          f'чистая кривая дала {curve_only["nrmse"]:.3f}', started)
    return predicted


def explain(predicted: pd.DataFrame, window: pd.DataFrame, journal: list, issue_time: pd.Timestamp) -> str:
    """Короткий разбор для диспетчера. Все числа посчитаны заранее, модель их только пересказывает."""
    started = time.perf_counter()
    day_ahead = predicted[predicted.lead_h == 24]
    if day_ahead.empty:
        _note(journal, issue_time, 'explain', 'skipped', 'в выпуске нет суток на горизонте 24 ч — разбор не нужен', started)
        return ''
    # по станции — среднее двух турбин в каждый час, иначе «часов выше 0,8» насчитает до 48 за сутки
    station = day_ahead.groupby('time')[['p10', 'p50', 'p90']].mean()
    weather = window[window.lead_h == 24]
    facts = {
        'период': f'{station.index.min():%d.%m %H:%M} — {station.index.max():%d.%m %H:%M}',
        'турбин': int(day_ahead.turbine.nunique()),
        'средняя мощность станции, доля номинала': round(float(station.p50.mean()), 2),
        'минимум и максимум по часам': [round(float(station.p50.min()), 2), round(float(station.p50.max()), 2)],
        'часов выше 0.8': int((station.p50 > 0.8).sum()),
        'часов ниже 0.1': int((station.p50 < 0.1).sum()),
        'ширина коридора P10–P90': round(float((station.p90 - station.p10).mean()), 2),
        'ветер на 100 м, м/с': [round(float(weather.wind_speed_100m.min()), 1),
                                round(float(weather.wind_speed_100m.max()), 1)],
        'температура, °C': [round(float(weather.temperature_2m.min()), 1),
                            round(float(weather.temperature_2m.max()), 1)],
    }
    answer = llm.ask(
        'Ты помощник диспетчера ветроэлектростанции. Пиши по-русски, три-четыре коротких предложения. '
        'Используй только переданные числа, ничего не придумывай и не добавляй рекомендаций, '
        'не следующих из них.',
        'Прогноз выработки станции на сутки вперёд, доли от установленной мощности.\n'
        + json.dumps(facts, ensure_ascii=False, indent=1)
        + '\nОбъясни, что ждёт станцию, где риск ошибки и на что смотреть диспетчеру.')

    if answer:
        _note(journal, issue_time, 'explain', 'ok', answer, started)
        return answer

    text = (f'Сутки {facts["период"]}: средняя выработка {facts["средняя мощность станции, доля номинала"]:.2f} '
            f'от номинала, размах {facts["минимум и максимум по часам"][0]:.2f}–{facts["минимум и максимум по часам"][1]:.2f}. '
            f'Ветер {facts["ветер на 100 м, м/с"][0]}–{facts["ветер на 100 м, м/с"][1]} м/с, '
            f'часов на полке {facts["часов выше 0.8"]}, почти без выработки {facts["часов ниже 0.1"]}. '
            f'Коридор P10–P90 шириной {facts["ширина коридора P10–P90"]:.2f} — это и есть мера риска.')
    _note(journal, issue_time, 'explain', 'fallback', text, started)
    return text


# ---------------------------------------------------------------- цикл и пересчёт

def decide_rerun(previous: dict | None, current: dict) -> tuple[bool, str]:
    """Нужен ли пересчёт по новому прогону погоды.

    Пересчитываем, если появились часы или горизонты, которых в прошлом выпуске не было, или ветер
    на общих часах сдвинулся не меньше RERUN_WIND_DELTA. Иначе прежний прогноз остаётся в силе."""
    if previous is None:
        return True, 'первый прогон за сессию'
    if current['issue_time'] <= previous['issue_time']:
        return False, 'новых данных нет, пересчёт не нужен'
    prev, cur = previous['window'], current['window']
    common = cur.merge(prev, on='time', suffixes=('', '_prev'))
    if common.empty:
        return True, 'новый прогон погоды: с прошлым выпуском часы не пересекаются'
    delta = float((common.wind_speed_100m - common.wind_speed_100m_prev).abs().mean())
    fresh = len(set(zip(cur.time, cur.lead_h)) - set(zip(prev.time, prev.lead_h)))
    if fresh:
        return True, (f'новый прогон: {fresh} часов ещё не прогнозировались на своём горизонте, '
                      f'на общих часах ветер сдвинулся на {delta:.1f} м/с')
    if delta >= RERUN_WIND_DELTA:
        return True, f'новый прогон: ветер сдвинулся на {delta:.1f} м/с — пересчитываю'
    return False, (f'новый прогон, но ветер почти тот же ({delta:.1f} м/с при пороге {RERUN_WIND_DELTA}) — '
                   f'прежний прогноз остаётся в силе')


def run_cycle(issue_time, ctx: dict | None = None, live: bool = False,
              previous: dict | None = None, journal: list | None = None, with_explain: bool = True,
              fact_until=None):
    """Один полный проход агента. Возвращает прогноз (None, если выпуск невозможен), журнал и
    состояние для следующего решения о пересчёте. `fact_until` — до какого часа агенту виден факт;
    нужен только прокрутке прошлого в режиме поставки."""
    issue_time = pd.Timestamp(issue_time)
    journal = journal if journal is not None else []
    ctx = ctx or context()

    window = collect(issue_time, journal, ctx, live=live)
    if window.empty:
        return None, journal, previous
    window = prepare(window, journal, issue_time)
    state = {'issue_time': issue_time, 'window': window[['time', 'lead_h', 'wind_speed_100m']]}

    started = time.perf_counter()
    needed, reason = decide_rerun(previous, state)
    _note(journal, issue_time, 'decide', 'ok' if needed else 'skipped', reason, started)
    if not needed:
        kept = previous['forecast'].merge(state['window'][['time', 'lead_h']], on=['time', 'lead_h'])
        return kept, journal, previous

    predicted = forecast(window, journal, ctx, issue_time)
    predicted = reflect(predicted, issue_time, journal, ctx, fact_until)
    predicted = verify(predicted, issue_time, journal, ctx, fact_until)
    predicted['issue_time'] = issue_time
    if with_explain:
        explain(predicted, window, journal, issue_time)
    state['forecast'] = predicted
    return predicted, journal, state


def rollout(first_issue, last_issue, ctx: dict | None = None, explain_every: int = 0, live: bool = False,
            fact_until=None):
    """Цикл по дням выпуска подряд: каждый выпуск d отдаёт сутки d+1 с 24 ч и d+2 с 48 ч."""
    ctx = ctx or context()
    journal, state, parts = [], None, []
    for number, issue_time in enumerate(pd.date_range(first_issue, last_issue, freq='D')):
        predicted, journal, state = run_cycle(
            issue_time, ctx=ctx, live=live, previous=state, journal=journal,
            with_explain=bool(explain_every) and number % explain_every == 0, fact_until=fact_until)
        if predicted is not None:
            parts.append(predicted)
    return pd.concat(parts, ignore_index=True), journal


def run_backtest_month(start='2026-02-01', end='2026-02-28', ctx: dict | None = None,
                       explain_every: int = 7, live: bool = False, fact_until=None):
    """Тестовый месяц «как в прошлом»: выпуски с 30.01 по 27.02.

    По условию первый выпуск — 31 января, он закрывает 1 февраля на горизонте 24 ч. Но 1 февраля
    на горизонте 48 ч даёт только выпуск 30 января, поэтому прокрутка начинается на день раньше:
    выпуск такой же честный, это прогноз погоды за двое суток. Последний выпуск 27.02 отдаёт 28.02
    на 24 ч, его сутки 01.03 уже вне месяца. Итог — 672 часа × 2 горизонта на турбину."""
    first, last = pd.Timestamp(start), pd.Timestamp(end)
    forecasts, journal = rollout(first - pd.Timedelta(days=2), last - pd.Timedelta(days=1),
                                 ctx=ctx, explain_every=explain_every, live=live, fact_until=fact_until)
    inside = forecasts.time.between(first, last + pd.Timedelta(hours=23))
    return forecasts[inside].reset_index(drop=True), journal


def deliver_months(first, last, ctx: dict) -> tuple[pd.DataFrame, list]:
    """Прошлое в режиме поставки: месяц за месяцем, и в каждом факт обрывается в его начале —
    так же, как в феврале история кончается 31.01."""
    parts, journal = [], []
    for start in pd.date_range(first, last, freq='MS'):
        forecasts, entries = run_backtest_month(start, start + pd.offsets.MonthEnd(0), ctx=ctx,
                                                explain_every=0, fact_until=start)
        parts.append(forecasts)
        journal += entries
    return pd.concat(parts, ignore_index=True), journal


def evaluate(forecasts: pd.DataFrame, ctx: dict, name: str) -> dict:
    """Метрики прокрутки против факта под именем модели: по горизонтам, все часы и без простоев."""
    table = ctx['table']
    joined = forecasts.merge(table[['time', 'turbine', 'lead_h', 'power', 'curtailed']],
                             on=['time', 'turbine', 'lead_h'])
    joined = bt.add_baselines(joined, ctx['hourly'], ctx['train_all'])
    rows = []
    for lead, group in joined.groupby('lead_h'):
        for scope, subset in (('все часы', group), ('без простоев', group[~group.curtailed])):
            row = bt.metrics(subset.power, subset.p50, subset.persistence)
            rows.append(dict(model=name, lead_h=int(lead), scope=scope, **row))
    coverage, width = bt.corridor(joined)
    return {'scores': pd.DataFrame(rows), 'coverage': coverage, 'width': width, 'forecasts': joined}


def validate(ctx: dict | None = None) -> dict:
    """Метрика агентного цикла на отложенном окне.

    «Наше решение» — в режиме поставки: факт обрывается в начале каждого месяца, и агент весь месяц
    живёт на поправке и проверке, перенесённых с последних двух недель факта. «Агент при ежедневном
    факте» — та же прокрутка, но факт подвозят каждый день, как будет в эксплуатации."""
    ctx = ctx or context()
    first, last = bt.VALID
    delivered, journal = deliver_months(first, last, ctx)
    daily, _ = rollout(first - pd.Timedelta(days=2), last.normalize() - pd.Timedelta(days=1), ctx=ctx)
    ours = evaluate(delivered, ctx, OURS)
    fresh = evaluate(daily[daily.time.between(first, last)], ctx, DAILY)
    return {**ours, 'scores': pd.concat([ours['scores'], fresh['scores']], ignore_index=True),
            'daily': fresh, 'journal': journal}


def save_journal(journal: list, path: Path = JOURNAL_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(journal, ensure_ascii=False, indent=1), encoding='utf-8')
    return path


def print_journal(journal: list):
    for entry in journal:
        print(f'  [{entry["step"]:<8} {entry["status"]:<8} {entry["duration_ms"]:>5} мс] {entry["text"]}')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description='Агент прогноза выработки ВЭС')
    parser.add_argument('--demo', action='store_true', help='один цикл: выпуск 31.01.2026, первый по условию кейса')
    parser.add_argument('--live', action='store_true', help='один цикл на живом прогнозе погоды')
    parser.add_argument('--month', action='store_true', help='прокрутить тестовый февраль 2026')
    parser.add_argument('--validate', action='store_true', help='метрика цикла на ноябре-январе')
    args = parser.parse_args()

    cfg = llm.settings()
    print(f'провайдер объяснений: {cfg["provider"]}, модель {cfg["model"]}, '
          f'ключ {"есть" if cfg["key"] else "не задан — объяснение соберётся шаблоном"}')

    if args.month:
        started = time.perf_counter()
        forecasts, journal = run_backtest_month()
        print(f'\nпрогноз на февраль: {len(forecasts)} строк, циклов в журнале '
              f'{sum(1 for e in journal if e["step"] == "forecast")}, '
              f'откатов {sum(1 for e in journal if e["status"] == "rollback")}, '
              f'время {time.perf_counter() - started:.1f} с')
        print(f'журнал: {save_journal(journal)}')
    elif args.validate:
        started = time.perf_counter()
        checked = validate()
        print(f'\nАГЕНТНЫЙ ЦИКЛ на ноябре 2025 — январе 2026, {time.perf_counter() - started:.1f} с')
        print(checked['scores'].pivot_table(index=['scope', 'model'], columns='lead_h',
                                            values=['nmae', 'skill']).round(3).to_string())
        print(f'покрытие коридора P10–P90: {checked["coverage"]:.1%}, средняя ширина {checked["width"]:.3f}')
    else:
        if args.live:
            issue_time = pd.Timestamp.now(tz='UTC').tz_localize(None).floor('h')
        else:
            issue_time = pd.Timestamp('2026-01-31')
        predicted, journal, _ = run_cycle(issue_time, live=args.live)
        if predicted is None:
            print(f'\nвыпуск {issue_time:%Y-%m-%d %H:%M} не состоялся — см. журнал\n')
        else:
            print(f'\nвыпуск {issue_time:%Y-%m-%d %H:%M}, строк прогноза {len(predicted)}\n')
        print_journal(journal)
