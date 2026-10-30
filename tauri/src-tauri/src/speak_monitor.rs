//! Rust-side subscriber for the backend `/events/speak` SSE stream.
//!
//! Owns the pill-window lifecycle for agent-initiated speech. The dictate
//! webview used to do this itself via `EventSource`, but hidden WebKit
//! windows on macOS throttle long-lived network connections, so speak events
//! never reached the pill. Tauri's event bus, on the other hand, reliably
//! delivers events to hidden webviews (the chord path proves it), so we
//! subscribe here and fan out via `emit`.
//!
//! Flow:
//!   backend speak-start → show dictate window + emit("dictate:speak-start")
//!   backend speak-end   → emit("dictate:speak-end")
//! The pill webview handles the rest (audio playback, then emits
//! `dictate:hide` back to Rust when the audio element's `ended` fires).
//!
//! Reconnect policy: idle-timeout + escalating backoff. The stream is
//! infinite by design, so a successful round means "we were receiving
//! frames and then the backend closed the connection" (typically a
//! server restart) — reset backoff and reconnect quickly. A failure or
//! a round that produced no frames escalates backoff up to a 30 s cap so
//! long-term outages stop filling stderr with reconnect log lines.
//!
//! The idle timeout guards against the worst silent-failure mode: a
//! backend that accepts the TCP connection but stops producing frames
//! (deadlocked SSE endpoint, zombie process). Without a timeout the
//! a read loop can wait forever and the task never notices. The
//! backend emits a `:ping` comment every 15 s, so 45 s without a complete frame
//! is a reliable signal the stream is dead.

use std::time::Duration;

use tauri::{AppHandle, Emitter};

use crate::{ensure_dictate_window, sse_frames::FrameBuffer, SERVER_PORT};

const INITIAL_BACKOFF: Duration = Duration::from_millis(500);
const MAX_BACKOFF: Duration = Duration::from_secs(30);
/// Backend emits a `:ping` heartbeat every 15 s. Giving the stream 45 s
/// of idle budget absorbs one missed heartbeat (slow GC pause, brief
/// backend stall) without being so long that a truly dead stream blocks
/// the pill from surfacing for minutes.
const STREAM_IDLE_TIMEOUT: Duration = Duration::from_secs(45);

pub fn spawn_speak_monitor(app: AppHandle) {
    tauri::async_runtime::spawn(async move {
        run(app).await;
    });
}

async fn run(app: AppHandle) {
    let url = format!("http://127.0.0.1:{}/events/speak", SERVER_PORT);
    let client = match monitor_client() {
        Ok(c) => c,
        Err(e) => {
            eprintln!("speak_monitor: failed to build HTTP client: {e}");
            return;
        }
    };

    let mut retry = Retry::default();

    loop {
        let stream_result = stream_once(&client, &url, &app).await;
        let had_success = matches!(stream_result, Ok(true));
        let (delay, attempt) = retry.after_round(had_success);

        if !had_success {
            let reason = match stream_result {
                Ok(_) => "stream closed without data".to_string(),
                Err(e) => format!("stream err: {e}"),
            };
            eprintln!(
                "speak_monitor: {reason} (attempt {attempt}, retry in {:?})",
                delay
            );
        }

        tokio::time::sleep(delay).await;
    }
}

fn monitor_client() -> Result<reqwest::Client, reqwest::Error> {
    reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .build()
}

struct Retry {
    backoff: Duration,
    attempts: u32,
}

impl Default for Retry {
    fn default() -> Self {
        Self {
            backoff: INITIAL_BACKOFF,
            attempts: 0,
        }
    }
}

impl Retry {
    fn after_round(&mut self, productive: bool) -> (Duration, u32) {
        if productive {
            *self = Self::default();
            return (self.backoff, self.attempts);
        }
        self.attempts = self.attempts.saturating_add(1);
        let delay = self.backoff;
        self.backoff = (self.backoff * 2).min(MAX_BACKOFF);
        (delay, self.attempts)
    }
}

