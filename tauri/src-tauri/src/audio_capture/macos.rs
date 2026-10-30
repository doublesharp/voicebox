use super::wav::samples_to_wav;
use crate::audio_capture::AudioCaptureState;
use base64::{engine::general_purpose, Engine as _};
use screencapturekit::{
    cm::CMSampleBuffer,
    shareable_content::SCShareableContent,
    stream::{
        configuration::SCStreamConfiguration, content_filter::SCContentFilter,
        output_trait::SCStreamOutputTrait, output_type::SCStreamOutputType, sc_stream::SCStream,
    },
};
use std::process::Command;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use tokio::sync::mpsc;

#[derive(Default)]
pub(super) struct CaptureLifecycle(Mutex<CapturePhase>);

#[derive(Default, PartialEq, Eq)]
enum CapturePhase {
    #[default]
    Idle,
    Starting,
    Active,
    Stopping,
}

struct StartGuard<'a> {
    lifecycle: &'a CaptureLifecycle,
    committed: bool,
}

struct StopGuard<'a>(&'a CaptureLifecycle);

impl CaptureLifecycle {
    fn begin_start(&self) -> Result<StartGuard<'_>, String> {
        let mut phase = self.0.lock().map_err(|_| "Capture state unavailable")?;
        if *phase != CapturePhase::Idle {
            return Err("System audio capture is already active or stopping".into());
        }
        *phase = CapturePhase::Starting;
        Ok(StartGuard {
            lifecycle: self,
            committed: false,
        })
    }

    fn begin_stop(&self) -> Result<StopGuard<'_>, String> {
        let mut phase = self.0.lock().map_err(|_| "Capture state unavailable")?;
        if matches!(*phase, CapturePhase::Starting | CapturePhase::Stopping) {
            return Err("System audio capture is starting or stopping".into());
        }
        *phase = CapturePhase::Stopping;
        Ok(StopGuard(self))
    }
}

impl StartGuard<'_> {
    fn commit(mut self) {
        *self.lifecycle.0.lock().unwrap() = CapturePhase::Active;
        self.committed = true;
    }
}

impl Drop for StartGuard<'_> {
    fn drop(&mut self) {
        if !self.committed {
            *self.lifecycle.0.lock().unwrap() = CapturePhase::Idle;
        }
    }
}

impl Drop for StopGuard<'_> {
    fn drop(&mut self) {
        *self.0 .0.lock().unwrap() = CapturePhase::Idle;
    }
}

pub async fn start_capture(
    state: &AudioCaptureState,
    max_duration_secs: u32,
) -> Result<(), String> {
    super::validate_capture_duration(max_duration_secs)?;
    if !is_supported() {
        return Err("System audio capture requires macOS 12.3 or newer.".to_string());
    }

    let start = state.lifecycle.begin_start()?;
    // Reset previous samples only after this call owns the capture lifecycle.
    state.reset();

    // Get shareable content
    let content =
        SCShareableContent::get().map_err(|e| format!("Failed to get shareable content: {}", e))?;

    // Get first display
    let displays = content.displays();
    if displays.is_empty() {
        return Err("No displays available".to_string());
    }
    let display = &displays[0];

    // Create content filter for desktop audio
    let filter = SCContentFilter::create()
        .with_display(display)
        .with_excluding_windows(&[])
        .build();

    // Create stream configuration - audio only
    let mut config = SCStreamConfiguration::default();
    config.set_captures_audio(true);
    config.set_excludes_current_process_audio(false);
    config.set_sample_rate(48000); // Use i32 directly
    config.set_channel_count(2); // Use i32 directly

    // Create stream using builder
    let (tx, mut rx) = mpsc::channel::<()>(1);

    let samples = state.samples.clone();
    let sample_rate = state.sample_rate.clone();
    let channels = state.channels.clone();

    // Set sample rate and channels
    *sample_rate.lock().unwrap() = 48000;
    *channels.lock().unwrap() = 2;

    // Create output handler struct
    struct AudioHandler {
        samples: Arc<Mutex<Vec<f32>>>,
        errors: Arc<Mutex<Option<String>>>,
        active: Arc<AtomicBool>,
    }

    impl SCStreamOutputTrait for AudioHandler {
        fn did_output_sample_buffer(&self, sample: CMSampleBuffer, _type: SCStreamOutputType) {
            if _type == SCStreamOutputType::Audio {
                record_capture_result(
                    &self.samples,
                    &self.errors,
                    &self.active,
                    extract_audio_samples(sample),
                );
            }
        }
    }

    let active = Arc::new(AtomicBool::new(true));
    let handler = AudioHandler {
        samples: samples.clone(),
        errors: state.error.clone(),
        active: active.clone(),
    };

    // Create stream
    let mut stream = SCStream::new(&filter, &config);

    // Add output handler for audio (order: handler, then output_type)
    stream.add_output_handler(handler, SCStreamOutputType::Audio);

    if let Err(error) = stream.start_capture() {
        active.store(false, Ordering::Release);
        return Err(format!("Failed to start capture: {}", error));
    }
    let stop_control = Arc::new(CaptureStop::new(active));
    *state.capture_stop.lock().unwrap() = Some(stop_control.clone());
    *state.stop_tx.lock().unwrap() = Some(tx);
    *state.stream.lock().unwrap() = Some(stream.clone());

    // Spawn task to stop after max duration
    let stream_clone = stream.clone();
    tokio::spawn(async move {
        tokio::select! {
            _ = tokio::time::sleep(tokio::time::Duration::from_secs(max_duration_secs as u64)) => {
                let _ = stop_control.stop(|| stream_clone.stop_capture().map_err(|error| error.to_string()));
            }
            _ = rx.recv() => {
                // Manual stop owns stopping the stream.
            }
        }
    });

    start.commit();
    Ok(())
}

