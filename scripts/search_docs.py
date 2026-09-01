#!/usr/bin/env python3
"""Search Automation Anywhere's public documentation and return compact evidence."""

import argparse
import concurrent.futures
import copy
import datetime as dt
import html
import json
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Sequence, Tuple

BASE_URL = "https://docs.automationanywhere.com"
DOCS_HOST = "docs.automationanywhere.com"
SEARCH_URL = BASE_URL + "/api/khub/clustered-search"
CALLING_APP = "automation-anywhere-docs-skill"
USER_AGENT = (
    "automation-anywhere-docs-skill/1.0 (+https://docs.automationanywhere.com/)"
)
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
BLOCK_TAGS = {
    "address",
    "article",
    "aside",
    "blockquote",
    "div",
    "dl",
    "dt",
    "dd",
    "figcaption",
    "figure",
    "footer",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "hr",
    "li",
    "main",
    "nav",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "tr",
    "ul",
}
STOPWORDS = {
    "about",
    "after",
    "automation",
    "anywhere",
    "does",
    "from",
    "have",
    "into",
    "that",
    "the",
    "their",
    "this",
    "what",
    "when",
    "where",
    "which",
    "with",
}


class DocsError(RuntimeError):
    """A bounded, user-actionable retrieval error."""


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self.skip_depth = 0

    def handle_starttag(
        self, tag: str, attrs: Sequence[Tuple[str, Optional[str]]]
    ) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"}:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag == "br":
            self.parts.append("\n")
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in BLOCK_TAGS:
            self.parts.append("\n\n")
        elif tag in {"td", "th"}:
            self.parts.append("\t")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"}:
            if self.skip_depth:
                self.skip_depth -= 1
            return
        if not self.skip_depth and tag in BLOCK_TAGS:
            self.parts.append("\n\n")

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.parts.append(data)


def html_to_text(value: str) -> str:
    parser = _HTMLTextExtractor()
    parser.feed(value or "")
    parser.close()
    text = html.unescape("".join(parser.parts)).replace("\xa0", " ")
    cleaned_lines: List[str] = []
    for line in text.splitlines():
        line = re.sub(r"[ \t]+", " ", line).strip()
        if line:
            cleaned_lines.append(line)
        elif cleaned_lines and cleaned_lines[-1] != "":
            cleaned_lines.append("")
    return "\n".join(cleaned_lines).strip()


def trim_text(value: str, limit: int) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    clipped = value[: max(1, limit - 1)]
    boundary = max(clipped.rfind("\n"), clipped.rfind(". "), clipped.rfind(" "))
    if boundary >= int(limit * 0.65):
        clipped = clipped[:boundary]
    return clipped.rstrip(" ,;:-") + "…"


def compact_field(value: Any, limit: int = 300) -> str:
    return trim_text(re.sub(r"\s+", " ", str(value or "")).strip(), limit)


def _query_terms(query: str) -> List[str]:
    terms = re.findall(r"[a-z0-9][a-z0-9_.:/-]+", query.lower())
    return list(
        dict.fromkeys(
            term for term in terms if len(term) >= 2 and term not in STOPWORDS
        )
    )


