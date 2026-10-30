use crate::publication::publish;
use serde::{Deserialize, Serialize};
use std::io::{self, Read};
use std::path::Path;
use std::path::PathBuf;
use tauri::AppHandle;
use tauri_plugin_dialog::DialogExt;

#[derive(Deserialize)]
pub struct FileFilter {
    name: String,
    extensions: Vec<String>,
}
#[derive(Serialize)]
pub struct SaveResult {
    status: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    path: Option<String>,
}

fn choose(
    app: &AppHandle,
    filename: &str,
    filters: &[FileFilter],
) -> Result<Option<PathBuf>, String> {
    let stamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_err(|e| e.to_string())?
        .as_millis();
    let suggested = suggested_filename(filename, stamp)?;
    let mut dialog = app.dialog().file().set_file_name(suggested);
    for filter in filters {
        let extensions: Vec<&str> = filter.extensions.iter().map(String::as_str).collect();
        dialog = dialog.add_filter(&filter.name, &extensions);
    }
    dialog
        .blocking_save_file()
        .map(|p| p.into_path().map_err(|e| e.to_string()))
        .transpose()
}

fn suggested_filename(filename: &str, stamp: u128) -> Result<String, String> {
    let name = Path::new(filename)
        .file_name()
        .and_then(|value| value.to_str())
        .filter(|value| !value.is_empty())
        .ok_or("Invalid filename")?;
    let name_path = Path::new(name);
    let stem = name_path
        .file_stem()
        .and_then(|value| value.to_str())
        .unwrap_or("export");
    let ext = name_path
        .extension()
        .and_then(|value| value.to_str())
        .map(|value| format!(".{value}"))
        .unwrap_or_default();
    Ok(format!("{stem}-{stamp}{ext}"))
}

fn run_with(
    select: impl FnOnce() -> Result<Option<PathBuf>, String>,
    input: impl FnOnce() -> Result<Box<dyn Read>, String>,
) -> Result<SaveResult, String> {
    let Some(path) = select()? else {
        return Ok(SaveResult {
            status: "cancelled",
            path: None,
        });
    };
    let reader = input()?;
    publish(&path, reader).map_err(|e| format!("Could not save {}: {e}", path.display()))?;
    Ok(SaveResult {
        status: "saved",
        path: Some(path.to_string_lossy().into_owned()),
    })
}

async fn run_on_worker(
    select: impl FnOnce() -> Result<Option<PathBuf>, String> + Send + 'static,
    input: impl FnOnce() -> Result<Box<dyn Read>, String> + Send + 'static,
) -> Result<SaveResult, String> {
    tauri::async_runtime::spawn_blocking(move || run_with(select, input))
        .await
        .map_err(|e| e.to_string())?
}

#[tauri::command]
pub async fn publish_file_bytes(
    app: AppHandle,
    filename: String,
    filters: Vec<FileFilter>,
    bytes: Vec<u8>,
) -> Result<SaveResult, String> {
    run_on_worker(
        move || choose(&app, &filename, &filters),
        move || Ok(Box::new(io::Cursor::new(bytes))),
    )
    .await
}

#[tauri::command]
pub async fn publish_file_url(
    app: AppHandle,
    filename: String,
    filters: Vec<FileFilter>,
    url: String,
) -> Result<SaveResult, String> {
    run_on_worker(
        move || choose(&app, &filename, &filters),
        move || download(&url, std::time::Duration::from_secs(30)),
    )
    .await
}

