mod wav;

#[cfg(target_os = "linux")]
mod linux;
#[cfg(target_os = "macos")]
mod macos;
#[cfg(target_os = "windows")]
mod windows;

#[cfg(target_os = "linux")]
pub use linux::*;
#[cfg(target_os = "macos")]
pub use macos::*;
#[cfg(target_os = "windows")]
pub use windows::*;

use std::sync::{Arc, Mutex};

#[cfg(target_os = "macos")]
use screencapturekit::stream::sc_stream::SCStream;

pub struct AudioCaptureState {
    pub samples: Arc<Mutex<Vec<f32>>>,
    pub sample_rate: Arc<Mutex<u32>>,
    pub channels: Arc<Mutex<u16>>,
    pub stop_tx: Arc<Mutex<Option<tokio::sync::mpsc::Sender<()>>>>,
    pub error: Arc<Mutex<Option<String>>>,
    #[cfg(any(test, target_os = "linux", target_os = "windows"))]
    portable_capture: Mutex<Option<PortableCapture>>,
    #[cfg(target_os = "macos")]
    pub stream: Arc<Mutex<Option<SCStream>>>,
    #[cfg(target_os = "macos")]
    pub(crate) lifecycle: macos::CaptureLifecycle,
    #[cfg(target_os = "macos")]
    pub(crate) capture_stop: Mutex<Option<Arc<macos::CaptureStop>>>,
}

impl AudioCaptureState {
    pub fn new() -> Self {
        Self {
            samples: Arc::new(Mutex::new(Vec::new())),
            sample_rate: Arc::new(Mutex::new(44100)),
            channels: Arc::new(Mutex::new(2)),
            stop_tx: Arc::new(Mutex::new(None)),
            error: Arc::new(Mutex::new(None)),
            #[cfg(any(test, target_os = "linux", target_os = "windows"))]
            portable_capture: Mutex::new(None),
            #[cfg(target_os = "macos")]
            stream: Arc::new(Mutex::new(None)),
            #[cfg(target_os = "macos")]
            lifecycle: macos::CaptureLifecycle::default(),
            #[cfg(target_os = "macos")]
            capture_stop: Mutex::new(None),
        }
    }

    pub fn reset(&self) {
        *self.samples.lock().unwrap() = Vec::new();
        *self.error.lock().unwrap() = None;
    }
}

#[cfg(any(test, target_os = "linux", target_os = "windows"))]
#[derive(Clone)]
struct PortableCapture {
    samples: Arc<Mutex<Vec<f32>>>,
    sample_rate: Arc<Mutex<u32>>,
    channels: Arc<Mutex<u16>>,
    error: Arc<Mutex<Option<String>>>,
    stop_tx: Arc<Mutex<Option<tokio::sync::mpsc::Sender<()>>>>,
    completion: Arc<Mutex<Option<tokio::sync::oneshot::Receiver<Result<(), String>>>>>,
}

#[cfg(any(test, target_os = "linux", target_os = "windows"))]
impl AudioCaptureState {
    fn begin_portable_capture(
        &self,
    ) -> (
        PortableCapture,
        tokio::sync::mpsc::Receiver<()>,
        tokio::sync::mpsc::WeakSender<()>,
        tokio::sync::oneshot::Sender<Result<(), String>>,
    ) {
        let (tx, rx) = tokio::sync::mpsc::channel(1);
        let (done_tx, done_rx) = tokio::sync::oneshot::channel();
        let timer_sender = tx.downgrade();
        let capture = PortableCapture {
            samples: Arc::new(Mutex::new(Vec::new())),
            sample_rate: Arc::new(Mutex::new(44100)),
            channels: Arc::new(Mutex::new(2)),
            error: Arc::new(Mutex::new(None)),
            stop_tx: Arc::new(Mutex::new(Some(tx))),
            completion: Arc::new(Mutex::new(Some(done_rx))),
        };
        *self.portable_capture.lock().unwrap() = Some(capture.clone());
        (capture, rx, timer_sender, done_tx)
    }

    fn take_portable_capture(&self) -> Result<PortableCapture, String> {
        self.portable_capture
            .lock()
            .map_err(|_| "Capture state unavailable")?
            .take()
            .ok_or_else(|| "No system audio capture is active".into())
    }
}

#[cfg(any(test, target_os = "linux", target_os = "windows"))]
fn schedule_capture_timeout(
    sender: tokio::sync::mpsc::WeakSender<()>,
    delay: std::time::Duration,
) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        tokio::time::sleep(delay).await;
        if let Some(tx) = sender.upgrade() {
            let _ = tx.send(()).await;
        }
    })
}

