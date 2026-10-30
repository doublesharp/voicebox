//! Synthetic keyboard event posting for the auto-paste pipeline.
//!
//! `send_paste` fires the four-event paste sequence onto the OS input
//! pipeline so the focused app performs its native paste action against
//! whatever the clipboard module has just staged.
//!
//! - **macOS** — Cmd down with Cmd flag, V down with Cmd flag, V up with
//!   Cmd flag, Cmd up via `CGEventPost` at `kCGHIDEventTap`. The Cmd-down
//!   event carries the Command flag so its `flagsChanged` representation
//!   matches hardware — Electron/Chromium tracks modifier state from that
//!   flag and drops the paste otherwise (see the note on the event table).
//!   Accessibility permission is load-bearing: without it the system
//!   swallows the events silently, so callers must gate on
//!   [`crate::accessibility::is_trusted`].
//! - **Windows** — Ctrl down, V down, V up, Ctrl up via `SendInput`. No
//!   permission gate, but UAC/UIPI blocks delivery into elevated target
//!   windows when we run non-elevated — nothing we can do short of also
//!   running elevated.
//!
//! On macOS the V keycode is resolved per-layout by
//! [`crate::keyboard_layout`] — Cmd+V is matched against the layout-
//! translated character via NSMenu key equivalents, so hardcoding
//! `kVK_ANSI_V` (the QWERTY V position) would fire Cmd+. on Dvorak. The
//! resolved keycode is read once per paste from an atomic; the cache is
//! primed at startup and refreshed on layout change.
//!
//! Windows hardcodes `VK_V`. `SendInput` with `wVk = VK_V` makes the
//! target receive `WM_KEYDOWN` with `wParam = VK_V` regardless of the
//! active layout, and most Windows apps treat that as Ctrl+V (the same
//! reason `Send "^v"` works in AutoHotkey on Dvorak Windows).

#[cfg(target_os = "macos")]
use std::ffi::c_void;

#[cfg(target_os = "macos")]
mod ffi {
    use std::ffi::c_void;

    #[repr(C)]
    pub struct CGEvent {
        _opaque: [u8; 0],
    }
    pub type CGEventRef = *mut CGEvent;

    #[repr(C)]
    pub struct CGEventSource {
        _opaque: [u8; 0],
    }
    pub type CGEventSourceRef = *mut CGEventSource;

    pub type CGEventTapLocation = u32;
    pub type CGKeyCode = u16;
    pub type CGEventFlags = u64;
    pub type CGEventSourceStateID = i32;

    /// `kCGHIDEventTap` — posted events enter at the HID level so every
    /// downstream tap (including the target app) sees them exactly as if the
    /// hardware had produced them.
    pub const K_CG_HID_EVENT_TAP: CGEventTapLocation = 0;

    /// `kCGEventSourceStateHIDSystemState` — mimics hardware, which is what
    /// we want: modifier bookkeeping inside target apps stays consistent.
    pub const K_CG_EVENT_SOURCE_STATE_HID_SYSTEM_STATE: CGEventSourceStateID = 1;

    /// `kCGEventFlagMaskCommand` — the Cmd modifier bit inside `CGEventFlags`.
    pub const K_CG_EVENT_FLAG_MASK_COMMAND: CGEventFlags = 0x00100000;

    /// `kVK_Command` (left Cmd).
    pub const KEYCODE_LEFT_CMD: CGKeyCode = 0x37;

    #[link(name = "CoreGraphics", kind = "framework")]
    extern "C" {
        pub fn CGEventSourceCreate(state_id: CGEventSourceStateID) -> CGEventSourceRef;
        pub fn CGEventCreateKeyboardEvent(
            source: CGEventSourceRef,
            virtual_key: CGKeyCode,
            key_down: bool,
        ) -> CGEventRef;
        pub fn CGEventSetFlags(event: CGEventRef, flags: CGEventFlags);
        pub fn CGEventPost(tap: CGEventTapLocation, event: CGEventRef);
    }

    #[link(name = "CoreFoundation", kind = "framework")]
    extern "C" {
        pub fn CFRelease(cf: *const c_void);
    }
}

