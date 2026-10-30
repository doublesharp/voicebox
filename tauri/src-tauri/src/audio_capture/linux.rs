use crate::audio_capture::AudioCaptureState;
use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use cpal::{SampleFormat, StreamConfig};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread;

/// Try to find a PulseAudio/PipeWire monitor source using `pactl`.
/// Returns the source name (e.g. "alsa_output.pci-0000_0d_00.6.analog-stereo.monitor") if found.
fn find_monitor_source_via_pactl() -> Option<String> {
    let output = std::process::Command::new("pactl")
        .args(["list", "short", "sources"])
        .output()
        .ok()?;

    if !output.status.success() {
        return None;
    }

    let stdout = String::from_utf8_lossy(&output.stdout);

    // First, try to find the monitor of the default sink
    let default_sink = std::process::Command::new("pactl")
        .args(["get-default-sink"])
        .output()
        .ok()
        .and_then(|o| {
            if o.status.success() {
                Some(String::from_utf8_lossy(&o.stdout).trim().to_string())
            } else {
                None
            }
        });

    let monitor = super::linux_device::monitor_source_from_pactl(&stdout, default_sink.as_deref())?;
    eprintln!(
        "Linux audio capture: Found monitor source via pactl: {}",
        monitor
    );
    Some(monitor.to_owned())
}

/// Select the capture device: prefer an exact match against the monitor
/// source name reported by `pactl`, then fall back to recognizable output
/// monitor names rather than arbitrary input names containing "monitor".
fn select_capture_device(host: &cpal::Host, monitor_source: Option<&str>) -> Option<cpal::Device> {
    let devices: Vec<cpal::Device> = host.input_devices().ok()?.collect();

    super::linux_device::select_monitor_device(devices, monitor_source, |device| device.name().ok())
}

