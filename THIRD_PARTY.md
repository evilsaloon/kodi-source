# Upstream Kodi materials

Kodi is developed by the Kodi Foundation and contributors. This repository is
an independent repackaging source and is not an official Kodi distribution channel.

- Project: https://kodi.tv/
- Upstream source: https://github.com/xbmc/xbmc
- Kodi license: GPL-2.0-or-later, with additional licenses for bundled components.
- Upstream licensing details: https://github.com/xbmc/xbmc/blob/master/LICENSE.md
- Additional component notices: https://github.com/xbmc/xbmc/tree/master/LICENSES

`assets/icon.png` is copied without modification from Kodi's
`21.3-Omega/media/icon256x256.png` and remains an upstream Kodi asset.

Release IPAs are repackaged from official Kodi iOS downloads. The updater preserves
the app contents and bundled notices, records the upstream package URL and hashes,
and publishes the corresponding Kodi source archive alongside the IPA. Consult
that archive's LICENSE.md, LICENSES directory, and build/dependency instructions
for licensing and source details of the individual components.

Apple development certificates, private keys, account credentials, and device
provisioning profiles are neither needed nor included in this project.
