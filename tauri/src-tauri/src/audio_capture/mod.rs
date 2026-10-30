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

#[cfg(test)]
mod capture_timeout_tests {
    use super::*;
    use std::time::Duration;

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
