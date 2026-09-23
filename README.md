# apkd

Standalone Android APK downloader with provider-specific implementations.

## Providers

| Provider | Implementation |
| --- | --- |
| APKMirror | Git submodule: `apkmirror-downloader` |
| APKPure | Git submodule: `apkpure-downloader` |
| Uptodown | Native Python provider |
| APKCombo | Native Python provider |
| Aptoide | Native Python provider |

The native providers resolve a package and version before downloading. Downloaded files are validated as ZIP-based Android packages and must contain `AndroidManifest.xml`.

## Usage

```bash
python -m pip install -e .
apkd uptodown com.google.android.apps.photos --version 7.90.0.971743778 -o photos.apk
apkd apkcombo com.google.android.apps.photos --version 7.90.0.971743778 -o photos.apk
apkd aptoide com.google.android.apps.photos --version 7.90.0.971743778 -o photos.apk
```

Omit `--version` to resolve the provider's current version.

## Testing

Unit tests run without network access:

```bash
python -m unittest discover -s tests -v
```

Provider integration tests run in GitHub Actions against the public provider endpoints. They resolve Google Photos and validate the actual downloaded artifact.

APKMirror and APKPure remain separate submodules so their existing downloader implementations can evolve independently.