pub async fn stop_capture(state: &AudioCaptureState) -> Result<String, String> {
    let _stopping = state.lifecycle.begin_stop()?;
    let stop_control = state.capture_stop.lock().unwrap().take();
    // Signal stop
    if let Some(tx) = state.stop_tx.lock().unwrap().take() {
        let _ = tx.try_send(());
    }

    // Stop stream if still active
    if let Some(stream) = state.stream.lock().unwrap().take() {
        let stop = || stream.stop_capture().map_err(|error| error.to_string());
        let result = match stop_control {
            Some(control) => control.stop(stop),
            None => stop(),
        };
        result.map_err(|error| format!("Failed to stop system audio capture: {error}"))?;
    }

    // Wait a bit for capture to stop
    tokio::time::sleep(tokio::time::Duration::from_millis(500)).await;

    // Take the sample lock first, matching the callback lock order. This waits
    // for an already-admitted callback to finish recording any error.
    let (samples, error) = {
        let samples = state.samples.lock().unwrap();
        let error = state.error.lock().unwrap();
        (samples.clone(), error.clone())
    };
    if let Some(error) = error {
        return Err(format!("System audio capture failed: {error}"));
    }
    let sample_rate = *state.sample_rate.lock().unwrap();
    let channels = *state.channels.lock().unwrap();

    if samples.is_empty() {
        return Err("No audio samples captured".to_string());
    }

    // Convert to WAV
    let wav_data = samples_to_wav(&samples, sample_rate, channels)?;

    // Encode to base64
    let base64_data = general_purpose::STANDARD.encode(&wav_data);

    Ok(base64_data)
}

pub fn is_supported() -> bool {
    macos_version_at_least(12, 3)
}

fn macos_version_at_least(required_major: u64, required_minor: u64) -> bool {
    let output = match Command::new("sw_vers").arg("-productVersion").output() {
        Ok(output) if output.status.success() => output,
        _ => return false,
    };

    let version = String::from_utf8_lossy(&output.stdout);
    let mut parts = version.trim().split('.');

    let major = parts
        .next()
        .and_then(|part| part.parse::<u64>().ok())
        .unwrap_or(0);
    let minor = parts
        .next()
        .and_then(|part| part.parse::<u64>().ok())
        .unwrap_or(0);

    major > required_major || (major == required_major && minor >= required_minor)
}

fn extract_audio_samples(sample_buffer: CMSampleBuffer) -> Result<Vec<f32>, String> {
    // Use the crate's built-in method to get audio buffer list
    let audio_buffer_list = sample_buffer
        .audio_buffer_list()
        .ok_or_else(|| "Failed to get audio buffer list".to_string())?;

    let buffers: Vec<_> = audio_buffer_list.iter().collect();
    let bytes: Vec<&[u8]> = buffers.iter().map(|buffer| buffer.data()).collect();
    interleave_capture_buffers(&bytes)
}

