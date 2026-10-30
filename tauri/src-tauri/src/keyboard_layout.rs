//! Layout-aware resolution of the keycode whose current-layout translation
//! is `'v'`. Drives [`crate::synthetic_keys::send_paste`] so the synthetic
//! Cmd+V it posts is interpreted as Paste by the focused app regardless of
//! the user's active keyboard layout (Dvorak, Colemak, AZERTY, …).
//!
//! macOS apps process Cmd+V via NSMenu key equivalents, which match against
//! `[NSEvent charactersIgnoringModifiers]` — i.e. the layout-translated
//! character, not the raw keycode. Posting `kVK_ANSI_V` (= 9, the QWERTY V
//! position) on Dvorak therefore produces Cmd+. and never triggers Paste.
//!
//! All TIS calls happen on the main thread: once at startup via [`init`]
//! from Tauri's setup hook, and again from the
//! `kTISNotifySelectedKeyboardInputSourceChanged` distributed notification
//! (delivered to the main runloop). The hot path ([`paste_keycode_v`])
//! only reads an [`AtomicU16`], so paste latency is unchanged.
//!
//! Windows is intentionally not covered here. `SendInput` with
//! `wVk = VK_V` delivers `WM_KEYDOWN` to the target with `wParam = VK_V`
//! regardless of the active layout — most Windows apps treat that as
//! Ctrl+V. AutoHotkey relies on the same behaviour.

#[cfg(target_os = "macos")]
use std::sync::atomic::{AtomicU16, Ordering};

/// `kVK_ANSI_V` — the keycode for the physical V key on a US QWERTY
/// layout. Used as the fallback whenever live resolution can't produce a
/// better answer (no Unicode key layout data, lookup failure, non-macOS).
#[cfg(target_os = "macos")]
const FALLBACK_V_KEYCODE: u16 = 9;

#[cfg(target_os = "macos")]
static V_KEYCODE: AtomicU16 = AtomicU16::new(FALLBACK_V_KEYCODE);

/// Returns the keycode whose current-layout translation is `'v'`. Falls
/// back to `kVK_ANSI_V` when resolution hasn't run, the active input
/// source carries no Unicode key layout data, or no keycode in the layout
/// produces `v`.
#[cfg(target_os = "macos")]
pub fn paste_keycode_v() -> u16 {
    V_KEYCODE.load(Ordering::Relaxed)
}

#[cfg(target_os = "macos")]
pub fn init() {
    macos::init();
}

#[cfg(not(target_os = "macos"))]
pub fn init() {}

#[cfg(target_os = "macos")]
mod macos {
    use super::{FALLBACK_V_KEYCODE, V_KEYCODE};
    use core_foundation_sys::base::CFRelease;
    use core_foundation_sys::data::{CFDataGetBytePtr, CFDataRef};
    use core_foundation_sys::dictionary::CFDictionaryRef;
    use core_foundation_sys::notification_center::{
        CFNotificationCenterAddObserver, CFNotificationCenterGetDistributedCenter,
        CFNotificationCenterRef, CFNotificationName,
        CFNotificationSuspensionBehaviorDeliverImmediately,
    };
    use core_foundation_sys::string::CFStringRef;
    use std::ffi::c_void;
    use std::ptr;
    use std::sync::atomic::Ordering;

    type TISInputSourceRef = *mut c_void;

    /// `kUCKeyActionDown`.
    const K_UC_KEY_ACTION_DOWN: u16 = 0;
    /// `kUCKeyTranslateNoDeadKeysMask` — collapse dead-key state machine so
    /// a single call gives us the bare character. V is never a dead key on
    /// any layout we care about, but the flag costs nothing and removes
    /// any chance of ambiguous output.
    const K_UC_KEY_TRANSLATE_NO_DEAD_KEYS_MASK: u32 = 1;
    /// Standard US-style virtual keycodes occupy 0..0x7F. We iterate the
    /// full range so non-US-extended layouts (ISO, JIS) can still be
    /// resolved if their `v` lives outside the ANSI range.
    const MAX_KEYCODE: u16 = 127;
    const TARGET_CHAR: u16 = b'v' as u16;

    #[link(name = "Carbon", kind = "framework")]
    extern "C" {
        fn TISCopyCurrentKeyboardLayoutInputSource() -> TISInputSourceRef;
        fn TISGetInputSourceProperty(source: TISInputSourceRef, key: CFStringRef) -> *mut c_void;
        fn LMGetKbdType() -> u8;
        fn UCKeyTranslate(
            keyboard_layout: *const u8,
            virtual_key_code: u16,
            key_action: u16,
            modifier_key_state: u32,
            keyboard_type: u32,
            key_translate_options: u32,
            dead_key_state: *mut u32,
            max_string_length: usize,
            actual_string_length: *mut usize,
            unicode_string: *mut u16,
        ) -> i32;

        static kTISPropertyUnicodeKeyLayoutData: CFStringRef;
        static kTISNotifySelectedKeyboardInputSourceChanged: CFStringRef;
    }

