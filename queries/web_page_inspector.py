from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from typing import Any, Iterator
from urllib.parse import urlsplit

from common import get_form_bool, h, parse_int
from osint_common import CORE_HEADERS, compact_json, core_row
from providers.web_inspector import (
    WebInspector,
    finding,
    is_connection_failure,
    redact_url,
    registrable_hint,
    rendered_findings,
    www_fallback_url,
)
from queries._shared import export_core, run_core


HEADERS = CORE_HEADERS + ["category", "finding_name", "finding_url", "host", "third_party", "basis", "evidence", "details"]

META = {
    "key": "web_page_inspector",
    "name": "Web Page Technology & Data-Flow Inspector",
    "description": "Passively inspect a public web page's metadata, technologies, resources, forms, cookies, storage signals, and third-party destinations.",
    "source_type": "public_web",
    "limitations": [
        "Observations describe what a public page declares or does during a short visit; they do not prove an organization's broader data practices.",
        "Technology matches are signature-based leads and may be incomplete or incorrect. Verify important findings independently.",
        "Rendered observation executes the page in an isolated, temporary browser and may trigger ordinary page-view analytics. It does not log in, click, submit forms, or bypass controls.",
        "Service workers and WebSockets are disabled in rendered mode as network-safety boundaries, so activity that depends on them will not be observed.",
        "Cookie values, storage values, request bodies, and query-string values are intentionally not collected; recognizable software-version parameters may be retained as version hints.",
        "Local, private, reserved, and non-standard-port destinations are blocked to prevent internal-network scanning.",
    ],
    "headers": HEADERS,
}


def _targets(raw: Any) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for line in str(raw or "").splitlines():
        value = line.strip()
        if value and value.casefold() not in seen:
            values.append(value)
            seen.add(value.casefold())
    if not values:
        raise ValueError("Enter at least one public web page URL.")
    if len(values) > 10:
        raise ValueError("Inspect at most 10 page URLs in one run.")
    return values


def render_fields(form: dict[str, Any]) -> str:
    inspection_mode = str(form.get("inspection_mode") or "browser").casefold()
    browser_selected = "selected" if inspection_mode == "browser" else ""
    static_selected = "selected" if inspection_mode == "static" else ""
    return f"""
    <div class="grid">
      <div class="row" style="grid-column: 1 / -1;">
        <label>Public page URLs (one per line, maximum 10)</label>
        <textarea name="urls" placeholder="https://www.example.org/about">{h(form.get("urls", ""))}</textarea>
      </div>
      <div class="row">
        <label>Inspection mode</label>
        <select name="inspection_mode">
          <option value="browser" {browser_selected}>Real browser observation (recommended)</option>
          <option value="static" {static_selected}>Static HTML request only</option>
        </select>
        <div class="subtle">Browser mode supports HTTPS and can work when a site rejects non-browser requests. It can trigger normal page-view analytics.</div>
      </div>
      <div class="row">
        <label>Observation time after page load, seconds</label>
        <input type="number" name="observe_seconds" min="0" max="15" step="0.5" value="{h(form.get("observe_seconds", "3"))}">
      </div>
      <div class="row">
        <label>Maximum observed requests per page</label>
        <input type="number" name="max_requests" min="25" max="1000" value="{h(form.get("max_requests", "500"))}">
      </div>
    </div>
    <div class="notice">
      Browser mode uses a fresh temporary browser context and also attempts a lightweight static inspection for additional headers.
      Review the target site's terms and applicable law before collecting or publishing results.
    </div>
    """


def _row(target: str, page_url: str, item: dict[str, Any], *, source_api: str) -> dict[str, Any]:
    safe_target = redact_url(target)
    details = item.get("details", "")
    fingerprint = compact_json(
        {
            "target": safe_target,
            "category": item.get("category"),
            "name": item.get("name"),
            "url": item.get("url"),
            "details": details,
        }
    )
    platform_item_id = hashlib.sha256(fingerprint.encode("utf-8", errors="replace")).hexdigest()
    evidence = str(item.get("evidence") or "")
    name = str(item.get("name") or "")
    category = str(item.get("category") or "finding")
    return core_row(
        source_platform="Public web page",
        source_api=source_api,
        source_type=META["source_type"],
        target_input=safe_target,
        query_text=safe_target,
        canonical_url=str(item.get("url") or page_url),
        text=f"{category}: {name}",
        media_summary=category,
        raw_json=item,
        notes=evidence,
        platform_item_id=platform_item_id,
        category=category,
        finding_name=name,
        finding_url=item.get("url") or "",
        host=item.get("host") or "",
        third_party=item.get("third_party") or "",
        basis=item.get("basis") or "",
        evidence=evidence,
        details=compact_json(details),
    )