fn download(url: &str, timeout: std::time::Duration) -> Result<Box<dyn Read>, String> {
    let parsed = reqwest::Url::parse(url).map_err(|e| format!("Invalid download URL: {e}"))?;
    if !matches!(parsed.scheme(), "http" | "https") {
        return Err("Only HTTP(S) download URLs are supported".into());
    }
    let client = reqwest::blocking::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(timeout)
        .build()
        .map_err(|e| e.to_string())?;
    let response = client
        .get(parsed)
        .send()
        .map_err(|e| format!("Download failed: {e}"))?;
    // This GET does not request a byte range: only a complete response is a file.
    if response.status() != reqwest::StatusCode::OK {
        return Err(format!("Download failed: HTTP {}", response.status()));
    }
    if response
        .headers()
        .contains_key(reqwest::header::CONTENT_RANGE)
    {
        return Err("Download failed: unexpected partial-content response".into());
    }
    Ok(Box::new(response))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::thread;
    use std::time::Duration;

    fn serve(
        response: &'static [u8],
        pause: Duration,
        tail: &'static [u8],
    ) -> (String, thread::JoinHandle<()>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}/file", listener.local_addr().unwrap());
        let handle = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            stream
                .set_read_timeout(Some(Duration::from_secs(2)))
                .unwrap();
            let mut request = [0; 1024];
            let _ = stream.read(&mut request);
            let _ = stream.write_all(response);
            if !pause.is_zero() {
                thread::sleep(pause);
            }
            let _ = stream.write_all(tail);
        });
        (url, handle)
    }

    #[test]
    fn export_http_regression_rejects_partial_content_headers_even_with_success_status() {
        for response in [
            b"HTTP/1.1 200 OK\r\nContent-Range: bytes 0-3/8\r\nContent-Length: 4\r\nConnection: close\r\n\r\npart".as_slice(),
            b"HTTP/1.1 200 OK\r\ncontent-range: bytes 0-3/4\r\nContent-Length: 4\r\nConnection: close\r\n\r\npart".as_slice(),
        ] {
            let (url, server) = serve(response, Duration::ZERO, b"");
            assert!(download(&url, Duration::from_secs(2)).is_err());
            server.join().unwrap();
        }
    }

    #[test]
    fn export_http_regression_accepts_complete_chunked_download() {
        let (url, server) = serve(
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n3\r\nabc\r\n2\r\nde\r\n0\r\n\r\n",
            Duration::ZERO, b"",
        );
        let mut result = Vec::new();
        download(&url, Duration::from_secs(2))
            .unwrap()
            .read_to_end(&mut result)
            .unwrap();
        server.join().unwrap();
        assert_eq!(result, b"abcde");
    }

    #[test]
    fn export_http_regression_rejects_incomplete_chunked_body() {
        let (url, server) = serve(
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n6\r\nabc",
            Duration::ZERO,
            b"",
        );
        let mut result = Vec::new();
        assert!(download(&url, Duration::from_secs(2))
            .unwrap()
            .read_to_end(&mut result)
            .is_err());
        server.join().unwrap();
    }

    #[test]
    fn export_http_regression_progressing_transfer_can_exceed_the_idle_timeout() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}/file", listener.local_addr().unwrap());
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            stream
                .set_read_timeout(Some(Duration::from_secs(2)))
                .unwrap();
            let mut request = [0; 1024];
            let _ = stream.read(&mut request);
            stream
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 6\r\nConnection: close\r\n\r\n")
                .unwrap();
            for byte in b"abcdef" {
                stream.write_all(&[*byte]).unwrap();
                thread::sleep(Duration::from_millis(60));
            }
        });
        let mut result = Vec::new();
        download(&url, Duration::from_millis(250))
            .unwrap()
            .read_to_end(&mut result)
            .unwrap();
        server.join().unwrap();
        assert_eq!(result, b"abcdef");
    }

    #[test]
    fn successful_download_streams_exact_bytes() {
        let (url, server) = serve(
            b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\nhello",
            Duration::ZERO,
            b"",
        );
        let mut result = Vec::new();
        download(&url, Duration::from_secs(2))
            .unwrap()
            .read_to_end(&mut result)
            .unwrap();
        server.join().unwrap();
        assert_eq!(result, b"hello");
    }

    #[test]
    fn rejects_bad_urls_and_non_http_schemes() {
        for url in ["not a url", "file:///etc/passwd", "ftp://example.com/file"] {
            assert!(download(url, Duration::from_secs(1)).is_err());
        }
    }

    #[test]
    fn rejects_redirects_and_unsuccessful_statuses() {
        for response in [b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1/elsewhere\r\nContent-Length: 0\r\n\r\n".as_slice(), b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n".as_slice()] {
            let (url, server) = serve(response, Duration::ZERO, b"");
            assert!(download(&url, Duration::from_secs(1)).is_err());
            server.join().unwrap();
        }
    }

    fn assert_failed_stream_preserves_file(
        response: &'static [u8],
        pause: Duration,
        tail: &'static [u8],
    ) {
        let (url, server) = serve(response, pause, tail);
        let directory = std::env::temp_dir().join(format!(
            "voicebox-export-test-{}-{:?}",
            std::process::id(),
            thread::current().id()
        ));
        std::fs::create_dir(&directory).unwrap();
        let target = directory.join("existing.wav");
        std::fs::write(&target, b"original").unwrap();
        let reader = download(&url, Duration::from_millis(80)).unwrap();
        let result = publish(&target, reader);
        server.join().unwrap();
        assert!(result.is_err());
        assert_eq!(std::fs::read(&target).unwrap(), b"original");
        assert_eq!(std::fs::read_dir(&directory).unwrap().count(), 1);
        std::fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn partial_and_no_content_responses_are_not_publishable() {
        for response in [
            b"HTTP/1.1 206 Partial Content\r\nContent-Length: 4\r\nContent-Range: bytes 0-3/8\r\n\r\npart".as_slice(),
            b"HTTP/1.1 204 No Content\r\n\r\n".as_slice(),
        ] {
            let (url, server) = serve(response, Duration::ZERO, b"");
            assert!(download(&url, Duration::from_secs(1)).is_err());
            server.join().unwrap();
        }
    }

    #[test]
    fn truncated_download_does_not_replace_existing_file() {
        assert_failed_stream_preserves_file(
            b"HTTP/1.1 200 OK\r\nContent-Length: 20\r\nConnection: close\r\n\r\npartial",
            Duration::ZERO,
            b"",
        );
    }

    #[test]
    fn stalled_download_times_out_without_replacing_existing_file() {
        assert_failed_stream_preserves_file(
            b"HTTP/1.1 200 OK\r\nContent-Length: 8\r\nConnection: close\r\n\r\npart",
            Duration::from_millis(400),
            b"rest",
        );
    }
}

