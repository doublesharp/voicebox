//! Global hotkey → dictation effect bridge.
//!
//! Thin adapter from `keytap::chord::ChordMatcher` to Tauri events. keytap
//! owns the OS event tap + the chord state machine (Momentary vs Toggle,
//! longest-match resolution, sticky-toggle semantics); this module's only
//! job is:
//!
//!   1. Build a `ChordMatcher` from the user's saved PTT + Toggle chords.
//!   2. Translate `ChordEvent` → voicebox's [`Effect`] on a dispatcher
//!      thread.
//!   3. Fan [`Effect`]s out into Tauri events + dictate-window show/hide.
//!
//! The [`Effect::RestartRecording`] signal is emitted when keytap fires
//! `End(PTT)` and `Start(Toggle)` with the *same* [`Instant`] — which
//! happens when the held set upgrades from a shorter chord to a longer
//! superset in a single event (the classic PTT→hands-free transition).
//! We detect the pair with a 5 ms peek on the matcher's receiver and
//! coalesce into one `Restart` so hosts can discard the transition-
//! moment audio rather than treat it as an unrelated Stop+Start pair.
//!
//! Left- and right-hand modifier variants are kept distinct all the way
//! down to the OS event tap (keytap's core promise). Defaults bind to
//! right-hand Cmd + right-hand Option on macOS / right-hand Ctrl +
//! right-hand Shift on Windows so the usual left-hand shortcuts stay
//! with the OS / app.

use std::collections::{HashMap, HashSet};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread::{self, JoinHandle};
use std::time::Duration;

use keytap::chord::{Chord, ChordEvent, ChordMatcher};
use keytap::{Key, RecvTimeoutError};
use tauri::{AppHandle, Emitter, Manager};

use crate::focus_capture;
use crate::DICTATE_WINDOW_LABEL;

// ========================================================================
// Public types
// ========================================================================

/// Semantic action a chord can be bound to. `PushToTalk` = hold chord to
/// record, release to stop. `ToggleToTalk` = press chord to start recording,
/// press again to stop.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum ChordAction {
    PushToTalk,
    ToggleToTalk,
}

/// Effect produced after the chord matcher resolves an event. Hosts
/// translate these into UI / recorder calls.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Effect {
    StartRecording(ChordAction),
    StopRecording(ChordAction),
    /// Emitted when a push-to-talk chord is "upgraded" into the toggle
    /// chord mid-hold — hosts may want to discard the captured audio and
    /// restart so the transition moment isn't in the recording.
    RestartRecording(ChordAction),
}

/// Chord key sets from capture settings. Both actions use the same
/// `HashSet<Key>` shape so callers don't need to know about keytap's
/// `Chord` type.
pub type Bindings = HashMap<ChordAction, HashSet<Key>>;

// ========================================================================
// Monitor
// ========================================================================

pub struct HotkeyMonitor {
    app: AppHandle,
    active: Option<Active>,
}

struct Active {
    dispatcher: JoinHandle<()>,
    shutdown: Arc<AtomicBool>,
}

impl HotkeyMonitor {
    /// Build the monitor with initial bindings. Equivalent to constructing
    /// an empty monitor and calling [`Self::update_bindings`] once.
    pub fn spawn(app: AppHandle, bindings: Bindings) -> Self {
        let mut m = Self { app, active: None };
        m.apply(bindings);
        m
    }

    /// Swap in a fresh set of chord bindings. Tears down the existing
    /// `ChordMatcher` (which stops keytap's chord worker thread and
    /// closes the OS tap) and spawns a new one. No-op for the "all
    /// empty" case so "disable hotkey" doesn't keep a tap running for
    /// no reason.
    pub fn update_bindings(&mut self, bindings: Bindings) {
        self.apply(bindings);
    }

