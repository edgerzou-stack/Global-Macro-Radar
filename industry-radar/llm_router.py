import json
import logging
import os
import threading
import time

from provider_errors import log_provider_error
from llm_cost_policy import active_run, resolve_policy


try:
    _max_concurrent = int(os.environ.get("LLM_MAX_CONCURRENT", "3"))
except ValueError as error:
    raise ValueError("LLM_MAX_CONCURRENT must be an integer") from error
if _max_concurrent <= 0:
    raise ValueError("LLM_MAX_CONCURRENT must be positive")
llm_semaphore = threading.Semaphore(_max_concurrent)
logger = logging.getLogger(__name__)


class _LazyGeminiModule:
    def Client(self, **kwargs):
        from google import genai as module

        return module.Client(**kwargs)

    @property
    def types(self):
        from google import genai as module

        return module.types


genai = _LazyGeminiModule()


def OpenAI(**kwargs):
    from openai import OpenAI as client_type

    return client_type(**kwargs)


def _provider_config(config, provider):
    return (
        (config or {})
        .get("llm", {})
        .get("providers", {})
        .get(provider, {})
    )


def _provider_enabled(config, provider, api_key):
    provider_cfg = _provider_config(config, provider)
    if "enabled" in provider_cfg:
        return bool(provider_cfg["enabled"]) and bool(api_key)
    return bool(api_key)


def get_gemini_client(config=None):
    api_key = os.getenv("GEMINI_API_KEY")
    if not _provider_enabled(config, "gemini", api_key):
        return None
    return genai.Client(api_key=api_key)


def get_openai_client(config=None):
    api_key = os.getenv("OPENAI_API_KEY")
    if not _provider_enabled(config, "openai", api_key):
        return None
    provider_cfg = _provider_config(config, "openai")
    base_url = provider_cfg.get(
        "base_url", os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    )
    timeout = float(provider_cfg.get("timeout", 120.0))
    return OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)


def get_deepseek_client(config=None):
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not _provider_enabled(config, "deepseek", api_key):
        return None
    provider_cfg = _provider_config(config, "deepseek")
    base_url = provider_cfg.get(
        "base_url", os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    )
    timeout = float(provider_cfg.get("timeout", 120.0))
    return OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)


def _model_for(config, provider, is_heavy):
    provider_cfg = _provider_config(config, provider)
    if is_heavy and provider_cfg.get("heavy_model"):
        return provider_cfg["heavy_model"]
    if provider_cfg.get("model"):
        return provider_cfg["model"]

    defaults = {
        "gemini": "gemini-2.5-flash",
        "openai": "gpt-4.1-mini",
        "deepseek": (config or {}).get("output", {}).get("model", "deepseek-v4-flash"),
    }
    return defaults[provider]


def _is_transient_error(exc):
    text = str(exc).lower()
    return any(
        token in text
        for token in (
            "408",
            "429",
            "500",
            "502",
            "503",
            "504",
            "timeout",
            "timed out",
            "connection",
            "quota",
            "exhausted",
            "unavailable",
            "overload",
        )
    )


def _retry_settings(config, provider):
    provider_cfg = _provider_config(config, provider)
    return int(provider_cfg.get("max_retries", 3)), float(
        provider_cfg.get("base_retry_delay", 2.0)
    )


def _call_openai_compatible(
    client, model, prompt, system_prompt, config, provider, title_context
):
    max_retries, base_delay = _retry_settings(config, provider)
    for attempt in range(max_retries):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0.0,
            )
            return json.loads(response.choices[0].message.content)
        except Exception as exc:
            transient = _is_transient_error(exc)
            will_retry = attempt + 1 < max_retries and transient
            log_provider_error(
                logger,
                exc,
                provider=provider,
                operation="chat_completion",
                retryable=will_retry,
                degraded_allowed=True,
            )
            if not will_retry:
                raise
            delay = base_delay * (2 ** attempt)
            print(
                f"{provider} transient error for '{title_context}'. "
                f"Retrying in {delay:g}s...",
                flush=True,
            )
            time.sleep(delay)


def _call_gemini(client, model, prompt, system_prompt, config, title_context):
    max_retries, base_delay = _retry_settings(config, "gemini")
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=genai.types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.0,
                    system_instruction=system_prompt,
                ),
            )
            return json.loads(response.text)
        except Exception as exc:
            transient = _is_transient_error(exc)
            will_retry = attempt + 1 < max_retries and transient
            log_provider_error(
                logger,
                exc,
                provider="gemini",
                operation="generate_content",
                retryable=will_retry,
                degraded_allowed=True,
            )
            if not will_retry:
                raise
            delay = base_delay * (2 ** attempt)
            print(
                f"gemini transient error for '{title_context}'. "
                f"Retrying in {delay:g}s...",
                flush=True,
            )
            time.sleep(delay)


def _call_llm_with_fallback(
    prompt,
    config,
    system_prompt="You are a helpful assistant designed to output JSON.",
    title_context="",
):
    """Call enabled providers in configured order and return validated JSON.

    Providers are no longer hard-disabled. A provider participates only when it
    is enabled (explicitly or by the presence of its API key) and has a key.
    """
    policy = resolve_policy(config or {})
    if not policy.api_enabled:
        raise RuntimeError(f"LLM APIs are disabled in {policy.mode} mode")
    controller = active_run(config or {})
    is_heavy = "vc analyst" in system_prompt.lower()
    order = (config or {}).get("llm", {}).get(
        "order", ["gemini", "openai", "deepseek"]
    )
    if policy.mode == "deepseek":
        order = ["deepseek"]
    factories = {
        "gemini": get_gemini_client,
        "openai": get_openai_client,
        "deepseek": get_deepseek_client,
    }
    clients = {}
    enabled_order = []
    for provider in order:
        factory = factories.get(provider)
        if factory is None:
            continue
        client = factory(config)
        if client is not None:
            clients[provider] = client
            enabled_order.append(provider)
    if not enabled_order:
        raise RuntimeError(
            f"All LLM APIs are disabled or unconfigured for '{title_context}'"
        )

    last_error = None
    preauthorized = controller.consume_router_preauthorization()
    for index, provider in enumerate(enabled_order):
        client = clients[provider]
        model = _model_for(config, provider, is_heavy)
        try:
            if not (index == 0 and preauthorized):
                controller.authorize_api_call(provider, title_context or "llm_call")
            with llm_semaphore:
                if provider == "gemini":
                    result = _call_gemini(
                        client, model, prompt, system_prompt, config, title_context
                    )
                else:
                    result = _call_openai_compatible(
                        client,
                        model,
                        prompt,
                        system_prompt,
                        config,
                        provider,
                        title_context,
                    )
                if not isinstance(result, dict):
                    raise ValueError(f"{provider} returned a non-object JSON payload")
                result["_llm"] = {
                    "provider": provider,
                    "model": model,
                    "degraded": index > 0 or len(enabled_order) == 1,
                }
                return result
        except Exception as exc:
            last_error = exc
            log_provider_error(
                logger,
                exc,
                provider=provider,
                operation="scoring_with_fallback",
                retryable=False,
                degraded_allowed=index + 1 < len(enabled_order),
            )
            next_provider = (
                enabled_order[index + 1]
                if index + 1 < len(enabled_order)
                else "failure"
            )
            print(
                f"{provider} error for '{title_context}': {exc}. "
                f"Falling back to {next_provider}...",
                flush=True,
            )

    raise RuntimeError(
        f"All enabled LLM APIs failed for '{title_context}'"
    ) from last_error
