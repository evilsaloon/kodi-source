# Kodi Stable Source

A personal AltStore Classic / SideStore source that follows Kodi's official recommended **iOS arm64 stable release**. A scheduled GitHub Actions job repackages the official download as an IPA and publishes it through GitHub Releases. SideStore signs and installs the app using the developer team selected on your phone.

This is an independent packaging project, not an official Kodi distribution channel. It does not compile, patch, or add content to Kodi. It does not need your Apple password, developer certificate, `.p12`, provisioning profile, or device pairing file.

## Enable and test

1. Open this repository's **Actions** tab. Enable Actions if GitHub asks.
2. Select **Update Kodi source**, then **Run workflow** on the default branch. Leave **force** unchecked.
3. Wait for the run to succeed. Its summary links to the source, and the Releases page should contain a Kodi IPA with provenance information. The initial `source.json` intentionally has no apps until the first successful publication.
4. On your iPhone, connect LocalDevVPN. In **SideStore → Sources → +**, paste:

   ```text
   https://raw.githubusercontent.com/evilsaloon/kodi-source/main/source.json
   ```

5. Install Kodi from this source. Later releases appear as updates after SideStore refreshes the source; tap **Update** to install. This workflow publishes releases automatically, but does not remotely trigger installations on your phone.
6. After the manual test succeeds, enable daily checks: **repository Settings → Secrets and variables → Actions → Variables → New repository variable**. Set **`AUTO_UPDATE_ENABLED`** to **`true`**.

The daily check is scheduled for **07:23 UTC**. GitHub may delay scheduled jobs. Manual **Run workflow** works even while `AUTO_UPDATE_ENABLED` is unset or false. There is deliberately no push trigger, so creating or editing this repository does not immediately publish Kodi.

If organization policies restrict Actions or the token's access, allow the actions used by the workflow and its **contents: write** permission. No additional personal access token is required. Branch protection can prevent the final feed commit; in that case inspect the failed run and adjust the repository policy before retrying.

## How stable releases are chosen

The updater reads the Kodi website's own [iOS distribution metadata](https://github.com/xbmc/kodi-tv/blob/main/src/data/yaml/distribution/ios.yaml), selecting **Recommended → iOS (64 Bit)**. It accepts official mirror URLs for final numeric release packages under `releases/darwin/ios-arm64/`.

It does not pick the newest file in the mirror directory, where stable releases, betas, and release candidates are mixed. If Kodi changes the metadata format or package layout, the job fails and leaves the existing working feed intact.

`state.json` remembers the last published upstream package. Checks normally avoid downloading or rebuilding that same large package. **force** can recheck it; it does not authorize replacing an existing published asset with different bytes.

## What gets published

The updater extracts `Kodi.app` from Kodi's official `.deb`, preserves permissions and symbolic links, and puts the app under `Payload/` in a ZIP archive named `.ipa`. It reads the bundle identifier, application version, build version, supported iOS version, and permission metadata from the actual app rather than guessing them from the filename.

Each release has version-specific asset URLs. The workflow uploads and publishes the release before committing the new `source.json` and state. A network or packaging failure cannot replace the feed with an incomplete build. Existing assets are checked by SHA-256 and are never silently overwritten. Older published releases remain available even when the feed retains only recent version entries.

Keep the same signed bundle identifier and developer team when updating an existing installation if you want iOS to treat it as the same app. An earlier Kodi installation made with a different signer or identifier may need separate migration; this source does not change that identity automatically.

## Maintenance

- GitHub can disable public-repository schedules after 60 days without repository activity. This workflow makes a monthly commit recording a successful upstream check in `status.json`, so the repository remains active between Kodi releases. A failing check cannot advance that record. If a schedule becomes disabled, re-enable it in Actions and investigate failed runs.
- The **force** option rebuilds an already known package for diagnostics. Deterministic packaging means the existing versioned IPA should have the same digest. A mismatch deliberately stops publication; inspect the reason instead of deleting a release to bypass it.
- Check Actions failure notifications. An upstream layout or permissions change can require an updater change before a release is publishable.
- To pause automatic checks, set `AUTO_UPDATE_ENABLED` to `false` or disable the workflow. Apps already installed on your phone continue working until their signing/profile validity expires.

## Local checks

On macOS, with Python 3.12 or newer:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python scripts/update.py --repository evilsaloon/kodi-source --output-dir build --existing-source source.json --state state.json
```

The updater only prepares files under `build/`. It does not publish to GitHub, sign an app, or contact Apple. The GitHub workflow performs publication after preparation succeeds.

## Upstream and licensing

Kodi is developed by the [Kodi Foundation and contributors](https://kodi.tv/). Its code is [GPL-2.0-or-later](https://github.com/xbmc/xbmc/blob/master/LICENSE.md), and the distribution includes components with additional licenses. Repackaging preserves the app's bundled notices. Release provenance identifies the upstream binary and corresponding source information; retain those materials with redistributed packages and follow the applicable upstream licenses. The license for this repository's automation does not replace Kodi's licenses.

Relevant documentation: [Kodi's iOS packaging instructions](https://kodi.wiki/view/HOW-TO:Install_Kodi_for_iOS), [AltStore source format](https://faq.altstore.io/developers/make-a-source), [SideStore sources](https://docs.sidestore.io/docs/advanced/app-sources), and [GitHub scheduled workflows](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).
