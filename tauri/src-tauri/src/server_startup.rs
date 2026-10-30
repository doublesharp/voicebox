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
        if line.contains("Uvicorn running") || line.contains("Application startup complete") {
            return Outcome::Ready;
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
                CommandEvent::Stdout(b"Application startup complete.\n".to_vec()),
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
    async fn ready_leaves_later_events_for_the_log_forwarder() {
        let (tx, mut rx) = channel(3);
        tx.send(CommandEvent::Stderr(
            b"Application startup complete".to_vec(),
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
}
