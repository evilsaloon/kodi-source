import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import stat
import struct
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


def signature_fixture(slots):
    header_size = 12 + 8 * len(slots)
    index = bytearray()
    payload = bytearray()
    for slot, magic, content in slots:
        index.extend(struct.pack(">II", slot, header_size + len(payload)))
        payload.extend(struct.pack(">II", magic, 8 + len(content)) + content)
    return struct.pack(">III", 0xFADE0CC0, header_size + len(payload), len(slots)) + index + payload


def thin_fixture(entitlements=None, signature=None, endian="<", is_64=True):
    if entitlements is not None:
        signature = signature_fixture([(5, 0xFADE7171, plistlib.dumps(entitlements))])
    header_size = 32 if is_64 else 28
    fields = [0xFEEDFACF if is_64 else 0xFEEDFACE, 0x100000C, 0, 2,
              1 if signature is not None else 0, 16 if signature is not None else 0, 0]
    if is_64:
        fields.append(0)
    header = struct.pack(endian + "I" * len(fields), *fields)
    if signature is None:
        return header
    return header + struct.pack(endian + "IIII", 0x1D, 16, header_size + 16, len(signature)) + signature


def fat_fixture(slices, is_64=False):
    offset = 8 + (32 if is_64 else 20) * len(slices)
    entries = bytearray()
    for index, binary in enumerate(slices):
        if is_64:
            entries.extend(struct.pack(">IIQQII", index, 0, offset, len(binary), 0, 0))
        else:
            entries.extend(struct.pack(">IIIII", index, 0, offset, len(binary), 0))
        offset += len(binary)
    return struct.pack(">II", 0xCAFEBABF if is_64 else 0xCAFEBABE, len(slices)) + entries + b"".join(slices)


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


class MachOTests(unittest.TestCase):
    def test_unsigned_does_not_scan_unreferenced_xml(self):
        binary = thin_fixture() + plistlib.dumps({"unreferenced": True})
        self.assertEqual(update.macho_entitlements(binary), {})

    def test_reads_xml_on_both_endiannesses_and_word_sizes(self):
        expected = {"get-task-allow": True, "example.groups": ["one", "two"]}
        for endian in ("<", ">"):
            for is_64 in (False, True):
                with self.subTest(endian=endian, is_64=is_64):
                    self.assertEqual(update.macho_entitlements(thin_fixture(expected, endian=endian, is_64=is_64)), expected)

    def test_reads_bundle_executable_without_platform_tools(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundle = Path(temporary)
            (bundle / "Info.plist").write_bytes(plistlib.dumps({"CFBundleExecutable": "Kodi"}))
            (bundle / "Kodi").write_bytes(thin_fixture({"example": True}))
            with mock.patch.object(update.subprocess, "run", side_effect=AssertionError("platform tool invoked")):
                self.assertEqual(update.read_entitlements(bundle), {"example": True})

    def test_merges_all_fat_slices_with_relative_signature_offsets(self):
        for is_64 in (False, True):
            binary = fat_fixture([thin_fixture({"first": True}), thin_fixture({"second": ["value"]})], is_64=is_64)
            self.assertEqual(update.macho_entitlements(binary), {"first": True, "second": ["value"]})

    def test_rejects_conflicting_architecture_entitlements(self):
        binary = fat_fixture([thin_fixture({"example": True}), thin_fixture({"example": False})])
        with self.assertRaisesRegex(update.UpdateError, "Conflicting"):
            update.macho_entitlements(binary)

    def test_rejects_der_only_instead_of_silently_omitting(self):
        binary = thin_fixture(signature=signature_fixture([(7, 0xFADE7172, b"DER")]))
        with self.assertRaisesRegex(update.UpdateError, "DER-only"):
            update.macho_entitlements(binary)

    def test_signature_without_entitlements_is_empty(self):
        binary = thin_fixture(signature=signature_fixture([(2, 0xFADE0C01, b"requirements")]))
        self.assertEqual(update.macho_entitlements(binary), {})

    def test_rejects_malformed_xml_and_superblob_indices(self):
        bad_xml = thin_fixture(signature=signature_fixture([(5, 0xFADE7171, b"not an XML plist")]))
        bad_index = bytearray(signature_fixture([(5, 0xFADE7171, plistlib.dumps({"x": True}))]))
        struct.pack_into(">I", bad_index, 16, 0xFFFFFFF0)
        for binary in (bad_xml, thin_fixture(signature=bad_index)):
            with self.assertRaises(update.UpdateError):
                update.macho_entitlements(binary)

    def test_rejects_signature_range_outside_slice_and_truncated_load_command(self):
        bad_range = bytearray(thin_fixture({"x": True}))
        struct.pack_into("<I", bad_range, 40, len(bad_range) + 1)
        bad_command = bytearray(thin_fixture({"x": True}))
        struct.pack_into("<I", bad_command, 36, 4)
        for binary in (bad_range, bad_command, b"\xcf\xfa\xed\xfe", b"unknown"):
            with self.assertRaises(update.UpdateError):
                update.macho_entitlements(binary)


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
