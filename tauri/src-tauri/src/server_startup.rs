//! Observe sidecar startup events without relying on process-global state.
use std::time::Duration;
use tauri::async_runtime::Receiver;
use tauri_plugin_shell::process::CommandEvent;

/// Serialize startup attempts and coordinate stop with child registration.
pub(crate) struct StartGate {
    state: std::sync::Mutex<StartState>,
    changed: tokio::sync::watch::Sender<()>,
}

impl Default for StartGate {
    fn default() -> Self {
        let (changed, _) = tokio::sync::watch::channel(());
        Self {
            state: std::sync::Mutex::new(StartState::default()),
            changed,
        }
    }
}

#[derive(Default)]
struct StartState {
    starting: bool,
    stopping: usize,
    generation: u64,
}

pub(crate) struct StartPermit<'a> {
    gate: &'a StartGate,
    generation: u64,
}

pub(crate) struct StopPermit {
    gate: std::sync::Arc<StartGate>,
}

impl Drop for StopPermit {
    fn drop(&mut self) {
        self.gate.state.lock().unwrap().stopping -= 1;
        self.gate.changed.send_replace(());
    }
}

impl StartGate {
    pub(crate) fn begin_stop<T>(
        self: &std::sync::Arc<Self>,
        action: impl FnOnce() -> T,
    ) -> (T, StopPermit) {
        self.begin_cleanup_inner(true, action)
    }

    pub(crate) fn begin_cleanup<T>(
        self: &std::sync::Arc<Self>,
        action: impl FnOnce() -> T,
    ) -> (T, StopPermit) {
        self.begin_cleanup_inner(false, action)
    }

    fn begin_cleanup_inner<T>(
        self: &std::sync::Arc<Self>,
        cancel_start: bool,
        action: impl FnOnce() -> T,
    ) -> (T, StopPermit) {
        let mut state = self.state.lock().unwrap();
        if cancel_start {
            state.generation = state.generation.wrapping_add(1);
        }
        state.stopping += 1;
        let result = action();
        drop(state);
        self.changed.send_replace(());
        (result, StopPermit { gate: self.clone() })
    }

    pub(crate) fn try_begin(&self) -> Option<StartPermit<'_>> {
        let mut state = self.state.lock().unwrap();
        if state.starting || state.stopping > 0 {
            return None;
        }
        state.starting = true;
        Some(StartPermit {
            gate: self,
            generation: state.generation,
        })
    }

    #[cfg(test)]
    pub(crate) fn cancel_and<T>(&self, action: impl FnOnce() -> T) -> T {
        let mut state = self.state.lock().unwrap();
        state.generation = state.generation.wrapping_add(1);
        let result = action();
        drop(state);
        self.changed.send_replace(());
        result
    }

    pub(crate) async fn wait_until_idle(&self) {
        let mut changed = self.changed.subscribe();
        loop {
            let idle = {
                let state = self.state.lock().unwrap();
                !state.starting && state.stopping == 0
            };
            if idle {
                return;
            }
            if changed.changed().await.is_err() {
                return;
            }
        }
    }
}

impl StartPermit<'_> {
    pub(crate) fn commit<T>(&self, action: impl FnOnce() -> T) -> Option<T> {
        let state = self.gate.state.lock().unwrap();
        if state.generation != self.generation {
            return None;
        }
        let result = action();
        drop(state);
        Some(result)
    }
}

impl StartPermit<'_> {
    pub(crate) async fn until_cancelled<F: std::future::Future>(
        &self,
        work: F,
    ) -> Option<F::Output> {
        let mut changed = self.gate.changed.subscribe();
        let cancelled = async {
            loop {
                if self.gate.state.lock().unwrap().generation != self.generation {
                    return;
                }
                if changed.changed().await.is_err() {
                    return;
                }
            }
        };
        tokio::select! {
            biased;
            _ = cancelled => None,
            result = work => Some(result),
        }
    }
}

impl Drop for StartPermit<'_> {
    fn drop(&mut self) {
        self.gate.state.lock().unwrap().starting = false;
        self.gate.changed.send_replace(());
    }
}

#[derive(Debug, PartialEq, Eq)]
pub(crate) enum Outcome {
    Ready,
    Exited,
    TimedOut(Vec<String>),
}

