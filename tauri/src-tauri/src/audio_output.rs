use crate::audio_pcm::{
    convert_channels, resample_interleaved, PlaybackControls, RenderCursor, RenderPosition,
};
use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use cpal::{Device, SampleFormat, StreamConfig};
use std::collections::{HashMap, HashSet};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{mpsc, Arc, Mutex};
use std::time::{Duration, Instant};
use tauri::Emitter;

#[derive(Debug, Clone, serde::Serialize)]
pub struct AudioOutputDevice {
    pub id: String,
    pub name: String,
    pub is_default: bool,
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct PlaybackSession {
    pub session_id: String,
    pub duration: f64,
    pub position: f64,
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct PlaybackEvent {
    pub session_id: String,
    pub state: &'static str,
    pub position: f64,
    pub duration: f64,
    pub error: Option<String>,
}

#[derive(Debug, serde::Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct PlaybackOptions {
    #[serde(default)]
    pub start_time: f64,
    #[serde(default = "default_volume")]
    pub volume: f32,
}
fn default_volume() -> f32 {
    1.0
}
impl Default for PlaybackOptions {
    fn default() -> Self {
        Self {
            start_time: 0.0,
            volume: 1.0,
        }
    }
}

#[derive(Debug, Default, serde::Deserialize)]
pub struct PlaybackControl {
    pub paused: Option<bool>,
    pub position: Option<f64>,
    pub volume: Option<f32>,
}

struct SessionHandle {
    id: String,
    controls: Arc<PlaybackControls>,
    wake: mpsc::Sender<()>,
}

/// CPAL streams are created and retained by their owner thread. The Tauri
/// command awaits only a startup acknowledgement, never the clip's duration.
pub struct AudioOutputState {
    active: Arc<Mutex<Option<SessionHandle>>>,
    next_id: AtomicU64,
}
impl AudioOutputState {
    pub fn new() -> Self {
        Self {
            active: Arc::new(Mutex::new(None)),
            next_id: AtomicU64::new(1),
        }
    }

    pub fn stop_playback(&self, session_id: Option<&str>) -> Result<(), String> {
        let active = self
            .active
            .lock()
            .map_err(|_| "Audio session state unavailable")?;
        if let Some(session) = active.as_ref() {
            // A late stop for an old player must not stop its replacement.
            if session_id.is_none() || session_id == Some(session.id.as_str()) {
                session.controls.stop();
                let _ = session.wake.send(());
            }
        }
        Ok(())
    }

    pub fn control_playback(
        &self,
        session_id: &str,
        control: PlaybackControl,
    ) -> Result<(), String> {
        if control
            .position
            .is_some_and(|value| !value.is_finite() || value < 0.0)
            || control
                .volume
                .is_some_and(|value| !value.is_finite() || !(0.0..=1.0).contains(&value))
        {
            return Err("Invalid playback position or volume".into());
        }
        let active = self
            .active
            .lock()
            .map_err(|_| "Audio session state unavailable")?;
        let session = active
            .as_ref()
            .filter(|session| {
                session.id == session_id && !session.controls.stopped.load(Ordering::Acquire)
            })
            .ok_or("Playback session has ended")?;
        if let Some(position) = control.position {
            session.controls.seek(position);
        }
        if let Some(volume) = control.volume {
            session.controls.set_volume(volume);
        }
        if let Some(paused) = control.paused {
            session.controls.paused.store(paused, Ordering::Release);
        }
        let _ = session.wake.send(());
        Ok(())
    }

    pub fn list_output_devices(&self) -> Result<Vec<AudioOutputDevice>, String> {
        let host = cpal::default_host();
        let default_name = host
            .default_output_device()
            .and_then(|device| device.name().ok());
        host.output_devices()
            .map_err(|error| format!("Failed to enumerate output devices: {error}"))?
            .map(|device| {
                let name = device
                    .name()
                    .map_err(|error| format!("Failed to get output name: {error}"))?;
                Ok(AudioOutputDevice {
                    id: device_id(&name),
                    is_default: default_name.as_ref() == Some(&name),
                    name,
                })
            })
            .collect()
    }

