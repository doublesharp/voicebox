use crate::audio_capture::AudioCaptureState;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread;
use wasapi::*;
use windows::Win32::System::Com::{CoInitializeEx, CoUninitialize, COINIT_MULTITHREADED};

pub async fn start_capture(
    state: &AudioCaptureState,
    max_duration_secs: u32,
) -> Result<(), String> {
    let (capture, mut rx, timeout_tx, done_tx) = state.begin_portable_capture();
    let samples = capture.samples.clone();
    let sample_rate_arc = capture.sample_rate.clone();
    let channels_arc = capture.channels.clone();
    let error_arc = capture.error.clone();

    // Use AtomicBool for stop signal (works with non-Send types)
    let stop_flag = Arc::new(AtomicBool::new(false));
    let stop_flag_clone = stop_flag.clone();

    // Bridge the capture-owned channel to the worker's stop flag.
    tokio::spawn(async move {
        rx.recv().await;
        stop_flag_clone.store(true, Ordering::Relaxed);
    });

    // Spawn capture task on a dedicated thread (WASAPI COM objects are not Send)
    // All WASAPI objects must be created and used on the same thread
    thread::spawn(move || {
        let _completion = super::capture_worker_completion(done_tx);
        // Initialize COM for this thread
        unsafe {
            let hr = CoInitializeEx(None, COINIT_MULTITHREADED);
            if hr.is_err() {
                super::record_windows_capture_error(
                    &error_arc,
                    format!("Failed to initialize COM: {:?}", hr),
                );
                return;
            }
        }

        // Ensure COM is uninitialized when thread exits
        let _com_guard = scopeguard::guard((), |_| unsafe {
            CoUninitialize();
        });

        // Initialize WASAPI on this thread
        let device = match DeviceEnumerator::new()
            .and_then(|enumerator| enumerator.get_default_device(&Direction::Render))
        {
            Ok(d) => d,
            Err(e) => {
                let error_msg = format!("Failed to get audio device: {}", e);
                eprintln!("{}", error_msg);
                *error_arc.lock().unwrap() = Some(error_msg);
                return;
            }
        };

        let mut audio_client = match device.get_iaudioclient() {
            Ok(client) => client,
            Err(e) => {
                let error_msg = format!("Failed to get audio client: {}", e);
                eprintln!("{}", error_msg);
                *error_arc.lock().unwrap() = Some(error_msg);
                return;
            }
        };

        let mix_format = match audio_client.get_mixformat() {
            Ok(format) => format,
            Err(e) => {
                let error_msg = format!("Failed to get mix format: {}", e);
                eprintln!("{}", error_msg);
                *error_arc.lock().unwrap() = Some(error_msg);
                return;
            }
        };

        // Set sample rate and channels
        let channels = mix_format.get_nchannels() as usize;
        let bits = mix_format.get_bitspersample();
        let sample_kind = match mix_format.get_subformat() {
            Ok(SampleType::Float) => super::windows_pcm::SampleKind::Float,
            Ok(SampleType::Int) => super::windows_pcm::SampleKind::Integer,
            Err(error) => {
                super::record_windows_capture_error(
                    &error_arc,
                    format!("Unsupported capture format: {error}"),
                );
                return;
            }
        };
        let bytes_per_sample = match super::windows_pcm::validate_format(
            channels,
            bits,
            mix_format.get_validbitspersample(),
            mix_format.get_blockalign() as usize,
            sample_kind,
        ) {
            Ok(width) => width,
            Err(error) => {
                super::record_windows_capture_error(&error_arc, error);
                return;
            }
        };
        *sample_rate_arc.lock().unwrap() = mix_format.get_samplespersec();
        *channels_arc.lock().unwrap() = mix_format.get_nchannels();

        // Get device period
        let (_def_period, min_period) = match audio_client.get_device_period() {
            Ok(periods) => periods,
            Err(e) => {
                super::record_windows_capture_error(
                    &error_arc,
                    format!("Failed to get device period: {}", e),
                );
                return;
            }
        };

        // Initialize audio client for loopback with StreamMode
        // For loopback mode: get Render device, initialize with Capture direction
        // This triggers AUDCLNT_STREAMFLAGS_LOOPBACK in the wasapi crate
        let stream_mode = StreamMode::EventsShared {
            autoconvert: true,               // Enable automatic format conversion
            buffer_duration_hns: min_period, // Use minimum period
        };

        if let Err(e) =
            audio_client.initialize_client(&mix_format, &Direction::Capture, &stream_mode)
        {
            let error_msg = format!("Failed to initialize audio client: {}", e);
            eprintln!("{}", error_msg);
            *error_arc.lock().unwrap() = Some(error_msg);
            return;
        }

        // Set up event handle for EventsShared mode
        let h_event = match audio_client.set_get_eventhandle() {
            Ok(event) => event,
            Err(e) => {
                super::record_windows_capture_error(
                    &error_arc,
                    format!("Failed to set event handle: {}", e),
                );
                return;
            }
        };

        let capture_client = match audio_client.get_audiocaptureclient() {
            Ok(client) => client,
            Err(e) => {
                let error_msg = format!("Failed to get capture client: {}", e);
                eprintln!("{}", error_msg);
                *error_arc.lock().unwrap() = Some(error_msg);
                return;
            }
        };

        if let Err(e) = audio_client.start_stream() {
            let error_msg = format!("Failed to start stream: {}", e);
            eprintln!("{}", error_msg);
            *error_arc.lock().unwrap() = Some(error_msg);
            return;
        }

        loop {
            // Check if stop signal was received
            if stop_flag.load(Ordering::Relaxed) {
                break;
            }

            // Try to get available data
            match capture_client.get_next_packet_size() {
                Ok(Some(frames_available)) => {
                    if frames_available > 0 {
                        // Calculate buffer size needed (frames * channels * bytes_per_sample)
                        let Some(buffer_size) = (frames_available as usize)
                            .checked_mul(channels)
                            .and_then(|value| value.checked_mul(bytes_per_sample))
                        else {
                            super::record_windows_capture_error(
                                &error_arc,
                                "Capture packet is too large".into(),
                            );
                            break;
                        };
                        let mut buffer = Vec::new();
                        if buffer.try_reserve_exact(buffer_size).is_err() {
                            super::record_windows_capture_error(
                                &error_arc,
                                "Capture packet is too large".into(),
                            );
                            break;
                        }
                        buffer.resize(buffer_size, 0);
                        match capture_client.read_from_device(&mut buffer) {
                            Ok((frames_read, buffer_info)) => {
                                if frames_read > 0 {
                                    match super::windows_pcm::decode_packet(
                                        &buffer,
                                        frames_read as usize,
                                        channels,
                                        bits,
                                        sample_kind,
                                        buffer_info.flags.silent,
                                    ) {
                                        Ok(decoded) => samples.lock().unwrap().extend(decoded),
                                        Err(error) => {
                                            super::record_windows_capture_error(&error_arc, error);
                                            break;
                                        }
                                    }
                                }
                            }
                            Err(e) => {
                                super::record_windows_capture_error(
                                    &error_arc,
                                    format!("Error reading from device: {}", e),
                                );
                                break;
                            }
                        }
                    }
                }
                Ok(None) => {
                    // Exclusive mode - handle differently if needed
                }
                Err(e) => {
                    super::record_windows_capture_error(
                        &error_arc,
                        format!("Error getting next packet size: {}", e),
                    );
                    break;
                }
            }

            // Wait for event signal (with timeout to allow checking stop flag)
            if h_event.wait_for_event(100).is_err() {
                // Timeout is expected - just continue to check stop flag
            }
        }

        // Stop the stream when done
        if let Err(e) = audio_client.stop_stream() {
            super::record_windows_capture_error(
                &error_arc,
                format!("Failed to stop audio stream: {}", e),
            );
        }
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
    #[cfg(target_os = "windows")]
    {
        true
    }
    #[cfg(not(target_os = "windows"))]
    {
        false
    }
}
