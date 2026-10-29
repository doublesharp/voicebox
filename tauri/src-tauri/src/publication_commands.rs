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
    let name = Path::new(filename)
        .file_name()
        .and_then(|v| v.to_str())
        .filter(|v| !v.is_empty())
        .ok_or("Invalid filename")?;
    let stamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_err(|e| e.to_string())?
        .as_millis();
    let name_path = Path::new(name);
    let stem = name_path
        .file_stem()
        .and_then(|v| v.to_str())
        .unwrap_or("export");
    let ext = name_path
        .extension()
        .and_then(|v| v.to_str())
        .map(|v| format!(".{v}"))
        .unwrap_or_default();
    let mut dialog = app
        .dialog()
        .file()
        .set_file_name(format!("{stem}-{stamp}{ext}"));
    for filter in filters {
        let extensions: Vec<&str> = filter.extensions.iter().map(String::as_str).collect();
        dialog = dialog.add_filter(&filter.name, &extensions);
    }
    dialog
        .blocking_save_file()
        .map(|p| p.into_path().map_err(|e| e.to_string()))
        .transpose()
}

fn run(
    app: AppHandle,
    filename: String,
    filters: Vec<FileFilter>,
    input: impl FnOnce() -> Result<Box<dyn Read>, String>,
) -> Result<SaveResult, String> {
    let Some(path) = choose(&app, &filename, &filters)? else {
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

#[tauri::command]
pub async fn publish_file_bytes(
    app: AppHandle,
    filename: String,
    filters: Vec<FileFilter>,
    bytes: Vec<u8>,
) -> Result<SaveResult, String> {
    tauri::async_runtime::spawn_blocking(move || {
        run(app, filename, filters, || {
            Ok(Box::new(io::Cursor::new(bytes)))
        })
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
pub async fn publish_file_url(
    app: AppHandle,
    filename: String,
    filters: Vec<FileFilter>,
    url: String,
) -> Result<SaveResult, String> {
    tauri::async_runtime::spawn_blocking(move || {
        run(app, filename, filters, || {
            download(&url, std::time::Duration::from_secs(30))
        })
    })
    .await
    .map_err(|e| e.to_string())?
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
