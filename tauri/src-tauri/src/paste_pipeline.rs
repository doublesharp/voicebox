//! Sequencing for dictation auto-paste. Native operations are kept behind a
//! boundary so ownership and error handling can be tested without using the
//! user's clipboard or changing foreground applications.
use std::future::Future;
use std::time::Duration;

use crate::{accessibility, clipboard, focus_capture, synthetic_keys};

trait PasteBackend {
    type Snapshot;
    fn trusted(&self) -> bool;
    fn activate(&self, pid: i32) -> Result<(), String>;
    fn save(&self) -> Result<Self::Snapshot, String>;
    fn write(&self, text: &str) -> Result<i64, String>;
    fn send(&self) -> Result<(), String>;
    fn change_count(&self) -> Result<i64, String>;
    fn restore(&self, snapshot: &Self::Snapshot) -> Result<(), String>;
}

pub(crate) struct NativePaste;
impl PasteBackend for NativePaste {
    type Snapshot = clipboard::ClipboardSnapshot;
    fn trusted(&self) -> bool {
        accessibility::is_trusted()
    }
    fn activate(&self, pid: i32) -> Result<(), String> {
        focus_capture::activate_pid(pid)
    }
    fn save(&self) -> Result<Self::Snapshot, String> {
        clipboard::save_clipboard()
    }
    fn write(&self, text: &str) -> Result<i64, String> {
        clipboard::write_text(text)
    }
    fn send(&self) -> Result<(), String> {
        synthetic_keys::send_paste()
    }
    fn change_count(&self) -> Result<i64, String> {
        clipboard::current_change_count()
    }
    fn restore(&self, snapshot: &Self::Snapshot) -> Result<(), String> {
        clipboard::restore_clipboard(snapshot)
    }
}

pub(crate) async fn paste_final_text<F: Future<Output = ()>>(
    text: &str,
    focus: &focus_capture::FocusSnapshot,
    backend: &NativePaste,
    sleep: impl Fn(Duration) -> F,
) -> Result<bool, String> {
    paste_with(text, focus, backend, sleep).await
}

