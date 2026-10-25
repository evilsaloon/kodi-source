#!/usr/bin/env python3
"""Repackage Kodi's official stable iOS app and prepare an AltSource publication.

This script never signs apps and never publishes remotely. The workflow publishes
the immutable assets first, then commits the prepared feed and state together.
"""

import argparse
import hashlib
import json
import mmap
import os
import plistlib
import re
import shutil
import stat
import struct
import subprocess
import tarfile
import tempfile
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import yaml


METADATA_URL = "https://raw.githubusercontent.com/xbmc/kodi-tv/main/src/data/yaml/distribution/ios.yaml"
STABLE_FILENAME = re.compile(r"org\.xbmc\.kodi-ios_(\d+(?:\.\d+)+)_iphoneos-arm64\.deb\Z")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
KEEP_VERSIONS = 5
MAX_PACKAGE_BYTES = 1024 * 1024 * 1024


class UpdateError(RuntimeError):
    pass


class HTTPSRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        if urllib.parse.urlsplit(new_url).scheme != "https":
            raise UpdateError("Refusing an upstream redirect to a non-HTTPS URL")
        return super().redirect_request(request, fp, code, message, headers, new_url)


def download(url, destination, limit=MAX_PACKAGE_BYTES):
    if urllib.parse.urlsplit(url).scheme != "https":
        raise UpdateError("Downloads must use HTTPS")
    opener = urllib.request.build_opener(HTTPSRedirects())
    request = urllib.request.Request(url, headers={"User-Agent": "kodi-source-updater/1.0"})
    total = 0
    with opener.open(request, timeout=90) as response, open(destination, "wb") as target:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise UpdateError("Upstream download exceeds the configured size limit")
            target.write(chunk)
    if not total:
        raise UpdateError("Upstream returned an empty download")


def recommended_download(document):
    """Use Kodi's explicit channel assignment, never newest mirror filenames."""
    if not isinstance(document, dict) or not isinstance(document.get("releases"), list):
        raise UpdateError("Kodi metadata no longer has the expected releases list")
    releases = [r for r in document["releases"] if isinstance(r, dict) and r.get("name") == "Recommended"]
    if len(releases) != 1:
        raise UpdateError("Expected exactly one Recommended release in Kodi metadata")
    release = releases[0]
    downloads = release.get("downloads")
    if not isinstance(downloads, list):
        raise UpdateError("Recommended release has no downloads list")
    candidates = [d for d in downloads if isinstance(d, dict) and d.get("name") == "iOS (64 Bit)"]
    if len(candidates) != 1 or not isinstance(candidates[0].get("url"), str):
        raise UpdateError("Expected exactly one recommended iOS (64 Bit) download")
    url = candidates[0]["url"]
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname != "mirrors.kodi.tv" or parsed.username
            or parsed.password or parsed.port not in (None, 443)
            or not parsed.path.startswith("/releases/darwin/ios-arm64/")):
        raise UpdateError("Recommended download is not an official stable iOS mirror URL")
    filename = PurePosixPath(parsed.path).name
    match = STABLE_FILENAME.fullmatch(filename)
    if not match:
        raise UpdateError("Recommended package filename is not a final numeric stable version")
    return {"url": url, "package_version": match.group(1), "filename": filename,
            "title": release.get("title", "Kodi " + match.group(1))}


def read_json(path):
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise UpdateError("Expected a JSON object in " + str(path))
    return value


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_path(value):
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\x00" in value:
        raise UpdateError("Unsafe path in upstream archive: " + value)
    return path


def extract_deb_data(package, target):
    """Read only the data member; never execute Debian package scripts."""
    try:
        members = subprocess.check_output(["ar", "-t", str(package)], text=True).splitlines()
    except (subprocess.CalledProcessError, OSError) as exc:
        raise UpdateError("Could not inspect the Debian package with ar") from exc
    data_members = [name.strip() for name in members if re.fullmatch(r"data\.tar(?:\.(?:gz|bz2|xz|lzma))?", name.strip())]
    if len(data_members) != 1:
        raise UpdateError("Expected one supported data.tar member; upstream package format may have changed")
    data_path = target / data_members[0]
    with data_path.open("wb") as output:
        subprocess.run(["ar", "-p", str(package), data_members[0]], stdout=output, check=True)
    return data_path


