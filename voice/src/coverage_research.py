"""Narrow, public-only research support for payer coverage criteria."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlparse

from tavily import AsyncTavilyClient

PAYER_DOMAINS: dict[str, tuple[str, ...]] = {
    "aetna": ("aetna.com",),
    "anthem": ("anthem.com",),
    "cigna": ("cigna.com",),
    "uhc": ("uhc.com", "uhcprovider.com"),
}
CMS_DOMAIN = "cms.gov"


class PublicResearchError(RuntimeError):
    """Public policy search was unavailable or did not return safe sources."""


class _TavilyClient(Protocol):
    async def search(self, **kwargs: Any) -> Mapping[str, Any]: ...

    async def close(self) -> None: ...


@dataclass(frozen=True)
class PublicSource:
    label: str
    url: str
    excerpt: str

    def to_callback_payload(self) -> dict[str, str]:
        return {"label": self.label, "url": self.url}


def official_domains_for(payer: str) -> tuple[str, ...]:
    try:
        return (*PAYER_DOMAINS[payer.lower()], CMS_DOMAIN)
    except KeyError as exc:
        raise PublicResearchError("unsupported payer for public policy search") from exc


def build_public_criteria_query(
    *, payer: str, plan_type: str, drug: str, topic: str
) -> str:
    """Build a query only from call-scoped, non-patient policy terms.

    ``topic`` deliberately does not participate in the query.  Tool arguments
    are model-generated and could inadvertently contain patient details; a
    fixed, policy-only query prevents those details from reaching Tavily.
    """

    del topic
    payer_name = payer.strip().title()
    plan = plan_type.strip().lower()
    drug_name = drug.strip()
    if not payer_name or not plan or not drug_name:
        raise PublicResearchError("coverage search needs payer, plan type, and drug")
    return f"{payer_name} {plan} {drug_name} coverage criteria"


class PublicCriteriaResearcher:
    """One-shot Tavily search restricted to official payer and CMS domains."""

    def __init__(
        self,
        *,
        api_key: str,
        timeout_seconds: float = 4.0,
        client_factory: Callable[[str], _TavilyClient] | None = None,
    ) -> None:
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._client_factory = client_factory or AsyncTavilyClient

    async def search(
        self,
        *,
        payer: str,
        plan_type: str,
        drug: str,
        topic: str,
    ) -> list[PublicSource]:
        domains = official_domains_for(payer)
        query = build_public_criteria_query(
            payer=payer,
            plan_type=plan_type,
            drug=drug,
            topic=topic,
        )
        client = self._client_factory(self._api_key)
        try:
            # Tavily receives a single basic search.  There is intentionally no
            # retry loop: voice latency matters and callers must not fan out.
            response = await asyncio.wait_for(
                client.search(
                    query=query,
                    search_depth="basic",
                    max_results=3,
                    include_domains=list(domains),
                    timeout=self._timeout_seconds,
                ),
                timeout=self._timeout_seconds + 0.5,
            )
        except (TimeoutError, asyncio.TimeoutError) as exc:
            raise PublicResearchError("public policy search timed out") from exc
        except Exception as exc:
            raise PublicResearchError("public policy search failed") from exc
        finally:
            await _close_client(client)

        if not isinstance(response, Mapping):
            raise PublicResearchError("public policy search returned invalid response")
        raw_results = response.get("results", [])
        if not isinstance(raw_results, list):
            raise PublicResearchError("public policy search returned invalid results")

        sources: list[PublicSource] = []
        for result in raw_results:
            if len(sources) == 3:
                break
            if not isinstance(result, Mapping):
                continue
            url = result.get("url")
            if not isinstance(url, str) or not _is_allowed_url(url, domains):
                continue
            title = result.get("title")
            content = result.get("content")
            sources.append(
                PublicSource(
                    label=title.strip()
                    if isinstance(title, str) and title.strip()
                    else url,
                    url=url,
                    excerpt=content.strip()[:1200] if isinstance(content, str) else "",
                )
            )
        return sources


async def _close_client(client: _TavilyClient) -> None:
    close = getattr(client, "close", None)
    if close is None:
        return
    outcome = close()
    if inspect.isawaitable(outcome):
        await outcome


def _is_allowed_url(url: str, domains: tuple[str, ...]) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    host = parsed.hostname
    if host is None:
        return False
    normalised = host.rstrip(".").lower()
    return any(
        normalised == domain or normalised.endswith(f".{domain}") for domain in domains
    )
