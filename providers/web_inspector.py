from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import time
from collections import Counter
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests

from common import HTTP_TIMEOUT, USER_AGENT


MAX_REDIRECTS = 5
MAX_HTML_BYTES = 5 * 1024 * 1024
ALLOWED_PORTS = {None, 80, 443}


class UnsafeUrlError(ValueError):
    """Raised when a URL could reach a non-public network destination."""


def normalize_url(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        raise ValueError("Enter a web page URL.")
    if "://" not in value:
        value = f"https://{value}"
    return value


def www_fallback_url(value: str) -> str | None:
    """Return a public-web-style www alternative without resolving it."""
    url = normalize_url(value)
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").rstrip(".").casefold()
    if not hostname or hostname.startswith("www.") or "." not in hostname:
        return None
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        return None
    port = f":{parsed.port}" if parsed.port is not None else ""
    candidate = urlunsplit((parsed.scheme.casefold(), f"www.{hostname}{port}", parsed.path or "/", parsed.query, ""))
    return validate_public_url(candidate, resolve=False)


def is_connection_failure(exc: BaseException) -> bool:
    """Identify failures where a browser may reasonably try another hostname."""
    if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout, socket.timeout, TimeoutError)):
        return True
    message = str(exc).casefold()
    return any(
        marker in message
        for marker in (
            "could not resolve host",
            "name or service not known",
            "temporary failure in name resolution",
            "connecttimeouterror",
            "connection timed out",
            "connection refused",
            "err_name_not_resolved",
            "err_connection_timed_out",
            "err_connection_refused",
            "err_address_unreachable",
        )
    )


def _public_addresses(hostname: str) -> list[str]:
    try:
        answers = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeUrlError(f"Could not resolve host {hostname!r}.") from exc
    addresses = sorted({answer[4][0] for answer in answers})
    if not addresses:
        raise UnsafeUrlError(f"Host {hostname!r} did not resolve to an address.")
    for value in addresses:
        address = ipaddress.ip_address(value)
        if not address.is_global:
            raise UnsafeUrlError(f"Host {hostname!r} resolves to a non-public address ({value}).")
    return addresses


def _validate_connected_peer(response: requests.Response) -> None:
    """Recheck the connected address when urllib3 exposes its live socket."""
    connection = getattr(response.raw, "_connection", None) or getattr(response.raw, "connection", None)
    sock = getattr(connection, "sock", None)
    if sock is None:
        return
    try:
        value = sock.getpeername()[0]
        address = ipaddress.ip_address(value)
    except (AttributeError, OSError, ValueError):
        return
    if not address.is_global:
        response.close()
        raise UnsafeUrlError(f"The connection reached a non-public address ({value}) and was blocked.")


def validate_public_url(value: str, *, resolve: bool = True) -> str:
    url = normalize_url(value)
    parsed = urlsplit(url)
    if parsed.scheme.casefold() not in {"http", "https"}:
        raise UnsafeUrlError("Only public http:// and https:// URLs are supported.")
    if parsed.username or parsed.password:
        raise UnsafeUrlError("URLs containing embedded credentials are not supported.")
    if not parsed.hostname:
        raise UnsafeUrlError("The URL must include a hostname.")
    if parsed.port not in ALLOWED_PORTS:
        raise UnsafeUrlError("Only standard web ports 80 and 443 are supported.")
    hostname = parsed.hostname.rstrip(".").casefold()
    if hostname == "localhost" or hostname.endswith(".localhost") or hostname.endswith(".local"):
        raise UnsafeUrlError("Local and private-network hostnames are not supported.")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        if resolve:
            _public_addresses(hostname)
    else:
        if not address.is_global:
            raise UnsafeUrlError("Local, private, reserved, and special-purpose IP addresses are not supported.")
    return urlunsplit((parsed.scheme.casefold(), parsed.netloc, parsed.path or "/", parsed.query, ""))


def redact_url(value: str) -> str:
    """Keep routing evidence while avoiding query-string value collection."""
    try:
        parsed = urlsplit(value)
        query = urlencode([(key, "<redacted>") for key, _ in parse_qsl(parsed.query, keep_blank_values=True)])
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))
    except Exception:
        return str(value or "")


