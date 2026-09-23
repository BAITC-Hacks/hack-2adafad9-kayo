# -*- coding: utf-8 -*-
"""Языковая модель для шага объяснения. Провайдер переключается одной переменной окружения.

NVIDIA NIM полностью совместим с OpenAI по протоколу, поэтому вся разница — адрес, ключ и имя модели.
Числа модель не считает: ей передают уже посчитанное и просят объяснить.

Ключа нет или сеть недоступна — возвращаем None, вызывающий код собирает объяснение сам.
"""
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

# ключи удобно держать в .env в корне проекта (он в .gitignore); переменные окружения важнее файла
ENV_FILE = Path(__file__).resolve().parent.parent / '.env'


def _parse_env_line(raw_line: str) -> tuple[str, str] | None:
    """Разобрать одну строку .env: export, комментарии, кавычки, хвостовой # комментарий."""
    line = raw_line.strip()
    if not line or line.startswith('#'):
        return None
    if line.startswith('export '):
        line = line[len('export '):].lstrip()
    name, sep, value = line.partition('=')
    if not sep:
        return None
    name = name.strip()
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
        value = value[1:-1]
    else:
        # незакавыченное значение — хвост вида "значение # комментарий" отрезаем
        value = re.split(r'\s+#', value, maxsplit=1)[0].rstrip()
    if not name or not value:
        return None
    return name, value


if ENV_FILE.exists():
    # utf-8-sig — некоторые редакторы на Windows пишут .env с BOM, обычный utf-8 споткнётся на первой строке
    for _raw_line in ENV_FILE.read_text(encoding='utf-8-sig').splitlines():
        _parsed = _parse_env_line(_raw_line)
        if _parsed:
            os.environ.setdefault(*_parsed)

last_error = ''   # почему последний вызов не удался — для диагностики, без ключа и заголовков

# 429 и 5xx — сбои на стороне сервера, имеет смысл повторить один раз; остальные коды (401, 404 и т.п.)
# сами себя не починят
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# gpt-6-luna — дешёвая модель линейки GPT-6 (сентябрь 2026, $0.10/$0.50 за миллион токенов): для
# пересказа готовых чисел хватает с запасом. Уровень выше — gpt-6-sol, задаётся через LLM_MODEL.
PROVIDERS = {
    'openai': ('https://api.openai.com/v1', 'OPENAI_API_KEY', 'gpt-6-luna'),
    'nvidia': ('https://integrate.api.nvidia.com/v1', 'NVIDIA_API_KEY', 'meta/llama-3.3-70b-instruct'),
}


def settings():
    name = os.environ.get('LLM_PROVIDER', 'openai').lower()
    base, key_env, default_model = PROVIDERS.get(name, PROVIDERS['openai'])
    return {
        'provider': name if name in PROVIDERS else 'openai',
        'base_url': os.environ.get('LLM_BASE_URL') or base,   # пустая переменная — тоже "не задана"
        'model': os.environ.get('LLM_MODEL', default_model),
        'key': os.environ.get(key_env, ''),
        'key_env': key_env,
    }


def available() -> bool:
    return bool(settings()['key'])


def _extract_text(message: dict) -> str:
    """content обычно строка, но часть моделей отвечает списком частей {'type': 'text', 'text': …}."""
    content = message.get('content')
    if isinstance(content, list):
        return ''.join(part.get('text', '') for part in content
                        if isinstance(part, dict) and part.get('type') == 'text').strip()
    return (content or '').strip()


def ask(system: str, prompt: str, max_tokens: int = 400, temperature: float = 0.2) -> str | None:
    """Ответ модели или None — при отсутствии ключа и при любом сбое или неожиданном ответе сервера.

    Исключений наружу не бросает: сервер может вернуть что угодно, шаг объяснения на этом не должен падать.
    """
    global last_error
    cfg = settings()
    if not cfg['key']:
        return None
    payload = {
        'model': cfg['model'],
        'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': prompt}],
        'temperature': temperature,
    }
    if cfg['provider'] == 'openai' and cfg['model'].startswith(('gpt-5', 'gpt-6', 'o')):
        # GPT-5+/GPT-6 в chat/completions: лимит только через max_completion_tokens, и без
        # reasoning_effort=none модель тратит его на рассуждения и возвращает пустой текст.
        # Старые модели (gpt-4.1-mini) этот параметр отвергают — им прежний max_tokens
        payload.update(max_completion_tokens=max_tokens, reasoning_effort='none')
    else:
        payload['max_tokens'] = max_tokens
    body = json.dumps(payload).encode('utf-8')
    request = urllib.request.Request(
        cfg['base_url'].rstrip('/') + '/chat/completions', data=body,
        headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {cfg["key"]}'})

    for attempt in (0, 1):
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                answer = json.load(response)
        except urllib.error.HTTPError as err:
            last_error = f'HTTP {err.code}: {err.read().decode("utf-8", "replace")[:300]}'
            if err.code in RETRYABLE_STATUS and attempt == 0:
                time.sleep(2)
                continue
            return None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as err:
            last_error = f'{type(err).__name__}: {err}'
            return None

        choices = answer.get('choices') or []
        if not choices:
            last_error = 'пустой choices в ответе'
            return None
        text = _extract_text(choices[0].get('message') or {})
        if text:
            last_error = ''
            return text
        last_error = f'пустой ответ, finish_reason={choices[0].get("finish_reason")}'
        return None
    return None