pub(crate) async fn wait_for_startup(
    rx: &mut Receiver<CommandEvent>,
    timeout: Duration,
    mut log: impl FnMut(&str, &str),
) -> Outcome {
    let deadline = tokio::time::Instant::now() + timeout;
    let mut errors = Vec::new();
    loop {
        let event = match tokio::time::timeout_at(deadline, rx.recv()).await {
            Err(_) => return Outcome::TimedOut(errors),
            Ok(None) => return Outcome::Exited,
            Ok(Some(event)) => event,
        };
        let (stream, line) = match event {
            CommandEvent::Stdout(bytes) => ("stdout", bytes),
            CommandEvent::Stderr(bytes) => ("stderr", bytes),
            CommandEvent::Terminated(_) => return Outcome::Exited,
            _ => continue,
        };
        let line = String::from_utf8_lossy(&line);
        log(stream, line.trim_end());
        if stream == "stderr"
            && (line.contains("ERROR") || line.contains("Error") || line.contains("Failed"))
        {
            errors.push(line.to_string());
        }
        if line.contains("Uvicorn running") {
            return Outcome::Ready;
        }
    }
}

/// Forward sidecar output after readiness. Only a confirmed termination may
/// clear ownership; a channel error alone does not prove the child has exited.
pub(crate) async fn forward_events(
    rx: &mut Receiver<CommandEvent>,
    mut log: impl FnMut(&str, &str),
    mut on_termination: impl FnMut(),
) {
    while let Some(event) = rx.recv().await {
        match event {
            CommandEvent::Stdout(bytes) => {
                log("stdout", String::from_utf8_lossy(&bytes).trim_end())
            }
            CommandEvent::Stderr(bytes) => {
                log("stderr", String::from_utf8_lossy(&bytes).trim_end())
            }
            CommandEvent::Terminated(_) => {
                on_termination();
                break;
            }
            _ => {}
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tauri::async_runtime::channel;
    use tauri_plugin_shell::process::TerminatedPayload;

    #[tokio::test]
    async fn startup_logs_are_forwarded_and_ready_signals_from_either_stream_are_recognized() {
        for (ready, stream) in [
            (
                CommandEvent::Stdout(b"Uvicorn running on http://127.0.0.1\n".to_vec()),
                "stdout",
            ),
            (
                CommandEvent::Stderr(b"Uvicorn running on http://127.0.0.1\n".to_vec()),
                "stderr",
            ),
        ] {
            let (tx, mut rx) = channel(4);
            tx.send(CommandEvent::Stdout(b"Loading model\n".to_vec()))
                .await
                .unwrap();
            tx.send(ready).await.unwrap();
            let mut logs = Vec::new();
            let result = wait_for_startup(&mut rx, Duration::from_secs(1), |source, line| {
                logs.push((source.to_string(), line.to_string()))
            })
            .await;
            assert_eq!(result, Outcome::Ready);
            assert_eq!(logs[0], ("stdout".into(), "Loading model".into()));
            assert_eq!(logs[1].0, stream);
            assert!(!logs[1].1.ends_with('\n'));
        }
    }

    #[tokio::test]
    async fn application_startup_message_does_not_hide_a_port_bind_failure() {
        let (tx, mut rx) = channel(3);
        tx.send(CommandEvent::Stderr(
            b"INFO: Application startup complete.\n".to_vec(),
        ))
        .await
        .unwrap();
        tx.send(CommandEvent::Stderr(
            b"ERROR: address already in use\n".to_vec(),
        ))
        .await
        .unwrap();
        tx.send(CommandEvent::Terminated(TerminatedPayload {
            code: Some(1),
            signal: None,
        }))
        .await
        .unwrap();
        let mut logs = Vec::new();
        let outcome = wait_for_startup(&mut rx, Duration::from_secs(1), |_, line| {
            logs.push(line.to_string())
        })
        .await;
        assert_eq!(outcome, Outcome::Exited);
        assert!(logs
            .iter()
            .any(|line| line.contains("address already in use")));
    }

    #[tokio::test]
    async fn application_initialization_alone_times_out_if_no_listener_is_started() {
        let (tx, mut rx) = channel(1);
        tx.send(CommandEvent::Stderr(
            b"INFO: Application startup complete.".to_vec(),
        ))
        .await
        .unwrap();
        assert_eq!(
            wait_for_startup(&mut rx, Duration::from_millis(30), |_, _| {}).await,
            Outcome::TimedOut(vec![])
        );
    }

    #[tokio::test]
    async fn initialization_then_listening_reports_ready_and_forwards_both_logs() {
        let (tx, mut rx) = channel(2);
        tx.send(CommandEvent::Stderr(
            b"INFO: Application startup complete.".to_vec(),
        ))
        .await
        .unwrap();
        tx.send(CommandEvent::Stderr(
            b"INFO: Uvicorn running on http://127.0.0.1:17493".to_vec(),
        ))
        .await
        .unwrap();
        let mut logs = Vec::new();
        assert_eq!(
            wait_for_startup(&mut rx, Duration::from_secs(1), |_, line| logs
                .push(line.to_string()))
            .await,
            Outcome::Ready
        );
        assert_eq!(logs.len(), 2);
    }

    #[tokio::test]
    async fn ready_leaves_later_events_for_the_log_forwarder() {
        let (tx, mut rx) = channel(3);
        tx.send(CommandEvent::Stderr(
            b"Uvicorn running on http://127.0.0.1".to_vec(),
        ))
        .await
        .unwrap();
        tx.send(CommandEvent::Stdout(b"later log".to_vec()))
            .await
            .unwrap();
        assert_eq!(
            wait_for_startup(&mut rx, Duration::from_secs(1), |_, _| {}).await,
            Outcome::Ready
        );
        assert!(
            matches!(rx.recv().await, Some(CommandEvent::Stdout(bytes)) if bytes == b"later log")
        );
    }

    #[tokio::test]
    async fn continuous_logs_do_not_reset_the_overall_deadline() {
        let (tx, mut rx) = channel(4);
        let producer = tokio::spawn(async move {
            for _ in 0..20 {
                if tx
                    .send(CommandEvent::Stdout(b"loading".to_vec()))
                    .await
                    .is_err()
                {
                    break;
                }
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
        });
        assert_eq!(
            wait_for_startup(&mut rx, Duration::from_millis(30), |_, _| {}).await,
            Outcome::TimedOut(vec![])
        );
        drop(rx);
        producer.await.unwrap();
    }

    #[tokio::test]
    async fn closed_channel_reports_exit() {
        let (tx, mut rx) = channel(1);
        drop(tx);
        assert_eq!(
            wait_for_startup(&mut rx, Duration::from_secs(1), |_, _| {}).await,
            Outcome::Exited
        );
    }

    #[tokio::test]
    async fn termination_is_reported_even_if_event_channel_remains_open() {
        let (tx, mut rx) = channel(1);
        tx.send(CommandEvent::Terminated(TerminatedPayload {
            code: Some(1),
            signal: None,
        }))
        .await
        .unwrap();
        assert_eq!(
            wait_for_startup(&mut rx, Duration::from_millis(30), |_, _| {}).await,
            Outcome::Exited
        );
    }

    #[tokio::test]
    async fn timeout_collects_only_error_lines_and_preserves_forwarded_output() {
        let (tx, mut rx) = channel(4);
        tx.send(CommandEvent::Stdout(
            b"Error from standard output\n".to_vec(),
        ))
        .await
        .unwrap();
        tx.send(CommandEvent::Stderr(b"warning\n".to_vec()))
            .await
            .unwrap();
        tx.send(CommandEvent::Stderr(b"ERROR bad model\n".to_vec()))
            .await
            .unwrap();
        let mut logs = Vec::new();
        let result = wait_for_startup(&mut rx, Duration::from_millis(30), |source, line| {
            logs.push((source.to_string(), line.to_string()))
        })
        .await;
        assert_eq!(result, Outcome::TimedOut(vec!["ERROR bad model\n".into()]));
        assert_eq!(logs.len(), 3);
    }

    #[tokio::test]
    async fn malformed_output_is_forwarded_without_panicking() {
        let (tx, mut rx) = channel(2);
        tx.send(CommandEvent::Stderr(vec![b'x', 0xff, b'\n']))
            .await
            .unwrap();
        drop(tx);
        let mut logs = Vec::new();
        assert_eq!(
            wait_for_startup(&mut rx, Duration::from_secs(1), |_, line| logs
                .push(line.to_string()))
            .await,
            Outcome::Exited
        );
        assert_eq!(logs, ["x�"]);
    }

    #[tokio::test]
    async fn termination_clears_only_the_matching_managed_child() {
        use std::sync::Mutex;
        let pid = Mutex::new(Some(123));
        let child = Mutex::new(Some("child"));
        let (tx, mut rx) = channel(2);
        tx.send(CommandEvent::Terminated(TerminatedPayload {
            code: Some(1),
            signal: None,
        }))
        .await
        .unwrap();
        drop(tx);
        forward_events(
            &mut rx,
            |_, _| {},
            || {
                crate::server_process::take_process(&pid, &child, Some(123));
            },
        )
        .await;
        assert_eq!(*pid.lock().unwrap(), None);
        assert_eq!(*child.lock().unwrap(), None);
    }

    #[tokio::test]
    async fn late_termination_does_not_clear_a_replacement_child() {
        use std::sync::Mutex;
        let pid = Mutex::new(Some(456));
        let child = Mutex::new(Some("replacement"));
        let (tx, mut rx) = channel(2);
        tx.send(CommandEvent::Terminated(TerminatedPayload {
            code: Some(0),
            signal: None,
        }))
        .await
        .unwrap();
        drop(tx);
        let mut called = false;
        forward_events(
            &mut rx,
            |_, _| {},
            || {
                called = true;
                crate::server_process::take_process(&pid, &child, Some(123));
            },
        )
        .await;
        assert!(called);
        assert_eq!(*pid.lock().unwrap(), Some(456));
        assert_eq!(*child.lock().unwrap(), Some("replacement"));
    }

    #[tokio::test]
    async fn post_start_logs_are_forwarded_before_termination() {
        let (tx, mut rx) = channel(4);
        tx.send(CommandEvent::Stdout(b"hello\n".to_vec()))
            .await
            .unwrap();
        tx.send(CommandEvent::Stderr(b"warning\n".to_vec()))
            .await
            .unwrap();
        tx.send(CommandEvent::Terminated(TerminatedPayload {
            code: None,
            signal: Some(9),
        }))
        .await
        .unwrap();
        drop(tx);
        let mut logs = Vec::new();
        let mut terminated = 0;
        forward_events(
            &mut rx,
            |stream, line| logs.push((stream.to_string(), line.to_string())),
            || terminated += 1,
        )
        .await;
        assert_eq!(
            logs,
            [
                ("stdout".into(), "hello".into()),
                ("stderr".into(), "warning".into())
            ]
        );
        assert_eq!(terminated, 1);
    }

    #[tokio::test]
    async fn channel_closure_without_termination_does_not_clear_a_live_child() {
        let (tx, mut rx) = channel(1);
        tx.send(CommandEvent::Error("reader failed".into()))
            .await
            .unwrap();
        drop(tx);
        let mut terminated = false;
        forward_events(&mut rx, |_, _| {}, || terminated = true).await;
        assert!(!terminated);
    }
}

#[cfg(test)]
mod start_gate_tests {
    use super::StartGate;
    use std::sync::{Arc, Barrier};

    #[test]
    fn overlapping_startup_is_rejected_until_the_first_attempt_ends() {
        let gate = StartGate::default();
        let first = gate.try_begin().unwrap();
        assert!(gate.try_begin().is_none());
        drop(first);
        assert!(gate.try_begin().is_some());
    }

    #[test]
    fn only_one_simultaneous_caller_can_enter() {
        let gate = Arc::new(StartGate::default());
        let ready = Arc::new(Barrier::new(3));
        let finished = Arc::new(Barrier::new(3));
        let workers: Vec<_> = (0..2)
            .map(|_| {
                let gate = gate.clone();
                let ready = ready.clone();
                let finished = finished.clone();
                std::thread::spawn(move || {
                    ready.wait();
                    let permit = gate.try_begin();
                    let admitted = permit.is_some();
                    finished.wait();
                    admitted
                })
            })
            .collect();
        ready.wait();
        finished.wait();
        assert_eq!(
            workers
                .into_iter()
                .map(|worker| worker.join().unwrap())
                .filter(|admitted| *admitted)
                .count(),
            1
        );
        assert!(gate.try_begin().is_some());
    }

    #[test]
    fn failure_unwinds_and_allows_a_retry() {
        fn failed(gate: &StartGate) -> Result<(), &'static str> {
            let _permit = gate.try_begin().ok_or("busy")?;
            Err("spawn failed")
        }
        let gate = StartGate::default();
        assert_eq!(failed(&gate), Err("spawn failed"));
        assert!(gate.try_begin().is_some());
    }

    #[tokio::test]
    async fn cancelling_an_inflight_start_releases_its_permit() {
        let gate = Arc::new(StartGate::default());
        let worker_gate = gate.clone();
        let (entered_tx, entered_rx) = tokio::sync::oneshot::channel();
        let worker = tokio::spawn(async move {
            let _permit = worker_gate.try_begin().unwrap();
            entered_tx.send(()).unwrap();
            std::future::pending::<()>().await;
        });
        entered_rx.await.unwrap();
        assert!(gate.try_begin().is_none());
        worker.abort();
        assert!(worker.await.unwrap_err().is_cancelled());
        assert!(gate.try_begin().is_some());
    }

    #[test]
    fn independent_app_states_do_not_share_a_gate() {
        let first = StartGate::default();
        let second = StartGate::default();
        let _permit = first.try_begin().unwrap();
        assert!(second.try_begin().is_some());
    }
}

#[cfg(test)]
mod cancellation_tests {
    use super::StartGate;
    use std::sync::{mpsc, Arc, Mutex};

    #[test]
    fn stopping_during_preflight_prevents_late_spawn() {
        let gate = StartGate::default();
        let permit = gate.try_begin().unwrap();
        gate.cancel_and(|| ());
        let mut spawned = false;
        assert!(permit.commit(|| spawned = true).is_none());
        assert!(!spawned);
        assert!(gate.try_begin().is_none());
        drop(permit);
        assert!(gate.try_begin().is_some());
    }

    #[test]
    fn stop_after_registration_takes_the_registered_child() {
        let gate = StartGate::default();
        let child = Mutex::new(None);
        let permit = gate.try_begin().unwrap();
        assert!(permit
            .commit(|| *child.lock().unwrap() = Some(42))
            .is_some());
        let stopped = gate.cancel_and(|| child.lock().unwrap().take());
        assert_eq!(stopped, Some(42));
        assert!(permit.commit(|| ()).is_none());
    }

    #[test]
    fn cancellation_before_a_new_attempt_does_not_cancel_the_new_attempt() {
        let gate = StartGate::default();
        gate.cancel_and(|| ());
        let permit = gate.try_begin().unwrap();
        assert_eq!(permit.commit(|| 7), Some(7));
    }

    #[cfg(unix)]
    #[test]
    fn registered_real_child_can_be_taken_and_stopped() {
        struct Child(std::process::Child);
        impl Drop for Child {
            fn drop(&mut self) {
                let _ = self.0.kill();
                let _ = self.0.wait();
            }
        }
        let gate = StartGate::default();
        let child = Mutex::new(None);
        let permit = gate.try_begin().unwrap();
        permit
            .commit(|| {
                *child.lock().unwrap() = Some(Child(
                    std::process::Command::new("/bin/sleep")
                        .arg("30")
                        .spawn()
                        .unwrap(),
                ));
            })
            .unwrap();
        let mut owned = gate.cancel_and(|| child.lock().unwrap().take()).unwrap();
        owned.0.kill().unwrap();
        let status = owned.0.wait().unwrap();
        assert!(!status.success());
        assert!(permit.commit(|| ()).is_none());
    }

    #[test]
    fn concurrent_stop_waits_for_registration_then_sees_the_child() {
        let gate = Arc::new(StartGate::default());
        let child = Arc::new(Mutex::new(None));
        let (inside_tx, inside_rx) = mpsc::channel();
        let (resume_tx, resume_rx) = mpsc::channel();
        let (stop_tx, stop_rx) = mpsc::channel();
        let worker_gate = gate.clone();
        let worker_child = child.clone();
        let worker = std::thread::spawn(move || {
            let permit = worker_gate.try_begin().unwrap();
            permit
                .commit(|| {
                    inside_tx.send(()).unwrap();
                    resume_rx.recv().unwrap();
                    *worker_child.lock().unwrap() = Some(99);
                })
                .unwrap();
        });
        inside_rx.recv().unwrap();
        let stop_gate = gate.clone();
        let stop_child = child.clone();
        let stopper = std::thread::spawn(move || {
            let stopped = stop_gate.cancel_and(|| stop_child.lock().unwrap().take());
            stop_tx.send(stopped).unwrap();
        });
        assert!(stop_rx.try_recv().is_err());
        resume_tx.send(()).unwrap();
        assert_eq!(stop_rx.recv().unwrap(), Some(99));
        worker.join().unwrap();
        stopper.join().unwrap();
    }
}

#[cfg(test)]
mod restart_wait_tests {
    use super::StartGate;
    use std::sync::Arc;
    use std::time::Duration;

    #[tokio::test]
    async fn restart_waits_until_the_cancelled_start_has_unwound() {
        let gate = StartGate::default();
        let permit = gate.try_begin().unwrap();
        gate.cancel_and(|| ());
        assert!(
            tokio::time::timeout(Duration::from_millis(20), gate.wait_until_idle())
                .await
                .is_err()
        );
        drop(permit);
        tokio::time::timeout(Duration::from_secs(1), gate.wait_until_idle())
            .await
            .unwrap();
        assert!(gate.try_begin().is_some());
    }

    #[tokio::test]
    async fn cancelled_preflight_interrupts_pending_work() {
        let gate = StartGate::default();
        let permit = gate.try_begin().unwrap();
        gate.cancel_and(|| ());
        let result = tokio::time::timeout(
            Duration::from_millis(100),
            permit.until_cancelled(std::future::pending::<()>()),
        )
        .await;
        assert_eq!(result.unwrap(), None);
    }

    #[tokio::test]
    async fn cancellation_wakes_a_probe_already_in_progress() {
        let gate = StartGate::default();
        let permit = gate.try_begin().unwrap();
        let (entered_tx, entered_rx) = tokio::sync::oneshot::channel();
        let work = async {
            entered_tx.send(()).unwrap();
            std::future::pending::<()>().await
        };
        let waiting = permit.until_cancelled(work);
        tokio::pin!(waiting);
        tokio::select! {
            _ = &mut waiting => panic!("probe ended before cancellation"),
            _ = entered_rx => {}
        }
        gate.cancel_and(|| ());
        assert_eq!(
            tokio::time::timeout(Duration::from_secs(1), waiting)
                .await
                .unwrap(),
            None
        );
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn cancelling_a_real_version_probe_terminates_its_child() {
        use std::os::unix::fs::PermissionsExt;
        use std::sync::atomic::{AtomicU64, Ordering};
        static NEXT: AtomicU64 = AtomicU64::new(0);
        let dir = std::env::temp_dir().join(format!(
            "voicebox-probe-cancel-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::create_dir(&dir).unwrap();
        struct Cleanup(std::path::PathBuf);
        impl Drop for Cleanup {
            fn drop(&mut self) {
                let _ = std::fs::remove_dir_all(&self.0);
            }
        }
        let _cleanup = Cleanup(dir.clone());
        let exe = dir.join("probe");
        std::fs::write(&exe, "#!/bin/sh\necho $$ > child.pid\nexec /bin/sleep 30\n").unwrap();
        std::fs::set_permissions(&exe, std::fs::Permissions::from_mode(0o700)).unwrap();
        let gate = StartGate::default();
        let permit = gate.try_begin().unwrap();
        let waiting = permit.until_cancelled(crate::probe_binary_version(&exe, &dir));
        tokio::pin!(waiting);
        let pid = tokio::time::timeout(Duration::from_secs(8), async {
            loop {
                if let Ok(contents) = std::fs::read_to_string(dir.join("child.pid")) {
                    if let Ok(pid) = contents.trim().parse::<u32>() {
                        break pid;
                    }
                }
                tokio::select! {
                    _ = &mut waiting => panic!("probe ended before cancellation"),
                    _ = tokio::time::sleep(Duration::from_millis(10)) => {}
                }
            }
        })
        .await
        .unwrap();
        gate.cancel_and(|| ());
        assert_eq!(
            tokio::time::timeout(Duration::from_secs(2), waiting)
                .await
                .unwrap(),
            None
        );
        tokio::time::timeout(Duration::from_secs(2), async {
            loop {
                let status = std::process::Command::new("/bin/kill")
                    .args(["-0", &pid.to_string()])
                    .status()
                    .unwrap();
                if !status.success() {
                    break;
                }
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
        .await
        .expect("cancelled probe process remained alive");
    }

    #[tokio::test]
    async fn normal_work_completes_if_start_has_not_been_cancelled() {
        let gate = StartGate::default();
        let permit = gate.try_begin().unwrap();
        assert_eq!(permit.until_cancelled(async { 42 }).await, Some(42));
    }

    #[tokio::test]
    async fn all_waiters_are_woken_after_a_start_ends() {
        let gate = Arc::new(StartGate::default());
        let permit = gate.try_begin().unwrap();
        let first_gate = gate.clone();
        let second_gate = gate.clone();
        let first = tokio::spawn(async move { first_gate.wait_until_idle().await });
        let second = tokio::spawn(async move { second_gate.wait_until_idle().await });
        tokio::task::yield_now().await;
        assert!(!first.is_finished() && !second.is_finished());
        drop(permit);
        tokio::time::timeout(Duration::from_secs(1), async {
            first.await.unwrap();
            second.await.unwrap();
        })
        .await
        .unwrap();
    }

    #[tokio::test]
    async fn idle_gate_returns_without_waiting() {
        let gate = StartGate::default();
        tokio::time::timeout(Duration::from_millis(100), gate.wait_until_idle())
            .await
            .unwrap();
    }
}

#[cfg(test)]
mod stop_ownership_tests {
    use super::StartGate;
    use std::sync::{mpsc, Arc, Mutex};
    use std::time::Duration;

    #[test]
    fn stop_holds_new_starts_until_cleanup_finishes() {
        let gate = Arc::new(StartGate::default());
        let child = Mutex::new(Some(42));
        let (taken, stop) = gate.begin_stop(|| child.lock().unwrap().take());
        assert_eq!(taken, Some(42));
        assert!(gate.try_begin().is_none());
        drop(stop);
        assert!(gate.try_begin().is_some());
    }

    #[test]
    fn concurrent_cleanups_hold_the_gate_until_both_finish() {
        let gate = Arc::new(StartGate::default());
        let (_, first) = gate.begin_stop(|| ());
        let (_, second) = gate.begin_stop(|| ());
        drop(first);
        assert!(gate.try_begin().is_none());
        drop(second);
        assert!(gate.try_begin().is_some());
    }

    #[tokio::test]
    async fn restart_waits_for_cleanup_to_finish() {
        let gate = Arc::new(StartGate::default());
        let (_, stop) = gate.begin_stop(|| ());
        assert!(
            tokio::time::timeout(Duration::from_millis(20), gate.wait_until_idle())
                .await
                .is_err()
        );
        drop(stop);
        tokio::time::timeout(Duration::from_secs(1), gate.wait_until_idle())
            .await
            .unwrap();
    }

    #[test]
    fn stale_owned_cleanup_does_not_cancel_a_new_start() {
        let gate = Arc::new(StartGate::default());
        let start = gate.try_begin().unwrap();
        let (matched, cleanup) = gate.begin_cleanup(|| None::<u32>);
        assert_eq!(matched, None);
        drop(cleanup);
        assert_eq!(start.commit(|| 42), Some(42));
    }

    #[test]
    fn stopping_cancels_inflight_start_and_waits_for_both_owners() {
        let gate = Arc::new(StartGate::default());
        let start = gate.try_begin().unwrap();
        let (_, stop) = gate.begin_stop(|| ());
        assert_eq!(start.commit(|| 42), None);
        drop(start);
        assert!(gate.try_begin().is_none());
        drop(stop);
        assert!(gate.try_begin().is_some());
    }

    #[tokio::test]
    async fn cancelling_stop_caller_does_not_release_running_cleanup() {
        let gate = Arc::new(StartGate::default());
        let (_, stop) = gate.begin_stop(|| ());
        let (started_tx, started_rx) = tokio::sync::oneshot::channel();
        let (release_tx, release_rx) = mpsc::channel();
        let caller = tokio::spawn(async move {
            tokio::task::spawn_blocking(move || {
                let _stop = stop;
                let _ = started_tx.send(());
                let _ = release_rx.recv();
            })
            .await
            .unwrap();
        });
        started_rx.await.unwrap();
        caller.abort();
        assert!(caller.await.unwrap_err().is_cancelled());
        assert!(gate.try_begin().is_none());
        release_tx.send(()).unwrap();
        tokio::time::timeout(Duration::from_secs(1), gate.wait_until_idle())
            .await
            .unwrap();
        assert!(gate.try_begin().is_some());
    }
}
