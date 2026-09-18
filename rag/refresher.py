"""
rag/refresher.py — Keep the RAG corpus current via GitHub SHA tracking.

How it works
------------
Most doc sources map to a specific path inside a PUBLIC GitHub repo.
On each run the script calls the GitHub commits API to get the latest
commit SHA that touched that path.  It compares against the SHA stored
in rag/.doc_versions.json from the previous ingest.  If the SHA changed
(or --force is given) it re-downloads the affected files and re-ingests
only that platform's ChromaDB collection.

Web sources (type: "web_sitemap")
---------------------------------
Some vendors do not publish their docs to a git repo — the official
Databricks platform docs are one.  Those are enumerated from the site's
published sitemap.xml and fetched over plain HTTP, honouring the
Disallow rules declared on each source (see robots_disallow).

Because there is no commit SHA, staleness is judged two ways:
  * the sha256 of the sorted URL set — catches pages added or removed;
  * age since last ingest (RAG_WEB_MAX_AGE_DAYS, default 7) — catches
    pages edited in place, which the URL set cannot see because these
    sitemaps carry no <lastmod>.

When each source refreshes
--------------------------
GitHub sources are checked on EVERY `python cli.py` launch (background
thread) and by the weekly task.  A SHA check is one small API call, so
this is nearly free and picks up upstream doc merges quickly.

Web sources refresh ONLY on the weekly scheduled task (Monday 03:00),
never at launch — see refresh_platform(include_web=...).  Checking one
costs a ~850 KB sitemap download, and acting on it re-scrapes thousands
of pages; neither belongs in a background thread while someone is using
the REPL.  Force one by hand any time with:

    python -m rag.refresher --platform databricks --force

A scrape stages into <out_dir>.staging and only replaces the live
directory once at least min_success_ratio of pages succeeded, so an
interrupted run leaves the existing corpus untouched.

Politeness knobs (env): RAG_WEB_WORKERS (default 4), RAG_WEB_DELAY_S
(default 0.2), RAG_WEB_USER_AGENT.  The crawler identifies itself
honestly and sends no credentials — see _fetch_web().

No credentials required
-----------------------
All repos used (delta-io/delta, snowflakedb/*, Snowflake-Labs/sfquickstarts,
boto/botocore, awsdocs/*, datahub-project/datahub) are PUBLIC.  GitHub's unauthenticated REST API
allows 60 requests/hour — more than enough for a weekly scheduled run.
Set GITHUB_TOKEN in .env to raise the limit to 5 000/hour.

Graceful offline behaviour
--------------------------
Every network call is wrapped in a try/except with a short timeout.
If the machine is offline the script logs a warning and exits cleanly
without touching the existing corpus.

CLI
---
    python -m rag.refresher                # check all, re-ingest if changed
    python -m rag.refresher --platform aws
    python -m rag.refresher --force        # re-ingest regardless of SHA
    python -m rag.refresher --check-only   # print status table, no changes
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import time
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_BASE = Path(__file__).parent.parent   # repo root
_VERSIONS_FILE = _BASE / "rag" / ".doc_versions.json"
_DOCS_ROOT = _BASE / "rag" / "docs"
_REFRESH_MARKER = _BASE / "rag" / ".auto_refresh_state.json"
_GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
_REQUEST_TIMEOUT = 15  # seconds per HTTP call

# --- Web scraping (type: "web_sitemap") ------------------------------------
# Used for docs that are not published to a public git repo. Identify the
# crawler honestly and stay polite: docs.databricks.com/robots.txt sets no
# Crawl-delay, so these are our own conservative limits, not a site rule.
_WEB_USER_AGENT = os.getenv(
    "RAG_WEB_USER_AGENT",
    "DSA-Agent-docs-fetch/1.0 (local RAG corpus builder; +https://github.com/tkim/DSA_Agent_II)",
)
_WEB_WORKERS = int(os.getenv("RAG_WEB_WORKERS", "4"))       # concurrent requests
_WEB_DELAY_S = float(os.getenv("RAG_WEB_DELAY_S", "0.2"))   # per-worker pause between pages
# Sitemaps here carry no <lastmod>, so page edits are invisible. The URL set is
# fingerprinted to catch added/removed pages; this max age forces a periodic
# re-scrape so in-place edits are eventually picked up.
_WEB_MAX_AGE_DAYS = int(os.getenv("RAG_WEB_MAX_AGE_DAYS", "7"))

# Progress messages route through _log() so the launch-time auto_refresh() can
# divert them to rag/refresh.log instead of spamming the interactive REPL.
# The `python -m rag.refresher` CLI leaves this as None -> normal stdout.
_LOG_SINK: Any = None


def _log(msg: str) -> None:
    if _LOG_SINK is None:
        print(msg)
    else:
        print(msg, file=_LOG_SINK)
        _LOG_SINK.flush()

# ---------------------------------------------------------------------------
# Doc source registry
#
# Each entry describes one "unit" whose staleness is tracked by a single
# GitHub commit SHA.  Types:
#   github_dir   — list all files in a repo directory, download matching exts
#   github_file  — download a single file
#   botocore_svc — fetch botocore/data/<svc>/service-2.json, convert to text
# ---------------------------------------------------------------------------

DOC_SOURCES: dict[str, list[dict]] = {
    "databricks": [
        {
            # Delta Lake OSS docs (delta.io project). NOTE: this is the Delta Lake
            # table format, NOT the Databricks platform. It covers ACID, time
            # travel, and table features only — nothing about Unity Catalog,
            # Lakeflow, Jobs, or SQL Warehouses. Kept for Delta internals depth;
            # `databricks-platform-docs` below is what covers the platform.
            "id":         "delta-lake-docs",
            "repo":       "delta-io/delta",
            "track_path": "docs/src/content/docs",
            "type":       "github_dir",
            "raw_base":   "https://raw.githubusercontent.com/delta-io/delta/master/docs/src/content/docs",
            "extensions": [".mdx", ".md"],
            "max_files":  40,
            # Subdirectory of rag/docs/databricks/ — ingestor.py rglob's the
            # platform dir, so both sources land in cloud_agents_databricks.
            "out_dir":    "databricks/delta_oss",
        },
        {
            # Official Databricks platform documentation.
            #
            # Databricks does not publish docs.databricks.com to a public git
            # repo, so there is no commit SHA to track. It does publish a
            # sitemap and a permissive robots.txt (User-agent: *, Allow: /),
            # so pages are enumerated from the sitemap rather than crawled.
            # Only the Disallow rules below are off-limits.
            "id":           "databricks-platform-docs",
            "type":         "web_sitemap",
            "sitemap_url":  "https://docs.databricks.com/aws/en/sitemap.xml",
            "url_base":     "https://docs.databricks.com/aws/en/",
            # From https://docs.databricks.com/robots.txt — internal search
            # pages and archived docs are Disallow'd for all user agents.
            "robots_disallow": ["/archive/", "/search-for", "?s=", "&s="],
            # PySpark API reference is ~1100 pages of method signatures. It
            # dominates the corpus by volume while answering few platform
            # questions, so it is excluded by default. Delete this line to
            # include it.
            "exclude_prefixes": ["/aws/en/pyspark/"],
            "min_chars":    200,     # skip nav-only stubs and error pages
            "out_dir":      "databricks/platform",
        },
    ],
    "snowflake": [
        {
            # Official Snowflake platform documentation. Like Databricks, Snowflake
            # does not publish docs.snowflake.com to a public git repo, so pages are
            # enumerated from its sitemap. robots.txt (docs.snowflake.com/robots.txt)
            # is permissive apart from the four Disallow patterns below.
            #
            # /release-notes/ (~1,690 pages) is excluded: it is historical changelog
            # noise that dominates by volume while answering almost no conceptual or
            # admin questions — the same rationale Databricks uses to drop the PySpark
            # API reference. Everything else (sql-reference, user-guide, developer-
            # guide, migrations, connectors, ~6,460 pages) is kept.
            "id":              "snowflake-platform-docs",
            "type":            "web_sitemap",
            "sitemap_url":     "https://docs.snowflake.com/en/sitemap.xml",
            "url_base":        "https://docs.snowflake.com/en/",
            "robots_disallow": ["/sql-reference/commands-", "/INCLUDE/", "/DRAFT/", "/PREVIEW/"],
            "exclude_prefixes": ["/release-notes/"],
            "min_chars":       200,     # skip nav-only stubs and error pages
            "out_dir":         "snowflake/platform",
        },
        {
            "id":         "snowflake-connector-python",
            "repo":       "snowflakedb/snowflake-connector-python",
            "track_path": "README.md",
            "type":       "github_file",
            "raw_url":    "https://raw.githubusercontent.com/snowflakedb/snowflake-connector-python/main/README.md",
            "out_dir":    "snowflake",
            "out_name":   "connector_python_readme.md",
        },
        {
            "id":         "snowpark-python",
            "repo":       "snowflakedb/snowpark-python",
            "track_path": "README.md",
            "type":       "github_file",
            "raw_url":    "https://raw.githubusercontent.com/snowflakedb/snowpark-python/main/README.md",
            "out_dir":    "snowflake",
            "out_name":   "snowpark_python_readme.md",
        },
        {
            "id":         "sfguide-python-api",
            "repo":       "Snowflake-Labs/sfquickstarts",
            "track_path": "site/sfguides/src/getting-started-snowflake-python-api",
            "type":       "github_file",
            "raw_url":    "https://raw.githubusercontent.com/Snowflake-Labs/sfquickstarts/master/site/sfguides/src/getting-started-snowflake-python-api/getting-started-snowflake-python-api.md",
            "out_dir":    "snowflake",
            "out_name":   "getting_started_python_api.md",
        },
        {
            "id":         "sfguide-snowpark-de",
            "repo":       "Snowflake-Labs/sfquickstarts",
            "track_path": "site/sfguides/src/data-engineering-with-snowpark-python-intro",
            "type":       "github_file",
            "raw_url":    "https://raw.githubusercontent.com/Snowflake-Labs/sfquickstarts/master/site/sfguides/src/data-engineering-with-snowpark-python-intro/data-engineering-with-snowpark-python-intro.md",
            "out_dir":    "snowflake",
            "out_name":   "data_engineering_snowpark.md",
        },
        {
            "id":         "sfguide-iceberg",
            "repo":       "Snowflake-Labs/sfquickstarts",
            "track_path": "site/sfguides/src/getting-started-iceberg-tables",
            "type":       "github_file",
            "raw_url":    "https://raw.githubusercontent.com/Snowflake-Labs/sfquickstarts/master/site/sfguides/src/getting-started-iceberg-tables/getting-started-iceberg-tables.md",
            "out_dir":    "snowflake",
            "out_name":   "iceberg_tables.md",
        },
    ],
    "aws": [
        {
            "id":         "botocore-s3",
            "repo":       "boto/botocore",
            "track_path": "botocore/data/s3",
            "type":       "botocore_svc",
            "service":    "s3",
            "raw_url":    "https://raw.githubusercontent.com/boto/botocore/develop/botocore/data/s3/2006-03-01/service-2.json.gz",
            "raw_url_plain": "https://raw.githubusercontent.com/boto/botocore/develop/botocore/data/s3/2006-03-01/service-2.json",
            "out_dir":    "aws",
            "out_name":   "botocore_s3.txt",
        },
        {
            "id":         "botocore-glue",
            "repo":       "boto/botocore",
            "track_path": "botocore/data/glue",
            "type":       "botocore_svc",
            "service":    "glue",
            "raw_url_plain": "https://raw.githubusercontent.com/boto/botocore/develop/botocore/data/glue/2017-03-31/service-2.json",
            "out_dir":    "aws",
            "out_name":   "botocore_glue.txt",
        },
        {
            "id":         "botocore-bedrock-runtime",
            "repo":       "boto/botocore",
            "track_path": "botocore/data/bedrock-runtime",
            "type":       "botocore_svc",
            "service":    "bedrock-runtime",
            "raw_url_plain": "https://raw.githubusercontent.com/boto/botocore/develop/botocore/data/bedrock-runtime/2023-09-30/service-2.json",
            "out_dir":    "aws",
            "out_name":   "botocore_bedrock_runtime.txt",
        },
        {
            "id":         "botocore-lambda",
            "repo":       "boto/botocore",
            "track_path": "botocore/data/lambda",
            "type":       "botocore_svc",
            "service":    "lambda",
            "raw_url_plain": "https://raw.githubusercontent.com/boto/botocore/develop/botocore/data/lambda/2015-03-31/service-2.json",
            "out_dir":    "aws",
            "out_name":   "botocore_lambda.txt",
        },
        {
            "id":         "botocore-iam",
            "repo":       "boto/botocore",
            "track_path": "botocore/data/iam",
            "type":       "botocore_svc",
            "service":    "iam",
            "raw_url_plain": "https://raw.githubusercontent.com/boto/botocore/develop/botocore/data/iam/2010-05-08/service-2.json",
            "out_dir":    "aws",
            "out_name":   "botocore_iam.txt",
        },
        {
            "id":         "botocore-ec2",
            "repo":       "boto/botocore",
            "track_path": "botocore/data/ec2",
            "type":       "botocore_svc",
            "service":    "ec2",
            "raw_url_plain": "https://raw.githubusercontent.com/boto/botocore/develop/botocore/data/ec2/2016-11-15/service-2.json",
            "out_dir":    "aws",
            "out_name":   "botocore_ec2.txt",
        },
        # NOTE: the awsdocs/amazon-s3-userguide and awsdocs/aws-glue-developer-guide
        # sources were removed — AWS archived those repos (default branch "archived")
        # and deleted their doc_source directories, so they now 404. AWS coverage
        # comes from the botocore service definitions above plus the boto3 SDK
        # references generated by rag/fetch_docs.ps1.
    ],
    "datahub": [
        {
            # Official DataHub documentation (docs.datahub.com, Docusaurus).
            #
            # The Markdown lives in datahub-project/datahub, but the site is the
            # rendered product — many pages (ingestion source reference, entity
            # metamodel) are GENERATED at build time and never exist as .md in the
            # repo. So, like Databricks and Snowflake, pages are enumerated from
            # the sitemap. The sitemap covers the current release only (archived
            # versions live on archive.docs.datahub.com and are not listed).
            #
            # docs.datahub.com serves no robots.txt (404), so there are no Disallow
            # rules to honour; /search is excluded by include_prefixes anyway.
            # include_prefixes keeps /docs/ only (~700 pages), dropping the
            # marketing/community pages (/learn blog, /champions, /events ...).
            # exclude_prefixes then drops the community pages that sit under
            # /docs/ and answer no technical question — the same rationale as
            # Snowflake's release-notes exclusion.
            "id":               "datahub-platform-docs",
            "type":             "web_sitemap",
            "sitemap_url":      "https://docs.datahub.com/sitemap.xml",
            "url_base":         "https://docs.datahub.com/docs/",
            "robots_disallow":  [],
            "include_prefixes": ["https://docs.datahub.com/docs/"],
            "exclude_prefixes": ["/docs/townhall", "/docs/posts", "/docs/releases",
                                 "/docs/roadmap", "/docs/slack", "/docs/links"],
            "min_chars":        200,     # skip nav-only stubs and error pages
            "out_dir":          "datahub/platform",
        },
        {
            # GMS GraphQL schema (SDL). The docstrings on every type and field are
            # the authoritative API reference, and they are what the agent needs to
            # write correct queries. Ingested as .graphql (see ingestor.DOC_EXTS).
            "id":         "datahub-graphql-schema",
            "repo":       "datahub-project/datahub",
            "track_path": "datahub-graphql-core/src/main/resources",
            "type":       "github_dir",
            "raw_base":   "https://raw.githubusercontent.com/datahub-project/datahub/master/datahub-graphql-core/src/main/resources",
            "extensions": [".graphql"],
            "max_files":  60,
            "out_dir":    "datahub/graphql",
        },
        {
            "id":         "datahub-metadata-ingestion-readme",
            "repo":       "datahub-project/datahub",
            "track_path": "metadata-ingestion/README.md",
            "type":       "github_file",
            "raw_url":    "https://raw.githubusercontent.com/datahub-project/datahub/master/metadata-ingestion/README.md",
            "out_dir":    "datahub",
            "out_name":   "metadata_ingestion_readme.md",
        },
        {
            "id":         "datahub-cli-ingestion",
            "repo":       "datahub-project/datahub",
            "track_path": "metadata-ingestion/cli-ingestion.md",
            "type":       "github_file",
            "raw_url":    "https://raw.githubusercontent.com/datahub-project/datahub/master/metadata-ingestion/cli-ingestion.md",
            "out_dir":    "datahub",
            "out_name":   "cli_ingestion.md",
        },
    ],
}

PLATFORMS = list(DOC_SOURCES.keys())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _HTMLStripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def _strip_html(s: str) -> str:
    p = _HTMLStripper()
    p.feed(s)
    return p.text()


def _gh_headers() -> dict[str, str]:
    h = {"Accept": "application/vnd.github+json",
         "X-GitHub-Api-Version": "2022-11-28"}
    if _GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {_GITHUB_TOKEN}"
    return h


def _fetch(url: str, timeout: int = _REQUEST_TIMEOUT) -> bytes | None:
    """
    Fetch URL bytes from GitHub; return None on any network error.

    Sends GitHub API headers including the bearer token, so this must only ever
    be pointed at github.com / raw.githubusercontent.com. Non-GitHub hosts go
    through _fetch_web(), which sends no credentials.
    """
    try:
        req = urllib.request.Request(url, headers=_gh_headers())
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception as exc:
        _log(f"  [warn] fetch failed: {url} — {exc}")
        return None


def _fetch_web(url: str, timeout: int = _REQUEST_TIMEOUT) -> bytes | None:
    """
    Fetch a public web page. Deliberately separate from _fetch(): it sends a
    plain identifying User-Agent and, critically, NO Authorization header, so a
    configured GITHUB_TOKEN is never transmitted to a third-party host.
    """
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": _WEB_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml",
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception as exc:
        _log(f"  [warn] web fetch failed: {url} — {exc}")
        return None


def _fetch_json(url: str) -> Any | None:
    raw = _fetch(url)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except Exception as exc:
        _log(f"  [warn] JSON parse failed: {url} — {exc}")
        return None


# ---------------------------------------------------------------------------
# Version store
# ---------------------------------------------------------------------------

def _load_versions() -> dict[str, dict]:
    if _VERSIONS_FILE.exists():
        try:
            return json.loads(_VERSIONS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_versions(versions: dict[str, dict]) -> None:
    _VERSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _VERSIONS_FILE.write_text(
        json.dumps(versions, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# SHA tracking
# ---------------------------------------------------------------------------

def _get_latest_sha(repo: str, path: str) -> str | None:
    """Return the commit SHA of the most recent commit touching `path`."""
    url = (f"https://api.github.com/repos/{repo}/commits"
           f"?path={path}&per_page=1")
    data = _fetch_json(url)
    if not data or not isinstance(data, list) or not data:
        return None
    return data[0].get("sha")


# ---------------------------------------------------------------------------
# Web sitemap scraping
# ---------------------------------------------------------------------------

class _ArticleExtractor(HTMLParser):
    """
    Pull the readable body out of a docs page.

    Only text inside <article> is kept, which drops the nav, sidebar, breadcrumb
    and footer chrome that would otherwise be embedded on every single page and
    pollute retrieval with boilerplate.
    """

    _SKIP_TAGS = {"script", "style", "nav", "svg", "button", "noscript"}
    _BLOCK_TAGS = {"p", "li", "h1", "h2", "h3", "h4", "h5", "pre", "tr", "div"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._article_depth = 0
        self._skip_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag == "article":
            self._article_depth += 1
            return
        if self._article_depth and tag in self._SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._article_depth and not self._skip_depth and tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "article" and self._article_depth:
            self._article_depth -= 1
        elif self._article_depth and tag in self._SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._article_depth and not self._skip_depth:
            self._parts.append(data)

    def text(self) -> str:
        raw = "".join(self._parts)
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r"\n\s*\n+", "\n\n", raw)
        return raw.strip()


def _extract_article(html: str) -> str:
    p = _ArticleExtractor()
    try:
        p.feed(html)
    except Exception:  # noqa: BLE001 - malformed markup shouldn't kill the run
        return ""
    return p.text()


def _robots_blocked(url: str, src: dict) -> bool:
    """
    Honour the Disallow rules we rely on from the target's robots.txt.

    These are declared per-source rather than fetched live so that the rules
    being obeyed are visible in review. For docs.databricks.com the relevant
    Disallow entries are the internal search pages (`*s=*`, `/search-for`) and
    `/archive/`.
    """
    for pat in src.get("robots_disallow", ()):
        if pat in url:
            return True
    return False


def _sitemap_urls(src: dict) -> list[str] | None:
    """Fetch the sitemap and return the filtered, sorted page list."""
    raw = _fetch_web(src["sitemap_url"])
    if raw is None:
        return None
    xml = raw.decode("utf-8", errors="ignore")
    urls = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml)

    include = tuple(src.get("include_prefixes", ()))
    exclude = tuple(src.get("exclude_prefixes", ()))

    kept: list[str] = []
    for u in urls:
        if _robots_blocked(u, src):
            continue
        if exclude and any(e in u for e in exclude):
            continue
        if include and not any(i in u for i in include):
            continue
        kept.append(u)

    kept = sorted(set(kept))
    max_pages = int(src.get("max_pages", 0) or 0)
    if max_pages and len(kept) > max_pages:
        kept = kept[:max_pages]
    return kept


def _url_to_filename(url: str, base: str) -> str:
    """Stable, filesystem-safe name derived from the URL path."""
    path = url.split(base, 1)[-1] if base in url else url
    path = path.strip("/") or "index"
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", path)
    return f"{name[:120]}.txt"


def _web_fingerprint(urls: list[str]) -> str:
    return hashlib.sha256("\n".join(urls).encode("utf-8")).hexdigest()


def _fetch_one_page(url: str, src: dict, out_dir: Path) -> Path | None:
    raw = _fetch_web(url)
    if raw is None:
        return None
    text = _extract_article(raw.decode("utf-8", errors="ignore"))
    if len(text) < int(src.get("min_chars", 200)):
        return None  # nav-only stub or an error page
    out = out_dir / _url_to_filename(url, src.get("url_base", ""))
    # Keep provenance in the file so retrieval hits can be traced to a page.
    out.write_text(f"Source: {url}\n\n{text}\n", encoding="utf-8")
    if _WEB_DELAY_S:
        time.sleep(_WEB_DELAY_S)
    return out


def _fetch_web_sitemap(src: dict, out_root: Path) -> list[Path]:
    """Scrape every page listed in the source's sitemap into out_dir."""
    from concurrent.futures import ThreadPoolExecutor

    urls = src.get("_resolved_urls") or _sitemap_urls(src)
    if not urls:
        return []

    out_dir = out_root / src["out_dir"]
    # Scrape into a staging directory and swap only once the run has succeeded.
    # Writing straight into out_dir would mean a network drop at page 2000 of
    # 4362 leaves a truncated corpus that the following re-ingest bakes in.
    # This module's contract is that a failed refresh leaves the existing corpus
    # untouched, so the live directory is not touched until the very end.
    staging = out_dir.with_name(out_dir.name + ".staging")
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)

    _log(f"  Scraping {len(urls)} pages from {src['sitemap_url']} "
         f"({_WEB_WORKERS} workers, {_WEB_DELAY_S}s delay)...")

    written: list[Path] = []
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, _WEB_WORKERS)) as pool:
        for res in pool.map(lambda u: _fetch_one_page(u, src, staging), urls):
            done += 1
            if res is not None:
                written.append(res)
            if done % 250 == 0:
                _log(f"    {done}/{len(urls)} pages ({len(written)} kept)")

    # Refuse to publish an obviously incomplete scrape over a good corpus.
    # Some pages legitimately fail min_chars, so this is a floor, not equality.
    min_ratio = float(src.get("min_success_ratio", 0.80))
    ok_ratio = (len(written) / len(urls)) if urls else 0.0
    if ok_ratio < min_ratio:
        _log(f"  [warn] only {len(written)}/{len(urls)} pages ({ok_ratio:.0%}) "
             f"succeeded, below {min_ratio:.0%} — keeping the existing corpus "
             f"and discarding this run.")
        shutil.rmtree(staging, ignore_errors=True)
        return []

    # Swap: staging becomes live. Done as late as possible to keep the window
    # where out_dir is absent as short as we can manage on Windows, which has
    # no atomic directory replace.
    if out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)
    staging.replace(out_dir)

    live = sorted(out_dir.glob("*.txt"))
    _log(f"  Scraped {len(live)}/{len(urls)} pages into {out_dir}")
    return live