def extract_app(archive_path, target):
    """Extract a single .app, with no writes through symlinks or outside target."""
    with tarfile.open(archive_path, "r:*") as archive:
        members = archive.getmembers()
        paths = {m.name: safe_path(m.name) for m in members}
        roots = set()
        for member in members:
            path = paths[member.name]
            if path.name == "Info.plist" and len(path.parts) >= 2 and path.parts[-2].endswith(".app"):
                if path.parts[-2] == "Kodi.app":
                    roots.add(PurePosixPath(*path.parts[:-1]))
        if len(roots) != 1:
            raise UpdateError("Could not find a unique Kodi.app/Info.plist in the official package")
        root = roots.pop()
        app = target / "Kodi.app"
        app.mkdir()
        selected = []
        seen = set()
        for member in members:
            path = paths[member.name]
            try:
                relative = path.relative_to(root)
            except ValueError:
                continue
            if str(relative) in seen:
                raise UpdateError("Duplicate archive path: " + str(relative))
            seen.add(str(relative))
            if not (member.isdir() or member.isfile() or member.issym() or member.islnk()):
                raise UpdateError("Unsupported special file in app: " + member.name)
            selected.append((member, relative))
        # Read regular files in archive order. Reordering compressed-tar reads can
        # repeatedly rewind/decompress a large package. Symlinks still come last
        # so no extraction writes can follow an archive-controlled symlink.
        for member, relative in selected:
            destination = app.joinpath(*relative.parts)
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source, destination.open("xb") as output:
                    shutil.copyfileobj(source, output)
                os.chmod(destination, member.mode & 0o777)
                os.utime(destination, (member.mtime, member.mtime))
        for member, relative in selected:
            if not member.islnk():
                continue
            linked = safe_path(member.linkname)
            try:
                linked = linked.relative_to(root)
            except ValueError as exc:
                raise UpdateError("Hard link points outside Kodi.app") from exc
            destination = app.joinpath(*relative.parts)
            source = app.joinpath(*linked.parts)
            if not source.is_file() or source.is_symlink():
                raise UpdateError("Hard link does not refer to a regular app file")
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.link(source, destination)
        for member, relative in selected:
            if not member.issym():
                continue
            destination = app.joinpath(*relative.parts)
            if PurePosixPath(member.linkname).is_absolute():
                raise UpdateError("Absolute symlink in app: " + member.name)
            # resolve(strict=False) also checks chained links already created.
            resolved = (destination.parent / member.linkname).resolve()
            try:
                resolved.relative_to(app.resolve())
            except ValueError as exc:
                raise UpdateError("Symlink points outside Kodi.app") from exc
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.symlink_to(member.linkname)
            os.utime(destination, (member.mtime, member.mtime), follow_symlinks=False)
        # Restore directory timestamps after creating children, for reproducibility.
        for member, relative in sorted(selected, key=lambda item: len(item[1].parts), reverse=True):
            if member.isdir():
                destination = app.joinpath(*relative.parts)
                os.chmod(destination, member.mode & 0o777)
                os.utime(destination, (member.mtime, member.mtime))
        if not any(relative == PurePosixPath(".") and member.isdir() for member, relative in selected):
            stamp = max((member.mtime for member, _ in selected), default=315532800)
            os.utime(app, (stamp, stamp))
        return app


def read_plist(path):
    with path.open("rb") as handle:
        return plistlib.load(handle)


def bundle_metadata(app):
    info = read_plist(app / "Info.plist")
    required = ["CFBundleIdentifier", "CFBundleShortVersionString", "CFBundleVersion", "MinimumOSVersion", "CFBundleExecutable"]
    for key in required:
        if not isinstance(info.get(key), str) or not info[key]:
            raise UpdateError("Missing or invalid " + key + " in Kodi.app/Info.plist")
    for key in ["CFBundleShortVersionString", "MinimumOSVersion"]:
        if not re.fullmatch(r"\d+(?:\.\d+)*", info[key]):
            raise UpdateError("Non-numeric app version field: " + key)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", info["CFBundleVersion"]):
        raise UpdateError("App build version contains characters unsafe for a release tag")
    executable_name = safe_path(info["CFBundleExecutable"])
    if len(executable_name.parts) != 1:
        raise UpdateError("Invalid app executable filename")
    executable = app / info["CFBundleExecutable"]
    if not executable.is_file() or not (executable.stat().st_mode & 0o111):
        raise UpdateError("Kodi executable is missing or is not executable")
    if not re.fullmatch(r"[A-Za-z0-9.-]+", info["CFBundleIdentifier"]):
        raise UpdateError("Unexpected bundle identifier")
    return info


