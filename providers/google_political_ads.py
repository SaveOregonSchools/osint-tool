from __future__ import annotations

import os
from typing import Any, Iterator


TABLE_NAME = os.getenv("GOOGLE_POLITICAL_ADS_TABLE", "bigquery-public-data.google_political_ads.creative_stats")


def build_sql(filters: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    limit = max(1, min(int(filters.get("limit") or 100), 10000))
    params = {
        "advertiser_name": (filters.get("advertiser_name_contains") or "").strip() or None,
        "advertiser_id": (filters.get("advertiser_id") or "").strip() or None,
        "date_min": (filters.get("date_min") or "").strip() or None,
        "date_max": (filters.get("date_max") or "").strip() or None,
        "region": (filters.get("region") or "").strip() or None,
        "keyword_regex": _keyword_regex(filters.get("keyword_terms") or ""),
        "limit": limit,
    }
    sql = f"""
    SELECT
      ad_id,
      advertiser_id,
      advertiser_name,
      date_range_start,
      date_range_end,
      impressions,
      spend_range_min_usd,
      spend_range_max_usd,
      regions,
      gender_targeting,
      age_targeting,
      geo_targeting_included,
      ad_url,
      creative_page_url,
      TO_JSON_STRING(t) AS raw_json
    FROM `{TABLE_NAME}` AS t
    WHERE TRUE
      AND (@advertiser_name IS NULL OR LOWER(CAST(advertiser_name AS STRING)) LIKE CONCAT('%', LOWER(@advertiser_name), '%'))
      AND (@advertiser_id IS NULL OR CAST(advertiser_id AS STRING) = @advertiser_id)
      AND (@date_min IS NULL OR CAST(date_range_end AS DATE) >= CAST(@date_min AS DATE))
      AND (@date_max IS NULL OR CAST(date_range_start AS DATE) <= CAST(@date_max AS DATE))
      AND (@region IS NULL OR REGEXP_CONTAINS(LOWER(TO_JSON_STRING(regions)), LOWER(@region))
           OR REGEXP_CONTAINS(LOWER(TO_JSON_STRING(geo_targeting_included)), LOWER(@region)))
      AND (@keyword_regex IS NULL OR REGEXP_CONTAINS(LOWER(TO_JSON_STRING(t)), @keyword_regex))
    ORDER BY date_range_start DESC
    LIMIT @limit
    """
    return sql, params


def _keyword_regex(raw: str) -> str | None:
    terms = []
    for line in str(raw or "").replace(",", "\n").replace(";", "\n").splitlines():
        term = line.strip().casefold()
        if term:
            terms.append(term)
    if not terms:
        return None
    import re

    return "(" + "|".join(re.escape(term) for term in terms[:25]) + ")"


def creative_url(advertiser_id: str, ad_id: str, fallback: str = "") -> str:
    if fallback:
        return fallback
    if advertiser_id and ad_id:
        return f"https://adstransparency.google.com/advertiser/{advertiser_id}/creative/{ad_id}"
    return ""


class GooglePoliticalAdsClient:
    def __init__(self, project: str | None = None):
        self.project = (project or os.getenv("GOOGLE_CLOUD_PROJECT") or "").strip()
        if not self.project:
            raise RuntimeError("Set GOOGLE_CLOUD_PROJECT to query Google public BigQuery datasets.")

    def iter_ads(self, filters: dict[str, Any]) -> Iterator[dict[str, Any]]:
        try:
            from google.cloud import bigquery  # type: ignore
        except Exception as exc:  # pragma: no cover - optional runtime dependency
            raise RuntimeError("Install google-cloud-bigquery to use the Google Political Ads BigQuery connector.") from exc

        sql, params = build_sql(filters)
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("advertiser_name", "STRING", params["advertiser_name"]),
                bigquery.ScalarQueryParameter("advertiser_id", "STRING", params["advertiser_id"]),
                bigquery.ScalarQueryParameter("date_min", "STRING", params["date_min"]),
                bigquery.ScalarQueryParameter("date_max", "STRING", params["date_max"]),
                bigquery.ScalarQueryParameter("region", "STRING", params["region"]),
                bigquery.ScalarQueryParameter("keyword_regex", "STRING", params["keyword_regex"]),
                bigquery.ScalarQueryParameter("limit", "INT64", params["limit"]),
            ]
        )
        client = bigquery.Client(project=self.project)
        for row in client.query(sql, job_config=job_config).result():
            item = dict(row.items())
            item["creative_page_url"] = creative_url(str(item.get("advertiser_id") or ""), str(item.get("ad_id") or ""), str(item.get("creative_page_url") or item.get("ad_url") or ""))
            yield item
