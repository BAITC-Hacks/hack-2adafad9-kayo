"""Финальная проверка временной доступности, выгрузок и паритета признаков."""
import json
from pathlib import Path
import sys
import re

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np
import pandas as pd

from windagent import agent, asof, backtest as bt, model as mdl, weather as wx
from windagent import data as turbines

ROOT = Path(__file__).resolve().parent.parent
FORECAST_DIR = ROOT / 'forecasts' / '2026-02'
JSON_PATH = ROOT / 'web' / 'data.json'
TURBINES = ('T1', 'T2')
LEADS = (24, 48)


def require(condition, message):
    if not bool(condition):
        raise AssertionError(message)


def close(actual, expected, message, atol=1e-9):
    np.testing.assert_allclose(np.asarray(actual), np.asarray(expected), atol=atol,
                               rtol=0, equal_nan=True, err_msg=message)


def read_delivery():
    frames = []
    for turbine in TURBINES:
        path = FORECAST_DIR / f'forecast_{turbine}.csv'
        frame = pd.read_csv(path, parse_dates=['issue_time', 'time', 'time_local', 'weather_reference_time'])
        require(len(frame) == 1344, f'{path.name}: ожидалось 1344 строки, получено {len(frame)}')
        require(frame.lead_h.value_counts().to_dict() == {24: 672, 48: 672},
                f'{path.name}: ожидалось по 672 часа на горизонт')
        require(frame.time.min() == pd.Timestamp('2026-02-01 00:00')
                and frame.time.max() == pd.Timestamp('2026-02-28 23:00'),
                f'{path.name}: неверный период поставки')
        expected_issue = asof.issue_times(frame)
        require((frame.issue_time == expected_issue).all(),
                f'{path.name}: issue_time не совпадает с ежедневным расписанием 23:00 UTC')
        real_lead = (frame.time - frame.issue_time) / pd.Timedelta(hours=1)
        require(real_lead[frame.lead_h == 24].between(1, 24).all(),
                f'{path.name}: горизонт 24 должен покрывать часы +1…+24')
        require(real_lead[frame.lead_h == 48].between(25, 48).all(),
                f'{path.name}: горизонт 48 должен покрывать часы +25…+48')
        require((frame.effective_lead_h == real_lead).all(),
                f'{path.name}: effective_lead_h не совпадает с issue_time')
        require((frame.weather_lead_h == frame.lead_h + asof.PUBLICATION_BUFFER_HOURS).all(),
                f'{path.name}: погодный lead не содержит буфер публикации')
        require((frame.weather_reference_time <= frame.issue_time
                 - pd.Timedelta(hours=asof.PUBLICATION_BUFFER_HOURS)).all(),
                f'{path.name}: погодный reference time позже выпуска минус 24 часа')
        require((frame.time_local - frame.time == pd.Timedelta(hours=5)).all(),
                f'{path.name}: local time должен быть UTC+5')
        require(np.isfinite(frame[['p10', 'p50', 'p90']].to_numpy()).all(),
                f'{path.name}: прогноз содержит NaN или бесконечность')
        require(((frame.p10 >= 0) & (frame.p10 <= frame.p50)
                 & (frame.p50 <= frame.p90) & (frame.p90 <= 1)).all(),
                f'{path.name}: нарушен порядок или диапазон квантилей')
        require(not frame.duplicated(['issue_time', 'time', 'lead_h']).any(),
                f'{path.name}: повторяются ключи прогноза')
        frames.append(frame.assign(turbine=turbine))
    return pd.concat(frames, ignore_index=True)


def check_weather_archive():
    archive = asof.daily_ensemble(*asof.ARCHIVE)
    issued = archive[archive.lead_h.isin(LEADS)].copy()
    issued['issue_time'] = asof.issue_times(issued)
    real_lead = (issued.time - issued.issue_time) / pd.Timedelta(hours=1)
    require(real_lead[issued.lead_h == 24].between(1, 24).all(),
            'Архив: lead 24 содержит время вне +1…+24')
    require(real_lead[issued.lead_h == 48].between(25, 48).all(),
            'Архив: lead 48 содержит время вне +25…+48')
    require((issued.weather_lead_h == issued.lead_h + asof.PUBLICATION_BUFFER_HOURS).all(),
            'Архив: погодный lead не содержит буфер публикации')
    require((issued.weather_source == 'previous_runs_conservative').all(),
            'Архив: в поставочных строках найден источник кроме Previous Runs')
    require((issued.weather_reference_time <= issued.issue_time
             - pd.Timedelta(hours=asof.PUBLICATION_BUFFER_HOURS)).all(),
            'Архив: погодный reference time позже допустимого окна до выпуска')
    require(not issued.duplicated(['time', 'lead_h']).any(), 'Архив: дублируются ключи (time, lead_h)')
    return archive, issued