# ---------------------------------------------------------------------------
# Fetch strategies
# ---------------------------------------------------------------------------

def _fetch_github_dir(src: dict, out_root: Path) -> list[Path]:
    """List a GitHub directory and download matching files."""
    out_dir = out_root / src["out_dir"]
    out_dir.mkdir(parents=True, exist_ok=True)

    api_url = (f"https://api.github.com/repos/{src['repo']}"
               f"/contents/{src['track_path']}")
    listing = _fetch_json(api_url)
    if not listing or not isinstance(listing, list):
        _log(f"  [warn] could not list {src['repo']}/{src['track_path']}")
        return []

    exts = {e.lower() for e in src.get("extensions", [".md"])}
    files = [f for f in listing
             if f.get("type") == "file"
             and Path(f["name"]).suffix.lower() in exts]

    max_files = src.get("max_files", 50)
    files = files[:max_files]

    written: list[Path] = []
    for f in files:
        raw_url = f"{src['raw_base']}/{f['name']}"
        content = _fetch(raw_url)
        if content is None:
            continue
        dest = out_dir / f["name"]
        dest.write_bytes(content)
        written.append(dest)
        time.sleep(0.1)   # be polite to GitHub

    return written


def _fetch_github_file(src: dict, out_root: Path) -> list[Path]:
    """Download a single file."""
    out_dir = out_root / src["out_dir"]
    out_dir.mkdir(parents=True, exist_ok=True)
    content = _fetch(src["raw_url"])
    if content is None:
        return []
    dest = out_dir / src["out_name"]
    dest.write_bytes(content)
    return [dest]