def registrable_hint(hostname: str) -> str:
    """A dependency-free site boundary approximation (not a public-suffix parser)."""
    labels = (hostname or "").rstrip(".").casefold().split(".")
    if len(labels) <= 2:
        return ".".join(labels)
    common_second_level = {"co.uk", "org.uk", "com.au", "net.au", "co.nz", "co.jp", "com.br"}
    tail2 = ".".join(labels[-2:])
    return ".".join(labels[-3:]) if tail2 in common_second_level else tail2


def is_third_party(url: str, page_url: str) -> bool:
    return registrable_hint(urlsplit(url).hostname or "") != registrable_hint(urlsplit(page_url).hostname or "")


@dataclass
class ParsedPage:
    title: str = ""
    metadata: list[tuple[str, str]] = field(default_factory=list)
    resources: list[tuple[str, str]] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    forms: list[dict[str, Any]] = field(default_factory=list)
    inline_scripts: list[str] = field(default_factory=list)
    comments: list[str] = field(default_factory=list)
    json_ld: list[Any] = field(default_factory=list)


class PageParser(HTMLParser):
    RESOURCE_TAGS = {
        "script": "script",
        "img": "image",
        "iframe": "iframe",
        "source": "media",
        "video": "media",
        "audio": "media",
        "embed": "embed",
    }

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.page = ParsedPage()
        self._in_title = False
        self._title_parts: list[str] = []
        self._script_type = ""
        self._script_parts: list[str] | None = None
        self._form: dict[str, Any] | None = None

    @staticmethod
    def _attrs(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        return {str(key).casefold(): str(value or "") for key, value in attrs}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        values = self._attrs(attrs)
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            name = values.get("name") or values.get("property") or values.get("http-equiv")
            if name and values.get("content"):
                self.page.metadata.append((name, values["content"]))
        elif tag == "base" and values.get("href"):
            self.base_url = urljoin(self.base_url, values["href"])
        elif tag == "link" and values.get("href"):
            rel = values.get("rel", "link").casefold()
            kind = "stylesheet" if "stylesheet" in rel else f"link:{rel or 'other'}"
            self.page.resources.append((kind, urljoin(self.base_url, values["href"])))
        elif tag == "a" and values.get("href"):
            link_url = urljoin(self.base_url, values["href"])
            if urlsplit(link_url).scheme.casefold() in {"http", "https"}:
                self.page.links.append(link_url)
        elif tag in self.RESOURCE_TAGS:
            source = values.get("src") or values.get("data-src")
            if source:
                self.page.resources.append((self.RESOURCE_TAGS[tag], urljoin(self.base_url, source)))
            if tag == "script" and not source:
                self._script_type = values.get("type", "")
                self._script_parts = []
        elif tag == "form":
            self._form = {
                "method": values.get("method", "get").upper(),
                "action": urljoin(self.base_url, values.get("action") or self.base_url),
                "fields": [],
            }
        elif tag in {"input", "select", "textarea", "button"} and self._form is not None:
            self._form["fields"].append(
                {
                    "tag": tag,
                    "name": values.get("name", ""),
                    "type": values.get("type", "text" if tag == "input" else tag),
                    "autocomplete": values.get("autocomplete", ""),
                }
            )

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag == "title":
            self._in_title = False
            self.page.title = " ".join("".join(self._title_parts).split())
        elif tag == "script" and self._script_parts is not None:
            script = "".join(self._script_parts)
            if "ld+json" in self._script_type.casefold():
                try:
                    self.page.json_ld.append(json.loads(script))
                except (TypeError, ValueError):
                    pass
            elif script.strip():
                self.page.inline_scripts.append(script[:250_000])
            self._script_parts = None
            self._script_type = ""
        elif tag == "form" and self._form is not None:
            self.page.forms.append(self._form)
            self._form = None

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)
        if self._script_parts is not None:
            self._script_parts.append(data)

    def handle_comment(self, data: str) -> None:
        cleaned = " ".join(data.split())
        if cleaned:
            self.page.comments.append(cleaned[:500])


