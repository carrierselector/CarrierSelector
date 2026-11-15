import json
import plistlib
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from tools.build_selector_ipcc import BuildError, normalized_selector, main


def fake_bundle(root: Path, identifier: str) -> Path:
    root.mkdir()
    (root / "signatures").mkdir()
    (root / "Info.plist").write_bytes(
        plistlib.dumps({"CFBundleIdentifier": identifier, "CFBundleVersion": "73.0"})
    )
    (root / "carrier.plist").write_bytes(plistlib.dumps({"CarrierName": identifier}))
    (root / "signatures" / "common.plist").write_bytes(b"apple-signature-fixture")
    (root / "overrides_D83_D84_D37_D38.plist").write_bytes(
        plistlib.dumps({"Show5GSwitch": True})
    )
    (root / "signatures" / "overrides_D83_D84_D37_D38.plist").write_bytes(
        b"override-signature-fixture"
    )
    return root


class SelectorTests(unittest.TestCase):
    def test_exact_selector(self):
        self.assertEqual(
            normalized_selector("250011234567890", "8970101234567890123", "0a"),
            "250011234567890_GID1-0A_ID-8970101234567890123_ID-.bundle",
        )

    def test_exact_selector_without_gid(self):
        self.assertEqual(
            normalized_selector("250011234567890", "8970101234567890123"),
            "250011234567890_ID-8970101234567890123_ID-.bundle",
        )

    def test_rejects_partial_gid_byte(self):
        with self.assertRaises(BuildError):
            normalized_selector("250011234567890", "8970101234567890123", "A")

    def test_pair_preserves_every_inner_byte(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            vodafone = fake_bundle(root / "Vodafone_ro.bundle", "com.apple.Vodafone_ro")
            mts = fake_bundle(root / "MTS_ru.bundle", "com.apple.MTS_ru")
            output = root / "private-output"
            with mock.patch(
                "tools.build_selector_ipcc.verify_apple_signatures",
                return_value={"test-fixture": "0" * 96},
            ):
                result = main(
                    [
                        "build-pair",
                        "--vodafone",
                        str(vodafone),
                        "--mts",
                        str(mts),
                        "--imsi",
                        "250011234567890",
                        "--iccid",
                        "8970101234567890123",
                        "--output",
                        str(output),
                    ]
                )
            self.assertEqual(result, 0)
            manifest = json.loads((output / "manifest.json").read_text())
            self.assertFalse(manifest["private_identifiers_recorded"])
            self.assertNotIn("250011234567890", json.dumps(manifest))

            with zipfile.ZipFile(output / "vodafone-ro-forward.ipcc") as archive:
                names = archive.namelist()
                root_name = names[0]
                recovered = {
                    name[len(root_name) :]: archive.read(name)
                    for name in names
                    if name.startswith(root_name) and not name.endswith("/")
                }
            original = {
                path.relative_to(vodafone).as_posix(): path.read_bytes()
                for path in vodafone.rglob("*")
                if path.is_file()
            }
            self.assertEqual(recovered, original)

    def test_private_state_json_does_not_reach_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            vodafone = fake_bundle(root / "Vodafone_ro.bundle", "com.apple.Vodafone_ro")
            mts = fake_bundle(root / "MTS_ru.bundle", "com.apple.MTS_ru")
            state = root / "phone-state.json"
            state.write_text(json.dumps({
                "imsi": "250011234567890",
                "iccid": "8970101234567890123",
                "gid1": "",
            }))
            output = root / "private-output"
            with mock.patch(
                "tools.build_selector_ipcc.verify_apple_signatures",
                return_value={"test-fixture": "0" * 96},
            ):
                result = main([
                    "build-pair", "--vodafone", str(vodafone), "--mts", str(mts),
                    "--state-json", str(state), "--output", str(output),
                ])
            self.assertEqual(result, 0)
            manifest_text = (output / "manifest.json").read_text()
            self.assertNotIn("250011234567890", manifest_text)
            self.assertNotIn("8970101234567890123", manifest_text)
            self.assertEqual(json.loads(manifest_text)["selector_fields"], ["IMSI", "ICCID"])


if __name__ == "__main__":
    unittest.main()


class ResetPayloadTests(unittest.TestCase):
    """v6.2 item 3: a genuine Apple bundle with only SupportedSIMs emptied."""

    SELECTOR = "250011234567890_ID-8970101234567890123_ID-.bundle"

    def source(self) -> Path:
        from carrier_app import bundle_path
        return bundle_path("ios2721-d83/MTS_ru.bundle")

    def test_only_supported_sims_is_emptied(self):
        import io
        from tools.build_selector_ipcc import build_reset_payload
        payload = build_reset_payload(self.source(), self.SELECTOR, "com.apple.MTS_ru")
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = archive.namelist()
            root = f"Payload/{self.SELECTOR}/"
            self.assertEqual(names[0], root)
            files = {n[len(root):]: archive.read(n) for n in names if not n.endswith("/")}
        source = {p.relative_to(self.source()).as_posix(): p.read_bytes()
                  for p in self.source().rglob("*") if p.is_file()}
        self.assertEqual(set(files), set(source))
        for name, data in source.items():
            if name != "carrier.plist":
                self.assertEqual(files[name], data, name)
        changed = plistlib.loads(files["carrier.plist"])
        original = plistlib.loads(source["carrier.plist"])
        self.assertEqual(original["SupportedSIMs"], ["25001"])
        self.assertEqual(changed.pop("SupportedSIMs"), [])
        original.pop("SupportedSIMs")
        self.assertEqual(changed, original)
        self.assertTrue(files["carrier.plist"].startswith(b"bplist"), "keeps the plist format")

    def test_refuses_bad_selector_wrong_identifier_and_non_apple_donor(self):
        from tools.build_selector_ipcc import build_reset_payload
        for selector in ("", "../x.bundle", "250011234567890_ID-8970101234567890123_ID-",
                         "Payload/250011234567890_ID-8970101234567890123_ID-.bundle"):
            with self.assertRaises(BuildError, msg=selector):
                build_reset_payload(self.source(), selector, "com.apple.MTS_ru")
        with self.assertRaises(BuildError):
            build_reset_payload(self.source(), self.SELECTOR, "com.apple.Telia_dk")
        with tempfile.TemporaryDirectory() as temp:
            fake = fake_bundle(Path(temp) / "MTS_ru.bundle", "com.apple.MTS_ru")
            with self.assertRaises(BuildError):
                build_reset_payload(fake, self.SELECTOR, "com.apple.MTS_ru")