def _botocore_svc_to_text(service_name: str, raw_json: bytes) -> str:
    """
    Convert a botocore service-2.json into a clean, readable text document
    suitable for embedding.  Extracts every operation name + documentation.
    """
    try:
        data = json.loads(raw_json)
    except Exception:
        return ""

    lines: list[str] = [
        f"# AWS {service_name.upper()} — API Reference",
        f"# Source: boto/botocore (service-2.json)",
        "",
    ]

    meta = data.get("metadata", {})
    if meta:
        lines += [
            f"Service:    {meta.get('serviceFullName', service_name)}",
            f"Protocol:   {meta.get('protocol', '?')}",
            f"API version:{meta.get('apiVersion', '?')}",
            "",
        ]

    ops = data.get("operations", {})
    for op_name, op in sorted(ops.items()):
        doc_raw = op.get("documentation", "")
        doc = _strip_html(doc_raw).strip()
        http = op.get("http", {})
        method = http.get("method", "")
        uri = http.get("requestUri", "")

        # Required input members
        input_shape = op.get("input", {}).get("shape", "")
        shapes = data.get("shapes", {})
        required: list[str] = []
        if input_shape and input_shape in shapes:
            required = shapes[input_shape].get("required", [])

        lines.append(f"## {op_name}")
        if method:
            lines.append(f"HTTP: {method} {uri}")
        if required:
            lines.append(f"Required params: {', '.join(required)}")
        if doc:
            # Keep first 400 chars to avoid bloat
            lines.append(doc[:400] + ("..." if len(doc) > 400 else ""))
        lines.append("")

    return "\n".join(lines)


