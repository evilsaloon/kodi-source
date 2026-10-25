import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import stat
import tarfile
import tempfile
import unittest
from unittest import mock
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("update", ROOT / "scripts/update.py")
update = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(update)


def metadata(url="https://mirrors.kodi.tv/releases/darwin/ios-arm64/org.xbmc.kodi-ios_21.3_iphoneos-arm64.deb?https=1"):
    return {"releases": [{"name": "Prerelease", "downloads": [{"name": "iOS (64 Bit)", "url": "bad"}]},
                         {"name": "Recommended", "title": "Kodi v21.3 (Omega)",
                          "downloads": [{"name": "iOS (64 Bit)", "url": url}]}]}


def make_tar(path, entries):
    with tarfile.open(path, "w:gz") as archive:
        for name, kind, value in entries:
            member = tarfile.TarInfo(name)
            member.mtime = 1700000000
            member.mode = 0o755 if kind == "dir" or name.endswith("/Kodi") else 0o644
            if kind == "dir":
                member.type = tarfile.DIRTYPE
                archive.addfile(member)
            elif kind == "link":
                member.type = tarfile.SYMTYPE
                member.linkname = value
                archive.addfile(member)
            else:
                member.size = len(value)
                archive.addfile(member, io.BytesIO(value))


class MetadataTests(unittest.TestCase):
    def test_uses_recommended_channel_not_first_or_prerelease(self):
        self.assertEqual(update.recommended_download(metadata())["package_version"], "21.3")

    def test_rejects_prerelease_and_untrusted_downloads(self):
        urls = ["https://mirrors.kodi.tv/releases/darwin/ios-arm64/org.xbmc.kodi-ios_22.0~rc1_iphoneos-arm64.deb",
                "https://example.com/releases/darwin/ios-arm64/org.xbmc.kodi-ios_21.3_iphoneos-arm64.deb",
                "http://mirrors.kodi.tv/releases/darwin/ios-arm64/org.xbmc.kodi-ios_21.3_iphoneos-arm64.deb",
                "https://mirrors.kodi.tv/nightlies/darwin/ios-arm64/org.xbmc.kodi-ios_21.3_iphoneos-arm64.deb"]
        for url in urls:
            with self.subTest(url=url), self.assertRaises(update.UpdateError):
                update.recommended_download(metadata(url))

    def test_metadata_schema_change_fails_closed(self):
        for value in [{}, {"releases": []}, {"releases": [metadata()["releases"][1]] * 2}]:
            with self.assertRaises(update.UpdateError):
                update.recommended_download(value)


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_extract_and_zip_preserve_executable_and_symlink(self):
        archive_path = self.path / "data.tar.gz"
        info = {"CFBundleIdentifier": "org.xbmc.kodi-ios", "CFBundleShortVersionString": "21.3",
                "CFBundleVersion": "21.3", "MinimumOSVersion": "11.0", "CFBundleExecutable": "Kodi"}
        make_tar(archive_path, [("./Applications/Kodi.app", "dir", None),
                                ("./Applications/Kodi.app/Info.plist", "file", plistlib.dumps(info)),
                                ("./Applications/Kodi.app/Kodi", "file", b"executable"),
                                ("./Applications/Kodi.app/alias", "link", "Kodi")])
        app = update.extract_app(archive_path, self.path)
        self.assertEqual(update.bundle_metadata(app)["CFBundleIdentifier"], "org.xbmc.kodi-ios")
        one, two = self.path / "one.ipa", self.path / "two.ipa"
        update.package_ipa(app, one)
        update.package_ipa(app, two)
        self.assertEqual(update.sha256(one), update.sha256(two))
        with zipfile.ZipFile(one) as archive:
            self.assertTrue(archive.getinfo("Payload/Kodi.app/Kodi").external_attr >> 16 & 0o111)
            link = archive.getinfo("Payload/Kodi.app/alias")
            self.assertTrue(stat.S_ISLNK(link.external_attr >> 16))
            self.assertEqual(archive.read(link), b"Kodi")

    def test_rejects_archive_traversal(self):
        archive = self.path / "bad.tar.gz"
        make_tar(archive, [("../../outside", "file", b"bad")])
        with self.assertRaises(update.UpdateError):
            update.extract_app(archive, self.path)
        self.assertFalse((self.path.parent / "outside").exists())

    def test_compressed_tar_files_are_read_in_archive_order(self):
        archive_path = self.path / "ordered.tar.gz"
        make_tar(archive_path, [("Applications/Kodi.app/Info.plist", "file", b"plist"),
                                ("Applications/Kodi.app/deep/resources/media.bin", "file", b"media"),
                                ("Applications/Kodi.app/Kodi", "file", b"executable")])
        offsets = []
        original = tarfile.TarFile.extractfile

        def track_extractfile(archive, member):
            offsets.append(member.offset_data)
            return original(archive, member)

        with mock.patch.object(tarfile.TarFile, "extractfile", new=track_extractfile):
            app = update.extract_app(archive_path, self.path)
        self.assertEqual(offsets, sorted(offsets))
        self.assertEqual((app / "deep/resources/media.bin").read_bytes(), b"media")
        self.assertEqual((app / "Kodi").read_bytes(), b"executable")

    def test_rejects_symlink_escaping_app(self):
        archive = self.path / "bad.tar.gz"
        make_tar(archive, [("Applications/Kodi.app/Info.plist", "file", b"plist"),
                           ("Applications/Kodi.app/link", "link", "../../outside")])
        with self.assertRaises(update.UpdateError):
            update.extract_app(archive, self.path)

    def test_collects_privacy_and_entitlements_from_actual_bundle(self):
        app = self.path / "Kodi.app"
        app.mkdir()
        (app / "Info.plist").write_bytes(plistlib.dumps({"NSLocalNetworkUsageDescription": "Find media"}))
        with mock.patch.object(update, "read_entitlements", return_value={"get-task-allow": True, "application-identifier": "OLD"}):
            result = update.app_permissions(app)
        self.assertEqual(result, {"entitlements": ["get-task-allow"], "privacy": {"NSLocalNetworkUsageDescription": "Find media"}})

    def test_accepts_kodi_actual_alphanumeric_build_version(self):
        app = self.path / "Kodi.app"
        app.mkdir()
        info = {"CFBundleIdentifier": "org.xbmc.kodi-ios", "CFBundleShortVersionString": "21.3",
                "CFBundleVersion": "20251031-a3a448d26b", "MinimumOSVersion": "11.0", "CFBundleExecutable": "Kodi"}
        (app / "Info.plist").write_bytes(plistlib.dumps(info))
        (app / "Kodi").write_bytes(b"binary")
        (app / "Kodi").chmod(0o755)
        self.assertEqual(update.bundle_metadata(app)["CFBundleVersion"], "20251031-a3a448d26b")

    def test_no_signature_warning_is_unsigned_but_other_codesign_failures_raise(self):
        no_signature = mock.Mock(returncode=1, stdout=b"", stderr=b"Kodi.app: no signature\nwarning: invalid entitlements blob\n")
        with mock.patch.object(update.subprocess, "run", return_value=no_signature):
            self.assertEqual(update.read_entitlements(Path("Kodi.app")), {})
        bad_signature = mock.Mock(returncode=1, stdout=b"", stderr=b"invalid or unsupported format for signature\n")
        with mock.patch.object(update.subprocess, "run", return_value=bad_signature):
            with self.assertRaises(update.UpdateError):
                update.read_entitlements(Path("Kodi.app"))


