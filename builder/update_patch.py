"""
update_patch.py — self-hosted RustDesk auto-updates.

This is a port of rdgen's `.github/patches/allow_custom_update.py`, adapted to
DVForge's source tree and logging. It is applied by `customize.apply()` when
the client config contains an `update_public_key` (normally injected by
rustdesk-api when a build is created).

What it changes:

  1. libs/hbb_common/src/lib.rs
       - hardcoded update check URL -> the self-hosted server
       - get_version_number() -> order build-metadata versions (1.5.1+016)
  2. src/common.rs
       - allow update checks for custom-branded clients
       - relax the github.com-only host check
       - accept +build metadata in display_version_from_release_id()
  3. src/updater/artifact.rs
       - retarget the hardcoded GitHub download prefix to the self-hosted server
       - relax the GitHub-only download host check
  4. src/update_metadata.rs
       - add the server's ed25519 public key to TRUSTED_UPDATE_KEYS
  5. Flutter sources
       - remove the is_custom_client() gates around the startup check, the
         update card/banner and the "check on startup" toggles
       - open the release page returned by the update server where no
         in-place updater exists

The matching server contract is implemented by rustdesk-api:

  * POST <base>/api/version/latest returns {"url": ".../releases/tag/<id>"}
  * <base>/rustdesk/rustdesk/releases/download/<id>/rustdesk-update.json
  * <base>/rustdesk/rustdesk/releases/download/<id>/rustdesk-update.json.sig
  * <id> is "v" + <version> + "+" + <build id padded to 3 digits>
"""

import base64
import os
import re
from urllib.parse import urlparse


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _read(path):
    with open(path, "r", encoding="utf-8", errors="surrogateescape") as f:
        return f.read()


def _write(path, text):
    with open(path, "w", encoding="utf-8", errors="surrogateescape") as f:
        f.write(text)


def _replace(path, old, new, count=-1):
    """Literal replace. Returns True when the file changed."""
    if not os.path.isfile(path):
        return False
    text = _read(path)
    if old not in text:
        return False
    _write(path, text.replace(old, new, count))
    return True


def _replace_regex(path, pattern, repl, count=1):
    """Regex replace. `repl` is a callable receiving the match."""
    if not os.path.isfile(path):
        return False
    text = _read(path)
    new_text, n = re.subn(pattern, repl, text, count=count)
    if not n:
        return False
    _write(path, new_text)
    return True


def _warn(log, msg):
    log(f"    ! self-hosted update: {msg}")


# ---------------------------------------------------------------------------
# hbb_common: update URL + build-metadata ordering
# ---------------------------------------------------------------------------

def _change_update_url(root, update_url, log):
    """Point the version check at the self-hosted rustdesk-api server."""
    target = os.path.join(root, "libs", "hbb_common", "src", "lib.rs")
    if not os.path.isfile(target):
        _warn(log, "libs/hbb_common/src/lib.rs not found (is the submodule "
                   "checked out?) — update URL not changed")
        return False
    if update_url in _read(target):
        # Source tree reused for another platform in the same job.
        return True

    patterns = (
        # Older releases: pub const UPDATE_URL: &str = "...";
        (r'(pub\s+const\s+UPDATE_URL\s*[:=]\s*")[^"]*(")',
         lambda m: m.group(1) + update_url + m.group(2)),
        (r'(UPDATE_URL\s*=\s*")[^"]*(")',
         lambda m: m.group(1) + update_url + m.group(2)),
        # Newer hbb_common: const URL: &str = "https://api.rustdesk.com/...";
        (r'https?://api\.rustdesk\.com/version/latest',
         lambda m: update_url),
    )
    for pattern, repl in patterns:
        if _replace_regex(target, pattern, repl):
            log(f"    · {os.path.basename(target)}: update URL -> {update_url}")
            return True

    _warn(log, "could not find the UPDATE_URL constant in hbb_common/src/lib.rs")
    return False


_GET_VERSION_OLD = '''// Support 1.1.10-1, the number after - is a patch version.
pub fn get_version_number(v: &str) -> i64 {
    let mut versions = v.split('-');

    let mut n = 0;

    // The first part is the version number.
    // 1.1.10 -> 1001100, 1.2.3 -> 1001030, multiple the last number by 10
    // to leave space for patch version.
    if let Some(v) = versions.next() {
        let mut last = 0;
        for x in v.split('.') {
            last = x.parse::<i64>().unwrap_or(0);
            n = n * 1000 + last;
        }
        n -= last;
        n += last * 10;
    }

    if let Some(v) = versions.next() {
        n += v.parse::<i64>().unwrap_or(0);
    }

    // Ignore the rest

    n
}'''