fn interleave_capture_buffers(buffers: &[&[u8]]) -> Result<Vec<f32>, String> {
    let mut channels = Vec::with_capacity(buffers.len());
    for bytes in buffers {
        if bytes.len() % std::mem::size_of::<f32>() != 0 {
            return Err("Capture buffer contains an incomplete float sample".into());
        }
        channels.push(
            bytes
                .chunks_exact(4)
                .map(|chunk| f32::from_ne_bytes(chunk.try_into().unwrap()))
                .collect::<Vec<_>>(),
        );
    }
    if channels.len() <= 1 {
        return Ok(channels.into_iter().next().unwrap_or_default());
    }
    let frames = channels.iter().map(Vec::len).max().unwrap_or(0);
    let capacity = frames
        .checked_mul(channels.len())
        .ok_or("Capture buffer is too large")?;
    let mut result = Vec::with_capacity(capacity);
    for index in 0..frames {
        for channel in &channels {
            result.push(channel.get(index).copied().unwrap_or(0.0));
        }
    }
    Ok(result)
}

#[cfg(test)]
mod capture_buffer_regression_tests {
    use super::interleave_capture_buffers;

    fn bytes(samples: &[f32]) -> Vec<u8> {
        samples
            .iter()
            .flat_map(|sample| sample.to_ne_bytes())
            .collect()
    }

    #[test]
    fn planar_stereo_preserves_left_and_right_sample_order() {
        let left = bytes(&[0.1, 0.2]);
        let right = bytes(&[-0.1, -0.2]);
        assert_eq!(
            interleave_capture_buffers(&[&left, &right]).unwrap(),
            [0.1, -0.1, 0.2, -0.2]
        );
    }

    #[test]
    fn empty_left_channel_is_kept_as_silence() {
        let right = bytes(&[0.5, -0.5]);
        assert_eq!(
            interleave_capture_buffers(&[&[], &right]).unwrap(),
            [0.0, 0.5, 0.0, -0.5]
        );
    }

    #[test]
    fn empty_right_channel_is_kept_as_silence() {
        let left = bytes(&[0.25, 0.75]);
        assert_eq!(
            interleave_capture_buffers(&[&left, &[]]).unwrap(),
            [0.25, 0.0, 0.75, 0.0]
        );
    }

    #[test]
    fn shorter_planar_channel_is_padded_without_shifting_frames() {
        let left = bytes(&[0.1, 0.2, 0.3]);
        let right = bytes(&[-0.1]);
        assert_eq!(
            interleave_capture_buffers(&[&left, &right]).unwrap(),
            [0.1, -0.1, 0.2, 0.0, 0.3, 0.0]
        );
    }

    #[test]
    fn interleaved_and_unaligned_byte_slices_decode_without_pointer_casts() {
        let mut unaligned = vec![0];
        unaligned.extend(bytes(&[0.25, -0.5, 1.0, -1.0]));
        assert_eq!(
            interleave_capture_buffers(&[&unaligned[1..]]).unwrap(),
            [0.25, -0.5, 1.0, -1.0]
        );
    }

    #[test]
    fn incomplete_float_samples_are_rejected() {
        for input in [vec![1], vec![1, 2], vec![1, 2, 3], vec![0; 5]] {
            assert!(interleave_capture_buffers(&[&input]).is_err());
            assert!(interleave_capture_buffers(&[&[], &input]).is_err());
        }
    }

    #[test]
    fn no_buffers_and_empty_channels_have_no_samples() {
        assert!(interleave_capture_buffers(&[]).unwrap().is_empty());
        assert!(interleave_capture_buffers(&[&[]]).unwrap().is_empty());
        assert!(interleave_capture_buffers(&[&[], &[]]).unwrap().is_empty());
    }

    #[test]
    fn three_planar_channels_preserve_order_and_padding() {
        let first = bytes(&[1.0, 2.0]);
        let third = bytes(&[3.0]);
        assert_eq!(
            interleave_capture_buffers(&[&first, &[], &third]).unwrap(),
            [1.0, 0.0, 3.0, 2.0, 0.0, 0.0]
        );
    }
}

#[cfg(test)]
mod capture_lifecycle_tests {
    use super::*;