TECH_SIGNATURES: list[tuple[str, str, str, str]] = [
    ("WordPress", "CMS", r"wp-content|wp-includes|wordpress", "high"),
    ("Drupal", "CMS", r"drupalSettings|sites/(?:default|all)/files|Drupal\.settings", "high"),
    ("Shopify", "commerce", r"cdn\.shopify\.com|Shopify\.theme|myshopify\.com", "high"),
    ("Wix", "site builder", r"wixstatic\.com|wix\.com/website-template", "high"),
    ("Squarespace", "site builder", r"static1\.squarespace\.com|squarespace-cdn\.com", "high"),
    ("Next.js", "web framework", r"/_next/|__NEXT_DATA__", "high"),
    ("Nuxt", "web framework", r"/_nuxt/|__NUXT__", "high"),
    ("React", "JavaScript framework", r"react(?:\.production)?(?:\.min)?\.js|data-reactroot|react-dom", "medium"),
    ("Vue.js", "JavaScript framework", r"vue(?:\.runtime)?(?:\.global)?(?:\.min)?\.js|data-v-[0-9a-f]", "medium"),
    ("Angular", "JavaScript framework", r"angular(?:\.min)?\.js|ng-version=|platform-browser", "medium"),
    ("jQuery", "JavaScript library", r"jquery(?:-|\.)[0-9]|jquery(?:\.min)?\.js", "high"),
    ("Bootstrap", "UI framework", r"bootstrap(?:\.bundle)?(?:\.min)?\.(?:css|js)", "high"),
    ("Google Tag Manager", "tag manager", r"googletagmanager\.com/(?:gtm\.js|ns\.html)|GTM-[A-Z0-9]+", "high"),
    ("Google Analytics", "analytics", r"google-analytics\.com|googletagmanager\.com/gtag/js|GoogleAnalyticsObject", "high"),
    ("Meta Pixel", "advertising/analytics", r"connect\.facebook\.net/.*/fbevents\.js|fbq\(", "high"),
    ("Hotjar", "behavior analytics", r"static\.hotjar\.com|hotjar\.com/c/hotjar-", "high"),
    ("Segment", "customer data platform", r"cdn\.segment\.com|analytics\.load\(", "high"),
    ("OneTrust", "consent management", r"cdn\.cookielaw\.org|OptanonWrapper", "high"),
    ("HubSpot", "marketing/CRM", r"js\.hs-scripts\.com|js\.hsforms\.net|_hsq", "high"),
    ("Cloudflare", "CDN/security", r"cdnjs\.cloudflare\.com|__cf_bm|cloudflareinsights\.com", "medium"),
    ("Google reCAPTCHA", "bot protection", r"google\.com/recaptcha|gstatic\.com/recaptcha", "high"),
    ("Brevo", "email marketing/forms", r"sibforms\.com|sendinblue\.com", "high"),
    ("Google Site Kit", "WordPress plugin", r"Site Kit by Google|google-site-kit", "high"),
    ("Yoast SEO", "WordPress plugin", r"yoast|wpseo|yoast-schema-graph", "high"),
    ("Elementor", "WordPress plugin", r"wp-content/plugins/elementor|elementor-frontend", "high"),
    ("WooCommerce", "WordPress plugin", r"wp-content/plugins/woocommerce|woocommerce-layout", "high"),
    ("WPForms", "WordPress plugin", r"wp-content/plugins/wpforms|wpforms-container", "high"),
]


SCRIPT_BEHAVIORS: list[tuple[str, str]] = [
    ("localStorage", r"\blocalStorage\b"),
    ("sessionStorage", r"\bsessionStorage\b"),
    ("browser cookies", r"\bdocument\.cookie\b"),
    ("Fetch API", r"\bfetch\s*\("),
    ("XMLHttpRequest", r"\bXMLHttpRequest\b"),
    ("Beacon API", r"\bnavigator\.sendBeacon\b"),
    ("geolocation API", r"\bnavigator\.geolocation\b"),
    ("service worker", r"\bserviceWorker\.(?:register|getRegistration)\b"),
    ("WebSocket", r"\bWebSocket\s*\("),
]


def _cookie_findings(response: requests.Response) -> list[dict[str, Any]]:
    raw_headers = getattr(response.raw, "headers", None)
    if raw_headers is not None and hasattr(raw_headers, "getlist"):
        values = raw_headers.getlist("Set-Cookie")
    else:
        values = [response.headers.get("Set-Cookie", "")] if response.headers.get("Set-Cookie") else []
    findings: list[dict[str, Any]] = []
    for raw in values:
        jar = SimpleCookie()
        try:
            jar.load(raw)
        except Exception:
            continue
        for name, morsel in jar.items():
            details = {
                key: morsel[key]
                for key in ("domain", "path", "expires", "max-age", "secure", "httponly", "samesite")
                if morsel[key]
            }
            findings.append(
                finding("cookie", name, response.url, details, "observed", "Response Set-Cookie header; value intentionally omitted.")
            )
    return findings


