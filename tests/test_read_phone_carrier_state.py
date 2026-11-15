import unittest
from types import SimpleNamespace
from unittest import mock

from tools.read_phone_carrier_state import read_states


class ReadPhoneCarrierStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_kTwo_row_never_borrows_unsuffixed_other_sim_identifiers(self):
        candidate = SimpleNamespace(connection_type="USB", serial="private-udid")
        values = {
            "ProductType": "iPhone19,3",
            "HardwareModel": "V64AP",
            "ProductVersion": "27.2.1",
            "BuildVersion": "24B156",
            "InternationalMobileSubscriberIdentity": "250019999999999",
            "IntegratedCircuitCardIdentity": "8970199999999999999",
            "InternationalMobileSubscriberIdentity2": "250029999999999",
            "IntegratedCircuitCardIdentity2": "8970299999999999999",
        }
        rows = [{
            "Slot": "kTwo",
            "CFBundleIdentifier": "com.apple.MegaFon_ru",
            "CFBundleVersion": "72.0",
            "MCC": "250",
            "MNC": "02",
        }]

        async def get_value(*, key=None):
            if key is None:
                return values
            if key == "CarrierBundleInfoArray":
                return rows
            return values.get(key, "")

        phone = SimpleNamespace(
            get_value=mock.AsyncMock(side_effect=get_value),
            close=mock.AsyncMock(),
        )
        with mock.patch("pymobiledevice3.usbmux.list_devices", mock.AsyncMock(return_value=[candidate])), \
                mock.patch("pymobiledevice3.lockdown.create_using_usbmux", mock.AsyncMock(return_value=phone)):
            overview = await read_states()

        self.assertEqual(overview["udid"], "private-udid")
        self.assertEqual(overview["lines"][0]["imsi"], values["InternationalMobileSubscriberIdentity2"])
        self.assertEqual(overview["lines"][0]["iccid"], values["IntegratedCircuitCardIdentity2"])
        self.assertNotEqual(overview["lines"][0]["imsi"], values["InternationalMobileSubscriberIdentity"])


if __name__ == "__main__":
    unittest.main()
