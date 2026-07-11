"""Bounded, generation-aware Chat and Work model metadata catalogs."""

from __future__ import annotations

from copy import deepcopy
import time
from collections.abc import Callable
from typing import Any, Mapping

from gpt2agent.errors import BackendContractError, BackendHTTPError
from gpt2agent.tools._backend import async_get
from gpt2agent.tools._redact import redact
from gpt2agent.tools._validation import bounded_string, nullable_bool


AuthSnapshot = tuple[int, Mapping[str, str]]
_MAX_MODELS = 256
_MAX_TOKEN_COUNT = 10_000_000


def _identifier_string(
    value: Any,
    *,
    adapter: str,
    field: str,
    required: bool = False,
    maximum: int = 256,
) -> str | None:
    if value is None and not required:
        return None
    normalized = bounded_string(
        value,
        adapter=adapter,
        field=field,
        required=True,
        maximum=maximum,
    )
    assert normalized is not None
    if normalized != normalized.strip() or not normalized.isprintable() or redact(normalized) != normalized:
        raise BackendContractError(adapter, f"{field} must be non-sensitive identifier metadata")
    return normalized


def _nullable_token_count(value: Any, *, adapter: str) -> int | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > _MAX_TOKEN_COUNT
    ):
        raise BackendContractError(
            adapter,
            f"max_tokens must be an integer from 0 through {_MAX_TOKEN_COUNT} or null",
        )
    return value


def _bounded_string_list(value: Any, *, adapter: str, field: str) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > 100:
        raise BackendContractError(adapter, f"{field} must be a bounded string list")
    result: list[str] = []
    for entry in value:
        normalized = _identifier_string(
            entry,
            adapter=adapter,
            field=field,
        )
        assert normalized is not None
        result.append(normalized)
    return result


def _capability_flags(value: Any, *, adapter: str) -> dict[str, bool | None] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or len(value) > 100:
        raise BackendContractError(adapter, "capabilities must be a bounded object")
    result: dict[str, bool | None] = {}
    for key, flag in value.items():
        normalized_key = _identifier_string(
            key,
            adapter=adapter,
            field="capabilities key",
            maximum=128,
        )
        assert normalized_key is not None
        result[normalized_key] = nullable_bool(
            flag,
            adapter=adapter,
            field="capabilities value",
        )
    return result


def _thinking_efforts(raw: Any, *, adapter: str) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > 32:
        raise BackendContractError(adapter, "thinking_efforts must be a bounded list")
    result: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise BackendContractError(adapter, "thinking_effort entry must be an object")
        effort = _identifier_string(
            entry.get("thinking_effort"),
            adapter=adapter,
            field="thinking_effort",
            required=True,
        )
        label = bounded_string(
            entry.get("label"),
            adapter=adapter,
            field="label",
            redact_value=True,
        )
        result.append({"thinking_effort": effort, "label": label})
    return result


def normalize_general_models(models: Any) -> list[dict]:
    adapter = "general_models"
    if not isinstance(models, list) or len(models) > _MAX_MODELS:
        raise BackendContractError(adapter, "models must be a bounded list")
    result: list[dict] = []
    for raw in models:
        if not isinstance(raw, dict):
            raise BackendContractError(adapter, "model must be an object")
        normalized = {
            "slug": _identifier_string(
                raw.get("slug"), adapter=adapter, field="slug", required=True
            ),
            "title": bounded_string(
                raw.get("title"), adapter=adapter, field="title", redact_value=True
            ),
            "description": bounded_string(
                raw.get("description"),
                adapter=adapter,
                field="description",
                redact_value=True,
                maximum=4096,
            ),
            "max_tokens": _nullable_token_count(
                raw.get("max_tokens"), adapter=adapter
            ),
            "reasoning_type": _identifier_string(
                raw.get("reasoning_type"), adapter=adapter, field="reasoning_type"
            ),
            "configurable_thinking_effort": nullable_bool(
                raw.get("configurable_thinking_effort"),
                adapter=adapter,
                field="configurable_thinking_effort",
            ),
            "default_thinking_effort": _identifier_string(
                raw.get("default_thinking_effort"),
                adapter=adapter,
                field="default_thinking_effort",
            ),
            "thinking_efforts": _thinking_efforts(
                raw.get("thinking_efforts"), adapter=adapter
            ),
            "tags": _bounded_string_list(
                raw.get("tags"), adapter=adapter, field="tags"
            ),
            "capabilities": _capability_flags(
                raw.get("capabilities"), adapter=adapter
            ),
            "enabled_tools": _bounded_string_list(
                raw.get("enabled_tools"), adapter=adapter, field="enabled_tools"
            ),
            "product_features_keys": _bounded_string_list(
                raw.get("product_features_keys"),
                adapter=adapter,
                field="product_features_keys",
            ),
        }
        result.append(normalized)
    return result


def normalize_work_models(models: Any) -> list[dict]:
    adapter = "work_models"
    if not isinstance(models, list) or len(models) > _MAX_MODELS:
        raise BackendContractError(adapter, "models must be a bounded list")
    result: list[dict] = []
    for raw in models:
        if not isinstance(raw, dict):
            raise BackendContractError(adapter, "model must be an object")
        result.append(
            {
                "surface": "work",
                "slug": _identifier_string(
                    raw.get("slug"), adapter=adapter, field="slug", required=True
                ),
                "title": bounded_string(
                    raw.get("title"),
                    adapter=adapter,
                    field="title",
                    redact_value=True,
                ),
                "max_tokens": _nullable_token_count(
                    raw.get("max_tokens"), adapter=adapter
                ),
                "reasoning_type": _identifier_string(
                    raw.get("reasoning_type"),
                    adapter=adapter,
                    field="reasoning_type",
                ),
                "configurable_thinking_effort": nullable_bool(
                    raw.get("configurable_thinking_effort"),
                    adapter=adapter,
                    field="configurable_thinking_effort",
                ),
                "default_thinking_effort": _identifier_string(
                    raw.get("default_thinking_effort"),
                    adapter=adapter,
                    field="default_thinking_effort",
                ),
                "thinking_efforts": _thinking_efforts(
                    raw.get("thinking_efforts"), adapter=adapter
                ),
            }
        )
    return result


