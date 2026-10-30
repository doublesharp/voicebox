//! Sequencing for dictation auto-paste. Native operations are kept behind a
//! boundary so ownership and error handling can be tested without using the
//! user's clipboard or changing foreground applications.
use std::future::Future;
use std::time::Duration;

use crate::{accessibility, clipboard, focus_capture, synthetic_keys};

static PASTE_LOCK: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

trait PasteBackend {
    type Snapshot;
    fn trusted(&self) -> bool;
    fn activate(&self, pid: i32) -> Result<(), String>;
    fn save(&self) -> Result<Self::Snapshot, String>;
    fn write(&self, text: &str) -> Result<i64, clipboard::ClipboardWriteFailure>;
    fn send(&self) -> Result<(), String>;
    fn change_count(&self) -> Result<i64, String>;
    fn restore(&self, snapshot: &Self::Snapshot) -> Result<(), String>;
}

// Restore while still holding the process-wide paste lock, including when the
// asynchronous paste future is cancelled during the consume delay.
struct ClipboardRestore<'a, B: PasteBackend> {
    backend: &'a B,
    snapshot: Option<B::Snapshot>,
    after_write: i64,
    armed: bool,
}

impl<B: PasteBackend> ClipboardRestore<'_, B> {
    fn restore_if_unchanged(&mut self) -> Result<(), String> {
        if !self.armed {
            return Ok(());
        }
        self.armed = false;
        if matches!(self.backend.change_count(), Ok(current) if current == self.after_write) {
            self.backend
                .restore(self.snapshot.as_ref().expect("armed clipboard snapshot"))
        } else {
            eprintln!("[voicebox] clipboard mutated during paste window — skipping restore to preserve newer content");
            Ok(())
        }
    }
}

impl<B: PasteBackend> Drop for ClipboardRestore<'_, B> {
    fn drop(&mut self) {
        if let Err(error) = self.restore_if_unchanged() {
            eprintln!("[voicebox] failed to restore clipboard after paste cancellation: {error}");
        }
    }
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
    fn write(&self, text: &str) -> Result<i64, clipboard::ClipboardWriteFailure> {
        clipboard::write_text_tracked(text)
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
    // Clipboard staging and restore must be one transaction across paste requests.
    let _paste_guard = PASTE_LOCK.lock().await;
    if !backend.trusted() {
        return Err("Accessibility permission required for auto-paste. Open System Settings → Privacy & Security → Accessibility and enable Voicebox.".into());
    }
    backend.activate(focus.pid)?;
    sleep(Duration::from_millis(crate::POST_ACTIVATE_SETTLE_MS)).await;
    let snapshot = backend.save()?;
    let after_write = match backend.write(text) {
        Ok(count) => count,
        Err(failure) => {
            if let Some(count) = failure.owned_change_count {
                let mut restore = ClipboardRestore {
                    backend,
                    snapshot: Some(snapshot),
                    after_write: count,
                    armed: true,
                };
                if let Err(error) = restore.restore_if_unchanged() {
                    return Err(format!(
                        "{}; clipboard restoration failed: {error}",
                        failure.message
                    ));
                }
            }
            return Err(failure.message);
        }
    };
    let mut restore = ClipboardRestore {
        backend,
        snapshot: Some(snapshot),
        after_write,
        armed: true,
    };
    let paste_result = backend.send();
    sleep(Duration::from_millis(crate::PASTE_CONSUME_MS)).await;
    restore.restore_if_unchanged()?;
    paste_result?;
    Ok(true)
}

pub(crate) struct DiagnosticResult<S> {
    pub snapshot: S,
    pub after_write: i64,
    pub before_restore: i64,
    pub after_restore: i64,
}

pub(crate) async fn diagnostic_clipboard(
    text: &str,
    pre_delay: Duration,
    post_delay: Duration,
    send: bool,
) -> Result<DiagnosticResult<clipboard::ClipboardSnapshot>, String> {
    diagnostic_with(
        text,
        pre_delay,
        post_delay,
        send,
        &NativePaste,
        tokio::time::sleep,
    )
    .await
}

