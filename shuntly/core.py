from __future__ import annotations

import functools
import time
import types
from collections.abc import Iterator
from typing import Any, TypeVar

from shuntly.record import ShuntlyRecord
from shuntly.sinks import Sink, SinkStream

TVClient = TypeVar('TVClient')


_METHOD_REGISTRY: dict[str, list[str]] = {
    'anthropic.Anthropic': [
        'messages.create',
        'messages.stream',
    ],
    'openai.OpenAI': [
        'chat.completions.create',
    ],
    'google.genai.client.Client': [
        'models.generate_content',
    ],
    'litellm': [
        'completion',
    ],
    'any_llm': [
        'completion',
    ],
    'ollama': [
        'chat',
        'generate',
    ],
    'ollama._client.Client': [
        'chat',
        'generate',
    ],
}


class StreamWrapper:
    """
    Wraps a stream to accumulate chunks while proxying all other access to the original.
    Intercepts iterable properties (like text_stream) to accumulate their chunks too.
    """

    __slots__ = ('_stream', '_chunks')

    def __init__(self, stream: Any):
        self._stream = stream
        self._chunks: list[Any] = []

    def _wrap_iterator(self, iterator: Iterator[Any]) -> Iterator[Any]:
        for chunk in iterator:
            self._chunks.append(chunk)
            yield chunk

    def __iter__(self) -> 'StreamWrapper':
        return self

    def __next__(self) -> Any:
        chunk = next(self._stream)
        self._chunks.append(chunk)
        return chunk

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._stream, name)
        # If it's an iterator/generator, wrap it to accumulate chunks
        if hasattr(attr, '__iter__') and hasattr(attr, '__next__'):
            return self._wrap_iterator(attr)
        return attr

    @property
    def chunks(self) -> list[Any]:
        return self._chunks


class StreamProxy:
    """Wraps a streaming context manager to defer recording until the stream is consumed."""

    __slots__ = (
        '_cmanager',
        '_client_name',
        '_method',
        '_request',
        '_sink',
        '_t_start',
        '_wrapper',
    )

    def __init__(
        self,
        cmanager: Any,  # context manager
        *,
        client_name: str,
        method: str,
        request: dict[str, Any],
        sink: Sink,
        t_start: float,
    ):
        self._cmanager = cmanager
        self._client_name = client_name
        self._method = method
        self._request = request
        self._sink = sink
        self._t_start = t_start
        self._wrapper: StreamWrapper | None = None

    def __enter__(self) -> StreamWrapper:
        stream = self._cmanager.__enter__()
        self._wrapper = StreamWrapper(stream)
        return self._wrapper

    def __exit__(
        self,
        exc_type: Any,
        exc_val: Any,
        exc_tb: Any,
    ) -> Any:
        error = None
        response: Any = None
        try:
            if exc_type is not None:
                error = f'{exc_type.__name__}: {exc_val}'
            elif self._wrapper is not None:
                response = self._wrapper.chunks
            return self._cmanager.__exit__(exc_type, exc_val, exc_tb)
        finally:
            duration_ms = (time.perf_counter() - self._t_start) * 1000
            record = ShuntlyRecord.build(
                client=self._client_name,
                method=self._method,
                request=self._request,
                response=response,
                duration_ms=duration_ms,
                error=error,
            )
            self._sink.write(record)


class IteratorProxy:
    """Wraps a plain iterator stream to defer recording until the iterator is exhausted."""

    __slots__ = (
        '_client_name',
        '_method',
        '_request',
        '_sink',
        '_t_start',
        '_wrapper',
        '_recorded',
    )

    def __init__(
        self,
        iterator: Any,
        *,
        client_name: str,
        method: str,
        request: dict[str, Any],
        sink: Sink,
        t_start: float,
    ):
        self._client_name = client_name
        self._method = method
        self._request = request
        self._sink = sink
        self._t_start = t_start
        self._wrapper = StreamWrapper(iterator)
        self._recorded = False

    def __iter__(self) -> 'IteratorProxy':
        return self

    def __next__(self) -> Any:
        try:
            return next(self._wrapper)
        except StopIteration:
            self._record(error=None)
            raise
        except Exception as exc:
            self._record(error=f'{type(exc).__name__}: {exc}')
            raise

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapper, name)

    def _record(self, error: str | None) -> None:
        if self._recorded:
            return
        self._recorded = True
        duration_ms = (time.perf_counter() - self._t_start) * 1000
        record = ShuntlyRecord.build(
            client=self._client_name,
            method=self._method,
            request=self._request,
            response=self._wrapper.chunks if error is None else None,
            duration_ms=duration_ms,
            error=error,
        )
        self._sink.write(record)


class Shuntly:
    """The  `shunt()` wrapper interface."""

    @staticmethod
    def _get_client_name(client: object) -> str:
        if isinstance(client, types.ModuleType):
            return client.__name__
        cls = client.__class__
        return f'{cls.__module__}.{cls.__qualname__}'

    @staticmethod
    def _resolve_qualified(obj: Any, method: str) -> tuple[Any, Any, str]:
        """
        Walk a qualified path like 'messages.create' and return (parent, attr_name). Must return parent and attr for subsequent re-assignment
        """
        parts = method.split('.')
        parent = obj
        for part in parts[:-1]:
            parent = getattr(parent, part)
            if parent is None:
                raise RuntimeError(f'Invalid method path: {method}')
        attr = parts[-1]
        func = getattr(parent, attr)
        # check that this is callable?
        return func, parent, attr

    @staticmethod
    def _get_wrapper(
        func: Any,
        client_name: str,
        method: str,
        sink: Sink,
    ) -> Any:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            t_start = time.perf_counter()
            error = None
            response: Any = None
            deferred = False

            request: dict[str, Any]
            if args:
                request = {'args': list(args), **kwargs}
            else:
                request = kwargs

            try:
                response = func(*args, **kwargs)
                # Streaming context manager — defer recording until stream is consumed
                if hasattr(response, '__enter__') and hasattr(response, '__exit__'):
                    deferred = True
                    return StreamProxy(
                        response,
                        client_name=client_name,
                        method=method,
                        request=request,
                        sink=sink,
                        t_start=t_start,
                    )
                # Plain iterator stream — defer recording until exhausted
                if hasattr(response, '__next__'):
                    deferred = True
                    return IteratorProxy(
                        response,
                        client_name=client_name,
                        method=method,
                        request=request,
                        sink=sink,
                        t_start=t_start,
                    )
                return response
            except Exception as exc:
                error = f'{type(exc).__name__}: {exc}'
                raise
            finally:
                if not deferred:
                    duration_ms = (time.perf_counter() - t_start) * 1000
                    record = ShuntlyRecord.build(
                        client=client_name,
                        method=method,
                        request=request,
                        response=response,
                        duration_ms=duration_ms,
                        error=error,
                    )
                    sink.write(record)

        return wrapper

    @classmethod
    def shunt(
        cls,
        client: TVClient,
        sink: Sink | None = None,
        *,
        methods: list[str] | None = None,
    ) -> TVClient:
        if sink is None:  # default stderr output
            sink = SinkStream()

        client_name = cls._get_client_name(client)

        if methods is None:
            if not (methods := _METHOD_REGISTRY.get(client_name)):
                raise ValueError(
                    f'Unknown client {client_name!r}. Pass methods=[...] to specify which methods to patch.'
                )

        for method in methods:
            func, parent, attr = cls._resolve_qualified(client, method)
            wrapper = cls._get_wrapper(func, client_name, method, sink)
            setattr(parent, attr, wrapper)

        return client


shunt = Shuntly.shunt
