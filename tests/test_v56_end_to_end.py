"""End-to-end v5.6 scenarios on the offline iPhone simulator (tests/fake_phone.py).

Real code paths: SIM reading, menu input, global catalog, IPCC build with Apple
P-384 signature verification, one-shot markers, receipts, send, restart, verify.
Only the USB transport is simulated.  All SIM numbers here are fictitious.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import plistlib
import zipfile
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import carrier_app
from tools import build_selector_ipcc, install_prepared_pair, read_phone_carrier_state
from tests.fake_phone import FakePhone, Sim, IMSI, ICCID

# Fictitious identifiers (valid lengths, not real subscribers).
MTS = dict(imsi="250010000000001", iccid="8970100000000000011", mcc="250", mnc="01")
MEGAFON = dict(imsi="250020000000002", iccid="8970200000000000022", mcc="250", mnc="02")
MTS_OTHER = dict(imsi="250010000000003", iccid="8970100000000000033", mcc="250", mnc="01")


def sim(slot: str, ids: dict[str, str], bundle="com.apple.MTS_ru", version="73.0") -> Sim:
    return Sim(slot=slot, bundle=bundle, version=version, **ids)


def keys(ids: dict[str, str], suffix: str = "") -> dict[str, str]:
    return {f"{IMSI}{suffix}": ids["imsi"], f"{ICCID}{suffix}": ids["iccid"]}



_LOG_SANDBOX = None


def setUpModule():
    """Keep test logs out of the program folder (v6.1 writes logs next to the program)."""
    global _LOG_SANDBOX
    _LOG_SANDBOX = (tempfile.TemporaryDirectory(), mock.patch("carrier_app.program_root"))
    _LOG_SANDBOX[1].start().return_value = Path(_LOG_SANDBOX[0].name)


def tearDownModule():
    _LOG_SANDBOX[1].stop()
    _LOG_SANDBOX[0].cleanup()

class EndToEnd(unittest.TestCase):
    catalog: dict[str, object]

    @classmethod
    def setUpClass(cls):
        cls.catalog = carrier_app.load_catalog()

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.addCleanup(self._temp.cleanup)
        stack.enter_context(mock.patch("carrier_app.state_root", return_value=self.root))
        stack.enter_context(mock.patch("carrier_app.program_root", return_value=self.root))
        stack.enter_context(mock.patch.multiple(
            "carrier_app", POLL_INTERVAL=0.01, VERIFY_TIMEOUT=1.0,
            RECONNECT_TIMEOUT=1.0, AFTER_SEND_TIMEOUT=1.0, REBOOT_DOWN_TIMEOUT=0.3,
            RESET_LOG_WAIT=0.5, RESET_LOG_SETTLE=0,
        ))

    def run_action(self, action, phone: FakePhone, answers: list[str]) -> tuple[int, str]:
        output = io.StringIO()
        with phone.attached(), mock.patch("builtins.input", side_effect=answers), \
                contextlib.redirect_stdout(output):
            result = action(self.catalog)
        return result, output.getvalue()

    def install(self, phone, answers):
        return self.run_action(carrier_app.install_interactive, phone, answers)

    def markers(self):
        return list(self.root.glob(".*-install.once"))

    # ---- iPhone 18 Pro Max US (eSIM only): one active line reported as kTwo --

    def test_v64_single_kTwo_root_keys_belong_to_active_esim(self):
        """The layout v5.4 handled and v5.5 broke: the package must target the active eSIM."""
        phone = FakePhone(
            product="iPhone19,3", hardware="V64AP",
            sims=[sim("kTwo", MTS)], identifiers_in="keys",
            root_keys={**keys(MTS), **keys(MEGAFON, "2")},
        )
        result, out = self.install(phone, ["Swisscom", "1", "INSTALL"])
        self.assertEqual(result, 0, out)
        self.assertEqual(phone.orphans, [], "package was built for the wrong SIM")
        self.assertEqual(len(phone.sent_selectors), 1)
        self.assertEqual(phone.restarts, 1)
        self.assertEqual(phone.sims[0].bundle, "com.apple.Swisscom_ch")
        self.assertEqual(self.markers(), [])

    def test_v64_single_kTwo_suffix_keys_belong_to_active_esim(self):
        """The layout v5.5 was written for keeps working."""
        phone = FakePhone(
            product="iPhone19,3", hardware="V64AP",
            sims=[sim("kTwo", MEGAFON, "com.apple.MegaFon_ru", "73.0")], identifiers_in="keys",
            root_keys={**keys(MTS), **keys(MEGAFON, "2")},
        )
        result, out = self.install(phone, ["Swisscom", "1", "INSTALL"])
        self.assertEqual(result, 0, out)
        self.assertEqual(phone.orphans, [])
        self.assertEqual(phone.sims[0].bundle, "com.apple.Swisscom_ch")

    def test_v64_same_operator_ambiguity_uses_v54_order(self):
        phone = FakePhone(
            product="iPhone19,3", hardware="V64AP",
            sims=[sim("kTwo", MTS)], identifiers_in="keys",
            root_keys={**keys(MTS), **keys(MTS_OTHER, "2")},
        )
        result, out = self.install(phone, ["Swisscom", "1", "INSTALL"])
        self.assertEqual(result, 0, out)
        self.assertEqual(phone.orphans, [])

    # ---- two physical lines (iPhone 17 Pro Max, V54) ------------------------

    def dual_phone(self, **kwargs) -> FakePhone:
        return FakePhone(
            product="iPhone18,2", hardware="V54AP",
            sims=[sim("kOne", MTS), sim("kTwo", MEGAFON, "com.apple.MegaFon_ru", "73.0")], **kwargs,
        )

    def test_dual_install_sends_each_once_and_restarts_once(self):
        phone = self.dual_phone()
        result, out = self.install(phone, ["1", "MTN cy", "1", "Telia dk 68", "1", "INSTALL"])
        self.assertEqual(result, 0, out)
        self.assertEqual(len(phone.sent_selectors), 2)
        self.assertEqual(phone.restarts, 1)
        self.assertEqual([s.bundle for s in phone.sims], ["com.apple.MTN_cy", "com.apple.Telia_dk"])
        # The diagnostic log must not contain SIM numbers or the UDID.
        log_text = "".join(p.read_text(encoding="utf-8") for p in (self.root / "logs").glob("*.log"))
        self.assertIn('"event": "verify"', log_text)
        for secret in (MTS["imsi"], MTS["iccid"], MEGAFON["imsi"], MEGAFON["iccid"], phone.udid):
            self.assertNotIn(secret, log_text)
        # Restore brings back the original profiles of both SIMs.
        result, out = self.run_action(carrier_app.restore_interactive, phone, ["1", "RESTORE"])
        self.assertEqual(result, 0, out)
        self.assertEqual([s.bundle for s in phone.sims], ["com.apple.MTS_ru", "com.apple.MegaFon_ru"])
        self.assertEqual(phone.restarts, 2)

    def test_second_sim_explicitly_rejected_first_still_verified(self):
        phone = self.dual_phone(behaviours=["ok", "reject"])
        result, out = self.install(phone, ["1", "MTN cy", "1", "Telia dk 68", "1", "INSTALL"])
        self.assertEqual(result, 3, out)
        self.assertIn("iPhone отклонил пакет", out)
        self.assertEqual(phone.restarts, 1)
        self.assertEqual(phone.sims[0].bundle, "com.apple.MTN_cy")
        self.assertEqual(phone.sims[1].bundle, "com.apple.MegaFon_ru")
        self.assertEqual(self.markers(), [], "rejected package must not block a later attempt")

    def test_explicit_rejection_is_not_followed_by_a_reboot(self):
        phone = FakePhone(sims=[sim("kOne", MTS)], behaviours=["reject"])
        result, out = self.install(phone, ["Telia dk 68", "1", "INSTALL"])
        self.assertEqual(result, 2, out)
        self.assertEqual(phone.restarts, 0)
        self.assertEqual(self.markers(), [])
        self.assertIn("перезагрузка не нужна", out)

    def test_ambiguous_socket_close_is_never_resent(self):
        phone = FakePhone(sims=[sim("kOne", MTS)], behaviours=["drop"])
        result, out = self.install(phone, ["Telia dk 68", "1", "INSTALL"])
        self.assertEqual(result, 0, out)
        self.assertEqual(len(phone.sent_selectors), 1)
        self.assertEqual(phone.restarts, 1)
        self.assertEqual(phone.sims[0].bundle, "com.apple.Telia_dk")

    def test_phone_briefly_absent_before_send_is_waited_for(self):
        phone = FakePhone(sims=[sim("kOne", MTS)])
        original = phone.list_devices
        calls = {"n": 0}

        async def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 3:  # disappears between the final check and the send
                phone.absent_now = 2
            return await original(*args, **kwargs)

        phone.list_devices = flaky
        result, out = self.install(phone, ["Telia dk 68", "1", "INSTALL"])
        self.assertEqual(result, 0, out)
        self.assertEqual(len(phone.sent_selectors), 1)

    # ---- the v5.5 dead end: an unconfirmed attempt blocked the plan forever --

    def test_unconfirmed_attempt_can_be_retried_with_explicit_retry(self):
        phone = FakePhone(sims=[sim("kOne", MTS)], behaviours=["ignore"])
        result, out = self.install(phone, ["Telia dk 68", "1", "INSTALL"])
        self.assertEqual(result, 3, out)
        self.assertIn("НЕ ПОДТВЕРЖДЕНО", out)
        self.assertIn("сейчас MTS_ru 73.0", out)
        self.assertEqual(len(self.markers()), 1)

        # Declining keeps the phone untouched.
        result, out = self.install(phone, ["Telia dk 68", "1", "INSTALL", "Q"])
        self.assertEqual(result, 0, out)
        self.assertIn("такой же план уже запускался", out)
        self.assertIn("Отменено; телефон не изменён.", out)
        self.assertEqual(len(phone.sent_selectors), 1)

        # RETRY sends the same package once more; now iOS applies it.
        result, out = self.install(phone, ["Telia dk 68", "1", "INSTALL", "RETRY"])
        self.assertEqual(result, 0, out)
        self.assertEqual(len(phone.sent_selectors), 2)
        self.assertEqual(phone.sims[0].bundle, "com.apple.Telia_dk")
        self.assertEqual(self.markers(), [])
        receipts = list(self.root.glob("*-install-result.json"))
        self.assertEqual(len(receipts), 2, "the first receipt is kept as evidence")

    def test_marker_left_by_v54_or_v55_no_longer_dead_ends(self):
        phone = FakePhone(sims=[sim("kOne", MTS)], behaviours=["ignore"])
        self.install(phone, ["Telia dk 68", "1", "INSTALL"])
        marker = self.markers()[0]
        receipt = self.root / (marker.name[1:].replace("-install.once", "-install-result.json"))
        data = json.loads(receipt.read_text(encoding="utf-8"))
        data["phase"] = "partially_installed"  # what the 20:48 v5.4 run left behind
        receipt.write_text(json.dumps(data), encoding="utf-8")
        result, out = self.install(phone, ["Telia dk 68", "1", "INSTALL", "RETRY"])
        self.assertEqual(result, 0, out)
        self.assertIn("отправлена только часть пакетов", out)

    def test_corrupted_cached_package_is_rebuilt(self):
        phone = FakePhone(sims=[sim("kOne", MTS)], behaviours=["ignore"])
        self.install(phone, ["Telia dk 68", "1", "INSTALL"])
        for ipcc in (self.root / "packages").rglob("*.ipcc"):
            ipcc.write_bytes(b"damaged")
        for marker in self.markers():
            marker.unlink()
        result, out = self.install(phone, ["Telia dk 68", "1", "INSTALL"])
        self.assertEqual(result, 0, out)
        self.assertIn("готовлю её заново", out)

    def test_no_phone_state_files_with_sim_numbers_are_written(self):
        phone = FakePhone(sims=[sim("kOne", MTS)])
        self.install(phone, ["Telia dk 68", "1", "INSTALL"])
        self.assertEqual(list(self.root.glob("phone-state-*.json")), [])
        for path in self.root.rglob("*"):
            if path.is_file() and path.suffix in {".json", ".log", ".once"}:
                text = path.read_text(encoding="utf-8")
                self.assertNotIn(MTS["imsi"], text, path)
                self.assertNotIn(MTS["iccid"], text, path)

    # ---- defects found in the second review pass -----------------------------

    def test_refusal_masked_by_cleanup_error_is_still_a_refusal(self):
        phone = FakePhone(sims=[sim("kOne", MTS)], behaviours=["reject_masked"])
        result, out = self.install(phone, ["Telia dk 68", "1", "INSTALL"])
        self.assertEqual(result, 2, out)
        self.assertEqual(phone.restarts, 0, "a refused package must not reboot the phone")
        self.assertIn("Экранное время", out)
        self.assertEqual(self.markers(), [])

    def test_profile_rolled_back_by_reboot_is_not_reported_as_success(self):
        phone = FakePhone(sims=[sim("kOne", MTS)], behaviours=["early_revert"], restart_delay=4)
        result, out = self.install(phone, ["Telia dk 68", "1", "INSTALL"])
        self.assertEqual(result, 3, out)
        self.assertNotIn("Готово.", out)
        self.assertIn("НЕ ПОДТВЕРЖДЕНО", out)
        self.assertEqual(phone.sims[0].bundle, "com.apple.MTS_ru")

    def test_refused_restart_asks_for_manual_reboot(self):
        phone = FakePhone(sims=[sim("kOne", MTS)], restart_mode="refuse")
        result, out = self.install(phone, ["Telia dk 68", "1", "INSTALL"])
        self.assertEqual(result, 3, out)
        self.assertIn("не принял команду перезагрузки", out)
        self.assertNotIn("Команда перезагрузки принята", out)
        self.assertEqual(len(self.markers()), 1)

    def test_silent_restart_is_not_mistaken_for_a_reboot(self):
        phone = FakePhone(sims=[sim("kOne", MTS)], restart_mode="silent", behaviours=["early_revert"])
        result, out = self.install(phone, ["Telia dk 68", "1", "INSTALL"])
        self.assertEqual(result, 3, out)
        self.assertIn("перезагрузка не обнаружена", out)
        self.assertNotIn("Готово.", out)

    def test_newer_version_already_active_is_explained(self):
        phone = FakePhone(sims=[sim("kOne", MTS, version="74.0")])
        result, out = self.install(phone, ["MTS ru", "1", "INSTALL"])
        self.assertEqual(result, 0, out)
        self.assertIn("уже активна более новая версия MTS_ru 74.0", out)
        self.assertEqual(phone.sent_selectors, [])

    def test_ctrl_c_returns_to_menu(self):
        phone = FakePhone(sims=[sim("kOne", MTS)])
        output = io.StringIO()
        with phone.attached(), mock.patch("builtins.input", side_effect=["1", KeyboardInterrupt(), "", "0"]), \
                contextlib.redirect_stdout(output):
            try:
                result = carrier_app.run_menu()
            except KeyboardInterrupt:  # unittest would silently stop the run otherwise
                self.fail("Ctrl+C escaped the menu and closed the program")
        self.assertEqual(result, 130)
        self.assertIn("Действие прервано", output.getvalue())
        self.assertEqual(output.getvalue().count(f"CarrierSelector v.{carrier_app.APP_VERSION}"), 2)

    def test_retry_prompt_describes_latest_attempt(self):
        paths = []
        for name, started, phase in (("k-install-result.json", "2026-11-13T10:00:00+00:00", "checked"),
                                     ("k-20261113T120000-install-result.json",
                                      "2026-11-13T12:00:00+00:00", "unconfirmed_after_restart")):
            path = self.root / name
            path.write_text(json.dumps({"started_utc": started, "phase": phase}), encoding="utf-8")
            paths.append(path)
        # Directory listing order is arbitrary: the newest attempt must win either way.
        for order in (paths, paths[::-1]):
            fake_root = mock.Mock(glob=mock.Mock(return_value=list(order)))
            with mock.patch("carrier_app.state_root", return_value=fake_root):
                self.assertEqual(carrier_app.latest_receipt("k")["phase"], "unconfirmed_after_restart")

    def test_original_profile_comes_from_earliest_attempt_not_latest_write(self):
        state = {"imsi": MTS["imsi"], "iccid": MTS["iccid"], "slot": "kOne",
                 "product": "iPhone19,3", "hardware": "V64AP"}
        digest = carrier_app.selector_digest(state)
        def receipt(name, started, before):
            (self.root / name).write_text(json.dumps({
                "operation": "carrier-install", "started_utc": started, "selector_hashes": [digest],
                "before": [{"bundle_identifier": before, "bundle_version": "73.0"}],
                "desired": [{"bundle_identifier": "com.apple.Telia_dk", "bundle_version": "68.0"}],
            }), encoding="utf-8")
        receipt("b-install-result.json", "2026-11-13T12:00:00+00:00", "com.apple.MTN_cy")
        receipt("a-install-result.json", "2026-11-13T10:00:00+00:00", "com.apple.MTS_ru")  # written last
        self.assertEqual(carrier_app.recover_legacy_baseline(state)["bundle_identifier"], "com.apple.MTS_ru")

    # ---- only the suitable version is offered -------------------------------

    def offered(self, product, hardware, query):
        overview = {"product": product, "hardware": hardware}
        profiles = carrier_app.profiles_for(self.catalog, carrier_app.matching_family(self.catalog, overview), overview)
        return [row["label"] for row in carrier_app.offered_profiles(profiles)
                if carrier_app.profile_matches_query(row, query)]

    def test_only_board_suitable_version_is_offered(self):
        # iPhone 17 Pro Max: its own iOS 27.2.1 firmware ships Vodafone_ro 73.0 with V54 settings (v6.0).
        self.assertEqual(self.offered("iPhone18,2", "V54AP", "Vodafone ro"), ["Vodafone_ro 73.0 · Vodafone RO"])
        # iPhone 15 Pro: 73.0 has exact D83 settings and is newer.
        self.assertEqual(self.offered("iPhone16,1", "D83AP", "Vodafone ro"), ["Vodafone_ro 73.0"])
        # v6.4: within one operator the newest version comes first; curated
        # tested older variants stay available below it.
        self.assertEqual(self.offered("iPhone18,2", "V54AP", "Telia dk"), ["Telia_dk 73.0 · Norlys", "Telia_dk 68.0"])
        # iPhone 17 (V57) on iOS 27.2.1: MTN_cy 73.0 is offered, not only the tested 72.0.
        self.assertEqual(self.offered("iPhone18,3", "V57AP", "MTN cy"), ["MTN_cy 73.0 · epic", "MTN_cy 72.0"])
        # v6.4: Telia_dk 68.0 and 62.0 from iOS 26.3 / 18.3 firmware for iPhone 12, 13 and SE 3.
        for product, hardware in (("iPhone13,2", "D53gAP"), ("iPhone14,5", "D17AP"), ("iPhone14,6", "D49AP")):
            self.assertEqual(self.offered(product, hardware, "Telia dk"),
                             ["Telia_dk 73.0 · Norlys", "Telia_dk 68.0", "Telia_dk 62.0"], product)
        # iPhone 18 Pro Max US: the iOS 27.2.1 Apple set brings exact V64 settings (v5.8).
        self.assertEqual(self.offered("iPhone19,3", "V64AP", "Vodafone ro"), ["Vodafone_ro 73.0 · Vodafone RO"])
        # iPhone 13 now has its own firmware set; an AT&T eSIM bootstrap profile has no D17 settings at all.
        self.assertEqual(self.offered("iPhone14,5", "D17AP", "Vodafone ro"), ["Vodafone_ro 73.0 · Vodafone RO"])
        self.assertEqual(self.offered("iPhone14,5", "D17AP", "BootstrapCAM ATT"), [])
        # Shared Russian profiles stay available (physically confirmed to apply).
        self.assertEqual(len(self.offered("iPhone19,3", "V64AP", "MTS ru")), 1)
        # One version per operator everywhere.
        for product, hardware in (("iPhone18,2", "V54AP"), ("iPhone19,3", "V64AP"), ("iPhone16,1", "D83AP")):
            overview = {"product": product, "hardware": hardware}
            profiles = carrier_app.profiles_for(self.catalog, carrier_app.matching_family(self.catalog, overview), overview)
            offered = carrier_app.offered_profiles(profiles)
            newest = [row for row in offered if str(row["key"]).startswith("catalog-")]
            self.assertEqual(len(newest), len({row["identifier"] for row in newest}))
            for row in offered:  # extra rows are only curated variants, one per version
                if not str(row["key"]).startswith("catalog-"):
                    self.assertEqual(sum(1 for o in offered if o["identifier"] == row["identifier"]
                                         and o["version"] == row["version"]), 1)
            self.assertTrue(all(carrier_app.is_suitable(row) for row in offered))

    def test_unsuitable_version_cannot_be_chosen_at_all(self):
        phone = FakePhone(sims=[sim("kOne", MTS)])
        # The old bypass words do nothing now: "A" and "BOARD" are just searches.
        result, out = self.install(phone, ["BootstrapCAM ATT", "A", "BOARD", "Q"])
        self.assertEqual(result, 0, out)
        self.assertIn("Для этого iPhone подходящих версий нет. Есть 2 для других моделей", out)
        self.assertEqual(phone.sent_selectors, [])

    def test_unsuitable_package_is_refused_right_before_sending(self):
        """Defense in depth: even a plan that somehow holds a wrong-board package sends nothing."""
        phone = FakePhone(sims=[sim("kOne", MTS)])
        # The iPhone 15-only Vodafone_ro 73.0 variant (catalog donor) has no V54 settings.
        donor = next(d for f in self.catalog["families"] for d in f.get("donors", []) if d["key"] == "vodafone73-d83")
        wrong = dict(donor, version="73.0")
        with phone.attached():
            line = asyncio.run(carrier_app.read_states())["lines"][0]
            package = carrier_app.prepare_package(wrong, line)
            with self.assertRaisesRegex(carrier_app.BuildError, "не содержит настроек для платы V54"):
                asyncio.run(carrier_app.execute_plan([(line, wrong, package)]))
        self.assertEqual(phone.sent_selectors, [])
        self.assertEqual(self.markers(), [])

    def test_unknown_board_blocks_installation(self):
        phone = FakePhone(hardware="", sims=[sim("kOne", MTS)])
        output = io.StringIO()
        with phone.attached(), mock.patch("builtins.input", side_effect=["1", "", "0"]), contextlib.redirect_stdout(output):
            result = carrier_app.run_menu()
        self.assertEqual(result, 2)
        self.assertIn("не сообщил модель и плату", output.getvalue())
        self.assertEqual(phone.sent_selectors, [])

    def test_operator_without_suitable_version_explains_and_cancels(self):
        phone = FakePhone(product="iPhone14,5", hardware="D17AP", sims=[sim("kOne", MTS)])
        result, out = self.install(phone, ["BootstrapCAM ATT", "Q"])
        self.assertEqual(result, 0, out)
        self.assertIn("Для этого iPhone подходящих версий нет. Есть 2 для других моделей", out)
        self.assertEqual(phone.sent_selectors, [])

    def test_iphone_18_pro_max_us_installs_vodafone_ro_73(self):
        """v5.8: the user's model with a single eSIM shown as kTwo gets Vodafone_ro 73.0 for V64."""
        phone = FakePhone(product="iPhone19,3", hardware="V64AP", sims=[sim("kTwo", MTS)],
                          identifiers_in="keys", root_keys={**keys(MTS), **keys(MEGAFON, "2")})
        result, out = self.install(phone, ["Vodafone ro", "1", "INSTALL"])
        self.assertEqual(result, 0, out)
        self.assertIn("точные настройки V64", out)
        self.assertEqual(phone.orphans, [])
        self.assertEqual((phone.sims[0].bundle, phone.sims[0].version), ("com.apple.Vodafone_ro", "73.0"))

    def test_restore_still_uses_full_catalog(self):
        phone = FakePhone(product="iPhone19,3", hardware="V64AP", sims=[sim("kOne", MTS)])
        self.install(phone, ["Swisscom", "1", "INSTALL"])
        result, out = self.run_action(carrier_app.restore_interactive, phone, ["RESTORE"])
        self.assertEqual(result, 0, out)
        self.assertEqual((phone.sims[0].bundle, phone.sims[0].version), ("com.apple.MTS_ru", "73.0"))

    # ---- iOS deletes a lower version sent over our own active package --------

    def test_older_package_over_own_active_package_is_refused_with_instructions(self):
        """Reproduces the 14 Nov iPhone 15 Pro case: Vodafone_ro 73.0 active -> Telia_dk 68.0."""
        phone = FakePhone(product="iPhone16,1", hardware="D83AP",
                          sims=[sim("kOne", MTS, "com.apple.MTS_ru", "72.0")])
        result, out = self.install(phone, ["Vodafone ro", "1", "INSTALL"])
        self.assertEqual(result, 0, out)
        self.assertEqual((phone.sims[0].bundle, phone.sims[0].version), ("com.apple.Vodafone_ro", "73.0"))
        with self.assertRaisesRegex(carrier_app.BuildError,
                                    r"активен ранее установленный пакет Vodafone_ro 73\.0.*Telia_dk 68\.0 — более "
                                    r"старая версия.*сначала верните исходный профиль \(пункт 3\)"):
            self.install(phone, ["Telia dk", "1"])
        self.assertEqual(len(phone.sent_selectors), 1, "nothing may be sent")
        self.assertEqual(phone.sims[0].bundle, "com.apple.Vodafone_ro")

    def test_restore_first_then_older_package_installs(self):
        # iOS 27.2.1 ships MTS_ru 73.0 as the built-in profile of this iPhone 15 Pro.
        phone = FakePhone(product="iPhone16,1", hardware="D83AP",
                          sims=[sim("kOne", MTS, "com.apple.MTS_ru", "73.0")])
        self.install(phone, ["Vodafone ro", "1", "INSTALL"])
        result, out = self.run_action(carrier_app.restore_interactive, phone, ["RESTORE"])
        self.assertEqual(result, 0, out)
        self.assertEqual((phone.sims[0].bundle, phone.sims[0].version), ("com.apple.MTS_ru", "73.0"))
        result, out = self.install(phone, ["Telia dk", "1", "INSTALL"])
        self.assertEqual(result, 0, out)
        self.assertEqual((phone.sims[0].bundle, phone.sims[0].version), ("com.apple.Telia_dk", "68.0"))

    def test_restore_on_older_ios_returns_its_own_version(self):
        phone = FakePhone(product="iPhone16,1", hardware="D83AP", ios="27.0.1", build="24A341",
                          sims=[sim("kOne", MTS, "com.apple.MTS_ru", "72.0")])
        self.install(phone, ["Vodafone ro", "1", "INSTALL"])
        result, out = self.run_action(carrier_app.restore_interactive, phone, ["RESTORE"])
        self.assertEqual(result, 0, out)
        self.assertEqual((phone.sims[0].bundle, phone.sims[0].version), ("com.apple.MTS_ru", "72.0"))

    def test_restore_after_ios_update_returns_newest_native_version(self):
        # Real case: Vodafone installed on iOS 27.0.1 (original MTS 72.0 recorded),
        # then the phone was updated to iOS 27.2.1, which ships MTS 73.0.
        phone = FakePhone(product="iPhone16,1", hardware="D83AP", ios="27.0.1", build="24A341",
                          sims=[sim("kOne", MTS, "com.apple.MTS_ru", "72.0")])
        result, out = self.install(phone, ["Vodafone ro", "1", "INSTALL"])
        self.assertEqual(result, 0, out)
        phone.ios, phone.build = "27.2.1", "24B156"
        phone.sims[0].system = ("com.apple.MTS_ru", "73.0")
        result, out = self.run_action(carrier_app.restore_interactive, phone, ["RESTORE"])
        self.assertEqual(result, 0, out)
        self.assertEqual((phone.sims[0].bundle, phone.sims[0].version), ("com.apple.MTS_ru", "73.0"))
        self.assertIn("MTS_ru 73.0", out)

    def test_simulator_reproduces_ios_deletion_without_the_guard(self):
        phone = FakePhone(product="iPhone16,1", hardware="D83AP",
                          sims=[sim("kOne", MTS, "com.apple.MTS_ru", "72.0")])
        self.install(phone, ["Vodafone ro", "1", "INSTALL"])
        with mock.patch("carrier_app.refuse_downgrade_over_own_package"):
            result, out = self.install(phone, ["Telia dk", "1", "INSTALL"])
        self.assertEqual(result, 3, out)
        self.assertEqual(phone.sims[0].bundle, "com.apple.MTS_ru")  # exactly what the real phone did

    def test_locked_phone_gives_actionable_message(self):
        from pymobiledevice3.exceptions import PasswordRequiredError
        phone = FakePhone(sims=[sim("kOne", MTS)])
        output = io.StringIO()

        async def locked(*args, **kwargs):
            raise PasswordRequiredError()

        with phone.attached(), mock.patch("pymobiledevice3.lockdown.create_using_usbmux", locked), \
                mock.patch("builtins.input", side_effect=["4", "", "0"]), contextlib.redirect_stdout(output):
            result = carrier_app.run_menu()
        self.assertEqual(result, 2)
        self.assertIn("iPhone заблокирован", output.getvalue())
        self.assertIn("Журнал для разбора", output.getvalue())