async fn diagnostic_with<B: PasteBackend, F: Future<Output = ()>>(
    text: &str,
    pre_delay: Duration,
    post_delay: Duration,
    send: bool,
    backend: &B,
    sleep: impl Fn(Duration) -> F,
) -> Result<DiagnosticResult<B::Snapshot>, String> {
    let _paste_guard = PASTE_LOCK.lock().await;
    let snapshot = backend.save()?;
    let after_write = match backend.write(text) {
        Ok(count) => count,
        Err(failure) => {
            if let Some(count) = failure.owned_change_count {
                let mut restore = ClipboardRestore {
                    backend,
                    snapshot: Some(snapshot),
                    after_write: count,
                    armed: true,
                };
                if let Err(error) = restore.restore_if_unchanged() {
                    return Err(format!(
                        "{}; clipboard restoration failed: {error}",
                        failure.message
                    ));
                }
            }
            return Err(failure.message);
        }
    };
    let mut restore = ClipboardRestore {
        backend,
        snapshot: Some(snapshot),
        after_write,
        armed: true,
    };
    sleep(pre_delay).await;
    if send {
        if backend.change_count()? != after_write {
            return Err("Clipboard changed before diagnostic paste; paste cancelled".into());
        }
        if let Err(error) = backend.send() {
            return match restore.restore_if_unchanged() {
                Ok(()) => Err(error),
                Err(restore_error) => Err(format!(
                    "{error}; clipboard restoration failed: {restore_error}"
                )),
            };
        }
    }
    sleep(post_delay).await;
    let before_restore = backend.change_count()?;
    restore.restore_if_unchanged()?;
    let after_restore = backend.change_count()?;
    let snapshot = restore
        .snapshot
        .take()
        .expect("diagnostic clipboard snapshot");
    Ok(DiagnosticResult {
        snapshot,
        after_write,
        before_restore,
        after_restore,
    })
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
        fn write(&self, text: &str) -> Result<i64, clipboard::ClipboardWriteFailure> {
            assert_eq!(text, "hello");
            self.call("write")
                .map_err(|message| clipboard::ClipboardWriteFailure {
                    message,
                    owned_change_count: None,
                })?;
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

#[cfg(test)]
mod concurrent_paste_tests {
    use super::*;
    use std::cell::RefCell;
    use tokio::sync::Notify;

    struct Clipboard {
        text: String,
        revision: i64,
        sent: Vec<String>,
        fail_first: bool,
    }
    struct Backend(RefCell<Clipboard>);
    impl PasteBackend for Backend {
        type Snapshot = String;
        fn trusted(&self) -> bool {
            true
        }
        fn activate(&self, _: i32) -> Result<(), String> {
            Ok(())
        }
        fn save(&self) -> Result<String, String> {
            Ok(self.0.borrow().text.clone())
        }
        fn write(&self, text: &str) -> Result<i64, clipboard::ClipboardWriteFailure> {
            let mut clipboard = self.0.borrow_mut();
            clipboard.text = text.into();
            clipboard.revision += 1;
            Ok(clipboard.revision)
        }
        fn send(&self) -> Result<(), String> {
            let mut clipboard = self.0.borrow_mut();
            let text = clipboard.text.clone();
            clipboard.sent.push(text.clone());
            if clipboard.fail_first && text == "first" {
                Err("send failed".into())
            } else {
                Ok(())
            }
        }
        fn change_count(&self) -> Result<i64, String> {
            Ok(self.0.borrow().revision)
        }
        fn restore(&self, snapshot: &String) -> Result<(), String> {
            let mut clipboard = self.0.borrow_mut();
            clipboard.text = snapshot.clone();
            clipboard.revision += 1;
            Ok(())
        }
    }

    #[tokio::test]
    async fn diagnostic_preserves_clipboard_changed_by_another_application() {
        let backend = Backend(RefCell::new(Clipboard {
            text: "original".into(),
            revision: 0,
            sent: vec![],
            fail_first: false,
        }));
        let result = diagnostic_with(
            "first",
            Duration::ZERO,
            Duration::from_millis(1),
            true,
            &backend,
            |duration| {
                if !duration.is_zero() {
                    let mut state = backend.0.borrow_mut();
                    state.text = "newer".into();
                    state.revision += 1;
                }
                async {}
            },
        )
        .await
        .unwrap();
        assert_ne!(result.before_restore, result.after_write);
        assert_eq!(backend.0.borrow().text, "newer");
    }

    #[tokio::test]
    async fn diagnostic_does_not_paste_newer_clipboard_content() {
        let backend = Backend(RefCell::new(Clipboard {
            text: "original".into(),
            revision: 0,
            sent: vec![],
            fail_first: false,
        }));
        let error = diagnostic_with(
            "first",
            Duration::from_millis(1),
            Duration::ZERO,
            true,
            &backend,
            |duration| {
                if !duration.is_zero() {
                    let mut state = backend.0.borrow_mut();
                    state.text = "newer".into();
                    state.revision += 1;
                }
                async {}
            },
        )
        .await
        .err()
        .unwrap();
        assert!(error.contains("Clipboard changed before diagnostic paste"));
        let state = backend.0.borrow();
        assert_eq!(state.text, "newer");
        assert!(state.sent.is_empty());
    }

    #[tokio::test]
    async fn diagnostic_send_failure_restores_original_clipboard() {
        let backend = Backend(RefCell::new(Clipboard {
            text: "original".into(),
            revision: 0,
            sent: vec![],
            fail_first: true,
        }));
        assert_eq!(
            diagnostic_with(
                "first",
                Duration::ZERO,
                Duration::ZERO,
                true,
                &backend,
                |_| async {}
            )
            .await
            .err()
            .unwrap(),
            "send failed"
        );
        assert_eq!(backend.0.borrow().text, "original");
    }

    #[tokio::test]
    async fn diagnostic_cancellation_restores_original_clipboard() {
        let backend = Backend(RefCell::new(Clipboard {
            text: "original".into(),
            revision: 0,
            sent: vec![],
            fail_first: false,
        }));
        let entered = Notify::new();
        let mut future = Box::pin(diagnostic_with(
            "first",
            Duration::ZERO,
            Duration::ZERO,
            false,
            &backend,
            |_| async {
                entered.notify_one();
                std::future::pending::<()>().await
            },
        ));
        tokio::select! { _ = entered.notified() => {}, _ = &mut future => panic!("unexpected completion") }
        drop(future);
        assert_eq!(backend.0.borrow().text, "original");
    }

    #[tokio::test]
    async fn diagnostic_roundtrip_restores_snapshot_and_reports_counts() {
        for send in [false, true] {
            let backend = Backend(RefCell::new(Clipboard {
                text: "original".into(),
                revision: 0,
                sent: vec![],
                fail_first: false,
            }));
            let result = diagnostic_with(
                "first",
                Duration::ZERO,
                Duration::ZERO,
                send,
                &backend,
                |_| async {},
            )
            .await
            .unwrap();
            assert_eq!(result.snapshot, "original");
            assert_eq!(
                (
                    result.after_write,
                    result.before_restore,
                    result.after_restore
                ),
                (1, 1, 2)
            );
            let state = backend.0.borrow();
            assert_eq!(state.text, "original");
            assert_eq!(state.sent.len(), usize::from(send));
        }
    }

    #[tokio::test]
    async fn cancelled_diagnostic_preserves_newer_clipboard() {
        let backend = Backend(RefCell::new(Clipboard {
            text: "original".into(),
            revision: 0,
            sent: vec![],
            fail_first: false,
        }));
        let entered = Notify::new();
        let mut future = Box::pin(diagnostic_with(
            "first",
            Duration::ZERO,
            Duration::ZERO,
            false,
            &backend,
            |_| async {
                entered.notify_one();
                std::future::pending::<()>().await
            },
        ));
        tokio::select! { _ = entered.notified() => {}, _ = &mut future => panic!("unexpected completion") }
        {
            let mut state = backend.0.borrow_mut();
            state.text = "newer".into();
            state.revision += 1;
        }
        drop(future);
        assert_eq!(backend.0.borrow().text, "newer");
    }

    #[tokio::test]
    async fn diagnostic_and_dictation_share_clipboard_transaction_lock() {
        let backend = Backend(RefCell::new(Clipboard {
            text: "original".into(),
            revision: 0,
            sent: vec![],
            fail_first: false,
        }));
        let entered = Notify::new();
        let release = Notify::new();
        let focus = focus_capture::FocusSnapshot {
            pid: 42,
            bundle_id: None,
            role: None,
        };
        let entered_ref = &entered;
        let release_ref = &release;
        let diagnostic = diagnostic_with(
            "first",
            Duration::from_millis(1),
            Duration::ZERO,
            false,
            &backend,
            |duration| async move {
                if !duration.is_zero() {
                    entered_ref.notify_one();
                    release_ref.notified().await;
                }
            },
        );
        let dictation = async {
            entered.notified().await;
            let paste = paste_with("second", &focus, &backend, |_| async {});
            tokio::pin!(paste);
            tokio::select! { _ = &mut paste => panic!("dictation crossed diagnostic transaction"), _ = tokio::task::yield_now() => {} }
            release.notify_one();
            paste.await
        };
        let (diagnostic_result, dictation_result) = tokio::join!(diagnostic, dictation);
        assert!(diagnostic_result.is_ok());
        assert_eq!(dictation_result, Ok(true));
        assert_eq!(backend.0.borrow().text, "original");
    }

    #[tokio::test]
    async fn overlapping_pastes_preserve_original_or_newer_clipboard() {
        for (fail_first, external_change) in
            [(false, false), (true, false), (false, true), (true, true)]
        {
            let backend = Backend(RefCell::new(Clipboard {
                text: "original clipboard".into(),
                revision: 0,
                sent: vec![],
                fail_first,
            }));
            let focus = focus_capture::FocusSnapshot {
                pid: 42,
                bundle_id: None,
                role: None,
            };
            let entered = Notify::new();
            let release = Notify::new();
            let attempted = Notify::new();
            let entered_ref = &entered;
            let release_ref = &release;
            let first = paste_with("first", &focus, &backend, |duration| async move {
                if duration == Duration::from_millis(crate::PASTE_CONSUME_MS) {
                    entered_ref.notify_one();
                    release_ref.notified().await;
                }
            });
            let second = async {
                entered.notified().await;
                attempted.notify_one();
                paste_with("second", &focus, &backend, |_| async {}).await
            };
            let unblock = async {
                // The second future has reached the paste call before releasing
                // the first request from its clipboard-consumption wait.
                attempted.notified().await;
                tokio::task::yield_now().await;
                if external_change {
                    let mut clipboard = backend.0.borrow_mut();
                    clipboard.text = "newer clipboard".into();
                    clipboard.revision += 1;
                }
                release.notify_one();
            };
            let (first_result, second_result, ()) = tokio::join!(first, second, unblock);
            assert_eq!(first_result.is_err(), fail_first);
            assert_eq!(second_result, Ok(true));
            let clipboard = backend.0.borrow();
            assert_eq!(clipboard.sent, ["first", "second"]);
            assert_eq!(
                clipboard.text,
                if external_change {
                    "newer clipboard"
                } else {
                    "original clipboard"
                }
            );
        }
    }

    #[tokio::test]
    async fn cancellation_restores_staged_clipboard_but_preserves_external_changes() {
        for (fail_first, external_change) in
            [(false, false), (true, false), (false, true), (true, true)]
        {
            let backend = Backend(RefCell::new(Clipboard {
                text: "original clipboard".into(),
                revision: 0,
                sent: vec![],
                fail_first,
            }));
            let focus = focus_capture::FocusSnapshot {
                pid: 42,
                bundle_id: None,
                role: None,
            };
            let entered = Notify::new();
            let entered_ref = &entered;
            let mut paste = Box::pin(paste_with(
                "first",
                &focus,
                &backend,
                |duration| async move {
                    if duration == Duration::from_millis(crate::PASTE_CONSUME_MS) {
                        entered_ref.notify_one();
                        std::future::pending::<()>().await;
                    }
                },
            ));
            tokio::select! {
                _ = entered.notified() => {},
                result = &mut paste => panic!("paste ended before cancellation: {result:?}"),
            }
            assert_eq!(backend.0.borrow().text, "first");
            if external_change {
                let mut clipboard = backend.0.borrow_mut();
                clipboard.text = "newer clipboard".into();
                clipboard.revision += 1;
            }
            drop(paste);
            // A cancelled request must release the transaction lock as well.
            assert_eq!(
                paste_with("second", &focus, &backend, |_| async {}).await,
                Ok(true)
            );
            assert_eq!(
                backend.0.borrow().text,
                if external_change {
                    "newer clipboard"
                } else {
                    "original clipboard"
                }
            );
        }
    }
}

#[cfg(test)]
mod staging_failure_regression_tests {
    use super::*;
    use std::cell::RefCell;

    #[derive(Clone, Copy)]
    enum Failure {
        BeforeClear,
        AfterClear,
        ExternalAfterClear,
    }
    struct State {
        value: String,
        revision: i64,
        restores: usize,
    }
    struct Backend {
        state: RefCell<State>,
        failure: Failure,
        restore_fails: bool,
    }
    impl PasteBackend for Backend {
        type Snapshot = String;
        fn trusted(&self) -> bool {
            true
        }
        fn activate(&self, _: i32) -> Result<(), String> {
            Ok(())
        }
        fn save(&self) -> Result<String, String> {
            Ok(self.state.borrow().value.clone())
        }
        fn write(&self, _: &str) -> Result<i64, clipboard::ClipboardWriteFailure> {
            let mut state = self.state.borrow_mut();
            if !matches!(self.failure, Failure::BeforeClear) {
                state.value.clear();
                state.revision += 1;
            }
            if matches!(self.failure, Failure::ExternalAfterClear) {
                state.value = "newer".into();
                state.revision += 1;
            }
            Err(clipboard::ClipboardWriteFailure {
                message: "staging failed".into(),
                owned_change_count: match self.failure {
                    Failure::BeforeClear => None,
                    _ => Some(1),
                },
            })
        }
        fn send(&self) -> Result<(), String> {
            panic!("paste must not run after failed staging")
        }
        fn change_count(&self) -> Result<i64, String> {
            Ok(self.state.borrow().revision)
        }
        fn restore(&self, snapshot: &String) -> Result<(), String> {
            if self.restore_fails {
                return Err("restore failed".into());
            }
            let mut state = self.state.borrow_mut();
            state.value = snapshot.clone();
            state.revision += 1;
            state.restores += 1;
            Ok(())
        }
    }

    #[tokio::test]
    async fn failed_staging_restores_owned_clear_but_preserves_external_changes() {
        for (failure, expected, restores) in [
            (Failure::BeforeClear, "original", 0),
            (Failure::AfterClear, "original", 1),
            (Failure::ExternalAfterClear, "newer", 0),
        ] {
            let backend = Backend {
                state: RefCell::new(State {
                    value: "original".into(),
                    revision: 0,
                    restores: 0,
                }),
                failure,
                restore_fails: false,
            };
            let focus = focus_capture::FocusSnapshot {
                pid: 42,
                bundle_id: None,
                role: None,
            };
            assert_eq!(
                paste_with("dictation", &focus, &backend, |_| async {})
                    .await
                    .unwrap_err(),
                "staging failed"
            );
            let state = backend.state.borrow();
            assert_eq!(state.value, expected);
            assert_eq!(state.restores, restores);
        }
    }

    #[tokio::test]
    async fn diagnostic_failed_staging_recovers_only_its_own_clear() {
        for (failure, expected, restores) in [
            (Failure::BeforeClear, "original", 0),
            (Failure::AfterClear, "original", 1),
            (Failure::ExternalAfterClear, "newer", 0),
        ] {
            let backend = Backend {
                state: RefCell::new(State {
                    value: "original".into(),
                    revision: 0,
                    restores: 0,
                }),
                failure,
                restore_fails: false,
            };
            let error = diagnostic_with(
                "dictation",
                Duration::ZERO,
                Duration::ZERO,
                false,
                &backend,
                |_| async {},
            )
            .await
            .err()
            .unwrap();
            assert_eq!(error, "staging failed");
            let state = backend.state.borrow();
            assert_eq!(state.value, expected);
            assert_eq!(state.restores, restores);
        }
    }

    #[tokio::test]
    async fn failed_recovery_reports_both_staging_and_restore_errors() {
        let backend = Backend {
            state: RefCell::new(State {
                value: "original".into(),
                revision: 0,
                restores: 0,
            }),
            failure: Failure::AfterClear,
            restore_fails: true,
        };
        let focus = focus_capture::FocusSnapshot {
            pid: 42,
            bundle_id: None,
            role: None,
        };
        let error = paste_with("dictation", &focus, &backend, |_| async {})
            .await
            .unwrap_err();
        assert!(error.contains("staging failed"));
        assert!(error.contains("clipboard restoration failed: restore failed"));
    }
}