def relevant_excerpt(text: str, query: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    blocks = [block.strip() for block in re.split(r"\n\s*\n", text) if block.strip()]
    terms = _query_terms(query)
    if not blocks or not terms:
        return trim_text(text, limit)

    scored: List[Tuple[int, int]] = []
    query_lower = query.lower().strip()
    for index, block in enumerate(blocks):
        lowered = block.lower()
        score = sum(lowered.count(term) for term in terms)
        if query_lower and query_lower in lowered:
            score += len(terms) + 2
        if score:
            scored.append((score, index))
    if not scored:
        return trim_text(text, limit)

    ranked_indices = [
        index for _, index in sorted(scored, key=lambda item: (-item[0], item[1]))
    ]
    selected: List[int] = []
    for index in ranked_indices:
        if index not in selected:
            selected.append(index)
        if len("\n\n".join(blocks[i] for i in selected)) >= limit:
            break
    for index in ranked_indices:
        for nearby in (index - 1, index + 1):
            if not 0 <= nearby < len(blocks) or nearby in selected:
                continue
            candidate = "\n\n".join(blocks[i] for i in selected + [nearby])
            if len(candidate) <= limit:
                selected.append(nearby)
    excerpt = "\n\n".join(blocks[i] for i in selected)
    return trim_text(excerpt, limit)


def validate_official_url(url: str) -> str:
    if len(url) > 2000:
        raise DocsError("Refusing an excessively long documentation URL")
    parsed = urllib.parse.urlparse(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise DocsError(
            "Refusing a malformed documentation URL: {}".format(url)
        ) from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != DOCS_HOST
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise DocsError("Refusing a non-official documentation URL: {}".format(url))
    return url


def canonical_official_url(url: str) -> str:
    validate_official_url(url)
    parsed = urllib.parse.urlparse(url)
    return urllib.parse.urlunparse(parsed._replace(query="", fragment=""))


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.URLError("Documentation redirects are disabled")


def _request_bytes(
    url: str,
    *,
    timeout: float,
    data: Optional[bytes] = None,
    content_type: Optional[str] = None,
) -> bytes:
    validate_official_url(url)
    headers = {
        "Accept": "application/json, text/html;q=0.9, */*;q=0.1",
        "Ft-Calling-App": CALLING_APP,
        "User-Agent": USER_AGENT,
    }
    if content_type:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(
        url, data=data, headers=headers, method="POST" if data else "GET"
    )
    try:
        opener = urllib.request.build_opener(_NoRedirectHandler())
        with opener.open(request, timeout=timeout) as response:
            validate_official_url(response.geturl())
            payload = response.read(MAX_RESPONSE_BYTES + 1)
            if len(payload) > MAX_RESPONSE_BYTES:
                raise DocsError(
                    "Official documentation response exceeded the 5 MiB safety limit"
                )
            return payload
    except urllib.error.HTTPError as exc:
        detail = exc.read(600).decode("utf-8", errors="replace").strip()
        raise DocsError(
            "Documentation API returned HTTP {}: {}".format(exc.code, detail)
        ) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        try:
            return _request_bytes_with_curl(
                url,
                timeout=timeout,
                data=data,
                content_type=content_type,
            )
        except DocsError as curl_exc:
            raise DocsError(
                "Could not reach Automation Anywhere documentation with urllib ({}) "
                "or curl ({})".format(exc, curl_exc)
            ) from exc


def _request_bytes_with_curl(
    url: str,
    *,
    timeout: float,
    data: Optional[bytes],
    content_type: Optional[str],
) -> bytes:
    validate_official_url(url)
    curl = shutil.which("curl")
    if not curl:
        raise DocsError("curl is not installed")
    try:
        version_check = subprocess.run(
            [curl, "--disable", "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DocsError("could not check curl version: {}".format(exc)) from exc
    version_match = re.search(rb"\bcurl (\d+)\.(\d+)", version_check.stdout[:200])
    if (
        version_check.returncode
        or not version_match
        or (int(version_match.group(1)), int(version_match.group(2))) < (8, 4)
    ):
        raise DocsError("curl 8.4 or newer is required for the bounded TLS fallback")
    command = [
        curl,
        "--disable",
        "--no-location",
        "--silent",
        "--show-error",
        "--fail",
        "--proto",
        "=https",
        "--max-time",
        str(timeout),
        "--max-filesize",
        str(MAX_RESPONSE_BYTES),
        "--header",
        "Accept: application/json, text/html;q=0.9, */*;q=0.1",
        "--header",
        "Ft-Calling-App: {}".format(CALLING_APP),
        "--header",
        "User-Agent: {}".format(USER_AGENT),
    ]
    if content_type:
        command.extend(["--header", "Content-Type: {}".format(content_type)])
    if data is not None:
        command.extend(["--request", "POST", "--data-binary", "@-"])
    command.append(url)
    try:
        completed = subprocess.run(
            command,
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout + 5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DocsError("curl execution failed: {}".format(exc)) from exc
    if completed.returncode:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise DocsError("curl exited {}: {}".format(completed.returncode, detail))
    if len(completed.stdout) > MAX_RESPONSE_BYTES:
        raise DocsError(
            "Official documentation response exceeded the 5 MiB safety limit"
        )
    return completed.stdout


def _request_json(url: str, *, timeout: float, body: Dict[str, Any]) -> Dict[str, Any]:
    payload = _request_bytes(
        url,
        timeout=timeout,
        data=json.dumps(body).encode("utf-8"),
        content_type="application/json",
    )
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DocsError("Documentation API returned invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise DocsError("Documentation API returned an unexpected response shape")
    return parsed


def _metadata(items: Any) -> Dict[str, List[str]]:
    result: Dict[str, List[str]] = {}
    if not isinstance(items, list):
        return result
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("key"), str):
            continue
        values = item.get("values")
        if isinstance(values, list):
            result[item["key"]] = [str(value) for value in values]
    return result


def _first(metadata: Dict[str, List[str]], *keys: str) -> str:
    for key in keys:
        values = metadata.get(key)
        if values:
            return values[0]
    return ""


def _payload_for_entry(entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    type_to_key = {
        "TOPIC": "topic",
        "MAP": "map",
        "DOCUMENT": "document",
        "HTML_PACKAGE": "htmlPackage",
        "HTML_PACKAGE_PAGE": "htmlPackagePage",
    }
    key = type_to_key.get(str(entry.get("type", "")).upper())
    if key and isinstance(entry.get(key), dict):
        return entry[key]
    for candidate_key, candidate in entry.items():
        if candidate_key not in {"missingTerms", "type"} and isinstance(
            candidate, dict
        ):
            return candidate
    return None


def _normalise_result(entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    payload = _payload_for_entry(entry)
    if not payload:
        return None
    metadata = _metadata(payload.get("metadata"))
    url = next(
        (
            value
            for value in (
                payload.get("readerUrl"),
                payload.get("viewerUrl"),
                payload.get("documentUrl"),
                payload.get("url"),
            )
            if isinstance(value, str) and value
        ),
        "",
    )
    content_url = (
        payload.get("contentUrl") if isinstance(payload.get("contentUrl"), str) else ""
    )
    if content_url:
        content_url = urllib.parse.urljoin(BASE_URL, content_url)
    try:
        if url:
            validate_official_url(url)
        if content_url:
            validate_official_url(content_url)
    except DocsError:
        return None
    if not url:
        return None
    url = canonical_official_url(url)

    raw_breadcrumb = payload.get("breadcrumb")
    if not isinstance(raw_breadcrumb, list):
        raw_breadcrumb = []
    breadcrumb = [
        compact_field(value) for value in raw_breadcrumb[:12] if str(value).strip()
    ]
    api_version = next(
        (part for part in breadcrumb if re.fullmatch(r"v\d+", part, re.I)), ""
    )
    if not api_version:
        version_hint = (
            _first(metadata, "ft:htmlPackagePath")
            + " "
            + str(payload.get("title") or "")
        )
        match = re.search(r"(?:api[-_ ]*)?v(\d+)\b", version_hint, re.I)
        if match:
            api_version = "v{}".format(match.group(1))
    parsed_url = urllib.parse.urlparse(url)
    unique_key = _first(metadata, "ft:prettyUrl") or parsed_url.path.rstrip("/") or url
    updated = str(payload.get("lastEditionDate") or "") or _first(
        metadata, "revised_modified", "ft:lastEdition", "ft:lastTechChange"
    )
    published = _first(metadata, "ft:lastPublication")
    publication = compact_field(
        payload.get("mapTitle")
        or payload.get("packageTitle")
        or _first(metadata, "ft:publication_title")
    )
    word_count_value = _first(metadata, "ft:wordCount")
    try:
        word_count = int(word_count_value or 0)
    except ValueError:
        word_count = 0
    metadata_version = _first(metadata, "vrm_version", "version")
    if metadata_version == "v-2019":
        metadata_version = ""
    result = {
        "kind": str(entry.get("type", "UNKNOWN")),
        "title": compact_field(
            payload.get("title") or _first(metadata, "ft:title") or "Untitled"
        ),
        "url": url,
        "content_url": content_url,
        "publication": publication,
        "product": compact_field(
            _first(metadata, "Product", "category", "prodname") or publication
        ),
        "version": compact_field(api_version or metadata_version, 80),
        "updated": compact_field(updated, 80),
        "published": published,
        "breadcrumb": breadcrumb,
        "snippet": html_to_text(str(payload.get("htmlExcerpt") or "")),
        "word_count": word_count,
        "unique_key": unique_key,
    }
    if result["content_url"] and 0 < word_count <= 5 and not result["snippet"]:
        return None
    return result


def parse_search_results(response: Dict[str, Any]) -> List[Dict[str, Any]]:
    ordered: List[Dict[str, Any]] = []
    positions: Dict[str, int] = {}
    clusters = response.get("results")
    if not isinstance(clusters, list):
        return ordered
    for cluster in clusters:
        if not isinstance(cluster, dict) or not isinstance(
            cluster.get("entries"), list
        ):
            continue
        for entry in cluster["entries"]:
            if not isinstance(entry, dict):
                continue
            result = _normalise_result(entry)
            if not result:
                continue
            key = result["unique_key"]
            if key not in positions:
                positions[key] = len(ordered)
                ordered.append(result)
                continue
            existing_index = positions[key]
            existing = ordered[existing_index]
            if (result.get("published", ""), result.get("updated", "")) > (
                existing.get("published", ""),
                existing.get("updated", ""),
            ):
                ordered[existing_index] = result
    return ordered


def search(
    query: str, *, locale: str, pool_size: int, timeout: float
) -> Tuple[List[Dict[str, Any]], int]:
    body = {
        "query": query,
        "contentLocale": locale,
        "uiLocale": locale,
        "scope": "ALL_TOPICS",
        "virtualField": "EVERYWHERE",
        "metadataFilters": [],
        "facets": [],
        "sort": [],
        "clusterSortCriterions": [],
        "paging": {"page": 1, "perPage": pool_size},
    }
    response = _request_json(
        SEARCH_URL + "?page=1&per_page={}".format(pool_size), timeout=timeout, body=body
    )
    paging = response.get("paging") if isinstance(response.get("paging"), dict) else {}
    try:
        total = int(paging.get("totalResultsCount", 0) or 0)
    except (TypeError, ValueError):
        total = 0
    total = min(max(total, 0), 999_999_999)
    return parse_search_results(response), total


def _fetch_content(
    result: Dict[str, Any], query: str, limit: int, timeout: float
) -> Tuple[str, str]:
    content_url = result.get("content_url", "")
    if not content_url:
        return (
            trim_text(result.get("snippet", ""), limit),
            "No public topic content endpoint; using search excerpt",
        )
    try:
        payload = _request_bytes(content_url, timeout=timeout)
        page_text = html_to_text(payload.decode("utf-8", errors="replace"))
        if not page_text:
            return trim_text(
                result.get("snippet", ""), limit
            ), "Topic content was empty; using search excerpt"
        return relevant_excerpt(page_text, query, limit), ""
    except DocsError as exc:
        return trim_text(result.get("snippet", ""), limit), str(exc)


def enrich_results(
    results: List[Dict[str, Any]],
    *,
    query: str,
    per_result_limit: int,
    timeout: float,
    search_only: bool,
) -> List[str]:
    warnings: List[str] = []
    if search_only:
        for result in results:
            result["text"] = trim_text(result.get("snippet", ""), per_result_limit)
            result["_warning"] = ""
        return warnings

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(4, max(1, len(results)))
    ) as executor:
        futures = [
            executor.submit(_fetch_content, result, query, per_result_limit, timeout)
            for result in results
        ]
        for result, future in zip(results, futures):
            text, warning = future.result()
            result["text"] = text
            result["_warning"] = warning
            if warning:
                warnings.append("{}: {}".format(result["title"], warning))
    return warnings


def public_result(result: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: value
        for key, value in result.items()
        if key
        not in {
            "content_url",
            "published",
            "snippet",
            "unique_key",
            "word_count",
            "_warning",
        }
        and value not in ("", [], None)
    }


def render_markdown(payload: Dict[str, Any], max_total_chars: int) -> str:
    lines = [
        "# Automation Anywhere documentation search",
        "",
        "- Query: {}".format(json.dumps(payload["query"], ensure_ascii=False)),
        "- Retrieved: {}".format(payload["retrieved_at"]),
        "- Matches reported by the official index: {}".format(payload["total_matches"]),
    ]
    if payload["warnings"]:
        lines.extend(["", "## Warnings"])
        lines.extend("- {}".format(warning) for warning in payload["warnings"])
    for index, result in enumerate(payload["results"], 1):
        lines.extend(["", "## {}. {}".format(index, result["title"])])
        lines.append("- Official URL: {}".format(result["url"]))
        if result.get("publication"):
            lines.append("- Publication: {}".format(result["publication"]))
        if result.get("version"):
            lines.append("- Version/API generation: {}".format(result["version"]))
        if result.get("updated"):
            lines.append("- Page updated: {}".format(result["updated"]))
        if result.get("breadcrumb"):
            lines.append("- Path: {}".format(" > ".join(result["breadcrumb"])))
        lines.extend(
            [
                "",
                "Relevant text:",
                "",
                result.get("text") or "(No extractable text returned.)",
            ]
        )
    output = "\n".join(lines).strip() + "\n"
    if len(output) > max_total_chars:
        output = (
            trim_text(output, max_total_chars - 80)
            + "\n\n[Output shortened by --max-total-chars.]\n"
        )
    return output


def render_json(payload: Dict[str, Any], max_total_chars: int) -> str:
    bounded = copy.deepcopy(payload)
    bounded["query"] = compact_field(bounded.get("query"), 1000)
    bounded["retrieved_at"] = compact_field(bounded.get("retrieved_at"), 80)
    bounded["source"] = compact_field(bounded.get("source", SEARCH_URL), 500)
    try:
        total_matches = int(bounded.get("total_matches", 0) or 0)
    except (TypeError, ValueError):
        total_matches = 0
    bounded["total_matches"] = min(max(total_matches, 0), 999_999_999)
    warnings = bounded.get("warnings")
    bounded["warnings"] = (
        [compact_field(warning, 700) for warning in warnings[:10]]
        if isinstance(warnings, list)
        else []
    )
    results = bounded.get("results")
    bounded["results"] = (
        [result for result in results[:10] if isinstance(result, dict)]
        if isinstance(results, list)
        else []
    )
    reduction_noted = False
    while True:
        output = json.dumps(bounded, ensure_ascii=False, indent=2) + "\n"
        if len(output) <= max_total_chars:
            return output

        text_results = [
            result
            for result in bounded.get("results", [])
            if len(str(result.get("text", ""))) > 120
        ]
        if text_results:
            excess = len(output) - max_total_chars
            reduction = max(32, (excess + len(text_results) - 1) // len(text_results))
            for result in text_results:
                current = str(result.get("text", ""))
                result["text"] = trim_text(current, max(120, len(current) - reduction))
            continue

        results = bounded.get("results", [])
        if len(results) > 1:
            results.pop()
            if not reduction_noted:
                bounded.setdefault("warnings", []).append(
                    "Results reduced to honor --max-total-chars."
                )
                reduction_noted = True
            continue

        if results:
            optional_keys = [
                "breadcrumb",
                "product",
                "publication",
                "updated",
                "kind",
                "version",
            ]
            removed = False
            for key in optional_keys:
                if key in results[0]:
                    del results[0][key]
                    removed = True
                    break
            if removed:
                continue

        minimal_result: Dict[str, Any] = {}
        if results:
            minimal_result = {
                "title": compact_field(results[0].get("title"), 160),
                "url": results[0].get("url", ""),
            }
        minimal = {
            "query": compact_field(bounded.get("query"), 200),
            "retrieved_at": bounded.get("retrieved_at", ""),
            "source": bounded.get("source", SEARCH_URL),
            "total_matches": bounded.get("total_matches", 0),
            "warnings": ["Output reduced to honor --max-total-chars."],
            "results": [minimal_result] if minimal_result else [],
        }
        output = json.dumps(minimal, ensure_ascii=False, separators=(",", ":")) + "\n"
        if len(output) <= max_total_chars:
            return output
        minimal["results"] = []
        final_output = (
            json.dumps(minimal, ensure_ascii=False, separators=(",", ":")) + "\n"
        )
        if len(final_output) <= max_total_chars:
            return final_output
        return '{"error":"Output could not fit within --max-total-chars."}\n'


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Search official Automation Anywhere documentation and emit compact, "
            "citable evidence."
        )
    )
    parser.add_argument("query", nargs="*", help="Focused documentation query")
    parser.add_argument(
        "--stdin",
        action="store_true",
        dest="read_stdin",
        help="Read the query from standard input instead of shell arguments",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=3,
        help="Number of unique results to return (default: 3)",
    )
    parser.add_argument(
        "--max-chars", type=int, default=2200, help="Maximum relevant text per result"
    )
    parser.add_argument(
        "--max-total-chars", type=int, default=8000, help="Maximum rendered output size"
    )
    parser.add_argument(
        "--locale", default="en-US", help="Documentation locale (default: en-US)"
    )
    parser.add_argument(
        "--timeout", type=float, default=20.0, help="Per-request timeout in seconds"
    )
    parser.add_argument(
        "--search-only",
        action="store_true",
        help="Return search excerpts without fetching topics",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit structured JSON instead of Markdown"
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 10:
        parser.error("--limit must be between 1 and 10")
    if not 300 <= args.max_chars <= 10000:
        parser.error("--max-chars must be between 300 and 10000")
    if not 2000 <= args.max_total_chars <= 50000:
        parser.error("--max-total-chars must be between 2000 and 50000")
    if not 1 <= args.timeout <= 120:
        parser.error("--timeout must be between 1 and 120 seconds")

    if args.read_stdin and args.query:
        parser.error("provide a positional query or --stdin, not both")
    query = (
        sys.stdin.readline(1002).strip()
        if args.read_stdin
        else " ".join(args.query).strip()
    )
    if not query:
        parser.error("query must not be empty")
    if len(query) > 1000:
        parser.error("query must not exceed 1000 characters")
    if len(args.locale) > 32 or not re.fullmatch(
        r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?", args.locale
    ):
        parser.error("--locale must look like en-US, zh-CN, or ja-JP")
    pool_size = min(80, max(20, args.limit * 8))
    try:
        results, total = search(
            query, locale=args.locale, pool_size=pool_size, timeout=args.timeout
        )
    except DocsError as exc:
        print("error: {}".format(exc), file=sys.stderr)
        return 3
    if not results:
        print(
            "No official Automation Anywhere documentation results found "
            "for: {}".format(query),
            file=sys.stderr,
        )
        return 4

    per_result_limit = min(
        args.max_chars,
        max(300, (args.max_total_chars - 1400) // max(1, args.limit)),
    )
    candidates = results[: min(len(results), args.limit + 4)]
    enrich_results(
        candidates,
        query=query,
        per_result_limit=per_result_limit,
        timeout=args.timeout,
        search_only=args.search_only,
    )
    results = [result for result in candidates if result.get("text", "").strip()][
        : args.limit
    ]
    if not results:
        print(
            "Official search returned results but no extractable evidence "
            "for: {}".format(query),
            file=sys.stderr,
        )
        return 5
    warnings = [
        "{}: {}".format(result["title"], result["_warning"])
        for result in results
        if result.get("_warning")
    ]
    payload = {
        "query": query,
        "retrieved_at": dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat(),
        "source": SEARCH_URL,
        "total_matches": total,
        "warnings": warnings,
        "results": [public_result(result) for result in results],
    }
    if args.json:
        sys.stdout.write(render_json(payload, args.max_total_chars))
    else:
        sys.stdout.write(render_markdown(payload, args.max_total_chars))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