    pub async fn play_audio_to_devices(
        &self,
        app: tauri::AppHandle,
        audio_data: Vec<u8>,
        device_ids: Vec<String>,
        options: Option<PlaybackOptions>,
    ) -> Result<PlaybackSession, String> {
        let options = options.unwrap_or_default();
        if !options.start_time.is_finite()
            || options.start_time < 0.0
            || !options.volume.is_finite()
            || !(0.0..=1.0).contains(&options.volume)
        {
            return Err("Invalid playback position or volume".into());
        }
        if device_ids.is_empty() {
            return Err("No output devices selected".into());
        }
        let id = format!("native-{}", self.next_id.fetch_add(1, Ordering::Relaxed));
        let controls = Arc::new(PlaybackControls::new(options.start_time, options.volume));
        let (wake, wake_receiver) = mpsc::channel();
        {
            let mut active = self
                .active
                .lock()
                .map_err(|_| "Audio session state unavailable")?;
            if let Some(previous) = active.take() {
                previous.controls.stop();
                let _ = previous.wake.send(());
            }
            *active = Some(SessionHandle {
                id: id.clone(),
                controls: controls.clone(),
                wake,
            });
        }
        let (ready_sender, ready_receiver) = tokio::sync::oneshot::channel();
        let active = self.active.clone();
        let worker_id = id.clone();
        let worker_controls = controls.clone();
        let spawned = std::thread::Builder::new()
            .name(format!("voicebox-{id}"))
            .spawn(move || {
                let mut ready = Some(ready_sender);
                if let Err(error) = run_session(
                    &app,
                    &worker_id,
                    audio_data,
                    device_ids,
                    worker_controls.clone(),
                    wake_receiver,
                    &mut ready,
                ) {
                    if let Some(sender) = ready.take() {
                        let _ = sender.send(Err(error.clone()));
                    }
                    let stopped = worker_controls.stopped.load(Ordering::Acquire);
                    emit(
                        &app,
                        &worker_id,
                        if stopped { "stopped" } else { "error" },
                        0.0,
                        0.0,
                        if stopped { None } else { Some(error) },
                    );
                }
                if let Ok(mut current) = active.lock() {
                    if current
                        .as_ref()
                        .is_some_and(|session| session.id == worker_id)
                    {
                        current.take();
                    }
                }
            });
        if let Err(error) = spawned {
            controls.stop();
            if let Ok(mut active) = self.active.lock() {
                if active.as_ref().is_some_and(|session| session.id == id) {
                    active.take();
                }
            }
            return Err(format!("Unable to start audio playback: {error}"));
        }
        ready_receiver
            .await
            .map_err(|_| "Audio playback worker exited during startup".to_string())?
    }
}
impl Default for AudioOutputState {
    fn default() -> Self {
        Self::new()
    }
}
impl Drop for AudioOutputState {
    fn drop(&mut self) {
        let _ = self.stop_playback(None);
    }
}
fn device_id(name: &str) -> String {
    format!("device_{}", name.replace(' ', "_").to_lowercase())
}
fn emit(
    app: &tauri::AppHandle,
    id: &str,
    state: &'static str,
    position: f64,
    duration: f64,
    error: Option<String>,
) {
    let _ = app.emit(
        "native-audio-playback",
        PlaybackEvent {
            session_id: id.into(),
            state,
            position,
            duration,
            error,
        },
    );
}

fn decode_audio(data: Vec<u8>) -> Result<(Vec<f32>, u32, usize), String> {
    use symphonia::core::audio::SampleBuffer;
    use symphonia::core::errors::Error;
    use symphonia::core::io::MediaSourceStream;
    let input = MediaSourceStream::new(Box::new(std::io::Cursor::new(data)), Default::default());
    let mut format = symphonia::default::get_probe()
        .format(
            &Default::default(),
            input,
            &Default::default(),
            &Default::default(),
        )
        .map_err(|error| format!("Unable to read audio: {error}"))?
        .format;
    let track = format.default_track().ok_or("No audio track found")?;
    let track_id = track.id;
    let rate = track
        .codec_params
        .sample_rate
        .ok_or("Audio sample rate is missing")?;
    let channels = track
        .codec_params
        .channels
        .ok_or("Audio channel layout is missing")?
        .count();
    if rate == 0 || channels == 0 {
        return Err("Invalid audio sample rate or channels".into());
    }
    let mut decoder = symphonia::default::get_codecs()
        .make(&track.codec_params, &Default::default())
        .map_err(|error| format!("Unable to decode audio: {error}"))?;
    let mut samples = Vec::new();
    loop {
        let packet = match format.next_packet() {
            Ok(packet) => packet,
            Err(Error::IoError(error)) if error.kind() == std::io::ErrorKind::UnexpectedEof => {
                break
            }
            Err(error) => return Err(format!("Unable to read audio packet: {error}")),
        };
        if packet.track_id() != track_id {
            continue;
        }
        let decoded = decoder
            .decode(&packet)
            .map_err(|error| format!("Unable to decode audio packet: {error}"))?;
        if decoded.spec().rate != rate || decoded.spec().channels.count() != channels {
            return Err("Audio format changes within the file".into());
        }
        let mut buffer = SampleBuffer::<f32>::new(decoded.capacity() as u64, *decoded.spec());
        buffer.copy_interleaved_ref(decoded);
        samples.extend_from_slice(buffer.samples());
    }
    if samples.is_empty() || samples.iter().any(|sample| !sample.is_finite()) {
        return Err("Audio contains no playable samples".into());
    }
    Ok((samples, rate, channels))
}

fn build_stream<T>(
    device: &Device,
    config: &StreamConfig,
    samples: Arc<[f32]>,
    controls: Arc<PlaybackControls>,
    position: Arc<RenderPosition>,
    errors: mpsc::Sender<String>,
) -> Result<cpal::Stream, String>
where
    T: cpal::SizedSample + cpal::FromSample<f32>,
{
    let mut cursor = RenderCursor::new();
    let rate = config.sample_rate.0;
    let channels = config.channels as usize;
    device
        .build_output_stream(
            config,
            move |output: &mut [T], _: &cpal::OutputCallbackInfo| {
                cursor.render(
                    output,
                    &samples,
                    rate,
                    channels,
                    &controls,
                    &position,
                    T::from_sample,
                );
            },
            move |error| {
                let _ = errors.send(error.to_string());
            },
            None,
        )
        .map_err(|error| format!("Unable to open output stream: {error}"))
}

fn run_session(
    app: &tauri::AppHandle,
    id: &str,
    data: Vec<u8>,
    ids: Vec<String>,
    controls: Arc<PlaybackControls>,
    wake: mpsc::Receiver<()>,
    ready: &mut Option<tokio::sync::oneshot::Sender<Result<PlaybackSession, String>>>,
) -> Result<(), String> {
    let (samples, rate, channels) = decode_audio(data)?;
    let duration = samples.len() as f64 / channels as f64 / rate as f64;
    let samples: Arc<[f32]> = samples.into();
    let mut requested: HashSet<String> = ids.into_iter().collect();
    let host = cpal::default_host();
    let devices: Vec<Device> = host
        .output_devices()
        .map_err(|error| format!("Unable to list outputs: {error}"))?
        .filter_map(|device| {
            let name = device.name().ok()?;
            requested.remove(&device_id(&name)).then_some(device)
        })
        .collect();
    if !requested.is_empty() {
        return Err(
            "A selected audio output is unavailable. Choose an available output and try again."
                .into(),
        );
    }
    let mut prepared: HashMap<(u32, u16), Arc<[f32]>> = HashMap::new();
    let mut streams = Vec::new();
    let mut positions = Vec::new();
    let (errors, error_receiver) = mpsc::channel();
    for device in devices {
        if controls.stopped.load(Ordering::Acquire) {
            return Err("Playback cancelled".into());
        }
        let supported = device
            .default_output_config()
            .map_err(|error| format!("Unable to configure output: {error}"))?;
        let config: StreamConfig = supported.clone().into();
        let key = (config.sample_rate.0, config.channels);
        let pcm = if let Some(pcm) = prepared.get(&key) {
            pcm.clone()
        } else {
            let pcm: Arc<[f32]> = if key == (rate, channels as u16) {
                samples.clone()
            } else {
                convert_channels(
                    &resample_interleaved(&samples, rate, key.0, channels)?,
                    channels,
                    key.1 as usize,
                )?
                .into()
            };
            prepared.insert(key, pcm.clone());
            pcm
        };
        let frames = pcm.len() / config.channels as usize;
        let position = Arc::new(RenderPosition::default());
        let stream = match supported.sample_format() {
            SampleFormat::F32 => build_stream::<f32>(
                &device,
                &config,
                pcm,
                controls.clone(),
                position.clone(),
                errors.clone(),
            ),
            SampleFormat::I16 => build_stream::<i16>(
                &device,
                &config,
                pcm,
                controls.clone(),
                position.clone(),
                errors.clone(),
            ),
            SampleFormat::U16 => build_stream::<u16>(
                &device,
                &config,
                pcm,
                controls.clone(),
                position.clone(),
                errors.clone(),
            ),
            format => Err(format!("Unsupported audio output format: {format:?}")),
        }?;
        streams.push(stream);
        positions.push((position, frames, config.sample_rate.0));
    }
    if streams.is_empty() {
        return Err("No output devices selected".into());
    }
    // Build all streams first; callbacks remain silent behind the shared pause
    // gate until every stream has started. Hardware latency may differ by device.
    for stream in &streams {
        stream
            .play()
            .map_err(|error| format!("Unable to start output: {error}"))?;
    }
    if controls.stopped.load(Ordering::Acquire) {
        return Err("Playback cancelled".into());
    }
    controls.paused.store(false, Ordering::Release);
    let start_position = controls.requested_position().min(duration);
    if let Some(sender) = ready.take() {
        if sender
            .send(Ok(PlaybackSession {
                session_id: id.into(),
                duration,
                position: start_position,
            }))
            .is_err()
        {
            controls.stop();
        }
    }
    emit(app, id, "playing", start_position, duration, None);
    let mut last_event = Instant::now();
    loop {
        let command = wake.recv_timeout(Duration::from_millis(20)).is_ok();
        let position = if positions[0].0.revision.load(Ordering::Acquire) == controls.revision() {
            positions[0].0.frame.load(Ordering::Acquire) as f64 / positions[0].2 as f64
        } else {
            controls.requested_position().min(duration)
        };
        if controls.stopped.load(Ordering::Acquire) {
            emit(app, id, "stopped", position, duration, None);
            break;
        }
        if let Ok(error) = error_receiver.try_recv() {
            return Err(format!("Audio output failed: {error}"));
        }
        if positions.iter().all(|(position, frames, _)| {
            position.frame.load(Ordering::Acquire) >= *frames
                && position.revision.load(Ordering::Acquire) == controls.revision()
        }) {
            emit(app, id, "ended", duration, duration, None);
            break;
        }
        if command || last_event.elapsed() >= Duration::from_millis(100) {
            emit(
                app,
                id,
                if controls.paused.load(Ordering::Acquire) {
                    "paused"
                } else {
                    "playing"
                },
                position.min(duration),
                duration,
                None,
            );
            last_event = Instant::now();
        }
    }
    drop(streams);
    Ok(())
}