def check_json(delivery, archive):
    payload = json.loads(JSON_PATH.read_text(encoding='utf-8'))
    protocol = payload['forecast_protocol']
    require(protocol['issue_hour_utc'] == asof.ISSUE_HOUR_UTC
            and protocol['publication_buffer_hours'] == asof.PUBLICATION_BUFFER_HOURS,
            'JSON: forecast_protocol расходится с контрактом выпуска')
    require(bool(protocol.get('availability_assumption'))
            and bool(protocol.get('reference_time_meaning')),
            'JSON: не задокументированы допущения о доступности и reference time')
    require(protocol['weather_sources'] == {'24': 'previous_day2', '48': 'previous_day3'},
            'JSON: прогноз собран не из консервативных источников day2/day3')
    exported = pd.DataFrame(payload['forecast'])
    require(len(exported) == len(delivery), 'JSON: число прогнозов отличается от двух CSV')
    for column in ('time', 'issue_time', 'weather_reference_time'):
        exported[column] = pd.to_datetime(exported[column])
    keys = ['issue_time', 'time', 'lead_h', 'turbine']
    joined = delivery.merge(exported, on=keys, how='outer', suffixes=('_csv', '_json'),
                            indicator=True, validate='one_to_one')
    require((joined._merge == 'both').all(), 'JSON: ключи прогнозов не совпадают с CSV')
    for column in ('effective_lead_h', 'weather_lead_h'):
        require((joined[f'{column}_csv'] == joined[f'{column}_json']).all(),
                f'JSON: {column} отличается от CSV')
    require((joined.weather_reference_time_csv == joined.weather_reference_time_json).all(),
            'JSON: weather_reference_time отличается от CSV')
    for column in ('p10', 'p50', 'p90'):
        close(joined[f'{column}_csv'], joined[f'{column}_json'], f'JSON: {column} отличается от CSV', atol=1.1e-4)
    require(exported[['wind_speed_100m', 'wind_direction_100m']].notna().all().all(),
            'JSON: отсутствует скорость или направление ветра')
    meteo = archive[archive.lead_h.isin(LEADS)][
        ['time', 'lead_h', 'wind_speed_100m', 'wind_direction_100m',
         'weather_lead_h', 'weather_reference_time', 'weather_source']]
    meteo_joined = exported.merge(meteo, on=['time', 'lead_h'], how='left',
                                  suffixes=('_json', '_archive'), validate='many_to_one')
    require((meteo_joined.weather_lead_h_json == meteo_joined.weather_lead_h_archive).all()
            and (meteo_joined.weather_reference_time_json == meteo_joined.weather_reference_time_archive).all()
            and (meteo_joined.weather_source_json == meteo_joined.weather_source_archive).all(),
            'JSON: временная метаинформация расходится с as-of архивом')
    require(meteo_joined.wind_speed_100m_archive.notna().all(),
            'JSON: не удалось сопоставить скорость ветра с архивом')
    close(meteo_joined.wind_speed_100m_json, meteo_joined.wind_speed_100m_archive,
          'JSON: скорость ветра отличается от as-of архива', atol=0.011)
    close(meteo_joined.wind_direction_100m_json, meteo_joined.wind_direction_100m_archive,
          'JSON: направление ветра отличается от as-of архива', atol=0.11)


def check_feature_parity(archive):
    # Пропускаем одну и ту же дату через путь обучения и путь выбора погоды агентом.
    hourly, train_table, curves = bt.prepare()
    issue_time = pd.Timestamp('2026-01-30 23:00')
    journal = []
    context = {'weather': archive, 'table': train_table, 'hourly': hourly,
               'train_all': turbines.clean_for_training(
                   train_table[(train_table.time <= bt.TRAIN_END)
                               & train_table.curve.notna()
                               & train_table.lead_h.isin(bt.LEADS)]),
               'curves': curves, 'turbines': list(TURBINES)}
    raw = agent.collect(issue_time, journal, context)
    delivered_weather = agent.prepare(raw, journal, issue_time)
    features = [column for column in mdl.FEATURE_COLUMNS if column not in {'curve', 'lead_h'}]
    for turbine in TURBINES:
        delivered = mdl.features(delivered_weather, turbine)
        delivered['turbine'] = turbine
        training = train_table[train_table.turbine == turbine]
        keys = ['time', 'lead_h', 'turbine']
        pairs = delivered[keys + features].merge(training[keys + features], on=keys,
                                                 suffixes=('_delivery', '_training'),
                                                 how='left', validate='one_to_one')
        require(len(pairs) == len(delivered) and pairs[[f'{c}_training' for c in features]].notna().all().all(),
                f'{turbine}: не удалось сопоставить обучающие и поставочные признаки')
        for column in features:
            close(pairs[f'{column}_delivery'], pairs[f'{column}_training'],
                  f'{turbine}: различаются признаки {column}')
        for lead, rows in delivered.groupby('lead_h'):
            curve = mdl.apply_curve(curves[(turbine, int(lead))], rows[mdl.CURVE_WIND])
            expected = training.merge(rows[keys], on=keys, validate='one_to_one').curve
            close(curve, expected, f'{turbine}: обучающая и поставочная кривые расходятся')


