# Vendored APKPure client

Source: https://github.com/codewithwan/apkpure-downloader
(by [@codewithwan](https://github.com/codewithwan) — credit for reversing the
APKPure v3 internal API from the official Android client.)

Upstream commit: `e6157617b0f89cd8b1da884398210b4c873fc37a` (main)

## Layout

Files here mirror `apkpure_dl/` 1:1 (`config`, `crypto`, `fingerprint`,
`transport`, `cache`, `client`) so upstream updates can be ported with a plain
diff:

```bash
git -C /tmp/apkpure-upstream fetch origin
git -C /tmp/apkpure-upstream diff e6157617b0f89cd8b1da884398210b4c873fc37a..origin/main \
  -- apkpure_dl/ > /tmp/apkpure.patch
# apply the apkpure_dl/ hunks onto apkd/providers/_apkpure/ by hand,
# then update the "Upstream commit" line above.
```

`client` (`call`, `app_detail`, `app_his_version`, `extract_asset`) and
`config` (`Config`) are used by `apkd/providers/apkpure.py`. `cache` backs
`client.call(use_cache=True)`, which the provider leaves off; it is kept because
it is a working part of the upstream client that library consumers can enable.
`crypto` and `fingerprint` support the request signing in `client`.

Local divergence from upstream, all in `transport.py`: `get()` (a CDN streaming
helper `apkd` does not use) was removed, and `IMPERSONATE_CHAIN` is now filtered
against the installed curl_cffi's `BrowserTypeLiteral` so a dropped fingerprint
name cannot cause silent handshake failures. Keep those edits when re-syncing.

CLI-only pieces (`cli.py`, `downloader.py`, `extract_keys.py`, `__main__.py`)
were deliberately NOT vendored — downloads and validation go through
`apkd.fallback` / `apkd.validation` instead.

There is no CI job asserting this tree still matches upstream. If you re-sync,
diff it and bump the commit above in the same change.
