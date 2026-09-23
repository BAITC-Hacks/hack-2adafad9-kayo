# -*- coding: utf-8 -*-
"""Живая проверка обоих провайдеров: отвечает ли модель и не течёт ли в текст служебное.

    python checks/llm_ping.py            # openai и nvidia по очереди
    LLM_MODEL=gpt-6-sol python checks/llm_ping.py

Ключи берутся из переменных окружения или .env в корне. Ключ в вывод не попадает.
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from windagent import llm  # noqa: E402

SYSTEM = ('Ты помощник диспетчера ветроэлектростанции. Пиши по-русски, два коротких предложения. '
          'Используй только переданные числа.')
PROMPT = ('Прогноз на сутки: средняя мощность 0.37 от номинала, размах 0.02–0.91, ветер 1.5–11 м/с, '
          'коридор P10–P90 шириной 0.45. Что ждёт станцию?')

for provider in ('openai', 'nvidia'):
    os.environ['LLM_PROVIDER'] = provider
    cfg = llm.settings()
    if not cfg['key']:
        print(f'{provider}: ключа {cfg["key_env"]} нет — пропуск')
        continue
    started = time.perf_counter()
    answer = llm.ask(SYSTEM, PROMPT)
    seconds = time.perf_counter() - started
    if answer is None:
        print(f'{provider} / {cfg["model"]}: ОШИБКА за {seconds:.1f} с — {llm.last_error}')
        continue
    leaked = '<think>' in answer or '</think>' in answer
    print(f'{provider} / {cfg["model"]}: ок за {seconds:.1f} с'
          + (' — ВНИМАНИЕ, в тексте теги рассуждений' if leaked else ''))
    print('  ' + answer.replace('\n', '\n  '))