def signature_entitlements(data, offset, size):
    """Read declared XML entitlements, not cryptographically verify a signature.

    Layout/constants: Apple's xnu osfmk/kern/cs_blobs.h. All signature blob
    integers are big-endian, independent of the containing Mach-O byte order.
    """
    if size < 12 or size > 64 * 1024 * 1024:
        raise UpdateError("Invalid embedded code-signature size")
    magic, length, count = struct.unpack_from(">III", data, offset)
    table_end = 12 + count * 8
    if magic != 0xFADE0CC0 or length > size or length < table_end or count > 4096:
        raise UpdateError("Malformed embedded code-signature superblob")
    slots = set()
    ranges = []
    xml = None
    has_der = False
    for index in range(count):
        slot, relative = struct.unpack_from(">II", data, offset + 12 + index * 8)
        if slot in slots or relative < table_end or relative + 8 > length:
            raise UpdateError("Invalid or duplicate code-signature slot")
        slots.add(slot)
        blob_magic, blob_length = struct.unpack_from(">II", data, offset + relative)
        if blob_length < 8 or relative + blob_length > length:
            raise UpdateError("Code-signature blob extends outside its superblob")
        ranges.append((relative, relative + blob_length))
        if slot == 5:
            if blob_magic != 0xFADE7171 or blob_length > 1024 * 1024:
                raise UpdateError("Malformed or oversized XML entitlement blob")
            xml = bytes(data[offset + relative + 8:offset + relative + blob_length])
        elif slot == 7:
            if blob_magic != 0xFADE7172:
                raise UpdateError("Malformed DER entitlement blob")
            has_der = True
        elif blob_magic in (0xFADE7171, 0xFADE7172):
            raise UpdateError("Entitlement blob is stored in an unexpected signature slot")
    ranges.sort()
    if any(left[1] > right[0] for left, right in zip(ranges, ranges[1:])):
        raise UpdateError("Overlapping embedded code-signature blobs")
    if xml is None:
        if has_der:
            raise UpdateError("DER-only entitlements are unsupported; refusing to omit app permissions")
        return {}
    if not xml.lstrip(b"\xef\xbb\xbf \t\r\n").startswith((b"<?xml", b"<plist")):
        raise UpdateError("XML entitlement slot does not contain an XML plist")
    try:
        entitlements = plistlib.loads(xml)
    except Exception as exc:
        raise UpdateError("Invalid XML entitlement plist") from exc
    if not isinstance(entitlements, dict) or not all(isinstance(key, str) for key in entitlements):
        raise UpdateError("App entitlements were not a string-keyed dictionary")
    return entitlements