def _fetch_botocore_svc(src: dict, out_root: Path) -> list[Path]:
    """Fetch botocore service-2.json and convert to readable text."""
    out_dir = out_root / src["out_dir"]
    out_dir.mkdir(parents=True, exist_ok=True)

    raw_url = src.get("raw_url_plain") or src.get("raw_url")
    raw = _fetch(raw_url)
    if raw is None:
        return []

    text = _botocore_svc_to_text(src["service"], raw)
    if not text:
        return []

    dest = out_dir / src["out_name"]
    dest.write_text(text, encoding="utf-8")
    return [dest]


# ---------------------------------------------------------------------------
# Core refresh logic
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _web_source_is_stale(stored: dict) -> bool:
    """
    True when a web source is old enough to warrant a re-scrape.

    The URL-set fingerprint catches pages being added or removed, but a page
    edited in place keeps the same URL and the sitemap carries no <lastmod>.
    Age is therefore the only signal for in-place edits.
    """
    last = stored.get("last_ingested")
    if not last:
        return True
    try:
        when = datetime.fromisoformat(last)
    except ValueError:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    age_days = (datetime.now(timezone.utc) - when).total_seconds() / 86400.0
    return age_days >= _WEB_MAX_AGE_DAYS


def refresh_source(src: dict, versions: dict, force: bool = False,
                   check_only: bool = False) -> dict:
    """
    Check and optionally refresh one doc source.
    Returns a status dict summarising what happened.
    """
    sid = src["id"]
    stored = versions.get(sid, {})
    stored_sha = stored.get("sha", "")

    # 1 — determine the latest version marker for this source
    if src["type"] == "web_sitemap":
        # No commit SHA and no <lastmod> in the sitemap, so staleness is judged
        # by (a) the set of URLs changing and (b) age since the last ingest.
        urls = _sitemap_urls(src)
        if urls is None:
            return {"id": sid, "status": "network_error",
                    "stored_sha": stored_sha, "latest_sha": None}
        latest_sha = _web_fingerprint(urls)
        src["_resolved_urls"] = urls   # reuse; avoids re-fetching the sitemap
        changed = (latest_sha != stored_sha) or _web_source_is_stale(stored)
    else:
        latest_sha = _get_latest_sha(src["repo"], src["track_path"])
        if latest_sha is None:
            return {"id": sid, "status": "network_error",
                    "stored_sha": stored_sha, "latest_sha": None}
        changed = (latest_sha != stored_sha)

    status = "changed" if changed else "up_to_date"

    if check_only:
        return {"id": sid, "status": status,
                "stored_sha": stored_sha[:10] if stored_sha else "none",
                "latest_sha": latest_sha[:10],
                "last_ingested": stored.get("last_ingested", "never")}

    if not changed and not force:
        return {"id": sid, "status": "up_to_date",
                "stored_sha": stored_sha[:10], "latest_sha": latest_sha[:10]}

    # 2 — fetch new content
    _log(f"  Fetching {sid} ({'changed' if changed else 'forced'})...")
    out_root = _DOCS_ROOT
    fetch_fn = {
        "github_dir":  _fetch_github_dir,
        "github_file": _fetch_github_file,
        "botocore_svc": _fetch_botocore_svc,
        "web_sitemap": _fetch_web_sitemap,
    }.get(src["type"])

    if fetch_fn is None:
        return {"id": sid, "status": "unknown_type"}

    written = fetch_fn(src, out_root)
    if not written:
        return {"id": sid, "status": "fetch_failed"}

    # 3 — update version store (ingest happens per-platform after all sources run)
    versions[sid] = {
        "sha":           latest_sha,
        "last_checked":  _now_iso(),
        "last_ingested": _now_iso(),
        "files":         [str(p.relative_to(_BASE)) for p in written],
    }

    return {"id": sid, "status": "fetched",
            "files_written": len(written),
            "latest_sha": latest_sha[:10]}