    fn apply(&mut self, bindings: Bindings) {
        // Tear down any existing matcher + dispatcher first. The
        // dispatcher sees the shutdown flag on its next recv_timeout
        // (≤100ms) and returns; joining waits for that. Dropping the
        // ChordMatcher stops keytap's chord-worker thread and the
        // underlying Tap.
        stop_dispatcher(self.active.take(), || {
            apply_effect(&self.app, Effect::StopRecording(ChordAction::PushToTalk));
        });

        if bindings.values().all(|set| set.is_empty()) {
            return;
        }

        let matcher = match build_matcher(&bindings) {
            Ok(m) => m,
            Err(err) => {
                eprintln!(
                    "HotkeyMonitor: ChordMatcher build failed ({err}). Global chord detection is disabled. On macOS, grant Input Monitoring in System Settings → Privacy & Security → Input Monitoring and relaunch."
                );
                return;
            }
        };

        let shutdown = Arc::new(AtomicBool::new(false));
        let shutdown_for_thread = shutdown.clone();
        let app = self.app.clone();
        let dispatcher = thread::Builder::new()
            .name("voicebox-hotkey-dispatcher".into())
            .spawn(move || dispatcher_loop(app, matcher, shutdown_for_thread))
            .expect("spawn hotkey dispatcher thread");

        self.active = Some(Active {
            dispatcher,
            shutdown,
        });
    }
}

fn stop_dispatcher(active: Option<Active>, on_stopped: impl FnOnce()) {
    if let Some(active) = active {
        active.shutdown.store(true, Ordering::Release);
        let _ = active.dispatcher.join();
        on_stopped();
    }
}

impl Drop for HotkeyMonitor {
    fn drop(&mut self) {
        if let Some(active) = self.active.take() {
            active.shutdown.store(true, Ordering::Release);
            let _ = active.dispatcher.join();
        }
    }
}

// ========================================================================
// Matcher construction + dispatch
// ========================================================================

fn build_matcher(bindings: &Bindings) -> Result<ChordMatcher<ChordAction>, keytap::Error> {
    let mut builder = ChordMatcher::builder();
    if let Some(keys) = bindings.get(&ChordAction::PushToTalk) {
        if !keys.is_empty() {
            builder = builder.add(ChordAction::PushToTalk, Chord::of(keys.iter().copied()));
        }
    }
    if let Some(keys) = bindings.get(&ChordAction::ToggleToTalk) {
        if !keys.is_empty() {
            builder =
                builder.add_toggle(ChordAction::ToggleToTalk, Chord::of(keys.iter().copied()));
        }
    }
    builder.build()
}

fn dispatcher_loop(app: AppHandle, matcher: ChordMatcher<ChordAction>, shutdown: Arc<AtomicBool>) {
    run_dispatcher(
        &shutdown,
        || matcher.recv_timeout(Duration::from_millis(100)),
        |event| process_event(&app, &matcher, &shutdown, event),
    );
}

fn run_dispatcher(
    shutdown: &AtomicBool,
    mut receive: impl FnMut() -> Result<ChordEvent<ChordAction>, RecvTimeoutError>,
    mut process: impl FnMut(ChordEvent<ChordAction>),
) {
    while !shutdown.load(Ordering::Acquire) {
        match receive() {
            Ok(event) => {
                if !shutdown.load(Ordering::Acquire) {
                    process(event);
                }
            }
            Err(RecvTimeoutError::Timeout) => continue,
            Err(RecvTimeoutError::Disconnected) => break,
        }
    }
}

/// Turn a single [`ChordEvent`] into zero or one [`Effect`]s, peeking at
/// the matcher once for a same-Instant follow-up so upgrade transitions
/// coalesce into [`Effect::RestartRecording`] instead of a Stop+Start
/// pair.
fn process_event(
    app: &AppHandle,
    matcher: &ChordMatcher<ChordAction>,
    shutdown: &AtomicBool,
    event: ChordEvent<ChordAction>,
) {
    process_events_with_shutdown(
        shutdown,
        event,
        || matcher.recv_timeout(Duration::from_millis(5)).ok(),
        |effect| apply_effect(app, effect),
    );
}