class RestoreV61(unittest.TestCase):
    """v6.1: item 3 no longer reuses a stale rollback package prepared by v5.7."""

    @classmethod
    def setUpClass(cls):
        cls.catalog = carrier_app.load_catalog()

    setUp = EndToEnd.setUp
    run_action = EndToEnd.run_action
    markers = EndToEnd.markers

    def iphone17(self, **kwargs):
        # iPhone 17 (V57) on iOS 27.2.1, Beeline SIM whose built-in profile is 73.0,
        # with our MTN_cy 72.0 active (installed by v5.7).
        beeline = dict(imsi="250990000000004", iccid="8970199000000000044", mcc="250", mnc="99")
        s = Sim(slot="kOne", bundle="com.apple.MTN_cy", version="72.0",
                system=("com.apple.VimpelCom_ru", "73.0"), user_active=True, **beeline)
        return FakePhone(product="iPhone18,3", hardware="V57AP", sims=[s], **kwargs)

    def seed_baseline(self, phone, rollback_version=None):
        line = {"imsi": phone.sims[0].imsi, "iccid": phone.sims[0].iccid}
        original = {"bundle_identifier": "com.apple.VimpelCom_ru", "bundle_version": "73.0"}
        path = carrier_app.baseline_path(line)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"format": 1, "selector_sha256": carrier_app.selector_digest(line),
                                    "original": original, "identifiers_recorded": False}))
        if rollback_version:
            overview = {"product": phone.product, "hardware": phone.hardware, "version": phone.ios}
            profiles = carrier_app.profiles_for(self.catalog, carrier_app.matching_family(self.catalog, overview), overview)
            old = next(p for p in profiles if p["identifier"] == "com.apple.VimpelCom_ru"
                       and p["version"] == rollback_version and carrier_app.is_suitable(p))
            state = dict(line, product=phone.product, hardware=phone.hardware, version=phone.ios)
            package = carrier_app.prepare_package(old, state)
            carrier_app.save_rollback_package(state, original, old, package)
        return original

    def test_restore_ignores_stale_v57_rollback_and_returns_73(self):
        phone = self.iphone17()
        self.seed_baseline(phone, rollback_version="72.0")
        result, out = self.run_action(carrier_app.restore_interactive, phone, ["RESTORE"])
        self.assertEqual(result, 0, out)
        self.assertEqual((phone.sims[0].bundle, phone.sims[0].version), ("com.apple.VimpelCom_ru", "73.0"))
        self.assertIn("VimpelCom_ru 73.0", out)