def iter_row_dicts(form: dict[str, Any]) -> Iterator[dict[str, Any]]:
    inspector = WebInspector()
    mode = str(form.get("inspection_mode") or "browser").casefold()
    # Keep compatibility with forms submitted by the first version of this module.
    render_page = mode == "browser" or get_form_bool(form, "render_page", False)
    try:
        observe_seconds = min(max(float(form.get("observe_seconds") or 3), 0.0), 15.0)
    except (TypeError, ValueError):
        observe_seconds = 3.0
    max_requests = parse_int(form.get("max_requests", 500), 500, 25, 1000)

    for target in _targets(form.get("urls")):
        page_url = target
        fallback_url: str | None = None
        try:
            page_url, static_items = inspector.static_findings(target)
        except Exception as exc:
            if is_connection_failure(exc):
                fallback_url = www_fallback_url(target)
            if fallback_url:
                fallback_item = finding(
                    "hostname_fallback",
                    "Tried www hostname after connection failure",
                    fallback_url,
                    {
                        "submitted_url": redact_url(target),
                        "fallback_url": redact_url(fallback_url),
                        "trigger_error_type": type(exc).__name__,
                    },
                    "inferred",
                    "The submitted hostname did not establish a connection, so the inspector tried its www-prefixed alternative. No redirect from the submitted hostname was observed.",
                )
                yield _row(target, fallback_url, fallback_item, source_api="hostname fallback")
                try:
                    page_url, static_items = inspector.static_findings(fallback_url)
                except Exception as fallback_exc:
                    error_item = finding(
                        "error",
                        "Static inspection failed after hostname fallback",
                        fallback_url,
                        {
                            "error": str(exc),
                            "fallback_url": redact_url(fallback_url),
                            "fallback_error": str(fallback_exc),
                        },
                        "observed",
                        f"The submitted hostname failed: {exc} The www alternative also failed during static inspection: {fallback_exc}",
                    )
                    yield _row(target, fallback_url, error_item, source_api="static HTML inspection")
                    page_url = fallback_url
                    if not render_page:
                        continue
                else:
                    for item in static_items:
                        yield _row(target, page_url, item, source_api="static HTML inspection")
            else:
                suggestion = ""
                if "403" in str(exc):
                    suggestion = " The site denied the plain HTML request. Enable rendered observation to try a normal temporary browser visit."
                error_item = finding("error", "Static inspection failed", target, {"error": str(exc)}, "observed", f"{exc}{suggestion}")
                yield _row(target, target, error_item, source_api="static HTML inspection")
                if not render_page:
                    continue
        else:
            for item in static_items:
                yield _row(target, page_url, item, source_api="static HTML inspection")

        if render_page:
            try:
                browser_items = rendered_findings(page_url, observe_seconds=observe_seconds, max_requests=max_requests)
            except Exception as exc:
                if fallback_url is None and is_connection_failure(exc):
                    browser_fallback = www_fallback_url(page_url)
                    if browser_fallback:
                        fallback_item = finding(
                            "hostname_fallback",
                            "Tried www hostname after browser connection failure",
                            browser_fallback,
                            {
                                "submitted_url": redact_url(target),
                                "fallback_url": redact_url(browser_fallback),
                                "trigger_error_type": type(exc).__name__,
                            },
                            "inferred",
                            "The browser could not connect to the submitted hostname, so the inspector tried its www-prefixed alternative. No redirect from the submitted hostname was observed.",
                        )
                        yield _row(target, browser_fallback, fallback_item, source_api="hostname fallback")
                        try:
                            browser_items = rendered_findings(
                                browser_fallback,
                                observe_seconds=observe_seconds,
                                max_requests=max_requests,
                            )
                        except Exception as fallback_exc:
                            error_item = finding(
                                "error",
                                "Rendered observation failed after hostname fallback",
                                browser_fallback,
                                {
                                    "error": str(exc),
                                    "fallback_url": redact_url(browser_fallback),
                                    "fallback_error": str(fallback_exc),
                                },
                                "observed",
                                f"The submitted hostname failed: {exc} The www alternative also failed in the browser: {fallback_exc}",
                            )
                            yield _row(target, browser_fallback, error_item, source_api="rendered browser observation")
                            continue
                        for item in browser_items:
                            yield _row(target, browser_fallback, item, source_api="rendered browser observation")
                        continue
                error_item = finding("error", "Rendered observation failed", page_url, {"error": str(exc)}, "observed", str(exc))
                yield _row(target, page_url, error_item, source_api="rendered browser observation")
                continue
            for item in browser_items:
                yield _row(target, page_url, item, source_api="rendered browser observation")