fn process_events_with_shutdown(
    shutdown: &AtomicBool,
    event: ChordEvent<ChordAction>,
    mut next: impl FnMut() -> Option<ChordEvent<ChordAction>>,
    mut emit: impl FnMut(Effect),
) {
    process_events(
        event,
        || {
            if shutdown.load(Ordering::Acquire) {
                None
            } else {
                next()
            }
        },
        |effect| {
            if !shutdown.load(Ordering::Acquire) {
                emit(effect);
            }
        },
    );
}

fn process_events(
    mut event: ChordEvent<ChordAction>,
    mut next: impl FnMut() -> Option<ChordEvent<ChordAction>>,
    mut emit: impl FnMut(Effect),
) {
    loop {
        match event {
            ChordEvent::Start { id, .. } => {
                emit(Effect::StartRecording(id));
                return;
            }
            ChordEvent::End {
                id: end_id,
                time: end_time,
            } => match next() {
                Some(ChordEvent::Start {
                    id: start_id,
                    time: start_time,
                }) if end_id == ChordAction::PushToTalk
                    && start_id == ChordAction::ToggleToTalk
                    && start_time == end_time =>
                {
                    emit(Effect::RestartRecording(start_id));
                    return;
                }
                Some(other) => {
                    emit(Effect::StopRecording(end_id));
                    event = other;
                }
                None => {
                    emit(Effect::StopRecording(end_id));
                    return;
                }
            },
        }
    }
}

#[cfg(test)]
mod event_tests {
    use super::*;
    use std::collections::VecDeque;
    use std::time::Instant;

    fn effects(first: ChordEvent<ChordAction>, rest: Vec<ChordEvent<ChordAction>>) -> Vec<Effect> {
        let mut rest = VecDeque::from(rest);
        let mut output = Vec::new();
        process_events(first, || rest.pop_front(), |effect| output.push(effect));
        output
    }

    #[test]
    fn push_to_toggle_upgrade_restarts_recording() {
        let time = Instant::now();
        assert_eq!(
            effects(
                ChordEvent::End {
                    id: ChordAction::PushToTalk,
                    time
                },
                vec![ChordEvent::Start {
                    id: ChordAction::ToggleToTalk,
                    time
                }]
            ),
            vec![Effect::RestartRecording(ChordAction::ToggleToTalk)]
        );
    }
    #[test]
    fn unrelated_start_is_not_lost_during_lookahead() {
        let time = Instant::now();
        assert_eq!(
            effects(
                ChordEvent::End {
                    id: ChordAction::PushToTalk,
                    time
                },
                vec![ChordEvent::Start {
                    id: ChordAction::ToggleToTalk,
                    time: time + Duration::from_millis(1)
                }]
            ),
            vec![
                Effect::StopRecording(ChordAction::PushToTalk),
                Effect::StartRecording(ChordAction::ToggleToTalk)
            ]
        );
    }
    #[test]
    fn reverse_or_same_action_transitions_do_not_discard_recording() {
        let time = Instant::now();
        for (end_id, start_id) in [
            (ChordAction::ToggleToTalk, ChordAction::PushToTalk),
            (ChordAction::PushToTalk, ChordAction::PushToTalk),
            (ChordAction::ToggleToTalk, ChordAction::ToggleToTalk),
        ] {
            assert_eq!(
                effects(
                    ChordEvent::End { id: end_id, time },
                    vec![ChordEvent::Start { id: start_id, time }]
                ),
                vec![
                    Effect::StopRecording(end_id),
                    Effect::StartRecording(start_id)
                ]
            );
        }
    }
    #[test]
    fn long_runs_of_end_events_are_processed_without_recursion() {
        let time = Instant::now();
        let count = 10_000;
        let output = effects(
            ChordEvent::End {
                id: ChordAction::PushToTalk,
                time,
            },
            (1..count)
                .map(|_| ChordEvent::End {
                    id: ChordAction::PushToTalk,
                    time,
                })
                .collect(),
        );
        assert_eq!(output.len(), count);
        assert!(output
            .iter()
            .all(|effect| *effect == Effect::StopRecording(ChordAction::PushToTalk)));
    }
}