/// Post the four-event Cmd+V sequence to the HID event tap.
///
/// Returns after the events are queued — there's no completion callback,
/// so callers should sleep briefly afterwards to let the target app
/// process the paste before any follow-up (e.g. clipboard restore).
#[cfg(target_os = "macos")]
pub fn send_paste() -> Result<(), String> {
    unsafe {
        send_paste_with(
            crate::keyboard_layout::paste_keycode_v(),
            || ffi::CGEventSourceCreate(ffi::K_CG_EVENT_SOURCE_STATE_HID_SYSTEM_STATE),
            |source, key, down| ffi::CGEventCreateKeyboardEvent(source, key, down),
            |event| ffi::CGEventPost(ffi::K_CG_HID_EVENT_TAP, event),
        )
    }
}

#[cfg(target_os = "macos")]
unsafe fn send_paste_with(
    v_keycode: ffi::CGKeyCode,
    create_source: impl FnOnce() -> ffi::CGEventSourceRef,
    mut create: impl FnMut(ffi::CGEventSourceRef, ffi::CGKeyCode, bool) -> ffi::CGEventRef,
    mut post: impl FnMut(ffi::CGEventRef),
) -> Result<(), String> {
    use ffi::*;
    {
        let source = create_source();
        if source.is_null() {
            return Err("CGEventSourceCreate returned null".into());
        }
        let _source_guard = scopeguard::guard(source, |s| CFRelease(s as *const c_void));

        let events = [
            // The Cmd-down event must carry the Command flag itself. On real
            // hardware the Cmd keyDown is a flagsChanged event whose flags
            // already include Command; Chromium/Electron builds its tracked
            // modifier state from that flag. Posting Cmd-down with flags = 0
            // leaves that tracker showing "Command up", so the following V —
            // even though its own flags carry Command — matches neither the
            // Cmd+V accelerator (tracker says no modifier) nor plain-text
            // insertion (event flags say Command), and Electron drops it
            // silently. AppKit reads the V event's own flags and pastes
            // regardless, which is why native apps worked but Electron
            // targets (Slack, VS Code) silently no-op'd.
            (KEYCODE_LEFT_CMD, true, K_CG_EVENT_FLAG_MASK_COMMAND),
            (v_keycode, true, K_CG_EVENT_FLAG_MASK_COMMAND),
            (v_keycode, false, K_CG_EVENT_FLAG_MASK_COMMAND),
            (KEYCODE_LEFT_CMD, false, 0),
        ];

        // Build the four events up front so CFRelease happens after all posts.
        // Posting in a loop that interleaved create → post → release would
        // work, but keeping the events alive for the full sequence matches
        // the pattern CGEventPost's docs show and is easier to reason about.
        let mut guards = Vec::with_capacity(events.len());
        let mut created = Vec::with_capacity(events.len());

        for (key, down, flags) in events {
            let event = create(source, key, down);
            if event.is_null() {
                return Err(format!(
                    "CGEventCreateKeyboardEvent(key={}, down={}) returned null",
                    key, down
                ));
            }
            let guard = scopeguard::guard(event, |e| CFRelease(e as *const c_void));
            // Clear inherited flags on key-up as well as setting them on key-down.
            CGEventSetFlags(event, flags);
            created.push(event);
            guards.push(guard);
        }

        for event in created {
            post(event);
        }

        drop(guards);
        Ok(())
    }
}

#[cfg(target_os = "windows")]
mod win {
    use windows::Win32::UI::Input::KeyboardAndMouse::{
        INPUT, INPUT_0, INPUT_KEYBOARD, KEYBDINPUT, KEYBD_EVENT_FLAGS, KEYEVENTF_KEYUP, VIRTUAL_KEY,
    };

    pub fn make_key(vk: VIRTUAL_KEY, up: bool) -> INPUT {
        let flags = if up {
            KEYEVENTF_KEYUP
        } else {
            KEYBD_EVENT_FLAGS(0)
        };
        INPUT {
            r#type: INPUT_KEYBOARD,
            Anonymous: INPUT_0 {
                ki: KEYBDINPUT {
                    wVk: vk,
                    wScan: 0,
                    dwFlags: flags,
                    time: 0,
                    dwExtraInfo: 0,
                },
            },
        }
    }
}