def run(form: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    safe_form = _safe_form_for_storage(form)
    return run_core(META, "Public web page", safe_form, lambda: iter_row_dicts(form), HEADERS)


def export_headers(form: dict[str, Any]) -> list[str]:
    return HEADERS


def export_rows(form: dict[str, Any]) -> Iterator[list[Any]]:
    safe_form = _safe_form_for_storage(form)
    yield from export_core(META, "Public web page", safe_form, lambda: iter_row_dicts(form), HEADERS)


def _safe_form_for_storage(form: dict[str, Any]) -> dict[str, Any]:
    safe_form = {key: value for key, value in form.items() if key != "_files"}
    safe_urls: list[str] = []
    for value in str(form.get("urls") or "").splitlines():
        value = value.strip()
        if value:
            safe_urls.append(redact_url(value))
    safe_form["urls"] = "\n".join(safe_urls)
    return safe_form


def render_results(form: dict[str, Any], headers: list[str], rows: list[list[Any]]) -> str:
    index = {name: position for position, name in enumerate(headers)}
    mapped = [{name: row[position] for name, position in index.items()} for row in rows]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in mapped:
        grouped.setdefault(str(row.get("target_input") or "Page"), []).append(row)
    dashboards = "".join(_render_dashboard_group(target, group) for target, group in grouped.items())
    return _dashboard_styles() + dashboards


SERVICE_NAMES = {
    "google-analytics.com": "Google Analytics",
    "googletagmanager.com": "Google Tag Manager",
    "google.com": "Google / reCAPTCHA",
    "gstatic.com": "Google static services",
    "sibforms.com": "Brevo forms/email marketing",
    "sendinblue.com": "Brevo email marketing",
    "facebook.com": "Meta / Facebook",
    "youtube.com": "YouTube",
    "cloudflare.com": "Cloudflare",
}

COMPONENT_NAMES = {
    "twentytwentyfive": "Twenty Twenty-Five",
    "twentytwentyfour": "Twenty Twenty-Four",
    "twentytwentythree": "Twenty Twenty-Three",
    "light-modal-block": "Light Modal Block",
}


def _details(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("details")
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or ""))
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _friendly_slug(value: str) -> str:
    return COMPONENT_NAMES.get(value.casefold(), " ".join(part.capitalize() for part in re.split(r"[-_]", value) if part))


def _service_name(domain: str) -> str:
    return SERVICE_NAMES.get(domain, domain)