def finding(category: str, name: str, url: str = "", details: Any = "", basis: str = "observed", evidence: str = "") -> dict[str, Any]:
    return {
        "category": category,
        "name": name,
        "url": redact_url(url),
        "host": urlsplit(url).hostname or "" if url else "",
        "third_party": "",
        "basis": basis,
        "evidence": evidence,
        "details": details,
    }


def _version_hint(url: str) -> str:
    for key, value in parse_qsl(urlsplit(url).query, keep_blank_values=False):
        if key.casefold() in {"ver", "version", "v"} and re.fullmatch(r"v?\d+(?:\.\d+){1,3}", value, flags=re.IGNORECASE):
            return value.lstrip("vV")
    return ""


def _linked_domain_findings(page: ParsedPage, page_url: str, *, rendered: bool = False) -> list[dict[str, Any]]:
    page_domain = registrable_hint(urlsplit(page_url).hostname or "")
    grouped: dict[str, list[str]] = {}
    for link_url in page.links:
        domain = registrable_hint(urlsplit(link_url).hostname or "")
        if not domain or domain == page_domain:
            continue
        grouped.setdefault(domain, []).append(link_url)
    category = "rendered_linked_domain" if rendered else "linked_domain"
    evidence = "Clickable off-site links present in the rendered DOM." if rendered else "Clickable off-site links declared in the page HTML."
    out: list[dict[str, Any]] = []
    for domain, urls in sorted(grouped.items()):
        unique_urls = list(dict.fromkeys(redact_url(url) for url in urls))
        row = finding(
            category,
            domain,
            f"https://{domain}/",
            {"link_count": len(urls), "sample_urls": unique_urls[:5]},
            "observed",
            evidence,
        )
        row["third_party"] = "yes"
        out.append(row)
    return out


def _wordpress_component_findings(urls: Iterable[str], page_url: str, *, rendered: bool = False) -> list[dict[str, Any]]:
    components: dict[tuple[str, str], dict[str, Any]] = {}
    for url in urls:
        path = urlsplit(url).path
        for kind, pattern in (
            ("plugin", r"/wp-content/plugins/([^/]+)/"),
            ("theme", r"/wp-content/themes/([^/]+)/"),
        ):
            match = re.search(pattern, path, flags=re.IGNORECASE)
            if not match:
                continue
            slug = match.group(1)
            record = components.setdefault((kind, slug.casefold()), {"slug": slug, "assets": 0, "versions": set()})
            record["assets"] += 1
            version = _version_hint(url)
            if version:
                record["versions"].add(version)
    prefix = "rendered_" if rendered else ""
    out: list[dict[str, Any]] = []
    for (kind, _), record in sorted(components.items()):
        versions = sorted(record["versions"])
        details = {"slug": record["slug"], "asset_count": record["assets"]}
        if versions:
            details["version_hints"] = versions
        out.append(
            finding(
                f"{prefix}wordpress_{kind}",
                record["slug"],
                page_url,
                details,
                "inferred",
                f"WordPress {kind} directory observed in public asset URLs; verify the component and version independently.",
            )
        )
    return out