def check_readme():
    scores = pd.read_csv(ROOT / 'forecasts' / 'metrics.csv', encoding='utf-8')
    ours = scores[(scores.model == agent.OURS) & (scores.scope == 'без простоев')]
    ours = ours[ours.lead_h.isin(LEADS)].drop_duplicates('lead_h').set_index('lead_h')
    require(set(ours.index) == set(LEADS), 'README: метрики поставки без простоев отсутствуют')
    readme = (ROOT / 'README.md').read_text(encoding='utf-8')
    row = next((line for line in readme.splitlines()
                if 'Наше решение (агентный цикл, режим поставки)' in line), None)
    require(row is not None, 'README: не найдена строка нашего решения в режиме поставки')
    displayed = [float(value.replace(',', '.'))
                 for value in re.findall(r'\*\*([0-9]+(?:,[0-9]+)?)\*\*', row)]
    expected = [round(float(ours.loc[24, 'nmae']), 3), round(float(ours.loc[48, 'nmae']), 3),
                round(float(ours.loc[24, 'skill']), 2), round(float(ours.loc[48, 'skill']), 2)]
    require(len(displayed) >= 4 and displayed[:4] == expected,
            f'README: таблица метрик {displayed[:4]} не совпадает с выгрузкой {expected}')

    payload = json.loads(JSON_PATH.read_text(encoding='utf-8'))
    coverage_line = next((line for line in readme.splitlines()
                          if 'Покрытие коридора P10–P90' in line), None)
    require(coverage_line is not None, 'README: не найдено описание покрытия P10–P90')
    coverage_numbers = re.findall(r'\*\*([0-9]+(?:,[0-9]+)?)\s*(%)?\*\*', coverage_line)
    require(len(coverage_numbers) >= 2, 'README: не удалось прочитать покрытие и ширину коридора')
    shown_coverage = float(coverage_numbers[0][0].replace(',', '.')) / (100 if coverage_numbers[0][1] else 1)
    shown_width = float(coverage_numbers[1][0].replace(',', '.'))
    require(abs(shown_coverage - float(payload['coverage'])) <= 0.0051,
            'README: покрытие коридора не совпадает с JSON')
    require(abs(shown_width - float(payload['width'])) <= 0.0051,
            'README: ширина коридора не совпадает с JSON')

def main():
    delivery = read_delivery()
    archive, issued = check_weather_archive()
    check_json(delivery, archive)
    first_validation_issue = asof.issue_times(pd.DataFrame({
        'time': [pd.Timestamp('2025-11-01 00:00')], 'lead_h': [48]})).iloc[0]
    require(bt.TRAIN_END < first_validation_issue,
            'Обучение включает часы после первого выпуска валидационного окна')
    check_feature_parity(archive)
    scores = pd.read_csv(ROOT / 'forecasts' / 'metrics.csv', encoding='utf-8')
    ours = scores[(scores.model == agent.OURS) & (scores.lead_h.isin(LEADS))]
    require(not ours.empty and np.isfinite(ours[['nmae', 'skill']].to_numpy()).all(),
            'Метрики поставки отсутствуют или содержат нечисловые значения')
    check_readme()
    print(f'PASS: {len(issued):,} архивных as-of строк; {len(delivery):,} CSV/JSON прогнозов; '
          'временные границы, буфер публикации, локальное время и паритет признаков соблюдены.')
    for row in ours[['lead_h', 'nmae', 'skill']].drop_duplicates().itertuples(index=False):
        print(f'Метрики поставки: lead bucket {row.lead_h}, nMAE {row.nmae:.4f}, skill {row.skill:.4f}')


if __name__ == '__main__':
    main()