def macho_entitlements(data):
    """Extract entitlement dictionaries from bounded thin/fat Mach-O slices.

    This inspects metadata only: it does not validate CodeDirectory hashes, CMS
    signatures, or the equivalence of an optional DER copy to the XML copy.
    A missing LC_CODE_SIGNATURE is unsigned; arbitrary XML elsewhere is ignored.
    """
    thin_formats = {b"\xce\xfa\xed\xfe": ("<", 28), b"\xfe\xed\xfa\xce": (">", 28),
                    b"\xcf\xfa\xed\xfe": ("<", 32), b"\xfe\xed\xfa\xcf": (">", 32)}
    fat_formats = {b"\xca\xfe\xba\xbe": (">", False), b"\xbe\xba\xfe\xca": ("<", False),
                   b"\xca\xfe\xba\xbf": (">", True), b"\xbf\xba\xfe\xca": ("<", True)}

    def thin(start, size):
        if size < 4 or bytes(data[start:start + 4]) not in thin_formats:
            raise UpdateError("Unsupported executable format; expected a Mach-O slice")
        endian, header_size = thin_formats[bytes(data[start:start + 4])]
        if size < header_size:
            raise UpdateError("Truncated Mach-O header")
        ncmds, command_bytes = struct.unpack_from(endian + "II", data, start + 16)
        if ncmds > 65536 or command_bytes > size - header_size or command_bytes < ncmds * 8:
            raise UpdateError("Invalid Mach-O load-command bounds")
        cursor = start + header_size
        command_end = cursor + command_bytes
        signature = None
        for _ in range(ncmds):
            if cursor + 8 > command_end:
                raise UpdateError("Truncated Mach-O load command")
            command, command_size = struct.unpack_from(endian + "II", data, cursor)
            if command_size < 8 or command_size % 4 or cursor + command_size > command_end:
                raise UpdateError("Invalid Mach-O load-command size")
            if command == 0x1D:  # LC_CODE_SIGNATURE / linkedit_data_command
                if command_size != 16 or signature is not None:
                    raise UpdateError("Invalid or duplicate LC_CODE_SIGNATURE")
                relative, signature_size = struct.unpack_from(endian + "II", data, cursor + 8)
                if relative < header_size + command_bytes or relative + signature_size > size:
                    raise UpdateError("Code-signature range is outside its Mach-O slice")
                signature = (start + relative, signature_size)
            cursor += command_size
        if cursor != command_end:
            raise UpdateError("Mach-O load-command sizes do not match its header")
        return signature_entitlements(data, *signature) if signature else {}

    if len(data) < 4:
        raise UpdateError("Truncated executable")
    magic = bytes(data[:4])
    if magic not in fat_formats:
        return thin(0, len(data))
    if len(data) < 8:
        raise UpdateError("Truncated fat Mach-O header")
    endian, is_64 = fat_formats[magic]
    count = struct.unpack_from(endian + "I", data, 4)[0]
    entry_size = 32 if is_64 else 20
    table_end = 8 + count * entry_size
    if not 1 <= count <= 128 or table_end > len(data):
        raise UpdateError("Invalid fat Mach-O architecture table")
    slices = []
    for index in range(count):
        cursor = 8 + index * entry_size
        if is_64:
            start, size, alignment = struct.unpack_from(endian + "QQI", data, cursor + 8)
        else:
            start, size, alignment = struct.unpack_from(endian + "III", data, cursor + 8)
        if (start < table_end or size < 4 or start + size > len(data)
                or alignment > 63 or start % (1 << alignment)):
            raise UpdateError("Invalid fat Mach-O slice bounds or alignment")
        slices.append((start, size))
    ordered = sorted(slices)
    if any(start + size > next_start for (start, size), (next_start, _) in zip(ordered, ordered[1:])):
        raise UpdateError("Overlapping fat Mach-O slices")
    combined = {}
    for start, size in slices:
        for key, value in thin(start, size).items():
            if key in combined and combined[key] != value:
                raise UpdateError("Conflicting entitlements between Mach-O architectures: " + key)
            combined[key] = value
    return combined


def read_entitlements(bundle):
    info = read_plist(bundle / "Info.plist")
    executable = info.get("CFBundleExecutable")
    if not isinstance(executable, str) or len(safe_path(executable).parts) != 1:
        raise UpdateError("Missing or invalid executable name in app bundle")
    executable = bundle / executable
    with executable.open("rb") as handle:
        if os.fstat(handle.fileno()).st_size < 4:
            raise UpdateError("Truncated app executable")
        with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
            return macho_entitlements(data)


def app_permissions(app):
    privacy = {}
    entitlements = set()
    bundles = [app] + sorted(p for p in app.rglob("*.appex") if p.is_dir() and not p.is_symlink())
    for bundle in bundles:
        info = read_plist(bundle / "Info.plist")
        for key, value in info.items():
            if "UsageDescription" in key:
                if not isinstance(value, str):
                    raise UpdateError("Unsupported structured privacy description: " + key)
                privacy.setdefault(key, value)
        entitlements.update(read_entitlements(bundle))
    entitlements.difference_update({"application-identifier", "com.apple.developer.team-identifier"})
    return {"entitlements": sorted(entitlements), "privacy": dict(sorted(privacy.items()))}


def zip_datetime(timestamp):
    value = datetime.fromtimestamp(max(timestamp, 315532800), timezone.utc)
    if value.year > 2107:
        value = datetime(2107, 12, 31, 23, 59, 58, tzinfo=timezone.utc)
    return (value.year, value.month, value.day, value.hour, value.minute, value.second)