class WebInspector:
    def __init__(self, *, session: requests.Session | None = None, timeout: float = HTTP_TIMEOUT) -> None:
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5"})
        self.timeout = min(max(float(timeout), 1.0), 60.0)

    def fetch(self, target: str) -> tuple[requests.Response, str]:
        current = validate_public_url(target)
        for redirect_count in range(MAX_REDIRECTS + 1):
            response = self.session.get(current, timeout=self.timeout, allow_redirects=False, stream=True)
            _validate_connected_peer(response)
            if response.is_redirect or response.is_permanent_redirect:
                if redirect_count >= MAX_REDIRECTS:
                    response.close()
                    raise RuntimeError(f"The page exceeded {MAX_REDIRECTS} redirects.")
                location = response.headers.get("Location")
                response.close()
                if not location:
                    raise RuntimeError("The site returned a redirect without a destination.")
                current = validate_public_url(urljoin(current, location))
                continue
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "").casefold()
            if "html" not in content_type and "xhtml" not in content_type:
                response.close()
                raise ValueError(f"The URL did not return an HTML page (Content-Type: {content_type or 'unknown'}).")
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                size += len(chunk)
                if size > MAX_HTML_BYTES:
                    response.close()
                    raise ValueError(f"The HTML response exceeded the {MAX_HTML_BYTES // (1024 * 1024)} MB safety limit.")
                chunks.append(chunk)
            body = b"".join(chunks)
            encoding = response.encoding
            if not encoding:
                charset_match = re.search(br"charset\s*=\s*['\"]?\s*([A-Za-z0-9._-]+)", body[:8192], flags=re.IGNORECASE)
                encoding = charset_match.group(1).decode("ascii", errors="ignore") if charset_match else "utf-8"
            try:
                html = body.decode(encoding, errors="replace")
            except LookupError:
                html = body.decode("utf-8", errors="replace")
            return response, html
        raise RuntimeError("Redirect processing failed.")

    def static_findings(self, target: str) -> tuple[str, list[dict[str, Any]]]:
        response, html = self.fetch(target)
        page_url = response.url
        parser = PageParser(page_url)
        parser.feed(html)
        page = parser.page
        out: list[dict[str, Any]] = []
        out.append(
            finding(
                "page",
                page.title or urlsplit(page_url).hostname or "page",
                page_url,
                {"status": response.status_code, "content_type": response.headers.get("Content-Type", ""), "html_bytes": len(html.encode('utf-8'))},
                "observed",
                "Final HTML response after validated redirects.",
            )
        )

        for key, value in response.headers.items():
            if key.casefold() in {"set-cookie", "cookie", "authorization", "proxy-authorization"}:
                continue
            basis = "observed"
            out.append(finding("response_header", key, page_url, value[:1000], basis, "HTTP response header."))
        out.extend(_cookie_findings(response))

        for name, value in page.metadata:
            out.append(finding("metadata", name, page_url, value[:2000], "observed", "HTML meta element."))
        for value in page.json_ld:
            types: list[str] = []
            nodes = value if isinstance(value, list) else [value]
            for node in nodes:
                if isinstance(node, dict):
                    item_type = node.get("@type")
                    if isinstance(item_type, list):
                        types.extend(str(item) for item in item_type)
                    elif item_type:
                        types.append(str(item_type))
            out.append(finding("structured_data", ", ".join(types) or "JSON-LD", page_url, value, "observed", "JSON-LD embedded in the page."))

        resource_counts: Counter[tuple[str, str]] = Counter()
        third_party_counts: Counter[tuple[str, str]] = Counter()
        for kind, url in page.resources:
            if urlsplit(url).scheme.casefold() not in {"http", "https"}:
                continue
            resource_counts[(kind, url)] += 1
            if is_third_party(url, page_url):
                third_party_counts[(urlsplit(url).hostname or "", kind)] += 1
        for (kind, url), count in resource_counts.items():
            details: dict[str, Any] = {"references": count}
            version = _version_hint(url)
            if version:
                details["version_hint"] = version
            row = finding("resource", kind, url, details, "observed", f"HTML {kind} reference.")
            row["third_party"] = "yes" if is_third_party(url, page_url) else "no"
            out.append(row)
        for (host, kind), count in third_party_counts.items():
            url = f"https://{host}/"
            row = finding("third_party_destination", host, url, {"resource_type": kind, "references": count}, "observed", "Off-site hostname referenced by page HTML.")
            row["third_party"] = "yes"
            out.append(row)
        out.extend(_linked_domain_findings(page, page_url))
        out.extend(_wordpress_component_findings((url for _, url in page.resources), page_url))

        for index, form in enumerate(page.forms, start=1):
            action = form["action"]
            safe_fields = [
                {key: value for key, value in field.items() if value}
                for field in form["fields"]
            ]
            row = finding("form", f"Form {index} ({form['method']})", action, {"fields": safe_fields}, "observed", "HTML form declaration; no values were submitted.")
            row["third_party"] = "yes" if is_third_party(action, page_url) else "no"
            out.append(row)

        signature_text = "\n".join([html[:1_000_000], *[url for _, url in page.resources], *[f"{k}={v}" for k, v in page.metadata]])
        for technology, tech_type, pattern, confidence in TECH_SIGNATURES:
            match = re.search(pattern, signature_text, flags=re.IGNORECASE)
            if match:
                out.append(
                    finding(
                        "technology",
                        technology,
                        page_url,
                        {"type": tech_type, "confidence": confidence},
                        "inferred",
                        f"Matched public page signature: {match.group(0)[:160]}",
                    )
                )
        server = response.headers.get("Server")
        powered_by = response.headers.get("X-Powered-By")
        generator = next((value for name, value in page.metadata if name.casefold() == "generator"), "")
        for name, value in (("Server", server), ("X-Powered-By", powered_by), ("Generator", generator)):
            if value:
                out.append(finding("technology_claim", name, page_url, value[:500], "self-reported", f"Value supplied by the site's {name} signal."))

        inline_text = "\n".join(page.inline_scripts)
        for behavior, pattern in SCRIPT_BEHAVIORS:
            if re.search(pattern, inline_text, flags=re.IGNORECASE):
                out.append(finding("script_behavior", behavior, page_url, "", "inferred", "API name appears in inline JavaScript; this does not prove execution or data transmission."))
        for comment in page.comments[:25]:
            if re.search(r"(?:built|developed|designed|powered|author|copyright|version|theme|plugin|generator)", comment, flags=re.IGNORECASE):
                out.append(finding("developer_note", "HTML comment", page_url, comment, "self-reported", "Attribution-like text in an HTML comment; verify independently."))
        return page_url, out


