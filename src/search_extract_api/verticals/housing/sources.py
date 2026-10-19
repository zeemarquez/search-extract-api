"""Housing sources: Idealista (official API or website) and a web-search source for
other portals."""

from __future__ import annotations

import base64
import re
import time
from typing import Any

from lxml import html as lxml_html

from ...adapters.base import SourceAdapter, SourceContext
from ...adapters.searxng import SearxngAdapter
from ...config import Settings
from ...core.numbers import parse_currency, parse_int, parse_number
from ...core.text import collapse_ws, strip_accents, truncate
from ...core.urls import is_http_url
from ...errors import ApiError, ErrorCode
from ...schemas.common import BaseQuery, Result
from ...sessions.profiles import DomainProfile
from .schemas import HousingQuery, HousingResult

IDEALISTA_PROFILE = DomainProfile(
    domain="idealista.com",
    check_url="https://www.idealista.com/",
    login_url="https://www.idealista.com/login",
    captcha_markers=(r"captcha-delivery\.com", r"geo\.captcha-delivery\.com"),
    notes=(
        "Idealista uses DataDome bot protection. Importing cookies from a real browser "
        "session (including the 'datadome' cookie) reduces captchas.",
    ),
)

IDEALISTA_API = "https://api.idealista.com"
IDEALISTA_API_TYPES = {
    "homes": "homes",
    "rooms": "bedrooms",
    "offices": "offices",
    "premises": "premises",
    "garages": "garages",
}
IDEALISTA_WEB_TYPES = {
    ("sale", "homes"): "venta-viviendas",
    ("rent", "homes"): "alquiler-viviendas",
    ("rent", "rooms"): "alquiler-habitacion",
    ("sale", "offices"): "venta-oficinas",
    ("rent", "offices"): "alquiler-oficinas",
    ("sale", "premises"): "venta-locales",
    ("rent", "premises"): "alquiler-locales",
    ("sale", "garages"): "venta-garajes",
    ("rent", "garages"): "alquiler-garajes",
    ("sale", "land"): "venta-terrenos",
    ("rent", "land"): "alquiler-terrenos",
}
_ROOMS = re.compile(r"(\d+)\s*(?:hab\b|hab\.|habitaci|dormitori|bedroom|rooms?\b)", re.I)
_BATHS = re.compile(r"(\d+)\s*(?:baños?|banos?|bathrooms?|baths?)\b", re.I)
_AREA = re.compile(r"(\d[\d.,]*)\s*(?:m²|m2|mts|metros)", re.I)
_PRICE = re.compile(r"(\d[\d.,\s]*\d)\s*(?:€|eur\b|euros)", re.I)


def slugify(text: str) -> str:
    value = strip_accents(text).lower()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value


def idealista_location_slug(location: str) -> str:
    """'Madrid' -> 'madrid-madrid' (province capitals); slugs are passed through."""
    value = location.strip().strip("/")
    if "/" in value or "-" in value:
        return value
    slug = slugify(value)
    return f"{slug}-{slug}"


def passes_filters(result: HousingResult, query: HousingQuery) -> bool:
    if query.min_price is not None and (result.price is None or result.price < query.min_price):
        return False
    if query.max_price is not None and (result.price is None or result.price > query.max_price):
        return False
    if query.min_area is not None and (result.area_m2 is None or result.area_m2 < query.min_area):
        return False
    if query.max_area is not None and (result.area_m2 is None or result.area_m2 > query.max_area):
        return False
    if query.min_rooms is not None and (result.rooms is None or result.rooms < query.min_rooms):
        return False
    if query.q:
        haystack = strip_accents(" ".join(filter(None, [result.title, result.snippet]))).casefold()
        if not all(strip_accents(t).casefold() in haystack for t in query.q.split()):
            return False
    return True