def refresh_platform(platform: str, versions: dict, force: bool = False,
                     check_only: bool = False,
                     include_web: bool = True) -> list[dict]:
    """
    include_web=False skips every web_sitemap source outright — no sitemap
    request at all. Launch-time auto_refresh() uses this: a GitHub SHA check is
    one small API call, but a web source means downloading a ~850 KB sitemap on
    every launch and can trigger a multi-thousand-page re-scrape while someone
    is mid-conversation. Web sources refresh on the weekly scheduled task
    instead, which runs `python rag/refresher.py` with include_web left True.
    """
    sources = DOC_SOURCES.get(platform, [])
    if not sources:
        print(f"Unknown platform: {platform}")
        return []

    results: list[dict] = []
    needs_ingest = False

    for src in sources:
        if not include_web and src["type"] == "web_sitemap":
            results.append({"id": src["id"], "status": "skipped_web"})
            continue
        r = refresh_source(src, versions, force=force, check_only=check_only)
        results.append(r)
        if r["status"] in ("fetched",):
            needs_ingest = True

    if not check_only and needs_ingest:
        _log(f"  Re-ingesting {platform} into ChromaDB...")
        try:
            from rag.ingestor import ingest_platform_docs
            count = ingest_platform_docs(platform, force=True)
            _log(f"  Ingested {count} chunks into cloud_agents_{platform}")
        except Exception as exc:
            _log(f"  [error] ingest failed: {exc}")

    return results