    #[test]
    fn active_capture_rejects_another_start_until_stopped() {
        let gate = CaptureLifecycle::default();
        gate.begin_start().unwrap().commit();
        assert!(gate.begin_start().is_err());
        drop(gate.begin_stop().unwrap());
        assert!(gate.begin_start().is_ok());
    }

    #[test]
    fn failed_start_releases_ownership_for_retry() {
        let gate = CaptureLifecycle::default();
        let start = gate.begin_start().unwrap();
        assert!(gate.begin_start().is_err());
        assert!(gate.begin_stop().is_err());
        drop(start);
        assert!(gate.begin_start().is_ok());
    }

    #[test]
    fn stop_blocks_new_start_and_another_stop_until_result_is_collected() {
        let gate = CaptureLifecycle::default();
        gate.begin_start().unwrap().commit();
        let stop = gate.begin_stop().unwrap();
        assert!(gate.begin_start().is_err());
        assert!(gate.begin_stop().is_err());
        drop(stop);
        assert!(gate.begin_start().is_ok());
    }

    #[test]
    fn abandoned_stop_releases_ownership() {
        let gate = CaptureLifecycle::default();
        gate.begin_start().unwrap().commit();
        let stop = gate.begin_stop().unwrap();
        drop(stop);
        assert!(gate.begin_start().is_ok());
    }

    #[test]
    fn stopping_an_idle_capture_still_serializes_new_starts() {
        let gate = CaptureLifecycle::default();
        let stop = gate.begin_stop().unwrap();
        assert!(gate.begin_start().is_err());
        drop(stop);
        assert!(gate.begin_start().is_ok());
    }

    #[test]
    fn separate_capture_states_are_independent() {
        let first = CaptureLifecycle::default();
        let second = CaptureLifecycle::default();
        let _first = first.begin_start().unwrap();
        assert!(second.begin_start().is_ok());
    }
}

fn append_capture_samples(samples: &Mutex<Vec<f32>>, active: &AtomicBool, input: &[f32]) {
    let mut samples = samples.lock().unwrap();
    if active.load(Ordering::Acquire) {
        samples.extend_from_slice(input);
    }
}

#[cfg(test)]
mod capture_callback_tests {
    use super::*;
    use std::sync::atomic::{AtomicBool, Ordering};

    #[test]
    fn active_callback_appends_samples_in_order() {
        let samples = Mutex::new(vec![0.1]);
        let active = AtomicBool::new(true);
        append_capture_samples(&samples, &active, &[0.2, 0.3]);
        assert_eq!(*samples.lock().unwrap(), [0.1, 0.2, 0.3]);
    }

    #[test]
    fn callback_after_stop_does_not_extend_the_recording() {
        let samples = Mutex::new(vec![0.1]);
        let active = AtomicBool::new(false);
        append_capture_samples(&samples, &active, &[0.9]);
        assert_eq!(*samples.lock().unwrap(), [0.1]);
    }

    #[test]
    fn late_old_callback_cannot_contaminate_a_new_recording() {
        let samples = Mutex::new(Vec::new());
        let old = AtomicBool::new(false);
        let new = AtomicBool::new(true);
        append_capture_samples(&samples, &new, &[0.2]);
        append_capture_samples(&samples, &old, &[0.9]);
        append_capture_samples(&samples, &new, &[0.3]);
        assert_eq!(*samples.lock().unwrap(), [0.2, 0.3]);
    }

    #[test]
    fn stop_while_callback_waits_for_samples_discards_the_late_buffer() {
        let samples = Arc::new(Mutex::new(vec![0.1]));
        let active = Arc::new(AtomicBool::new(true));
        let locked = samples.lock().unwrap();
        let (started, waiting) = std::sync::mpsc::channel();
        let worker_samples = samples.clone();
        let worker_active = active.clone();
        let worker = std::thread::spawn(move || {
            started.send(()).unwrap();
            append_capture_samples(&worker_samples, &worker_active, &[0.9]);
        });
        waiting.recv().unwrap();
        active.store(false, Ordering::Release);
        drop(locked);
        worker.join().unwrap();
        assert_eq!(*samples.lock().unwrap(), [0.1]);
    }