#[cfg(any(test, target_os = "linux", target_os = "windows"))]
fn signal_capture_stop(slot: &Arc<Mutex<Option<tokio::sync::mpsc::Sender<()>>>>) {
    if let Some(tx) = slot.lock().unwrap().take() {
        let _ = tx.try_send(());
    }
}

#[cfg(any(test, target_os = "linux", target_os = "windows"))]
fn capture_worker_completion(
    sender: tokio::sync::oneshot::Sender<Result<(), String>>,
) -> impl Drop {
    scopeguard::guard(sender, |sender| {
        let result = if std::thread::panicking() {
            Err("System audio capture worker panicked".to_string())
        } else {
            Ok(())
        };
        let _ = sender.send(result);
    })
}

#[cfg(any(test, target_os = "linux", target_os = "windows"))]
async fn stop_portable_capture(state: &AudioCaptureState) -> Result<String, String> {
    stop_portable_capture_with_timeout(state, std::time::Duration::from_secs(5)).await
}

#[cfg(any(test, target_os = "linux", target_os = "windows"))]
async fn stop_portable_capture_with_timeout(
    state: &AudioCaptureState,
    timeout: std::time::Duration,
) -> Result<String, String> {
    use base64::Engine;
    let capture = state.take_portable_capture()?;
    signal_capture_stop(&capture.stop_tx);
    let completion = capture
        .completion
        .lock()
        .map_err(|_| "Capture state unavailable")?
        .take()
        .ok_or("Capture worker completion unavailable")?;
    tokio::time::timeout(timeout, completion)
        .await
        .map_err(|_| "System audio capture worker did not stop in time")?
        .map_err(|_| "System audio capture worker exited unexpectedly")??;
    if let Some(error) = capture.error.lock().unwrap().as_ref() {
        return Err(error.clone());
    }
    let samples = capture.samples.lock().unwrap().clone();
    let sample_rate = *capture.sample_rate.lock().unwrap();
    let channels = *capture.channels.lock().unwrap();
    if samples.is_empty() {
        return Err("No audio samples captured. Make sure audio is playing on your system during recording.".into());
    }
    let wav = wav::samples_to_wav(&samples, sample_rate, channels)?;
    Ok(base64::engine::general_purpose::STANDARD.encode(&wav))
}

#[cfg(test)]
mod capture_timeout_tests {
    use super::*;
    use std::time::Duration;

    #[tokio::test]
    async fn stop_waits_for_the_worker_to_finish_before_encoding_audio() {
        use base64::Engine;
        let state = AudioCaptureState::new();
        let (capture, mut rx, _timer, done) = state.begin_portable_capture();
        *capture.samples.lock().unwrap() = vec![0.25, -0.25];
        let samples = capture.samples.clone();
        let worker = tokio::spawn(async move {
            assert_eq!(rx.recv().await, Some(()));
            tokio::time::sleep(Duration::from_millis(650)).await;
            samples.lock().unwrap().extend([0.5, -0.5]);
            let _ = done.send(Ok(()));
        });
        let encoded = stop_portable_capture(&state).await.unwrap();
        worker.await.unwrap();
        let data = base64::engine::general_purpose::STANDARD
            .decode(encoded)
            .unwrap();
        let mut wav = hound::WavReader::new(std::io::Cursor::new(data)).unwrap();
        assert_eq!(
            wav.samples::<i16>().map(Result::unwrap).collect::<Vec<_>>(),
            [8191, -8191, 16383, -16383]
        );
    }

    #[tokio::test]
    async fn overlapping_stop_and_start_return_their_own_audio() {
        use base64::Engine;
        let state = Arc::new(AudioCaptureState::new());
        let (first, mut first_rx, _timer, first_done) = state.begin_portable_capture();
        *first.samples.lock().unwrap() = vec![0.25, -0.25];
        let state_for_stop = state.clone();
        let stopping = tokio::spawn(async move { stop_portable_capture(&state_for_stop).await });
        assert_eq!(first_rx.recv().await, Some(()));
        let (second, _second_rx, _timer, second_done) = state.begin_portable_capture();
        *second.samples.lock().unwrap() = vec![0.5, -0.5];
        first_done.send(Ok(())).unwrap();
        let first_result = stopping.await.unwrap().unwrap();
        second_done.send(Ok(())).unwrap();
        let second_result = stop_portable_capture(&state).await.unwrap();
        for (encoded, expected) in [
            (first_result, [8191, -8191]),
            (second_result, [16383, -16383]),
        ] {
            let data = base64::engine::general_purpose::STANDARD
                .decode(encoded)
                .unwrap();
            let mut wav = hound::WavReader::new(std::io::Cursor::new(data)).unwrap();
            assert_eq!(
                wav.samples::<i16>().map(Result::unwrap).collect::<Vec<_>>(),
                expected
            );
        }
    }