def parse_idealista_listing(
    html_text: str, operation: str, property_type: str
) -> list[HousingResult]:
    """Parses result cards (``article.item``) of an Idealista listing page."""
    if not html_text.strip():
        return []
    tree = lxml_html.fromstring(html_text)
    cards = tree.xpath("//article[contains(concat(' ', normalize-space(@class), ' '), ' item ')]")
    results: list[HousingResult] = []
    for card in cards:
        link = card.xpath(".//a[contains(@class, 'item-link')]")
        if not link:
            continue
        anchor = link[0]
        href = anchor.get("href") or ""
        match = re.search(r"/inmueble/(\d+)", href)
        element_id = card.get("data-element-id") or (match.group(1) if match else None)
        if not element_id:
            continue
        title = collapse_ws(anchor.get("title") or anchor.text_content()) or None
        price_text = collapse_ws(
            " ".join(card.xpath(".//*[contains(@class, 'item-price')]//text()"))
        )
        details = [
            collapse_ws(d.text_content())
            for d in card.xpath(".//*[contains(@class, 'item-detail')]")
        ]
        rooms = area = baths = None
        for detail in details:
            if rooms is None and _ROOMS.search(detail):
                rooms = parse_int(_ROOMS.search(detail).group(1))  # type: ignore[union-attr]
            elif area is None and _AREA.search(detail):
                area = parse_number(_AREA.search(detail).group(1))  # type: ignore[union-attr]
            elif baths is None and _BATHS.search(detail):
                baths = parse_int(_BATHS.search(detail).group(1))  # type: ignore[union-attr]
        description = collapse_ws(
            " ".join(card.xpath(".//*[contains(@class, 'item-description')]//text()"))
        )
        images = card.xpath(".//img/@src | .//img/@data-ondemand-img")
        image = next((src for src in images if is_http_url(src)), None)
        location = None
        if title and " en " in title:
            location = title.split(" en ", 1)[1].strip()
        results.append(
            HousingResult(
                url=f"https://www.idealista.com/inmueble/{element_id}/",
                title=title,
                snippet=truncate(description, 400) or None,
                operation=operation,  # type: ignore[arg-type]
                property_type=property_type,
                price=parse_number(price_text),
                currency=parse_currency(price_text, "EUR") if price_text else None,
                area_m2=area,
                rooms=rooms,
                bathrooms=baths,
                location=location,
                image=image,
                source="idealista.com",
                providers=["idealista"],
                extra={"element_id": element_id, "details": details},
            )
        )
    if not results and len(html_text) > 5000:
        lowered = html_text.lower()
        if not any(
            hint in lowered
            for hint in ("no hay anuncios", "no hemos encontrado", "0 anuncios", "sin resultados")
        ):
            raise ApiError(
                ErrorCode.PARSE_ERROR,
                "Idealista returned a page without recognizable listings (layout may have changed).",
            )
    return results


class IdealistaWebAdapter(SourceAdapter):
    name = "idealista"
    title = "Idealista (website)"
    description = "Listings from idealista.com pages. A session is optional and reduces captchas."
    result_model = HousingResult
    session_policy = "optional"
    session_domain = "idealista.com"

    def notes(self, settings: Settings) -> list[str]:
        return [
            "Idealista blocks automated traffic aggressively (DataDome): expect "
            "CAPTCHA_REQUIRED unless a session from a real browser is provided.",
            "Pass the location as Idealista's URL slug (e.g. 'madrid-madrid', "
            "'barcelona-barcelona', 'chamberi-madrid') for exact areas.",
            "Automated access may break the site's terms of service: use at your own risk.",
        ]

    async def search(self, query: BaseQuery, ctx: SourceContext) -> list[Result]:
        assert isinstance(query, HousingQuery)
        path = IDEALISTA_WEB_TYPES.get((query.operation, query.property_type))
        if path is None:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                f"Idealista has no '{query.property_type}' listings for '{query.operation}'.",
            )
        if not query.location:
            raise ApiError(ErrorCode.VALIDATION_ERROR, "Idealista needs a 'location'.")
        slug = idealista_location_slug(query.location)
        url = f"https://www.idealista.com/{path}/{slug}/"
        if query.page > 1:
            url += f"pagina-{query.page}.htm"
        if query.sort == "date":
            url += "?ordenado-por=fecha-publicacion-desc"
        page = await ctx.fetch_page(url, session=ctx.session, mode="auto", lang="es")
        listings = parse_idealista_listing(page.html, query.operation, query.property_type)
        return [r for r in listings if passes_filters(r, query)]