def _render_dashboard_group(target: str, rows: list[dict[str, Any]]) -> str:
    categories = Counter(str(row.get("category") or "") for row in rows)
    rendered_ok = any(row.get("category") == "rendered_page" for row in rows)
    page_row = next((row for row in rows if row.get("category") in {"rendered_page", "page"}), {})
    page_title = str(page_row.get("finding_name") or urlsplit(target).hostname or "Web page")
    page_url = str(page_row.get("finding_url") or target)

    technology_rows = [row for row in rows if row.get("category") in {"technology", "rendered_technology"}]
    technologies: dict[str, dict[str, Any]] = {}
    for row in technology_rows:
        technologies.setdefault(str(row.get("finding_name") or "Unknown"), _details(row))

    metadata = {
        str(row.get("finding_name") or "").casefold(): str(row.get("details") or "")
        for row in rows
        if row.get("category") in {"metadata", "rendered_metadata"}
    }
    generator = metadata.get("generator", "")
    generator_match = re.match(r"(.+?)\s+(\d+(?:\.\d+)+)$", generator)
    if generator_match:
        generator_name = generator_match.group(1)
        canonical_name = "Google Site Kit" if generator_name.casefold() == "site kit by google" else generator_name
        generator_details = technologies.setdefault(canonical_name, {"type": "plugin/generator", "confidence": "self-reported"})
        generator_details["version"] = generator_match.group(2)

    component_rows = [
        row
        for row in rows
        if row.get("category") in {
            "wordpress_plugin",
            "rendered_wordpress_plugin",
            "wordpress_theme",
            "rendered_wordpress_theme",
        }
    ]
    components: dict[tuple[str, str], dict[str, Any]] = {}
    for row in component_rows:
        component_type = "Theme" if "theme" in str(row.get("category")) else "Plugin"
        slug = str(row.get("finding_name") or "")
        details = _details(row)
        components[(component_type, slug)] = details

    network_rows = [row for row in rows if row.get("category") == "network_request"]
    main_document = next(
        (
            row
            for row in network_rows
            if row.get("finding_name") == "document"
            and registrable_hint(str(row.get("host") or "")) == registrable_hint(urlsplit(page_url).hostname or "")
        ),
        {},
    )
    selected_headers = _details(main_document).get("selected_response_headers") or {}
    for row in network_rows:
        path = urlsplit(str(row.get("finding_url") or "")).path
        for component_type, pattern in (("Plugin", r"/wp-content/plugins/([^/]+)/"), ("Theme", r"/wp-content/themes/([^/]+)/")):
            match = re.search(pattern, path, flags=re.IGNORECASE)
            if match:
                key = (component_type, match.group(1))
                details = components.setdefault(key, {"slug": match.group(1)})
                version = _details(row).get("version_hint")
                if version:
                    details.setdefault("version_hints", [])
                    if version not in details["version_hints"]:
                        details["version_hints"].append(version)

    platform = "Not confidently identified"
    platform_version = ""
    for name, details in technologies.items():
        if str(details.get("type") or "").casefold() in {"cms", "site builder", "commerce"}:
            platform = name
            platform_version = str(details.get("version") or "")
            break
    if platform == "WordPress" and not platform_version:
        core_versions = Counter(
            str(_details(row).get("version_hint") or "")
            for row in network_rows
            if "/wp-includes/" in str(row.get("finding_url") or "") and _details(row).get("version_hint")
        )
        if core_versions:
            platform_version = core_versions.most_common(1)[0][0]

    cookie_names = sorted(
        {
            str(row.get("finding_name") or "")
            for row in rows
            if row.get("category") in {"cookie", "browser_cookie"} and row.get("finding_name")
        }
    )
    storage_names = sorted(
        {
            str(row.get("finding_name") or "")
            for row in rows
            if row.get("category") == "browser_storage" and row.get("finding_name")
        }
    )
    form_rows = [row for row in rows if row.get("category") in {"form", "rendered_form"}]
    external_forms = [row for row in form_rows if row.get("third_party") == "yes"]

    services: dict[str, dict[str, Any]] = {}
    for row in network_rows:
        if row.get("third_party") != "yes":
            continue
        domain = registrable_hint(str(row.get("host") or ""))
        if not domain:
            continue
        service = services.setdefault(domain, {"requests": 0, "types": Counter(), "methods": Counter()})
        service["requests"] += 1
        service["types"][str(row.get("finding_name") or "other")] += 1
        service["methods"][str(_details(row).get("method") or "GET")] += 1
    for row in external_forms:
        domain = registrable_hint(str(row.get("host") or ""))
        if domain:
            services.setdefault(domain, {"requests": 0, "types": Counter(), "methods": Counter()})["types"]["form destination"] += 1

    linked_domains: dict[str, int] = {}
    for row in rows:
        if row.get("category") not in {"linked_domain", "rendered_linked_domain"}:
            continue
        domain = registrable_hint(str(row.get("finding_name") or row.get("host") or ""))
        if domain:
            linked_domains[domain] = max(linked_domains.get(domain, 0), int(_details(row).get("link_count") or 1))

    core_tech: list[str] = ["HTML5"] if page_row else []
    if any(row.get("finding_name") == "stylesheet" for row in network_rows):
        core_tech.append("CSS")
    if any(row.get("finding_name") == "script" for row in network_rows):
        core_tech.append("JavaScript")

    status_text = "Browser observation completed" if rendered_ok else "Static inspection only"
    status_class = "status-good" if rendered_ok else "status-neutral"
    platform_label = f"{platform} {platform_version}".strip()
    kpis = [
        ("Platform", platform_label, "Detected platform; versions are hints"),
        ("Cookies", str(len(cookie_names)), ", ".join(cookie_names[:3]) or "None observed"),
        ("Browser storage", str(len(storage_names)), ", ".join(storage_names[:3]) or "None observed"),
        ("Third-party services", str(len(services)), "Domains contacted during load"),
        ("Linked websites", str(len(linked_domains)), "Clickable external domains"),
        ("Network requests", str(len(network_rows)), "Observed during the short visit"),
    ]
    kpi_html = "".join(
        f'<div class="inspector-kpi"><div class="inspector-kpi-label">{h(label)}</div><div class="inspector-kpi-value">{h(value)}</div><div class="inspector-kpi-note">{h(note)}</div></div>'
        for label, value, note in kpis
    )

    technology_chips: list[str] = []
    for name in core_tech:
        technology_chips.append(f'<span class="tech-chip"><b>{h(name)}</b><small>web standard</small></span>')
    for name, details in sorted(technologies.items()):
        version = str(details.get("version") or "")
        tech_type = str(details.get("type") or "technology")
        label = f"{name} {version}".strip()
        technology_chips.append(f'<span class="tech-chip"><b>{h(label)}</b><small>{h(tech_type)}</small></span>')
    for (component_type, slug), details in sorted(components.items()):
        versions = details.get("version_hints") or []
        version = f" {versions[0]}" if versions else ""
        technology_chips.append(f'<span class="tech-chip"><b>{h(_friendly_slug(slug) + version)}</b><small>WordPress {h(component_type.casefold())}</small></span>')
    technology_html = "".join(technology_chips) or '<span class="empty-note">No technology signatures identified.</span>'

    service_rows: list[str] = []
    for domain, info in sorted(services.items(), key=lambda item: (-item[1]["requests"], item[0])):
        types = ", ".join(sorted(info["types"]))
        methods = ", ".join(f"{method} × {count}" for method, count in sorted(info["methods"].items()))
        service_rows.append(
            f'<tr><td><b>{h(_service_name(domain))}</b><br><span class="subtle">{h(domain)}</span></td>'
            f'<td>{info["requests"]}</td><td>{h(types)}</td><td>{h(methods or "Form destination")}</td></tr>'
        )
    services_html = (
        '<table class="summary-table"><thead><tr><th>Service</th><th>Requests</th><th>Observed activity</th><th>Methods</th></tr></thead><tbody>'
        + "".join(service_rows)
        + "</tbody></table>"
        if service_rows
        else '<div class="empty-note">No third-party services were contacted during the observation window.</div>'
    )

    linked_html = (
        "".join(
            f'<a class="domain-chip" href="https://{h(domain)}/" target="_blank" rel="noreferrer">{h(domain)} <small>({count} link{("s" if count != 1 else "")})</small></a>'
            for domain, count in sorted(linked_domains.items())
        )
        or '<span class="empty-note">No clickable external domains were observed.</span>'
    )

    review_items: list[tuple[str, str]] = []
    fallback_row = next((row for row in rows if row.get("category") == "hostname_fallback"), None)
    if fallback_row:
        fallback_host = str(fallback_row.get("host") or urlsplit(str(fallback_row.get("finding_url") or "")).hostname or "the www hostname")
        review_items.append(
            (
                "info",
                f"The submitted hostname could not connect, so inspection continued at {fallback_host}. This was an inspector fallback, not an observed redirect.",
            )
        )
    if any(row.get("category") == "error" for row in rows) and rendered_ok:
        review_items.append(("info", "The plain HTML request failed, but the real-browser observation succeeded."))
    if external_forms:
        domains = sorted({registrable_hint(str(row.get("host") or "")) for row in external_forms})
        review_items.append(("review", f"Form submissions are configured to go to an outside service: {', '.join(filter(None, domains))}."))
    analytics_posts = [
        row
        for row in network_rows
        if row.get("third_party") == "yes" and str(_details(row).get("method") or "").upper() == "POST"
    ]
    if analytics_posts:
        domains = sorted({registrable_hint(str(row.get("host") or "")) for row in analytics_posts})
        review_items.append(("review", f"Outbound POST traffic was observed to: {', '.join(filter(None, domains))}. Request bodies were not collected."))
    insecure_cookies = [row for row in rows if row.get("category") == "browser_cookie" and _details(row).get("secure") is False]
    if insecure_cookies:
        review_items.append(("review", f"{len(insecure_cookies)} observed cookie(s) did not carry the Secure attribute in the browser cookie record."))
    http_assets = [row for row in network_rows if str(row.get("finding_url") or "").startswith("http://")]
    if http_assets:
        review_items.append(("review", f"{len(http_assets)} HTTP asset reference(s) were observed on an HTTPS page; the browser may have upgraded or blocked them."))
    if not review_items:
        review_items.append(("info", "No immediate review flags were generated from this short observation."))
    review_html = "".join(f'<li class="review-{kind}">{h(message)}</li>' for kind, message in review_items)

    identity_bits = [
        ("Site name", metadata.get("og:site_name", "")),
        ("Last modified", metadata.get("article:modified_time", "")),
        ("Publisher", metadata.get("article:publisher", "")),
        ("Social handle", metadata.get("twitter:site", "")),
        ("Web server", str(selected_headers.get("server") or "")),
        (
            "Protection headers",
            ", ".join(
                label
                for key, label in (
                    ("content-security-policy", "CSP"),
                    ("strict-transport-security", "HSTS"),
                    ("permissions-policy", "Permissions Policy"),
                    ("referrer-policy", "Referrer Policy"),
                    ("x-frame-options", "Frame protection"),
                    ("x-content-type-options", "MIME protection"),
                )
                if selected_headers.get(key)
            ),
        ),
    ]
    identity_html = "".join(
        f'<div><span>{h(label)}</span><b>{h(value)}</b></div>' for label, value in identity_bits if value
    ) or '<span class="empty-note">No additional identity metadata observed.</span>'

    detail_body: list[str] = []
    for row in rows:
        url = str(row.get("finding_url") or "")
        url_html = f'<a href="{h(url)}" target="_blank" rel="noreferrer">{h(row.get("host") or url)}</a>' if url else h(row.get("host") or "")
        detail_body.append(
            f'<tr data-category="{h(row.get("category") or "")}"><td>{h(row.get("category") or "")}</td>'
            f'<td>{h(row.get("finding_name") or "")}</td><td>{url_html}</td><td>{h(row.get("basis") or "")}</td>'
            f'<td>{h(row.get("details") or "")}</td><td>{h(row.get("evidence") or "")}</td></tr>'
        )
    count_badges = " ".join(f'<span class="pill">{h(name)}: {count}</span>' for name, count in sorted(categories.items()))

    return f"""
    <section class="investigator-dashboard">
      <div class="inspector-heading">
        <div><span class="eyebrow">Website intelligence summary</span><h2>{h(page_title)}</h2><a href="{h(page_url)}" target="_blank" rel="noreferrer">{h(page_url)}</a></div>
        <span class="inspection-status {status_class}">{h(status_text)}</span>
      </div>
      <div class="inspector-kpi-grid">{kpi_html}</div>
      <div class="inspector-two-column">
        <div class="inspector-card"><h3>Technology stack</h3><div class="tech-list">{technology_html}</div></div>
        <div class="inspector-card"><h3>Site identity signals</h3><div class="identity-list">{identity_html}</div></div>
      </div>
      <div class="inspector-card"><h3>Third-party services contacted</h3><p class="section-help">Services that received a browser request or are configured as a form destination. This is different from an ordinary clickable link.</p>{services_html}</div>
      <div class="inspector-two-column">
        <div class="inspector-card"><h3>Linked websites</h3><p class="section-help">Top-level domains a visitor could navigate to from visible page links.</p><div class="domain-list">{linked_html}</div></div>
        <div class="inspector-card"><h3>Investigator review items</h3><ul class="review-list">{review_html}</ul></div>
      </div>
      <details class="evidence-details">
        <summary>Detailed evidence ({len(rows)} rows)</summary>
        <div class="evidence-counts">{count_badges}</div>
        <div class="results"><table class="evidence-table">
          <thead><tr><th>Category</th><th>Finding</th><th>Destination</th><th>Basis</th><th>Details</th><th>Evidence / caveat</th></tr></thead>
          <tbody>{''.join(detail_body)}</tbody>
        </table></div>
      </details>
    </section>
    """