class ModelCatalog:
    """Cache only non-content model metadata for 60 seconds per auth generation."""

    def __init__(
        self,
        client,
        *,
        ttl: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._ttl = float(ttl)
        self._clock = clock
        self._generation: int | None = None
        self._cache: dict[str, tuple[float, list[dict]]] = {}

    def _auth_generation(self) -> int:
        value = getattr(self._client, "auth_generation", 0)
        return int(value() if callable(value) else value)

    def _auth_snapshot(self) -> tuple[int, dict[str, str] | None]:
        snapshot = getattr(self._client, "auth_snapshot", None)
        if callable(snapshot):
            generation, headers = snapshot()
            return int(generation), dict(headers)
        return self._auth_generation(), None

    async def _read(
        self,
        namespace: str,
        *,
        force: bool = False,
        auth_snapshot: AuthSnapshot | None = None,
    ) -> list[dict]:
        if auth_snapshot is None:
            generation, auth_headers = self._auth_snapshot()
        else:
            generation, headers = auth_snapshot
            generation, auth_headers = int(generation), dict(headers)

        # Auth generations are monotonic. A still-running operation from an
        # older account may complete after rotation; it must neither clear nor
        # read the newer account's cache.
        if self._generation is None or generation > self._generation:
            self._cache.clear()
            self._generation = generation
        now = self._clock()
        cached = self._cache.get(namespace) if generation == self._generation else None
        if not force and cached is not None and now < cached[0]:
            return deepcopy(cached[1])

        request_kwargs: dict[str, Any] = {}
        if auth_headers is not None:
            request_kwargs["auth_headers"] = auth_headers
        if namespace == "general":
            data = await async_get(
                self._client,
                "/backend-api/models",
                params={"history_and_training_disabled": "false"},
                target_path="/backend-api/models",
                fixed_probe=True,
                **request_kwargs,
            )
            if not isinstance(data, dict) or not isinstance(data.get("models"), list):
                raise BackendContractError("general_models", "models list envelope required")
            models = normalize_general_models(data["models"])
        else:
            data = await async_get(
                self._client,
                "/backend-api/tpp/models/",
                target_path="/backend-api/tpp/models/",
                fixed_probe=True,
                **request_kwargs,
            )
            if not isinstance(data, dict) or not isinstance(data.get("models"), list):
                raise BackendContractError("work_models", "models list envelope required")
            models = normalize_work_models(data["models"])
        # Start the TTL when the fetch completes, but only if this operation's
        # account generation is still current. A slow request from the previous
        # account may finish after a new generation has already populated the
        # cache; it can satisfy its original caller but must never overwrite the
        # new account's metadata.
        if generation == self._generation and generation == self._auth_generation():
            self._cache[namespace] = (self._clock() + self._ttl, models)
        return deepcopy(models)

    async def general(
        self, *, force: bool = False, auth_snapshot: AuthSnapshot | None = None
    ) -> list[dict]:
        return await self._read("general", force=force, auth_snapshot=auth_snapshot)

    async def work(
        self, *, force: bool = False, auth_snapshot: AuthSnapshot | None = None
    ) -> list[dict]:
        return await self._read("work", force=force, auth_snapshot=auth_snapshot)

    async def validate_general(
        self,
        slug: str,
        thinking_effort: str | None,
        *,
        auth_snapshot: AuthSnapshot | None = None,
    ) -> dict:
        if not isinstance(slug, str) or not slug or len(slug) > 256:
            raise self._validation_error("invalid_input")
        if thinking_effort is not None and (
            not isinstance(thinking_effort, str)
            or not thinking_effort
            or len(thinking_effort) > 128
        ):
            raise self._validation_error("invalid_input")

        models = await self.general(auth_snapshot=auth_snapshot)
        model = self._find(models, slug)
        if self._accepts(model, thinking_effort):
            return model  # type: ignore[return-value]

        # Account model metadata changes independently of this process. A
        # cached rejection therefore gets exactly one forced refresh before it
        # becomes a user-visible error.
        model = self._find(
            await self.general(force=True, auth_snapshot=auth_snapshot), slug
        )
        if model is None:
            raise self._validation_error("unsupported")
        if thinking_effort is None:
            return model
        if model.get("configurable_thinking_effort") is not True:
            raise self._validation_error("unsupported")
        raise self._validation_error("invalid_input")

    @staticmethod
    def _find(models: list[dict], slug: str) -> dict | None:
        return next((item for item in models if item.get("slug") == slug), None)

    @staticmethod
    def _accepts(model: dict | None, thinking_effort: str | None) -> bool:
        if model is None:
            return False
        if thinking_effort is None:
            return True
        if model.get("configurable_thinking_effort") is not True:
            return False
        return thinking_effort in {
            item.get("thinking_effort")
            for item in model.get("thinking_efforts", [])
            if isinstance(item, dict)
        }

    @staticmethod
    def _validation_error(code: str) -> BackendHTTPError:
        # Never include account model slugs or user-supplied values in the
        # exception text: MCP clients receive only a stable machine-safe code.
        return BackendHTTPError(
            "VALIDATE",
            "/backend-api/models",
            422,
            code=code,
            retryable=False,
        )