_GET_VERSION_NEW = '''// Support 1.1.10-1, the number after - is a patch version.
// Build metadata after `+` (e.g. 1.5.1+20) is an ordered build number, so
// 1.5.1+20 > 1.5.1+10 > 1.5.1.
pub fn get_version_number(v: &str) -> i64 {
    let mut versions = v.split('-');

    let mut n = 0;

    // The first part is the version number.
    // 1.1.10 -> 1001100, 1.2.3 -> 1001030, multiple the last number by 10
    // to leave space for patch version.
    if let Some(v) = versions.next() {
        let (base, build) = v.split_once('+').unwrap_or((v, ""));
        let mut last = 0;
        for x in base.split('.') {
            last = x.parse::<i64>().unwrap_or(0);
            n = n * 1000 + last;
        }
        n -= last;
        n += last * 10;

        if !build.is_empty() {
            n += build.parse::<i64>().unwrap_or(0);
        }
    }

    if let Some(v) = versions.next() {
        n += v.parse::<i64>().unwrap_or(0);
    }

    // Ignore the rest

    n
}'''

_GET_VERSION_TEST_OLD = '''    #[test]
    fn test_get_version_number() {
        assert_eq!(get_version_number("1.1.10"), 1001100);
        assert_eq!(get_version_number("1.1.10-1"), 1001101);
        assert_eq!(get_version_number("1.1.11-1"), 1001111);
        assert_eq!(get_version_number("1.2.3"), 1002030);
    }'''

_GET_VERSION_TEST_NEW = '''    #[test]
    fn test_get_version_number() {
        assert_eq!(get_version_number("1.1.10"), 1001100);
        assert_eq!(get_version_number("1.1.10-1"), 1001101);
        assert_eq!(get_version_number("1.1.11-1"), 1001111);
        assert_eq!(get_version_number("1.2.3"), 1002030);
        // Build metadata is an ordered build number.
        assert_eq!(get_version_number("1.5.1+20"), 1005030);
        assert_eq!(get_version_number("1.5.1+10"), 1005020);
        assert_eq!(get_version_number("1.5.1"), 1005010);
        assert!(get_version_number("1.5.1+20") > get_version_number("1.5.1+10"));
        assert!(get_version_number("1.5.1+10") > get_version_number("1.5.1"));
    }'''


def _modify_get_version_number(root, log):
    target = os.path.join(root, "libs", "hbb_common", "src", "lib.rs")
    if os.path.isfile(target) and "let (base, build) = v.split_once('+')" in _read(target):
        return True
    if not _replace(target, _GET_VERSION_OLD, _GET_VERSION_NEW, 1):
        _warn(log, "could not find get_version_number() block to update")
        return False
    log("    · hbb_common: +build metadata ordered in get_version_number()")
    # Keep the unit test in sync when this release ships one.
    if _replace(target, _GET_VERSION_TEST_OLD, _GET_VERSION_TEST_NEW, 1):
        log("    · hbb_common: test_get_version_number() updated")
    return True


def _normalize_update_platform(root, log):
    """Report the platform name instead of a Linux distribution id.

    version_check_request() uses System::distribution_id(), which on Linux is
    the /etc/os-release ID ("ubuntu", "debian", ...). rustdesk-api keys update
    metadata on "linux", so those checks would be rejected. Windows, macOS and
    Android already report std::env::consts::OS.
    """
    target = os.path.join(root, "libs", "hbb_common", "src", "lib.rs")
    if not os.path.isfile(target):
        _warn(log, "libs/hbb_common/src/lib.rs not found — update platform "
                   "not normalized")
        return False
    if "let os = std::env::consts::OS.to_string();" in _read(target):
        return True
    if _replace(target, "let os = system.distribution_id();",
                "let os = std::env::consts::OS.to_string();", 1):
        log("    · hbb_common: update checks report the platform name")
        return True
    _warn(log, "could not find distribution_id() assignment to normalize")
    return False


