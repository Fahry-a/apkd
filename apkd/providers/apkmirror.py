"""APKMirror provider — native Python port of the apkmirror-downloader scrapers.

Upstream: https://github.com/tanishqmanuja/apkmirror-downloader
(by [@tanishqmanuja](https://github.com/tanishqmanuja))
Upstream commit: `b6bba610f180169c0c5f3eceaa6eac0660511eca` (v2.0.11)

Porting map (TypeScript -> Python, kept 1:1 where possible):
  src/lib/utils.ts                 -> with_base_url / make_repo_url /
                                      make_variants_url / version predicates
  src/lib/scrapers/versions.ts     -> extract_versions
  src/lib/scrapers/variants.ts     -> extract_variants (+ Redirected sentinel)
  src/lib/scrapers/downloads.ts    -> extract_redirect_download_url /
                                      extract_final_download_url
  src/lib/helpers.ts               -> normalize_variants / filter_variant
  src/lib/index.ts (APKMirrorDownloader.download) -> resolve_request flow

To port an upstream update: diff the files above against the upstream commit,
then apply the corresponding hunk to the same-named function here and update
the "Upstream commit" line.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from .base import Provider, normalize_arch
from ._cloudflare import solve_cloudflare_challenge
from ..models import Artifact, DownloadRequest, ProviderError
from ..validation import APK_EXTENSIONS

from ._apkpure.transport import supported_impersonation

try:
    from curl_cffi import requests as _impersonated_requests

    _HAS_IMPERSONATION = True
except ImportError:  # pragma: no cover - curl_cffi is a hard dependency
    _impersonated_requests = None  # type: ignore[assignment]
    _HAS_IMPERSONATION = False

# A 403 was measured across chrome145/146/150, so the fingerprint is not a
# tuning knob to age: try the newest the installed curl_cffi offers first.
_IMPERSONATE = supported_impersonation(("chrome150", "chrome146", "chrome145", "chrome131"))[0]

BASE_URL = "https://www.apkmirror.com"
CHALLENGE_MARKER = "Enable JavaScript and cookies to continue"
SPECIAL_VERSION_TOKENS = ("latest", "stable", "beta", "alpha")

# Android SDK -> Android version, for comparing request.min_sdk against the
# variant's minimum-Android-version cell (e.g. "9.0+"). Unknown SDKs skip the
# filter instead of failing closed.
SDK_TO_VERSION = {
    21: 5.0, 22: 5.1, 23: 6.0, 24: 7.0, 25: 7.1, 26: 8.0, 27: 8.1,
    28: 9.0, 29: 10.0, 30: 11.0, 31: 12.0, 32: 12.1, 33: 13.0,
    34: 14.0, 35: 15.0, 36: 16.0,
}


class Redirected:
    """A variants URL that redirects straight to a single download page."""

    def __init__(self, url: str) -> None:
        self.url = url


def with_base_url(endpoint: str) -> str:
    endpoint = endpoint.strip()
    if endpoint.startswith(("http://", "https://")):
        return endpoint
    if not endpoint.startswith("/"):
        endpoint = "/" + endpoint
    return BASE_URL + endpoint


def make_repo_url(org: str, repo: str) -> str:
    return with_base_url(f"/apk/{org}/{repo}")


def make_variants_url(org: str, repo: str, version: str) -> str:
    slug = version.replace(".", "-")
    return with_base_url(f"/apk/{org}/{repo}/{repo}-{slug}-release/")


def normalize_version_key(value: object) -> str:
    """Compare versions ignoring case and separator style (dots/dashes/spaces)."""
    return re.sub(r"[\s\-_.]+", "", str(value or "").lower())


def prefix_from_version_entry(version_url: str, version_name: str) -> str | None:
    """Derive the app-name URL prefix (e.g. ``x-``) from a versions-table entry.

    APKMirror version URLs are shaped ``<prefix><version-dashes>-release/``
    where ``<prefix>`` is the display-name slug (``x-``, ``google-photos-``,
    ``advanced-download-manager-``), which is frequently *not* the repo slug,
    so blindly building ``<repo>-<version>-release`` 404s. The version part
    starts at the first dash-separated token containing a digit; everything
    before it is the prefix. Returns None when the entry does not align.
    """
    basename = unquote(urlparse(version_url).path.rstrip("/").rsplit("/", 1)[-1])
    core = basename[:-len("-release")] if basename.endswith("-release") else basename
    slug_tokens = [t for t in re.split(r"[\s\-]+", version_name.strip().replace(".", "-")) if t]
    start = next((i for i, t in enumerate(slug_tokens) if any(ch.isdigit() for ch in t)), None)
    if start is None:
        return None
    version_tokens = [t.lower() for t in slug_tokens[start:]]
    core_tokens = core.split("-")
    if len(core_tokens) >= len(version_tokens) and \
            [t.lower() for t in core_tokens[-len(version_tokens):]] == version_tokens:
        head = core_tokens[: len(core_tokens) - len(version_tokens)]
        return ("-".join(head) + "-") if head else ""
    return None


def _is_alpha(name: str) -> bool:
    return "alpha" in name.lower()


def _is_beta(name: str) -> bool:
    return "beta" in name.lower()


def _is_stable(name: str) -> bool:
    lowered = name.lower()
    return "alpha" not in lowered and "beta" not in lowered


def _is_universal(arch: str) -> bool:
    return arch in ("universal", "noarch")


def extract_versions(html: str) -> list[dict]:
    """Port of extractVersions (scrapers/versions.ts)."""
    if CHALLENGE_MARKER in html:
        raise ProviderError("APKMirror served a bot challenge page", provider="apkmirror")
    soup = BeautifulSoup(html, "html.parser")
    table = None
    for widget in soup.select(".listWidget"):
        if widget.select_one('a[name="all_versions"]'):
            table = widget
            break
    if table is None:
        raise ProviderError("APKMirror: could not find versions table", provider="apkmirror")
    rows = [child for child in table.children if getattr(child, "name", None)][2:-1]
    versions = []
    for row in rows:
        cells = row.select(".table-cell")
        if len(cells) < 2:
            continue
        link = cells[1].select_one("a[href]")
        if not link:
            continue
        name = link.get_text(strip=True)
        url = link.get("href", "")
        if not name or not url:
            continue
        versions.append({"name": name, "url": with_base_url(url)})
    return versions


def extract_variants(html: str) -> list[dict]:
    """Port of extractVariants (scrapers/variants.ts)."""
    if CHALLENGE_MARKER in html:
        raise ProviderError("APKMirror served a bot challenge page", provider="apkmirror")
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one(".variants-table")
    if table is None:
        raise ProviderError("APKMirror: could not find variants table", provider="apkmirror")
    rows = [child for child in table.children if getattr(child, "name", None)][1:]
    variants = []
    for row in rows:
        cells = row.select(".table-cell")
        if not cells:
            continue
        link = cells[0].select_one("a[href]")
        url = link.get("href", "") if link else ""
        if not url:
            continue
        span_text = " ".join(s.get_text(" ", strip=True) for s in cells[0].select("span"))
        if "BUNDLE" in span_text.upper():
            kind = "bundle"
        elif "APK" in span_text.upper():
            kind = "apk"
        else:
            kind = "unknown"

        def cell(i: int) -> str:
            return cells[i].get_text(" ", strip=True) if len(cells) > i else ""

        variants.append({
            "version": link.get_text(strip=True) if link else "",
            "type": kind,
            "arch": cell(1),
            "min_android_version": cell(2),
            "dpi": cell(3),
            "url": with_base_url(url),
        })
    return variants


def normalize_variants(variants: list[dict]) -> list[dict]:
    """Port of normalizeVariants (helpers.ts): split 'a + b' arch cells."""
    normalized = []
    for variant in variants:
        parts = [a.strip() for a in str(variant.get("arch", "")).split("+")]
        parts = [a for a in parts if a]
        if len(parts) > 1:
            normalized.extend({**variant, "arch": arch} for arch in parts)
        else:
            normalized.append(variant)
    return normalized


def _min_android_float(value: object) -> float | None:
    match = re.search(r"\d+(?:\.\d+)?", str(value or ""))
    try:
        return float(match.group(0)) if match else None
    except ValueError:
        return None


def filter_variant(variants: list[dict], *, arch: str, dpi: str,
                   min_android: float | None, kind: str) -> dict | None:
    """Port of getFilteredVariant (helpers.ts)."""
    variants = normalize_variants(variants)
    if arch not in ("universal", "noarch"):
        matching = [v for v in variants if v.get("arch") == arch]
        variants = matching or [v for v in variants if _is_universal(str(v.get("arch", "")))]
    else:
        variants = [v for v in variants if _is_universal(str(v.get("arch", "")))]
    if dpi not in ("*", "any"):
        variants = [v for v in variants if v.get("dpi") == dpi]
    if min_android is not None:
        kept = []
        for variant in variants:
            floor = _min_android_float(variant.get("min_android_version"))
            if floor is None or floor <= min_android:
                kept.append(variant)
        variants = kept
    variants = [v for v in variants if v.get("type") == kind]
    return variants[0] if variants else None


def extract_redirect_download_url(html: str) -> str:
    """Port of extractRedirectDownloadUrl (scrapers/downloads.ts)."""
    soup = BeautifulSoup(html, "html.parser")
    link = soup.select_one("a.downloadButton[href]")
    if link is None:
        raise ProviderError("APKMirror: could not find redirect download url",
                            provider="apkmirror")
    return with_base_url(link["href"])


def extract_final_download_url(html: str) -> str:
    """Port of extractFinalDownloadUrl (scrapers/downloads.ts)."""
    soup = BeautifulSoup(html, "html.parser")
    link = soup.select_one(".card-with-tabs a[href]")
    if link is None:
        raise ProviderError("APKMirror: could not find final download url",
                            provider="apkmirror")
    return with_base_url(link["href"])


def extension_for(final_url: str, kind: str) -> str:
    filename = unquote(urlparse(final_url).path.rsplit("/", 1)[-1])
    suffix = Path(filename).suffix.lower()
    if suffix in APK_EXTENSIONS:
        return suffix
    return ".apkm" if kind == "bundle" else ".apk"


class APKMirrorProvider(Provider):
    """APKMirror via native HTTP scraping (no bun/node/subprocess needed).

    APKMirror identifies apps by ``org/repo`` slug, not by Android package
    name, so resolution starts with ``package -> (org, repo)``.

    APKMirror intermittently answers with a Cloudflare managed challenge that
    TLS impersonation alone cannot pass. When that happens this provider can
    fall back to solving the challenge in a real browser with
    ``playwright-captcha``'s Click Solver, then replay the resulting
    ``cf_clearance`` cookie through the normal HTTP session so the rest of the
    scrape is unchanged. That browser step is opt-in (it needs a headed browser
    and is slower), enabled with ``APKD_APKMIRROR_BROWSER=1`` or
    ``browser_solve=True``.
    """

    name = "apkmirror"
    capabilities = {
        "version": True,
        "arch": True,
        "dpi": True,
        "min_sdk": True,
        "browser": True,
    }

    def __init__(
        self,
        http=None,
        slug_map: dict[str, tuple[str, str]] | None = None,
        browser_solve: bool | None = None,
        browser_framework: str | None = None,
        browser_name: str | None = None,
        browser_captcha_type: str | None = None,
        browser_headless: bool | None = None,
        browser_humanised: bool | None = None,
        browser_pace: str | None = None,
        browser_video: str | None = None,
    ) -> None:
        super().__init__(http)
        self.slug_map = dict(slug_map or {})
        self.browser_solve = browser_solve
        self.browser_framework = browser_framework
        self.browser_name = browser_name
        self.browser_captcha_type = browser_captcha_type
        self.browser_headless = browser_headless
        self.browser_humanised = browser_humanised
        self.browser_pace = browser_pace
        self.browser_video = browser_video
        # Clearance is reusable, so only pay for one browser session per run.
        self._cleared = False

    def resolve_request(self, request: DownloadRequest) -> Artifact:
        org, repo = self.resolve_slug(request.package)
        arch = normalize_arch(request.arch) or "universal"
        dpi = (request.dpi or "nodpi").strip() or "nodpi"
        kind = "bundle" if request.prefer_xapk else "apk"
        min_android = SDK_TO_VERSION.get(request.min_sdk) if request.min_sdk else None
        self.http.timeout = request.timeout

        wanted = (request.version or "latest").strip()
        if wanted.lower() in SPECIAL_VERSION_TOKENS:
            versions = extract_versions(self._get_text(make_repo_url(org, repo)))
            if not versions:
                raise ProviderError(f"APKMirror found no versions for {org}/{repo}",
                                    provider=self.name)
            lowered = wanted.lower()
            if lowered == "latest":
                selected = versions[0]
            elif lowered == "beta":
                selected = next((v for v in versions if _is_beta(v["name"])), None)
            elif lowered == "alpha":
                selected = next((v for v in versions if _is_alpha(v["name"])), None)
            else:
                selected = next((v for v in versions if _is_stable(v["name"])), None)
            if selected is None:
                raise ProviderError(f"APKMirror found no {lowered} version for {org}/{repo}",
                                    provider=self.name)
            variants_url = selected["url"]
            version_name = selected["name"]
            outcome = self._get_variants(variants_url)
        else:
            candidates = self._candidate_variants_urls(org, repo, wanted)
            version_name = wanted
            outcome = None
            failures = []
            for variants_url in candidates:
                try:
                    outcome = self._get_variants(variants_url)
                    break
                except Exception as exc:  # noqa: BLE001 - try next URL shape
                    failures.append(f"{variants_url}: {exc}")
                    continue
            if outcome is None:
                raise ProviderError(
                    f"APKMirror could not load variants for {org}/{repo} "
                    f"{wanted} (" + "; ".join(failures) + ")",
                    provider=self.name,
                )
        if isinstance(outcome, Redirected):
            download_page_url = outcome.url
        else:
            variant = filter_variant(outcome, arch=arch, dpi=dpi,
                                     min_android=min_android, kind=kind)
            if variant is None and kind == "bundle":
                # Deviation from upstream (which errors out): fall back to apk
                # so prefer_xapk stays a preference, not a hard requirement.
                variant = filter_variant(outcome, arch=arch, dpi=dpi,
                                         min_android=min_android, kind="apk")
            if variant is None and kind == "apk":
                # Symmetric to the above: when the exact version only ships
                # as a bundle (e.g. ADM 14.0.39, X 12.19.1-release.0), return
                # the bundle instead of failing. Callers must check
                # artifact.extension (.apkm/.xapk vs .apk) to decide whether
                # they can consume it.
                variant = filter_variant(outcome, arch=arch, dpi=dpi,
                                         min_android=min_android, kind="bundle")
            if variant is None:
                raise ProviderError(
                    f"APKMirror found no suitable variant for {org}/{repo} "
                    f"{version_name} (arch={arch} dpi={dpi} type={kind})",
                    provider=self.name,
                )
            kind = variant["type"]
            download_page_url = variant["url"]

        redirect_url = extract_redirect_download_url(self._get_text(download_page_url))
        final_url = extract_final_download_url(self._get_text(redirect_url))
        extension = extension_for(final_url, kind)
        extra = {"org": org, "repo": repo, "dpi": dpi, "min_sdk": request.min_sdk,
                 "variant_type": kind}
        return Artifact(self.name, request.package, version_name, final_url,
                        extension, arch, extra)

    def _candidate_variants_urls(self, org: str, repo: str, wanted: str) -> list[str]:
        """Candidate variants-page URLs for an exact version, in try order.

        Deviation from upstream: instead of blindly building
        ``<repo>-<version>-release`` (which 404s whenever the display-name
        slug differs from the repo slug, e.g. ``x-`` vs ``twitter``,
        ``google-photos-`` vs ``photos``, or the truncated ``pinterest-``
        vs ``pinterest-one-destination-for-a-world-of-inspiration``), match
        against the repo's version listing first, then try the display
        prefix derived from any entry, the full repo slug, and its first
        dash-token. Callers try each until one yields a variants table.
        """
        slug = wanted.replace(".", "-")
        try:
            versions = extract_versions(self._get_text(make_repo_url(org, repo)))
        except Exception:
            versions = []
        want_key = normalize_version_key(wanted)
        for entry in versions:
            name_key = normalize_version_key(entry.get("name"))
            if name_key == want_key or name_key.endswith(want_key):
                return [entry["url"]]
        candidates = []
        if versions:
            prefix = prefix_from_version_entry(versions[0]["url"], versions[0].get("name", ""))
            if prefix is not None:
                candidates.append(
                    with_base_url(f"/apk/{org}/{repo}/{prefix}{slug}-release/"))
        first_token = repo.split("-")[0]
        if first_token and first_token != repo:
            candidates.append(
                with_base_url(f"/apk/{org}/{repo}/{first_token}-{slug}-release/"))
        candidates.append(make_variants_url(org, repo, wanted))
        seen, ordered = set(), []
        for url in candidates:
            if url not in seen:
                seen.add(url)
                ordered.append(url)
        return ordered

    def _get_variants(self, url: str) -> list[dict] | Redirected:
        text, final_url, redirected = self._fetch(url)
        if redirected:
            return Redirected(final_url)
        return extract_variants(text)
    def _get_text(self, url: str) -> str:
        text, _, _ = self._fetch(url)
        return text

    @staticmethod
    def _challenge_error(exc: Exception) -> bool:
        message = str(exc).lower()
        return any(marker in message for marker in (
            "403", "429", "just a moment", "enable javascript", "cloudflare",
        ))

    def _browser_solve_enabled(self) -> bool:
        """Whether a challenge may be solved in a real browser."""
        if self.browser_solve is not None:
            return self.browser_solve
        value = os.getenv("APKD_APKMIRROR_BROWSER", "").strip().lower()
        return value in {"1", "true", "yes", "on", "headed"}

    def _browser_headless_enabled(self) -> bool:
        """Headless is opt-in; Cloudflare rejects headless sessions outright."""
        if self.browser_headless is not None:
            return self.browser_headless
        value = os.getenv("APKD_CF_HEADLESS", "").strip().lower()
        return value in {"1", "true", "yes", "on"}

    def _humanised_clicks_enabled(self) -> bool:
        """Use a curved, jittered virtual mouse instead of an instant click."""
        if self.browser_humanised is not None:
            return self.browser_humanised
        value = os.getenv("APKD_CF_HUMANISED", "").strip().lower()
        return value not in {"0", "false", "no", "off"}

    def _solve_challenge_in_browser(self, url: str) -> None:
        """Pass the challenge in a real browser and adopt its clearance.

        The browser session is not a separate scraping path: it only exists to
        obtain ``cf_clearance`` (plus the matching User-Agent, since Cloudflare
        binds the cookie to it), which is then replayed on the HTTP session so
        every later request in this resolve passes.
        """
        captcha_type = (
            self.browser_captcha_type
            or os.getenv("APKD_CF_CAPTCHA", "")
            or "interstitial"
        ).strip().lower()
        clearance = solve_cloudflare_challenge(
            url,
            provider=self.name,
            captcha_type=captcha_type,
            framework=self.browser_framework,
            browser=self.browser_name or os.getenv("APKD_CF_BROWSER", "") or "firefox",
            headless=self._browser_headless_enabled(),
            humanised=self._humanised_clicks_enabled(),
            pace=(self.browser_pace or os.getenv("APKD_CF_PACE", "") or "quick"),
            video_path=self.browser_video,
            timeout=max(self.http.timeout, 60.0),
        )
        if not clearance.challenge_cleared or not clearance.cookies:
            raise ProviderError(
                "APKMirror Cloudflare challenge: the browser run produced no "
                "usable cookies, so the clearance could not be replayed. "
                "Automated Chromium is usually detected; try firefox "
                "(APKD_CF_BROWSER=firefox, the default) or install patchright "
                "('uv pip install patchright' then "
                "'python -m patchright install chromium') for chromium",
                provider=self.name,
            )
        clearance.to_session(self.http)
        self._cleared = True

    def _fetch(self, url: str) -> tuple[str, str, bool]:
        """GET with a staged bot-challenge escalation.

        Stage 1  plain HTTP via the shared session.
        Stage 2  curl_cffi TLS impersonation, but only when stage 1 returned a
                 page that turned out to be a challenge. A request stage 1 had
                 outright *refused* is not re-asked under a different TLS
                 fingerprint, because the refusal is about the client, not the
                 body, and retrying only adds latency.
        Stage 3  opt-in real browser: hand the URL to playwright-captcha's
                 Click Solver, then replay the ``cf_clearance`` it earns. Run
                 at most once per provider instance; a clearance that already
                 failed to stick will not start working on a second try, and
                 relaunching a browser per scraped page would be unusably slow.

        Without stage 3, a challenge is reported explicitly so a human can
        complete it, instead of it being mistaken for a missing package or a
        universal-ABI failure.
        """
        blocked: Exception | None = None
        challenged_body = False

        try:
            response = self.http.get(url)
        except Exception as exc:
            if not self._challenge_error(exc):
                raise
            blocked = exc
        else:
            text = response.text
            if CHALLENGE_MARKER not in text:
                return text, self._url_of(response, url), self._was_redirected(response, url)
            challenged_body = True

        if challenged_body and _HAS_IMPERSONATION:
            try:
                imp = _impersonated_requests.get(
                    url,
                    headers={"User-Agent": self.http.session.headers.get("User-Agent", "")},
                    impersonate=_IMPERSONATE,
                    timeout=self.http.timeout,
                    allow_redirects=True,
                )
            except Exception as exc:
                if not self._challenge_error(exc):
                    raise
                blocked = blocked or exc
            else:
                imp_text = imp.text
                if (
                    getattr(imp, "status_code", 200) not in (403, 429)
                    and CHALLENGE_MARKER not in imp_text
                ):
                    return (
                        imp_text,
                        str(getattr(imp, "url", url) or url),
                        self._was_redirected(imp, url),
                    )

        if self._cleared:
            raise ProviderError(
                "APKMirror Cloudflare challenge: a browser solve already ran in "
                "this run and its cf_clearance cookie was still rejected, so the "
                "browser is being detected; install patchright or point at a "
                "real Chrome",
                provider=self.name,
            ) from blocked

        if self._browser_solve_enabled():
            self._solve_challenge_in_browser(url)
            try:
                retry = self.http.get(url)
            except Exception as exc:
                if self._challenge_error(exc):
                    raise ProviderError(
                        "APKMirror still served a Cloudflare challenge after a "
                        "browser solve; the cf_clearance cookie did not stick",
                        provider=self.name,
                    ) from exc
                raise
            if CHALLENGE_MARKER not in retry.text:
                return (
                    retry.text,
                    self._url_of(retry, url),
                    self._was_redirected(retry, url),
                )

        raise ProviderError(
            "APKMirror Cloudflare/Turnstile challenge; rerun with "
            "APKD_APKMIRROR_BROWSER=1 to solve it automatically in a real "
            "browser, or complete it in a browser and retry this target",
            provider=self.name,
        ) from blocked

    @staticmethod
    def _url_of(response: object, fallback: str) -> str:
        return str(getattr(response, "url", fallback) or fallback)

    @staticmethod
    def _was_redirected(response: object, requested: str) -> bool:
        """A redirect matters: it is how APKMirror signals a single-variant page."""
        history = getattr(response, "history", None) or []
        return bool(history) or APKMirrorProvider._url_of(
            response, requested
        ).rstrip("/") != requested.rstrip("/")

    def resolve_slug(self, package: str) -> tuple[str, str]:
        if package in self.slug_map:
            return self.slug_map[package]
        env = os.getenv("APKD_APKMIRROR_SLUGS", "")
        for chunk in env.split(","):
            chunk = chunk.strip()
            if not chunk or "=" not in chunk:
                continue
            pkg, slug = chunk.split("=", 1)
            if pkg.strip() == package and "/" in slug:
                org, repo = slug.strip().split("/", 1)
                return org.strip(), repo.strip()
        # Let a transport-level failure (a Cloudflare challenge, an outage)
        # surface as itself. Reporting that as "slug not found" would send the
        # user off to configure a slug mapping for a package that was never the
        # problem.
        found = self._search_slug(package)
        if found is not None:
            return found
        raise ProviderError(
            f"APKMirror has no entry for package {package} in its search results; "
            "pass slug_map={'pkg': ('org','repo')} or set APKD_APKMIRROR_SLUGS='pkg=org/repo'",
            provider=self.name,
        )

    def _search_slug(self, package: str) -> tuple[str, str] | None:
        """Find the ``org/repo`` slug for a package via APKMirror's own search.

        Goes through ``_fetch`` rather than ``http.get`` so this request gets
        the same escalation as every other page: TLS impersonation, and the
        browser solve when one is enabled. The search endpoint is just as likely
        to be challenged as a download page.

        A failure here must be distinguishable from "this package is not listed".
        Collapsing a Cloudflare challenge into a silent ``None`` is what makes
        the caller tell a user to set ``APKD_APKMIRROR_SLUGS`` when the real
        problem is that the site refused the request, so transport errors
        propagate and only an exhausted-but-successful search returns ``None``.
        """
        url = (
            f"https://www.apkmirror.com/?s={quote(package)}"
            "&post_type=app_release&searchtype=apk"
        )
        text, _, _ = self._fetch(url)
        soup = BeautifulSoup(text, "html.parser")
        for link in soup.select("a[href]"):
            match = re.search(r"/apk/([^/]+)/([^/]+)/?", link.get("href", ""))
            if match:
                return match.group(1), match.group(2)
        return None