    #[test]
    fn independent_sessions_do_not_share_their_active_flags() {
        let first = AtomicBool::new(false);
        let second = AtomicBool::new(true);
        let one = Mutex::new(Vec::new());
        let two = Mutex::new(Vec::new());
        append_capture_samples(&one, &first, &[1.0]);
        append_capture_samples(&two, &second, &[2.0]);
        assert!(one.lock().unwrap().is_empty());
        assert_eq!(*two.lock().unwrap(), [2.0]);
    }
}

fn record_capture_result(
    samples: &Mutex<Vec<f32>>,
    errors: &Mutex<Option<String>>,
    active: &AtomicBool,
    result: Result<Vec<f32>, String>,
) {
    match result {
        Ok(input) => append_capture_samples(samples, active, &input),
        Err(error) => {
            // Serialize with sample appends and recheck ownership after waiting.
            let _samples = samples.lock().unwrap();
            if active.load(Ordering::Acquire) {
                let mut first_error = errors.lock().unwrap();
                if first_error.is_none() {
                    *first_error = Some(error);
                }
            }
        }
    }
}

#[cfg(test)]
mod capture_error_regression_tests {
    use super::*;

    #[test]
    fn malformed_callback_records_the_conversion_error() {
        let samples = Mutex::new(vec![0.25]);
        let errors = Mutex::new(None);
        let active = AtomicBool::new(true);
        let result = interleave_capture_buffers(&[&[1, 2, 3]]);
        record_capture_result(&samples, &errors, &active, result);
        assert_eq!(*samples.lock().unwrap(), [0.25]);
        assert!(errors
            .lock()
            .unwrap()
            .as_deref()
            .unwrap()
            .contains("incomplete"));
    }

    #[test]
    fn first_capture_error_is_preserved() {
        let samples = Mutex::new(Vec::new());
        let errors = Mutex::new(None);
        let active = AtomicBool::new(true);
        record_capture_result(&samples, &errors, &active, Err("first error".into()));
        record_capture_result(&samples, &errors, &active, Err("second error".into()));
        assert_eq!(errors.lock().unwrap().as_deref(), Some("first error"));
    }

    #[test]
    fn late_error_from_stopped_capture_cannot_poison_replacement() {
        let samples = Mutex::new(Vec::new());
        let errors = Mutex::new(None);
        let old = AtomicBool::new(false);
        let current = AtomicBool::new(true);
        record_capture_result(&samples, &errors, &old, Err("old failure".into()));
        record_capture_result(&samples, &errors, &current, Ok(vec![0.5]));
        assert_eq!(*errors.lock().unwrap(), None);
        assert_eq!(*samples.lock().unwrap(), [0.5]);
    }

    #[test]
    fn valid_callback_preserves_existing_error_and_appends_audio() {
        let samples = Mutex::new(Vec::new());
        let errors = Mutex::new(Some("earlier failure".into()));
        let active = AtomicBool::new(true);
        record_capture_result(&samples, &errors, &active, Ok(vec![0.5, -0.5]));
        assert_eq!(*samples.lock().unwrap(), [0.5, -0.5]);
        assert_eq!(errors.lock().unwrap().as_deref(), Some("earlier failure"));
    }

    #[tokio::test]
    async fn stop_returns_the_capture_error_instead_of_partial_audio() {
        let state = AudioCaptureState::new();
        *state.samples.lock().unwrap() = vec![0.25, -0.25];
        *state.error.lock().unwrap() = Some("incomplete float sample".into());
        let result = stop_capture(&state).await;
        assert!(result.unwrap_err().contains("incomplete float sample"));
    }

    #[tokio::test]
    async fn capture_error_takes_priority_over_missing_samples() {
        let state = AudioCaptureState::new();
        *state.error.lock().unwrap() = Some("capture buffer unavailable".into());
        assert!(stop_capture(&state)
            .await
            .unwrap_err()
            .contains("capture buffer unavailable"));
    }

    #[tokio::test]
    async fn successful_stop_still_returns_decodable_audio() {
        use base64::Engine;
        let state = AudioCaptureState::new();
        *state.samples.lock().unwrap() = vec![0.25, -0.25];
        let encoded = stop_capture(&state).await.unwrap();
        let bytes = general_purpose::STANDARD.decode(encoded).unwrap();
        let reader = hound::WavReader::new(std::io::Cursor::new(bytes)).unwrap();
        assert_eq!(reader.spec().channels, 2);
        assert_eq!(reader.duration(), 1);
    }
}