# ---------------------------------------------------------------------------
# Flutter sources: remove the is_custom_client() gates around updates
# ---------------------------------------------------------------------------

def _replace_gate_before(path, marker, old, new, label, log):
    """Replace the last `old` appearing before `marker` in `path`."""
    if not os.path.isfile(path):
        _warn(log, f"{path} not found — {label} not enabled")
        return False
    text = _read(path)
    marker_idx = text.rfind(marker)
    if marker_idx < 0:
        _warn(log, f"could not find {label} marker in {path}")
        return False
    gate_idx = text.rfind(old, 0, marker_idx)
    if gate_idx < 0:
        # Already patched (source tree reused for another platform).
        if text.rfind(new, 0, marker_idx) >= 0:
            return True
        _warn(log, f"could not find {label} gate in {path}")
        return False
    _write(path, text[:gate_idx] + new + text[gate_idx + len(old):])
    log(f"    · {label}")
    return True


def _patch_flutter_update_gates(root, log):
    """Remove the Flutter-side is_custom_client() gates around updates.

    Removing the Rust guard in check_software_update() is not enough: the Dart
    caller only invokes mainGetSoftwareUpdateUrl() when bind.isCustomClient()
    is false, so a custom-branded client never contacts the update server at
    all. The update card, the mobile banner and the "check on startup" toggles
    are gated the same way.
    """
    ok = True
    flutter = os.path.join(root, "flutter", "lib")

    # Startup check: common.dart checkUpdate()
    target = os.path.join(flutter, "common.dart")
    if not os.path.isfile(target):
        _warn(log, "flutter/lib/common.dart not found — startup update check "
                   "not enabled")
        ok = False
    else:
        text = _read(target)
        pattern = (r"(void\s+checkUpdate\(\)\s*\{\s*if\s*\(!isWeb\)\s*\{\s*)"
                   r"if\s*\(!bind\.isCustomClient\(\)\)\s*\{")
        new_text, n = re.subn(pattern, r"\1if (true) {", text, count=1)
        if n:
            _write(target, new_text)
            log("    · common.dart: startup update check enabled")
        elif not re.search(
                r"void\s+checkUpdate\(\)\s*\{\s*if\s*\(!isWeb\)\s*\{\s*"
                r"if\s*\(true\)\s*\{", text):
            _warn(log, "could not find the startup update check gate in "
                       "flutter/lib/common.dart")
            ok = False

    # Desktop update card: desktop_home_page.dart buildHelpCards()
    target = os.path.join(flutter, "desktop", "pages", "desktop_home_page.dart")
    if not os.path.isfile(target):
        _warn(log, "flutter/lib/desktop/pages/desktop_home_page.dart not "
                   "found — desktop update card not enabled")
        ok = False
    else:
        text = _read(target)
        pattern = (r"if\s*\(!bind\.isCustomClient\(\)\s*&&\s*"
                   r"updateUrl\.isNotEmpty\s*&&\s*!isCardClosed\s*&&\s*"
                   r"bind\.mainUriPrefixSync\(\)\.contains\('rustdesk'\)\)")
        new_text, n = re.subn(
            pattern, "if (updateUrl.isNotEmpty && !isCardClosed)",
            text, count=1)
        if n:
            _write(target, new_text)
            log("    · desktop_home_page.dart: update card enabled")
        elif "if (updateUrl.isNotEmpty && !isCardClosed)" not in text:
            _warn(log, "could not find the update card gate in "
                       "flutter/lib/desktop/pages/desktop_home_page.dart")
            ok = False

    # Mobile update banner
    if not _replace_gate_before(
            os.path.join(flutter, "mobile", "pages", "connection_page.dart"),
            "_buildUpdateUI", "if (!bind.isCustomClient() && !isIOS)",
            "if (!isIOS)", "connection_page.dart: mobile update banner enabled",
            log):
        ok = False

    # Desktop "Check for software update on startup" toggle
    if not _replace_gate_before(
            os.path.join(flutter, "desktop", "pages", "desktop_setting_page.dart"),
            "kOptionEnableCheckUpdate",
            "if (!isWeb && !bind.isCustomClient())", "if (!isWeb)",
            "desktop_setting_page.dart: update toggle enabled", log):
        ok = False

    # Mobile "Check for software update on startup" toggle
    if not _replace_gate_before(
            os.path.join(flutter, "mobile", "pages", "settings_page.dart"),
            "kOptionEnableCheckUpdate", "if (!bind.isCustomClient()) {",
            "if (true) {", "settings_page.dart: update toggle enabled", log):
        ok = False

    # Platforms without an in-place updater open the release page returned by
    # the update server, so the user can pick the package for their distro/arch.
    for rel, old, new, label in (
        (os.path.join("desktop", "pages", "desktop_home_page.dart"),
         "final Uri url = Uri.parse('https://rustdesk.com/download');",
         "final Uri url = Uri.parse(updateUrl);",
         "desktop_home_page.dart: release button opens the update server"),
        (os.path.join("mobile", "pages", "connection_page.dart"),
         "final url = 'https://rustdesk.com/download';",
         "final url = updateUrl;",
         "connection_page.dart: release button opens the update server"),
    ):
        target = os.path.join(flutter, rel)
        if not os.path.isfile(target):
            _warn(log, f"flutter/lib/{rel} not found — release button not "
                       "retargeted")
            ok = False
            continue
        text = _read(target)
        if new in text:
            continue
        if _replace(target, old, new, 1):
            log(f"    · {label}")
        else:
            _warn(log, f"could not find the static release URL in "
                       f"flutter/lib/{rel}")
            ok = False

    return ok


