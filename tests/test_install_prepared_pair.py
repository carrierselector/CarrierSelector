import hashlib
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from pymobiledevice3.exceptions import ConnectionTerminatedError, DeviceNotFoundError

from tools.build_selector_ipcc import BuildError, normalized_selector
from tools.install_prepared_pair import (
    InstallResultUnknownError, PreTransferConnectionError, RestartResultUnknownError,
    check_state, restart_once, safe_state, send_once, version_tuple,
)


class InstallPreflightTests(unittest.TestCase):
    def manifest(self):
        selector = normalized_selector("250011234567890", "8970101234567890123")
        return {
            "selector_sha256": hashlib.sha256(selector.encode()).hexdigest(),
            "target": {
                "product": "iPhone16,1", "hardware": "D83AP",
                "version": "27.0.1", "build": "24A446",
            },
        }

    def state(self):
        return {
            "product": "iPhone16,1",
            "hardware": "D83AP",
            "version": "27.0.1",
            "build": "24A446",
            "imsi": "250011234567890",
            "iccid": "8970101234567890123",
            "bundle_identifier": "com.apple.MTS_ru",
        }

    def test_exact_target_and_sim_pass(self):
        check_state(self.state(), self.manifest(), "forward")

    def test_other_build_is_allowed(self):
        state = self.state()
        state.update(version="27.2", build="24B119")
        check_state(state, self.manifest(), "forward")

    def test_other_model_is_allowed(self):
        state = self.state()
        state.update(product="iPhone17,2", hardware="D94AP")
        check_state(state, self.manifest(), "forward")

    def test_other_sim_is_rejected(self):
        state = self.state()
        state["iccid"] = state["iccid"][:-1] + "4"
        with self.assertRaisesRegex(BuildError, "SIM does not match"):
            check_state(state, self.manifest(), "forward")

    def test_safe_state_omits_sim_identifiers(self):
        state = self.state()
        state.update(imsi="secret-imsi", iccid="secret-iccid")
        encoded = json.dumps(safe_state(state))
        self.assertNotIn("secret-imsi", encoded)
        self.assertNotIn("secret-iccid", encoded)

    def test_numeric_bundle_versions(self):
        self.assertGreater(version_tuple("73.0"), version_tuple("72.0"))

    def test_family_target_accepts_listed_board(self):
        manifest = self.manifest()
        manifest["target"] = {
            "products": ["iPhone16,1", "iPhone16,2"],
            "hardwares": ["D83AP", "D84AP"],
            "version": "27.0.1", "build": "24A446",
        }
        check_state(self.state(), manifest, "forward")


class SendOnceReconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_device_disappearing_before_transfer_is_reconnected_without_resend(self):
        candidate = SimpleNamespace(connection_type="USB", serial="phone")
        phone = SimpleNamespace(close=mock.AsyncMock())
        installed = mock.AsyncMock()

        class Service:
            def __init__(self, _phone):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return False

            install_from_bytes = installed

        with mock.patch("pymobiledevice3.usbmux.list_devices", mock.AsyncMock(return_value=[candidate])), \
                mock.patch("pymobiledevice3.lockdown.create_using_usbmux", mock.AsyncMock(
                    side_effect=[DeviceNotFoundError("transient"), phone]
                )) as connect, \
                mock.patch("pymobiledevice3.services.installation_proxy.InstallationProxyService", Service), \
                mock.patch("tools.install_prepared_pair.asyncio.sleep", mock.AsyncMock()):
            await send_once(b"ipcc")

        self.assertEqual(connect.await_count, 2)
        installed.assert_awaited_once_with(b"ipcc")
        phone.close.assert_awaited_once()

    async def test_service_failure_before_transfer_is_safe_to_retry(self):
        candidate = SimpleNamespace(connection_type="USB", serial="phone")
        phone = SimpleNamespace(close=mock.AsyncMock())

        class Service:
            def __init__(self, _phone):
                pass

            async def __aenter__(self):
                raise DeviceNotFoundError("service unavailable")

            async def __aexit__(self, *_):
                return False

        with mock.patch("pymobiledevice3.usbmux.list_devices", mock.AsyncMock(return_value=[candidate])), \
                mock.patch("pymobiledevice3.lockdown.create_using_usbmux", mock.AsyncMock(return_value=phone)), \
                mock.patch("pymobiledevice3.services.installation_proxy.InstallationProxyService", Service):
            with self.assertRaises(PreTransferConnectionError):
                await send_once(b"ipcc", expected_serial="phone")
        phone.close.assert_awaited_once()

    async def test_any_failure_after_transfer_starts_is_never_treated_as_safe_retry(self):
        candidate = SimpleNamespace(connection_type="USB", serial="phone")
        phone = SimpleNamespace(close=mock.AsyncMock())
        installed = mock.AsyncMock(side_effect=ConnectionTerminatedError("closed"))

        class Service:
            def __init__(self, _phone):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return False

            install_from_bytes = installed

        with mock.patch("pymobiledevice3.usbmux.list_devices", mock.AsyncMock(return_value=[candidate])), \
                mock.patch("pymobiledevice3.lockdown.create_using_usbmux", mock.AsyncMock(return_value=phone)), \
                mock.patch("pymobiledevice3.services.installation_proxy.InstallationProxyService", Service):
            with self.assertRaises(InstallResultUnknownError):
                await send_once(b"ipcc", expected_serial="phone")
        installed.assert_awaited_once_with(b"ipcc")

    async def test_service_close_after_reported_completion_does_not_resend_or_fail(self):
        candidate = SimpleNamespace(connection_type="USB", serial="phone")
        phone = SimpleNamespace(close=mock.AsyncMock())
        installed = mock.AsyncMock()

        class Service:
            def __init__(self, _phone):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                raise ConnectionTerminatedError("closed after completion")

            install_from_bytes = installed

        with mock.patch("pymobiledevice3.usbmux.list_devices", mock.AsyncMock(return_value=[candidate])), \
                mock.patch("pymobiledevice3.lockdown.create_using_usbmux", mock.AsyncMock(return_value=phone)), \
                mock.patch("pymobiledevice3.services.installation_proxy.InstallationProxyService", Service):
            await send_once(b"ipcc", expected_serial="phone")
        installed.assert_awaited_once_with(b"ipcc")


class RestartReconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_restart_reconnects_before_command_and_sends_command_once(self):
        candidate = SimpleNamespace(connection_type="USB", serial="phone")
        phone = SimpleNamespace(close=mock.AsyncMock())
        restarted = mock.AsyncMock()

        class Service:
            def __init__(self, _phone):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return False

            restart = restarted

        with mock.patch("pymobiledevice3.usbmux.list_devices", mock.AsyncMock(return_value=[candidate])), \
                mock.patch("pymobiledevice3.lockdown.create_using_usbmux", mock.AsyncMock(
                    side_effect=[DeviceNotFoundError("transient"), phone]
                )) as connect, \
                mock.patch("pymobiledevice3.services.diagnostics.DiagnosticsService", Service), \
                mock.patch("tools.install_prepared_pair.asyncio.sleep", mock.AsyncMock()):
            await restart_once(expected_serial="phone")
        self.assertEqual(connect.await_count, 2)
        restarted.assert_awaited_once_with()

    async def test_restart_disconnect_after_command_is_ambiguous_and_not_resent(self):
        candidate = SimpleNamespace(connection_type="USB", serial="phone")
        phone = SimpleNamespace(close=mock.AsyncMock())
        restarted = mock.AsyncMock(side_effect=ConnectionTerminatedError("restarting"))

        class Service:
            def __init__(self, _phone):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return False

            restart = restarted

        with mock.patch("pymobiledevice3.usbmux.list_devices", mock.AsyncMock(return_value=[candidate])), \
                mock.patch("pymobiledevice3.lockdown.create_using_usbmux", mock.AsyncMock(return_value=phone)), \
                mock.patch("pymobiledevice3.services.diagnostics.DiagnosticsService", Service):
            with self.assertRaises(RestartResultUnknownError):
                await restart_once(expected_serial="phone")
        restarted.assert_awaited_once_with()


if __name__ == "__main__":
    unittest.main()
