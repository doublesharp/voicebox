//! Observe sidecar startup events without relying on process-global state.
use std::time::Duration;
use tauri::async_runtime::Receiver;
use tauri_plugin_shell::process::CommandEvent;

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