# ---------------------------------------------------------------------------
# src/common.rs: allow updates for branded clients + release-id parsing
# ---------------------------------------------------------------------------

def _allow_updates_for_custom_clients(root, log):
    target = os.path.join(root, "src", "common.rs")
    old = '''pub fn check_software_update() {
    if is_custom_client() {
        return;
    }
'''
    new = '''pub fn check_software_update() {
'''
    text = _read(target) if os.path.isfile(target) else ""
    if old not in text and "pub fn check_software_update() {" in text:
        return True  # already patched (source tree reused)
    if _replace(target, old, new, 1):
        log("    · common.rs: update checks enabled for branded clients")
        return True
    _warn(log, "could not find the is_custom_client() guard in check_software_update()")
    return False


def _relax_common_host_check(root, log):
    target = os.path.join(root, "src", "common.rs")
    old = 'if url.scheme() != "https" || url.host_str() != Some("github.com") {'
    new = 'if false {'
    text = _read(target) if os.path.isfile(target) else ""
    if old not in text and "if false {" in text:
        return True  # already patched
    if _replace(target, old, new):
        log("    · common.rs: release URL host check relaxed")
        return True
    _warn(log, "could not find the github.com host check in src/common.rs")
    return False


_DISPLAY_VERSION_OLD = '''pub(crate) fn display_version_from_release_id(release_id: &str) -> ResultType<String> {
    let display_version = release_id.strip_prefix('v').unwrap_or(release_id);
    let segments = display_version.split('.').collect::<Vec<_>>();
    if segments.len() != 3
        || segments.iter().any(|segment| {
            segment.is_empty()
                || !segment.chars().all(|c| c.is_ascii_digit())
                || segment.parse::<u16>().is_err()
        })
    {
        bail!("Release id is not a stable display version: {}", release_id);
    }
    if get_version_number(display_version) <= 0 {
        bail!("Release id has invalid display version: {}", release_id);
    }
    Ok(display_version.to_owned())
}'''

_DISPLAY_VERSION_NEW = '''pub(crate) fn display_version_from_release_id(release_id: &str) -> ResultType<String> {
    let display_version = release_id.strip_prefix('v').unwrap_or(release_id);
    // Validate the base version (3 dot-separated numeric components). Build
    // metadata after `+` (e.g. 1.5.1+20) is allowed and kept in the returned
    // version so get_version_number() can order builds.
    let (base, _build) = display_version.split_once('+').unwrap_or((display_version, ""));
    let segments = base.split('.').collect::<Vec<_>>();
    if segments.len() != 3
        || segments.iter().any(|segment| {
            segment.is_empty()
                || !segment.chars().all(|c| c.is_ascii_digit())
                || segment.parse::<u16>().is_err()
        })
    {
        bail!("Release id is not a stable display version: {}", release_id);
    }
    if get_version_number(display_version) <= 0 {
        bail!("Release id has invalid display version: {}", release_id);
    }
    Ok(display_version.to_owned())
}'''