/// Consume the SSE stream until it closes or errors. Returns `Ok(true)`
/// if at least one frame was received (the connection was genuinely
/// productive), `Ok(false)` on a clean but empty close, and `Err` for
/// any connection or parse failure.
async fn stream_once(
    client: &reqwest::Client,
    url: &str,
    app: &AppHandle,
) -> Result<bool, Box<dyn std::error::Error + Send + Sync>> {
    stream_once_with(client, url, STREAM_IDLE_TIMEOUT, |event, data| {
        dispatch(app, event, data);
    })
    .await
}

async fn stream_once_with(
    client: &reqwest::Client,
    url: &str,
    idle_timeout: Duration,
    mut on_event: impl FnMut(&str, &str),
) -> Result<bool, Box<dyn std::error::Error + Send + Sync>> {
    let mut resp = tokio::time::timeout(
        idle_timeout,
        client.get(url).header("Accept", "text/event-stream").send(),
    )
    .await
    .map_err(|_| format!("no response headers for {:?}", idle_timeout))??;
    if resp.status() != reqwest::StatusCode::OK {
        return Err(format!("speak_monitor: backend returned {}", resp.status()).into());
    }
    let is_event_stream = resp
        .headers()
        .get(reqwest::header::CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.split(';').next())
        .is_some_and(|value| value.trim().eq_ignore_ascii_case("text/event-stream"));
    if !is_event_stream {
        return Err("speak_monitor: backend did not return an event stream".into());
    }
    let mut buf = FrameBuffer::default();
    let mut deadline = tokio::time::Instant::now() + idle_timeout;
    loop {
        let chunk = match tokio::time::timeout_at(deadline, resp.chunk()).await {
            Ok(Ok(Some(chunk))) => chunk,
            Ok(Ok(None)) => return Ok(buf.has_complete_frame()),
            Ok(Err(e)) => return Err(Box::new(e)),
            Err(_) => return Err(format!(
                "no data forming a complete frame for {:?} (heartbeat should arrive every 15 s)",
                idle_timeout
            )
            .into()),
        };
        let previous_frames = buf.completed_frames();
        let events = buf.push(&chunk)?;
        if buf.completed_frames() != previous_frames {
            deadline = tokio::time::Instant::now() + idle_timeout;
        }
        for (event, data) in events {
            on_event(&event, &data);
        }
    }
}