// ========================================================================
// Effect → Tauri
// ========================================================================

trait DictationTarget {
    type Window;
    fn capture_focus(&self) -> Option<focus_capture::FocusSnapshot>;
    fn window(&self) -> Option<Self::Window>;
    fn present(&self, window: &Self::Window);
    fn start(&self, window: &Self::Window, payload: serde_json::Value);
    fn signal(&self, window: &Self::Window, event: &'static str);
}

struct NativeDictationTarget<'a, R: tauri::Runtime>(&'a AppHandle<R>);

impl<R: tauri::Runtime> DictationTarget for NativeDictationTarget<'_, R> {
    type Window = tauri::WebviewWindow<R>;
    fn capture_focus(&self) -> Option<focus_capture::FocusSnapshot> {
        focus_capture::capture_focus().ok()
    }
    fn window(&self) -> Option<Self::Window> {
        self.0.get_webview_window(DICTATE_WINDOW_LABEL)
    }
    fn present(&self, window: &Self::Window) {
        // The previous hide-cycle parked the window off-screen and
        // made it click-through — undo both before showing, so the
        // pill lands at top-center and the user can actually click
        // the error pill / stop button.
        //
        // `current_monitor()` returns None when the window is off
        // any display (our hide handler parks it at -10_000, -10_000
        // precisely so it never intercepts clicks), so fall back to
        // the primary monitor for the reposition.
        let monitor = window
            .current_monitor()
            .ok()
            .flatten()
            .or_else(|| window.primary_monitor().ok().flatten());
        if let Some(monitor) = monitor {
            let monitor_pos = monitor.position();
            let monitor_size = monitor.size();
            if let Ok(win_size) = window.outer_size() {
                let x = monitor_pos.x + (monitor_size.width as i32 - win_size.width as i32) / 2;
                let y = monitor_pos.y + (monitor_size.height as f64 * 0.04) as i32;
                let _ = window.set_position(tauri::PhysicalPosition::new(x, y));
            }
        }
        // Skip on Linux: aborts if the window was never realized
        // (see show_dictate_window in main.rs).
        #[cfg(not(target_os = "linux"))]
        let _ = window.set_ignore_cursor_events(false);
        // Deliberately no set_focus() — taking key focus would yank
        // it out of whatever app the user was typing in, which is
        // the opposite of what a dictation overlay should do.
        let _ = window.show();
    }
    fn start(&self, window: &Self::Window, payload: serde_json::Value) {
        let _ = window.emit("dictate:start", payload);
    }
    fn signal(&self, window: &Self::Window, event: &'static str) {
        let _ = window.emit(event, ());
    }
}

fn apply_effect<R: tauri::Runtime>(app: &AppHandle<R>, effect: Effect) {
    apply_effect_to(&NativeDictationTarget(app), effect);
}

fn apply_effect_to(target: &impl DictationTarget, effect: Effect) {
    match effect {
        Effect::StartRecording(_) => {
            // Focus must be captured before positioning or showing the pill.
            let focus = target.capture_focus();
            if let Some(window) = target.window() {
                target.present(&window);
                target.start(&window, serde_json::json!({ "focus": focus }));
            }
        }
        Effect::StopRecording(_) => {
            if let Some(window) = target.window() {
                target.signal(&window, "dictate:stop");
            }
        }
        Effect::RestartRecording(_) => {
            if let Some(window) = target.window() {
                target.signal(&window, "dictate:restart");
            }
        }
    }
}

#[cfg(test)]
mod shutdown_tests {
    use super::*;
    use std::time::Instant;

    fn start() -> ChordEvent<ChordAction> {
        ChordEvent::Start {
            id: ChordAction::PushToTalk,
            time: Instant::now(),
        }
    }