class AlreadyActiveV61(unittest.TestCase):
    """v6.1: nothing to send -> said at once, no INSTALL/RESTORE prompt, no restart."""

    @classmethod
    def setUpClass(cls):
        cls.catalog = carrier_app.load_catalog()

    setUp = EndToEnd.setUp
    run_action = EndToEnd.run_action

    def test_install_of_active_profile_asks_nothing(self):
        phone = FakePhone(product="iPhone16,1", hardware="D83AP", sims=[sim("kOne", MTS, "com.apple.MTS_ru", "73.0")])
        result, out = self.run_action(carrier_app.install_interactive, phone, ["MTS ru", "1"])
        self.assertEqual(result, 0, out)
        self.assertIn("уже активен; установка не нужна", out)
        self.assertNotIn("введите INSTALL", out)
        self.assertEqual((phone.sent_selectors, phone.restarts), ([], 0))

    def test_older_same_operator_over_newer_explains_the_way(self):
        phone = FakePhone(product="iPhone16,1", hardware="D83AP", sims=[sim("kOne", MTS, "com.apple.MTS_ru", "73.0")])
        result, out = self.run_action(carrier_app.install_interactive, phone, ["MTN cy", "1", "INSTALL"])
        self.assertEqual((phone.sims[0].bundle, phone.sims[0].version), ("com.apple.MTN_cy", "73.0"))
        result, out = self.run_action(carrier_app.install_interactive, phone, ["MTN cy", "2"])
        self.assertEqual(result, 0, out)
        self.assertIn("MTN_cy 72.0 не ставится", out)
        self.assertIn("сначала верните исходный профиль (пункт 3)", out)
        self.assertEqual(phone.restarts, 1)

    def test_restore_when_original_active_asks_nothing(self):
        phone = FakePhone(product="iPhone16,1", hardware="D83AP", sims=[sim("kOne", MTS, "com.apple.MTS_ru", "73.0")])
        result, out = self.run_action(carrier_app.install_interactive, phone, ["MTN cy", "2", "INSTALL"])
        self.assertEqual(result, 0, out)
        result, out = self.run_action(carrier_app.restore_interactive, phone, ["RESTORE"])
        self.assertEqual(result, 0, out)
        self.assertEqual((phone.sims[0].bundle, phone.sims[0].version), ("com.apple.MTS_ru", "73.0"))
        result, out = self.run_action(carrier_app.restore_interactive, phone, [])
        self.assertEqual(result, 0, out)
        self.assertIn("Исходные профили уже активны", out)
        self.assertEqual(phone.restarts, 2)


