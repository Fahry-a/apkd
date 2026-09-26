# apkd

Standalone Android APK downloader with provider-specific implementations.
All providers are native Python — one `pip install` is everything you need.

## Providers

| Provider | Implementation | Status |
| --- | --- | --- |
| APKPure | Native Python, vendored from `apkpure-downloader` (see Credits) | Active |
| Aptoide | Native Python provider | Active |
| APKCombo | Native Python provider | Active |
| APKMirror | Native Python port of `apkmirror-downloader` (see Credits) | Active |
| Uptodown | Exact-version metadata + visible-browser Turnstile handoff | Opt-in |

All providers resolve a package and version before downloading. Downloaded files are validated as ZIP-based Android packages and must contain `AndroidManifest.xml`.

`arch="universal"` is a strict contract: a native APK/bundle must contain both
`arm64-v8a` and `armeabi-v7a` (or no native code at all). Providers must not
silently substitute a single-ABI asset. APKCombo `/r2?u=...` and `/d?u=...`
redirect URLs are preserved with their guard parameters before download.

## Usage

```bash
python -m pip install -e .
apkd download com.google.android.apps.photos --version 7.90.0.971743778 --arch arm64-v8a -o photos.apk
apkd download com.google.android.apps.photos --providers apkpure,aptoide,apkcombo -o photos.apk
apkd download com.rawcam.app --version 1.4.1 --arch arm32 --app-slug native-camera -o camera.apk
apkd providers
```

Omit `--version` to resolve the provider's current version. `--app-slug`
passes a known APKPure page slug so the exact-version fallback does not need
to discover it; the output extension is corrected to the real container
(`.xapk`/`.apkm`/`.apks`) when the provider returns a bundle. `-v` logs each
provider attempt to stderr. Exit codes: `0` ok, `2` usage, `3` every provider
failed, `4` the artifact failed validation.

Legacy single-provider form still works but is deprecated:

```bash
apkd aptoide com.google.android.apps.photos --version 7.90.0.971743778 -o photos.apk
```

## Library

```python
from apkd import DownloadRequest, download, download_simple

req = DownloadRequest(package="com.google.android.apps.photos",
                      version="7.90.0.971743778", arch="arm64-v8a")
result = download(req, providers=["apkpure", "aptoide", "apkcombo", "apkmirror"],
                  output="photos.apk")
print(result.provider, result.version, result.path)

# Shorthand without dataclass:
result = download_simple("com.termux", arch="arm64-v8a", output="termux.apk")
```

`download()` tries each provider in order, validates the artifact as a
ZIP-based Android package containing `AndroidManifest.xml`, checks the
`arch="universal"` contract (both `arm64-v8a` and `armeabi-v7a`, or no native
code) and any `file_sha256` the provider reported, then promotes a `.part`
file atomically. It raises `DownloadError` with per-provider errors if every
provider fails.