def _modify_display_version(root, log):
    target = os.path.join(root, "src", "common.rs")
    if os.path.isfile(target) and "let (base, _build) = display_version.split_once('+')" in _read(target):
        return True
    if _replace(target, _DISPLAY_VERSION_OLD, _DISPLAY_VERSION_NEW, 1):
        log("    · common.rs: +build metadata accepted in release ids")
        return True
    _warn(log, "could not find display_version_from_release_id() to update")
    return False


# ---------------------------------------------------------------------------
# src/updater/artifact.rs: self-hosted metadata + artifact URLs
# ---------------------------------------------------------------------------

def _relax_artifact_host_check(root, log):
    target = os.path.join(root, "src", "updater", "artifact.rs")
    old = '''    if !url.starts_with("https://github.com/")
        || parsed.scheme() != "https"
        || parsed.host_str() != Some("github.com")
        || !parsed.username().is_empty()
        || parsed.password().is_some()
        || parsed.port().is_some()
        || parsed.query().is_some()
        || parsed.fragment().is_some()
    {
        return None;
    }'''
    new = '''    if false {
        return None;
    }'''
    text = _read(target) if os.path.isfile(target) else ""
    if old not in text and new in text:
        return True  # already patched
    if _replace(target, old, new, 1):
        log("    · updater/artifact.rs: download host check relaxed")
        return True
    _warn(log, "could not find the GitHub download host check in updater/artifact.rs")
    return False


def _retarget_artifact_base(root, update_url, log):
    parsed = urlparse(update_url)
    if not parsed.scheme or not parsed.netloc:
        _warn(log, f"cannot derive an origin from update URL: {update_url}")
        return False
    base = f"{parsed.scheme}://{parsed.netloc}"
    target = os.path.join(root, "src", "updater", "artifact.rs")

    old = ('let expected_artifact_url_prefix =\n'
           '        format!("https://github.com/rustdesk/rustdesk/releases/download/{release_id}/");')
    new = ('let expected_artifact_url_prefix =\n'
           f'        format!("{base}/rustdesk/rustdesk/releases/download/{{release_id}}/");')
    if os.path.isfile(target) and new in _read(target):
        return True  # already patched
    if _replace(target, old, new, 1):
        log(f"    · updater/artifact.rs: metadata/artifact base -> {base}")
        return True

    # Tolerate formatting changes in newer RustDesk versions.
    pattern = (r'format!\("https://github\.com/rustdesk/rustdesk/releases/download/'
               r'\{release_id\}/"\)')
    if _replace_regex(
        target, pattern,
        lambda m: f'format!("{base}/rustdesk/rustdesk/releases/download/{{release_id}}/")',
    ):
        log(f"    · updater/artifact.rs: metadata/artifact base -> {base}")
        return True

    _warn(log, "could not find expected_artifact_url_prefix in updater/artifact.rs")
    return False


# ---------------------------------------------------------------------------
# src/update_metadata.rs: trust the server's ed25519 public key
# ---------------------------------------------------------------------------