# ---------------------------------------------------------------------------
# Launch-time entry point (throttled, quiet, best-effort)
# ---------------------------------------------------------------------------

def _read_last_check() -> datetime | None:
    try:
        data = json.loads(_REFRESH_MARKER.read_text(encoding="utf-8"))
        return datetime.fromisoformat(data["last_check"])
    except Exception:
        return None


def _write_last_check(when: datetime) -> None:
    try:
        _REFRESH_MARKER.write_text(
            json.dumps({"last_check": when.isoformat(timespec="seconds")}),
            encoding="utf-8",
        )
    except Exception:
        pass


def auto_refresh(interval_hours: float | None = None) -> dict:
    """
    Launch-time refresh: check every source's upstream commit SHA and re-ingest
    only the platforms whose docs actually changed.

    Throttled — if a check already ran within `interval_hours` this returns
    immediately with no network calls. `interval_hours` defaults to env
    RAG_REFRESH_INTERVAL_HOURS (0 = check on every launch).

    Best-effort and quiet: all progress/warnings are diverted to
    rag/refresh.log; offline or API errors leave the existing corpus untouched.
    Returns a small summary dict; callers should treat it as advisory.
    """
    global _LOG_SINK

    if interval_hours is None:
        try:
            interval_hours = float(os.getenv("RAG_REFRESH_INTERVAL_HOURS", "0"))
        except ValueError:
            interval_hours = 0.0

    now = datetime.now(timezone.utc)
    if interval_hours > 0:
        last = _read_last_check()
        if last is not None and (now - last).total_seconds() < interval_hours * 3600:
            return {"status": "throttled", "changed": [],
                    "last_check": last.isoformat(timespec="seconds")}

    summary = {"status": "ran", "changed": [], "checked": 0, "errors": 0}
    logf = None
    try:
        _REFRESH_MARKER.parent.mkdir(parents=True, exist_ok=True)
        logf = open(_BASE / "rag" / "refresh.log", "a", encoding="utf-8")
        logf.write(f"\n=== Auto-refresh {now.isoformat(timespec='seconds')} ===\n")
        _LOG_SINK = logf

        versions = _load_versions()
        for platform in PLATFORMS:
            # include_web=False: never scrape a docs site on launch. See
            # refresh_platform's docstring; the weekly task handles those.
            results = refresh_platform(platform, versions, force=False,
                                       check_only=False, include_web=False)
            summary["checked"] += len(results)
            if any(r.get("status") == "fetched" for r in results):
                summary["changed"].append(platform)
            if any(r.get("status") in ("network_error", "fetch_failed") for r in results):
                summary["errors"] += 1
        _save_versions(versions)
        _write_last_check(now)
    except Exception as exc:  # never let a refresh break the caller
        summary["status"] = "error"
        summary["error"] = str(exc)
    finally:
        _LOG_SINK = None
        if logf is not None:
            logf.close()

    return summary


