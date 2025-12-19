"""OpenRouter API client for chat completions and model listing."""
from __future__ import annotations

import base64
import copy
import json
import mimetypes
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Union

import requests


def encode_image_to_data_url(
    data: Union[bytes, str, Path],
    mime: Optional[str] = None,
) -> str:
    """Encode image bytes or file path to base64 data URL for vision API."""
    # Pass through existing data URLs
    if isinstance(data, str) and data.startswith("data:"):
        return data

    # Handle file paths
    if isinstance(data, (str, Path)):
        path = Path(str(data))
        if not path.exists():
            raise ValueError(f"Image file does not exist: {path}")
        raw = path.read_bytes()
        if mime is None:
            mime, _ = mimetypes.guess_type(str(path))
            mime = mime or "image/png"
    # Handle raw bytes
    elif isinstance(data, (bytes, bytearray)):
        raw = bytes(data)
        mime = mime or "image/png"
    else:
        raise ValueError(
            "encode_image_to_data_url expects bytes, file path, or data: URL"
        )

    # Encode to base64
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


class OpenRouterError(RuntimeError):
    """Raised when OpenRouter API requests fail."""


@dataclass(frozen=True)
class ModelInfo:
    """Information about an OpenRouter model."""
    id: str
    name: str
    has_text_input: bool
    has_image_input: bool
    prompt_price: float
    completion_price: float
    created: int
    supported_parameters: List[str] = field(default_factory=list)


class OpenRouterClient:
    """OpenRouter client for chat completions and model listing."""

    CACHE_DURATION = 3600.0

    def __init__(
        self,
        api_key: Optional[str] = None,
        endpoint: str = "https://openrouter.ai/api/v1/chat/completions",
        session: Optional[requests.Session] = None,
    ) -> None:
        key = api_key or os.getenv("OPENROUTER_API_KEY")
        if not key:
            raise OpenRouterError("OPENROUTER_API_KEY environment variable must be set.")
        self._api_key = key
        
        base_url = os.getenv("OPENROUTER_BASE_URL")
        if base_url:
            base_url = base_url.rstrip("/")
            if not base_url.endswith("/chat/completions"):
                base_url = f"{base_url}/chat/completions"
            endpoint = base_url
            
        self._endpoint = endpoint
        self._base_url = self._endpoint.replace("/chat/completions", "")
        self._session = session or requests.Session()
        self._models_cache: Optional[List[ModelInfo]] = None
        self._cache_timestamp: Optional[float] = None
        self._default_tools: List[Mapping[str, Any]] = []

    def chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        model: str,
        tools: Optional[Sequence[Mapping[str, Any]]] = None,
        max_retries: int = 3,
        backoff_initial: float = 2.0,
        timeout: float = 300.0,
    ) -> Mapping[str, Any]:
        """Execute a chat completion request with retries."""
        payload: Dict[str, Any] = {"model": model, "messages": list(messages)}
        
        # Default temperature
        payload["temperature"] = 0.7
            
        if tools is not None:
            payload["tools"] = list(tools)
        elif self._default_tools:
            payload["tools"] = [copy.deepcopy(tool) for tool in self._default_tools]

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Title": "opencode-agent-vibe",
        }

        attempt = 0
        while True:
            attempt += 1
            try:
                response = self._session.post(self._endpoint, json=payload, headers=headers, timeout=timeout)
                if response.status_code in {429, 500, 502, 503, 504}:
                    response.raise_for_status()
                response.raise_for_status()
                data = response.json()
                if "error" in data:
                    error_payload = data["error"]
                    msg = str(error_payload.get("message") if isinstance(error_payload, dict) else error_payload)
                    raise OpenRouterError(f"OpenRouter returned error payload: {msg}")
                return data
            except (requests.RequestException, ValueError) as exc:
                response = getattr(exc, "response", None)
                status_code = response.status_code if response is not None else None
                retryable_status = status_code in {429, 500, 502, 503, 504}
                is_retryable = retryable_status or isinstance(exc, requests.RequestException)
                if not is_retryable or attempt > max_retries:
                    raise OpenRouterError(f"OpenRouter request failed after {max_retries} attempts.") from exc
                
                sleep_for = backoff_initial * (2 ** (attempt - 1))
                time.sleep(sleep_for)

    def chat_with_vision(
        self,
        text: str,
        images: List[Union[bytes, str, Path]],
        *,
        model: str,
        max_retries: int = 3,
        backoff_initial: float = 2.0,
        timeout: float = 300.0,
    ) -> Mapping[str, Any]:
        """Execute a vision chat completion with text prompt and images."""
        content: List[Dict[str, Any]] = [{"type": "text", "text": text}]

        for img in images:
            data_url = encode_image_to_data_url(img)
            content.append({
                "type": "image_url",
                "image_url": {"url": data_url}
            })

        messages = [{"role": "user", "content": content}]

        return self.chat(
            messages=messages,
            model=model,
            max_retries=max_retries,
            backoff_initial=backoff_initial,
            timeout=timeout,
        )
