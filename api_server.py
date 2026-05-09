"""
Scrapling HTTP API — thin FastAPI wrapper around Scrapling's fetchers.

Exposes a JSON HTTP endpoint so external services (Clay, Zapier, n8n, etc.)
can use Scrapling without writing Python.

Run locally:
    SCRAPLING_API_KEY=secret uvicorn api_server:app --host 0.0.0.0 --port 8000

Endpoints:
    GET  /health          → liveness probe
    POST /scrape          → scrape a single URL
"""

from __future__ import annotations

import os
from typing import Annotated, Any, List, Literal, Optional

from fastapi import FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from scrapling.core.shell import Convertor
from scrapling.fetchers import (
    AsyncDynamicSession,
    AsyncFetcher,
    AsyncStealthySession,
)

API_KEY = os.environ.get("SCRAPLING_API_KEY")

app = FastAPI(
    title="Scrapling API",
    version="1.0.0",
    description="HTTP wrapper around Scrapling for use with no-code tools like Clay.",
)


FetcherKind = Literal["http", "stealth", "dynamic"]
OutputFormat = Literal["html", "text", "markdown", "raw"]


class ScrapeRequest(BaseModel):
    url: str = Field(..., description="URL to scrape.")
    fetcher: FetcherKind = Field(
        "http",
        description=(
            "Which Scrapling fetcher to use. "
            "'http' = fast TLS-spoofed request (no browser). "
            "'stealth' = Chromium with anti-bot bypass (Cloudflare etc). "
            "'dynamic' = plain Chromium for JS-rendered pages."
        ),
    )
    format: OutputFormat = Field(
        "markdown",
        description=(
            "Output format. 'markdown' / 'text' / 'html' return processed content; "
            "'raw' returns the unprocessed page HTML."
        ),
    )
    css_selector: Optional[str] = Field(
        None, description="CSS selector to narrow content before formatting."
    )
    main_content_only: bool = Field(
        False,
        description=(
            "Strip <script>/<style>/hidden elements and limit to <body>. "
            "Useful when you'll feed the result to an LLM."
        ),
    )

    # Browser-only options (ignored when fetcher='http')
    headless: bool = True
    network_idle: bool = False
    solve_cloudflare: bool = False
    google_search: bool = True
    wait_selector: Optional[str] = None
    wait: int = Field(0, description="Milliseconds to wait after load before returning.")
    timeout: int = Field(
        30000, description="Request timeout in milliseconds (browser) / seconds*1000 (http)."
    )

    proxy: Optional[str] = Field(
        None,
        description="Proxy URL, e.g. http://user:pass@host:port. Optional.",
    )


class ScrapeResponse(BaseModel):
    success: bool
    url: str
    status: Optional[int] = None
    content: List[str] = []
    error: Optional[str] = None


def _check_api_key(provided: Optional[str]) -> None:
    if not API_KEY:
        return
    if provided != API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-API-Key header.",
        )


async def _fetch(req: ScrapeRequest) -> Any:
    if req.fetcher == "http":
        return await AsyncFetcher.get(
            req.url,
            stealthy_headers=True,
            timeout=req.timeout / 1000,
            proxy=req.proxy,
        )

    if req.fetcher == "stealth":
        async with AsyncStealthySession(
            headless=req.headless,
            solve_cloudflare=req.solve_cloudflare,
            network_idle=req.network_idle,
            google_search=req.google_search,
            wait=req.wait,
            wait_selector=req.wait_selector,
            timeout=req.timeout,
            proxy=req.proxy,
        ) as session:
            return await session.fetch(req.url)

    async with AsyncDynamicSession(
        headless=req.headless,
        network_idle=req.network_idle,
        google_search=req.google_search,
        wait=req.wait,
        wait_selector=req.wait_selector,
        timeout=req.timeout,
        proxy=req.proxy,
    ) as session:
        return await session.fetch(req.url)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/scrape", response_model=ScrapeResponse)
async def scrape(
    req: ScrapeRequest,
    x_api_key: Annotated[Optional[str], Header(alias="X-API-Key")] = None,
) -> ScrapeResponse:
    _check_api_key(x_api_key)

    try:
        page = await _fetch(req)
    except Exception as e:
        return ScrapeResponse(success=False, url=req.url, error=f"{type(e).__name__}: {e}")

    if req.format == "raw":
        return ScrapeResponse(
            success=True,
            url=req.url,
            status=getattr(page, "status", None),
            content=[str(page.html_content)],
        )

    pieces = [
        str(p)
        for p in Convertor._extract_content(
            page,
            extraction_type=req.format,
            css_selector=req.css_selector,
            main_content_only=req.main_content_only,
        )
    ]

    return ScrapeResponse(
        success=True,
        url=req.url,
        status=getattr(page, "status", None),
        content=pieces,
    )