fn dispatch(app: &AppHandle, event: &str, data: &str) {
    match event {
        "speak-start" => {
            // Defensive for dev/restart paths where the setup-created pill
            // is not present — but don't *show* it here. The pill
            // surfaces itself from `audio.onplaying` via `dictate:show`, so
            // users never see the empty-silent generation window.
            ensure_dictate_window(app);
            let _ = app.emit("dictate:speak-start", data.to_string());
        }
        "speak-end" => {
            let _ = app.emit("dictate:speak-end", data.to_string());
        }
        // `ready` and `ping` are heartbeats; ignore.
        _ => {}
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;

    #[test]
    fn retry_backoff_caps_and_resets_after_productive_stream() {
        let mut retry = Retry::default();
        for (attempt, milliseconds) in [500, 1000, 2000, 4000, 8000, 16000, 30000, 30000]
            .into_iter()
            .enumerate()
        {
            assert_eq!(
                retry.after_round(false),
                (Duration::from_millis(milliseconds), attempt as u32 + 1)
            );
        }
        assert_eq!(retry.after_round(true), (INITIAL_BACKOFF, 0));
        assert_eq!(retry.after_round(false), (INITIAL_BACKOFF, 1));
        retry.attempts = u32::MAX;
        assert_eq!(retry.after_round(false).1, u32::MAX);
    }

    #[tokio::test]
    async fn redirect_cannot_supply_events_from_a_different_server() {
        let (target, target_task) = serve(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\nevent: speak-start\ndata: wrong server\n\n",
            Duration::ZERO,
        ).await;
        let source = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}/events/speak", source.local_addr().unwrap());
        let source_task = tokio::spawn(async move {
            let (mut stream, _) = source.accept().await.unwrap();
            let mut request = [0; 1024];
            let _ = stream.read(&mut request).await;
            let response = format!("HTTP/1.1 302 Found\r\nLocation: {target}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n");
            stream.write_all(response.as_bytes()).await.unwrap();
        });
        let mut events = Vec::new();
        let result = stream_once_with(
            &monitor_client().unwrap(),
            &url,
            Duration::from_secs(1),
            |event, data| events.push((event.to_string(), data.to_string())),
        )
        .await;
        source_task.await.unwrap();
        // The target may never be contacted when redirects are disabled.
        target_task.abort();
        assert!(
            result.is_err(),
            "a redirect must not be accepted as the backend stream"
        );
        assert!(events.is_empty());
    }

    async fn serve(
        response: &'static [u8],
        delay: Duration,
    ) -> (String, tokio::task::JoinHandle<()>) {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}/events/speak", listener.local_addr().unwrap());
        let task = tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await.unwrap();
            let mut request = [0; 1024];
            let _ = stream.read(&mut request).await;
            if !response.is_empty() {
                let _ = stream.write_all(response).await;
            }
            tokio::time::sleep(delay).await;
        });
        (url, task)
    }

    #[tokio::test]
    async fn silent_server_must_not_block_before_response_headers() {
        let (url, task) = serve(b"", Duration::from_millis(250)).await;
        let result = tokio::time::timeout(
            Duration::from_millis(150),
            stream_once_with(
                &reqwest::Client::new(),
                &url,
                Duration::from_millis(30),
                |_, _| {},
            ),
        )
        .await;
        assert!(result.is_ok(), "initial connection exceeded its timeout");
        assert!(result.unwrap().is_err());
        task.await.unwrap();
    }

    #[tokio::test]
    async fn event_stream_dispatches_ordered_frames() {
        let response = b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\nevent: speak-start\ndata: one\n\nevent: speak-end\ndata: two\n\n";
        let (url, task) = serve(response, Duration::ZERO).await;
        let mut events = Vec::new();
        let result = stream_once_with(
            &reqwest::Client::new(),
            &url,
            Duration::from_secs(1),
            |e, d| events.push((e.to_string(), d.to_string())),
        )
        .await
        .unwrap();
        assert!(result);
        assert_eq!(
            events,
            [
                ("speak-start".into(), "one".into()),
                ("speak-end".into(), "two".into())
            ]
        );
        task.await.unwrap();
    }

    #[tokio::test]
    async fn idle_body_fails_instead_of_waiting_forever() {
        let (url, task) = serve(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n",
            Duration::from_millis(150),
        )
        .await;
        let result = stream_once_with(
            &reqwest::Client::new(),
            &url,
            Duration::from_millis(30),
            |_, _| {},
        )
        .await;
        assert!(result.unwrap_err().to_string().contains("no data"));
        task.await.unwrap();
    }

    #[tokio::test]
    async fn empty_response_is_not_reported_as_productive() {
        let (url, task) = serve(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: 0\r\n\r\n",
            Duration::ZERO,
        )
        .await;
        assert!(!stream_once_with(
            &reqwest::Client::new(),
            &url,
            Duration::from_secs(1),
            |_, _| {}
        )
        .await
        .unwrap());
        task.await.unwrap();
    }

    #[tokio::test]
    async fn rejects_error_status_and_invalid_utf8() {
        for response in [b"HTTP/1.1 503 Unavailable\r\nContent-Length: 0\r\n\r\n".as_slice(), b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\nevent: x\ndata: \xff\n\n".as_slice()] {
            let (url, task) = serve(response, Duration::ZERO).await;
            assert!(stream_once_with(&reqwest::Client::new(), &url, Duration::from_secs(1), |_, _| {}).await.is_err());
            task.await.unwrap();
        }
    }

    #[tokio::test]
    async fn rejects_success_responses_that_are_not_event_streams() {
        for response in [
            b"HTTP/1.1 204 No Content\r\n\r\n".as_slice(),
            b"HTTP/1.1 206 Partial Content\r\nContent-Type: text/event-stream\r\nContent-Length: 0\r\n\r\n".as_slice(),
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 5\r\n\r\nhello"
                .as_slice(),
        ] {
            let (url, task) = serve(response, Duration::ZERO).await;
            assert!(stream_once_with(
                &reqwest::Client::new(),
                &url,
                Duration::from_secs(1),
                |_, _| {}
            )
            .await
            .is_err());
            task.await.unwrap();
        }
    }

    #[tokio::test]
    async fn accepts_content_type_parameters_and_heartbeat_frames() {
        let (url, task) = serve(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream; charset=utf-8\r\nConnection: close\r\n\r\n: ping\r\n\r\nevent: speak-end\r\ndata: done\r\n\r\n",
            Duration::ZERO,
        ).await;
        let mut events = Vec::new();
        let productive = stream_once_with(
            &reqwest::Client::new(),
            &url,
            Duration::from_secs(1),
            |event, data| events.push((event.to_string(), data.to_string())),
        )
        .await
        .unwrap();
        assert!(productive);
        assert_eq!(events, [("speak-end".into(), "done".into())]);
        task.await.unwrap();
    }

    #[tokio::test]
    async fn oversized_incomplete_event_aborts_the_stream() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}/events/speak", listener.local_addr().unwrap());
        let task = tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await.unwrap();
            let mut request = [0; 1024];
            let _ = stream.read(&mut request).await;
            let _ = stream.write_all(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n").await;
            let _ = stream.write_all(&vec![b'x'; 1024 * 1024 + 1]).await;
        });
        let mut events = Vec::new();
        let error = stream_once_with(
            &reqwest::Client::new(),
            &url,
            Duration::from_secs(2),
            |event, data| events.push((event.to_string(), data.to_string())),
        )
        .await
        .unwrap_err();
        assert!(error.to_string().contains("maximum frame size"));
        assert!(events.is_empty());
        task.await.unwrap();
    }

    #[tokio::test]
    async fn truncated_or_empty_frames_do_not_reset_reconnect_backoff() {
        for body in ["event: speak-start\ndata: incomplete", "\n\n\r\n"] {
            let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
            let url = format!("http://{}/events/speak", listener.local_addr().unwrap());
            let payload = format!("HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n{body}");
            let task = tokio::spawn(async move {
                let (mut stream, _) = listener.accept().await.unwrap();
                let mut request = [0; 1024];
                let _ = stream.read(&mut request).await;
                stream.write_all(payload.as_bytes()).await.unwrap();
            });
            let productive = stream_once_with(
                &reqwest::Client::new(),
                &url,
                Duration::from_secs(1),
                |_, _| panic!("incomplete event dispatched"),
            )
            .await
            .unwrap();
            assert!(
                !productive,
                "partial or empty frames must not reset retry backoff"
            );
            task.await.unwrap();
        }
    }

    #[tokio::test]
    async fn a_complete_heartbeat_is_a_productive_connection() {
        let (url, task) = serve(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n: ping\n\n", Duration::ZERO).await;
        let productive = stream_once_with(
            &reqwest::Client::new(),
            &url,
            Duration::from_secs(1),
            |_, _| panic!("heartbeat dispatched"),
        )
        .await
        .unwrap();
        assert!(productive);
        task.await.unwrap();
    }

    #[tokio::test]
    async fn fragmented_utf8_events_arrive_once_and_in_order() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}/events/speak", listener.local_addr().unwrap());
        let task = tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await.unwrap();
            let mut request = [0; 1024];
            let _ = stream.read(&mut request).await;
            stream.write_all(b"HTTP/1.1 200 OK\r\nContent-Type: Text/Event-Stream; charset=utf-8\r\nConnection: close\r\n\r\nevent: speak-start\ndata: \xe2").await.unwrap();
            tokio::time::sleep(Duration::from_millis(5)).await;
            stream
                .write_all(b"\x82\xac\n\nevent: speak-end\ndata: done\n\n")
                .await
                .unwrap();
        });
        let mut events = Vec::new();
        assert!(stream_once_with(
            &reqwest::Client::new(),
            &url,
            Duration::from_secs(1),
            |event, data| events.push((event.to_string(), data.to_string()))
        )
        .await
        .unwrap());
        assert_eq!(
            events,
            [
                ("speak-start".into(), "€".into()),
                ("speak-end".into(), "done".into())
            ]
        );
        task.await.unwrap();
    }

    #[tokio::test]
    async fn event_followed_by_partial_frame_is_productive_without_dispatching_partial_data() {
        let (url, task) = serve(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\nevent: speak-end\ndata: done\n\nevent: speak-start\ndata: incomplete", Duration::ZERO).await;
        let mut events = Vec::new();
        assert!(stream_once_with(
            &reqwest::Client::new(),
            &url,
            Duration::from_secs(1),
            |event, data| events.push((event.to_string(), data.to_string()))
        )
        .await
        .unwrap());
        assert_eq!(events, [("speak-end".into(), "done".into())]);
        task.await.unwrap();
    }
}

