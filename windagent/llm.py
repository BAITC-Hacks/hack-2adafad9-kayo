# -*- coding: utf-8 -*-
"""Языковая модель для шага объяснения. Провайдер переключается одной переменной окружения.

NVIDIA NIM полностью совместим с OpenAI по протоколу, поэтому вся разница — адрес, ключ и имя модели.
Числа модель не считает: ей передают уже посчитанное и просят объяснить.

Ключа нет или сеть недоступна — возвращаем None, вызывающий код собирает объяснение сам.
"""
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

# ключи удобно держать в .env в корне проекта (он в .gitignore); переменные окружения важнее файла
ENV_FILE = Path(__file__).resolve().parent.parent / '.env'
if ENV_FILE.exists():
    for line in ENV_FILE.read_text(encoding='utf-8').splitlines():
        name, sep, value = line.partition('=')
        if sep and not name.strip().startswith('#'):
            os.environ.setdefault(name.strip(), value.strip().strip('"\''))

last_error = ''   # почему последний вызов не удался — для диагностики, без ключа

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
        'base_url': os.environ.get('LLM_BASE_URL', base),
        'model': os.environ.get('LLM_MODEL', default_model),
        'key': os.environ.get(key_env, ''),
        'key_env': key_env,
    }


def available() -> bool:
    return bool(settings()['key'])


def ask(system: str, prompt: str, max_tokens: int = 400, temperature: float = 0.2) -> str | None:
    """Ответ модели или None, если ключа нет и сеть не отвечает."""
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
    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                answer = json.load(response)
            text = (answer['choices'][0]['message']['content'] or '').strip()
            if text:
                last_error = ''
                return text
            last_error = f'пустой ответ, finish_reason={answer["choices"][0].get("finish_reason")}'
        except urllib.error.HTTPError as err:
            last_error = f'HTTP {err.code}: {err.read().decode("utf-8", "replace")[:300]}'
        except (urllib.error.URLError, TimeoutError, KeyError, IndexError, json.JSONDecodeError) as err:
            last_error = f'{type(err).__name__}: {err}'
    return None
