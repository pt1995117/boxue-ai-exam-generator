from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Dict, Iterator


class _NoopSpan:
    def set_attribute(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def record_exception(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _NoopTracer:
    @contextmanager
    def start_as_current_span(self, _name: str) -> Iterator[_NoopSpan]:
        yield _NoopSpan()


_tracer: Any = _NoopTracer()
_otel_ready = False


def init_observability(service_name: str = "admin-api") -> None:
    global _tracer, _otel_ready
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not endpoint:
        return
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except Exception:
        return
    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=endpoint)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer(service_name)
    _otel_ready = True


@contextmanager
def start_span(name: str, attributes: Dict[str, Any] | None = None) -> Iterator[Any]:
    with _tracer.start_as_current_span(name) as span:
        if attributes:
            for key, value in attributes.items():
                try:
                    span.set_attribute(key, value)
                except Exception:
                    continue
        yield span


def observability_enabled() -> bool:
    return _otel_ready