APKMirror needs an `org/repo` slug (`APKD_APKMIRROR_SLUGS="pkg=org/repo"` or
`slug_map`) since APKMirror identifies apps by slug, not package name. A
Cloudflare/Turnstile response is reported explicitly by default and must be
completed manually in a browser. The provider can also solve it for you in a
real browser; see [Cloudflare challenge solving](#cloudflare-challenge-solving).

Uptodown is intentionally not part of the default fallback order. To use its
exact-version metadata and visible-browser download flow, select it explicitly
and set `APKD_UPTODOWN_BROWSER=1`; the operator must complete the challenge in
the opened Chromium window. If Turnstile rejects Playwright's bundled
Chromium, start a normal Chrome yourself and set `APKD_UPTODOWN_CDP_URL` to its
remote-debugging endpoint. Set `APKD_UPTODOWN_MANUAL_CLICK=1` if you want
Playwright to observe the response while you click Download yourself. This
still requires the operator to solve the challenge manually. Pass
`app_id` on the request (`DownloadRequest(..., app_id="20013")`, mirrored from
a config that already knows the stable numeric ID) alongside the slug to avoid
a fragile app-page lookup. No third-party CAPTCHA solver is used. The AJAX
`downloadURL` grant is redacted before it can reach an exception message.

## Cloudflare challenge solving

APKMirror intermittently answers with a Cloudflare managed challenge that TLS
impersonation alone cannot pass. This is opt-in: the default behaviour is still
to stop and report the challenge, because it needs a real browser, is slower,
and may be rate-limited by repeated attempts.

```bash
uv pip install 'apkd[captcha]'          # playwright-captcha, playwright, patchright
python -m patchright install chromium   # once, to fetch the browser

APKD_APKMIRROR_BROWSER=1 apkd download com.pinterest.pin -o out.apk
```

When a challenge appears, the provider opens a real browser, solves it with
[`playwright-captcha`](https://github.com/techinz/playwright-captcha)'s Click
Solver, then replays the resulting `cf_clearance` cookie (and the matching
User-Agent, which Cloudflare binds it to) on the normal HTTP session. Scraping
is unchanged; only the clearance comes from the browser.

Stealth matters here. Standard Playwright Chromium is fingerprinted and the
challenge never mounts. `patchright` is a drop-in stealth fork and is preferred
automatically, but it injects over CDP, so it only works with Chromium. Firefox
therefore uses the vanilla driver; you can try `APKD_CF_BROWSER=firefox`, but
Chromium + patchright gets furthest.

### Why this needs patching, not just configuring

`playwright-captcha` mounts nothing itself: it reports
`CaptchaDetectionError: Cloudflare iframes not found` and stops. Four separate
defects sit behind that message, all verified against the live site and all
handled in `apkd/providers/_shadow_unlock.py`:

1. **Its shadow-root patch never runs under patchright.** The challenge widget
   lives in a *closed* shadow root, so it has to be unsealed before anything
   can find it. Every injection route the library tries is a no-op there
   (context/page `add_init_script` and both CDP forms), and `page.evaluate`
   under patchright defaults to `isolated_context=True`, writing to an isolated
   world the page never reads. The library logs *"Injected unlockShadowRoot.js
   via CDP"* while `window._shadowRootPatched` stays `false`. `apkd` patches
   the main world explicitly with `isolated_context=False`.
2. **The challenge iframe is a separate document.** It has its own
   `attachShadow`, so patching the top page does nothing for the checkbox that
   actually lives inside it. `ShadowKeepalive` patches each document as it
   appears, until the `input[type="checkbox"]` (labelled *"Verify you are
   human"*) is visible inside it.
3. **Its shadow-root traversal loses the handle.** Once the roots are open,
   Playwright's own CSS engine pierces them, so the hand-rolled walk is
   replaced with plain selectors — keeping the original's waiting behaviour,
   which a bare `query_selector_all` would drop.
4. **It cannot verify success either**, for the same blind spot, so a real pass
   still raises. `apkd` asks the page instead: no challenge token in the URL, no
   live challenge iframe, and none of the interstitial's own copy.

With these in place the checkbox is found and clicked, and Cloudflare issues a
`cf_clearance` cookie.

### What still does not work, and why

**The clearance does not transfer to a plain HTTP client.** `cf_clearance` is
bound to the browser session that earned it, not just to a User-Agent. Replaying
it through `requests` or through `curl_cffi` was measured at HTTP 403 across
`chrome145`, `chrome146` and `chrome150` impersonation, and `curl_cffi` cannot
reach a Chrome 153 fingerprint at all. The cookie is harvested and reported, but
treating it as a portable token does not work.

The click itself does register — the widget transitions from *"Verify you are
human"* to *"Verifying you are human"* — but Cloudflare's server-side verdict
keeps declining and re-issues the challenge. That decision is not something this
code can influence. What does affect it:

* `APKD_CF_PROFILE=/path/to/dir` reuses a persistent browser profile, so trust
  accumulates across runs instead of starting from zero each time. This is the
  only lever here that changes the verdict rather than our behaviour.
* Real Chrome, not bundled Chromium: install `google-chrome-stable`.
  `patchright` uses `channel="chrome"` automatically when it is present, and is
  the configuration its author recommends.
* A cool-off. Repeated attempts from one address make the challenge stricter.

When the verdict is refused, the provider reports the challenge rather than
pretending to have downloaded anything.

| Variable | Purpose |
| --- | --- |
| `APKD_APKMIRROR_BROWSER=1` | enable the browser solve (this is the opt-in) |
| `APKD_CF_BROWSER` | `firefox` (default) or `chromium` |
| `APKD_CF_FRAMEWORK` | `patchright` or `playwright`; auto-detected otherwise |
| `APKD_CF_CAPTCHA` | `interstitial` (default) or `turnstile` |
| `APKD_CF_PACE` | `flick`, `quick`, or `careful` |
| `APKD_CF_HEADLESS=1` | run without a window (Cloudflare usually rejects this) |
| `APKD_CF_HUMANISED=0` | use plain `element.click()` instead of the virtual mouse |
| `APKD_CF_VIDEO=/path/solve.webm` | record the solve |
| `APKD_CF_PROFILE` | reuse a persistent browser profile (lets trust accumulate) |

### The virtual mouse

`playwright-captcha` clicks with `element.click()`, which teleports the cursor
to the element centre in a single tick. `apkd` replaces only that click step
(the library still does the finding and the verifying) with a short
`page.mouse` approach: a few waypoints with jitter, a randomised pause, an
off-centre landing, then press and release. Every input goes through
`page.mouse`, so the browser still synthesises trusted events itself —
nothing spoofs `isTrusted`.

One note worth knowing up front: Playwright's screenshots and videos capture
page compositing output, which does not include the OS pointer. The movement is
therefore invisible in any recording, including the ones in the
playwright-captcha README — watch the real window instead.

Only use this against sites you are authorised to fetch.

## Credits

* **APKPure internal-API client** — reversed and originally implemented by
  [@codewithwan](https://github.com/codewithwan) in
  [apkpure-downloader](https://github.com/codewithwan/apkpure-downloader).
  The signing/fingerprint/transport layer is vendored 1:1 under
  `apkd/providers/_apkpure/` (only the CLI/download pieces were left out;
  downloads go through `apkd.fallback` instead).
* **APKMirror scraping flow** (versions → variants → arch/dpi/type filtering
  → two-step download redirect) — designed and originally implemented by
  [@tanishqmanuja](https://github.com/tanishqmanuja) in
  [apkmirror-downloader](https://github.com/tanishqmanuja/apkmirror-downloader).
  Ported to Python in `apkd/providers/apkmirror.py` with function-by-function
  correspondence (see the porting map at the top of that file).

Keeping their work easy to follow upstream: each vendored/ported area records
its upstream commit (`apkd/providers/_apkpure/VENDORED.md`, header of
`apkd/providers/apkmirror.py`) plus the exact diff/sync steps.

## Testing

Unit tests run without network access:

```bash
uv run --no-sync python -m pytest tests/ -v
```

Live provider tests are opt-in (they hit public endpoints and download real
artifacts). The default Google Photos case can be overridden per provider:

```bash
APKD_LIVE_TESTS=1 APKD_PROVIDER=apkpure \
  APKD_APKPURE_PACKAGE=com.rawcam.app APKD_APKPURE_VERSION=1.4.1 \
  uv run --no-sync python -m pytest tests/test_live_providers.py -v
```

Diagnostics used to find the four defects above, kept because they are how you
re-check them if the site changes:

```bash
python tools/diagnose_shadow.py        # is the shadow root sealed?
python tools/diagnose_injection.py     # which injection route actually runs?
python tools/probe_checkbox.py         # can we reach the frame and checkbox?
python tools/diagnose_cf.py            # dump the frame tree and widget state
```

Provider integration tests run in GitHub Actions against the public provider endpoints. They resolve Google Photos and validate the actual downloaded artifact.