def package_ipa(app, target):
    """Store symlinks, modes, and stable archive order without following links."""
    entries = [app]
    for root, directories, files in os.walk(app, followlinks=False):
        directories.sort()
        files.sort()
        entries.extend(Path(root) / name for name in directories + files)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        payload = zipfile.ZipInfo("Payload/", (1980, 1, 1, 0, 0, 0))
        payload.create_system = 3
        payload.external_attr = (stat.S_IFDIR | 0o755) << 16
        archive.writestr(payload, b"")
        for path in sorted(entries, key=lambda p: p.as_posix()):
            metadata = path.lstat()
            name = "Payload/Kodi.app" + ("/" + path.relative_to(app).as_posix() if path != app else "")
            if stat.S_ISDIR(metadata.st_mode):
                name += "/"
            info = zipfile.ZipInfo(name, zip_datetime(metadata.st_mtime))
            info.create_system = 3
            info.external_attr = metadata.st_mode << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            if path.is_symlink():
                archive.writestr(info, os.readlink(path).encode("utf-8"))
            elif path.is_dir():
                archive.writestr(info, b"")
            else:
                with path.open("rb") as source, archive.open(info, "w", force_zip64=True) as output:
                    shutil.copyfileobj(source, output)
    with zipfile.ZipFile(target) as archive:
        bad = archive.testzip()
        if bad:
            raise UpdateError("IPA integrity check failed: " + bad)


def make_source(repository, info, permissions, version_record, existing):
    bundle_id = info["CFBundleIdentifier"]
    old_apps = existing.get("apps", [])
    if not isinstance(old_apps, list):
        raise UpdateError("Existing source has an invalid apps list")
    old_app = next((app for app in old_apps if app.get("bundleIdentifier") == bundle_id), None)
    if old_apps and old_app is None:
        raise UpdateError("Kodi's bundle identifier changed; refusing to publish a separate app automatically")
    history = old_app.get("versions", []) if old_app else []
    records = [version_record] + [v for v in history if (v.get("version"), v.get("buildVersion")) !=
                               (version_record["version"], version_record["buildVersion"])]
    app = {"name": "Kodi", "bundleIdentifier": bundle_id, "developerName": "Kodi Foundation",
           "subtitle": "Official stable Kodi, repackaged for personal signing",
           "localizedDescription": "The official stable Kodi iOS app, repackaged as an unsigned IPA. "
                                   "This independently maintained source is not operated by the Kodi Foundation.",
           "iconURL": "https://raw.githubusercontent.com/" + repository + "/main/assets/icon.png",
           "tintColor": "17B2E7", "category": "entertainment", "versions": records[:KEEP_VERSIONS],
           "appPermissions": permissions}
    return {"name": "Kodi Stable (Unofficial)", "identifier": repository.replace("/", ".") + ".kodi-stable",
            "subtitle": "Official Kodi releases, prepared for SideStore",
            "description": "An independent source that follows Kodi's recommended stable iOS release.",
            "website": "https://github.com/" + repository, "apps": [app], "news": []}


def already_current(state, source, repository, upstream):
    if state.get("repository") != repository or state.get("upstream_url") != upstream["url"]:
        return False
    if not isinstance(source.get("apps", []), list):
        raise UpdateError("Existing source has an invalid apps list")
    for app in source.get("apps", []):
        if app.get("bundleIdentifier") == state.get("bundle_identifier"):
            versions = app.get("versions", [])
            return bool(versions and versions[0].get("version") == state.get("version")
                        and versions[0].get("buildVersion") == state.get("build_version"))
    return False