#[cfg(test)]
mod export_flow_tests {
    use super::*;
    use std::cell::Cell;
    use std::sync::atomic::{AtomicU64, Ordering};

    static NEXT: AtomicU64 = AtomicU64::new(0);
    struct Fixture(PathBuf);
    impl Fixture {
        fn new() -> Self {
            let path = std::env::temp_dir().join(format!(
                "voicebox-export-flow-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            std::fs::create_dir(&path).unwrap();
            Self(path)
        }
        fn target(&self) -> PathBuf {
            self.0.join("export.wav")
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }
    fn input(bytes: &'static [u8]) -> Box<dyn Read> {
        Box::new(io::Cursor::new(bytes))
    }

    #[tokio::test]
    async fn worker_cancel_and_dialog_error_never_open_the_input() {
        use std::sync::{atomic::AtomicBool, Arc};
        for cancelled in [true, false] {
            let called = Arc::new(AtomicBool::new(false));
            let input_called = called.clone();
            let result = run_on_worker(
                move || {
                    if cancelled {
                        Ok(None)
                    } else {
                        Err("dialog failed".into())
                    }
                },
                move || {
                    input_called.store(true, Ordering::SeqCst);
                    Ok(input(b"unused"))
                },
            )
            .await;
            if cancelled {
                assert_eq!(result.unwrap().status, "cancelled");
            } else {
                assert_eq!(result.err().unwrap(), "dialog failed");
            }
            assert!(!called.load(Ordering::SeqCst));
        }
    }

    #[tokio::test]
    async fn worker_publishes_complete_bytes_and_reports_the_destination() {
        let fixture = Fixture::new();
        let target = fixture.target();
        std::fs::write(&target, b"old").unwrap();
        let selected = target.clone();
        let result = run_on_worker(
            move || Ok(Some(selected)),
            move || Ok(input(b"complete audio")),
        )
        .await
        .unwrap();
        assert_eq!(result.status, "saved");
        assert_eq!(result.path.as_deref(), target.to_str());
        assert_eq!(std::fs::read(&target).unwrap(), b"complete audio");
        assert_eq!(std::fs::read_dir(&fixture.0).unwrap().count(), 1);
    }

    #[tokio::test]
    async fn worker_rejects_truncated_http_body_without_replacing_file() {
        use std::io::Write;
        use std::net::TcpListener;
        let fixture = Fixture::new();
        let target = fixture.target();
        std::fs::write(&target, b"original").unwrap();
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}/audio", listener.local_addr().unwrap());
        let server = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = [0; 1024];
            let _ = stream.read(&mut request);
            stream
                .write_all(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 20\r\nConnection: close\r\n\r\npartial",
                )
                .unwrap();
        });
        let selected = target.clone();
        let error = run_on_worker(
            move || Ok(Some(selected)),
            move || download(&url, std::time::Duration::from_secs(2)),
        )
        .await
        .err()
        .unwrap();
        server.join().unwrap();
        assert!(error.contains("Could not save"));
        assert_eq!(std::fs::read(&target).unwrap(), b"original");
        assert_eq!(std::fs::read_dir(&fixture.0).unwrap().count(), 1);
    }

