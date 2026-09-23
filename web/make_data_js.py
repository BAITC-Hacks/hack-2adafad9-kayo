# -*- coding: utf-8 -*-
"""data.json → data.js.

Открытую с диска страницу браузер не пускает к соседнему JSON (политика file://), зато обычный
скрипт подключить разрешает. Дашборд сначала ищет data.js, потом уже пробует fetch — поэтому после
каждого прогона пайплайна достаточно прогнать этот файл, и страница откроется двойным кликом.
"""
from pathlib import Path

HERE = Path(__file__).resolve().parent

source = HERE / 'data.json'
if not source.exists():
    source = HERE / 'data.sample.json'

text = source.read_text(encoding='utf-8')
(HERE / 'data.js').write_text('window.DASHBOARD_DATA = ' + text + ';\n', encoding='utf-8')
print(f'{source.name} → data.js, {len(text) // 1024} КБ')