    pub fn init() {
        resolve_into_cache();
        register_layout_change_observer();
    }

    fn resolve_into_cache() {
        let kc = resolve_v_keycode().unwrap_or(FALLBACK_V_KEYCODE);
        V_KEYCODE.store(kc, Ordering::Relaxed);
    }

    fn resolve_v_keycode() -> Option<u16> {
        unsafe {
            let source = TISCopyCurrentKeyboardLayoutInputSource();
            if source.is_null() {
                return None;
            }
            let _src_guard = scopeguard::guard(source, |s| CFRelease(s as *const c_void));

            let layout_data_ptr =
                TISGetInputSourceProperty(source, kTISPropertyUnicodeKeyLayoutData);
            if layout_data_ptr.is_null() {
                return None;
            }
            let layout_bytes = CFDataGetBytePtr(layout_data_ptr as CFDataRef);
            if layout_bytes.is_null() {
                return None;
            }

            let kbd_type = LMGetKbdType() as u32;

            find_v_keycode(|keycode| {
                let mut dead_key_state: u32 = 0;
                let mut chars: [u16; 4] = [0; 4];
                let mut actual_len: usize = 0;
                let status = UCKeyTranslate(
                    layout_bytes,
                    keycode,
                    K_UC_KEY_ACTION_DOWN,
                    0,
                    kbd_type,
                    K_UC_KEY_TRANSLATE_NO_DEAD_KEYS_MASK,
                    &mut dead_key_state,
                    chars.len(),
                    &mut actual_len,
                    chars.as_mut_ptr(),
                );
                (status, actual_len, chars[0])
            })
        }
    }

    fn find_v_keycode(mut translate: impl FnMut(u16) -> (i32, usize, u16)) -> Option<u16> {
        (0..=MAX_KEYCODE).find(|&keycode| {
            let (status, len, first) = translate(keycode);
            status == 0 && len == 1 && first == TARGET_CHAR
        })
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[test]
        fn resolves_v_at_different_physical_keys_including_extended_layouts() {
            for target in [0, 9, 37, 127] {
                assert_eq!(
                    find_v_keycode(|key| (
                        0,
                        1,
                        if key == target {
                            b'v' as u16
                        } else {
                            b'x' as u16
                        }
                    )),
                    Some(target)
                );
            }
        }

        #[test]
        fn ignores_errors_empty_and_multicharacter_translations() {
            assert_eq!(
                find_v_keycode(|key| match key {
                    1 => (-1, 1, TARGET_CHAR),
                    2 => (0, 0, TARGET_CHAR),
                    3 => (0, 2, TARGET_CHAR),
                    4 => (0, 1, TARGET_CHAR),
                    _ => (0, 1, b'x' as u16),
                }),
                Some(4)
            );
        }

        #[test]
        fn returns_none_if_layout_has_no_unmodified_v() {
            assert_eq!(find_v_keycode(|_| (0, 1, b'V' as u16)), None);
        }

        #[test]
        fn chooses_the_first_matching_key_and_stops_scanning() {
            let mut seen = Vec::new();
            let key = find_v_keycode(|code| {
                seen.push(code);
                (0, 1, if code >= 4 { TARGET_CHAR } else { 0 })
            });
            assert_eq!(key, Some(4));
            assert_eq!(seen, [0, 1, 2, 3, 4]);
        }

        #[test]
        fn current_macos_layout_resolution_stays_within_the_scanned_range() {
            if let Some(key) = resolve_v_keycode() {
                assert!(key <= MAX_KEYCODE);
            }
        }
    }

    extern "C" fn layout_changed(
        _center: CFNotificationCenterRef,
        _observer: *mut c_void,
        _name: CFNotificationName,
        _object: *const c_void,
        _user_info: CFDictionaryRef,
    ) {
        resolve_into_cache();
    }

    fn register_layout_change_observer() {
        unsafe {
            let center = CFNotificationCenterGetDistributedCenter();
            if center.is_null() {
                return;
            }
            CFNotificationCenterAddObserver(
                center,
                ptr::null(),
                layout_changed,
                kTISNotifySelectedKeyboardInputSourceChanged,
                ptr::null(),
                CFNotificationSuspensionBehaviorDeliverImmediately,
            );
        }
    }
}