    #[test]
    fn shutdown_during_receive_does_not_process_a_late_hotkey() {
        let shutdown = AtomicBool::new(false);
        let mut processed = 0;
        run_dispatcher(
            &shutdown,
            || {
                shutdown.store(true, Ordering::Release);
                Ok(start())
            },
            |_| processed += 1,
        );
        assert_eq!(processed, 0);
    }

    #[test]
    fn shutdown_during_upgrade_lookahead_does_not_emit_restart() {
        let shutdown = AtomicBool::new(false);
        let time = Instant::now();
        let first = ChordEvent::End {
            id: ChordAction::PushToTalk,
            time,
        };
        let mut effects = Vec::new();
        process_events_with_shutdown(
            &shutdown,
            first,
            || {
                shutdown.store(true, Ordering::Release);
                Some(ChordEvent::Start {
                    id: ChordAction::ToggleToTalk,
                    time,
                })
            },
            |effect| effects.push(effect),
        );
        assert!(effects.is_empty());
    }

    #[test]
    fn shutdown_does_not_wait_for_further_lookahead() {
        let shutdown = AtomicBool::new(true);
        process_events_with_shutdown(
            &shutdown,
            ChordEvent::End {
                id: ChordAction::PushToTalk,
                time: Instant::now(),
            },
            || panic!("lookahead polled after shutdown"),
            |_| panic!("effect emitted after shutdown"),
        );
    }

    #[test]
    fn shutdown_before_receive_never_polls_the_matcher() {
        let shutdown = AtomicBool::new(true);
        run_dispatcher(
            &shutdown,
            || panic!("matcher polled after shutdown"),
            |_| panic!("event processed"),
        );
    }

    #[test]
    fn timeouts_are_retried_and_disconnect_stops_the_dispatcher() {
        let shutdown = AtomicBool::new(false);
        let mut polls = 0;
        let mut count = 0;
        run_dispatcher(
            &shutdown,
            || {
                polls += 1;
                match polls {
                    1 => Err(RecvTimeoutError::Timeout),
                    2 => Ok(start()),
                    _ => Err(RecvTimeoutError::Disconnected),
                }
            },
            |_| count += 1,
        );
        assert_eq!((polls, count), (3, 1));
    }

    #[test]
    fn active_upgrade_still_emits_a_single_restart() {
        let shutdown = AtomicBool::new(false);
        let time = Instant::now();
        let mut effects = Vec::new();
        process_events_with_shutdown(
            &shutdown,
            ChordEvent::End {
                id: ChordAction::PushToTalk,
                time,
            },
            || {
                Some(ChordEvent::Start {
                    id: ChordAction::ToggleToTalk,
                    time,
                })
            },
            |effect| effects.push(effect),
        );
        assert_eq!(
            effects,
            [Effect::RestartRecording(ChordAction::ToggleToTalk)]
        );
    }

    #[test]
    fn shutdown_between_unrelated_effects_suppresses_the_followup_start() {
        let shutdown = AtomicBool::new(false);
        let time = Instant::now();
        let mut effects = Vec::new();
        process_events_with_shutdown(
            &shutdown,
            ChordEvent::End {
                id: ChordAction::PushToTalk,
                time,
            },
            || {
                Some(ChordEvent::Start {
                    id: ChordAction::ToggleToTalk,
                    time: time + Duration::from_millis(1),
                })
            },
            |effect| {
                effects.push(effect);
                shutdown.store(true, Ordering::Release);
            },
        );
        assert_eq!(effects, [Effect::StopRecording(ChordAction::PushToTalk)]);
    }
}

#[cfg(test)]
mod teardown_tests {
    use super::*;
    use std::sync::atomic::AtomicBool;

