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

/// Keep audio binary across IPC; only the small routing metadata is JSON.
pub fn read_playback_request(
    body: &tauri::ipc::InvokeBody,
    metadata: Option<&str>,
) -> Result<(Vec<u8>, Vec<String>, Option<PlaybackOptions>), String> {
    #[derive(serde::Deserialize)]
    #[serde(rename_all = "camelCase")]
    struct Metadata {
        device_ids: Vec<String>,
        options: Option<PlaybackOptions>,
    }
    let metadata = metadata
        .filter(|value| value.len() <= 16 * 1024)
        .ok_or("Missing or oversized playback metadata")?;
    let metadata: Metadata =
        serde_json::from_str(metadata).map_err(|_| "Invalid playback metadata")?;
    let tauri::ipc::InvokeBody::Raw(data) = body else {
        return Err("Playback audio must use a binary request body".into());
    };
    Ok((data.clone(), metadata.device_ids, metadata.options))
}

#[derive(Debug, Default, serde::Deserialize)]
pub struct PlaybackControl {
    pub paused: Option<bool>,
    pub position: Option<f64>,
    pub volume: Option<f32>,
}

type StreamQueues = Arc<Mutex<Vec<Arc<Mutex<crate::audio_stream::PcmQueue>>>>>;

struct SessionHandle {
    id: String,
    controls: Arc<PlaybackControls>,
    wake: mpsc::Sender<()>,
    streaming: Option<StreamQueues>,
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
        if control.position.is_some() && session.streaming.is_some() {
            return Err("Speech preview cannot seek".into());
        }
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

