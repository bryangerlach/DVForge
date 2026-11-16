"""Self-hosted update support: config plumbing + RustDesk source patches."""

import os
import tempfile
import unittest

from builder import config_gen, update_patch


class ConfigEnvTests(unittest.TestCase):
    def test_update_fields_disable_removed_notification(self):
        env = config_gen.build_custom_env({
            "appname": "Example",
            "downloadLink": "https://api.example.com/",
            "removeNewVersionNotif": "on",
            "update_public_key": "q83vASNFZ4mrze8=",
            "app_version": "1.5.1+016",
        })
        self.assertEqual(env["CUSTOM_UPDATE_URL"],
                         "https://api.example.com/api/version/latest")
        self.assertEqual(env["CUSTOM_UPDATE_PUBLIC_KEY"], "q83vASNFZ4mrze8=")
        self.assertEqual(env["CUSTOM_APP_VERSION"], "1.5.1+016")
        # An update-enabled client must keep checking.
        self.assertEqual(env["CUSTOM_REMOVE_NEW_VERSION_NOTIF"], "false")

    def test_no_key_leaves_update_disabled(self):
        env = config_gen.build_custom_env({
            "appname": "Example",
            "downloadLink": "https://rustdesk.com/download",
            "removeNewVersionNotif": "on",
        })
        self.assertEqual(env["CUSTOM_UPDATE_URL"], "")
        self.assertEqual(env["CUSTOM_UPDATE_PUBLIC_KEY"], "")
        self.assertEqual(env["CUSTOM_REMOVE_NEW_VERSION_NOTIF"], "true")

    def test_explicit_update_url_wins(self):
        env = config_gen.build_custom_env({
            "appname": "Example",
            "downloadLink": "https://downloads.example.com",
            "update_public_key": "q83vASNFZ4mrze8=",
            "update_url": "https://updates.example.com/api/version/latest",
        })
        self.assertEqual(env["CUSTOM_UPDATE_URL"],
                         "https://updates.example.com/api/version/latest")


class SourcePatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        os.makedirs(os.path.join(self.root, "libs", "hbb_common", "src"))
        os.makedirs(os.path.join(self.root, "src", "updater"))
        self._write("libs/hbb_common/src/lib.rs",
                    'const URL: &str = "https://api.rustdesk.com/version/latest";\n\n'
                    + update_patch._GET_VERSION_OLD + "\n\n"
                    + update_patch._GET_VERSION_TEST_OLD + "\n")
        self._write("src/common.rs",
                    "pub fn check_software_update() {\n"
                    "    if is_custom_client() {\n"
                    "        return;\n"
                    "    }\n"
                    "}\n\n"
                    '    if url.scheme() != "https" || url.host_str() != Some("github.com") {\n'
                    '        bail!("host");\n'
                    "    }\n\n"
                    + update_patch._DISPLAY_VERSION_OLD + "\n")
        self._write("src/updater/artifact.rs",
                    'let expected_artifact_url_prefix =\n'
                    '        format!("https://github.com/rustdesk/rustdesk/releases/download/{release_id}/");\n\n'
                    '    if !url.starts_with("https://github.com/")\n'
                    '        || parsed.scheme() != "https"\n'
                    '        || parsed.host_str() != Some("github.com")\n'
                    '        || !parsed.username().is_empty()\n'
                    '        || parsed.password().is_some()\n'
                    '        || parsed.port().is_some()\n'
                    '        || parsed.query().is_some()\n'
                    '        || parsed.fragment().is_some()\n'
                    '    {\n'
                    '        return None;\n'
                    '    }\n')
        self._write("src/update_metadata.rs",
                    "const TRUSTED_UPDATE_KEYS: &[TrustedUpdateKey] = &["
                    'TrustedUpdateKey { key_id: "main" }];\n')
        self._write("Cargo.toml", '[package]\nname = "rustdesk"\nversion = "1.5.1"\n')
        self._write("Cargo.lock",
                    '[[package]]\nname = "rustdesk"\nversion = "1.5.1"\n')
        self.key = "q83vASNFZ4mrze8BI0VniavN7wEjRWeJq83vASNFZ4k="

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, rel, text):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def _read(self, rel):
        with open(os.path.join(self.root, rel), encoding="utf-8") as f:
            return f.read()

    def test_patches_all_update_points(self):
        warnings = update_patch.apply_custom_update(
            self.root, "https://api.example.com/api/version/latest", self.key,
            log=lambda _m: None)
        self.assertEqual(warnings, [])

        lib = self._read("libs/hbb_common/src/lib.rs")
        self.assertIn("https://api.example.com/api/version/latest", lib)
        self.assertNotIn("https://api.rustdesk.com/version/latest", lib)
        self.assertIn("split_once('+')", lib)

        common = self._read("src/common.rs")
        self.assertNotIn("is_custom_client() {\n        return;", common)
        self.assertIn("if false {", common)
        self.assertIn("split_once('+')", common)

        artifact = self._read("src/updater/artifact.rs")
        self.assertIn(
            "https://api.example.com/rustdesk/rustdesk/releases/download/{release_id}/",
            artifact)

        metadata = self._read("src/update_metadata.rs")
        self.assertIn('key_id: "custom-update"', metadata)
        self.assertIn("171, 205, 239, 1, 35, 69, 103, 137", metadata)

        # Re-applying to a reused source tree (multi-platform job) is a no-op.
        self.assertEqual(update_patch.apply_custom_update(
            self.root, "https://api.example.com/api/version/latest", self.key,
            log=lambda _m: None), [])

    def test_app_version_updates_cargo(self):
        self.assertTrue(update_patch.apply_app_version(
            self.root, "1.5.1+016", log=lambda _m: None))
        self.assertIn('version = "1.5.1+016"', self._read("Cargo.toml"))
        self.assertIn('version = "1.5.1+016"', self._read("Cargo.lock"))

    def test_invalid_app_version_is_rejected(self):
        self.assertFalse(update_patch.apply_app_version(
            self.root, "not-a-version", log=lambda _m: None))
        self.assertIn('version = "1.5.1"', self._read("Cargo.toml"))


if __name__ == "__main__":
    unittest.main()