class FeedTests(unittest.TestCase):
    def test_history_replaces_same_build_and_keeps_other_versions(self):
        info = {"CFBundleIdentifier": "org.xbmc.kodi-ios"}
        old = {"apps": [{"bundleIdentifier": info["CFBundleIdentifier"],
                          "versions": [{"version": "21.3", "buildVersion": "1", "downloadURL": "old"},
                                       {"version": "21.2", "buildVersion": "1"}]}]}
        record = {"version": "21.3", "buildVersion": "1", "downloadURL": "new"}
        source = update.make_source("owner/repo", info, {}, record, old)
        self.assertEqual(source["apps"][0]["versions"], [record, {"version": "21.2", "buildVersion": "1"}])
        self.assertNotIn("marketplaceID", source["apps"][0])

    def test_bundle_identity_change_is_not_published_automatically(self):
        with self.assertRaises(update.UpdateError):
            update.make_source("owner/repo", {"CFBundleIdentifier": "new.id"}, {}, {"version": "1", "buildVersion": "1"},
                               {"apps": [{"bundleIdentifier": "old.id", "versions": []}]})

    def test_unchanged_requires_matching_feed_and_state(self):
        upstream = update.recommended_download(metadata())
        state = {"repository": "owner/repo", "upstream_url": upstream["url"],
                 "bundle_identifier": "id", "version": "21.3", "build_version": "1"}
        source = {"apps": [{"bundleIdentifier": "id", "versions": [{"version": "21.3", "buildVersion": "1"}]}]}
        self.assertTrue(update.already_current(state, source, "owner/repo", upstream))
        self.assertFalse(update.already_current(state, {}, "owner/repo", upstream))
        self.assertFalse(update.already_current(state, source, "different/repo", upstream))


if __name__ == "__main__":
    unittest.main()