MTS_BY = dict(imsi="257020000000011", iccid="8937502000000000111", mcc="257", mnc="02")
LIFE_BY = dict(imsi="257040000000012", iccid="8937504000000000122", mcc="257", mnc="04")
A1_BY = dict(imsi="257010000000013", iccid="8937501000000000133", mcc="257", mnc="01")


class V62(unittest.TestCase):
    """v6.2: Belarus SIMs, no misleading Telia_dk 72.0 on iPhone 18, MTS hint."""

    @classmethod
    def setUpClass(cls):
        cls.catalog = carrier_app.load_catalog()

    setUp = EndToEnd.setUp
    run_action = EndToEnd.run_action
    offered = EndToEnd.offered

    def test_iphone18_offers_only_telia_73(self):
        for product, hardware in (("iPhone19,2", "V63AP"), ("iPhone19,4", "V68AP"),
                                  ("iPhone19,7", "V64sAP"), ("iPhone19,3", "V64AP")):
            labels = self.offered(product, hardware, "Telia dk")
            self.assertEqual(len(labels), 1, (product, labels))
            self.assertTrue(labels[0].startswith("Telia_dk 73.0"), (product, labels))
        # Models that have the curated Telia_dk 68.0 keep it (v6.4: and 62.0 «TELIA»).
        self.assertEqual(self.offered("iPhone16,1", "D83AP", "Telia dk")[1:], ["Telia_dk 68.0", "Telia_dk 62.0"])
        for family in self.catalog["families"]:
            for donor in family.get("donors", []):
                self.assertTrue(carrier_app.bundle_path(str(donor["path"])).is_dir(), donor["key"])
                self.assertNotEqual(donor["key"], "telia72-v63")

    def test_mts_on_iphone18_search_telia_shows_vodafone_ro_hint(self):
        phone = FakePhone(product="iPhone19,2", hardware="V63AP", sims=[sim("kOne", MTS, "com.apple.MTS_ru", "73.0")])
        result, out = self.run_action(carrier_app.install_interactive, phone, ["Telia dk", "Q"])
        self.assertIn("Telia_dk 68.0 для этой модели нет", out)
        self.assertIn("Vodafone_ro 73.0", out)
        self.assertEqual((phone.sent_selectors, phone.restarts), ([], 0))

    def test_active_mark_only_on_installed_version(self):
        phone = FakePhone(product="iPhone16,1", hardware="D83AP", sims=[sim("kOne", MTS, "com.apple.Telia_dk", "68.0")])
        result, out = self.run_action(carrier_app.install_interactive, phone, ["Telia dk", "Q"])
        search = out[out.index("Поиск: Telia dk"):]  # v6.4: recommended rows also head the full list
        rows = [l for l in search.splitlines() if l.strip().startswith(("1. Telia_dk", "2. Telia_dk"))]
        self.assertEqual(len(rows), 2, out)
        self.assertIn("Telia_dk 68.0", rows[0])
        self.assertIn("активен", rows[0])
        self.assertNotIn("активен", rows[1])
        self.assertTrue(carrier_app.is_active_row({"identifier": "x", "version": "1"}, {"bundle_identifier": "x"}))
        self.assertFalse(carrier_app.is_active_row({"identifier": "y", "version": "1"},
                                                   {"bundle_identifier": "x", "bundle_version": "1"}))

    def test_mts_on_iphone15_search_telia_has_no_hint(self):
        phone = FakePhone(product="iPhone16,1", hardware="D83AP", sims=[sim("kOne", MTS, "com.apple.MTS_ru", "73.0")])
        result, out = self.run_action(carrier_app.install_interactive, phone, ["Telia dk", "Q"])
        self.assertNotIn("для этой модели нет", out)

    def test_belarus_two_sims_install_and_restore(self):
        phone = FakePhone(product="iPhone16,1", hardware="D83AP",
                          sims=[sim("kOne", MTS_BY, "com.apple.MTS_by", "73.0"),
                                sim("kTwo", LIFE_BY, "com.apple.life_by", "73.0")])
        result, out = self.run_action(carrier_app.install_interactive, phone,
                                      ["1", "Telia dk", "1", "MTN cy", "1", "INSTALL"])
        self.assertEqual(result, 0, out)
        self.assertNotIn("не России/Беларуси", out)
        self.assertEqual([(s.bundle, s.version) for s in phone.sims],
                         [("com.apple.Telia_dk", "73.0"), ("com.apple.MTN_cy", "73.0")])
        result, out = self.run_action(carrier_app.restore_interactive, phone, ["1", "RESTORE"])
        self.assertEqual(result, 0, out)
        self.assertEqual([(s.bundle, s.version) for s in phone.sims],
                         [("com.apple.MTS_by", "73.0"), ("com.apple.life_by", "73.0")])
        self.assertEqual(phone.restarts, 2)
        for secret in (MTS_BY["imsi"], MTS_BY["iccid"], LIFE_BY["imsi"], LIFE_BY["iccid"]):
            self.assertNotIn(secret, out)

    def test_belarus_a1_iphone18_install_and_restore(self):
        phone = FakePhone(product="iPhone19,2", hardware="V63AP", sims=[sim("kOne", A1_BY, "com.apple.mobilkom_by", "73.0")])
        result, out = self.run_action(carrier_app.install_interactive, phone, ["Telia dk", "1", "INSTALL"])
        self.assertEqual(result, 0, out)
        self.assertNotIn("для этой модели нет", out)  # the hint is for MTS Russia only
        self.assertEqual((phone.sims[0].bundle, phone.sims[0].version), ("com.apple.Telia_dk", "73.0"))
        result, out = self.run_action(carrier_app.restore_interactive, phone, ["RESTORE"])
        self.assertEqual(result, 0, out)
        self.assertEqual((phone.sims[0].bundle, phone.sims[0].version), ("com.apple.mobilkom_by", "73.0"))


