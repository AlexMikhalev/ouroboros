"""Pin native process/manifest contracts; physical lifecycle tests run on Android."""
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET


HOST = Path(__file__).resolve().parents[1] / "host"
A = "{http://schemas.android.com/apk/res/android}"


class AndroidHostTest(unittest.TestCase):
    def test_assistant_entry_and_user_role_flow_are_declared(self):
        manifest = ET.parse(HOST / "AndroidManifest.xml").getroot()
        activity = manifest.find("application/activity")
        actions = {
            action.get(A + "name")
            for intent_filter in activity.findall("intent-filter")
            for action in intent_filter.findall("action")
        }
        self.assertIn("android.intent.action.ASSIST", actions)
        source = (HOST / "src/ai/ouroboros/android/MainActivity.java").read_text()
        self.assertIn("createRequestRoleIntent(RoleManager.ROLE_ASSISTANT)", source)
        self.assertIn("isRoleHeld(RoleManager.ROLE_ASSISTANT)", source)

    def test_location_bridge_has_state_and_bounded_current_fix_methods(self):
        source = (HOST / "src/ai/ouroboros/android/AndroidBridge.java").read_text()
        self.assertIn('"location.state"', source)
        self.assertIn('"location.get"', source)
        self.assertIn("getCurrentLocation", source)
        self.assertIn("no_fix_within_timeout", source)

    def test_cleartext_is_loopback_only(self):
        manifest = ET.parse(HOST / "AndroidManifest.xml").getroot()
        app = manifest.find("application")
        self.assertEqual(app.get(A + "usesCleartextTraffic"), "false")
        self.assertEqual(app.get(A + "networkSecurityConfig"), "@xml/network_security_config")
        config = (HOST / "res/xml/network_security_config.xml").read_text()
        self.assertIn('cleartextTrafficPermitted="false"', config)
        self.assertIn("localhost", config)
        self.assertIn("127.0.0.1", config)

    def test_bridge_lifetime_does_not_load_webview_and_can_recover_after_host_update(self):
        manifest = ET.parse(HOST / "AndroidManifest.xml").getroot()
        app = manifest.find("application")
        service = app.find("service")
        self.assertEqual(service.get(A + "process"), ":native")
        self.assertEqual(service.get(A + "exported"), "false")
        self.assertIsNone(app.find("activity").get(A + "process"))
        receiver = app.find("receiver")
        self.assertEqual(receiver.get(A + "process"), ":native")
        actions = {item.get(A + "name") for item in receiver.findall("intent-filter/action")}
        self.assertEqual(actions, {"android.intent.action.BOOT_COMPLETED", "android.intent.action.MY_PACKAGE_REPLACED"})

    def test_alarm_and_initial_data_capabilities_are_declared(self):
        manifest = ET.parse(HOST / "AndroidManifest.xml").getroot()
        permissions = {row.get(A + "name"): row for row in manifest.findall("uses-permission")}
        self.assertIn("com.android.alarm.permission.SET_ALARM", permissions)
        self.assertIn("android.permission.ACCESS_NETWORK_STATE", permissions)
        self.assertEqual(permissions["android.permission.READ_EXTERNAL_STORAGE"].get(A + "maxSdkVersion"), "32")
        for permission in ("READ_CONTACTS", "WRITE_CONTACTS", "READ_CALENDAR", "WRITE_CALENDAR",
                           "CAMERA", "RECORD_AUDIO", "ACCESS_COARSE_LOCATION", "ACCESS_FINE_LOCATION"):
            self.assertIn("android.permission." + permission, permissions)


if __name__ == "__main__":
    unittest.main()
