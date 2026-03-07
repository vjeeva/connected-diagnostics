"""Multi-provider LLM client. Supports Anthropic, OpenAI, and Google."""

from __future__ import annotations

import time

from backend.app.core.config import settings

MAX_RETRIES = 10

_clients: dict[str, object] = {}


def _get_client(provider: str):
    if provider not in _clients:
        if provider == "anthropic":
            import anthropic
            _clients[provider] = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        elif provider == "openai":
            import openai
            _clients[provider] = openai.OpenAI(api_key=settings.openai_api_key)
        elif provider == "google":
            import google.generativeai as genai
            genai.configure(api_key=settings.google_api_key)
            _clients[provider] = genai
        else:
            raise ValueError(f"Unknown provider: {provider}")
    return _clients[provider]


def _call_with_retry(provider: str, model: str, system: str, messages, max_tokens: int, temperature: float) -> str:
    """Call the appropriate provider API with rate limit retry."""
    for attempt in range(MAX_RETRIES):
        try:
            if provider == "anthropic":
                client = _get_client("anthropic")
                response = client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    system=system,
                    messages=messages,
                )
                return response.content[0].text

            elif provider == "openai":
                client = _get_client("openai")
                oai_messages = [{"role": "system", "content": system}]
                for msg in messages:
                    oai_messages.append(msg)
                response = client.chat.completions.create(
                    model=model,
                    messages=oai_messages,
                )
                return response.choices[0].message.content or ""

            elif provider == "google":
                genai = _get_client("google")
                gen_model = genai.GenerativeModel(model, system_instruction=system)
                user_text = messages[0]["content"] if isinstance(messages[0]["content"], str) else str(messages[0]["content"])
                response = gen_model.generate_content(user_text)
                return response.text

        except Exception as e:
            if "rate_limit" in str(e).lower() or "429" in str(e):
                if attempt == MAX_RETRIES - 1:
                    raise
                headers = getattr(getattr(e, "response", None), "headers", {})
                retry_after = headers.get("retry-after") if hasattr(headers, "get") else None
                wait = float(retry_after) if retry_after else 15
                time.sleep(wait)
            else:
                raise


def chat(
    system: str,
    messages: list[dict],
    model: str | None = None,
    max_tokens: int = 4096,
    temperature: float = 0.3,
) -> str:
    """Send a chat request. Returns the text response."""
    return _call_with_retry(
        provider=settings.chat_provider,
        model=model or settings.chat_model,
        system=system,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
    )


def chat_stream(
    system: str,
    messages: list[dict],
    model: str | None = None,
    max_tokens: int = 4096,
    temperature: float = 0.3,
):
    """Send a chat request, yielding text chunks as they arrive."""
    provider = settings.chat_provider
    mdl = model or settings.chat_model

    if provider == "anthropic":
        client = _get_client("anthropic")
        with client.messages.stream(
            model=mdl, max_tokens=max_tokens, temperature=temperature,
            system=system, messages=messages,
        ) as stream:
            for text in stream.text_stream:
                yield text

    elif provider == "openai":
        client = _get_client("openai")
        oai_messages = [{"role": "system", "content": system}] + list(messages)
        response = client.chat.completions.create(
            model=mdl, messages=oai_messages, stream=True,
        )
        for chunk in response:
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta and delta.content:
                yield delta.content

    elif provider == "google":
        genai = _get_client("google")
        gen_model = genai.GenerativeModel(mdl, system_instruction=system)
        user_text = messages[0]["content"] if isinstance(messages[0]["content"], str) else str(messages[0]["content"])
        response = gen_model.generate_content(user_text, stream=True)
        for chunk in response:
            if chunk.text:
                yield chunk.text

    else:
        raise ValueError(f"Unknown provider: {provider}")


def interpret(
    system: str,
    messages: list[dict],
    model: str | None = None,
    max_tokens: int = 256,
    temperature: float = 0.0,
) -> str:
    """Send an interpret/classification request. Uses the light model by default."""
    provider = settings.interpret_provider or settings.chat_provider
    mdl = model or settings.interpret_model or settings.light_model
    return _call_with_retry(
        provider=provider,
        model=mdl,
        system=system,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
    )


def extract_json(
    system: str,
    user_prompt: str,
    model: str | None = None,
    max_tokens: int = 8192,
) -> str:
    """Send a structured extraction request. Returns raw text (caller parses JSON)."""
    return _call_with_retry(
        provider=settings.extraction_provider,
        model=model or settings.extraction_model,
        system=system,
        messages=[{"role": "user", "content": user_prompt}],
        max_tokens=max_tokens,
        temperature=0.0,
    )


def vision(
    system: str,
    image_b64: str,
    prompt: str,
    media_type: str = "image/png",
    model: str | None = None,
    max_tokens: int = 4096,
) -> str:
    """Send an image + text prompt for vision processing. Returns text response."""
    provider = settings.vision_provider
    model = model or settings.vision_model

    if provider == "anthropic":
        messages = [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_b64}},
            {"type": "text", "text": prompt},
        ]}]
    elif provider == "openai":
        messages = [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{image_b64}"}},
            {"type": "text", "text": prompt},
        ]}]
    elif provider == "google":
        import base64
        genai = _get_client("google")
        gen_model = genai.GenerativeModel(model, system_instruction=system)
        img_bytes = base64.b64decode(image_b64)
        response = gen_model.generate_content([
            {"mime_type": media_type, "data": img_bytes},
            prompt,
        ])
        return response.text
    else:
        raise ValueError(f"Unknown vision provider: {provider}")

    return _call_with_retry(
        provider=provider,
        model=model,
        system=system,
        messages=messages,
        max_tokens=max_tokens,
        temperature=0.0,
    )