#[cfg(test)]
mod progress_timeout_tests {
    use super::*;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;

    async fn trickle(
        parts: Vec<&'static [u8]>,
        delay: Duration,
    ) -> (String, tokio::task::JoinHandle<()>) {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}/events/speak", listener.local_addr().unwrap());
        let task = tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await.unwrap();
            let mut request = [0; 1024];
            let _ = stream.read(&mut request).await;
            if stream.write_all(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n").await.is_err() { return; }
            for part in parts {
                if stream.write_all(part).await.is_err() {
                    return;
                }
                tokio::time::sleep(delay).await;
            }
        });
        (url, task)
    }

    #[tokio::test]
    async fn partial_bytes_cannot_keep_a_dead_stream_alive() {
        for part in [b"x".as_slice(), b"\n".as_slice()] {
            let (url, task) = trickle(vec![part; 30], Duration::from_millis(30)).await;
            let result = tokio::time::timeout(
                Duration::from_millis(550),
                stream_once_with(
                    &reqwest::Client::new(),
                    &url,
                    Duration::from_millis(200),
                    |_, _| panic!("incomplete frame dispatched"),
                ),
            )
            .await;
            task.abort();
            let result =
                result.expect("partial bytes kept the stream alive beyond the frame deadline");
            assert!(result.is_err());
        }
    }

    #[tokio::test]
    async fn complete_heartbeats_extend_the_deadline_and_later_event_is_delivered() {
        let mut parts = vec![b": ping\n\n".as_slice(); 8];
        parts.push(b"event: speak-end\ndata: done\n\n");
        let (url, task) = trickle(parts, Duration::from_millis(40)).await;
        let mut events = Vec::new();
        let result = stream_once_with(
            &reqwest::Client::new(),
            &url,
            Duration::from_millis(200),
            |event, data| events.push((event.to_owned(), data.to_owned())),
        )
        .await;
        task.await.unwrap();
        assert!(result.unwrap());
        assert_eq!(events, [("speak-end".into(), "done".into())]);
    }

    #[tokio::test]
    async fn partial_event_after_a_heartbeat_still_times_out() {
        let mut parts = vec![b": ping\n\n".as_slice()];
        parts.extend(vec![b"x".as_slice(); 30]);
        let (url, task) = trickle(parts, Duration::from_millis(30)).await;
        let result = tokio::time::timeout(
            Duration::from_millis(550),
            stream_once_with(
                &reqwest::Client::new(),
                &url,
                Duration::from_millis(200),
                |_, _| panic!("partial frame dispatched"),
            ),
        )
        .await;
        task.abort();
        assert!(result.expect("trickle prevented timeout").is_err());
    }

    #[tokio::test]
    async fn fragmented_event_completes_before_deadline() {
        let (url, task) = trickle(
            vec![b"event: speak-", b"start\ndata: ", b"hello\n\n"],
            Duration::from_millis(25),
        )
        .await;
        let mut events = Vec::new();
        assert!(stream_once_with(
            &reqwest::Client::new(),
            &url,
            Duration::from_millis(200),
            |event, data| events.push((event.to_owned(), data.to_owned()))
        )
        .await
        .unwrap());
        task.await.unwrap();
        assert_eq!(events, [("speak-start".into(), "hello".into())]);
    }
}
