"""
Gemini-based translation service for medical insights.
Translates English insight JSON to Hindi (hi) or Kannada (kn) using Gemini 2.5 Flash.

Translations are cached in Django's cache backend for 24 hours.
The public API (translate_insight / translate_insights_list) is unchanged so
views that previously used Sarvam require no modification.
"""
import hashlib
import json
import logging
import os

from django.core.cache import cache

logger = logging.getLogger(__name__)

_SUPPORTED_LANGS = {
    'hi': 'Hindi',
    'kn': 'Kannada',
}


def _translate_insight_with_gemini(insight_data: dict, target_lang: str) -> dict:
    """
    Call Gemini to translate a single insight dict.
    Translates: title, summary, key_findings, risk_flags.
    Leaves tags (high/medium/low) untouched.
    Returns original dict on any failure.
    """
    lang_name = _SUPPORTED_LANGS[target_lang]

    # Build a minimal payload — only the fields we want translated
    payload = {
        'title': insight_data.get('title', ''),
        'summary': insight_data.get('summary', ''),
        'key_findings': insight_data.get('key_findings', []),
        'risk_flags': insight_data.get('risk_flags', []),
    }

    prompt = (
        f"You are a medical translator. Translate the following medical insight JSON from English to {lang_name}.\n"
        "Rules:\n"
        "- Translate: title, summary, all items in key_findings, all items in risk_flags.\n"
        "- Preserve medical terminology accurately.\n"
        "- Keep the exact same JSON structure and array lengths.\n"
        "- Return ONLY valid JSON with no markdown fences or extra text.\n\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )

    try:
        from google import genai
        client = genai.Client(api_key=os.getenv('GOOGLE_GENAI_API_KEY'))
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
        )
        text = (response.text or '').strip()
        # Strip markdown fences if present
        if text.startswith('```'):
            text = '\n'.join(text.split('\n')[1:])
            if '```' in text:
                text = text[:text.rfind('```')]

        translated = json.loads(text.strip())

        # Merge back — keep original keys (tags, created_at, id, etc.) unchanged
        return {
            **insight_data,
            'title': translated.get('title', insight_data.get('title', '')),
            'summary': translated.get('summary', insight_data.get('summary', '')),
            'key_findings': translated.get('key_findings', insight_data.get('key_findings', [])),
            'risk_flags': translated.get('risk_flags', insight_data.get('risk_flags', [])),
        }

    except Exception as e:
        logger.error("Gemini translation failed for lang=%s: %s", target_lang, e)
        return insight_data


def translate_insight(insight_data: dict, target_lang: str) -> dict:
    """
    Translate an insight dict to target_lang ('hi' or 'kn') using Gemini.
    The 'tags' field (high/medium/low) is never translated.
    Returns the original dict unchanged if target_lang is 'en' or unsupported.
    Caches results for 24 hours.
    """
    if not target_lang or target_lang == 'en' or target_lang not in _SUPPORTED_LANGS:
        return insight_data

    # Cache key based on the translatable content + language
    translatable = {
        'title': insight_data.get('title', ''),
        'summary': insight_data.get('summary', ''),
        'key_findings': insight_data.get('key_findings', []),
        'risk_flags': insight_data.get('risk_flags', []),
    }
    cache_key = (
        f"gemini_translate:{target_lang}:"
        + hashlib.md5(json.dumps(translatable, sort_keys=True).encode()).hexdigest()
    )

    cached = cache.get(cache_key)
    if cached is not None:
        return {**insight_data, **cached}

    result = _translate_insight_with_gemini(insight_data, target_lang)

    # Cache only the translated fields
    translated_fields = {
        'title': result.get('title'),
        'summary': result.get('summary'),
        'key_findings': result.get('key_findings'),
        'risk_flags': result.get('risk_flags'),
    }
    cache.set(cache_key, translated_fields, timeout=86400)  # 24 hrs

    return result


def translate_insights_list(insights: list, target_lang: str) -> list:
    """Translate a list of insight dicts."""
    if not target_lang or target_lang == 'en' or target_lang not in _SUPPORTED_LANGS:
        return insights
    return [translate_insight(insight, target_lang) for insight in insights]