class IdealistaApiAdapter(SourceAdapter):
    name = "idealista_api"
    title = "Idealista (official API)"
    description = "Idealista partner API (needs IDEALISTA_API_KEY / IDEALISTA_API_SECRET)."
    result_model = HousingResult
    optional_config = True

    def configured(self, settings: Settings) -> bool:
        return bool(settings.idealista_api_key and settings.idealista_api_secret)

    async def _token(self, ctx: SourceContext) -> str:
        cached = await ctx.cache.get("idealista:token")
        if cached:
            return cached.decode()
        credentials = f"{ctx.settings.idealista_api_key}:{ctx.settings.idealista_api_secret}"
        response = await ctx.http.client.post(
            f"{IDEALISTA_API}/oauth/token",
            data={"grant_type": "client_credentials", "scope": "read"},
            headers={"Authorization": "Basic " + base64.b64encode(credentials.encode()).decode()},
            timeout=ctx.settings.http_timeout,
        )
        if response.status_code >= 400:
            raise ApiError(
                ErrorCode.SOURCE_ERROR, f"Idealista auth failed (HTTP {response.status_code})."
            )
        data = response.json()
        token = data.get("access_token")
        if not token:
            raise ApiError(ErrorCode.SOURCE_ERROR, "Idealista auth returned no token.")
        ttl = max(60, int(data.get("expires_in", 3600)) - 120)
        await ctx.cache.set("idealista:token", token.encode(), ttl)
        return token

    async def search(self, query: BaseQuery, ctx: SourceContext) -> list[Result]:
        assert isinstance(query, HousingQuery)
        property_type = IDEALISTA_API_TYPES.get(query.property_type)
        if property_type is None:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                f"Unsupported property_type for the API: {query.property_type}",
            )
        form: dict[str, Any] = {
            "operation": query.operation,
            "propertyType": property_type,
            "maxItems": min(50, query.limit),
            "numPage": query.page,
            "locale": query.lang or "es",
        }
        if query.sort == "date":
            form["order"] = "publicationDate"
            form["sort"] = "desc"
        if query.location_id:
            form["locationId"] = query.location_id
        elif query.latitude is not None and query.longitude is not None:
            form["center"] = f"{query.latitude},{query.longitude}"
            form["distance"] = query.radius_m or 2000
        else:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                "The Idealista API needs 'location_id' or 'latitude'+'longitude'.",
            )
        for key, value in (
            ("minPrice", query.min_price),
            ("maxPrice", query.max_price),
            ("minSize", query.min_area),
            ("maxSize", query.max_area),
        ):
            if value is not None:
                form[key] = int(value)
        if query.min_rooms is not None:
            form["bedrooms"] = ",".join(str(n) for n in range(min(query.min_rooms, 4), 5))
        token = await self._token(ctx)
        started = time.perf_counter()
        response = await ctx.http.client.post(
            f"{IDEALISTA_API}/3.5/es/search",
            data=form,
            headers={"Authorization": f"Bearer {token}"},
            timeout=ctx.settings.http_timeout,
        )
        ctx.details["api_ms"] = int((time.perf_counter() - started) * 1000)
        if response.status_code == 401:
            await ctx.cache.delete("idealista:token")
            raise ApiError(ErrorCode.SOURCE_ERROR, "Idealista API rejected the token.")
        if response.status_code >= 400:
            raise ApiError(
                ErrorCode.SOURCE_ERROR, f"Idealista API returned HTTP {response.status_code}."
            )
        data = response.json()
        results: list[Result] = []
        for item in data.get("elementList") or []:
            url = item.get("url")
            if not is_http_url(url):
                continue
            location = ", ".join(
                str(p)
                for p in (item.get("address"), item.get("district"), item.get("municipality"))
                if p
            )
            subtitle = (item.get("suggestedTexts") or {}).get("title")
            results.append(
                HousingResult(
                    url=url,
                    title=subtitle or location or None,
                    snippet=truncate(item.get("description"), 400) or None,
                    operation=item.get("operation")
                    if item.get("operation") in ("sale", "rent")
                    else query.operation,
                    property_type=item.get("propertyType"),
                    price=item.get("price"),
                    currency="EUR",
                    area_m2=item.get("size"),
                    rooms=item.get("rooms"),
                    bathrooms=item.get("bathrooms"),
                    location=location or None,
                    image=item.get("thumbnail") if is_http_url(item.get("thumbnail")) else None,
                    source="idealista.com",
                    providers=[self.name],
                    extra={
                        "property_code": item.get("propertyCode"),
                        "latitude": item.get("latitude"),
                        "longitude": item.get("longitude"),
                    },
                )
            )
        return results


HOUSING_SITES = (
    "fotocasa.es",
    "pisos.com",
    "habitaclia.com",
    "idealista.com/inmueble",
    "yaencontre.com",
)


class WebHousingAdapter(SearxngAdapter):
    """Listings from other portals found through general web search (heuristic)."""

    default_enabled = False

    def __init__(self) -> None:
        super().__init__(
            name="web_housing",
            category="general",
            result_model=HousingResult,
            title="Web search (real-estate portals)",
            description="SearXNG restricted to real-estate portals (opt-in, heuristic parsing).",
        )
        self._query: HousingQuery | None = None

    def build_query(self, query: BaseQuery) -> str:
        assert isinstance(query, HousingQuery)
        words = {"sale": "venta", "rent": "alquiler"}[query.operation]
        kind = {
            "homes": "piso",
            "rooms": "habitación",
            "offices": "oficina",
            "premises": "local",
            "garages": "garaje",
            "land": "terreno",
        }[query.property_type]
        sites = " OR ".join(f"site:{s}" for s in HOUSING_SITES)
        parts = [kind, words, query.location or "", query.q or ""]
        return f"{' '.join(p for p in parts if p)} ({sites})"

    def to_result(self, item: dict[str, Any]) -> Result | None:
        base = super().to_result(item)
        if base is None:
            return None
        text = " ".join(filter(None, [base.title, base.snippet]))
        price_match = _PRICE.search(text)
        area_match = _AREA.search(text)
        rooms_match = _ROOMS.search(text)
        return HousingResult(
            **{k: v for k, v in base.model_dump().items() if k in Result.model_fields},
            price=parse_number(price_match.group(1)) if price_match else None,
            currency="EUR" if price_match else None,
            area_m2=parse_number(area_match.group(1)) if area_match else None,
            rooms=parse_int(rooms_match.group(1)) if rooms_match else None,
        )

    async def search(self, query: BaseQuery, ctx: SourceContext) -> list[Result]:
        assert isinstance(query, HousingQuery)
        results = await super().search(query, ctx)
        out: list[Result] = []
        for result in results:
            if isinstance(result, HousingResult):
                result.operation = query.operation
                result.property_type = query.property_type
                result.location = result.location or query.location
                out.append(result)
        return out