class ResetToBuiltIn(unittest.TestCase):
    """v6.2 item 3: back to the carrier profile built into iOS by sending a copy of
    the SIM's own bundle with an empty SupportedSIMs (real iPhone 15 Pro / 27.2.1)."""

    @classmethod
    def setUpClass(cls):
        cls.catalog = carrier_app.load_catalog()

    setUp = EndToEnd.setUp
    run_action = EndToEnd.run_action

    def reset(self, phone, answers):
        return self.run_action(carrier_app.reset_interactive, phone, answers)

    def log_text(self):
        return "".join(p.read_text(encoding="utf-8") for p in (self.root / "logs").glob("*.log"))

    def phone15(self, **kwargs):
        kwargs.setdefault("sims", [sim("kOne", MTS)])
        return FakePhone(product="iPhone16,1", hardware="D83AP", **kwargs)

    @staticmethod
    def bound(ids=MTS, bundle="com.apple.Telia_dk", version="68.0", system=("com.apple.MTS_ru", "73.0")):
        """A SIM with a user package bound to it (installed by anything)."""
        return Sim(slot="kOne", bundle=bundle, version=version, system=system, user_active=True, **ids)

    def test_reset_after_install_returns_builtin_profile(self):
        phone = self.phone15()
        result, out = self.run_action(carrier_app.install_interactive, phone, ["Telia dk", "1", "INSTALL"])
        self.assertEqual(result, 0, out)
        self.assertEqual((phone.sims[0].bundle, phone.sims[0].version), ("com.apple.Telia_dk", "68.0"))
        result, out = self.reset(phone, ["RESET"])
        self.assertEqual(result, 0, out)
        self.assertEqual((phone.sims[0].bundle, phone.sims[0].version), ("com.apple.MTS_ru", "73.0"))
        self.assertFalse(phone.sims[0].user_active)
        self.assertEqual((phone.resets, phone.restarts), (1, 2))
        self.assertIn("привязка удалена, работает встроенный профиль iOS MTS_ru 73.0", out)
        self.assertIn("встроенный профиль iOS MTS_ru 73.0 или новее", out)
        for secret in (MTS["imsi"], MTS["iccid"], phone.udid):
            self.assertNotIn(secret, out)
            self.assertNotIn(secret, self.log_text())
        self.assertIn('"event": "reset_send"', self.log_text())
        self.assertIn('"result": "rejected"', self.log_text())

    def test_reset_package_is_the_sims_own_bundle_with_empty_supported_sims(self):
        phone = self.phone15(sims=[self.bound()])
        sent = []
        original = phone._receive
        phone._receive = lambda payload: (sent.append(payload), original(payload))[1]
        result, out = self.reset(phone, ["RESET"])
        self.assertEqual(result, 0, out)
        with zipfile.ZipFile(io.BytesIO(sent[0])) as archive:
            root = archive.namelist()[0]
            self.assertEqual(root, f"Payload/{phone.sims[0].selector}/")
            info = plistlib.loads(archive.read(root + "Info.plist"))
            carrier = plistlib.loads(archive.read(root + "carrier.plist"))
            source = carrier_app.bundle_path("ios2721-d83/MTS_ru.bundle")
            for name in ("Info.plist", "signatures/common.plist", "supported_devices.plist"):
                self.assertEqual(archive.read(root + name), (source / name).read_bytes(), name)
        self.assertEqual(info["CFBundleIdentifier"], "com.apple.MTS_ru")
        self.assertEqual(carrier["SupportedSIMs"], [])
        original_carrier = plistlib.loads((source / "carrier.plist").read_bytes())
        original_carrier["SupportedSIMs"] = []
        self.assertEqual(carrier, original_carrier, "only SupportedSIMs may change")

    def test_reset_needs_no_saved_baseline(self):
        """A package bound by another computer or another tool can be removed too."""
        phone = self.phone15(sims=[Sim(slot="kOne", bundle="com.apple.Telia_dk", version="68.0",
                                       system=("com.apple.MTS_ru", "73.0"), user_active=True, **MTS)])
        self.assertEqual(list(self.root.glob("baselines/*.json")), [])
        result, out = self.reset(phone, ["RESET"])
        self.assertEqual(result, 0, out)
        self.assertEqual(phone.sims[0].bundle, "com.apple.MTS_ru")

    def test_reset_without_binding_changes_nothing_and_is_safe(self):
        phone = self.phone15()
        result, out = self.reset(phone, ["RESET"])
        self.assertEqual(result, 0, out)
        self.assertIn("сейчас уже этот оператор", out)
        self.assertEqual((phone.sims[0].bundle, phone.sims[0].version), ("com.apple.MTS_ru", "73.0"))
        result, out = self.reset(phone, ["RESET"])  # repeating is harmless
        self.assertEqual(result, 0, out)
        self.assertEqual(phone.resets, 2)

    def test_cancel_sends_nothing(self):
        phone = self.phone15(sims=[self.bound()])
        result, out = self.reset(phone, ["нет"])
        self.assertEqual(result, 0, out)
        self.assertEqual((phone.resets, phone.restarts), (0, 0))
        self.assertIn("Отменено; телефон не изменён.", out)

    def test_two_sims_reset_only_one_keeps_the_other(self):
        phone = FakePhone(product="iPhone18,2", hardware="V54AP",
                          sims=[sim("kOne", MTS), sim("kTwo", MEGAFON, "com.apple.MegaFon_ru", "73.0")])
        result, out = self.run_action(carrier_app.install_interactive, phone,
                                      ["1", "MTN cy", "1", "Telia dk 68", "1", "INSTALL"])
        self.assertEqual(result, 0, out)
        result, out = self.reset(phone, ["3", "RESET"])  # only the second SIM
        self.assertEqual(result, 0, out)
        self.assertIn("Для каких SIM вернуть профиль, встроенный в iOS?", out)
        self.assertNotIn("установить профили", out)
        self.assertEqual([s.bundle for s in phone.sims], ["com.apple.MTN_cy", "com.apple.MegaFon_ru"])
        self.assertEqual(phone.resets, 1)
        result, out = self.reset(phone, ["1", "RESET"])  # both
        self.assertEqual(result, 0, out)
        self.assertEqual([s.bundle for s in phone.sims], ["com.apple.MTS_ru", "com.apple.MegaFon_ru"])
        self.assertEqual((phone.resets, phone.restarts), (3, 3))
        self.assertEqual(phone.orphans, [])

    def test_log_unavailable_is_judged_by_profile(self):
        phone = self.phone15(sims=[self.bound()], log_mode="broken")
        result, out = self.reset(phone, ["RESET"])
        self.assertEqual(result, 0, out)
        self.assertIn("подтверждено только по профилю", out)

    def test_ios_accepting_the_reset_package_is_reported(self):
        phone = self.phone15(sims=[Sim(slot="kOne", bundle="com.apple.Telia_dk", version="68.0",
                                       system=("com.apple.MTS_ru", "73.0"), user_active=True, **MTS)],
                             reset_answer="accept")
        result, out = self.reset(phone, ["RESET"])
        self.assertEqual(result, 3, out)
        self.assertIn("iOS приняла пакет сброса", out)
        self.assertIn("пункт 7", out)

    def test_profile_not_back_after_reboot_is_not_success(self):
        phone = self.phone15(sims=[Sim(slot="kOne", bundle="com.apple.Telia_dk", version="68.0",
                                       system=("com.apple.Telia_dk", "68.0"), user_active=True, **MTS)])
        result, out = self.reset(phone, ["RESET"])
        self.assertEqual(result, 3, out)
        self.assertIn("НЕ ПОДТВЕРЖДЕНО", out)

    def test_belarus_and_iphone18(self):
        for product, hardware, ids, native in (
                ("iPhone16,1", "D83AP", LIFE_BY, "com.apple.life_by"),
                ("iPhone19,2", "V63AP", A1_BY, "com.apple.mobilkom_by"),
                ("iPhone19,3", "V64AP", MTS_BY, "com.apple.MTS_by")):
            phone = FakePhone(product=product, hardware=hardware,
                              sims=[Sim(slot="kOne", bundle="com.apple.Telia_dk", version="73.0",
                                        system=(native, "73.0"), user_active=True, **ids)])
            result, out = self.reset(phone, ["RESET"])
            self.assertEqual(result, 0, (product, out))
            self.assertEqual(phone.sims[0].bundle, native, product)

    def test_network_without_own_ios_profile(self):
        ids = dict(imsi="250620000000021", iccid="8970620000000000211", mcc="250", mnc="62")
        phone = self.phone15(sims=[Sim(slot="kOne", bundle="com.apple.MTN_cy", version="73.0",
                                       system=("com.apple.Unknown", "73.0"), user_active=True, **ids)])
        result, out = self.reset(phone, ["RESET"])
        self.assertEqual(result, 0, out)
        self.assertIn("своего профиля у этой сети в iOS нет", out)
        self.assertEqual(phone.sims[0].bundle, "com.apple.Unknown")

    def test_older_ios_expects_its_own_native_version(self):
        phone = FakePhone(product="iPhone18,2", hardware="V54AP", ios="27.0.1", build="24A341",
                          sims=[Sim(slot="kOne", bundle="com.apple.Telia_dk", version="72.0",
                                    system=("com.apple.MTS_ru", "72.0"), user_active=True, **MTS)])
        result, out = self.reset(phone, ["RESET"])
        self.assertEqual(result, 0, out)
        self.assertIn("MTS_ru 72", out)

    def test_menu_item_3_is_reset_and_item_7_is_old_restore(self):
        with mock.patch("carrier_app.reset_interactive", return_value=0) as reset, \
                mock.patch("carrier_app.restore_interactive", return_value=0) as restore, \
                mock.patch("builtins.input", side_effect=["3", "", "7", "", "0"]), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            carrier_app.run_menu()
        self.assertEqual((reset.call_count, restore.call_count), (1, 1))
        self.assertIn("3. Вернуть профиль, встроенный в iOS", out.getvalue())
        self.assertIn("7. Старый способ возврата", out.getvalue())