def _dashboard_styles() -> str:
    return """
    <style>
      .investigator-dashboard { display:grid; gap:16px; margin:18px 0 28px; }
      .inspector-heading { display:flex; justify-content:space-between; gap:20px; align-items:flex-start; padding:20px; border-radius:14px; color:#fff; background:linear-gradient(135deg,#123b52,#1c78a6); }
      .inspector-heading h2 { margin:3px 0 4px; color:#fff; }
      .inspector-heading a { color:#dff5ff; word-break:break-all; }
      .eyebrow { font-size:11px; letter-spacing:.12em; text-transform:uppercase; color:#bde8f8; }
      .inspection-status { white-space:nowrap; padding:7px 11px; border-radius:999px; font-size:12px; font-weight:700; }
      .status-good { background:#d9f8e8; color:#11613a; } .status-neutral { background:#eef2f6; color:#425066; }
      .inspector-kpi-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(155px,1fr)); gap:12px; }
      .inspector-kpi { min-height:112px; padding:15px; border:1px solid var(--border); border-top:4px solid var(--primary); border-radius:11px; background:#fff; box-shadow:0 3px 12px rgba(27,50,66,.06); }
      .inspector-kpi-label { color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.05em; }
      .inspector-kpi-value { margin:7px 0 4px; font-size:25px; font-weight:750; line-height:1.1; overflow-wrap:anywhere; }
      .inspector-kpi-note,.section-help { color:var(--muted); font-size:12px; }
      .inspector-two-column { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:16px; }
      .inspector-card { padding:18px; border:1px solid var(--border); border-radius:12px; background:var(--panel); }
      .inspector-card h3 { margin:0 0 6px; font-size:16px; }
      .tech-list,.domain-list { display:flex; flex-wrap:wrap; gap:8px; margin-top:12px; }
      .tech-chip { display:flex; flex-direction:column; gap:2px; padding:8px 11px; border:1px solid #bdd8e7; border-radius:9px; background:#fff; }
      .tech-chip small,.domain-chip small { color:var(--muted); }
      .domain-chip { padding:7px 10px; border-radius:999px; background:#e7f3f9; text-decoration:none; }
      .identity-list { display:grid; gap:8px; margin-top:12px; }
      .identity-list div { display:grid; grid-template-columns:110px 1fr; gap:10px; padding-bottom:7px; border-bottom:1px solid var(--border); }
      .identity-list span { color:var(--muted); }
      .summary-table { width:100%; margin-top:10px; background:#fff; border-radius:8px; overflow:hidden; }
      .summary-table th { background:#eaf2f7; text-align:left; }
      .summary-table td,.summary-table th { padding:9px; border-bottom:1px solid var(--border); }
      .review-list { margin:12px 0 0; padding-left:20px; display:grid; gap:8px; }
      .review-review::marker { color:#bc6a00; } .review-info::marker { color:var(--primary); }
      .empty-note { color:var(--muted); font-style:italic; }
      .evidence-details { border:1px solid var(--border); border-radius:12px; background:#fff; }
      .evidence-details summary { cursor:pointer; padding:14px 16px; font-weight:700; }
      .evidence-counts { padding:0 16px 12px; }
      .evidence-table td { max-width:420px; white-space:normal; overflow-wrap:anywhere; vertical-align:top; }
      @media (max-width:800px) { .inspector-two-column { grid-template-columns:1fr; } .inspector-kpi-grid { grid-template-columns:repeat(2,minmax(0,1fr)); } .inspector-heading { flex-direction:column; } }
      @media (max-width:480px) { .inspector-kpi-grid { grid-template-columns:1fr; } }
    </style>
    """