def rendered_findings(target: str, *, observe_seconds: float = 3.0, max_requests: int = 500) -> list[dict[str, Any]]:
    project_browsers = Path(__file__).resolve().parents[1] / "data" / "playwright-browsers"
    if project_browsers.exists():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(project_browsers)
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # pragma: no cover - depends on optional runtime
        raise RuntimeError("Rendered observation requires Playwright. Install the project requirements first.") from exc

    target = validate_public_url(target)
    observe_seconds = min(max(float(observe_seconds), 0.0), 15.0)
    max_requests = min(max(int(max_requests), 25), 1000)
    out: list[dict[str, Any]] = []
    requests_seen: dict[int, dict[str, Any]] = {}
    host_decisions: dict[str, str | None] = {}

    def route_request(route: Any) -> None:
        request = route.request
        request_url = request.url
        parsed = urlsplit(request_url)
        if parsed.scheme.casefold() not in {"http", "https"}:
            route.continue_()
            return
        hostname = parsed.hostname or ""
        if hostname not in host_decisions:
            try:
                validate_public_url(request_url)
                host_decisions[hostname] = None
            except Exception as exc:
                host_decisions[hostname] = str(exc)
        reason = host_decisions[hostname]
        if reason:
            out.append(finding("blocked_request", hostname or "invalid URL", request_url, "", "observed", reason))
            route.abort("blockedbyclient")
        elif len(requests_seen) >= max_requests:
            route.abort("blockedbyclient")
        else:
            route.continue_()

    def on_request(request: Any) -> None:
        if len(requests_seen) >= max_requests:
            return
        parsed = urlsplit(request.url)
        query_names = sorted({key for key, _ in parse_qsl(parsed.query, keep_blank_values=True)})
        request_details: dict[str, Any] = {"method": request.method, "query_parameter_names": query_names}
        version = _version_hint(request.url)
        if version:
            request_details["version_hint"] = version
        record = finding(
            "network_request",
            request.resource_type,
            request.url,
            request_details,
            "observed",
            "Browser request observed; request bodies, cookie values, and query values were not collected.",
        )
        record["third_party"] = "yes" if is_third_party(request.url, target) else "no"
        requests_seen[id(request)] = record
        out.append(record)

    def on_response(response: Any) -> None:
        record = requests_seen.get(id(response.request))
        if record is not None:
            details = dict(record["details"])
            details["status"] = response.status
            try:
                content_type = response.header_value("content-type") or ""
            except Exception:
                content_type = ""
            if content_type:
                details["content_type"] = content_type
            if record.get("name") == "document":
                selected_headers: dict[str, str] = {}
                for header in (
                    "server",
                    "content-security-policy",
                    "strict-transport-security",
                    "permissions-policy",
                    "referrer-policy",
                    "x-frame-options",
                    "x-content-type-options",
                ):
                    try:
                        value = response.header_value(header) or ""
                    except Exception:
                        value = ""
                    if value:
                        selected_headers[header] = value[:1000]
                if selected_headers:
                    details["selected_response_headers"] = selected_headers
            record["details"] = details

    with sync_playwright() as playwright:
        try:
            local_chrome = next(project_browsers.glob("chromium-*/chrome-win64/chrome.exe"), None)
            launch_options: dict[str, Any] = {"headless": True}
            if local_chrome is not None:
                launch_options["executable_path"] = str(local_chrome)
            browser = playwright.chromium.launch(**launch_options)
        except Exception as exc:
            raise RuntimeError(
                "Could not launch Playwright Chromium. "
                f"Details: {exc}. Run 'playwright install chromium' once if the executable is missing."
            ) from exc
        # Let Chromium use its own browser user agent. The static pass identifies
        # itself as this research tool; rendered mode is explicitly a browser visit.
        context = browser.new_context(service_workers="block")
        context.add_init_script(
            """(() => {
              class BlockedWebSocket {
                constructor() { throw new DOMException('WebSockets are disabled by the page inspector', 'SecurityError'); }
              }
              Object.defineProperty(window, 'WebSocket', {value: BlockedWebSocket, configurable: false});
            })();"""
        )
        context.route("**/*", route_request)
        page = context.new_page()
        page.on("request", on_request)
        page.on("response", on_response)
        try:
            page.goto(target, wait_until="domcontentloaded", timeout=min(int(HTTP_TIMEOUT * 1000), 60_000))
            if observe_seconds:
                time.sleep(observe_seconds)
            try:
                rendered_html = page.content()
                parser = PageParser(page.url)
                parser.feed(rendered_html[:MAX_HTML_BYTES])
                parsed_page = parser.page
                out.append(
                    finding(
                        "rendered_page",
                        parsed_page.title or urlsplit(page.url).hostname or "page",
                        page.url,
                        {"html_characters": min(len(rendered_html), MAX_HTML_BYTES)},
                        "observed",
                        "DOM present after the temporary browser loaded the page.",
                    )
                )
                for name, value in parsed_page.metadata:
                    out.append(finding("rendered_metadata", name, page.url, value[:2000], "observed", "Meta element in the rendered DOM."))
                out.extend(_linked_domain_findings(parsed_page, page.url, rendered=True))
                out.extend(
                    _wordpress_component_findings(
                        (str(row.get("url") or "") for row in out if row.get("category") == "network_request"),
                        page.url,
                        rendered=True,
                    )
                )
                for index, form in enumerate(parsed_page.forms, start=1):
                    action = form["action"]
                    row = finding(
                        "rendered_form",
                        f"Form {index} ({form['method']})",
                        action,
                        {"fields": [{key: value for key, value in field.items() if value} for field in form["fields"]]},
                        "observed",
                        "Form present in the rendered DOM; no values were submitted.",
                    )
                    row["third_party"] = "yes" if is_third_party(action, page.url) else "no"
                    out.append(row)
                signature_text = "\n".join([rendered_html[:1_000_000], *[str(row.get("url") or "") for row in out if row.get("category") == "network_request"]])
                for technology, tech_type, pattern, confidence in TECH_SIGNATURES:
                    match = re.search(pattern, signature_text, flags=re.IGNORECASE)
                    if match:
                        out.append(
                            finding(
                                "rendered_technology",
                                technology,
                                page.url,
                                {"type": tech_type, "confidence": confidence},
                                "inferred",
                                f"Matched rendered-page signature: {match.group(0)[:160]}",
                            )
                        )
            except Exception:
                pass
            for cookie in context.cookies():
                details = {key: cookie.get(key) for key in ("domain", "path", "expires", "httpOnly", "secure", "sameSite") if cookie.get(key) not in (None, "")}
                out.append(finding("browser_cookie", cookie.get("name", "cookie"), page.url, details, "observed", "Cookie present after page load; value intentionally omitted."))
            try:
                storage = page.evaluate(
                    """() => ({
                      localStorage: Object.keys(window.localStorage),
                      sessionStorage: Object.keys(window.sessionStorage)
                    })"""
                )
                for storage_type, keys in storage.items():
                    for key in keys:
                        out.append(finding("browser_storage", str(key), page.url, {"storage_type": storage_type}, "observed", "Storage key present after page load; value intentionally omitted."))
            except Exception:
                pass
        finally:
            context.close()
            browser.close()
    return out


def summarize_destinations(findings: Iterable[dict[str, Any]]) -> Counter[str]:
    return Counter(str(row.get("host") or "") for row in findings if row.get("third_party") == "yes" and row.get("host"))