class ModelBinding(unittest.TestCase):
    """v6.0: every model gets the profile from its OWN iOS 27.2.1 firmware set
    (or a curated tested variant that also carries exact settings for its board)."""

    def test_key_operators_come_from_each_models_own_firmware(self):
        catalog = carrier_app.load_catalog()
        base = carrier_app.resource_root() / "embedded_catalog"
        for family in catalog["families"]:
            for product, hardware in zip(family["products"], family["hardwares"]):
                overview = {"product": product, "hardware": hardware}
                self.assertIs(carrier_app.matching_family(catalog, overview), family, product)
                offered = {row["identifier"]: row for row in carrier_app.offered_profiles(
                    carrier_app.profiles_for(catalog, family, overview))}
                short = carrier_app.board_short(hardware).casefold()
                for name in ("MTS_ru", "MegaFon_ru", "VimpelCom_ru", "Vodafone_ro"):
                    row = offered.get(f"com.apple.{name}")
                    self.assertIsNotNone(row, f"{product}: {name} не предлагается")
                    chosen = carrier_app.bundle_path(str(row["path"]))
                    own = (base / family["catalog_root"] / f"{name}.bundle").resolve()
                    if chosen != own:
                        self.assertFalse(str(row["key"]).startswith("catalog-"), f"{product}: {name} из {row['path']}")
                        boards = {b.casefold() for p in chosen.glob("overrides_*.plist")
                                  for b in p.stem.removeprefix("overrides_").split("_")}
                        self.assertIn(short, boards, f"{product}: проверенный {name} без настроек платы")