# ---------------------------------------------------------------------------
# Pretty status table
# ---------------------------------------------------------------------------

def _print_table(all_results: dict[str, list[dict]]) -> None:
    try:
        from rich.table import Table
        from rich.console import Console
        console = Console()
        t = Table(title="RAG Corpus Status")
        t.add_column("Platform", style="cyan")
        t.add_column("Source ID")
        t.add_column("Status")
        t.add_column("Stored SHA")
        t.add_column("Latest SHA")
        t.add_column("Last Ingested")

        colours = {
            "up_to_date":    "green",
            "changed":       "yellow",
            "fetched":       "blue",
            "fetch_failed":  "red",
            "network_error": "red",
        }
        for platform, results in all_results.items():
            for r in results:
                colour = colours.get(r["status"], "white")
                t.add_row(
                    platform,
                    r["id"],
                    f"[{colour}]{r['status']}[/{colour}]",
                    r.get("stored_sha", "none"),
                    r.get("latest_sha", "?"),
                    r.get("last_ingested", "—"),
                )
        console.print(t)
    except ImportError:
        # Fallback plain text
        header = f"{'Platform':<12} {'Source ID':<35} {'Status':<15} {'Latest SHA'}"
        print(header)
        print("-" * len(header))
        for platform, results in all_results.items():
            for r in results:
                print(f"{platform:<12} {r['id']:<35} {r['status']:<15} "
                      f"{r.get('latest_sha', '?')}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Keep DSA Agent RAG corpus current via GitHub SHA tracking."
    )
    parser.add_argument(
        "--platform",
        choices=PLATFORMS,
        help="Refresh a single platform (default: all)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-fetch and re-ingest even if SHA is unchanged",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Show status table without downloading or ingesting anything",
    )
    args = parser.parse_args()

    versions = _load_versions()
    targets = [args.platform] if args.platform else PLATFORMS
    all_results: dict[str, list[dict]] = {}

    for platform in targets:
        print(f"\n[{platform}]")
        results = refresh_platform(
            platform, versions,
            force=args.force,
            check_only=args.check_only,
        )
        all_results[platform] = results

    if not args.check_only:
        _save_versions(versions)
        print("\nVersion store updated.")

    print()
    _print_table(all_results)


if __name__ == "__main__":
    main()