    #[test]
    fn stopping_an_active_matcher_notifies_after_the_dispatcher_has_exited() {
        let shutdown = Arc::new(AtomicBool::new(false));
        let exited = Arc::new(AtomicBool::new(false));
        let worker_shutdown = shutdown.clone();
        let worker_exited = exited.clone();
        let dispatcher = std::thread::spawn(move || {
            while !worker_shutdown.load(Ordering::Acquire) {
                std::thread::yield_now();
            }
            worker_exited.store(true, Ordering::Release);
        });
        let mut notifications = 0;
        stop_dispatcher(
            Some(Active {
                dispatcher,
                shutdown,
            }),
            || {
                assert!(exited.load(Ordering::Acquire));
                notifications += 1;
            },
        );
        assert_eq!(notifications, 1);
    }

    #[test]
    fn no_matcher_does_not_send_a_spurious_stop() {
        let mut notifications = 0;
        stop_dispatcher(None, || notifications += 1);
        assert_eq!(notifications, 0);
    }
}

#[cfg(test)]
mod window_bridge_tests {
    use super::*;
    use std::cell::RefCell;
    use std::time::Instant;

    struct FakeTarget {
        has_window: bool,
        focus: Option<focus_capture::FocusSnapshot>,
        calls: RefCell<Vec<String>>,
        payloads: RefCell<Vec<serde_json::Value>>,
    }
    impl FakeTarget {
        fn new(has_window: bool, focus: bool) -> Self {
            Self {
                has_window,
                focus: focus.then(|| focus_capture::FocusSnapshot {
                    pid: 42,
                    bundle_id: Some("other.app".into()),
                    role: Some("AXTextField".into()),
                }),
                calls: RefCell::new(vec![]),
                payloads: RefCell::new(vec![]),
            }
        }
        fn calls(&self) -> Vec<String> {
            self.calls.borrow().clone()
        }
    }
    impl DictationTarget for FakeTarget {
        type Window = ();
        fn capture_focus(&self) -> Option<focus_capture::FocusSnapshot> {
            self.calls.borrow_mut().push("focus".into());
            self.focus.clone()
        }
        fn window(&self) -> Option<Self::Window> {
            self.calls.borrow_mut().push("window".into());
            self.has_window.then_some(())
        }
        fn present(&self, _: &()) {
            self.calls.borrow_mut().push("show".into());
        }
        fn start(&self, _: &(), payload: serde_json::Value) {
            self.calls.borrow_mut().push("dictate:start".into());
            self.payloads.borrow_mut().push(payload);
        }
        fn signal(&self, _: &(), event: &'static str) {
            self.calls.borrow_mut().push(event.into());
        }
    }

    #[test]
    fn both_start_actions_capture_focus_before_showing_the_pill() {
        for action in [ChordAction::PushToTalk, ChordAction::ToggleToTalk] {
            let target = FakeTarget::new(true, true);
            apply_effect_to(&target, Effect::StartRecording(action));
            assert_eq!(target.calls(), ["focus", "window", "show", "dictate:start"]);
            assert_eq!(
                target.payloads.borrow()[0],
                serde_json::json!({"focus":{"pid":42,"bundle_id":"other.app","role":"AXTextField"}})
            );
        }
    }

    #[test]
    fn unavailable_focus_is_reported_as_null_without_blocking_recording() {
        let target = FakeTarget::new(true, false);
        apply_effect_to(&target, Effect::StartRecording(ChordAction::PushToTalk));
        assert_eq!(target.calls(), ["focus", "window", "show", "dictate:start"]);
        assert_eq!(
            target.payloads.borrow()[0],
            serde_json::json!({"focus":null})
        );
    }

    #[test]
    fn absent_window_does_not_emit_start_stop_or_restart() {
        let target = FakeTarget::new(false, true);
        apply_effect_to(&target, Effect::StartRecording(ChordAction::PushToTalk));
        apply_effect_to(&target, Effect::StopRecording(ChordAction::PushToTalk));
        apply_effect_to(&target, Effect::RestartRecording(ChordAction::ToggleToTalk));
        assert_eq!(target.calls(), ["focus", "window", "window", "window"]);
        assert!(target.payloads.borrow().is_empty());
    }