    #[tokio::test]
    async fn worker_guard_reports_a_real_thread_panic() {
        let state = AudioCaptureState::new();
        let (capture, _rx, _timer, done) = state.begin_portable_capture();
        *capture.samples.lock().unwrap() = vec![0.25, -0.25];
        let worker = std::thread::spawn(move || {
            let _completion = capture_worker_completion(done);
            panic!("simulated capture worker panic");
        });
        assert!(worker.join().is_err());
        let error = stop_portable_capture_with_timeout(&state, Duration::from_secs(1))
            .await
            .unwrap_err();
        assert!(error.contains("worker panicked"));
    }

    #[tokio::test]
    async fn completed_worker_without_samples_returns_an_error() {
        let state = AudioCaptureState::new();
        let (_capture, _rx, _timer, done) = state.begin_portable_capture();
        let worker = std::thread::spawn(move || {
            let _completion = capture_worker_completion(done);
        });
        worker.join().unwrap();
        let error = stop_portable_capture_with_timeout(&state, Duration::from_secs(1))
            .await
            .unwrap_err();
        assert!(error.contains("No audio samples"));
        assert!(stop_portable_capture(&state)
            .await
            .unwrap_err()
            .contains("No system audio capture is active"));
    }

    #[tokio::test]
    async fn a_stalled_worker_reports_an_error_instead_of_partial_audio() {
        let state = AudioCaptureState::new();
        let (capture, _rx, _timer, _done) = state.begin_portable_capture();
        *capture.samples.lock().unwrap() = vec![0.25, -0.25];
        let error = stop_portable_capture_with_timeout(&state, Duration::from_millis(30))
            .await
            .unwrap_err();
        assert!(error.contains("did not stop in time"));
    }

    #[tokio::test]
    async fn worker_failure_and_missing_completion_are_reported() {
        for failure in [Some("capture worker panicked"), None] {
            let state = AudioCaptureState::new();
            let (_capture, _rx, _timer, done) = state.begin_portable_capture();
            if let Some(message) = failure {
                let _ = done.send(Err(message.into()));
            } else {
                drop(done);
            }
            let error = stop_portable_capture_with_timeout(&state, Duration::from_secs(1))
                .await
                .unwrap_err();
            assert!(error.contains(failure.unwrap_or("exited unexpectedly")));
        }
    }

    #[tokio::test]
    async fn capture_errors_and_invalid_samples_are_not_returned_as_audio() {
        for mode in 0..3 {
            let state = AudioCaptureState::new();
            let (capture, _rx, _timer, done) = state.begin_portable_capture();
            match mode {
                0 => *capture.error.lock().unwrap() = Some("device failed".into()),
                1 => *capture.samples.lock().unwrap() = vec![0.1],
                _ => *capture.samples.lock().unwrap() = vec![f32::NAN, 0.0],
            }
            let _ = done.send(Ok(()));
            let error = stop_portable_capture_with_timeout(&state, Duration::from_secs(1))
                .await
                .unwrap_err();
            assert!(error.contains(match mode {
                0 => "device failed",
                1 => "incomplete audio frame",
                _ => "non-finite",
            }));
        }
    }

    #[test]
    fn late_worker_from_previous_capture_cannot_contaminate_new_audio() {
        let state = AudioCaptureState::new();
        let (old, _old_rx, _timer, _done) = state.begin_portable_capture();
        old.samples.lock().unwrap().push(0.5);
        let (new, _new_rx, _timer, _done) = state.begin_portable_capture();
        *new.sample_rate.lock().unwrap() = 48000;
        *new.channels.lock().unwrap() = 2;
        new.samples.lock().unwrap().extend([0.25, -0.25]);
        old.samples.lock().unwrap().push(0.75);
        *old.sample_rate.lock().unwrap() = 8000;
        *old.channels.lock().unwrap() = 1;
        *old.error.lock().unwrap() = Some("old capture failed".into());
        assert_eq!(*new.samples.lock().unwrap(), [0.25, -0.25]);
        assert_eq!(*new.sample_rate.lock().unwrap(), 48000);
        assert_eq!(*new.channels.lock().unwrap(), 2);
        assert!(new.error.lock().unwrap().is_none());
    }

    #[tokio::test]
    async fn replacement_closes_the_old_channel_and_stop_takes_only_current_capture() {
        let state = AudioCaptureState::new();
        let (old, mut old_rx, _timer, _done) = state.begin_portable_capture();
        drop(old);
        let (current, mut current_rx, _timer, _done) = state.begin_portable_capture();
        assert_eq!(old_rx.recv().await, None);
        let stopped = state.take_portable_capture().unwrap();
        assert!(Arc::ptr_eq(&stopped.samples, &current.samples));
        signal_capture_stop(&stopped.stop_tx);
        assert_eq!(current_rx.recv().await, Some(()));
        assert!(state.take_portable_capture().is_err());
    }

