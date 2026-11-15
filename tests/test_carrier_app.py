import contextlib
from datetime import datetime, timezone
import io
import json
import os
import plistlib
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import carrier_app
from pymobiledevice3.exceptions import ConnectionTerminatedError


def line(slot: str, imsi: str, iccid: str) -> dict[str, object]:
    return {
        "slot": slot,
        "imsi": imsi,
        "iccid": iccid,
        "product": "iPhone16,1",
        "hardware": "D83AP",
        "version": "27.2",
        "build": "24B119",
        "bundle_identifier": "com.apple.MTS_ru",
        "bundle_version": "72.0",
    }



_LOG_SANDBOX = None


def setUpModule():
    """Keep test logs out of the program folder (v6.1 writes logs next to the program)."""
    global _LOG_SANDBOX
    _LOG_SANDBOX = (tempfile.TemporaryDirectory(), mock.patch("carrier_app.program_root"))
    _LOG_SANDBOX[1].start().return_value = Path(_LOG_SANDBOX[0].name)


def tearDownModule():
    _LOG_SANDBOX[1].stop()
    _LOG_SANDBOX[0].cleanup()

class CarrierCatalogTests(unittest.TestCase):
    def test_iphone_18_pro_max_v64_uses_its_own_catalog(self):
        catalog = carrier_app.load_catalog()
        family = carrier_app.matching_family(catalog, {
            "product": "iPhone19,3", "hardware": "V64AP",
        })
        self.assertIsNotNone(family)
        self.assertEqual(family["catalog_root"], "ios2721-v63")
        self.assertIn("full-v64", family["extra_roots"])
        self.assertNotIn("V64AP", next(
            item for item in catalog["families"] if "full-v63" in item.get("extra_roots", [])
        )["hardwares"])

    def test_global_iphone_18_pro_max_keeps_v64s_catalog(self):
        catalog = carrier_app.load_catalog()
        family = carrier_app.matching_family(catalog, {
            "product": "iPhone19,7", "hardware": "V64sAP",
        })
        self.assertIsNotNone(family)
        self.assertEqual(family["catalog_root"], "ios2721-v63")
        self.assertIn("full-v63", family["extra_roots"])

    def test_v64_catalog_contains_signed_board_overrides(self):
        catalog = carrier_app.load_catalog()
        self.assertIn("full-v64", carrier_app.catalog_roots(catalog))
        profiles = carrier_app.catalog_profiles({"name": "full-v64", "catalog_root": "full-v64"})
        self.assertGreaterEqual(len(profiles), 20)
        root = Path(carrier_app.resource_root()) / "embedded_catalog" / "full-v64"
        with_v64 = [path for path in root.glob("*.bundle") if (
            path / "overrides_V64.plist"
        ).is_file() and (path / "signatures" / "overrides_V64.plist").is_file()]
        self.assertEqual(len(with_v64), len(profiles))
        # iOS 27.2.1 (24B156) Apple set: V64 settings for most operators, Vodafone_ro included.
        new_root = Path(carrier_app.resource_root()) / "embedded_catalog" / "ios2721-v63"
        signed_v64 = [path for path in new_root.glob("*.bundle") if (
            path / "overrides_V64.plist").is_file() and (path / "signatures" / "overrides_V64.plist").is_file()]
        self.assertGreaterEqual(len(signed_v64), 600)
        self.assertIn(new_root / "Vodafone_ro.bundle", signed_v64)

    def test_v64_uses_exact_profiles_and_global_common_fallbacks(self):
        catalog = carrier_app.load_catalog()
        overview = {"product": "iPhone19,3", "hardware": "V64AP"}
        family = carrier_app.matching_family(catalog, overview)
        profiles = carrier_app.profiles_for(catalog, family, overview)
        ro = [row for row in profiles if row["identifier"] == "com.apple.Vodafone_ro"]
        tr = [row for row in profiles if row["identifier"] == "com.apple.Vodafone_tr"]
        self.assertTrue(ro)
        self.assertTrue(any("общие настройки" in str(row["compatibility"]) for row in ro))
        self.assertTrue(any(
            row["version"] == "72.1" and "точные настройки V64" in str(row["compatibility"])
            for row in tr
        ))

    def test_iphone_air_can_restore_foreign_common_profile(self):
        catalog = carrier_app.load_catalog()
        overview = {"product": "iPhone18,4", "hardware": "D23AP"}
        family = carrier_app.matching_family(catalog, overview)
        profiles = carrier_app.profiles_for(catalog, family, overview)
        original = {"bundle_identifier": "com.apple.Free_fr", "bundle_version": "73.0"}
        fallback = carrier_app.compatible_original_profile(profiles, original)
        self.assertIsNotNone(fallback)
        self.assertEqual(fallback["identifier"], "com.apple.Free_fr")
        self.assertEqual(fallback["version"], "73.0")  # exact original now in the iOS 27.2.1 set
        self.assertTrue(any(row["identifier"] == "com.apple.MTS_ru" for row in profiles))

    def test_profile_search_normalizes_spaces_dashes_and_underscores(self):
        profile = {"label": "Vodafone_tr 72.1", "identifier": "com.apple.Vodafone_tr"}
        for query in ("Vodafone TR", "vodafone_tr", "VODAFONE-TR", "vodafone"):
            self.assertTrue(carrier_app.profile_matches_query(profile, query))

    def test_full_iphone_15_catalog_contains_all_major_ru_operators(self):
        catalog = carrier_app.load_catalog()
        family = next(item for item in catalog["families"] if item["name"] == "iPhone 15 family")
        profiles = carrier_app.profiles_for(catalog, family)
        labels = "\n".join(str(item["label"]) for item in profiles)
        self.assertGreaterEqual(len(profiles), 680)
        for expected in ("MegaFon_ru", "MTS_ru", "Tele2_ru", "VimpelCom_ru", "Beeline", "Telia_dk"):
            self.assertIn(expected, labels)

    def test_baseline_is_private_and_keeps_exact_original_profile(self):
        state = line("kOne", "250011234567890", "8970101234567890123")
        state["version"] = "27.0.1"  # MTS_ru 72.0 is the built-in profile of iOS 27.0.x
        with tempfile.TemporaryDirectory() as temp, mock.patch(
            "carrier_app.state_root", return_value=Path(temp)
        ):
            original = carrier_app.remember_baseline(state)
            self.assertEqual(original["bundle_identifier"], "com.apple.MTS_ru")
            self.assertEqual(original["bundle_version"], "72.0")
            raw = carrier_app.baseline_path(state).read_text(encoding="utf-8")
            self.assertNotIn(str(state["imsi"]), raw)
            self.assertNotIn(str(state["iccid"]), raw)
            self.assertEqual(carrier_app.load_baseline(state), original)

    def test_restore_never_redefines_missing_baseline_from_current_profile(self):
        state = line("kOne", "250011234567890", "8970101234567890123")
        state["bundle_identifier"] = "com.apple.MTN_cy"
        with tempfile.TemporaryDirectory() as temp, mock.patch(
            "carrier_app.state_root", return_value=Path(temp)
        ):
            self.assertIsNone(carrier_app.remember_baseline(state, allow_current=False))
            self.assertFalse(carrier_app.baseline_path(state).exists())

    def test_prepared_rollback_survives_catalog_lookup(self):
        state = line("kOne", "208151234567890", "8933012345678901234")
        state.update({
            "product": "iPhone18,4", "hardware": "D23AP",
            "bundle_identifier": "com.apple.Free_fr", "bundle_version": "73.0",
        })
        catalog = carrier_app.load_catalog()
        family = carrier_app.matching_family(catalog, state)
        profiles = carrier_app.profiles_for(catalog, family, state)
        fallback = carrier_app.compatible_original_profile(profiles, state)
        self.assertIsNotNone(fallback)
        with tempfile.TemporaryDirectory() as temp, mock.patch(
            "carrier_app.state_root", return_value=Path(temp)
        ):
            original = carrier_app.remember_baseline(state)
            package = carrier_app.prepare_package(fallback, state)
            carrier_app.save_rollback_package(state, original, fallback, package)
            cached = carrier_app.cached_rollback_package(state, original)
            self.assertIsNotNone(cached)
            self.assertEqual(cached[0]["identifier"], "com.apple.Free_fr")
            self.assertEqual(cached[0]["version"], "73.0")

    @unittest.skipIf(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                     "POSIX permissions are not enforced here")
    def test_locked_package_cache_falls_back_and_keeps_rollback(self):
        # Windows WinError 5: a cache folder left by an elevated run can no longer be written.
        state = line("kOne", "208151234567890", "8933012345678901234")
        state.update({
            "product": "iPhone18,4", "hardware": "D23AP",
            "bundle_identifier": "com.apple.Free_fr", "bundle_version": "73.0",
        })
        catalog = carrier_app.load_catalog()
        family = carrier_app.matching_family(catalog, state)
        profiles = carrier_app.profiles_for(catalog, family, state)
        fallback = carrier_app.compatible_original_profile(profiles, state)
        with tempfile.TemporaryDirectory() as temp, mock.patch(
            "carrier_app.state_root", return_value=Path(temp)
        ):
            locked = Path(temp) / "packages"
            locked.mkdir()
            os.chmod(locked, 0o500)
            try:
                original = carrier_app.remember_baseline(state)
                package = carrier_app.prepare_package(fallback, state)
                self.assertEqual(package.relative_to(Path(temp)).parts[0], "packages-2")
                carrier_app.save_rollback_package(state, original, fallback, package)
                cached = carrier_app.cached_rollback_package(state, original)
                self.assertIsNotNone(cached)
                # The second preparation reuses the fallback copy.
                self.assertEqual(carrier_app.prepare_package(fallback, state), package)
            finally:
                os.chmod(locked, 0o700)

    @unittest.skipIf(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                     "POSIX permissions are not enforced here")
    def test_locked_profile_folder_inside_cache_falls_back(self):
        state = line("kOne", "208151234567890", "8933012345678901234")
        state.update({
            "product": "iPhone18,4", "hardware": "D23AP",
            "bundle_identifier": "com.apple.Free_fr", "bundle_version": "73.0",
        })
        catalog = carrier_app.load_catalog()
        family = carrier_app.matching_family(catalog, state)
        profile = carrier_app.compatible_original_profile(
            carrier_app.profiles_for(catalog, family, state), state)
        with tempfile.TemporaryDirectory() as temp, mock.patch(
            "carrier_app.state_root", return_value=Path(temp)
        ):
            first = carrier_app.prepare_package(profile, state)
            shutil.rmtree(first)
            os.chmod(first.parent, 0o500)
            try:
                second = carrier_app.prepare_package(profile, state)
                self.assertEqual(second.relative_to(Path(temp)).parts[0], "packages-2")
                self.assertTrue((second / "manifest.json").is_file())
            finally:
                os.chmod(first.parent, 0o700)

    def test_cache_cleanup_refuses_paths_outside_cache(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch(
            "carrier_app.state_root", return_value=Path(temp)
        ):
            for bad in (Path(temp) / "baselines" / "x" / "y", Path(temp) / "packages",
                        Path(temp).parent / "packages" / "a" / "b"):
                with self.assertRaises(carrier_app.BuildError):
                    carrier_app._remove_prepared_dir(bad)

    def test_windows_folders_get_no_owner_only_acl(self):
        # Lenovo-PC, WinError 5: Python 3.13+ turns mode=0o700 into an owner-only ACL on Windows.
        from tools import build_selector_ipcc
        calls = []
        x, y = Path("x"), Path("y")   # created before os.name is faked (pathlib checks it on Linux)
        with mock.patch.object(build_selector_ipcc.os, "name", "nt"), \
                mock.patch.object(Path, "mkdir", lambda self, *a, **k: calls.append(k)):
            build_selector_ipcc.make_private_dir(x)
            build_selector_ipcc.make_private_dir(y, exist_ok=False)
        self.assertEqual(calls, [{"parents": True, "exist_ok": True}, {"parents": True, "exist_ok": False}])
        calls.clear()
        with mock.patch.object(build_selector_ipcc.os, "name", "posix"), \
                mock.patch.object(Path, "mkdir", lambda self, *a, **k: calls.append(k)):
            build_selector_ipcc.make_private_dir(x)
        self.assertEqual(calls, [{"parents": True, "exist_ok": True, "mode": 0o700}])

    def test_logs_are_written_into_the_program_folder(self):
        with tempfile.TemporaryDirectory() as program, tempfile.TemporaryDirectory() as data, \
                mock.patch("carrier_app.program_root", return_value=Path(program)), \
                mock.patch("carrier_app.state_root", return_value=Path(data)):
            carrier_app.log("probe_event", note="x")
            self.assertEqual(carrier_app.log_path().parent, Path(program) / "logs")
            self.assertIn("probe_event", carrier_app.log_path().read_text(encoding="utf-8"))
            self.assertFalse((Path(data) / "logs").exists())

    @unittest.skipIf(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                     "POSIX permissions are not enforced here")
    def test_read_only_program_folder_falls_back_to_data_folder(self):
        with tempfile.TemporaryDirectory() as program, tempfile.TemporaryDirectory() as data, \
                mock.patch("carrier_app.program_root", return_value=Path(program)), \
                mock.patch("carrier_app.state_root", return_value=Path(data)):
            os.chmod(program, 0o500)
            try:
                carrier_app.log("probe_event")
                self.assertEqual(carrier_app.log_path().parent, Path(data) / "logs")
                self.assertTrue(carrier_app.log_path().is_file())
            finally:
                os.chmod(program, 0o700)

    def test_program_root_for_each_layout(self):
        program_root = _LOG_SANDBOX[1].temp_original   # the real function, not the sandbox
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "app").mkdir()
            (root / "CarrierSelector.cmd").write_text("@echo off\n")
            with mock.patch("carrier_app.__file__", str(root / "app" / "carrier_app.py")):
                self.assertEqual(program_root(), root.resolve())   # Windows
            with mock.patch("carrier_app.__file__", str(root / "carrier_app.py")):
                self.assertEqual(program_root(), root.resolve())   # Python
            with mock.patch.object(carrier_app.sys, "frozen", True, create=True), \
                    mock.patch.object(carrier_app.sys, "executable", str(root / "CarrierSelector")):
                self.assertEqual(program_root(), root.resolve())   # macOS/Linux

    def test_existing_baseline_survives_later_profile_changes(self):
        state = line("kOne", "250011234567890", "8970101234567890123")
        with tempfile.TemporaryDirectory() as temp, mock.patch(
            "carrier_app.state_root", return_value=Path(temp)
        ):
            original = carrier_app.remember_baseline(state)
            changed = dict(state, bundle_identifier="com.apple.MTN_cy", bundle_version="72.0")
            self.assertEqual(carrier_app.remember_baseline(changed), original)
            self.assertEqual(carrier_app.load_baseline(changed), original)

    def test_invalid_saved_baseline_is_repaired_from_current_valid_profile(self):
        state = line("kOne", "250011234567890", "8970101234567890123")
        state["version"] = "27.0.1"
        with tempfile.TemporaryDirectory() as temp, mock.patch(
            "carrier_app.state_root", return_value=Path(temp)
        ):
            baseline = carrier_app.baseline_path(state)
            baseline.parent.mkdir(parents=True)
            baseline.write_text(json.dumps({
                "selector_sha256": carrier_app.selector_digest(state),
                "original": {"bundle_identifier": "", "bundle_version": ""},
            }), encoding="utf-8")
            expected = {"bundle_identifier": "com.apple.MTS_ru", "bundle_version": "72.0"}
            self.assertEqual(carrier_app.remember_baseline(state), expected)
            self.assertEqual(carrier_app.load_baseline(state), expected)

    def test_unchanged_original_is_not_rewritten_and_keeps_rollback(self):
        # v6.0 stored a whole state snapshot as the original; the next run then
        # "updated" MTS 72.0 to MTS 72.0 and lost the prepared rollback package.
        state = line("kOne", "250011234567890", "8970101234567890123")
        state["version"] = "27.0.1"
        with tempfile.TemporaryDirectory() as temp, mock.patch(
            "carrier_app.state_root", return_value=Path(temp)
        ):
            baseline = carrier_app.baseline_path(state)
            baseline.parent.mkdir(parents=True)
            baseline.write_text(json.dumps({
                "selector_sha256": carrier_app.selector_digest(state),
                "original": carrier_app.safe_state(state), "rollback": {"package": "packages/x"},
            }), encoding="utf-8")
            receipt = {"before": [carrier_app.safe_state(state)], "desired": [{"slot": "kOne"}],
                       "selector_hashes": [carrier_app.selector_digest(state)]}
            (Path(temp) / "a-install-result.json").write_text(json.dumps(receipt), encoding="utf-8")
            before = baseline.read_bytes()
            with mock.patch("carrier_app.log") as logged:
                original = carrier_app.remember_baseline(
                    dict(state, bundle_identifier="com.apple.Vodafone_ro", bundle_version="73.0"))
            self.assertEqual((original["bundle_identifier"], original["bundle_version"]),
                             ("com.apple.MTS_ru", "72.0"))
            self.assertEqual(baseline.read_bytes(), before)
            logged.assert_not_called()

    def test_unknown_future_model_still_gets_global_catalog(self):
        catalog = carrier_app.load_catalog()
        overview = {"product": "iPhone99,1", "hardware": "X99AP"}
        profiles = carrier_app.profiles_for(catalog, None, overview)
        identifiers = {row["identifier"] for row in profiles}
        self.assertGreaterEqual(len(profiles), 700)
        self.assertIn("com.apple.Vodafone_ro", identifiers)
        self.assertIn("com.apple.Free_fr", identifiers)
        self.assertIn("com.apple.MTS_ru", identifiers)

    def test_foreign_sim_requires_separate_confirmation(self):
        foreign = line("kOne", "208151234567890", "8933012345678901234")
        foreign.update({"mcc": "208", "mnc": "15"})
        plan = [(foreign, {"label": "MTN_cy 72.0", "legacy": False}, Path("unused"))]
        with mock.patch("builtins.input", return_value="INSTALL"), \
                mock.patch("carrier_app.asyncio.run") as run:
            self.assertEqual(carrier_app.confirm_plan(
                plan, "INSTALL", remember_original=True, operation="test"
            ), 0)
            run.assert_not_called()

    def test_foreign_sim_can_be_confirmed_explicitly(self):
        foreign = line("kOne", "208151234567890", "8933012345678901234")
        foreign.update({"mcc": "208", "mnc": "15"})
        plan = [(foreign, {"label": "MTN_cy 72.0", "legacy": False}, Path("unused"))]
        def finish(coroutine):
            coroutine.close()
            return 7

        with mock.patch("builtins.input", side_effect=["FOREIGN", "INSTALL"]), \
                mock.patch("carrier_app.asyncio.run", side_effect=finish) as run:
            self.assertEqual(carrier_app.confirm_plan(
                plan, "INSTALL", remember_original=True, operation="test"
            ), 7)
            run.assert_called_once()

    def test_native_profile_follows_phone_ios_version(self):
        iphone_15_pro = {"product": "iPhone16,1", "hardware": "D83AP"}
        iphone_17_pro_max = {"product": "iPhone18,2", "hardware": "V54AP"}
        cases = [
            (iphone_15_pro, "27.0.1", "com.apple.MTS_ru", "72.0"),
            (iphone_15_pro, "27.0", "com.apple.MTS_ru", "72.0"),
            (iphone_15_pro, "27.1", "com.apple.MTS_ru", "72.0"),
            (iphone_15_pro, "27.2", "com.apple.MTS_ru", "73.0"),
            (iphone_15_pro, "27.2.1", "com.apple.MTS_ru", "73.0"),
            (iphone_15_pro, "27.10", "com.apple.MTS_ru", "73.0"),
            (iphone_15_pro, "28.0", "com.apple.MegaFon_ru", "73.0"),
            (iphone_17_pro_max, "27.2.1", "com.apple.VimpelCom_ru", "73.0"),
            (iphone_15_pro, "26.4", "com.apple.MTS_ru", None),  # older than every set: unknown
            (iphone_15_pro, "", "com.apple.MTS_ru", None),
            (iphone_15_pro, "27.2.1", "com.apple.NoSuchCarrier", None),
            ({"product": "iPhone99,1", "hardware": "X99AP"}, "27.2.1", "com.apple.MTS_ru", None),
        ]
        for model, ios, identifier, expected in cases:
            with self.subTest(model=model["product"], ios=ios, identifier=identifier):
                state = dict(model, version=ios)
                self.assertEqual(carrier_app.native_firmware_version(state, identifier), expected)

    def test_stale_receipt_is_raised_to_native_profile_of_running_ios(self):
        # Real case: first install was recorded on iOS 27.0.1 (MTS 72.0); the phone
        # now runs iOS 27.2.1 whose built-in MTS profile is 73.0.
        state = line("kOne", "250011234567890", "8970101234567890123")
        state.update(version="27.2.1", bundle_identifier="com.apple.Vodafone_ro", bundle_version="73.0")
        with tempfile.TemporaryDirectory() as temp, mock.patch(
            "carrier_app.state_root", return_value=Path(temp)
        ):
            receipt = {
                "started_utc": "2026-11-12T10:00:00+00:00",
                "before": [dict(state, bundle_identifier="com.apple.MTS_ru", bundle_version="72.0", version="27.0.1")],
                "desired": [{"slot": "kOne", "bundle_identifier": "com.apple.Vodafone_ro", "bundle_version": "73.0"}],
                "selector_hashes": [carrier_app.selector_digest(state)],
            }
            (Path(temp) / "a-install-result.json").write_text(json.dumps(receipt), encoding="utf-8")
            expected = {"bundle_identifier": "com.apple.MTS_ru", "bundle_version": "73.0"}
            self.assertEqual(carrier_app.remember_baseline(state), expected)
            self.assertEqual(carrier_app.load_baseline(state), expected)
            # The same SIM on iOS 27.0.1 keeps 72.0.
            older = dict(state, version="27.0.1")
            with mock.patch("carrier_app.load_baseline", return_value=None):
                self.assertEqual(carrier_app.remember_baseline(older)["bundle_version"], "72.0")

    def test_overwritten_baseline_is_healed_to_native_version(self):
        # iPhone 17 Pro Max case: the saved original was lowered to 72.0 by an old receipt.
        state = line("kTwo", "250991234567890", "8970101234567890999")
        state.update(product="iPhone18,2", hardware="V54AP", version="27.2.1",
                     bundle_identifier="com.apple.VimpelCom_ru", bundle_version="72.0")
        with tempfile.TemporaryDirectory() as temp, mock.patch(
            "carrier_app.state_root", return_value=Path(temp)
        ):
            baseline = carrier_app.baseline_path(state)
            baseline.parent.mkdir(parents=True)
            baseline.write_text(json.dumps({
                "selector_sha256": carrier_app.selector_digest(state),
                "original": {"bundle_identifier": "com.apple.VimpelCom_ru", "bundle_version": "72.0"},
            }), encoding="utf-8")
            with mock.patch("carrier_app.log") as logged:
                original = carrier_app.remember_baseline(state)
            self.assertEqual(original, {"bundle_identifier": "com.apple.VimpelCom_ru", "bundle_version": "73.0"})
            self.assertEqual(carrier_app.load_baseline(state), original)
            logged.assert_any_call("baseline_updated", old="com.apple.VimpelCom_ru 72.0",
                                   new="com.apple.VimpelCom_ru 73.0")

    def test_native_profile_never_lowers_or_changes_carrier(self):
        state = line("kOne", "250011234567890", "8970101234567890123")
        state.update(version="27.0.1", bundle_identifier="com.apple.Telia_dk", bundle_version="68.0")
        with tempfile.TemporaryDirectory() as temp, mock.patch(
            "carrier_app.state_root", return_value=Path(temp)
        ):
            baseline = carrier_app.baseline_path(state)
            baseline.parent.mkdir(parents=True)
            saved = {"bundle_identifier": "com.apple.MTS_ru", "bundle_version": "73.0"}
            baseline.write_text(json.dumps({
                "selector_sha256": carrier_app.selector_digest(state), "original": saved,
            }), encoding="utf-8")
            # iOS 27.0.1 ships 72.0, but a higher recorded original is kept; the
            # foreign profile active now (Telia) never replaces the carrier.
            self.assertEqual(carrier_app.remember_baseline(state), saved)

    def test_receipt_recovery_is_bound_to_exact_sim_not_slot(self):
        state = line("kOne", "250011234567890", "8970101234567890123")
        other = line("kOne", "250991234567890", "8979901234567890123")
        with tempfile.TemporaryDirectory() as temp, mock.patch(
            "carrier_app.state_root", return_value=Path(temp)
        ):
            receipt = {
                "before": [dict(other, bundle_identifier="com.apple.Wrong_ru", bundle_version="73.0"),
                           dict(state, bundle_identifier="com.apple.VimpelCom_ru", bundle_version="73.0")],
                "desired": [{"slot": "kOne"}, {"slot": "kOne"}],
                "selector_hashes": [carrier_app.selector_digest(other), carrier_app.selector_digest(state)],
            }
            (Path(temp) / "old-install-result.json").write_text(json.dumps(receipt), encoding="utf-8")
            self.assertEqual(carrier_app.recover_legacy_baseline(state), {
                "bundle_identifier": "com.apple.VimpelCom_ru", "bundle_version": "73.0",
            })

    def test_exact_receipt_repairs_wrong_existing_baseline(self):
        state = line("kTwo", "250021234567890", "8970101234567890124")
        with tempfile.TemporaryDirectory() as temp, mock.patch(
            "carrier_app.state_root", return_value=Path(temp)
        ):
            baseline = carrier_app.baseline_path(state)
            baseline.parent.mkdir(parents=True)
            baseline.write_text(json.dumps({
                "selector_sha256": carrier_app.selector_digest(state),
                "original": {"bundle_identifier": "com.apple.MTS_ru", "bundle_version": "73.0"},
            }), encoding="utf-8")
            receipt = {
                "before": [dict(state, bundle_identifier="com.apple.VimpelCom_ru", bundle_version="73.0")],
                "desired": [{"slot": "kTwo"}],
                "selector_hashes": [carrier_app.selector_digest(state)],
            }
            (Path(temp) / "new-install-result.json").write_text(json.dumps(receipt), encoding="utf-8")
            self.assertEqual(carrier_app.remember_baseline(state), {
                "bundle_identifier": "com.apple.VimpelCom_ru", "bundle_version": "73.0",
            })

    def test_legacy_receipt_is_linked_through_exact_sim_manifest(self):
        state = line("kOne", "250011234567890", "8970101234567890123")
        with tempfile.TemporaryDirectory() as temp, mock.patch(
            "carrier_app.state_root", return_value=Path(temp)
        ):
            package = Path(temp) / "packages" / "telia" / carrier_app.selector_digest(state)[:16]
            package.mkdir(parents=True)
            (package / "manifest.json").write_text(json.dumps({
                "selector_sha256": carrier_app.selector_digest(state),
                "prepared_from": {"product": state["product"], "hardware": state["hardware"], "slot": "kOne"},
                "packages": [{"role": "forward", "bundle_identifier": "com.apple.Telia_dk",
                              "bundle_version": "68.0"}],
            }), encoding="utf-8")
            receipt = {
                "operation": "carrier-install",
                "started_utc": datetime.now(timezone.utc).isoformat(),
                "before": [dict(state, bundle_identifier="com.apple.MTS_ru", bundle_version="72.0")],
                "desired": [{"slot": "kOne", "bundle_identifier": "com.apple.Telia_dk",
                             "bundle_version": "68.0"}],
            }
            (Path(temp) / "legacy-install-result.json").write_text(json.dumps(receipt), encoding="utf-8")
            self.assertEqual(carrier_app.recover_legacy_baseline(state), {
                "bundle_identifier": "com.apple.MTS_ru", "bundle_version": "72.0",
            })

    def test_restore_receipt_cannot_redefine_original(self):
        state = line("kOne", "250011234567890", "8970101234567890123")
        with tempfile.TemporaryDirectory() as temp, mock.patch(
            "carrier_app.state_root", return_value=Path(temp)
        ):
            receipt = {
                "operation": "carrier-baseline-restore",
                "before": [dict(state, bundle_identifier="com.apple.Telia_dk", bundle_version="68.0")],
                "desired": [{"slot": "kOne", "bundle_identifier": "com.apple.Vodafone_ro",
                             "bundle_version": "72.0"}],
                "selector_hashes": [carrier_app.selector_digest(state)],
            }
            (Path(temp) / "bad-install-result.json").write_text(json.dumps(receipt), encoding="utf-8")
            self.assertIsNone(carrier_app.recover_legacy_baseline(state))

    def test_restore_falls_back_to_latest_signed_same_carrier_version(self):
        profiles = [
            {"identifier": "com.apple.VimpelCom_ru", "version": "70.0", "label": "old"},
            {"identifier": "com.apple.VimpelCom_ru", "version": "72.0", "label": "best"},
            {"identifier": "com.apple.MTS_ru", "version": "73.0", "label": "other"},
        ]
        selected = carrier_app.compatible_original_profile(profiles, {
            "bundle_identifier": "com.apple.VimpelCom_ru", "bundle_version": "73.0",
        })
        self.assertEqual(selected["label"], "best")

    def test_apn_help_covers_apple_profiles_and_major_mvnos(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            carrier_app.show_apn_help()
        text = output.getvalue()
        for operator in (
            "МТС", "МегаФон", "Yota", "Билайн", "T2 / Tele2", "СберМобайл",
            "Ростелеком Мобайл", "Т-Банк Мобайл", "ВТБ Мобайл", "Газпромбанк Мобайл",
        ):
            self.assertIn(operator, text)
        self.assertIn("internet.mts.ru", text)
        self.assertIn("internet.beeline.ru", text)

    def test_apn_help_belarus_matches_apple_bundles(self):
        """v6.2: Belarus values in item 6 are exactly those in Apple's own bundles."""
        text = carrier_app.APN_HELP_RU
        self.assertIn("APN и MMS операторов России и Беларуси", text)
        root = Path(carrier_app.bundle_path("ios2721-v53/MTS_by.bundle")).parent
        for name, title in (("MTS_by", "Беларусь — МТС BY"), ("mobilkom_by", "Беларусь — A1"),
                            ("life_by", "Беларусь — life:)")):
            self.assertIn(title, text)
            section = text.split(title, 1)[1].split("\n\n", 1)[0]
            data = plistlib.loads((root / f"{name}.bundle" / "carrier.plist").read_bytes())
            internet = next(a for a in data["apns"] if int(a.get("type-mask", 0)) & 1)
            mms = next(a for a in data["apns"] if int(a.get("type-mask", 0)) & 4)
            first, second = section.split("\n  MMS:", 1)
            self.assertIn(f"Режим модема: {internet['apn']} ", first)
            self.assertIn(f"APN {mms['apn']} ", second)
            if internet.get("username"):
                self.assertIn(f"пользователь {internet['username']} · пароль {internet['password']}", first)
            else:
                self.assertIn("имя и пароль пустые", first)
            self.assertIn(f"MMSC {data['MMS']['MMSC']} ", second)
            self.assertIn(f"прокси {data['MMS']['Proxy']} ", second)
            self.assertIn(f"размер {data['MMS']['MaxMessageSize']}", second)

    def test_mts_telia_hint(self):
        """v6.2: on a model without Telia_dk 68.0 an MTS user searching Telia is
        pointed to Vodafone_ro; elsewhere, or for another operator, nothing is said."""
        ro = {"identifier": "com.apple.Vodafone_ro", "version": "73.0"}
        t73 = {"identifier": "com.apple.Telia_dk", "version": "73.0"}
        t68 = {"identifier": "com.apple.Telia_dk", "version": "68.0"}
        mts = {"mcc": "250", "mnc": "01"}
        hint = carrier_app.mts_telia_hint([t73, ro], mts, "Telia dk")
        self.assertIn("Telia_dk 68.0 для этой модели нет", hint)
        self.assertIn("Vodafone_ro 73.0", hint)
        self.assertNotIn("Vodafone_ro", carrier_app.mts_telia_hint([t73], mts, "telia"))
        self.assertEqual(carrier_app.mts_telia_hint([t68, t73, ro], mts, "Telia"), "")
        self.assertEqual(carrier_app.mts_telia_hint([t73, ro], {"mcc": "250", "mnc": "99"}, "Telia"), "")
        self.assertEqual(carrier_app.mts_telia_hint([t73, ro], {"mcc": "257", "mnc": "02"}, "Telia"), "")
        self.assertEqual(carrier_app.mts_telia_hint([t73, ro], mts, "MTN"), "")
        self.assertEqual(carrier_app.mts_telia_hint([t73, ro], mts, ""), "")
        self.assertTrue(carrier_app.mts_telia_hint([t73, ro], {"mcc": "250", "mnc": "1"}, "telia"))

    def test_windows_cmd_is_ascii_only(self):
        command = Path(__file__).resolve().parents[1] / "windows_assets" / "CarrierSelector.cmd"
        command.read_bytes().decode("ascii")

    @unittest.skipIf(os.name == "nt", "XDG directories exist only on Linux")
    def test_linux_state_uses_xdg_directory(self):
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch("carrier_app.os.name", "posix"), \
                mock.patch("carrier_app.sys.platform", "linux"), \
                mock.patch.dict("carrier_app.os.environ", {"XDG_STATE_HOME": temp}, clear=False):
            self.assertEqual(carrier_app.state_root(), Path(temp) / "CarrierSelector")


class DualLineExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_pretransfer_failure_on_first_line_leaves_no_one_shot_marker(self):
        first = line("kOne", "250011234567890", "8970101234567890123")
        manifest = {"selector_sha256": carrier_app.selector_digest(first)}
        entry = {
            "bundle_identifier": "com.apple.Telia_dk", "bundle_version": "68.0",
            "sha256": "1" * 64,
        }
        plan = [(first, {"label": "Telia_dk 68.0"}, Path("one"))]
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch("carrier_app.state_root", return_value=Path(temp)), \
                mock.patch("carrier_app.read_states", mock.AsyncMock(return_value={"lines": [first]})), \
                mock.patch("carrier_app.load_manifest", return_value=manifest), \
                mock.patch("carrier_app.inspect_package", return_value=b"first"), \
                mock.patch("carrier_app.package_entry_for", return_value=entry), \
                mock.patch("carrier_app.remember_baseline"), \
                mock.patch("carrier_app.send_once", mock.AsyncMock(
                    side_effect=carrier_app.PreTransferConnectionError("not sent")
                )), \
                mock.patch("carrier_app.restart_once", mock.AsyncMock()) as restarted:
            # v5.6: nothing reached the phone, so this is a clean "not installed"
            # result instead of an exception; no marker and no restart.
            self.assertEqual(await carrier_app.execute_plan(plan), 2)
            self.assertEqual(list(Path(temp).glob(".*-install.once")), [])
            restarted.assert_not_awaited()

    async def test_missing_phone_before_restart_requests_manual_restart_without_resend(self):
        first = line("kOne", "250011234567890", "8970101234567890123")
        manifest = {"selector_sha256": carrier_app.selector_digest(first)}
        entry = {
            "bundle_identifier": "com.apple.Telia_dk", "bundle_version": "68.0",
            "sha256": "1" * 64,
        }
        plan = [(first, {"label": "Telia_dk 68.0"}, Path("one"))]
        sent = mock.AsyncMock()
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch("carrier_app.state_root", return_value=Path(temp)), \
                mock.patch("carrier_app.read_states", mock.AsyncMock(return_value={"lines": [first]})), \
                mock.patch("carrier_app.load_manifest", return_value=manifest), \
                mock.patch("carrier_app.inspect_package", return_value=b"first"), \
                mock.patch("carrier_app.package_entry_for", return_value=entry), \
                mock.patch("carrier_app.remember_baseline"), \
                mock.patch("carrier_app.send_once", sent), \
                mock.patch("carrier_app.restart_once", mock.AsyncMock(
                    side_effect=carrier_app.PreTransferConnectionError("no restart")
                )), \
                mock.patch("carrier_app.wait_for_plan", mock.AsyncMock()) as verified:
            self.assertEqual(await carrier_app.execute_plan(plan), 3)
            sent.assert_awaited_once_with(b"first", expected_serial=None)
            verified.assert_not_awaited()
            self.assertEqual(len(list(Path(temp).glob(".*-install.once"))), 1)

    async def test_two_slotless_rows_are_matched_by_exact_sim_not_row_position(self):
        first = line("", "250011234567890", "8970101234567890123")
        second = line("", "250021234567890", "8970101234567890124")
        manifests = [
            {"selector_sha256": carrier_app.selector_digest(first)},
            {"selector_sha256": carrier_app.selector_digest(second)},
        ]
        entries = [
            {"bundle_identifier": "com.apple.Telia_dk", "bundle_version": "68.0", "sha256": "1" * 64},
            {"bundle_identifier": "com.apple.MegaFon_ru", "bundle_version": "72.0", "sha256": "2" * 64},
        ]
        plan = [
            (first, {"label": "Telia_dk 68.0"}, Path("one")),
            (second, {"label": "MegaFon_ru 72.0"}, Path("two")),
        ]
        after = {
            manifests[0]["selector_sha256"]: dict(first, **entries[0]),
            manifests[1]["selector_sha256"]: dict(second, **entries[1]),
        }
        sent = mock.AsyncMock()
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch("carrier_app.state_root", return_value=Path(temp)), \
                mock.patch("carrier_app.read_states", mock.AsyncMock(return_value={"lines": [second, first]})), \
                mock.patch("carrier_app.load_manifest", side_effect=manifests), \
                mock.patch("carrier_app.inspect_package", side_effect=[b"first", b"second"]), \
                mock.patch("carrier_app.package_entry_for", side_effect=entries), \
                mock.patch("carrier_app.remember_baseline"), \
                mock.patch("carrier_app.wait_for_selected_lines", mock.AsyncMock(return_value=True)), \
                mock.patch("carrier_app.send_once", sent), \
                mock.patch("carrier_app.restart_once", mock.AsyncMock()), \
                mock.patch("carrier_app.wait_for_plan", mock.AsyncMock(return_value=(after, after))), \
                mock.patch("carrier_app.wait_for_reboot", mock.AsyncMock(return_value=True)):
            self.assertEqual(await carrier_app.execute_plan(plan), 0)
        sent.assert_has_awaits([
            mock.call(b"first", expected_serial=None),
            mock.call(b"second", expected_serial=None),
        ])

    async def test_two_lines_are_sent_once_each_and_restarted_once(self):
        first = line("kOne", "250011234567890", "8970101234567890123")
        second = line("kTwo", "250021234567890", "8970101234567890124")
        manifests = [
            {"selector_sha256": carrier_app.selector_digest(first)},
            {"selector_sha256": carrier_app.selector_digest(second)},
        ]
        entries = [
            {"bundle_identifier": "com.apple.Telia_dk", "bundle_version": "72.0", "sha256": "1" * 64},
            {"bundle_identifier": "com.apple.MegaFon_ru", "bundle_version": "72.0", "sha256": "2" * 64},
        ]
        plan = [
            (first, {"label": "Telia_dk 72.0"}, Path("one")),
            (second, {"label": "MegaFon_ru 72.0"}, Path("two")),
        ]
        after = {
            manifests[0]["selector_sha256"]: dict(first, **entries[0]),
            manifests[1]["selector_sha256"]: dict(second, **entries[1]),
        }
        sent = mock.AsyncMock()
        restarted = mock.AsyncMock()
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch("carrier_app.state_root", return_value=Path(temp)), \
                mock.patch("carrier_app.read_states", mock.AsyncMock(return_value={"lines": [first, second]})), \
                mock.patch("carrier_app.load_manifest", side_effect=manifests), \
                mock.patch("carrier_app.inspect_package", side_effect=[b"first", b"second"]), \
                mock.patch("carrier_app.package_entry_for", side_effect=entries), \
                mock.patch("carrier_app.remember_baseline"), \
                mock.patch("carrier_app.wait_for_selected_lines", mock.AsyncMock(return_value=True)), \
                mock.patch("carrier_app.send_once", sent), \
                mock.patch("carrier_app.restart_once", restarted), \
                mock.patch("carrier_app.wait_for_plan", mock.AsyncMock(return_value=(after, after))), \
                mock.patch("carrier_app.wait_for_reboot", mock.AsyncMock(return_value=True)):
            result = await carrier_app.execute_plan(plan)
        self.assertEqual(result, 0)
        self.assertEqual(sent.await_count, 2)
        sent.assert_has_awaits([
            mock.call(b"first", expected_serial=None),
            mock.call(b"second", expected_serial=None),
        ])
        restarted.assert_awaited_once_with(expected_serial=None)

    async def test_second_line_disconnect_is_not_resent_and_is_verified_after_restart(self):
        first = line("kOne", "250011234567890", "8970101234567890123")
        second = line("kTwo", "250021234567890", "8970101234567890124")
        manifests = [
            {"selector_sha256": carrier_app.selector_digest(first)},
            {"selector_sha256": carrier_app.selector_digest(second)},
        ]
        entries = [
            {"bundle_identifier": "com.apple.MTN_cy", "bundle_version": "72.0", "sha256": "1" * 64},
            {"bundle_identifier": "com.apple.Telia_dk", "bundle_version": "68.0", "sha256": "2" * 64},
        ]
        plan = [
            (first, {"label": "MTN_cy 72.0"}, Path("one")),
            (second, {"label": "Telia_dk 68.0"}, Path("two")),
        ]
        after = {
            manifests[0]["selector_sha256"]: dict(first, **entries[0]),
            manifests[1]["selector_sha256"]: dict(second, **entries[1]),
        }
        sent = mock.AsyncMock(side_effect=[None, ConnectionTerminatedError("closed")])
        restarted = mock.AsyncMock()
        ready = mock.AsyncMock(return_value=True)
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch("carrier_app.state_root", return_value=Path(temp)), \
                mock.patch("carrier_app.read_states", mock.AsyncMock(return_value={"lines": [first, second]})), \
                mock.patch("carrier_app.load_manifest", side_effect=manifests), \
                mock.patch("carrier_app.inspect_package", side_effect=[b"first", b"second"]), \
                mock.patch("carrier_app.package_entry_for", side_effect=entries), \
                mock.patch("carrier_app.remember_baseline"), \
                mock.patch("carrier_app.wait_for_selected_lines", ready), \
                mock.patch("carrier_app.send_once", sent), \
                mock.patch("carrier_app.restart_once", restarted), \
                mock.patch("carrier_app.wait_for_plan", mock.AsyncMock(return_value=(after, after))), \
                mock.patch("carrier_app.wait_for_reboot", mock.AsyncMock(return_value=True)):
            result = await carrier_app.execute_plan(plan)
        self.assertEqual(result, 0)
        self.assertEqual(sent.await_count, 2)
        sent.assert_has_awaits([
            mock.call(b"first", expected_serial=None),
            mock.call(b"second", expected_serial=None),
        ])
        self.assertEqual(ready.await_count, 2)
        restarted.assert_awaited_once_with(expected_serial=None)


class ResetHelpers(unittest.TestCase):
    """v6.2 item 3 helpers: the SIM's own iOS profile and the CommCenter answer."""

    def test_native_profile_by_mcc_mnc(self):
        catalog = carrier_app.load_catalog()
        cases = {("250", "01"): "com.apple.MTS_ru", ("250", "1"): "com.apple.MTS_ru",
                 ("250", "02"): "com.apple.MegaFon_ru", ("250", "99"): "com.apple.VimpelCom_ru",
                 ("250", "20"): "com.apple.Tele2_ru", ("250", "11"): "com.apple.MegaFon_Yota_ru",
                 ("257", "01"): "com.apple.mobilkom_by", ("257", "02"): "com.apple.MTS_by",
                 ("257", "04"): "com.apple.life_by"}
        for family in catalog["families"]:
            line = {"product": family["products"][0], "hardware": family["hardwares"][0]}
            for (mcc, mnc), identifier in cases.items():
                found = carrier_app.native_profile(catalog, dict(line, mcc=mcc, mnc=mnc))
                self.assertIsNotNone(found, (family["name"], mcc, mnc))
                self.assertEqual(found["identifier"], identifier, (family["name"], mcc, mnc))
            self.assertIsNone(carrier_app.native_profile(catalog, dict(line, mcc="250", mnc="62")))
            self.assertIsNone(carrier_app.native_profile(catalog, dict(line, mcc="", mnc="")))

    def test_native_profile_comes_from_the_models_own_firmware(self):
        catalog = carrier_app.load_catalog()
        line = {"product": "iPhone16,1", "hardware": "D83AP", "mcc": "250", "mnc": "01"}
        self.assertEqual(carrier_app.native_profile(catalog, line)["path"], "ios2721-d83/MTS_ru.bundle")

    def test_commcenter_answer_is_read_for_this_sim_only(self):
        selector = "250010000000001_ID-8970100000000000011_ID-.bundle"
        other = "250020000000002_ID-8970200000000000022_ID-.bundle"
        evidence = {}
        note = carrier_app._note_reset_message
        note(f"Deleting Bundle /private/var/mobile/Library/Carrier Bundles/iPhone/{other}", selector, evidence)
        self.assertNotIn("removed", evidence)
        note(f"Deleting Bundle /private/var/mobile/Library/Carrier Bundles/iPhone/{selector}", selector, evidence)
        note("No symlinks available. Nothing to do.", selector, evidence)
        note('Reply dictionary after carrier bundle installation: {\n\t"kInstallResult": false\n}', selector, evidence)
        note('received result: {\n\t"kInstallResult": true\n}', selector, evidence)
        self.assertEqual(evidence, {"removed": True, "no_symlinks": True, "result": False})
        accepted = {}
        note('Reply dictionary after carrier bundle installation: { "kInstallResult": true }', selector, accepted)
        self.assertIs(accepted["result"], True)


if __name__ == "__main__":
    unittest.main()