#[cfg(target_os = "windows")]
pub fn send_paste() -> Result<(), String> {
    use windows::Win32::UI::Input::KeyboardAndMouse::{SendInput, INPUT, VK_CONTROL, VK_V};

    // Four-event Ctrl+V sequence. Matches the macOS CGEvent pattern: the
    // modifier brackets the letter so the target app sees a fully formed
    // accelerator rather than a lone V. `dwExtraInfo` is zero — we're not
    // tagging these as "ours" because no consumer in the paste path needs
    // to distinguish synthetic events from hardware ones.
    let events = [
        win::make_key(VK_CONTROL, false),
        win::make_key(VK_V, false),
        win::make_key(VK_V, true),
        win::make_key(VK_CONTROL, true),
    ];

    unsafe {
        let sent = SendInput(&events, std::mem::size_of::<INPUT>() as i32);
        if sent as usize != events.len() {
            return Err(format!(
                "SendInput delivered {} of {} events — the input desktop may be locked (secure attention sequence) or a higher-integrity window is intercepting.",
                sent,
                events.len()
            ));
        }
    }

    Ok(())
}

#[cfg(not(any(target_os = "macos", target_os = "windows")))]
pub fn send_paste() -> Result<(), String> {
    Err("synthetic paste is not yet implemented on this platform".into())
}

#[cfg(all(test, target_os = "macos"))]
mod macos_event_tests {
    use super::*;
    use std::cell::Cell;

    #[link(name = "CoreGraphics", kind = "framework")]
    extern "C" {
        fn CGEventGetFlags(event: ffi::CGEventRef) -> u64;
        fn CGEventGetIntegerValueField(event: ffi::CGEventRef, field: u32) -> i64;
        fn CGEventGetType(event: ffi::CGEventRef) -> u32;
    }

    #[test]
    fn created_paste_events_have_ordered_keys_and_clear_the_modifier_on_release() {
        for v_keycode in [9, 16] {
            let mut posted = Vec::new();
            unsafe {
                send_paste_with(
                    v_keycode,
                    || ffi::CGEventSourceCreate(ffi::K_CG_EVENT_SOURCE_STATE_HID_SYSTEM_STATE),
                    |source, key, down| {
                        let event = ffi::CGEventCreateKeyboardEvent(source, key, down);
                        assert!(!event.is_null());
                        // Simulate an event source carrying an already-held modifier.
                        ffi::CGEventSetFlags(event, ffi::K_CG_EVENT_FLAG_MASK_COMMAND);
                        event
                    },
                    |event| {
                        posted.push((
                            CGEventGetIntegerValueField(event, 9),
                            CGEventGetType(event),
                            CGEventGetFlags(event),
                        ));
                    },
                )
                .unwrap();
            }
            assert_eq!(
                posted.iter().map(|event| event.0).collect::<Vec<_>>(),
                [55, v_keycode as i64, v_keycode as i64, 55]
            );
            assert_eq!(posted[1].1, 10, "V key must go down");
            assert_eq!(posted[2].1, 11, "V key must go up");
            for event in &posted[..3] {
                assert_ne!(event.2 & ffi::K_CG_EVENT_FLAG_MASK_COMMAND, 0);
            }
            assert_eq!(
                posted[3].2 & ffi::K_CG_EVENT_FLAG_MASK_COMMAND,
                0,
                "the command key-up event must clear the command flag"
            );
        }
    }

    #[test]
    fn a_missing_event_source_cannot_create_or_post_any_events() {
        let result = unsafe {
            send_paste_with(
                9,
                || std::ptr::null_mut(),
                |_, _, _| panic!("no event may be created"),
                |_| panic!("no event may be posted"),
            )
        };
        assert!(result.unwrap_err().contains("CGEventSourceCreate"));
    }

    #[test]
    fn an_event_creation_failure_never_posts_a_partial_paste() {
        for failed_index in 0..4 {
            let count = Cell::new(0);
            let posted = Cell::new(0);
            let result = unsafe {
                send_paste_with(
                    9,
                    || ffi::CGEventSourceCreate(ffi::K_CG_EVENT_SOURCE_STATE_HID_SYSTEM_STATE),
                    |source, key, down| {
                        let index = count.get();
                        count.set(index + 1);
                        if index == failed_index {
                            std::ptr::null_mut()
                        } else {
                            ffi::CGEventCreateKeyboardEvent(source, key, down)
                        }
                    },
                    |_| posted.set(posted.get() + 1),
                )
            };
            assert!(result.unwrap_err().contains("CGEventCreateKeyboardEvent"));
            assert_eq!(count.get(), failed_index + 1);
            assert_eq!(posted.get(), 0);
        }
    }
}
