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

PROVIDERS = {
    'openai': ('https://api.openai.com/v1', 'OPENAI_API_KEY', 'gpt-4.1-mini'),
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
    cfg = settings()
    if not cfg['key']:
        return None
    body = json.dumps({
        'model': cfg['model'],
        'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': prompt}],
        'temperature': temperature,
        'max_tokens': max_tokens,
    }).encode('utf-8')
    request = urllib.request.Request(
        cfg['base_url'].rstrip('/') + '/chat/completions', data=body,
        headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {cfg["key"]}'})
    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                answer = json.load(response)
            return answer['choices'][0]['message']['content'].strip()
        except (urllib.error.URLError, TimeoutError, KeyError, IndexError, json.JSONDecodeError):
            if attempt:
                return None
    return None