def _add_trusted_key(root, public_key, key_id="custom-update", log=None):
    target = os.path.join(root, "src", "update_metadata.rs")
    if not os.path.isfile(target):
        _warn(log, "src/update_metadata.rs not found (signed updates need "
                   "RustDesk 1.5.1+) — trusted key not added")
        return False

    key_bytes = None
    try:
        key_bytes = base64.b64decode(public_key, validate=True)
    except Exception:
        pass
    if key_bytes is None:
        try:
            key_bytes = bytes.fromhex(public_key)
        except Exception:
            pass
    if key_bytes is None or len(key_bytes) != 32:
        _warn(log, "update_public_key must be a 32-byte ed25519 key "
                   "(base64 or hex)")
        return False

    byte_list = ",\n            ".join(
        ", ".join(str(b) for b in key_bytes[i:i + 12])
        for i in range(0, len(key_bytes), 12)
    )
    entry = f'''TrustedUpdateKey {{
        key_id: "{key_id}",
        algorithm: SIGNATURE_ALGORITHM,
        public_key: [
            {byte_list}
        ],
    }}'''

    text = _read(target)

    # Idempotent: replace our own entry if it already exists.
    entry_pattern = re.compile(
        r'TrustedUpdateKey\s*\{\s*key_id:\s*"' + re.escape(key_id) +
        r'",\s*algorithm:\s*SIGNATURE_ALGORITHM,\s*public_key:\s*\[[^\]]*\],\s*\}',
        re.S,
    )
    if entry_pattern.search(text):
        _write(target, entry_pattern.sub(entry, text, count=1))
        log(f"    · update_metadata.rs: trusted key '{key_id}' updated")
        return True

    anchor = "const TRUSTED_UPDATE_KEYS: &[TrustedUpdateKey] = &["
    idx = text.find(anchor)
    if idx < 0:
        _warn(log, "could not find TRUSTED_UPDATE_KEYS in update_metadata.rs")
        return False
    end = text.find("];", idx)
    if end < 0:
        _warn(log, "could not find the end of TRUSTED_UPDATE_KEYS")
        return False

    before = text[:end].rstrip()
    if not before.endswith(","):
        before += ","
    _write(target, before + "\n    " + entry + "\n" + text[end:])
    log(f"    · update_metadata.rs: trusted key '{key_id}' added")
    return True


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def apply_custom_update(src_dir, update_url, public_key, log=print):
    """Apply all self-hosted update source changes.

    Returns a list of warning strings (empty when everything matched).
    """
    warnings = []
    root = os.path.abspath(src_dir)
    log(f"  Self-hosted auto-update (check URL: {update_url})")

    parsed = urlparse(update_url or "")
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return [f"invalid update URL: {update_url!r}"]

    # Signed update metadata (and therefore a custom trusted key) only exists
    # in RustDesk 1.5.1+. Half-patching an older tree would leave a client that
    # checks for updates it can never verify, so skip everything instead.
    if not os.path.isfile(os.path.join(root, "src", "update_metadata.rs")):
        return ["signed updates require RustDesk 1.5.1+ "
                "(src/update_metadata.rs is missing); update patch skipped"]

    if not _change_update_url(root, update_url, log):
        warnings.append("update URL not changed")
    if not _modify_get_version_number(root, log):
        warnings.append("get_version_number() not patched")
    if not _normalize_update_platform(root, log):
        warnings.append("update platform not normalized")
    if not _allow_updates_for_custom_clients(root, log):
        warnings.append("custom-client update guard not removed")
    if not _relax_common_host_check(root, log):
        warnings.append("release URL host check not relaxed")
    if not _modify_display_version(root, log):
        warnings.append("release-id build metadata not accepted")
    if not _patch_flutter_update_gates(root, log):
        warnings.append("Flutter update gates not patched")
    if not _relax_artifact_host_check(root, log):
        warnings.append("artifact download host check not relaxed")
    if not _retarget_artifact_base(root, update_url, log):
        warnings.append("artifact base URL not retargeted")
    if not _add_trusted_key(root, public_key, log=log):
        warnings.append("update public key not added to TRUSTED_UPDATE_KEYS")

    return warnings


APP_VERSION_RE = re.compile(r"^v?\d+\.\d+\.\d+(?:[+-][0-9A-Za-z.-]+)?$")


def apply_app_version(src_dir, app_version, log=print):
    """Set Cargo.toml/Cargo.lock to <version>+<build id>.

    rustdesk-api injects `app_version` for update builds so the running client
    reports the same ordered version as the signed update metadata. Without it
    a freshly updated client keeps offering the same update forever.
    """
    app_version = (app_version or "").strip().lstrip("v")
    if not app_version:
        return True
    if not APP_VERSION_RE.match(app_version):
        _warn(log, f"invalid app_version {app_version!r} (expected X.Y.Z or "
                   "X.Y.Z+NNN) — Cargo version not changed")
        return False

    cargo = os.path.join(src_dir, "Cargo.toml")
    if not _replace_regex(
        cargo,
        r'(?m)^version = "[^"]*"',
        lambda m: f'version = "{app_version}"',
    ):
        _warn(log, "could not set the package version in Cargo.toml")
        return False
    log(f"    · Cargo.toml: version -> {app_version}")

    lock = os.path.join(src_dir, "Cargo.lock")
    if _replace_regex(
        lock,
        r'(?m)(name = "rustdesk"\r?\nversion = ")[^"]*(")',
        lambda m: m.group(1) + app_version + m.group(2),
    ):
        log(f"    · Cargo.lock: rustdesk version -> {app_version}")
    # A missing Cargo.lock entry is not fatal for a source build; Cargo fixes
    # it up during the build.

    return True