    #[test]
    fn stop_and_restart_are_distinct_and_do_not_change_focus_or_show_the_window() {
        let target = FakeTarget::new(true, true);
        apply_effect_to(&target, Effect::StopRecording(ChordAction::PushToTalk));
        apply_effect_to(&target, Effect::RestartRecording(ChordAction::ToggleToTalk));
        assert_eq!(
            target.calls(),
            ["window", "dictate:stop", "window", "dictate:restart"]
        );
    }

    #[test]
    fn push_to_hands_free_upgrade_emits_only_restart() {
        let target = FakeTarget::new(true, true);
        let time = Instant::now();
        process_events(
            ChordEvent::End {
                id: ChordAction::PushToTalk,
                time,
            },
            || {
                Some(ChordEvent::Start {
                    id: ChordAction::ToggleToTalk,
                    time,
                })
            },
            |effect| apply_effect_to(&target, effect),
        );
        assert_eq!(target.calls(), ["window", "dictate:restart"]);
    }

    #[test]
    fn unrelated_chords_stop_then_start_with_a_new_focus_snapshot() {
        let target = FakeTarget::new(true, true);
        let time = Instant::now();
        process_events(
            ChordEvent::End {
                id: ChordAction::PushToTalk,
                time,
            },
            || {
                Some(ChordEvent::Start {
                    id: ChordAction::ToggleToTalk,
                    time: time + Duration::from_millis(1),
                })
            },
            |effect| apply_effect_to(&target, effect),
        );
        assert_eq!(
            target.calls(),
            [
                "window",
                "dictate:stop",
                "focus",
                "window",
                "show",
                "dictate:start"
            ]
        );
        assert_eq!(target.payloads.borrow()[0]["focus"]["pid"], 42);
    }
}

#[cfg(test)]
mod tauri_target_integration_tests {
    use super::*;
    use std::sync::Mutex;
    use tauri::Listener;

    fn window(
        app: &tauri::App<tauri::test::MockRuntime>,
    ) -> tauri::WebviewWindow<tauri::test::MockRuntime> {
        tauri::WebviewWindowBuilder::new(app, DICTATE_WINDOW_LABEL, Default::default())
            .build()
            .unwrap()
    }

    #[test]
    fn native_target_delivers_recording_signals_to_dictate_window() {
        let app = tauri::test::mock_app();
        let window = window(&app);
        let events = Arc::new(Mutex::new(Vec::<(String, serde_json::Value)>::new()));
        for name in ["dictate:start", "dictate:stop", "dictate:restart"] {
            let events = events.clone();
            window.listen(name, move |event| {
                events
                    .lock()
                    .unwrap()
                    .push((name.into(), serde_json::from_str(event.payload()).unwrap()));
            });
        }
        let target = NativeDictationTarget(app.handle());
        let payload =
            serde_json::json!({"focus":{"pid":42,"bundle_id":"example.app","role":"AXTextField"}});
        target.present(&window);
        target.start(&window, payload.clone());
        apply_effect(app.handle(), Effect::StopRecording(ChordAction::PushToTalk));
        apply_effect(
            app.handle(),
            Effect::RestartRecording(ChordAction::ToggleToTalk),
        );
        assert_eq!(
            *events.lock().unwrap(),
            [
                ("dictate:start".into(), payload),
                ("dictate:stop".into(), serde_json::Value::Null),
                ("dictate:restart".into(), serde_json::Value::Null),
            ]
        );
    }

    #[test]
    fn stop_and_restart_are_safe_when_dictate_window_is_missing() {
        let app = tauri::test::mock_app();
        apply_effect(app.handle(), Effect::StopRecording(ChordAction::PushToTalk));
        apply_effect(
            app.handle(),
            Effect::RestartRecording(ChordAction::ToggleToTalk),
        );
        assert!(app.get_webview_window(DICTATE_WINDOW_LABEL).is_none());
    }
}