    #[tokio::test]
    async fn worker_panics_become_errors_and_do_not_leave_staged_files() {
        struct PanicReader;
        impl Read for PanicReader {
            fn read(&mut self, _: &mut [u8]) -> io::Result<usize> {
                panic!("injected stream panic")
            }
        }
        let fixture = Fixture::new();
        let target = fixture.target();
        std::fs::write(&target, b"original").unwrap();
        let selected = target.clone();
        let result = run_on_worker(move || Ok(Some(selected)), || Ok(Box::new(PanicReader))).await;
        assert!(result.is_err());
        assert_eq!(std::fs::read(&target).unwrap(), b"original");
        assert_eq!(std::fs::read_dir(&fixture.0).unwrap().count(), 1);
    }

    #[tokio::test]
    async fn overlapping_workers_never_publish_mixed_content() {
        let fixture = Fixture::new();
        let target = fixture.target();
        let first_target = target.clone();
        let second_target = target.clone();
        let first = run_on_worker(
            move || Ok(Some(first_target)),
            || Ok(Box::new(io::Cursor::new(vec![1u8; 16384]))),
        );
        let second = run_on_worker(
            move || Ok(Some(second_target)),
            || Ok(Box::new(io::Cursor::new(vec![2u8; 16384]))),
        );
        let (a, b) = tokio::join!(first, second);
        assert!(a.is_ok() && b.is_ok());
        let bytes = std::fs::read(&target).unwrap();
        assert_eq!(bytes.len(), 16384);
        assert!(bytes.iter().all(|v| *v == bytes[0]));
        assert!([1, 2].contains(&bytes[0]));
        assert_eq!(std::fs::read_dir(&fixture.0).unwrap().count(), 1);
    }