/// Start capturing system audio on Linux using PulseAudio monitor sources.
///
/// On modern Linux with PulseAudio or PipeWire, we first try to detect the
/// monitor source via `pactl`, then select the matching cpal input device by
/// name. This avoids mutating the process environment (`PULSE_SOURCE`), which
/// is not thread-safe and would affect every thread in the process. If `pactl`
/// is unavailable, we fall back to recognizable cpal output-monitor names.
pub async fn start_capture(
    state: &AudioCaptureState,
    max_duration_secs: u32,
) -> Result<(), String> {
    super::validate_capture_duration(max_duration_secs)?;
    let (capture, mut rx, timeout_tx, done_tx) = state.begin_portable_capture();
    let samples = capture.samples.clone();
    let sample_rate_arc = capture.sample_rate.clone();
    let channels_arc = capture.channels.clone();
    let error_arc = capture.error.clone();

    // Use AtomicBool for stop signal (works across threads)
    let stop_flag = Arc::new(AtomicBool::new(false));
    let stop_flag_clone = stop_flag.clone();

    // Bridge the capture-owned channel to the worker's stop flag.
    tokio::spawn(async move {
        rx.recv().await;
        stop_flag_clone.store(true, Ordering::Relaxed);
    });

    // Spawn capture on a dedicated thread
    thread::spawn(move || {
        let _completion = super::capture_worker_completion(done_tx);
        let host = cpal::default_host();
        let monitor_source = find_monitor_source_via_pactl();

        let device = match select_capture_device(&host, monitor_source.as_deref()) {
            Some(d) => d,
            None => {
                let error_msg = "No system audio monitor is available. Check your PulseAudio or PipeWire configuration.".to_string();
                eprintln!("{}", error_msg);
                *error_arc.lock().unwrap() = Some(error_msg);
                return;
            }
        };

        let device_name = device.name().unwrap_or_else(|_| "unknown".to_string());
        eprintln!("Linux audio capture: Using device: {}", device_name);

        // Get supported config
        let config = match device.default_input_config() {
            Ok(c) => c,
            Err(e) => {
                let error_msg = format!("Failed to get default input config: {}", e);
                eprintln!("{}", error_msg);
                *error_arc.lock().unwrap() = Some(error_msg);
                return;
            }
        };

        let sample_rate = config.sample_rate().0;
        let channels = config.channels();
        let sample_format = config.sample_format();

        eprintln!(
            "Linux audio capture: Config - {}Hz, {} channels, format: {:?}",
            sample_rate, channels, sample_format
        );

        *sample_rate_arc.lock().unwrap() = sample_rate;
        *channels_arc.lock().unwrap() = channels;

        let stream_config = StreamConfig {
            channels,
            sample_rate: cpal::SampleRate(sample_rate),
            buffer_size: cpal::BufferSize::Default,
        };

        let samples_clone = samples.clone();
        let error_arc_clone = error_arc.clone();
        let stop_flag_for_stream = stop_flag.clone();

        let err_fn = {
            let error_arc = error_arc.clone();
            move |err: cpal::StreamError| {
                let error_msg = format!("Stream error: {}", err);
                eprintln!("{}", error_msg);
                *error_arc.lock().unwrap() = Some(error_msg);
            }
        };

        let stream = match sample_format {
            SampleFormat::F32 => {
                let samples = samples_clone.clone();
                let stop = stop_flag_for_stream.clone();
                device.build_input_stream(
                    &stream_config,
                    move |data: &[f32], _: &cpal::InputCallbackInfo| {
                        if stop.load(Ordering::Relaxed) {
                            return;
                        }
                        let mut guard = samples.lock().unwrap();
                        guard.extend_from_slice(data);
                    },
                    err_fn,
                    None,
                )
            }
            SampleFormat::I16 => {
                let samples = samples_clone.clone();
                let stop = stop_flag_for_stream.clone();
                device.build_input_stream(
                    &stream_config,
                    move |data: &[i16], _: &cpal::InputCallbackInfo| {
                        if stop.load(Ordering::Relaxed) {
                            return;
                        }
                        let mut guard = samples.lock().unwrap();
                        for &s in data {
                            guard.push(s as f32 / 32768.0);
                        }
                    },
                    err_fn,
                    None,
                )
            }
            SampleFormat::U16 => {
                let samples = samples_clone.clone();
                let stop = stop_flag_for_stream.clone();
                device.build_input_stream(
                    &stream_config,
                    move |data: &[u16], _: &cpal::InputCallbackInfo| {
                        if stop.load(Ordering::Relaxed) {
                            return;
                        }
                        let mut guard = samples.lock().unwrap();
                        for &s in data {
                            guard.push((s as f32 / 32768.0) - 1.0);
                        }
                    },
                    err_fn,
                    None,
                )
            }
            _ => {
                let error_msg = format!("Unsupported sample format: {:?}", sample_format);
                eprintln!("{}", error_msg);
                *error_arc_clone.lock().unwrap() = Some(error_msg);
                return;
            }
        };

        let stream = match stream {
            Ok(s) => s,
            Err(e) => {
                let error_msg = format!("Failed to build input stream: {}", e);
                eprintln!("{}", error_msg);
                *error_arc_clone.lock().unwrap() = Some(error_msg);
                return;
            }
        };

        if let Err(e) = stream.play() {
            let error_msg = format!("Failed to start stream: {}", e);
            eprintln!("{}", error_msg);
            *error_arc_clone.lock().unwrap() = Some(error_msg);
            return;
        }

        eprintln!("Linux audio capture: Stream started successfully");

        // Keep thread alive until stop signal
        loop {
            if stop_flag.load(Ordering::Relaxed) {
                break;
            }
            std::thread::sleep(std::time::Duration::from_millis(100));
        }

        // Stream will be dropped here, stopping capture
        eprintln!("Linux audio capture: Stream stopped");
    });

    // Each timer only owns a weak reference to its own capture's stop signal.
    super::schedule_capture_timeout(
        timeout_tx,
        std::time::Duration::from_secs(max_duration_secs as u64),
    );

    Ok(())
}

pub async fn stop_capture(state: &AudioCaptureState) -> Result<String, String> {
    super::stop_portable_capture(state).await
}

pub fn is_supported() -> bool {
    let monitor_source = find_monitor_source_via_pactl();
    let host = cpal::default_host();
    super::linux_device::capture_supported(monitor_source.as_deref(), |source| {
        select_capture_device(&host, source)
    })
}