pub(super) struct CaptureStop {
    active: Arc<AtomicBool>,
    result: Mutex<Option<Result<(), String>>>,
}

impl CaptureStop {
    fn new(active: Arc<AtomicBool>) -> Self {
        Self {
            active,
            result: Mutex::new(None),
        }
    }

    fn stop(&self, action: impl FnOnce() -> Result<(), String>) -> Result<(), String> {
        self.active.store(false, Ordering::Release);
        let mut result = self
            .result
            .lock()
            .map_err(|_| "Capture stop state unavailable")?;
        if let Some(result) = result.as_ref() {
            return result.clone();
        }
        let completed = action();
        *result = Some(completed.clone());
        completed
    }
}

#[cfg(test)]
mod capture_stop_regression_tests {
    use super::*;
    use std::sync::atomic::AtomicUsize;

    #[test]
    fn timeout_then_manual_stop_only_stops_the_stream_once() {
        let active = Arc::new(AtomicBool::new(true));
        let stop = CaptureStop::new(active.clone());
        let calls = AtomicUsize::new(0);
        stop.stop(|| {
            calls.fetch_add(1, Ordering::Relaxed);
            Ok(())
        })
        .unwrap();
        stop.stop(|| {
            calls.fetch_add(1, Ordering::Relaxed);
            Ok(())
        })
        .unwrap();
        assert_eq!(calls.load(Ordering::Relaxed), 1);
        assert!(!active.load(Ordering::Acquire));
    }

    #[test]
    fn timeout_stop_failure_is_reported_again_to_the_manual_caller() {
        let stop = CaptureStop::new(Arc::new(AtomicBool::new(true)));
        assert_eq!(
            stop.stop(|| Err("device stop failed".into())).unwrap_err(),
            "device stop failed"
        );
        assert_eq!(stop.stop(|| Ok(())).unwrap_err(), "device stop failed");
    }

    #[test]
    fn manual_stop_failure_is_not_silently_retried() {
        let stop = CaptureStop::new(Arc::new(AtomicBool::new(true)));
        assert!(stop.stop(|| Err("cannot stop".into())).is_err());
        assert!(stop
            .stop(|| panic!("a failed native stop must not run twice"))
            .is_err());
    }

    #[test]
    fn stop_disables_callbacks_before_stopping_the_stream() {
        let active = Arc::new(AtomicBool::new(true));
        let stop = CaptureStop::new(active.clone());
        let samples = Mutex::new(Vec::new());
        stop.stop(|| {
            append_capture_samples(&samples, &active, &[0.8]);
            Ok(())
        })
        .unwrap();
        assert!(samples.lock().unwrap().is_empty());
    }

    #[test]
    fn concurrent_stop_waits_for_the_original_stop_result() {
        let stop = Arc::new(CaptureStop::new(Arc::new(AtomicBool::new(true))));
        let (started, waiting) = std::sync::mpsc::channel();
        let (release, blocked) = std::sync::mpsc::channel();
        let (second_action, second_seen) = std::sync::mpsc::channel();
        let first_stop = stop.clone();
        let first = std::thread::spawn(move || {
            first_stop.stop(|| {
                started.send(()).unwrap();
                let _ = blocked.recv();
                Err("native stop failed".into())
            })
        });
        waiting.recv().unwrap();
        let second_stop = stop.clone();
        let second = std::thread::spawn(move || {
            second_stop.stop(|| {
                let _ = second_action.send(());
                Ok(())
            })
        });
        std::thread::sleep(std::time::Duration::from_millis(30));
        let prematurely_ran = second_seen.try_recv().is_ok();
        release.send(()).unwrap();
        assert_eq!(first.join().unwrap().unwrap_err(), "native stop failed");
        let second_result = second.join().unwrap();
        assert!(!prematurely_ran, "a concurrent native stop was started");
        assert_eq!(second_result.unwrap_err(), "native stop failed");
    }

    #[test]
    fn separate_captures_have_independent_stop_ownership() {
        let first = CaptureStop::new(Arc::new(AtomicBool::new(true)));
        let second = CaptureStop::new(Arc::new(AtomicBool::new(true)));
        first.stop(|| Err("first failed".into())).unwrap_err();
        second.stop(|| Ok(())).unwrap();
    }
}