def run(args):
    if not REPOSITORY.fullmatch(args.repository):
        raise UpdateError("--repository must be an owner/repository name")
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    # Never leave an old successful manifest for the publisher after a failed run.
    manifest_path = output / "manifest.json"
    if manifest_path.exists():
        manifest_path.unlink()
    existing = read_json(Path(args.existing_source))
    state = read_json(Path(args.state))
    with tempfile.TemporaryDirectory(prefix="kodi-source-") as temporary:
        temporary = Path(temporary)
        metadata_path = temporary / "ios.yaml"
        download(args.metadata_url, metadata_path, limit=2 * 1024 * 1024)
        upstream = recommended_download(yaml.safe_load(metadata_path.read_text(encoding="utf-8")))
        if not args.force and already_current(state, existing, args.repository, upstream):
            write_json(manifest_path, {"changed": False, "upstream_url": upstream["url"],
                                       "version": state["version"], "tag": state["tag"]})
            print("No new recommended Kodi release: " + upstream["title"])
            return
        package = Path(args.package) if args.package else temporary / upstream["filename"]
        if not args.package:
            download(upstream["url"], package)
        data = extract_deb_data(package.resolve(), temporary)
        app = extract_app(data, temporary)
        info = bundle_metadata(app)
        permissions = app_permissions(app)
        version = info["CFBundleShortVersionString"]
        build = info["CFBundleVersion"]
        if tuple(map(int, version.split("."))) != tuple(map(int, upstream["package_version"].split("."))):
            raise UpdateError("Package filename and app version disagree")
        tag = "kodi-" + version + "-" + build
        ipa = output / ("Kodi-" + version + "-" + build + ".ipa")
        package_ipa(app, ipa)
        ipa_hash = sha256(ipa)
        archive_match = re.search(r"\(([A-Za-z0-9_-]+)\)", upstream["title"])
        if not archive_match:
            raise UpdateError("Could not identify Kodi's source-release codename from Recommended metadata")
        source_tag = upstream["package_version"] + "-" + archive_match.group(1)
        source_archive_url = "https://github.com/xbmc/xbmc/archive/refs/tags/" + source_tag + ".tar.gz"
        source_archive = output / ("Kodi-" + source_tag + "-source.tar.gz")
        download(source_archive_url, source_archive)
        date = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        record = {"version": version, "buildVersion": build, "date": date,
                  "localizedDescription": "Repackaged from " + upstream["title"] + ". Official package: " + upstream["url"],
                  "downloadURL": "https://github.com/" + args.repository + "/releases/download/" + tag + "/" + ipa.name,
                  "size": ipa.stat().st_size, "minOSVersion": info["MinimumOSVersion"]}
        source = make_source(args.repository, info, permissions, record, existing)
        provenance = {"repository": args.repository, "upstream_url": upstream["url"],
                      "upstream_package_sha256": sha256(package), "upstream_title": upstream["title"],
                      "bundle_identifier": info["CFBundleIdentifier"], "version": version,
                      "build_version": build, "minimum_ios": info["MinimumOSVersion"], "tag": tag,
                      "ipa_sha256": ipa_hash, "ipa_size": ipa.stat().st_size,
                      "source_archive_url": source_archive_url,
                      "source_archive_sha256": sha256(source_archive),
                      "packaging": "Official Kodi.app preserved and wrapped in Payload; no code signing performed."}
        provenance_path = output / "provenance.json"
        source_path = output / "source.json"
        state_path = output / "state.json"
        notes_path = output / "release-notes.md"
        write_json(provenance_path, provenance)
        write_json(source_path, source)
        write_json(state_path, provenance)
        notes_path.write_text("# " + upstream["title"] + " for SideStore\n\n"
                              "Official Kodi iOS binary, repackaged as an unsigned IPA. SideStore signs it on your device.\n\n"
                              "This source is independently maintained and is not an official Kodi distribution service.\n\n"
                              "- Upstream package: " + upstream["url"] + "\n"
                              "- Kodi source: " + (source_archive_url or "https://github.com/xbmc/xbmc") + "\n"
                              "- IPA SHA-256: `" + ipa_hash + "`\n\n"
                              "Upstream license notices remain inside Kodi.app. See provenance.json for exact input and output hashes.\n",
                              encoding="utf-8")
        manifest = {"changed": True, "tag": tag, "release_name": upstream["title"] + " (SideStore IPA)",
                    "version": version, "build_version": build, "ipa_path": str(ipa),
                    "source_path": str(source_path), "state_path": str(state_path),
                    "release_notes_path": str(notes_path), "provenance_path": str(provenance_path),
                    "ipa_sha256": ipa_hash, "ipa_size": ipa.stat().st_size,
                    "upstream_url": upstream["url"], "source_archive_url": source_archive_url,
                    "assets": [str(ipa), str(provenance_path), str(source_archive)]}
        write_json(manifest_path, manifest)
        print("Prepared " + tag + ": " + str(ipa))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--output-dir", default="build")
    parser.add_argument("--existing-source", default="source.json")
    parser.add_argument("--state", default="state.json")
    parser.add_argument("--metadata-url", default=METADATA_URL)
    parser.add_argument("--package", help="Reuse a locally downloaded official .deb")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    try:
        run(args)
    except (UpdateError, OSError, ValueError, tarfile.TarError, subprocess.CalledProcessError, yaml.YAMLError) as exc:
        parser.exit(1, "Update failed: " + str(exc) + "\n")


if __name__ == "__main__":
    main()
