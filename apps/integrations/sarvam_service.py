"""
Sarvam AI translation service.
Translates English medical insight text to Hindi (hi) or Kannada (kn).

Translations are cached in Django's cache backend for 24 hours to avoid
redundant API calls for identical text.
"""
import os
import hashlib
import logging

import requests
from django.core.cache import cache

logger = logging.getLogger(__name__)

SARVAM_API_URL = "https://api.sarvam.ai/translate"

# Sarvam language codes for supported target languages
_LANG_CODE_MAP = {
    'hi': 'hi-IN',
    'kn': 'kn-IN',
}

# Tags like 'high', 'medium', 'low' are programmatic — never translated
UNTRANSLATABLE_TAGS = {'high', 'medium', 'low', 'critical', 'normal'}


def _translate_text(text: str, target_lang: str) -> str:
    """
    Translate a single string from English to target_lang using Sarvam AI.
    Returns the original text if translation fails or lang is unsupported.
    Caches results for 24 hours.
    """
    if not text or not text.strip():
        return text

    sarvam_lang = _LANG_CODE_MAP.get(target_lang)
    if not sarvam_lang:
        return text

    cache_key = f"sarvam:{target_lang}:{hashlib.md5(text.encode('utf-8')).hexdigest()}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    api_key = os.getenv('SARVAM_API_KEY')
    if not api_key:
        logger.warning("SARVAM_API_KEY not configured — skipping translation")
        return text

    try:
        response = requests.post(
            SARVAM_API_URL,
            headers={
                "api-subscription-key": api_key,
                "Content-Type": "application/json",
            },
            json={
                "input": text,
                "source_language_code": "en-IN",
                "target_language_code": sarvam_lang,
                "speaker_gender": "Female",
                "mode": "formal",
                "model": "mayura:v1",
                "enable_preprocessing": False,
            },
            timeout=10,
        )
        if response.status_code == 200:
            translated = response.json().get("translated_text", text)
            cache.set(cache_key, translated, timeout=86400)  # 24 hrs
            return translated
        logger.warning(
            "Sarvam API returned %d for lang=%s: %s",
            response.status_code, target_lang, response.text[:200],
        )
    except requests.exceptions.Timeout:
        logger.warning("Sarvam API timed out for lang=%s", target_lang)
    except Exception as e:
        logger.error("Sarvam translation error for lang=%s: %s", target_lang, e)

    return text


def _translate_list(items: list, target_lang: str) -> list:
    """
    Translate a list of strings with a single Sarvam API call by joining
    them with a newline delimiter, then splitting the result back.

    Falls back to per-item calls if the response line count doesn't match.
    """
    if not items:
        return items

    joined = "\n".join(items)
    translated_joined = _translate_text(joined, target_lang)
    result = translated_joined.split("\n")

    if len(result) == len(items):
        return result

    # Sarvam merged or split lines — fall back to individual calls
    logger.debug(
        "Sarvam list translation line mismatch (%d → %d), falling back to per-item",
        len(items), len(result),
    )
    return [_translate_text(item, target_lang) for item in items]


def translate_insight(insight_data: dict, target_lang: str) -> dict:
    """
    Translate an insight dict to target_lang ('hi' or 'kn').
    Only translates: title, summary, key_findings, risk_flags.
    The 'tags' field (high/medium/low) is never translated.

    Returns the original dict unchanged if target_lang is 'en' or unsupported.
    """
    if not target_lang or target_lang == 'en' or target_lang not in _LANG_CODE_MAP:
        return insight_data

    return {
        **insight_data,
        'title': _translate_text(insight_data.get('title', ''), target_lang),
        'summary': _translate_text(insight_data.get('summary', ''), target_lang),
        'key_findings': _translate_list(insight_data.get('key_findings', []), target_lang),
        'risk_flags': _translate_list(insight_data.get('risk_flags', []), target_lang),
    }


def translate_insights_list(insights: list, target_lang: str) -> list:
    """Translate a list of insight dicts."""
    if not target_lang or target_lang == 'en' or target_lang not in _LANG_CODE_MAP:
        return insights
    return [translate_insight(insight, target_lang) for insight in insights]
