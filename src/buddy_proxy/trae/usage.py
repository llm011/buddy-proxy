"""Preserve upstream usage evidence without estimating cache hits or credits."""
import math
from typing import Any


_CACHE_FIELDS = ('cached_tokens', 'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens',
                 'cache_read_input_tokens', 'cache_creation_input_tokens')
_DETAILS_FIELDS = ('prompt_tokens_details', 'input_tokens_details')


def _valid_cache_count(value: Any) -> bool:
    # Cache counts are whole tokens, not bools, strings, negative or nonfinite values.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if isinstance(value, int):
        return value >= 0
    return math.isfinite(value) and value >= 0 and value.is_integer()


def normalize_token_usage(data: dict[str, Any]) -> dict[str, Any] | None:
    """Retain valid extensions; never pass unsafe cache counters downstream."""
    try:
        usage = dict(data)
        usage.update({key: int(data.get(key) or 0)
                      for key in ('prompt_tokens', 'completion_tokens', 'total_tokens')})
        for key in _CACHE_FIELDS:
            if key in usage and not _valid_cache_count(usage[key]):
                usage.pop(key)
        for key in _DETAILS_FIELDS:
            details = usage.get(key)
            if key not in usage:
                continue
            if not isinstance(details, dict):
                usage.pop(key)
                continue
            details = dict(details)
            if 'cached_tokens' in details and not _valid_cache_count(details['cached_tokens']):
                details.pop('cached_tokens')
            usage[key] = details
        # Presence matters: explicit zero wins; missing/invalid fields do not
        # fabricate a cache hit. Preserve other usage/detail extensions untouched.
        details = usage.get('prompt_tokens_details', {})
        if 'cached_tokens' in details:
            cached = details['cached_tokens']
        else:
            cached = next((usage[key] for key in ('cached_tokens', 'prompt_cache_hit_tokens',
                                                 'cache_read_input_tokens') if key in usage), None)
        if cached is not None:
            usage.setdefault('prompt_tokens_details', {})['cached_tokens'] = int(cached)
        return usage
    except (TypeError, ValueError):
        return None