async fn paste_with<B: PasteBackend, F: Future<Output = ()>>(
    text: &str,
    focus: &focus_capture::FocusSnapshot,
    backend: &B,
    sleep: impl Fn(Duration) -> F,
) -> Result<bool, String> {
    if focus.bundle_id.as_deref() == Some(crate::VOICEBOX_BUNDLE_ID) {
        return Ok(false);
    }
    if !backend.trusted() {
        return Err("Accessibility permission required for auto-paste. Open System Settings → Privacy & Security → Accessibility and enable Voicebox.".into());
    }
    backend.activate(focus.pid)?;
    sleep(Duration::from_millis(crate::POST_ACTIVATE_SETTLE_MS)).await;
    let snapshot = backend.save()?;
    let after_write = backend.write(text)?;
    let paste_result = backend.send();
    sleep(Duration::from_millis(crate::PASTE_CONSUME_MS)).await;
    let safe_to_restore = matches!(backend.change_count(), Ok(current) if current == after_write);
    if safe_to_restore {
        backend.restore(&snapshot)?;
    } else {
        eprintln!("[voicebox] clipboard mutated during paste window — skipping restore to preserve newer content");
    }
    paste_result?;
    Ok(true)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;

    struct Fake {
        calls: RefCell<Vec<String>>,
        trusted: bool,
        fail: Option<&'static str>,
        count: Option<i64>,
    }
    impl Fake {
        fn new() -> Self {
            Self {
                calls: RefCell::new(vec![]),
                trusted: true,
                fail: None,
                count: Some(10),
            }
        }
        fn call(&self, name: &str) -> Result<(), String> {
            self.calls.borrow_mut().push(name.into());
            if self.fail == Some(name) {
                Err(format!("{name} failed"))
            } else {
                Ok(())
            }
        }
    }
    impl PasteBackend for Fake {
        type Snapshot = &'static str;
        fn trusted(&self) -> bool {
            self.calls.borrow_mut().push("trusted".into());
            self.trusted
        }
        fn activate(&self, pid: i32) -> Result<(), String> {
            assert_eq!(pid, 42);
            self.call("activate")
        }
        fn save(&self) -> Result<Self::Snapshot, String> {
            self.call("save")?;
            Ok("original")
        }
        fn write(&self, text: &str) -> Result<i64, String> {
            assert_eq!(text, "hello");
            self.call("write")?;
            Ok(10)
        }
        fn send(&self) -> Result<(), String> {
            self.call("send")
        }
        fn change_count(&self) -> Result<i64, String> {
            self.call("count")?;
            self.count.ok_or("unknown".into())
        }
        fn restore(&self, snapshot: &Self::Snapshot) -> Result<(), String> {
            assert_eq!(*snapshot, "original");
            self.call("restore")
        }
    }
    fn focus(bundle: Option<&str>) -> focus_capture::FocusSnapshot {
        focus_capture::FocusSnapshot {
            pid: 42,
            bundle_id: bundle.map(str::to_string),
            role: None,
        }
    }
    async fn run(fake: &Fake, focus: &focus_capture::FocusSnapshot) -> Result<bool, String> {
        paste_with("hello", focus, fake, |_| async {}).await
    }
    fn calls(fake: &Fake) -> Vec<String> {
        fake.calls.borrow().clone()
    }

    #[tokio::test]
    async fn successful_paste_restores_the_original_clipboard_in_order() {
        let fake = Fake::new();
        assert_eq!(run(&fake, &focus(Some("other.app"))).await, Ok(true));
        assert_eq!(
            calls(&fake),
            ["trusted", "activate", "save", "write", "send", "count", "restore"]
        );
    }
    #[tokio::test]
    async fn activation_and_paste_settle_for_the_expected_durations() {
        let fake = Fake::new();
        let delays = RefCell::new(Vec::new());
        let result = paste_with("hello", &focus(None), &fake, |duration| {
            delays.borrow_mut().push(duration);
            async {}
        })
        .await;
        assert_eq!(result, Ok(true));
        assert_eq!(
            delays.into_inner(),
            [
                Duration::from_millis(crate::POST_ACTIVATE_SETTLE_MS),
                Duration::from_millis(crate::PASTE_CONSUME_MS)
            ]
        );
    }

    #[tokio::test]
    async fn own_window_skips_all_native_operations() {
        let fake = Fake::new();
        assert_eq!(
            run(&fake, &focus(Some(crate::VOICEBOX_BUNDLE_ID))).await,
            Ok(false)
        );
        assert!(calls(&fake).is_empty());
    }
    #[tokio::test]
    async fn missing_permission_does_not_activate_or_touch_clipboard() {
        let mut fake = Fake::new();
        fake.trusted = false;
        assert!(run(&fake, &focus(None))
            .await
            .unwrap_err()
            .contains("Accessibility permission"));
        assert_eq!(calls(&fake), ["trusted"]);
    }
    #[tokio::test]
    async fn early_failures_stop_before_subsequent_operations() {
        for (failure, expected) in [
            ("activate", vec!["trusted", "activate"]),
            ("save", vec!["trusted", "activate", "save"]),
            ("write", vec!["trusted", "activate", "save", "write"]),
        ] {
            let mut fake = Fake::new();
            fake.fail = Some(failure);
            assert_eq!(
                run(&fake, &focus(None)).await.unwrap_err(),
                format!("{failure} failed")
            );
            assert_eq!(calls(&fake), expected);
        }
    }
    #[tokio::test]
    async fn failed_paste_still_restores_unchanged_clipboard() {
        let mut fake = Fake::new();
        fake.fail = Some("send");
        assert_eq!(run(&fake, &focus(None)).await.unwrap_err(), "send failed");
        assert_eq!(
            calls(&fake),
            ["trusted", "activate", "save", "write", "send", "count", "restore"]
        );
    }
    #[tokio::test]
    async fn changed_or_unknown_clipboard_is_never_overwritten() {
        for count in [Some(11), None] {
            let mut fake = Fake::new();
            fake.count = count;
            assert_eq!(run(&fake, &focus(None)).await, Ok(true));
            assert_eq!(
                calls(&fake),
                ["trusted", "activate", "save", "write", "send", "count"]
            );
        }
    }
    #[tokio::test]
    async fn restore_failure_is_propagated() {
        let mut fake = Fake::new();
        fake.fail = Some("restore");
        assert_eq!(
            run(&fake, &focus(None)).await.unwrap_err(),
            "restore failed"
        );
    }
    #[tokio::test]
    async fn failed_paste_preserves_newer_clipboard_content() {
        let mut fake = Fake::new();
        fake.fail = Some("send");
        fake.count = Some(11);
        assert_eq!(run(&fake, &focus(None)).await.unwrap_err(), "send failed");
        assert!(!calls(&fake).contains(&"restore".into()));
    }
}