    #[test]
    fn cancelled_dialog_does_not_read_or_create_a_file() {
        let called = Cell::new(false);
        let result = run_with(
            || Ok(None),
            || {
                called.set(true);
                Ok(input(b"unused"))
            },
        )
        .unwrap();
        assert_eq!(
            serde_json::to_value(result).unwrap(),
            serde_json::json!({"status":"cancelled"})
        );
        assert!(!called.get());
    }
    #[test]
    fn dialog_error_is_propagated_before_input_is_opened() {
        let called = Cell::new(false);
        let error = run_with(
            || Err("dialog failed".into()),
            || {
                called.set(true);
                Ok(input(b"unused"))
            },
        )
        .err()
        .unwrap();
        assert_eq!(error, "dialog failed");
        assert!(!called.get());
    }
    #[test]
    fn selected_destination_is_written_and_returned_to_the_frontend() {
        let fixture = Fixture::new();
        let target = fixture.target();
        let result = run_with(|| Ok(Some(target.clone())), || Ok(input(b"new audio"))).unwrap();
        assert_eq!(std::fs::read(&target).unwrap(), b"new audio");
        assert_eq!(
            serde_json::to_value(result).unwrap(),
            serde_json::json!({"status":"saved", "path":target.to_str().unwrap()})
        );
        assert_eq!(std::fs::read_dir(&fixture.0).unwrap().count(), 1);
    }
    #[test]
    fn input_failure_leaves_existing_file_untouched() {
        let fixture = Fixture::new();
        let target = fixture.target();
        std::fs::write(&target, b"original").unwrap();
        let error = run_with(
            || Ok(Some(target.clone())),
            || Err("download failed".into()),
        )
        .err()
        .unwrap();
        assert_eq!(error, "download failed");
        assert_eq!(std::fs::read(&target).unwrap(), b"original");
        assert_eq!(std::fs::read_dir(&fixture.0).unwrap().count(), 1);
    }
    struct Broken;
    impl Read for Broken {
        fn read(&mut self, _: &mut [u8]) -> io::Result<usize> {
            Err(io::Error::new(io::ErrorKind::Other, "stream failed"))
        }
    }
    #[test]
    fn interrupted_stream_preserves_existing_file_and_reports_destination() {
        let fixture = Fixture::new();
        let target = fixture.target();
        std::fs::write(&target, b"original").unwrap();
        let error = run_with(|| Ok(Some(target.clone())), || Ok(Box::new(Broken)))
            .err()
            .unwrap();
        assert!(
            error.contains("Could not save")
                && error.contains("stream failed")
                && error.contains(target.to_str().unwrap())
        );
        assert_eq!(std::fs::read(&target).unwrap(), b"original");
        assert_eq!(std::fs::read_dir(&fixture.0).unwrap().count(), 1);
    }
    #[test]
    fn missing_destination_parent_reports_failure_without_creating_files() {
        let fixture = Fixture::new();
        let target = fixture.0.join("missing").join("export.wav");
        let error = run_with(|| Ok(Some(target)), || Ok(input(b"audio")))
            .err()
            .unwrap();
        assert!(error.contains("Could not save"));
        assert_eq!(std::fs::read_dir(&fixture.0).unwrap().count(), 0);
    }
    #[test]
    fn downloaded_export_reaches_the_selected_file() {
        use std::io::Write;
        use std::net::TcpListener;
        let fixture = Fixture::new();
        let target = fixture.target();
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}/audio", listener.local_addr().unwrap());
        let server = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = [0; 1024];
            let _ = stream.read(&mut request);
            stream
                .write_all(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\nhello",
                )
                .unwrap();
        });
        let result = run_with(
            || Ok(Some(target.clone())),
            || download(&url, std::time::Duration::from_secs(2)),
        )
        .unwrap();
        server.join().unwrap();
        assert_eq!(result.status, "saved");
        assert_eq!(std::fs::read(&target).unwrap(), b"hello");

        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}/audio", listener.local_addr().unwrap());
        let server = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = [0; 1024];
            let _ = stream.read(&mut request);
            let _ = stream.write_all(b"HTTP/1.1 200 OK\r\nContent-Range: bytes 0-3/8\r\nContent-Length: 4\r\nConnection: close\r\n\r\npart");
        });
        let result = run_with(
            || Ok(Some(target.clone())),
            || download(&url, std::time::Duration::from_secs(2)),
        );
        server.join().unwrap();
        assert!(result.is_err());
        assert_eq!(std::fs::read(&target).unwrap(), b"hello");
        assert_eq!(std::fs::read_dir(&fixture.0).unwrap().count(), 1);
    }

    #[test]
    fn suggested_name_removes_directory_and_keeps_extension() {
        for (input, expected) in [
            ("audio.wav", "audio-123.wav"),
            ("folder/audio.final.mp3", "audio.final-123.mp3"),
            ("hello", "hello-123"),
            ("音声.wav", "音声-123.wav"),
        ] {
            assert_eq!(suggested_filename(input, 123).unwrap(), expected);
        }
        assert!(suggested_filename("", 123).is_err());
        assert!(suggested_filename("/", 123).is_err());
    }
}