    #[test]
    fn stopping_old_capture_cannot_take_replacement_buffers() {
        let state = AudioCaptureState::new();
        let (old, _old_rx, _timer, _done) = state.begin_portable_capture();
        let taken = state.take_portable_capture().unwrap();
        let (new, _new_rx, _timer, _done) = state.begin_portable_capture();
        taken.samples.lock().unwrap().push(0.75);
        assert!(new.samples.lock().unwrap().is_empty());
        let current = state.take_portable_capture().unwrap();
        assert!(Arc::ptr_eq(&current.samples, &new.samples));
        assert!(Arc::ptr_eq(&old.samples, &taken.samples));
    }

    #[tokio::test]
    async fn stop_can_run_before_the_start_caller_schedules_its_timer() {
        let state = AudioCaptureState::new();
        let (_capture, mut rx, timer_sender, _done) = state.begin_portable_capture();
        let stopped = state.take_portable_capture().unwrap();
        signal_capture_stop(&stopped.stop_tx);
        assert_eq!(rx.recv().await, Some(()));
        schedule_capture_timeout(timer_sender, Duration::ZERO)
            .await
            .unwrap();
        assert_eq!(rx.recv().await, None);
    }

    #[tokio::test]
    async fn old_capture_timeout_must_not_stop_a_replacement() {
        let (first, mut first_rx) = tokio::sync::mpsc::channel(1);
        let weak = first.downgrade();
        let slot = Arc::new(Mutex::new(Some(first)));
        let timer = schedule_capture_timeout(weak, Duration::from_millis(50));
        let old = slot.lock().unwrap().take().unwrap();
        old.send(()).await.unwrap();
        assert_eq!(first_rx.recv().await, Some(()));
        drop(old);
        let (second, mut second_rx) = tokio::sync::mpsc::channel(1);
        *slot.lock().unwrap() = Some(second);
        timer.await.unwrap();
        assert!(matches!(
            second_rx.try_recv(),
            Err(tokio::sync::mpsc::error::TryRecvError::Empty)
        ));
        assert!(slot.lock().unwrap().is_some());
    }

    #[tokio::test]
    async fn replacing_a_capture_drops_the_old_channel_without_waiting_for_its_timer() {
        let (first, mut first_rx) = tokio::sync::mpsc::channel(1);
        let timer = schedule_capture_timeout(first.downgrade(), Duration::from_millis(100));
        let slot = Arc::new(Mutex::new(Some(first)));
        let (second, _second_rx) = tokio::sync::mpsc::channel(1);
        *slot.lock().unwrap() = Some(second);
        assert_eq!(
            tokio::time::timeout(Duration::from_millis(50), first_rx.recv())
                .await
                .unwrap(),
            None
        );
        timer.await.unwrap();
        assert!(slot.lock().unwrap().is_some());
    }

    #[tokio::test]
    async fn manual_stop_signals_even_when_another_sender_exists() {
        let (sender, mut receiver) = tokio::sync::mpsc::channel(1);
        let other = sender.clone();
        let slot = Arc::new(Mutex::new(Some(sender)));
        signal_capture_stop(&slot);
        assert_eq!(receiver.try_recv(), Ok(()));
        assert!(slot.lock().unwrap().is_none());
        drop(other);
    }

    #[tokio::test]
    async fn manual_stop_preserves_an_already_queued_stop_signal() {
        let (sender, mut receiver) = tokio::sync::mpsc::channel(1);
        sender.try_send(()).unwrap();
        let slot = Arc::new(Mutex::new(Some(sender)));
        signal_capture_stop(&slot);
        assert_eq!(receiver.recv().await, Some(()));
        assert!(slot.lock().unwrap().is_none());
    }

    #[tokio::test]
    async fn a_timeout_after_manual_stop_is_harmless() {
        let (sender, mut receiver) = tokio::sync::mpsc::channel(1);
        let timer = schedule_capture_timeout(sender.downgrade(), Duration::from_millis(30));
        sender.send(()).await.unwrap();
        drop(sender);
        assert_eq!(receiver.recv().await, Some(()));
        timer.await.unwrap();
        assert_eq!(receiver.recv().await, None);
    }

    #[tokio::test]
    async fn active_capture_timeout_stops_its_own_recording() {
        let (sender, mut receiver) = tokio::sync::mpsc::channel(1);
        let timer = schedule_capture_timeout(sender.downgrade(), Duration::from_millis(20));
        assert_eq!(
            tokio::time::timeout(Duration::from_secs(2), receiver.recv())
                .await
                .unwrap(),
            Some(())
        );
        timer.await.unwrap();
    }
}