class UnitChecks(unittest.TestCase):
    def test_board_name_case_does_not_matter(self):
        catalog = carrier_app.load_catalog()
        for hardware in ("D53gAP", "D53GAP", "d53gap"):
            family = carrier_app.matching_family(catalog, {"product": "iPhone13,2", "hardware": hardware})
            self.assertIsNotNone(family, hardware)
            self.assertEqual(family["name"], "iPhone 12 family")

    def test_windows_cmd_keeps_crlf_line_endings(self):
        """cmd.exe can mis-parse labels/goto in LF-only batch files."""
        data = (Path(__file__).resolve().parents[1] / "windows_assets" / "CarrierSelector.cmd").read_bytes()
        self.assertEqual(data.count(b"\n"), data.count(b"\r\n"))
        self.assertGreater(data.count(b"\r\n"), 10)

    def test_version_tuple_is_tolerant_but_ordered(self):
        vt = install_prepared_pair.version_tuple
        self.assertEqual(vt("73.0"), vt("73"))
        self.assertGreater(vt("73.0.1"), vt("73.0"))
        self.assertLess(vt("47.5a"), vt("48.0"))
        with self.assertRaises(build_selector_ipcc.BuildError):
            vt("")

    def test_plmn_match_accepts_unpadded_mnc(self):
        match = read_phone_carrier_state.imsi_matches_plmn
        self.assertTrue(match("250010000000001", "250", "01"))
        self.assertTrue(match("250010000000001", "250", "1"))
        self.assertTrue(match("310260000000001", "310", "260"))
        self.assertFalse(match("250020000000002", "250", "01"))
        self.assertFalse(match("", "250", "01"))

    def test_duplicate_identifiers_for_two_lines_are_refused(self):
        phone = FakePhone(sims=[sim("kOne", MTS), sim("kTwo", MTS)])
        with phone.attached():
            overview = asyncio.run(read_phone_carrier_state.read_states())
        self.assertTrue(all(line.get("conflict") for line in overview["lines"]))
        self.assertTrue(all(not carrier_app.valid_line(line) for line in overview["lines"]))

    def test_os_junk_inside_bundle_is_ignored_and_signature_still_valid(self):
        source = carrier_app.bundle_path("full-v64/Turkcell_tr.bundle")
        with tempfile.TemporaryDirectory() as temp:
            copy = Path(temp) / "Turkcell_tr.bundle"
            shutil.copytree(source, copy)
            (copy / "._carrier.plist").write_bytes(b"\0\5\26\7AppleDouble")
            (copy / ".DS_Store").write_bytes(b"junk")
            files, _ = build_selector_ipcc.read_bundle(copy)
            self.assertFalse(any(name.startswith("._") or name.endswith(".DS_Store") for name in files))
            build_selector_ipcc.verify_apple_signatures(files)

    def test_appledouble_bundle_files_are_not_listed_as_profiles(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "embedded_catalog" / "junk-root"
            root.mkdir(parents=True)
            shutil.copytree(carrier_app.bundle_path("full-v64/Turkcell_tr.bundle"), root / "Turkcell_tr.bundle")
            (root / "._Turkcell_tr.bundle").write_bytes(b"AppleDouble")
            with mock.patch("carrier_app.resource_root", return_value=Path(temp)):
                rows = carrier_app.catalog_profiles({"name": "junk", "catalog_root": "junk-root"})
        self.assertEqual([row["identifier"] for row in rows], ["com.apple.Turkcell_tr"])

    def test_explain_error_messages(self):
        from pymobiledevice3 import exceptions as e
        self.assertIn("Доверять", carrier_app.explain_error(e.PairingDialogResponsePendingError()))
        self.assertIn("Не доверять", carrier_app.explain_error(e.UserDeniedPairingError()))
        self.assertIn("usbmuxd", carrier_app.explain_error(e.ConnectionFailedToUsbmuxdError())
                      if carrier_app.os.name != "nt" else "usbmuxd")
        self.assertIn("не ответил", carrier_app.explain_error(asyncio.TimeoutError()))

    def test_scrub_removes_identifiers(self):
        text = carrier_app.scrub(f"imsi {MTS['imsi']} iccid {MTS['iccid']} udid 00008150-001A2B3C4D5E6F70")
        self.assertNotIn(MTS["imsi"], text)
        self.assertNotIn(MTS["iccid"], text)
        self.assertNotIn("001A2B3C4D5E6F70", text)


if __name__ == "__main__":
    unittest.main()


class V64Recommendations(unittest.TestCase):
    """v6.4: recommended profiles per operator, MTS Wi-Fi calling check, APN warning."""

    @classmethod
    def setUpClass(cls):
        cls.catalog = carrier_app.load_catalog()

    def offered(self, product, hardware):
        overview = {"product": product, "hardware": hardware}
        family = carrier_app.matching_family(self.catalog, overview)
        return carrier_app.offered_profiles(carrier_app.profiles_for(self.catalog, family, overview))

    def labels(self, product, hardware, mcc, mnc):
        line = {"mcc": mcc, "mnc": mnc, "hardware": hardware}
        return [row["label"].split(" · ")[0]
                for row, _ in carrier_app.recommended_rows(self.offered(product, hardware), line)]

    def test_mts_order_vodafone_tr_first(self):
        self.assertEqual(self.labels("iPhone13,2", "D53gAP", "250", "01"),
                         ["Vodafone_tr 73.0", "Telia_dk 68.0", "Telia_dk 62.0", "Vodafone_ro 73.0"])
        # iPhone 18: no Telia_dk 68.0 / 62.0 with MTS ciphers.
        self.assertEqual(self.labels("iPhone19,2", "V63AP", "250", "1"),
                         ["Vodafone_tr 73.0", "Vodafone_ro 73.0"])
        self.assertEqual(carrier_app.operator_name({"mcc": "250", "mnc": "001"}), "МТС")

    def test_beeline_only_profiles_with_board_settings(self):
        # v6.5: Ufone_pk is not recommended (incoming VoLTE calls fail on Beeline:
        # Preconditions=None without RequirePreconditionsWhenMandatory).
        self.assertEqual(self.labels("iPhone18,2", "V54AP", "250", "99"),
                         ["Ncell_np 73.0", "MobiCom_mn 73.0", "MTN_cy 73.0", "Vodafone_hu 73.0"])
        # iPhone 16e: Ncell_np / MobiCom_mn exist only as a common profile without
        # the V59 modem file (no 5G) — they must not be recommended.
        names = self.labels("iPhone17,5", "V59AP", "250", "99")
        self.assertNotIn("Ncell_np 73.0", names)
        self.assertNotIn("MobiCom_mn 73.0", names)
        self.assertNotIn("Ufone_pk 73.0", names)
        self.assertEqual(names[0], "MTN_cy 73.0")
        for operator in (("250", "02"), ("250", "20"), ("250", "11")):
            self.assertNotIn("Ufone_pk 73.0", self.labels("iPhone18,2", "V54AP", *operator))

    def test_mts_wifi_calling_check(self):
        line = {"mcc": "250", "mnc": "01", "hardware": "D83AP"}
        rows = {(str(r["identifier"]), str(r["version"])): r for r in self.offered("iPhone16,1", "D83AP")}
        ok = lambda ident, ver: carrier_app.mts_wifi_calling(rows[(f"com.apple.{ident}", ver)], line)
        self.assertTrue(ok("Telia_dk", "68.0"))
        self.assertTrue(ok("Telia_dk", "62.0"))
        self.assertTrue(ok("Vodafone_tr", "73.0"))
        self.assertTrue(ok("Vodafone_ro", "73.0"))
        self.assertFalse(ok("Telia_dk", "73.0"))     # AES-256/SHA2-256
        self.assertFalse(ok("MTN_cy", "73.0"))       # iOS default proposals
        self.assertFalse(ok("Vodafone_hu", "73.0"))
        self.assertFalse(ok("MTS_ru", "73.0"))       # Wi-Fi calling only with MTS permission
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            carrier_app.choice_warnings(rows[("com.apple.MTN_cy", "73.0")], line)
        self.assertIn("звонков по Wi-Fi на МТС не будет", out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            carrier_app.choice_warnings(rows[("com.apple.Vodafone_tr", "73.0")], line)
        self.assertEqual(out.getvalue(), "")

    def test_apn_warning(self):
        line = {"mcc": "250", "mnc": "99", "hardware": "V54AP"}
        rows = {str(r["identifier"]): r for r in self.offered("iPhone18,2", "V54AP")}
        self.assertEqual(carrier_app.apn_editable(rows["com.apple.Ncell_np"], line), (False, "web"))
        self.assertTrue(carrier_app.apn_editable(rows["com.apple.Ufone_pk"], line)[0])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            carrier_app.choice_warnings(rows["com.apple.Ncell_np"], line)
        self.assertIn("правка APN недоступна", out.getvalue())

    def test_belarus_recommendations(self):
        self.assertEqual(self.labels("iPhone16,1", "D83AP", "257", "04"), ["MTS_ua 73.0"])
        self.assertEqual(self.labels("iPhone16,1", "D83AP", "257", "02"), ["Telia_dk 73.0"])
        self.assertEqual(self.labels("iPhone16,1", "D83AP", "257", "01"), [])  # A1: built-in profile

    def test_untested_old_telia_is_not_called_tested(self):
        phone = FakePhone(product="iPhone13,2", hardware="D53gAP", sims=[sim("kOne", MTS)])
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as root, \
                mock.patch("carrier_app.state_root", return_value=Path(root)), \
                mock.patch("carrier_app.program_root", return_value=Path(root)), \
                phone.attached(), mock.patch("builtins.input", side_effect=["Telia dk", "Q"]), \
                contextlib.redirect_stdout(output):
            carrier_app.install_interactive(self.catalog)
        out = output.getvalue()
        search = out[out.index("Поиск: Telia dk"):]
        rows = [line for line in search.splitlines() if "Telia_dk 6" in line]
        self.assertEqual(len(rows), 2, out)
        for row in rows:
            self.assertIn("старая версия", row)
            self.assertNotIn("не проверял", row)  # v6.5: no "not tested" remarks
            self.assertNotIn("проверенная старая версия", row)
        # Telia_dk 68.0 on iPhone 15 Pro was checked on a real phone.
        rows15 = [row for row in carrier_app.offered_profiles(carrier_app.profiles_for(
            self.catalog, carrier_app.matching_family(self.catalog, {"product": "iPhone16,1", "hardware": "D83AP"}),
            {"product": "iPhone16,1", "hardware": "D83AP"})) if row["label"] == "Telia_dk 68.0"]
        self.assertTrue(rows15[0].get("tested", True))


class V65Ios26Support(unittest.TestCase):
    """v6.5: iOS 26 support (26.6-26.7), 70.0 rollback, Beeline incoming warning."""

    @classmethod
    def setUpClass(cls):
        cls.catalog = carrier_app.load_catalog()

    def test_ios26_models_offer_70_and_find_rollback(self):
        models = [
            ("iPhone16,1", "D83AP", "iPhone 15 Pro"),
            ("iPhone13,2", "D53gAP", "iPhone 12"),
            ("iPhone18,2", "V54AP", "iPhone 17 Pro Max"),
        ]
        for product, hardware, name in models:
            for ver in ("26.6.2", "26.7.3"):
                state = {"mcc": "250", "mnc": "99", "product": product, "hardware": hardware, "version": ver}
                family = carrier_app.matching_family(self.catalog, state)
                profs = carrier_app.profiles_for(self.catalog, family, state)
                offered = carrier_app.offered_profiles(profs)

                # None of the offered profiles should be >= 72.0
                bad = [p["label"] for p in offered if p.get("version") and carrier_app.version_tuple(p.get("version")) >= (72, 0)]
                self.assertEqual(bad, [], f"{name} on iOS {ver} offers >= 72.x profiles")

                # Native firmware version must be 70.0
                self.assertEqual(carrier_app.native_firmware_version(state, "com.apple.VimpelCom_ru"), "70.0")

                # Rollback package VimpelCom_ru 70.0 must be found
                original = {"bundle_identifier": "com.apple.VimpelCom_ru", "bundle_version": "70.0"}
                rollback = carrier_app.exact_profile(profs, original)
                self.assertIsNotNone(rollback, f"rollback VimpelCom_ru 70.0 not found for {name} on iOS {ver}")
                self.assertEqual(str(rollback.get("version")), "70.0")

                # Beeline recommendations must be 70.0
                recs = carrier_app.recommended_rows(offered, state)
                self.assertEqual(len(recs), 4, f"expected 4 recommendations for {name} on iOS {ver}")
                rec_labels = [r["label"].split(" · ")[0] for r, _ in recs]
                self.assertEqual(rec_labels, ["Ncell_np 70.0", "MobiCom_mn 70.0", "MTN_cy 70.0", "Vodafone_hu 70.0"])

    def test_beeline_incoming_call_warning(self):
        line = {"mcc": "250", "mnc": "99", "hardware": "V54AP", "product": "iPhone18,2"}
        overview = {"product": "iPhone18,2", "hardware": "V54AP"}
        family = carrier_app.matching_family(self.catalog, overview)
        profs = {f"{str(p.get('identifier')).removeprefix('com.apple.')} {p.get('version')}": p
                 for p in carrier_app.profiles_for(self.catalog, family, overview)}

        # breaks_beeline_incoming True for Ufone_pk 73.0 and Algar_br 73.0
        self.assertTrue(carrier_app.breaks_beeline_incoming(profs["Ufone_pk 73.0"], line))
        self.assertTrue(carrier_app.breaks_beeline_incoming(profs["Algar_br 73.0"], line))

        # breaks_beeline_incoming False for Ncell_np 73.0, MTN_cy 73.0, Vodafone_hu 73.0
        self.assertFalse(carrier_app.breaks_beeline_incoming(profs["Ncell_np 73.0"], line))
        self.assertFalse(carrier_app.breaks_beeline_incoming(profs["MTN_cy 73.0"], line))
        self.assertFalse(carrier_app.breaks_beeline_incoming(profs["Vodafone_hu 73.0"], line))

        # Warning is emitted by choice_warnings for Ufone_pk and Algar_br
        for key in ("Ufone_pk 73.0", "Algar_br 73.0"):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                carrier_app.choice_warnings(profs[key], line)
            self.assertIn("входящие звонки на Билайне не проходят", out.getvalue())

        # No warning emitted for recommended profiles
        for key in ("Ncell_np 73.0", "MTN_cy 73.0", "Vodafone_hu 73.0"):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                carrier_app.choice_warnings(profs[key], line)
            self.assertNotIn("входящие звонки на Билайне не проходят", out.getvalue())