    /// Start a continuous output using the same process-wide owner as clips.
    pub async fn start_pcm_stream(
        &self,
        app: tauri::AppHandle,
        rate: u32,
        ids: Vec<String>,
    ) -> Result<PlaybackSession, String> {
        crate::audio_stream::PcmQueue::new(rate)?;
        let id = format!("native-{}", self.next_id.fetch_add(1, Ordering::Relaxed));
        let controls = Arc::new(PlaybackControls::new(0.0, 1.0));
        let queues: StreamQueues = Arc::new(Mutex::new(Vec::new()));
        let (wake, receiver) = mpsc::channel();
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
                streaming: Some(queues.clone()),
            });
        }
        let (sender, ready) = tokio::sync::oneshot::channel();
        let active = self.active.clone();
        let worker_id = id.clone();
        let worker_controls = controls.clone();
        let spawned = std::thread::Builder::new()
            .name(format!("voicebox-{id}"))
            .spawn(move || {
                let mut ready = Some(sender);
                if let Err(error) = run_pcm_session(
                    &app,
                    &worker_id,
                    rate,
                    ids,
                    worker_controls.clone(),
                    queues,
                    receiver,
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
                worker_controls.stop();
                if let Ok(mut active) = active.lock() {
                    if active
                        .as_ref()
                        .is_some_and(|current| current.id == worker_id)
                    {
                        active.take();
                    }
                }
            });
        if let Err(error) = spawned {
            controls.stop();
            self.stop_playback(Some(&id))?;
            return Err(format!("Unable to start preview output: {error}"));
        }
        ready
            .await
            .map_err(|_| "Preview worker exited during startup".to_string())?
    }

    fn stream_queues(&self, id: &str) -> Result<(StreamQueues, Arc<PlaybackControls>), String> {
        let active = self
            .active
            .lock()
            .map_err(|_| "Audio session state unavailable")?;
        let session = active
            .as_ref()
            .filter(|session| session.id == id && !session.controls.stopped.load(Ordering::Acquire))
            .ok_or("Preview session has ended")?;
        Ok((
            session.streaming.clone().ok_or("Not a preview session")?,
            session.controls.clone(),
        ))
    }

    /// Backpressure is async and bounded. No audio callback waits on IPC.
    pub async fn append_pcm_stream(&self, id: &str, pcm: Vec<u8>) -> Result<(), String> {
        if pcm.is_empty() || pcm.len() % 2 != 0 || pcm.len() > 8192 {
            return Err("Invalid PCM preview block".into());
        }
        let samples: Vec<f32> = pcm
            .chunks_exact(2)
            .map(|bytes| i16::from_le_bytes([bytes[0], bytes[1]]) as f32 / 32768.0)
            .collect();
        let (queues, controls) = self.stream_queues(id)?;
        let start = Instant::now();
        loop {
            if controls.stopped.load(Ordering::Acquire) {
                return Err("Preview session has ended".into());
            }
            {
                let queues = queues.lock().map_err(|_| "Preview queue unavailable")?;
                let mut locked = queues
                    .iter()
                    .map(|queue| queue.lock().map_err(|_| "Preview queue unavailable"))
                    .collect::<Result<Vec<_>, _>>()?;
                if locked.iter().any(|queue| queue.finished) {
                    return Err("Preview already finished".into());
                }
                if !locked.is_empty() && locked.iter().all(|queue| queue.can_append(samples.len()))
                {
                    for queue in &mut locked {
                        queue.append(&samples)?;
                    }
                    return Ok(());
                }
            }
            if start.elapsed() > Duration::from_secs(15) {
                return Err("Audio output is not consuming the preview".into());
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    }

    pub fn finish_pcm_stream(&self, id: &str) -> Result<(), String> {
        let (queues, _) = self.stream_queues(id)?;
        let queues = queues.lock().map_err(|_| "Preview queue unavailable")?;
        for queue in queues.iter() {
            queue
                .lock()
                .map_err(|_| "Preview queue unavailable")?
                .finished = true;
        }
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
                streaming: None,
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

fn selected_device_indices(names: &[String], ids: &[String]) -> Result<Vec<usize>, String> {
    let requested: HashSet<&str> = ids.iter().map(String::as_str).collect();
    let mut found = HashSet::new();
    let mut selected = Vec::new();
    for (index, name) in names.iter().enumerate() {
        let id = device_id(name);
        if requested.contains(id.as_str()) {
            if !found.insert(id) {
                return Err("Multiple audio outputs share the same identifier. Rename or disconnect one and try again.".into());
            }
            selected.push(index);
        }
    }
    if found.len() != requested.len() {
        return Err(
            "A selected audio output is unavailable. Choose an available output and try again."
                .into(),
        );
    }
    Ok(selected)
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
    // Symphonia treats an unexpected EOF as the end of a stream. For RIFF
    // WAVE files with a known container size that can silently play only a
    // prefix of a damaged recording, so verify its declared length first.
    if data.len() >= 12 && &data[..4] == b"RIFF" && &data[8..12] == b"WAVE" {
        let declared = u32::from_le_bytes(data[4..8].try_into().unwrap());
        // Some streaming encoders use the sentinel because the length was
        // unknown at the time the header was written.
        if declared != u32::MAX && u64::from(declared) + 8 > data.len() as u64 {
            return Err("Audio file is truncated".into());
        }
    }
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
    let host = cpal::default_host();
    let devices: Vec<(Device, String)> = host
        .output_devices()
        .map_err(|error| format!("Unable to list outputs: {error}"))?
        .filter_map(|device| device.name().ok().map(|name| (device, name)))
        .collect();
    let names = devices
        .iter()
        .map(|(_, name)| name.clone())
        .collect::<Vec<_>>();
    let selected = selected_device_indices(&names, &ids)?
        .into_iter()
        .collect::<HashSet<_>>();
    let mut prepared: HashMap<(u32, u16), Arc<[f32]>> = HashMap::new();
    let mut streams = Vec::new();
    let mut positions = Vec::new();
    let (errors, error_receiver) = mpsc::channel();
    for (index, (device, _)) in devices.into_iter().enumerate() {
        if !selected.contains(&index) {
            continue;
        }
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

fn build_pcm_stream<T>(
    device: &Device,
    config: &StreamConfig,
    queue: Arc<Mutex<crate::audio_stream::PcmQueue>>,
    controls: Arc<PlaybackControls>,
    errors: mpsc::Sender<String>,
) -> Result<cpal::Stream, String>
where
    T: cpal::SizedSample + cpal::FromSample<f32>,
{
    let rate = config.sample_rate.0;
    let channels = config.channels as usize;
    device
        .build_output_stream(
            config,
            move |output: &mut [T], _: &cpal::OutputCallbackInfo| {
                output.fill(T::from_sample(0.0));
                if controls.stopped.load(Ordering::Acquire)
                    || controls.paused.load(Ordering::Acquire)
                {
                    return;
                }
                if let Ok(mut queue) = queue.try_lock() {
                    queue.render(output, rate, channels, T::from_sample);
                }
            },
            move |error| {
                let _ = errors.send(error.to_string());
            },
            None,
        )
        .map_err(|error| format!("Unable to open preview output: {error}"))
}

fn run_pcm_session(
    app: &tauri::AppHandle,
    id: &str,
    rate: u32,
    ids: Vec<String>,
    controls: Arc<PlaybackControls>,
    queues: StreamQueues,
    wake: mpsc::Receiver<()>,
    ready: &mut Option<tokio::sync::oneshot::Sender<Result<PlaybackSession, String>>>,
) -> Result<(), String> {
    let host = cpal::default_host();
    let devices: Vec<Device> = if ids.is_empty() {
        vec![host
            .default_output_device()
            .ok_or("No default audio output is available")?]
    } else {
        let devices: Vec<Device> = host
            .output_devices()
            .map_err(|error| error.to_string())?
            .collect();
        let names = devices
            .iter()
            .map(|device| device.name().map_err(|error| error.to_string()))
            .collect::<Result<Vec<_>, _>>()?;
        let selected: HashSet<usize> = selected_device_indices(&names, &ids)?.into_iter().collect();
        devices
            .into_iter()
            .enumerate()
            .filter_map(|(index, device)| selected.contains(&index).then_some(device))
            .collect()
    };
    let output_names = devices
        .iter()
        .map(|device| device.name().map_err(|error| error.to_string()))
        .collect::<Result<Vec<_>, _>>()?;
    let default_route = ids.is_empty();
    let mut streams = Vec::new();
    let (errors, receiver) = mpsc::channel();
    for device in devices {
        if controls.stopped.load(Ordering::Acquire) {
            return Err("Preview cancelled".into());
        }
        let supported = device
            .default_output_config()
            .map_err(|error| error.to_string())?;
        let config: StreamConfig = supported.clone().into();
        let queue = Arc::new(Mutex::new(crate::audio_stream::PcmQueue::new(rate)?));
        let stream = match supported.sample_format() {
            SampleFormat::F32 => build_pcm_stream::<f32>(
                &device,
                &config,
                queue.clone(),
                controls.clone(),
                errors.clone(),
            ),
            SampleFormat::I16 => build_pcm_stream::<i16>(
                &device,
                &config,
                queue.clone(),
                controls.clone(),
                errors.clone(),
            ),
            SampleFormat::U16 => build_pcm_stream::<u16>(
                &device,
                &config,
                queue.clone(),
                controls.clone(),
                errors.clone(),
            ),
            format => Err(format!("Unsupported output format: {format:?}")),
        }?;
        queues
            .lock()
            .map_err(|_| "Preview queue unavailable")?
            .push(queue);
        streams.push(stream);
    }
    for stream in &streams {
        stream.play().map_err(|error| error.to_string())?;
    }
    if controls.stopped.load(Ordering::Acquire) {
        return Err("Preview cancelled".into());
    }
    controls.paused.store(false, Ordering::Release);
    if let Some(sender) = ready.take() {
        if sender
            .send(Ok(PlaybackSession {
                session_id: id.into(),
                duration: 0.0,
                position: 0.0,
            }))
            .is_err()
        {
            controls.stop();
        }
    }
    let mut last_event = Instant::now();
    let mut last_device_check = Instant::now();
    loop {
        let _ = wake.recv_timeout(Duration::from_millis(20));
        if last_device_check.elapsed() >= Duration::from_secs(1) {
            if default_route {
                let current = host
                    .default_output_device()
                    .and_then(|device| device.name().ok());
                if current.as_ref() != output_names.first() {
                    return Err("Audio output changed; preview stopped".into());
                }
            } else {
                let available = host
                    .output_devices()
                    .map_err(|error| error.to_string())?
                    .filter_map(|device| device.name().ok())
                    .collect::<Vec<_>>();
                if output_names.iter().any(|name| !available.contains(name)) {
                    return Err("A selected audio output disconnected".into());
                }
            }
            last_device_check = Instant::now();
        }
        let (position, ended) = {
            let queues = queues.lock().map_err(|_| "Preview queue unavailable")?;
            let locked = queues
                .iter()
                .map(|queue| queue.lock().map_err(|_| "Preview queue unavailable"))
                .collect::<Result<Vec<_>, _>>()?;
            (
                locked.first().map_or(0.0, |queue| queue.position()),
                locked.iter().all(|queue| queue.ended()),
            )
        };
        if controls.stopped.load(Ordering::Acquire) {
            emit(app, id, "stopped", position, 0.0, None);
            break;
        }
        if let Ok(error) = receiver.try_recv() {
            return Err(format!("Audio output disconnected: {error}"));
        }
        if ended {
            emit(app, id, "ended", position, position, None);
            break;
        }
        if last_event.elapsed() >= Duration::from_millis(100) {
            emit(app, id, "playing", position, 0.0, None);
            last_event = Instant::now();
        }
    }
    drop(streams);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn binary_playback_preserves_bytes_and_routing_metadata() {
        let body = tauri::ipc::InvokeBody::Raw(vec![0, 127, 255]);
        let (data, devices, options) = read_playback_request(
            &body,
            Some(r#"{"deviceIds":["device-a"],"options":{"startTime":1.5,"volume":0.25}}"#),
        )
        .unwrap();
        assert_eq!(data, vec![0, 127, 255]);
        assert_eq!(devices, vec!["device-a"]);
        let options = options.unwrap();
        assert_eq!(options.start_time, 1.5);
        assert_eq!(options.volume, 0.25);
        assert!(read_playback_request(&body, None).is_err());
        assert!(read_playback_request(&body, Some("invalid")).is_err());
        assert!(read_playback_request(
            &tauri::ipc::InvokeBody::Json(serde_json::json!({})),
            Some(r#"{"deviceIds":[]}"#)
        )
        .is_err());
    }
    use std::io::Cursor;

    fn wav(samples: &[i16], channels: u16, rate: u32) -> Vec<u8> {
        let mut cursor = Cursor::new(Vec::new());
        {
            let spec = hound::WavSpec {
                channels,
                sample_rate: rate,
                bits_per_sample: 16,
                sample_format: hound::SampleFormat::Int,
            };
            let mut writer = hound::WavWriter::new(&mut cursor, spec).unwrap();
            for sample in samples {
                writer.write_sample(*sample).unwrap();
            }
            writer.finalize().unwrap();
        }
        cursor.into_inner()
    }

    #[test]
    fn decode_audio_preserves_stereo_frames_and_rejects_invalid_input() {
        let input = wav(&[0, 16384, -16384, 32767], 2, 24000);
        let (samples, rate, channels) = decode_audio(input).unwrap();
        assert_eq!((rate, channels, samples.len()), (24000, 2, 4));
        assert!((samples[1] - 0.5).abs() < 0.0001);
        assert!((samples[2] + 0.5).abs() < 0.0001);
        assert!(decode_audio(b"not audio".to_vec()).is_err());
        assert!(decode_audio(wav(&[], 1, 24000)).is_err());
    }

    #[test]
    fn truncated_wav_is_not_accepted_as_a_complete_clip() {
        let mut audio = wav(&vec![16384; 24000], 1, 24000);
        audio.truncate(audio.len() - 10000);
        assert!(decode_audio(audio).is_err());
    }

    #[test]
    fn wav_with_truncated_data_chunk_is_rejected_even_if_container_length_matches() {
        let mut audio = wav(&vec![16384; 24000], 1, 24000);
        audio.truncate(audio.len() - 10000);
        let size = (audio.len() - 8) as u32;
        audio[4..8].copy_from_slice(&size.to_le_bytes());
        assert!(decode_audio(audio).is_err());
    }

    #[test]
    fn decode_accepts_complete_wave_with_trailing_bytes_and_streaming_size() {
        let mut with_trailing_bytes = wav(&[1, 2, 3], 1, 16000);
        with_trailing_bytes.extend_from_slice(b"extra");
        assert_eq!(decode_audio(with_trailing_bytes).unwrap().0.len(), 3);
        let mut unknown_size = wav(&[1, 2, 3], 1, 16000);
        unknown_size[4..8].copy_from_slice(&u32::MAX.to_le_bytes());
        assert_eq!(decode_audio(unknown_size).unwrap().0.len(), 3);
    }

    #[test]
    fn decode_rejects_nonfinite_float_samples() {
        let mut cursor = Cursor::new(Vec::new());
        {
            let spec = hound::WavSpec {
                channels: 1,
                sample_rate: 24000,
                bits_per_sample: 32,
                sample_format: hound::SampleFormat::Float,
            };
            let mut writer = hound::WavWriter::new(&mut cursor, spec).unwrap();
            writer.write_sample(f32::NAN).unwrap();
            writer.finalize().unwrap();
        }
        assert!(decode_audio(cursor.into_inner()).is_err());
    }

    #[test]
    fn stale_controls_and_stops_do_not_touch_current_session() {
        let state = AudioOutputState::new();
        let controls = Arc::new(PlaybackControls::new(0.0, 1.0));
        let (wake, receiver) = mpsc::channel();
        *state.active.lock().unwrap() = Some(SessionHandle {
            id: "current".into(),
            controls: controls.clone(),
            wake,
            streaming: None,
        });
        assert!(state
            .control_playback(
                "old",
                PlaybackControl {
                    paused: Some(false),
                    position: None,
                    volume: None
                }
            )
            .is_err());
        state.stop_playback(Some("old")).unwrap();
        assert!(!controls.stopped.load(Ordering::Acquire));
        assert!(receiver.try_recv().is_err());
        state
            .control_playback(
                "current",
                PlaybackControl {
                    paused: Some(false),
                    position: Some(0.5),
                    volume: Some(0.25),
                },
            )
            .unwrap();
        assert_eq!(controls.requested_position(), 0.5);
        assert!(!controls.paused.load(Ordering::Acquire));
        assert!(receiver.try_recv().is_ok());
        state.stop_playback(Some("current")).unwrap();
        assert!(controls.stopped.load(Ordering::Acquire));
        assert!(state
            .control_playback(
                "current",
                PlaybackControl {
                    paused: None,
                    position: None,
                    volume: None
                }
            )
            .is_err());
    }

    #[test]
    fn invalid_controls_do_not_change_playback() {
        let state = AudioOutputState::new();
        let controls = Arc::new(PlaybackControls::new(0.25, 1.0));
        let (wake, receiver) = mpsc::channel();
        *state.active.lock().unwrap() = Some(SessionHandle {
            id: "current".into(),
            controls: controls.clone(),
            wake,
            streaming: None,
        });
        for control in [
            PlaybackControl {
                paused: Some(false),
                position: Some(f64::NAN),
                volume: None,
            },
            PlaybackControl {
                paused: Some(false),
                position: Some(-1.0),
                volume: None,
            },
            PlaybackControl {
                paused: Some(false),
                position: None,
                volume: Some(f32::INFINITY),
            },
            PlaybackControl {
                paused: Some(false),
                position: None,
                volume: Some(1.1),
            },
        ] {
            assert!(state.control_playback("current", control).is_err());
        }
        assert_eq!(controls.requested_position(), 0.25);
        assert!(controls.paused.load(Ordering::Acquire));
        assert!(receiver.try_recv().is_err());
    }

    #[test]
    fn stop_without_session_id_cancels_current_playback() {
        let state = AudioOutputState::new();
        let controls = Arc::new(PlaybackControls::new(0.0, 1.0));
        let (wake, receiver) = mpsc::channel();
        *state.active.lock().unwrap() = Some(SessionHandle {
            id: "current".into(),
            controls: controls.clone(),
            wake,
            streaming: None,
        });
        state.stop_playback(None).unwrap();
        assert!(controls.stopped.load(Ordering::Acquire));
        assert!(receiver.try_recv().is_ok());
    }

    #[test]
    fn selected_outputs_reject_ambiguous_device_identifiers() {
        let names: Vec<String> = vec!["USB Audio".into(), "USB_Audio".into(), "Speakers".into()];
        assert_eq!(device_id(&names[0]), device_id(&names[1]));
        assert!(selected_device_indices(&names, &[device_id(&names[0])]).is_err());
        assert_eq!(
            selected_device_indices(&names, &[device_id(&names[2])]).unwrap(),
            vec![2]
        );
        assert!(selected_device_indices(&names, &["device_missing".into()]).is_err());
        let unique: Vec<String> = vec!["Speakers".into(), "Headphones".into()];
        assert_eq!(
            selected_device_indices(&unique, &[device_id(&unique[1]), device_id(&unique[0])])
                .unwrap(),
            vec![0, 1]
        );
    }
    #[tokio::test]
    async fn preview_queue_rejects_stale_finish_and_append_and_stop_unblocks_backpressure() {
        let state = Arc::new(AudioOutputState::new());
        let controls = Arc::new(PlaybackControls::new(0.0, 1.0));
        let queue = Arc::new(Mutex::new(
            crate::audio_stream::PcmQueue::new(8000).unwrap(),
        ));
        let queues = Arc::new(Mutex::new(vec![queue.clone()]));
        let (wake, _receiver) = mpsc::channel();
        *state.active.lock().unwrap() = Some(SessionHandle {
            id: "current-preview".into(),
            controls: controls.clone(),
            wake,
            streaming: Some(queues),
        });
        assert!(state.append_pcm_stream("old", vec![0, 0]).await.is_err());
        assert!(state.finish_pcm_stream("old").is_err());
        assert!(state
            .append_pcm_stream("current-preview", vec![0])
            .await
            .is_err());
        assert!(state
            .append_pcm_stream("current-preview", vec![0; 8194])
            .await
            .is_err());
        state
            .append_pcm_stream("current-preview", vec![0; 8192])
            .await
            .unwrap();
        assert_eq!(queue.lock().unwrap().samples_len_for_test(), 4096);
        queue.lock().unwrap().append(&vec![0.25; 11904]).unwrap();
        let pending_state = state.clone();
        let pending = tokio::spawn(async move {
            pending_state
                .append_pcm_stream("current-preview", vec![0, 0])
                .await
        });
        tokio::time::sleep(Duration::from_millis(25)).await;
        assert!(!pending.is_finished());
        state.stop_playback(Some("old")).unwrap();
        assert!(!controls.stopped.load(Ordering::Acquire));
        for _ in 0..10 {
            state.stop_playback(Some("current-preview")).unwrap();
        }
        assert!(pending.await.unwrap().is_err());
        assert!(state.finish_pcm_stream("current-preview").is_err());
    }

    /// Opt-in host proof: uses the production CPAL callback, no webview/mock device.
    #[test]
    #[ignore = "plays sound on the host; run explicitly for native qualification"]
    fn preview_live_output_consumes_before_finish_and_recovers_from_underrun() {
        let host = cpal::default_host();
        let device = host.default_output_device().expect("No host output device");
        let supported = device.default_output_config().unwrap();
        let config: StreamConfig = supported.clone().into();
        let queue = Arc::new(Mutex::new(
            crate::audio_stream::PcmQueue::new(24000).unwrap(),
        ));
        let controls = Arc::new(PlaybackControls::new(0.0, 1.0));
        let (errors, receiver) = mpsc::channel();
        let stream = match supported.sample_format() {
            SampleFormat::F32 => {
                build_pcm_stream::<f32>(&device, &config, queue.clone(), controls.clone(), errors)
            }
            SampleFormat::I16 => {
                build_pcm_stream::<i16>(&device, &config, queue.clone(), controls.clone(), errors)
            }
            SampleFormat::U16 => {
                build_pcm_stream::<u16>(&device, &config, queue.clone(), controls.clone(), errors)
            }
            format => panic!("Unsupported output format: {format:?}"),
        }
        .unwrap();
        stream.play().unwrap();
        controls.paused.store(false, Ordering::Release);
        let tone: Vec<f32> = (0..2400)
            .map(|frame| (frame as f32 * 330.0 * std::f32::consts::TAU / 24000.0).sin() * 0.04)
            .collect();
        queue.lock().unwrap().append(&tone).unwrap();
        let start = Instant::now();
        while queue.lock().unwrap().position() == 0.0 && start.elapsed() < Duration::from_secs(2) {
            std::thread::sleep(Duration::from_millis(5));
        }
        assert!(
            queue.lock().unwrap().position() > 0.0,
            "No native callbacks consumed PCM"
        );
        assert!(!queue.lock().unwrap().finished);
        println!("NATIVE_CALLBACK_BEFORE_FINISH output={} source_rate=24000 output_rate={} first_callback_ms={}", device.name().unwrap(), config.sample_rate.0, start.elapsed().as_millis());
        std::thread::sleep(Duration::from_millis(175));
        assert!(
            !queue.lock().unwrap().ended(),
            "Underrun must not terminate output"
        );
        queue.lock().unwrap().append(&tone).unwrap();
        queue.lock().unwrap().finished = true;
        let deadline = Instant::now();
        while !queue.lock().unwrap().ended() && deadline.elapsed() < Duration::from_secs(2) {
            std::thread::sleep(Duration::from_millis(5));
        }
        assert!(queue.lock().unwrap().ended());
        assert!(receiver.try_recv().is_err());
        controls.stop();
        drop(stream);
    }
    /// Explicit local-backend qualification; neither contacts a default service
    /// nor creates or changes a voice profile. The caller provides a test profile.
    #[test]
    #[ignore = "requires a prepared local backend/profile and plays sound"]
    fn preview_live_http_reaches_native_callback_before_response_finishes() {
        use std::io::Read;
        let endpoint =
            std::env::var("VOICEBOX_TEST_PREVIEW_URL").expect("Provide local preview endpoint");
        assert!(
            endpoint.starts_with("http://127.0.0.1:"),
            "Runtime test accepts loopback only"
        );
        let body: serde_json::Value = serde_json::from_str(
            &std::env::var("VOICEBOX_TEST_PREVIEW_BODY").expect("Provide test profile request"),
        )
        .unwrap();
        assert_eq!(body["normalize"], false);
        assert_eq!(body["effects_chain"], serde_json::json!([]));
        let started = Instant::now();
        let mut response = reqwest::blocking::Client::new()
            .post(endpoint)
            .json(&body)
            .send()
            .unwrap()
            .error_for_status()
            .unwrap();
        let mut header = [0u8; 44];
        response.read_exact(&mut header).unwrap();
        assert_eq!(&header[..4], b"RIFF");
        assert_eq!(&header[8..16], b"WAVEfmt ");
        assert_eq!(u16::from_le_bytes(header[20..22].try_into().unwrap()), 1);
        assert_eq!(u16::from_le_bytes(header[22..24].try_into().unwrap()), 1);
        assert_eq!(u16::from_le_bytes(header[34..36].try_into().unwrap()), 16);
        let rate = u32::from_le_bytes(header[24..28].try_into().unwrap());
        let header_ms = started.elapsed().as_millis();
        let device = cpal::default_host()
            .default_output_device()
            .expect("No host output device");
        let supported = device.default_output_config().unwrap();
        let config: StreamConfig = supported.clone().into();
        let queue = Arc::new(Mutex::new(
            crate::audio_stream::PcmQueue::new(rate).unwrap(),
        ));
        let controls = Arc::new(PlaybackControls::new(0.0, 1.0));
        let state = AudioOutputState::new();
        let (wake, _receiver) = mpsc::channel();
        *state.active.lock().unwrap() = Some(SessionHandle {
            id: "live-http".into(),
            controls: controls.clone(),
            wake,
            streaming: Some(Arc::new(Mutex::new(vec![queue.clone()]))),
        });
        let (errors, error_receiver) = mpsc::channel();
        let stream = match supported.sample_format() {
            SampleFormat::F32 => {
                build_pcm_stream::<f32>(&device, &config, queue.clone(), controls.clone(), errors)
            }
            SampleFormat::I16 => {
                build_pcm_stream::<i16>(&device, &config, queue.clone(), controls.clone(), errors)
            }
            SampleFormat::U16 => {
                build_pcm_stream::<u16>(&device, &config, queue.clone(), controls.clone(), errors)
            }
            format => panic!("Unsupported output format: {format:?}"),
        }
        .unwrap();
        stream.play().unwrap();
        controls.paused.store(false, Ordering::Release);
        let runtime = tokio::runtime::Runtime::new().unwrap();
        let mut bytes = [0u8; 8192];
        let mut carry = Vec::new();
        let mut total = 0usize;
        let mut chunks = 0usize;
        let mut first_callback_ms = None;
        loop {
            let count = response.read(&mut bytes).unwrap();
            if count == 0 {
                break;
            }
            carry.extend_from_slice(&bytes[..count]);
            let count = (carry.len() / 2 * 2).min(8192);
            if count == 0 {
                continue;
            }
            let block = carry.drain(..count).collect::<Vec<_>>();
            total += block.len();
            chunks += 1;
            runtime
                .block_on(state.append_pcm_stream("live-http", block))
                .unwrap();
            if first_callback_ms.is_none() {
                let first = Instant::now();
                while queue.lock().unwrap().position() == 0.0
                    && first.elapsed() < Duration::from_secs(2)
                {
                    std::thread::sleep(Duration::from_millis(5));
                }
                if queue.lock().unwrap().position() > 0.0 {
                    first_callback_ms = Some(started.elapsed().as_millis());
                }
            }
        }
        assert!(carry.is_empty());
        assert!(total > 0);
        assert!(chunks > 1);
        assert!(!queue.lock().unwrap().finished);
        let response_ms = started.elapsed().as_millis();
        assert!(first_callback_ms.expect("No native callback before EOF") < response_ms);
        state.finish_pcm_stream("live-http").unwrap();
        let draining = Instant::now();
        while !queue.lock().unwrap().ended() && draining.elapsed() < Duration::from_secs(5) {
            std::thread::sleep(Duration::from_millis(10));
        }
        assert!(queue.lock().unwrap().ended());
        assert!(error_receiver.try_recv().is_err());
        state.stop_playback(Some("live-http")).unwrap();
        drop(stream);
        println!("LIVE_PREVIEW_HTTP_CPAl {{\"device\":{:?},\"source_rate\":{},\"device_rate\":{},\"header_ms\":{},\"first_callback_ms\":{},\"response_eof_ms\":{},\"pcm_bytes\":{},\"blocks\":{}}}", device.name().unwrap(), rate, config.sample_rate.0, header_ms, first_callback_ms.unwrap(), response_ms, total, chunks);
    }
}
