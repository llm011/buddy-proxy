"""Preserve upstream usage evidence without estimating cache hits or credits."""
import math
from typing import Any


def normalize_token_usage(data: dict[str, Any]) -> dict[str, Any] | None:
    """Retain extensions; expose cached_tokens only when an upstream field exists."""
    try:
        usage = dict(data)
        usage.update({key: int(data.get(key) or 0)
                      for key in ('prompt_tokens', 'completion_tokens', 'total_tokens')})
        details = data.get('prompt_tokens_details')
        if isinstance(details, dict):
            usage['prompt_tokens_details'] = dict(details)
        else:
            details = {}
        # Presence matters: explicit zero must not be replaced by another field,
        # and missing cache evidence must not become a fabricated zero.
        if 'cached_tokens' in details:
            cached = details['cached_tokens']
        else:
            cached = next((data[key] for key in ('cached_tokens', 'prompt_cache_hit_tokens',
                                                'cache_read_input_tokens') if key in data), None)
        if (isinstance(cached, (int, float)) and not isinstance(cached, bool)
                and math.isfinite(cached) and cached >= 0):
            cached = int(cached)
            if not isinstance(usage.get('prompt_tokens_details'), dict):
                usage['prompt_tokens_details'] = {}
            usage['prompt_tokens_details']['cached_tokens'] = cached
        return usage
    except (TypeError, ValueError):
        return None
