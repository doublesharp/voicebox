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

struct SessionCleanup {
    active: Arc<Mutex<Option<SessionHandle>>>,
    controls: Arc<PlaybackControls>,
    id: String,
}

impl SessionCleanup {
    fn new(
        active: Arc<Mutex<Option<SessionHandle>>>,
        controls: Arc<PlaybackControls>,
        id: String,
    ) -> Self {
        Self {
            active,
            controls,
            id,
        }
    }
}

impl Drop for SessionCleanup {
    fn drop(&mut self) {
        self.controls.stop();
        if let Ok(mut active) = self.active.lock() {
            if active.as_ref().is_some_and(|session| session.id == self.id) {
                active.take();
            }
        }
    }
}

fn execute_audio_worker(
    controls: &PlaybackControls,
    mut ready: Option<tokio::sync::oneshot::Sender<Result<PlaybackSession, String>>>,
    run: impl FnOnce(
        &mut Option<tokio::sync::oneshot::Sender<Result<PlaybackSession, String>>>,
    ) -> Result<(), String>,
    mut report: impl FnMut(&'static str, Option<String>),
) {
    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| run(&mut ready)))
        .unwrap_or_else(|_| Err("Audio output worker failed unexpectedly".into()));
    if let Err(error) = result {
        if let Some(sender) = ready.take() {
            let _ = sender.send(Err(error.clone()));
        }
        let stopped = controls.stopped.load(Ordering::Acquire);
        report(
            if stopped { "stopped" } else { "error" },
            if stopped { None } else { Some(error) },
        );
    }
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
    pub async fn start_pcm_stream<R: tauri::Runtime>(
        &self,
        app: tauri::AppHandle<R>,
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
        let cleanup = SessionCleanup::new(self.active.clone(), controls.clone(), id.clone());
        let worker_id = id.clone();
        let worker_controls = controls.clone();
        let spawned = std::thread::Builder::new()
            .name(format!("voicebox-{id}"))
            .spawn(move || {
                let _cleanup = cleanup;
                execute_audio_worker(
                    &worker_controls,
                    Some(sender),
                    |ready| {
                        run_pcm_session(
                            &app,
                            &worker_id,
                            rate,
                            ids,
                            worker_controls.clone(),
                            queues,
                            receiver,
                            ready,
                        )
                    },
                    |state, error| emit(&app, &worker_id, state, 0.0, 0.0, error),
                );
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

    pub async fn play_audio_to_devices<R: tauri::Runtime>(
        &self,
        app: tauri::AppHandle<R>,
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
        let cleanup = SessionCleanup::new(self.active.clone(), controls.clone(), id.clone());
        let worker_id = id.clone();
        let worker_controls = controls.clone();
        let spawned = std::thread::Builder::new()
            .name(format!("voicebox-{id}"))
            .spawn(move || {
                let _cleanup = cleanup;
                execute_audio_worker(
                    &worker_controls,
                    Some(ready_sender),
                    |ready| {
                        run_session(
                            &app,
                            &worker_id,
                            audio_data,
                            device_ids,
                            worker_controls.clone(),
                            wake_receiver,
                            ready,
                        )
                    },
                    |state, error| emit(&app, &worker_id, state, 0.0, 0.0, error),
                );
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
fn validate_default_output_route(
    expected: Option<&str>,
    current: Option<&str>,
) -> Result<(), String> {
    if expected.is_none() || expected != current {
        return Err("Audio output changed; preview stopped".into());
    }
    Ok(())
}

fn validate_selected_output_routes(
    selected: &[String],
    available: &[String],
) -> Result<(), String> {
    if selected.iter().any(|name| !available.contains(name)) {
        return Err("A selected audio output disconnected".into());
    }
    let ids: Vec<String> = selected.iter().map(|name| device_id(name)).collect();
    selected_device_indices(available, &ids)
        .map_err(|_| "A selected audio output is ambiguous; preview stopped".to_string())?;
    Ok(())
}

fn emit<R: tauri::Runtime>(
    app: &tauri::AppHandle<R>,
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

/// Reject partial PCM frames that a permissive WAV decoder would silently drop.
fn validate_wave_frame_alignment(data: &[u8]) -> Result<(), String> {
    let mut offset = 12usize;
    let mut pcm_block_align = None;
    while let Some(header) = data.get(offset..offset.saturating_add(8)) {
        let size = u32::from_le_bytes(header[4..8].try_into().unwrap());
        let body = offset + 8;
        if &header[..4] == b"data" {
            let available = data.len() - body;
            if size != u32::MAX && u64::from(size) > available as u64 {
                return Err("Audio file is truncated".into());
            }
            if let Some(align) = pcm_block_align {
                // A streaming data chunk extends to EOF when its size is unknown.
                let audio_bytes = if size == u32::MAX {
                    available as u64
                } else {
                    u64::from(size)
                };
                if audio_bytes % u64::from(align) != 0 {
                    return Err("WAV data ends in an incomplete audio frame".into());
                }
            }
            break;
        }
        let Some(end) = body.checked_add(size as usize) else {
            break;
        };
        let Some(chunk) = data.get(body..end) else {
            break;
        };
        if &header[..4] == b"fmt " && chunk.len() >= 16 {
            let format = u16::from_le_bytes([chunk[0], chunk[1]]);
            let align = u16::from_le_bytes([chunk[12], chunk[13]]);
            // WAVE_FORMAT_EXTENSIBLE stores the real format in a standard GUID.
            let subtype = if format == 0xfffe
                && chunk.len() >= 40
                && chunk[26..40] == [0, 0, 0, 0, 16, 0, 128, 0, 0, 170, 0, 56, 155, 113]
                && u16::from_le_bytes([chunk[16], chunk[17]]) >= 22
            {
                u16::from_le_bytes([chunk[24], chunk[25]])
            } else {
                format
            };
            if matches!(subtype, 1 | 3) {
                let channels = u16::from_le_bytes([chunk[2], chunk[3]]);
                let bits_per_sample = u16::from_le_bytes([chunk[14], chunk[15]]);
                let sample_bytes = u32::from(bits_per_sample).div_ceil(8);
                let expected = u32::from(channels) * sample_bytes;
                if expected == 0 || u32::from(align) != expected {
                    return Err("Invalid WAV PCM frame alignment".into());
                }
                pcm_block_align = Some(expected);
            }
        }
        let Some(next) = end.checked_add((size & 1) as usize) else {
            break;
        };
        offset = next;
    }
    Ok(())
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
        validate_wave_frame_alignment(&data)?;
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

fn prepare_output_pcm(
    samples: &Arc<[f32]>,
    rate: u32,
    channels: usize,
    key: (u32, u16),
    prepared: &mut HashMap<(u32, u16), Arc<[f32]>>,
) -> Result<Arc<[f32]>, String> {
    let source_channels = u16::try_from(channels).map_err(|_| "Invalid PCM channel count")?;
    if rate == 0
        || source_channels == 0
        || key.0 == 0
        || key.1 == 0
        || samples.len() % channels != 0
    {
        return Err("Invalid PCM sample rate, channel count, or incomplete frame".into());
    }
    if let Some(pcm) = prepared.get(&key) {
        return Ok(pcm.clone());
    }
    let pcm: Arc<[f32]> = if key == (rate, source_channels) {
        samples.clone()
    } else {
        convert_channels(
            &resample_interleaved(samples, rate, key.0, channels)?,
            channels,
            key.1 as usize,
        )?
        .into()
    };
    prepared.insert(key, pcm.clone());
    Ok(pcm)
}

fn start_output_streams<T>(
    streams: &[T],
    controls: &PlaybackControls,
    cancellation: &str,
    mut start: impl FnMut(&T) -> Result<(), String>,
) -> Result<(), String> {
    for stream in streams {
        if controls.stopped.load(Ordering::Acquire) {
            return Err(cancellation.into());
        }
        start(stream)?;
    }
    if controls.stopped.load(Ordering::Acquire) {
        return Err(cancellation.into());
    }
    Ok(())
}

fn acknowledge_audio_start(
    controls: &PlaybackControls,
    ready: &mut Option<tokio::sync::oneshot::Sender<Result<PlaybackSession, String>>>,
    session: PlaybackSession,
) -> bool {
    let Some(sender) = ready.take() else {
        controls.stop();
        return false;
    };
    if controls.stopped.load(Ordering::Acquire) || sender.is_closed() {
        controls.stop();
        return false;
    }
    controls.paused.store(false, Ordering::Release);
    if sender.send(Ok(session)).is_err() {
        controls.stop();
        return false;
    }
    true
}

fn run_session<R: tauri::Runtime>(
    app: &tauri::AppHandle<R>,
    id: &str,
    data: Vec<u8>,
    ids: Vec<String>,
    controls: Arc<PlaybackControls>,
    wake: mpsc::Receiver<()>,
    ready: &mut Option<tokio::sync::oneshot::Sender<Result<PlaybackSession, String>>>,
) -> Result<(), String> {
    run_session_with(
        id,
        data,
        ids,
        controls,
        wake,
        ready,
        |state, position, duration| {
            emit(app, id, state, position, duration, None);
        },
    )
}

trait ClipHost: PreviewHost {
    fn clip_config(&self, device: &Self::Device) -> Result<(StreamConfig, SampleFormat), String>;
    fn build_clip(
        &self,
        device: &Self::Device,
        config: &StreamConfig,
        format: SampleFormat,
        pcm: Arc<[f32]>,
        controls: Arc<PlaybackControls>,
        position: Arc<RenderPosition>,
        errors: mpsc::Sender<String>,
    ) -> Result<Self::Stream, String>;
}

impl ClipHost for CpalAudioHost {
    fn clip_config(&self, device: &Device) -> Result<(StreamConfig, SampleFormat), String> {
        let supported = device
            .default_output_config()
            .map_err(|error| format!("Unable to configure output: {error}"))?;
        Ok((supported.clone().into(), supported.sample_format()))
    }
    fn build_clip(
        &self,
        device: &Device,
        config: &StreamConfig,
        format: SampleFormat,
        pcm: Arc<[f32]>,
        controls: Arc<PlaybackControls>,
        position: Arc<RenderPosition>,
        errors: mpsc::Sender<String>,
    ) -> Result<cpal::Stream, String> {
        match format {
            SampleFormat::F32 => {
                build_stream::<f32>(device, config, pcm, controls, position, errors)
            }
            SampleFormat::I16 => {
                build_stream::<i16>(device, config, pcm, controls, position, errors)
            }
            SampleFormat::U16 => {
                build_stream::<u16>(device, config, pcm, controls, position, errors)
            }
            format => Err(format!("Unsupported audio output format: {format:?}")),
        }
    }
}

fn run_session_with(
    id: &str,
    data: Vec<u8>,
    ids: Vec<String>,
    controls: Arc<PlaybackControls>,
    wake: mpsc::Receiver<()>,
    ready: &mut Option<tokio::sync::oneshot::Sender<Result<PlaybackSession, String>>>,
    report: impl FnMut(&'static str, f64, f64),
) -> Result<(), String> {
    run_session_on_host(
        &CpalAudioHost(cpal::default_host()),
        id,
        data,
        ids,
        controls,
        wake,
        ready,
        report,
    )
}

fn run_session_on_host<H: ClipHost>(
    host: &H,
    id: &str,
    data: Vec<u8>,
    ids: Vec<String>,
    controls: Arc<PlaybackControls>,
    wake: mpsc::Receiver<()>,
    ready: &mut Option<tokio::sync::oneshot::Sender<Result<PlaybackSession, String>>>,
    mut report: impl FnMut(&'static str, f64, f64),
) -> Result<(), String> {
    let (samples, rate, channels) = decode_audio(data)?;
    let duration = samples.len() as f64 / channels as f64 / rate as f64;
    let samples: Arc<[f32]> = samples.into();
    let devices = host
        .devices()
        .map_err(|error| format!("Unable to list outputs: {error}"))?
        .into_iter()
        .filter_map(|device| host.name(&device).ok().map(|name| (device, name)))
        .collect::<Vec<_>>();
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
        let (config, format) = host.clip_config(&device)?;
        let key = (config.sample_rate.0, config.channels);
        let pcm = prepare_output_pcm(&samples, rate, channels, key, &mut prepared)?;
        let frames = pcm.len() / config.channels as usize;
        let position = Arc::new(RenderPosition::default());
        let stream = host.build_clip(
            &device,
            &config,
            format,
            pcm,
            controls.clone(),
            position.clone(),
            errors.clone(),
        )?;
        streams.push(stream);
        positions.push((position, frames, config.sample_rate.0));
    }
    if streams.is_empty() {
        return Err("No output devices selected".into());
    }
    // Build all streams first; callbacks remain silent behind the shared pause
    // gate until every stream has started. Hardware latency may differ by device.
    start_output_streams(&streams, &controls, "Playback cancelled", |stream| {
        host.start(stream)
            .map_err(|error| format!("Unable to start output: {error}"))
    })?;
    let start_position = controls.requested_position().min(duration);
    if !acknowledge_audio_start(
        &controls,
        ready,
        PlaybackSession {
            session_id: id.into(),
            duration,
            position: start_position,
        },
    ) {
        return Err("Playback cancelled".into());
    }
    report("playing", start_position, duration);
    monitor_playback(
        &controls,
        &positions,
        &wake,
        &error_receiver,
        duration,
        |state, position| report(state, position, duration),
    )?;
    drop(streams);
    Ok(())
}

fn monitor_playback(
    controls: &PlaybackControls,
    positions: &[(Arc<RenderPosition>, usize, u32)],
    wake: &mpsc::Receiver<()>,
    errors: &mpsc::Receiver<String>,
    duration: f64,
    mut report: impl FnMut(&'static str, f64),
) -> Result<(), String> {
    let mut last_event = Instant::now();
    loop {
        let command = wake.recv_timeout(Duration::from_millis(20)).is_ok();
        let position = if positions[0].0.revision.load(Ordering::Acquire) == controls.revision() {
            positions[0].0.frame.load(Ordering::Acquire) as f64 / positions[0].2 as f64
        } else {
            controls.requested_position().min(duration)
        };
        if controls.stopped.load(Ordering::Acquire) {
            report("stopped", position);
            break;
        }
        if let Ok(error) = errors.try_recv() {
            return Err(format!("Audio output failed: {error}"));
        }
        if positions.iter().all(|(position, frames, _)| {
            position.frame.load(Ordering::Acquire) >= *frames
                && position.revision.load(Ordering::Acquire) == controls.revision()
        }) {
            report("ended", duration);
            break;
        }
        if command || last_event.elapsed() >= Duration::from_millis(100) {
            report(
                if controls.paused.load(Ordering::Acquire) {
                    "paused"
                } else {
                    "playing"
                },
                position.min(duration),
            );
            last_event = Instant::now();
        }
    }
    Ok(())
}

// Render one preview callback independently of the physical output device.
fn render_preview<T: Copy>(
    output: &mut [T],
    rate: u32,
    channels: usize,
    queue: &mut crate::audio_stream::PcmQueue,
    controls: &PlaybackControls,
    convert: impl Fn(f32) -> T,
) {
    output.fill(convert(0.0));
    if controls.stopped.load(Ordering::Acquire) || controls.paused.load(Ordering::Acquire) {
        return;
    }
    let volume = controls.volume();
    queue.render(output, rate, channels, |sample| {
        convert((sample * volume).clamp(-1.0, 1.0))
    });
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
                    render_preview(
                        output,
                        rate,
                        channels,
                        &mut queue,
                        &controls,
                        T::from_sample,
                    );
                }
            },
            move |error| {
                let _ = errors.send(error.to_string());
            },
            None,
        )
        .map_err(|error| format!("Unable to open preview output: {error}"))
}

fn run_pcm_session<R: tauri::Runtime>(
    app: &tauri::AppHandle<R>,
    id: &str,
    rate: u32,
    ids: Vec<String>,
    controls: Arc<PlaybackControls>,
    queues: StreamQueues,
    wake: mpsc::Receiver<()>,
    ready: &mut Option<tokio::sync::oneshot::Sender<Result<PlaybackSession, String>>>,
) -> Result<(), String> {
    run_pcm_session_with(
        id,
        rate,
        ids,
        controls,
        queues,
        wake,
        ready,
        |state, position, duration| {
            emit(app, id, state, position, duration, None);
        },
    )
}

trait PreviewHost {
    type Device;
    type Stream;
    fn default_device(&self) -> Option<Self::Device>;
    fn devices(&self) -> Result<Vec<Self::Device>, String>;
    fn name(&self, device: &Self::Device) -> Result<String, String>;
    fn build(
        &self,
        device: &Self::Device,
        queue: Arc<Mutex<crate::audio_stream::PcmQueue>>,
        controls: Arc<PlaybackControls>,
        errors: mpsc::Sender<String>,
    ) -> Result<Self::Stream, String>;
    fn start(&self, stream: &Self::Stream) -> Result<(), String>;
}

struct CpalAudioHost(cpal::Host);
impl PreviewHost for CpalAudioHost {
    type Device = Device;
    type Stream = cpal::Stream;
    fn default_device(&self) -> Option<Device> {
        self.0.default_output_device()
    }
    fn devices(&self) -> Result<Vec<Device>, String> {
        self.0
            .output_devices()
            .map(|devices| devices.collect())
            .map_err(|error| error.to_string())
    }
    fn name(&self, device: &Device) -> Result<String, String> {
        device.name().map_err(|error| error.to_string())
    }
    fn build(
        &self,
        device: &Device,
        queue: Arc<Mutex<crate::audio_stream::PcmQueue>>,
        controls: Arc<PlaybackControls>,
        errors: mpsc::Sender<String>,
    ) -> Result<cpal::Stream, String> {
        let supported = device
            .default_output_config()
            .map_err(|error| error.to_string())?;
        let config: StreamConfig = supported.clone().into();
        match supported.sample_format() {
            SampleFormat::F32 => build_pcm_stream::<f32>(device, &config, queue, controls, errors),
            SampleFormat::I16 => build_pcm_stream::<i16>(device, &config, queue, controls, errors),
            SampleFormat::U16 => build_pcm_stream::<u16>(device, &config, queue, controls, errors),
            format => Err(format!("Unsupported output format: {format:?}")),
        }
    }
    fn start(&self, stream: &cpal::Stream) -> Result<(), String> {
        stream.play().map_err(|error| error.to_string())
    }
}

fn run_pcm_session_with(
    id: &str,
    rate: u32,
    ids: Vec<String>,
    controls: Arc<PlaybackControls>,
    queues: StreamQueues,
    wake: mpsc::Receiver<()>,
    ready: &mut Option<tokio::sync::oneshot::Sender<Result<PlaybackSession, String>>>,
    report: impl FnMut(&'static str, f64, f64),
) -> Result<(), String> {
    run_pcm_session_on_host(
        &CpalAudioHost(cpal::default_host()),
        id,
        rate,
        ids,
        controls,
        queues,
        wake,
        ready,
        report,
    )
}

fn run_pcm_session_on_host<H: PreviewHost>(
    host: &H,
    id: &str,
    rate: u32,
    ids: Vec<String>,
    controls: Arc<PlaybackControls>,
    queues: StreamQueues,
    wake: mpsc::Receiver<()>,
    ready: &mut Option<tokio::sync::oneshot::Sender<Result<PlaybackSession, String>>>,
    mut report: impl FnMut(&'static str, f64, f64),
) -> Result<(), String> {
    let devices = if ids.is_empty() {
        vec![host
            .default_device()
            .ok_or("No default audio output is available")?]
    } else {
        let devices = host.devices()?;
        let names = devices
            .iter()
            .map(|device| host.name(device))
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
        .map(|device| host.name(device))
        .collect::<Result<Vec<_>, _>>()?;
    let default_route = ids.is_empty();
    let mut streams = Vec::new();
    let (errors, receiver) = mpsc::channel();
    for device in devices {
        if controls.stopped.load(Ordering::Acquire) {
            return Err("Preview cancelled".into());
        }
        let queue = Arc::new(Mutex::new(crate::audio_stream::PcmQueue::new(rate)?));
        let stream = host.build(&device, queue.clone(), controls.clone(), errors.clone())?;
        queues
            .lock()
            .map_err(|_| "Preview queue unavailable")?
            .push(queue);
        streams.push(stream);
    }
    start_output_streams(&streams, &controls, "Preview cancelled", |stream| {
        host.start(stream)
    })?;
    if !acknowledge_audio_start(
        &controls,
        ready,
        PlaybackSession {
            session_id: id.into(),
            duration: 0.0,
            position: 0.0,
        },
    ) {
        return Err("Preview cancelled".into());
    }
    monitor_preview(
        &controls,
        &queues,
        &wake,
        &receiver,
        Duration::from_secs(1),
        Duration::from_millis(100),
        || {
            if default_route {
                let current = host
                    .default_device()
                    .and_then(|device| host.name(&device).ok());
                validate_default_output_route(
                    output_names.first().map(String::as_str),
                    current.as_deref(),
                )?;
            } else {
                let available = host
                    .devices()?
                    .iter()
                    .filter_map(|device| host.name(device).ok())
                    .collect::<Vec<_>>();
                validate_selected_output_routes(&output_names, &available)?;
            }
            Ok(())
        },
        |state, position, duration| report(state, position, duration),
    )?;
    drop(streams);
    Ok(())
}

fn monitor_preview(
    controls: &PlaybackControls,
    queues: &StreamQueues,
    wake: &mpsc::Receiver<()>,
    errors: &mpsc::Receiver<String>,
    device_check_interval: Duration,
    event_interval: Duration,
    mut check_devices: impl FnMut() -> Result<(), String>,
    mut report: impl FnMut(&'static str, f64, f64),
) -> Result<(), String> {
    let mut last_event = Instant::now();
    let mut last_device_check = Instant::now();
    loop {
        let _ = wake.recv_timeout(Duration::from_millis(20));
        if last_device_check.elapsed() >= device_check_interval {
            check_devices()?;
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
            report("stopped", position, 0.0);
            break;
        }
        if let Ok(error) = errors.try_recv() {
            return Err(format!("Audio output disconnected: {error}"));
        }
        if ended {
            report("ended", position, position);
            break;
        }
        if last_event.elapsed() >= event_interval {
            let status = if controls.paused.load(Ordering::Acquire) {
                "paused"
            } else {
                "playing"
            };
            report(status, position, 0.0);
            last_event = Instant::now();
        }
    }
    Ok(())
}

#[cfg(test)]
mod monitor_tests {
    use super::*;
    use crate::audio_stream::PcmQueue;

    fn position(frame: usize, revision: usize) -> Arc<RenderPosition> {
        let position = Arc::new(RenderPosition::default());
        position.frame.store(frame, Ordering::Release);
        position.revision.store(revision, Ordering::Release);
        position
    }

    #[test]
    fn clip_monitor_reports_stop_at_the_rendered_position() {
        let controls = PlaybackControls::new(0.0, 1.0);
        controls.stop();
        let positions = vec![(position(25, controls.revision()), 100, 100)];
        let (wake_tx, wake) = mpsc::channel();
        let (_error_tx, errors) = mpsc::channel();
        wake_tx.send(()).unwrap();
        let mut events = Vec::new();
        monitor_playback(&controls, &positions, &wake, &errors, 1.0, |state, time| {
            events.push((state, time))
        })
        .unwrap();
        assert_eq!(events, vec![("stopped", 0.25)]);
    }

    #[test]
    fn clip_monitor_reports_device_errors_instead_of_success() {
        let controls = PlaybackControls::new(0.0, 1.0);
        let positions = vec![(position(100, controls.revision()), 100, 100)];
        let (wake_tx, wake) = mpsc::channel();
        let (error_tx, errors) = mpsc::channel();
        error_tx.send("device lost".into()).unwrap();
        wake_tx.send(()).unwrap();
        let mut events = Vec::new();
        let error = monitor_playback(&controls, &positions, &wake, &errors, 1.0, |state, time| {
            events.push((state, time))
        })
        .unwrap_err();
        assert_eq!(error, "Audio output failed: device lost");
        assert!(events.is_empty());
    }

    #[test]
    fn clip_monitor_waits_for_every_output_to_finish() {
        let controls = PlaybackControls::new(0.0, 1.0);
        controls.paused.store(false, Ordering::Release);
        let first = position(100, controls.revision());
        let second = position(25, controls.revision());
        let positions = vec![(first, 100, 100), (second.clone(), 100, 100)];
        let (wake_tx, wake) = mpsc::channel();
        let (_error_tx, errors) = mpsc::channel();
        wake_tx.send(()).unwrap();
        let mut events = Vec::new();
        monitor_playback(&controls, &positions, &wake, &errors, 1.0, |state, time| {
            events.push((state, time));
            if state == "playing" {
                second.frame.store(100, Ordering::Release);
                wake_tx.send(()).unwrap();
            }
        })
        .unwrap();
        assert_eq!(events, vec![("playing", 1.0), ("ended", 1.0)]);
    }

    #[test]
    fn clip_monitor_uses_requested_seek_until_renderers_acknowledge_it() {
        let controls = PlaybackControls::new(0.0, 1.0);
        controls.paused.store(false, Ordering::Release);
        let output = position(100, controls.revision());
        controls.seek(0.4);
        let positions = vec![(output.clone(), 100, 100)];
        let (wake_tx, wake) = mpsc::channel();
        let (_error_tx, errors) = mpsc::channel();
        wake_tx.send(()).unwrap();
        let mut events = Vec::new();
        monitor_playback(&controls, &positions, &wake, &errors, 1.0, |state, time| {
            events.push((state, time));
            if state == "playing" {
                output.frame.store(100, Ordering::Release);
                output
                    .revision
                    .store(controls.revision(), Ordering::Release);
                wake_tx.send(()).unwrap();
            }
        })
        .unwrap();
        assert_eq!(events, vec![("playing", 0.4), ("ended", 1.0)]);
    }

    #[test]
    fn clip_monitor_reports_pause_and_clamps_progress_to_duration() {
        let controls = PlaybackControls::new(0.0, 1.0);
        let positions = vec![(position(150, controls.revision()), 200, 100)];
        let (wake_tx, wake) = mpsc::channel();
        let (_error_tx, errors) = mpsc::channel();
        wake_tx.send(()).unwrap();
        let mut events = Vec::new();
        monitor_playback(&controls, &positions, &wake, &errors, 1.0, |state, time| {
            events.push((state, time));
            if state == "paused" {
                controls.stop();
                wake_tx.send(()).unwrap();
            }
        })
        .unwrap();
        assert_eq!(events[0], ("paused", 1.0));
        assert_eq!(events[1].0, "stopped");
    }

    fn preview_queue() -> Arc<Mutex<PcmQueue>> {
        Arc::new(Mutex::new(PcmQueue::new(8000).unwrap()))
    }

    #[test]
    fn preview_monitor_reports_paused_state_before_stopping() {
        let controls = PlaybackControls::new(0.0, 1.0);
        let queues: StreamQueues = Arc::new(Mutex::new(vec![preview_queue()]));
        let (wake_tx, wake) = mpsc::channel();
        let (_error_tx, errors) = mpsc::channel();
        wake_tx.send(()).unwrap();
        let mut events = Vec::new();
        monitor_preview(
            &controls,
            &queues,
            &wake,
            &errors,
            Duration::from_secs(1),
            Duration::ZERO,
            || Ok(()),
            |state, _, _| {
                events.push(state);
                if events.len() == 1 {
                    controls.stop();
                    wake_tx.send(()).unwrap();
                }
            },
        )
        .unwrap();
        assert_eq!(events, vec!["paused", "stopped"]);
    }

    #[test]
    fn preview_monitor_waits_for_all_output_queues() {
        let controls = PlaybackControls::new(0.0, 1.0);
        controls.paused.store(false, Ordering::Release);
        let first = preview_queue();
        first.lock().unwrap().finished = true;
        let second = preview_queue();
        {
            let mut queue = second.lock().unwrap();
            queue.append(&[0.2, 0.4]).unwrap();
            queue.finished = true;
        }
        let queues: StreamQueues = Arc::new(Mutex::new(vec![first, second.clone()]));
        let (wake_tx, wake) = mpsc::channel();
        let (_error_tx, errors) = mpsc::channel();
        wake_tx.send(()).unwrap();
        let mut events = Vec::new();
        monitor_preview(
            &controls,
            &queues,
            &wake,
            &errors,
            Duration::from_secs(1),
            Duration::ZERO,
            || Ok(()),
            |state, _, _| {
                events.push(state);
                if state == "playing" {
                    second
                        .lock()
                        .unwrap()
                        .render(&mut [0.0; 2], 8000, 1, |sample| sample);
                    wake_tx.send(()).unwrap();
                }
            },
        )
        .unwrap();
        assert_eq!(events, vec!["playing", "ended"]);
    }

    #[test]
    fn preview_monitor_reports_output_errors_before_completion() {
        let controls = PlaybackControls::new(0.0, 1.0);
        let queue = preview_queue();
        queue.lock().unwrap().finished = true;
        let queues: StreamQueues = Arc::new(Mutex::new(vec![queue]));
        let (wake_tx, wake) = mpsc::channel();
        let (error_tx, errors) = mpsc::channel();
        error_tx.send("speaker disappeared".into()).unwrap();
        wake_tx.send(()).unwrap();
        let mut events = Vec::new();
        let error = monitor_preview(
            &controls,
            &queues,
            &wake,
            &errors,
            Duration::from_secs(1),
            Duration::ZERO,
            || Ok(()),
            |state, _, _| events.push(state),
        )
        .unwrap_err();
        assert_eq!(error, "Audio output disconnected: speaker disappeared");
        assert!(events.is_empty());
    }

    #[test]
    fn preview_monitor_stops_when_the_output_route_changes() {
        let controls = PlaybackControls::new(0.0, 1.0);
        let queues: StreamQueues = Arc::new(Mutex::new(vec![preview_queue()]));
        let (wake_tx, wake) = mpsc::channel();
        let (_error_tx, errors) = mpsc::channel();
        wake_tx.send(()).unwrap();
        let error = monitor_preview(
            &controls,
            &queues,
            &wake,
            &errors,
            Duration::ZERO,
            Duration::ZERO,
            || Err("device changed".into()),
            |_, _, _| {},
        )
        .unwrap_err();
        assert_eq!(error, "device changed");
    }

    #[test]
    fn preview_monitor_reports_stopped_without_claiming_completion() {
        let controls = PlaybackControls::new(0.0, 1.0);
        controls.stop();
        let queues: StreamQueues = Arc::new(Mutex::new(vec![preview_queue()]));
        let (wake_tx, wake) = mpsc::channel();
        let (_error_tx, errors) = mpsc::channel();
        wake_tx.send(()).unwrap();
        let mut events = Vec::new();
        monitor_preview(
            &controls,
            &queues,
            &wake,
            &errors,
            Duration::from_secs(1),
            Duration::ZERO,
            || Ok(()),
            |state, position, duration| events.push((state, position, duration)),
        )
        .unwrap();
        assert_eq!(events, vec![("stopped", 0.0, 0.0)]);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn preview_volume_applies_to_each_channel_and_can_be_changed() {
        let controls = PlaybackControls::new(0.0, 1.0);
        controls.paused.store(false, Ordering::Release);
        let mut queue = crate::audio_stream::PcmQueue::new(8000).unwrap();
        queue.append(&[0.8, -0.4, 0.6, -0.2]).unwrap();
        queue.finished = true;
        let mut out = [0.0; 4];
        controls.set_volume(0.5);
        render_preview(&mut out, 8000, 2, &mut queue, &controls, |s| s);
        assert_eq!(out, [0.4, 0.4, -0.2, -0.2]);
        controls.set_volume(0.0);
        render_preview(&mut out, 8000, 2, &mut queue, &controls, |s| s);
        assert_eq!(out, [0.0; 4]);
        assert!(queue.ended());
    }

    #[test]
    fn paused_preview_is_silent_without_consuming_audio() {
        let controls = PlaybackControls::new(0.0, 1.0);
        let mut queue = crate::audio_stream::PcmQueue::new(8000).unwrap();
        queue.append(&[0.25, 0.5]).unwrap();
        queue.finished = true;
        let mut out = [1.0; 2];
        render_preview(&mut out, 8000, 1, &mut queue, &controls, |s| s);
        assert_eq!(out, [0.0; 2]);
        assert!(!queue.ended());
        controls.paused.store(false, Ordering::Release);
        render_preview(&mut out, 8000, 1, &mut queue, &controls, |s| s);
        assert_eq!(out, [0.25, 0.5]);
        assert!(queue.ended());
    }

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
    fn aligned_pcm_and_float_wav_remain_playable() {
        for (bits, format) in [
            (16, hound::SampleFormat::Int),
            (24, hound::SampleFormat::Int),
            (32, hound::SampleFormat::Float),
        ] {
            let mut cursor = Cursor::new(Vec::new());
            {
                let spec = hound::WavSpec {
                    channels: 2,
                    sample_rate: 24000,
                    bits_per_sample: bits,
                    sample_format: format,
                };
                let mut writer = hound::WavWriter::new(&mut cursor, spec).unwrap();
                match format {
                    hound::SampleFormat::Float => {
                        writer.write_sample(0.5_f32).unwrap();
                        writer.write_sample(-0.5_f32).unwrap();
                    }
                    hound::SampleFormat::Int => {
                        writer.write_sample(0_i32).unwrap();
                        writer.write_sample(1000_i32).unwrap();
                    }
                }
                writer.finalize().unwrap();
            }
            let (samples, rate, channels) = decode_audio(cursor.into_inner()).unwrap();
            assert_eq!((samples.len(), rate, channels), (2, 24000, 2));
        }
    }

    #[test]
    fn truncated_stereo_wav_cannot_hide_a_partial_frame_behind_invalid_block_alignment() {
        for declared_alignment in [0_u16, 1, 2, 6] {
            let mut audio = wav(&[0, 1000, -1000, 2000], 2, 24000);
            let fmt = audio.windows(4).position(|v| v == b"fmt ").unwrap();
            let data = audio.windows(4).position(|v| v == b"data").unwrap();
            audio[fmt + 20..fmt + 22].copy_from_slice(&declared_alignment.to_le_bytes());
            audio.truncate(audio.len() - 2);
            let riff_size = (audio.len() as u32) - 8;
            audio[4..8].copy_from_slice(&riff_size.to_le_bytes());
            audio[data + 4..data + 8].copy_from_slice(&6_u32.to_le_bytes());
            assert!(
                decode_audio(audio).is_err(),
                "invalid alignment {declared_alignment}"
            );
        }
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
    fn decode_rejects_wav_with_incomplete_stereo_frame_even_when_lengths_match() {
        let mut audio = wav(&[100, 200, 300, 400], 2, 24000);
        let data = audio.windows(4).position(|bytes| bytes == b"data").unwrap();
        audio.truncate(audio.len() - 2);
        let riff_size = (audio.len() - 8) as u32;
        audio[4..8].copy_from_slice(&riff_size.to_le_bytes());
        audio[data + 4..data + 8].copy_from_slice(&6_u32.to_le_bytes());
        assert!(decode_audio(audio).is_err());
    }

    #[test]
    fn decode_rejects_partial_extensible_pcm_frame() {
        let mut audio = wav(&[100, 200, 300, 400], 2, 24000);
        let fmt = audio.windows(4).position(|bytes| bytes == b"fmt ").unwrap();
        assert_eq!(&audio[fmt + 4..fmt + 8], &16_u32.to_le_bytes());
        audio[fmt + 4..fmt + 8].copy_from_slice(&40_u32.to_le_bytes());
        audio[fmt + 8..fmt + 10].copy_from_slice(&0xfffe_u16.to_le_bytes());
        let extension = [
            22, 0, 16, 0, 3, 0, 0, 0, 1, 0, 0, 0, 0, 0, 16, 0, 128, 0, 0, 170, 0, 56, 155, 113,
        ];
        audio.splice(fmt + 24..fmt + 24, extension);
        let riff_size = (audio.len() - 8) as u32;
        audio[4..8].copy_from_slice(&riff_size.to_le_bytes());
        assert_eq!(decode_audio(audio.clone()).unwrap().0.len(), 4);
        audio.truncate(audio.len() - 2);
        let data = audio.windows(4).position(|bytes| bytes == b"data").unwrap();
        audio[data + 4..data + 8].copy_from_slice(&6_u32.to_le_bytes());
        let riff_size = (audio.len() - 8) as u32;
        audio[4..8].copy_from_slice(&riff_size.to_le_bytes());
        assert!(decode_audio(audio).is_err());
    }

    #[test]
    fn decode_rejects_a_partial_mono_sample_with_a_valid_padding_byte() {
        let mut audio = wav(&[100, 200], 1, 24000);
        let data = audio.windows(4).position(|bytes| bytes == b"data").unwrap();
        audio.truncate(audio.len() - 1);
        audio.push(0); // RIFF pads odd-size chunks, but padding is not audio.
        let riff_size = (audio.len() - 8) as u32;
        audio[4..8].copy_from_slice(&riff_size.to_le_bytes());
        audio[data + 4..data + 8].copy_from_slice(&3_u32.to_le_bytes());
        assert!(decode_audio(audio).is_err());
    }

    #[test]
    fn decode_accepts_odd_metadata_chunk_with_padding() {
        let mut audio = wav(&[100, 200], 1, 24000);
        let data = audio.windows(4).position(|bytes| bytes == b"data").unwrap();
        audio.splice(data..data, b"JUNK\x03\x00\x00\x00abc\x00".iter().copied());
        let riff_size = (audio.len() - 8) as u32;
        audio[4..8].copy_from_slice(&riff_size.to_le_bytes());
        assert_eq!(decode_audio(audio).unwrap().0.len(), 2);
    }

    #[test]
    fn wave_alignment_check_defers_streaming_and_non_pcm_formats() {
        let mut audio = wav(&[100, 200], 1, 24000);
        let data = audio.windows(4).position(|bytes| bytes == b"data").unwrap();
        audio[data + 4..data + 8].copy_from_slice(&u32::MAX.to_le_bytes());
        assert!(validate_wave_frame_alignment(&audio).is_ok());
        audio[data + 4..data + 8].copy_from_slice(&3_u32.to_le_bytes());
        let fmt = audio.windows(4).position(|bytes| bytes == b"fmt ").unwrap();
        audio[fmt + 8..fmt + 10].copy_from_slice(&6_u16.to_le_bytes());
        assert!(validate_wave_frame_alignment(&audio).is_ok());
    }

    #[test]
    fn decode_accepts_wav_with_unknown_chunk_before_audio_data() {
        let mut audio = wav(&[100, 200, 300, 400], 2, 24000);
        let data = audio.windows(4).position(|bytes| bytes == b"data").unwrap();
        audio.splice(data..data, b"JUNK\x04\x00\x00\x00test".iter().copied());
        let riff_size = (audio.len() - 8) as u32;
        audio[4..8].copy_from_slice(&riff_size.to_le_bytes());
        assert_eq!(decode_audio(audio).unwrap().0.len(), 4);
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

    #[tokio::test]
    async fn multi_output_preview_waits_until_every_queue_can_accept_a_block() {
        let state = Arc::new(AudioOutputState::new());
        let controls = Arc::new(PlaybackControls::new(0.0, 1.0));
        let fast = Arc::new(Mutex::new(
            crate::audio_stream::PcmQueue::new(8000).unwrap(),
        ));
        let slow = Arc::new(Mutex::new(
            crate::audio_stream::PcmQueue::new(8000).unwrap(),
        ));
        slow.lock().unwrap().append(&vec![0.5; 16000]).unwrap();
        let (wake, _receiver) = mpsc::channel();
        *state.active.lock().unwrap() = Some(SessionHandle {
            id: "multi".into(),
            controls,
            wake,
            streaming: Some(Arc::new(Mutex::new(vec![fast.clone(), slow.clone()]))),
        });
        let pending_state = state.clone();
        let pending = tokio::spawn(async move {
            pending_state
                .append_pcm_stream("multi", vec![0, 64, 0, 192])
                .await
        });
        tokio::time::sleep(Duration::from_millis(30)).await;
        assert!(!pending.is_finished());
        assert_eq!(fast.lock().unwrap().samples_len_for_test(), 0);
        let mut output = [0.0_f32; 4];
        slow.lock()
            .unwrap()
            .render(&mut output, 8000, 1, |sample| sample);
        pending.await.unwrap().unwrap();
        assert_eq!(fast.lock().unwrap().samples_len_for_test(), 2);
        assert_eq!(slow.lock().unwrap().samples_len_for_test(), 15998);
        state.finish_pcm_stream("multi").unwrap();
        assert!(fast.lock().unwrap().finished && slow.lock().unwrap().finished);
        assert!(state.append_pcm_stream("multi", vec![0, 0]).await.is_err());
    }

    #[tokio::test]
    async fn finishing_a_backpressured_multi_output_rejects_the_block_everywhere() {
        let state = Arc::new(AudioOutputState::new());
        let fast = Arc::new(Mutex::new(
            crate::audio_stream::PcmQueue::new(8000).unwrap(),
        ));
        let slow = Arc::new(Mutex::new(
            crate::audio_stream::PcmQueue::new(8000).unwrap(),
        ));
        slow.lock().unwrap().append(&vec![0.5; 16000]).unwrap();
        let (wake, _receiver) = mpsc::channel();
        *state.active.lock().unwrap() = Some(SessionHandle {
            id: "multi".into(),
            controls: Arc::new(PlaybackControls::new(0.0, 1.0)),
            wake,
            streaming: Some(Arc::new(Mutex::new(vec![fast.clone(), slow.clone()]))),
        });
        let pending_state = state.clone();
        let pending =
            tokio::spawn(async move { pending_state.append_pcm_stream("multi", vec![0, 0]).await });
        tokio::time::sleep(Duration::from_millis(30)).await;
        assert!(!pending.is_finished());
        state.finish_pcm_stream("multi").unwrap();
        assert!(pending.await.unwrap().is_err());
        assert_eq!(fast.lock().unwrap().samples_len_for_test(), 0);
        assert_eq!(slow.lock().unwrap().samples_len_for_test(), 16000);
    }

    #[test]
    fn playback_metadata_checks_limits_types_and_defaults() {
        let body = tauri::ipc::InvokeBody::Raw(vec![0, 1]);
        let (_, devices, options) =
            read_playback_request(&body, Some(r#"{"deviceIds":[],"options":{}}"#)).unwrap();
        assert!(devices.is_empty());
        let options = options.unwrap();
        assert_eq!(options.start_time, 0.0);
        assert_eq!(options.volume, 1.0);
        for metadata in [
            "{}",
            r#"{"deviceIds":"speaker"}"#,
            r#"{"deviceIds":[],"options":{"volume":"loud"}}"#,
        ] {
            assert!(read_playback_request(&body, Some(metadata)).is_err());
        }
        assert!(read_playback_request(&body, Some(&" ".repeat(16 * 1024 + 1))).is_err());
    }

    #[tokio::test]
    async fn preview_append_rejects_empty_blocks_and_non_preview_sessions() {
        let state = AudioOutputState::new();
        let (wake, _receiver) = mpsc::channel();
        *state.active.lock().unwrap() = Some(SessionHandle {
            id: "clip".into(),
            controls: Arc::new(PlaybackControls::new(0.0, 1.0)),
            wake,
            streaming: None,
        });
        assert_eq!(
            state.append_pcm_stream("clip", vec![]).await.unwrap_err(),
            "Invalid PCM preview block"
        );
        assert_eq!(
            state
                .append_pcm_stream("clip", vec![0, 0])
                .await
                .unwrap_err(),
            "Not a preview session"
        );
        assert_eq!(
            state.finish_pcm_stream("clip").unwrap_err(),
            "Not a preview session"
        );
    }

    #[tokio::test]
    async fn preview_block_is_delivered_identically_to_all_outputs_and_finish_is_final() {
        let state = AudioOutputState::new();
        let controls = Arc::new(PlaybackControls::new(0.0, 1.0));
        controls.paused.store(false, Ordering::Release);
        let first = Arc::new(Mutex::new(
            crate::audio_stream::PcmQueue::new(8000).unwrap(),
        ));
        let second = Arc::new(Mutex::new(
            crate::audio_stream::PcmQueue::new(8000).unwrap(),
        ));
        let (wake, _receiver) = mpsc::channel();
        *state.active.lock().unwrap() = Some(SessionHandle {
            id: "preview".into(),
            controls: controls.clone(),
            wake,
            streaming: Some(Arc::new(Mutex::new(vec![first.clone(), second.clone()]))),
        });
        state
            .append_pcm_stream("preview", vec![0, 64, 0, 192])
            .await
            .unwrap();
        state.finish_pcm_stream("preview").unwrap();
        state.finish_pcm_stream("preview").unwrap();
        assert_eq!(
            state
                .append_pcm_stream("preview", vec![0, 0])
                .await
                .unwrap_err(),
            "Preview already finished"
        );
        let mut left = [0.0; 2];
        let mut right = [0.0; 2];
        render_preview(
            &mut left,
            8000,
            1,
            &mut first.lock().unwrap(),
            &controls,
            |s| s,
        );
        render_preview(
            &mut right,
            8000,
            1,
            &mut second.lock().unwrap(),
            &controls,
            |s| s,
        );
        assert_eq!(left, [0.5, -0.5]);
        assert_eq!(right, left);
        assert!(first.lock().unwrap().ended() && second.lock().unwrap().ended());
    }

    #[test]
    fn preview_rejects_seek_without_applying_other_control_changes() {
        let state = AudioOutputState::new();
        let controls = Arc::new(PlaybackControls::new(0.0, 1.0));
        let (wake, receiver) = mpsc::channel();
        *state.active.lock().unwrap() = Some(SessionHandle {
            id: "preview".into(),
            controls: controls.clone(),
            wake,
            streaming: Some(Arc::new(Mutex::new(vec![]))),
        });
        let result = state.control_playback(
            "preview",
            PlaybackControl {
                paused: Some(false),
                position: Some(1.0),
                volume: Some(0.2),
            },
        );
        assert_eq!(result.unwrap_err(), "Speech preview cannot seek");
        assert!(controls.paused.load(Ordering::Acquire));
        assert_eq!(controls.requested_position(), 0.0);
        assert!(receiver.try_recv().is_err());
    }

    #[test]
    fn device_selection_deduplicates_requests_and_preserves_device_order() {
        let devices = vec!["Speaker".to_string(), "Headphones".to_string()];
        let speaker = device_id(&devices[0]);
        let headphones = device_id(&devices[1]);
        assert_eq!(
            selected_device_indices(&devices, &[headphones, speaker.clone(), speaker]).unwrap(),
            vec![0, 1]
        );
        assert!(selected_device_indices(&devices, &["device_unknown".into()]).is_err());
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
#[cfg(test)]
mod state_lifecycle_tests {
    use super::*;
    use crate::audio_stream::PcmQueue;

    fn session(
        streaming: bool,
        count: usize,
    ) -> (
        AudioOutputState,
        Arc<PlaybackControls>,
        StreamQueues,
        mpsc::Receiver<()>,
    ) {
        let state = AudioOutputState::new();
        let controls = Arc::new(PlaybackControls::new(0.0, 1.0));
        let queues: StreamQueues = Arc::new(Mutex::new(
            (0..count)
                .map(|_| Arc::new(Mutex::new(PcmQueue::new(8000).unwrap())))
                .collect(),
        ));
        let (wake, rx) = mpsc::channel();
        *state.active.lock().unwrap() = Some(SessionHandle {
            id: "current".into(),
            controls: controls.clone(),
            wake,
            streaming: streaming.then(|| queues.clone()),
        });
        (state, controls, queues, rx)
    }

    #[test]
    fn clip_controls_update_together_and_wake_worker() {
        let (state, controls, _, wake) = session(false, 0);
        let revision = controls.revision();
        state
            .control_playback(
                "current",
                PlaybackControl {
                    paused: Some(false),
                    position: Some(2.5),
                    volume: Some(0.25),
                },
            )
            .unwrap();
        assert!(!controls.paused.load(Ordering::Acquire));
        assert_eq!(controls.requested_position(), 2.5);
        assert!(controls.revision() > revision);
        assert!(wake.try_recv().is_ok());
        let mut queue = PcmQueue::new(8000).unwrap();
        queue.append(&[0.8, -0.4]).unwrap();
        queue.finished = true;
        let mut output = [0.0; 2];
        render_preview(&mut output, 8000, 1, &mut queue, &controls, |s| s);
        assert_eq!(output, [0.2, -0.1]);
    }

    #[tokio::test]
    async fn pcm_append_delivers_little_endian_samples_to_all_outputs() {
        let (state, _, queues, _) = session(true, 2);
        state
            .append_pcm_stream("current", vec![0x00, 0x80, 0x00, 0x40])
            .await
            .unwrap();
        state.finish_pcm_stream("current").unwrap();
        for queue in queues.lock().unwrap().iter() {
            let mut output = [0.0; 2];
            let mut queue = queue.lock().unwrap();
            queue.render(&mut output, 8000, 1, |sample| sample);
            assert_eq!(output, [-1.0, 0.5]);
            assert!(queue.ended());
        }
        assert!(state
            .append_pcm_stream("current", vec![0, 0])
            .await
            .is_err());
    }
}

#[cfg(test)]
mod output_preparation_tests {
    use super::*;

    #[test]
    fn matching_devices_share_original_pcm_without_copying() {
        let samples: Arc<[f32]> = vec![0.1, -0.2, 0.3, -0.4].into();
        let mut cache = HashMap::new();
        let first = prepare_output_pcm(&samples, 48000, 2, (48000, 2), &mut cache).unwrap();
        let second = prepare_output_pcm(&samples, 48000, 2, (48000, 2), &mut cache).unwrap();
        assert!(Arc::ptr_eq(&first, &samples));
        assert!(Arc::ptr_eq(&first, &second));
        assert_eq!(&*first, &[0.1, -0.2, 0.3, -0.4]);
        assert_eq!(cache.len(), 1);
    }

    #[test]
    fn mono_is_replicated_for_stereo_outputs_and_cached() {
        let samples: Arc<[f32]> = vec![0.25, -0.5, 1.0].into();
        let mut cache = HashMap::new();
        let first = prepare_output_pcm(&samples, 24000, 1, (24000, 2), &mut cache).unwrap();
        let second = prepare_output_pcm(&samples, 24000, 1, (24000, 2), &mut cache).unwrap();
        assert_eq!(&*first, &[0.25, 0.25, -0.5, -0.5, 1.0, 1.0]);
        assert!(Arc::ptr_eq(&first, &second));
        assert!(!Arc::ptr_eq(&first, &samples));
    }

    #[test]
    fn stereo_is_downmixed_per_frame_for_mono_output() {
        let samples: Arc<[f32]> = vec![0.5, -0.5, 1.0, 0.5].into();
        let pcm = prepare_output_pcm(&samples, 48000, 2, (48000, 1), &mut HashMap::new()).unwrap();
        assert_eq!(&*pcm, &[0.0, 0.75]);
    }

    #[test]
    fn different_rates_and_channel_counts_prepare_independent_formats() {
        let samples: Arc<[f32]> = vec![0.25; 48].into();
        let mut cache = HashMap::new();
        let stereo_48 = prepare_output_pcm(&samples, 24000, 1, (48000, 2), &mut cache).unwrap();
        let mono_48 = prepare_output_pcm(&samples, 24000, 1, (48000, 1), &mut cache).unwrap();
        let stereo_24 = prepare_output_pcm(&samples, 24000, 1, (24000, 2), &mut cache).unwrap();
        assert_eq!(
            (stereo_48.len(), mono_48.len(), stereo_24.len()),
            (192, 96, 96)
        );
        assert_eq!(cache.len(), 3);
        assert!(stereo_48.chunks_exact(2).all(|pair| pair[0] == pair[1]));
        assert!(stereo_48.iter().all(|sample| sample.is_finite()));
    }

    #[test]
    fn invalid_device_config_does_not_pollute_the_cache() {
        let samples: Arc<[f32]> = vec![0.5, -0.5].into();
        let mut cache = HashMap::new();
        for key in [(0, 1), (48000, 0)] {
            assert!(prepare_output_pcm(&samples, 48000, 1, key, &mut cache).is_err());
        }
        assert!(cache.is_empty());
        assert!(prepare_output_pcm(&samples, 48000, 1, (48000, 1), &mut cache).is_ok());
    }

    #[test]
    fn incomplete_source_frames_are_rejected_when_conversion_is_needed() {
        let samples: Arc<[f32]> = vec![0.5, -0.5, 0.1].into();
        let mut cache = HashMap::new();
        assert!(prepare_output_pcm(&samples, 48000, 2, (24000, 1), &mut cache).is_err());
        assert!(cache.is_empty());
    }
}

#[cfg(test)]
mod startup_ack_tests {
    use super::*;

    fn session() -> PlaybackSession {
        PlaybackSession {
            session_id: "native-1".into(),
            duration: 4.0,
            position: 1.5,
        }
    }

    #[tokio::test]
    async fn active_caller_receives_session_and_playback_is_unpaused() {
        let controls = PlaybackControls::new(1.5, 1.0);
        let (tx, rx) = tokio::sync::oneshot::channel();
        let mut ready = Some(tx);
        assert!(acknowledge_audio_start(&controls, &mut ready, session()));
        let result = rx.await.unwrap().unwrap();
        assert_eq!(
            (result.session_id.as_str(), result.duration, result.position),
            ("native-1", 4.0, 1.5)
        );
        assert!(!controls.paused.load(Ordering::Acquire));
        assert!(!controls.stopped.load(Ordering::Acquire));
        assert!(ready.is_none());
    }

    #[test]
    fn abandoned_caller_never_unpauses_or_reports_start() {
        let controls = PlaybackControls::new(0.0, 1.0);
        let (tx, rx) = tokio::sync::oneshot::channel();
        drop(rx);
        let mut ready = Some(tx);
        assert!(!acknowledge_audio_start(&controls, &mut ready, session()));
        assert!(controls.stopped.load(Ordering::Acquire));
        assert!(controls.paused.load(Ordering::Acquire));
    }

    #[test]
    fn missing_acknowledgement_is_treated_as_cancelled() {
        let controls = PlaybackControls::new(0.0, 1.0);
        let mut ready = None;
        assert!(!acknowledge_audio_start(&controls, &mut ready, session()));
        assert!(controls.stopped.load(Ordering::Acquire));
        assert!(controls.paused.load(Ordering::Acquire));
    }

    #[tokio::test]
    async fn previously_stopped_session_cannot_report_ready() {
        let controls = PlaybackControls::new(0.0, 1.0);
        controls.stop();
        let (tx, rx) = tokio::sync::oneshot::channel();
        let mut ready = Some(tx);
        assert!(!acknowledge_audio_start(&controls, &mut ready, session()));
        assert!(controls.paused.load(Ordering::Acquire));
        drop(ready);
        assert!(rx.await.is_err());
    }
}

#[cfg(test)]
mod output_start_tests {
    use super::*;

    #[test]
    fn all_outputs_start_in_order_while_the_pause_gate_remains_closed() {
        let controls = PlaybackControls::new(0.0, 1.0);
        let mut started = Vec::new();
        start_output_streams(&[1, 2, 3], &controls, "Playback cancelled", |id| {
            assert!(controls.paused.load(Ordering::Acquire));
            started.push(*id);
            Ok(())
        })
        .unwrap();
        assert_eq!(started, [1, 2, 3]);
        assert!(controls.paused.load(Ordering::Acquire));
    }

    #[test]
    fn cancellation_before_start_never_starts_any_output() {
        let controls = PlaybackControls::new(0.0, 1.0);
        controls.stop();
        let mut started = Vec::new();
        let result = start_output_streams(&[1, 2], &controls, "Playback cancelled", |id| {
            started.push(*id);
            Ok(())
        });
        assert_eq!(result.unwrap_err(), "Playback cancelled");
        assert!(started.is_empty());
    }

    #[test]
    fn cancellation_during_start_does_not_start_later_outputs() {
        let controls = PlaybackControls::new(0.0, 1.0);
        let mut started = Vec::new();
        let result = start_output_streams(&[1, 2, 3], &controls, "Preview cancelled", |id| {
            started.push(*id);
            if *id == 1 {
                controls.stop();
            }
            Ok(())
        });
        assert_eq!(result.unwrap_err(), "Preview cancelled");
        assert_eq!(started, [1]);
        assert!(controls.paused.load(Ordering::Acquire));
    }

    #[test]
    fn device_start_error_is_returned_and_later_devices_are_not_started() {
        let controls = PlaybackControls::new(0.0, 1.0);
        let mut started = Vec::new();
        let result = start_output_streams(&[1, 2, 3], &controls, "Playback cancelled", |id| {
            started.push(*id);
            if *id == 2 {
                Err("device disconnected".into())
            } else {
                Ok(())
            }
        });
        assert_eq!(result.unwrap_err(), "device disconnected");
        assert_eq!(started, [1, 2]);
        assert!(controls.paused.load(Ordering::Acquire));
    }

    #[test]
    fn cancellation_during_the_final_start_is_reported() {
        let controls = PlaybackControls::new(0.0, 1.0);
        let result = start_output_streams(&[1], &controls, "Preview cancelled", |_| {
            controls.stop();
            Ok(())
        });
        assert_eq!(result.unwrap_err(), "Preview cancelled");
    }
}

#[cfg(test)]
mod wav_truncation_regression_tests {
    use super::*;
    use std::io::Cursor;

    fn wav() -> Vec<u8> {
        let mut out = Cursor::new(Vec::new());
        {
            let mut writer = hound::WavWriter::new(
                &mut out,
                hound::WavSpec {
                    channels: 2,
                    sample_rate: 24000,
                    bits_per_sample: 16,
                    sample_format: hound::SampleFormat::Int,
                },
            )
            .unwrap();
            for sample in [0_i16, 1000, -1000, 2000] {
                writer.write_sample(sample).unwrap();
            }
            writer.finalize().unwrap();
        }
        out.into_inner()
    }

    fn data_header(bytes: &[u8]) -> usize {
        bytes.windows(4).position(|bytes| bytes == b"data").unwrap()
    }

    #[test]
    fn streaming_wave_accepts_each_complete_stereo_frame_count() {
        for frames in 1..=8 {
            let mut bytes = wav();
            let header = data_header(&bytes);
            bytes.truncate(header + 8);
            bytes.extend(std::iter::repeat_n(0, frames * 4));
            bytes[4..8].copy_from_slice(&u32::MAX.to_le_bytes());
            bytes[header + 4..header + 8].copy_from_slice(&u32::MAX.to_le_bytes());
            assert_eq!(decode_audio(bytes).unwrap().0.len(), frames * 2);
        }
    }

    #[test]
    fn streaming_wave_rejects_every_partial_stereo_frame_length() {
        for remainder in 1..=3 {
            let mut bytes = wav();
            let header = data_header(&bytes);
            bytes.extend(std::iter::repeat_n(0, remainder));
            bytes[4..8].copy_from_slice(&u32::MAX.to_le_bytes());
            bytes[header + 4..header + 8].copy_from_slice(&u32::MAX.to_le_bytes());
            assert!(
                decode_audio(bytes).is_err(),
                "accepted {remainder} extra bytes"
            );
        }
    }

    #[test]
    fn known_data_size_does_not_include_a_following_metadata_chunk() {
        let mut bytes = wav();
        bytes.extend_from_slice(b"JUNK\x04\x00\x00\x00test");
        let length = (bytes.len() - 8) as u32;
        bytes[4..8].copy_from_slice(&length.to_le_bytes());
        assert_eq!(decode_audio(bytes).unwrap().0.len(), 4);
    }

    #[test]
    fn playback_metadata_defaults_and_size_limit_are_enforced() {
        let body = tauri::ipc::InvokeBody::Raw(vec![1, 2]);
        let (bytes, ids, options) =
            read_playback_request(&body, Some(r#"{"deviceIds":["a"]}"#)).unwrap();
        assert_eq!(bytes, [1, 2]);
        assert_eq!(ids, ["a"]);
        assert!(options.is_none());
        assert!(read_playback_request(&body, Some(&" ".repeat(16 * 1024 + 1))).is_err());
        assert!(read_playback_request(&body, Some(r#"{"deviceIds":42}"#)).is_err());
    }

    #[test]
    fn wave_rejects_a_data_chunk_larger_than_available_bytes() {
        let mut bytes = wav();
        let header = data_header(&bytes);
        bytes[header + 4..header + 8].copy_from_slice(&100_u32.to_le_bytes());
        assert!(
            decode_audio(bytes).is_err(),
            "a truncated data chunk must not play as a successful recording"
        );
    }

    #[test]
    fn wave_with_unknown_container_length_still_rejects_truncated_known_data() {
        let mut bytes = wav();
        let header = data_header(&bytes);
        bytes[4..8].copy_from_slice(&u32::MAX.to_le_bytes());
        bytes[header + 4..header + 8].copy_from_slice(&100_u32.to_le_bytes());
        assert!(decode_audio(bytes).is_err());
    }

    #[test]
    fn streaming_wave_with_unknown_data_size_rejects_partial_frame() {
        let mut bytes = wav();
        let header = data_header(&bytes);
        bytes[4..8].copy_from_slice(&u32::MAX.to_le_bytes());
        bytes[header + 4..header + 8].copy_from_slice(&u32::MAX.to_le_bytes());
        bytes.pop();
        assert!(decode_audio(bytes).is_err());
    }

    #[test]
    fn valid_streaming_wave_with_unknown_sizes_decodes_complete_frames() {
        let mut bytes = wav();
        let header = data_header(&bytes);
        bytes[4..8].copy_from_slice(&u32::MAX.to_le_bytes());
        bytes[header + 4..header + 8].copy_from_slice(&u32::MAX.to_le_bytes());
        let (samples, rate, channels) = decode_audio(bytes).unwrap();
        assert_eq!((samples.len(), rate, channels), (4, 24000, 2));
    }
}

#[cfg(test)]
mod worker_cleanup_regression_tests {
    use super::*;
    use crate::audio_stream::PcmQueue;

    fn session(
        state: &AudioOutputState,
        id: &str,
        queue: Option<Arc<Mutex<PcmQueue>>>,
    ) -> Arc<PlaybackControls> {
        let controls = Arc::new(PlaybackControls::new(0.0, 1.0));
        let (wake, _) = mpsc::channel();
        *state.active.lock().unwrap() = Some(SessionHandle {
            id: id.into(),
            controls: controls.clone(),
            wake,
            streaming: queue.map(|queue| Arc::new(Mutex::new(vec![queue]))),
        });
        controls
    }

    #[test]
    fn normal_worker_exit_clears_and_stops_its_session() {
        let state = AudioOutputState::new();
        let controls = session(&state, "first", None);
        let cleanup = SessionCleanup::new(state.active.clone(), controls.clone(), "first".into());
        drop(cleanup);
        assert!(controls.stopped.load(Ordering::Acquire));
        assert!(state.active.lock().unwrap().is_none());
    }

    #[test]
    fn panicking_worker_clears_and_stops_its_session() {
        let state = AudioOutputState::new();
        let controls = session(&state, "first", None);
        let cleanup = SessionCleanup::new(state.active.clone(), controls.clone(), "first".into());
        let worker = std::thread::spawn(move || {
            let _cleanup = cleanup;
            panic!("worker failure");
        });
        assert!(worker.join().is_err());
        assert!(controls.stopped.load(Ordering::Acquire));
        assert!(state.active.lock().unwrap().is_none());
    }

    #[test]
    fn old_worker_exit_cannot_clear_replacement_session() {
        let state = AudioOutputState::new();
        let old = session(&state, "old", None);
        let cleanup = SessionCleanup::new(state.active.clone(), old.clone(), "old".into());
        let current = session(&state, "new", None);
        drop(cleanup);
        assert!(old.stopped.load(Ordering::Acquire));
        assert!(!current.stopped.load(Ordering::Acquire));
        assert_eq!(state.active.lock().unwrap().as_ref().unwrap().id, "new");
    }

    #[tokio::test]
    async fn worker_panic_releases_backpressured_preview_append() {
        let state = Arc::new(AudioOutputState::new());
        let queue = Arc::new(Mutex::new(PcmQueue::new(8000).unwrap()));
        queue.lock().unwrap().append(&vec![0.0; 16000]).unwrap();
        let controls = session(&state, "preview", Some(queue));
        let pending_state = state.clone();
        let pending =
            tokio::spawn(
                async move { pending_state.append_pcm_stream("preview", vec![0, 0]).await },
            );
        tokio::time::sleep(Duration::from_millis(30)).await;
        assert!(!pending.is_finished());
        let cleanup = SessionCleanup::new(state.active.clone(), controls, "preview".into());
        let worker = std::thread::spawn(move || {
            let _cleanup = cleanup;
            panic!("worker failure");
        });
        assert!(worker.join().is_err());
        assert!(tokio::time::timeout(Duration::from_secs(1), pending)
            .await
            .unwrap()
            .unwrap()
            .is_err());
    }
}

#[cfg(test)]
mod worker_reporting_regression_tests {
    use super::*;

    fn session() -> PlaybackSession {
        PlaybackSession {
            session_id: "one".into(),
            duration: 2.0,
            position: 0.0,
        }
    }

    #[tokio::test]
    async fn panic_before_ready_reports_error_to_caller_and_event_listener() {
        let controls = PlaybackControls::new(0.0, 1.0);
        let (sender, receiver) = tokio::sync::oneshot::channel();
        let mut events = Vec::new();
        let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            execute_audio_worker(
                &controls,
                Some(sender),
                |_| panic!("device failed"),
                |state, error| events.push((state, error)),
            );
        }));
        assert!(outcome.is_ok(), "worker panic escaped error handling");
        assert!(receiver.await.unwrap().unwrap_err().contains("worker"));
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].0, "error");
        assert!(events[0].1.as_ref().unwrap().contains("worker"));
    }

    #[tokio::test]
    async fn panic_after_ready_reports_a_terminal_error_event() {
        let controls = PlaybackControls::new(0.0, 1.0);
        let (sender, receiver) = tokio::sync::oneshot::channel();
        let mut events = Vec::new();
        let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            execute_audio_worker(
                &controls,
                Some(sender),
                |ready| {
                    ready.take().unwrap().send(Ok(session())).unwrap();
                    panic!("callback failed");
                },
                |state, error| events.push((state, error)),
            );
        }));
        assert!(outcome.is_ok());
        assert_eq!(receiver.await.unwrap().unwrap().session_id, "one");
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].0, "error");
        assert!(events[0].1.is_some());
    }

    #[tokio::test]
    async fn normal_start_failure_is_returned_and_reported_once() {
        let controls = PlaybackControls::new(0.0, 1.0);
        let (sender, receiver) = tokio::sync::oneshot::channel();
        let mut events = Vec::new();
        execute_audio_worker(
            &controls,
            Some(sender),
            |_| Err("device unavailable".into()),
            |state, error| events.push((state, error)),
        );
        assert_eq!(receiver.await.unwrap().unwrap_err(), "device unavailable");
        assert_eq!(events, [("error", Some("device unavailable".into()))]);
    }

    #[tokio::test]
    async fn stop_during_failure_reports_stopped_without_error_event() {
        let controls = PlaybackControls::new(0.0, 1.0);
        controls.stop();
        let (sender, receiver) = tokio::sync::oneshot::channel();
        let mut events = Vec::new();
        execute_audio_worker(
            &controls,
            Some(sender),
            |_| Err("cancelled".into()),
            |state, error| events.push((state, error)),
        );
        assert_eq!(receiver.await.unwrap().unwrap_err(), "cancelled");
        assert_eq!(events, [("stopped", None)]);
    }

    #[tokio::test]
    async fn worker_panic_reports_error_and_releases_owned_session() {
        let state = AudioOutputState::new();
        let controls = Arc::new(PlaybackControls::new(0.0, 1.0));
        let (wake, _) = mpsc::channel();
        *state.active.lock().unwrap() = Some(SessionHandle {
            id: "one".into(),
            controls: controls.clone(),
            wake,
            streaming: None,
        });
        let cleanup = SessionCleanup::new(state.active.clone(), controls.clone(), "one".into());
        let worker_controls = controls.clone();
        let (sender, receiver) = tokio::sync::oneshot::channel();
        let (event_tx, event_rx) = mpsc::channel();
        let worker = std::thread::spawn(move || {
            let _cleanup = cleanup;
            execute_audio_worker(
                &worker_controls,
                Some(sender),
                |_| panic!("worker failure"),
                |state, error| {
                    event_tx.send((state, error)).unwrap();
                },
            );
        });
        worker.join().unwrap();
        assert!(receiver.await.unwrap().is_err());
        assert_eq!(event_rx.recv().unwrap().0, "error");
        assert!(controls.stopped.load(Ordering::Acquire));
        assert!(state.active.lock().unwrap().is_none());
    }

    #[tokio::test]
    async fn panic_after_stop_reports_stopped() {
        let controls = PlaybackControls::new(0.0, 1.0);
        controls.stop();
        let (sender, receiver) = tokio::sync::oneshot::channel();
        let mut events = Vec::new();
        execute_audio_worker(
            &controls,
            Some(sender),
            |_| panic!("worker failure"),
            |state, error| events.push((state, error)),
        );
        assert!(receiver.await.unwrap().is_err());
        assert_eq!(events, [("stopped", None)]);
    }

    #[test]
    fn successful_worker_does_not_emit_a_failure_event() {
        let controls = PlaybackControls::new(0.0, 1.0);
        execute_audio_worker(
            &controls,
            None,
            |_| Ok(()),
            |_, _| panic!("unexpected failure"),
        );
    }

    #[test]
    fn abandoned_start_caller_does_not_prevent_error_event() {
        let controls = PlaybackControls::new(0.0, 1.0);
        let (sender, receiver) = tokio::sync::oneshot::channel();
        drop(receiver);
        let mut events = Vec::new();
        execute_audio_worker(
            &controls,
            Some(sender),
            |_| Err("failure".into()),
            |state, error| events.push((state, error)),
        );
        assert_eq!(events, [("error", Some("failure".into()))]);
    }
}

#[cfg(test)]
mod output_route_regression_tests {
    use super::*;
    use crate::audio_stream::PcmQueue;

    fn names(values: &[&str]) -> Vec<String> {
        values.iter().map(|value| (*value).into()).collect()
    }

    #[test]
    fn default_route_requires_the_original_output_to_remain_default() {
        for (expected, current, valid) in [
            (Some("Speaker"), Some("Speaker"), true),
            (Some("Speaker"), Some("Headphones"), false),
            (Some("Speaker"), None, false),
            (None, None, false),
            (None, Some("Speaker"), false),
        ] {
            assert_eq!(
                validate_default_output_route(expected, current).is_ok(),
                valid
            );
        }
    }

    #[test]
    fn selected_outputs_stay_valid_when_unrelated_devices_change_order() {
        assert!(validate_selected_output_routes(
            &names(&["Speakers", "Headphones"]),
            &names(&["Headphones", "USB", "Speakers"])
        )
        .is_ok());
    }

    #[test]
    fn missing_any_selected_output_is_an_error() {
        assert!(validate_selected_output_routes(
            &names(&["Speakers", "Headphones"]),
            &names(&["Speakers", "USB"])
        )
        .is_err());
    }

    #[test]
    fn a_new_duplicate_of_a_selected_output_is_ambiguous() {
        for duplicate in ["USB Audio", "USB_Audio", "usb audio"] {
            let result = validate_selected_output_routes(
                &names(&["USB Audio"]),
                &names(&["USB Audio", duplicate]),
            );
            assert!(result.is_err(), "accepted ambiguous output {duplicate}");
        }
    }

    #[test]
    fn collisions_between_unselected_outputs_do_not_affect_the_selected_route() {
        assert!(validate_selected_output_routes(
            &names(&["Speakers"]),
            &names(&["Speakers", "USB Audio", "USB_Audio"])
        )
        .is_ok());
    }

    #[test]
    fn a_renamed_selected_device_is_not_silently_replaced() {
        assert!(
            validate_selected_output_routes(&names(&["USB Audio"]), &names(&["USB_Audio"]))
                .is_err()
        );
    }

    #[test]
    fn monitor_terminates_when_a_selected_route_becomes_ambiguous() {
        let controls = PlaybackControls::new(0.0, 1.0);
        let queues: StreamQueues = Arc::new(Mutex::new(vec![Arc::new(Mutex::new(
            PcmQueue::new(8000).unwrap(),
        ))]));
        let (wake_tx, wake) = mpsc::channel();
        let (_error_tx, errors) = mpsc::channel();
        wake_tx.send(()).unwrap();
        let mut events = Vec::new();
        let result = monitor_preview(
            &controls,
            &queues,
            &wake,
            &errors,
            Duration::ZERO,
            Duration::ZERO,
            || {
                validate_selected_output_routes(
                    &names(&["USB Audio"]),
                    &names(&["USB Audio", "USB_Audio"]),
                )
            },
            |state, _, _| {
                events.push(state);
                controls.stop();
                let _ = wake_tx.send(());
            },
        );
        assert!(result.is_err());
        assert!(events.is_empty());
    }
}

#[cfg(test)]
mod native_silent_output_tests {
    use super::*;
    use crate::audio_stream::PcmQueue;

    fn output() -> (Device, StreamConfig, SampleFormat) {
        let device = cpal::default_host()
            .default_output_device()
            .expect("native output device required");
        let supported = device
            .default_output_config()
            .expect("native output configuration required");
        let format = supported.sample_format();
        (device, supported.into(), format)
    }

    fn wait_for(mut condition: impl FnMut() -> bool, label: &str) {
        let start = Instant::now();
        while !condition() && start.elapsed() < Duration::from_secs(3) {
            std::thread::sleep(Duration::from_millis(5));
        }
        assert!(condition(), "native audio callback did not {label}");
    }

    #[test]
    #[ignore = "requires a working native output device; emits silence"]
    fn native_silent_clip_pauses_resumes_seeks_and_stops() {
        let (device, config, format) = output();
        let samples: Arc<[f32]> =
            vec![0.0; config.sample_rate.0 as usize * config.channels as usize * 2].into();
        let controls = Arc::new(PlaybackControls::new(0.0, 1.0));
        let position = Arc::new(RenderPosition::default());
        let (error_tx, errors) = mpsc::channel();
        let stream = match format {
            SampleFormat::F32 => build_stream::<f32>(
                &device,
                &config,
                samples,
                controls.clone(),
                position.clone(),
                error_tx,
            ),
            SampleFormat::I16 => build_stream::<i16>(
                &device,
                &config,
                samples,
                controls.clone(),
                position.clone(),
                error_tx,
            ),
            SampleFormat::U16 => build_stream::<u16>(
                &device,
                &config,
                samples,
                controls.clone(),
                position.clone(),
                error_tx,
            ),
            other => panic!("unsupported output format: {other:?}"),
        }
        .expect("open native output");
        start_output_streams(&[&stream], &controls, "cancelled", |stream| {
            stream.play().map_err(|error| error.to_string())
        })
        .unwrap();
        wait_for(
            || position.revision.load(Ordering::Acquire) == controls.revision(),
            "acknowledge initial position",
        );
        assert_eq!(position.frame.load(Ordering::Acquire), 0);
        controls.paused.store(false, Ordering::Release);
        wait_for(
            || position.frame.load(Ordering::Acquire) > 0,
            "advance after resume",
        );
        controls.seek(1.0);
        wait_for(
            || {
                position.revision.load(Ordering::Acquire) == controls.revision()
                    && position.frame.load(Ordering::Acquire) >= config.sample_rate.0 as usize
            },
            "apply seek",
        );
        controls.stop();
        std::thread::sleep(Duration::from_millis(100));
        let stopped = position.frame.load(Ordering::Acquire);
        std::thread::sleep(Duration::from_millis(100));
        assert_eq!(position.frame.load(Ordering::Acquire), stopped);
        assert!(errors.try_recv().is_err());
        drop(stream);
    }

    #[test]
    #[ignore = "requires a working native output device; emits silence"]
    fn native_silent_preview_waits_while_paused_and_drains_after_resume() {
        let (device, config, format) = output();
        let queue = Arc::new(Mutex::new(PcmQueue::new(8000).unwrap()));
        queue.lock().unwrap().append(&vec![0.0; 8000]).unwrap();
        queue.lock().unwrap().finished = true;
        let controls = Arc::new(PlaybackControls::new(0.0, 1.0));
        let (error_tx, errors) = mpsc::channel();
        let stream = match format {
            SampleFormat::F32 => {
                build_pcm_stream::<f32>(&device, &config, queue.clone(), controls.clone(), error_tx)
            }
            SampleFormat::I16 => {
                build_pcm_stream::<i16>(&device, &config, queue.clone(), controls.clone(), error_tx)
            }
            SampleFormat::U16 => {
                build_pcm_stream::<u16>(&device, &config, queue.clone(), controls.clone(), error_tx)
            }
            other => panic!("unsupported output format: {other:?}"),
        }
        .expect("open native preview output");
        stream.play().unwrap();
        std::thread::sleep(Duration::from_millis(100));
        assert_eq!(queue.lock().unwrap().samples_len_for_test(), 8000);
        controls.paused.store(false, Ordering::Release);
        wait_for(
            || queue.lock().unwrap().position() > 0.0,
            "consume preview samples",
        );
        wait_for(|| queue.lock().unwrap().ended(), "drain finished preview");
        assert!(errors.try_recv().is_err());
        controls.stop();
        drop(stream);
    }
}

#[cfg(test)]
mod pcm_validation_regression_tests {
    use super::*;

    #[test]
    fn matching_stereo_format_rejects_incomplete_frames() {
        let samples: Arc<[f32]> = vec![0.1, 0.2, 0.3].into();
        let mut cache = HashMap::new();
        assert!(prepare_output_pcm(&samples, 48000, 2, (48000, 2), &mut cache).is_err());
        assert!(cache.is_empty());
    }

    #[test]
    fn matching_zero_rate_is_rejected_without_caching() {
        let samples: Arc<[f32]> = vec![0.1].into();
        let mut cache = HashMap::new();
        assert!(prepare_output_pcm(&samples, 0, 1, (0, 1), &mut cache).is_err());
        assert!(cache.is_empty());
    }

    #[test]
    fn matching_zero_channels_is_rejected_without_caching() {
        let samples: Arc<[f32]> = vec![0.1].into();
        let mut cache = HashMap::new();
        assert!(prepare_output_pcm(&samples, 48000, 0, (48000, 0), &mut cache).is_err());
        assert!(cache.is_empty());
    }

    #[test]
    fn source_channel_count_cannot_wrap_into_a_different_output_layout() {
        let samples: Arc<[f32]> = vec![0.1].into();
        let mut cache = HashMap::new();
        assert!(prepare_output_pcm(&samples, 48000, 65537, (48000, 1), &mut cache).is_err());
        assert!(cache.is_empty());
    }

    #[test]
    fn cached_output_does_not_mask_an_invalid_source_format() {
        let valid: Arc<[f32]> = vec![0.1, 0.2].into();
        let incomplete: Arc<[f32]> = vec![0.1].into();
        let mut cache = HashMap::new();
        prepare_output_pcm(&valid, 48000, 2, (48000, 2), &mut cache).unwrap();
        assert!(prepare_output_pcm(&incomplete, 48000, 2, (48000, 2), &mut cache).is_err());
        assert_eq!(cache.len(), 1);
    }

    #[test]
    fn invalid_output_dimensions_fail_even_when_other_formats_are_cached() {
        let samples: Arc<[f32]> = vec![0.1, 0.2].into();
        let mut cache = HashMap::new();
        prepare_output_pcm(&samples, 48000, 2, (48000, 2), &mut cache).unwrap();
        for key in [(0, 2), (48000, 0)] {
            assert!(prepare_output_pcm(&samples, 48000, 2, key, &mut cache).is_err());
        }
        assert_eq!(cache.len(), 1);
    }
}

#[cfg(test)]
mod clip_session_regression_tests {
    use super::*;
    use std::io::Cursor;

    fn silent_wav(rate: u32, frames: u32) -> Vec<u8> {
        let mut bytes = Cursor::new(Vec::new());
        {
            let spec = hound::WavSpec {
                channels: 1,
                sample_rate: rate,
                bits_per_sample: 16,
                sample_format: hound::SampleFormat::Int,
            };
            let mut writer = hound::WavWriter::new(&mut bytes, spec).unwrap();
            for _ in 0..frames {
                writer.write_sample(0_i16).unwrap();
            }
            writer.finalize().unwrap();
        }
        bytes.into_inner()
    }

    fn selected_output() -> (String, u32) {
        let device = cpal::default_host()
            .default_output_device()
            .expect("native output device required");
        (
            device_id(&device.name().unwrap()),
            device.default_output_config().unwrap().sample_rate().0,
        )
    }

    #[test]
    fn invalid_audio_fails_before_device_selection_or_ready() {
        let controls = Arc::new(PlaybackControls::new(0.0, 1.0));
        let (_wake_tx, wake) = mpsc::channel();
        let (ready_tx, _ready_rx) = tokio::sync::oneshot::channel();
        let mut ready = Some(ready_tx);
        let result = run_session_with(
            "test",
            b"not audio".to_vec(),
            vec!["missing".into()],
            controls,
            wake,
            &mut ready,
            |_, _, _| panic!("invalid audio reported playback"),
        );
        assert!(result.is_err());
        assert!(ready.is_some());
    }

    struct Worker {
        controls: Arc<PlaybackControls>,
        wake: mpsc::Sender<()>,
        thread: Option<std::thread::JoinHandle<Result<(), String>>>,
    }
    impl Drop for Worker {
        fn drop(&mut self) {
            self.controls.stop();
            let _ = self.wake.send(());
            if let Some(thread) = self.thread.take() {
                let _ = thread.join();
            }
        }
    }

    fn launch(
        data: Vec<u8>,
        ids: Vec<String>,
        controls: Arc<PlaybackControls>,
        ready: tokio::sync::oneshot::Sender<Result<PlaybackSession, String>>,
    ) -> (Worker, mpsc::Receiver<(&'static str, f64, f64)>) {
        let (wake_tx, wake_rx) = mpsc::channel();
        let (event_tx, events) = mpsc::channel();
        let worker_controls = controls.clone();
        let thread = std::thread::spawn(move || {
            let mut ready = Some(ready);
            run_session_with(
                "test",
                data,
                ids,
                worker_controls,
                wake_rx,
                &mut ready,
                |state, position, duration| {
                    let _ = event_tx.send((state, position, duration));
                },
            )
        });
        (
            Worker {
                controls,
                wake: wake_tx,
                thread: Some(thread),
            },
            events,
        )
    }

    #[test]
    #[ignore = "requires a working native output device; emits silence"]
    fn native_silent_session_reports_ready_and_completion() {
        let (id, rate) = selected_output();
        let controls = Arc::new(PlaybackControls::new(0.0, 1.0));
        let (sender, receiver) = tokio::sync::oneshot::channel();
        let (mut worker, events) = launch(silent_wav(rate, rate / 4), vec![id], controls, sender);
        let first = events
            .recv_timeout(Duration::from_secs(6))
            .expect("playback did not start");
        assert_eq!(first.0, "playing");
        let session = receiver.blocking_recv().unwrap().unwrap();
        assert_eq!(session.session_id, "test");
        assert!((session.duration - 0.25).abs() < 0.001);
        loop {
            let event = events
                .recv_timeout(Duration::from_secs(6))
                .expect("playback did not finish");
            if event.0 == "ended" {
                assert!((event.1 - session.duration).abs() < 0.001);
                break;
            }
        }
        worker.thread.take().unwrap().join().unwrap().unwrap();
    }

    #[test]
    #[ignore = "requires a working native output device; emits silence"]
    fn native_silent_session_reports_manual_stop() {
        let (id, rate) = selected_output();
        let controls = Arc::new(PlaybackControls::new(0.0, 1.0));
        let (sender, receiver) = tokio::sync::oneshot::channel();
        let (mut worker, events) = launch(silent_wav(rate, rate * 3), vec![id], controls, sender);
        assert_eq!(
            events.recv_timeout(Duration::from_secs(6)).unwrap().0,
            "playing"
        );
        assert!(receiver.blocking_recv().unwrap().is_ok());
        worker.controls.stop();
        worker.wake.send(()).unwrap();
        loop {
            let event = events
                .recv_timeout(Duration::from_secs(6))
                .expect("stop was not reported");
            assert_ne!(event.0, "ended");
            if event.0 == "stopped" {
                break;
            }
        }
        worker.thread.take().unwrap().join().unwrap().unwrap();
    }

    #[test]
    #[ignore = "requires a working native output device; emits silence"]
    fn native_silent_session_abandoned_before_ready_is_cancelled() {
        let (id, rate) = selected_output();
        let controls = Arc::new(PlaybackControls::new(0.0, 1.0));
        let (sender, receiver) = tokio::sync::oneshot::channel();
        drop(receiver);
        let (mut worker, events) = launch(
            silent_wav(rate, rate / 4),
            vec![id],
            controls.clone(),
            sender,
        );
        let result = worker.thread.take().unwrap().join().unwrap();
        assert_eq!(result.unwrap_err(), "Playback cancelled");
        assert!(controls.stopped.load(Ordering::Acquire));
        assert!(events.try_recv().is_err());
    }

    #[test]
    #[ignore = "requires native audio device enumeration"]
    fn native_silent_session_rejects_unavailable_output() {
        let (_, rate) = selected_output();
        let controls = Arc::new(PlaybackControls::new(0.0, 1.0));
        let (sender, receiver) = tokio::sync::oneshot::channel();
        let (mut worker, events) = launch(
            silent_wav(rate, rate / 4),
            vec!["device_that_does_not_exist".into()],
            controls,
            sender,
        );
        let result = worker.thread.take().unwrap().join().unwrap();
        assert!(result.unwrap_err().contains("unavailable"));
        assert!(receiver.blocking_recv().is_err());
        assert!(events.try_recv().is_err());
    }
}

#[cfg(test)]
mod preview_session_regression_tests {
    use super::*;

    struct Worker {
        state: Arc<AudioOutputState>,
        controls: Arc<PlaybackControls>,
        thread: Option<std::thread::JoinHandle<Result<(), String>>>,
    }
    impl Drop for Worker {
        fn drop(&mut self) {
            let _ = self.state.stop_playback(Some("preview-test"));
            if let Some(thread) = self.thread.take() {
                let _ = thread.join();
            }
        }
    }

    fn launch(
        ids: Vec<String>,
        sender: tokio::sync::oneshot::Sender<Result<PlaybackSession, String>>,
    ) -> (Worker, mpsc::Receiver<(&'static str, f64, f64)>) {
        let state = Arc::new(AudioOutputState::new());
        let controls = Arc::new(PlaybackControls::new(0.0, 1.0));
        let queues: StreamQueues = Arc::new(Mutex::new(Vec::new()));
        let (wake, wake_rx) = mpsc::channel();
        *state.active.lock().unwrap() = Some(SessionHandle {
            id: "preview-test".into(),
            controls: controls.clone(),
            wake,
            streaming: Some(queues.clone()),
        });
        let (events_tx, events) = mpsc::channel();
        let worker_controls = controls.clone();
        let thread = std::thread::spawn(move || {
            let mut ready = Some(sender);
            run_pcm_session_with(
                "preview-test",
                8000,
                ids,
                worker_controls,
                queues,
                wake_rx,
                &mut ready,
                |state, position, duration| {
                    let _ = events_tx.send((state, position, duration));
                },
            )
        });
        (
            Worker {
                state,
                controls,
                thread: Some(thread),
            },
            events,
        )
    }

    fn wait_ready(
        mut ready: tokio::sync::oneshot::Receiver<Result<PlaybackSession, String>>,
    ) -> PlaybackSession {
        let deadline = Instant::now() + Duration::from_secs(6);
        loop {
            match ready.try_recv() {
                Ok(result) => return result.unwrap(),
                Err(tokio::sync::oneshot::error::TryRecvError::Closed) => {
                    panic!("preview worker exited before ready")
                }
                Err(tokio::sync::oneshot::error::TryRecvError::Empty)
                    if Instant::now() < deadline =>
                {
                    std::thread::sleep(Duration::from_millis(5))
                }
                Err(_) => panic!("preview worker did not become ready"),
            }
        }
    }

    fn selected_id() -> String {
        let device = cpal::default_host()
            .default_output_device()
            .expect("native output device required");
        device_id(&device.name().unwrap())
    }

    #[test]
    #[ignore = "requires a working native output device; emits silence"]
    fn native_silent_preview_session_streams_and_finishes() {
        let (sender, ready) = tokio::sync::oneshot::channel();
        let (mut worker, events) = launch(vec![], sender);
        assert_eq!(wait_ready(ready).session_id, "preview-test");
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_time()
            .build()
            .unwrap();
        runtime
            .block_on(
                worker
                    .state
                    .append_pcm_stream("preview-test", vec![0; 8192]),
            )
            .unwrap();
        worker.state.finish_pcm_stream("preview-test").unwrap();
        loop {
            let event = events
                .recv_timeout(Duration::from_secs(6))
                .expect("preview did not finish");
            if event.0 == "ended" {
                assert!((event.1 - 4096.0 / 8000.0).abs() < 0.002);
                assert_eq!(event.1, event.2);
                break;
            }
        }
        worker.thread.take().unwrap().join().unwrap().unwrap();
    }

    #[test]
    #[ignore = "requires a working native output device; emits silence"]
    fn native_silent_preview_session_selected_output_can_stop() {
        let (sender, ready) = tokio::sync::oneshot::channel();
        let (mut worker, events) = launch(vec![selected_id()], sender);
        assert_eq!(wait_ready(ready).session_id, "preview-test");
        worker.state.stop_playback(Some("preview-test")).unwrap();
        loop {
            let event = events
                .recv_timeout(Duration::from_secs(6))
                .expect("preview did not stop");
            assert_ne!(event.0, "ended");
            if event.0 == "stopped" {
                break;
            }
        }
        worker.thread.take().unwrap().join().unwrap().unwrap();
    }

    #[test]
    #[ignore = "requires a working native output device; emits silence"]
    fn native_silent_preview_session_abandoned_start_is_cancelled() {
        let (sender, ready) = tokio::sync::oneshot::channel();
        drop(ready);
        let (mut worker, events) = launch(vec![], sender);
        assert_eq!(
            worker.thread.take().unwrap().join().unwrap().unwrap_err(),
            "Preview cancelled"
        );
        assert!(worker.controls.stopped.load(Ordering::Acquire));
        assert!(events.try_recv().is_err());
    }

    #[test]
    #[ignore = "requires native audio device enumeration"]
    fn native_silent_preview_session_rejects_unavailable_output() {
        let (sender, ready) = tokio::sync::oneshot::channel();
        let (mut worker, events) = launch(vec!["device_that_does_not_exist".into()], sender);
        let result = worker.thread.take().unwrap().join().unwrap();
        assert!(result.unwrap_err().contains("unavailable"));
        assert!(ready.blocking_recv().is_err());
        assert!(events.try_recv().is_err());
    }
}

#[cfg(test)]
mod public_lifecycle_tests {
    use super::*;
    use tauri::{Listener, Manager};

    fn silent_wav() -> Vec<u8> {
        let mut buffer = std::io::Cursor::new(Vec::new());
        {
            let mut writer = hound::WavWriter::new(
                &mut buffer,
                hound::WavSpec {
                    channels: 1,
                    sample_rate: 8000,
                    bits_per_sample: 16,
                    sample_format: hound::SampleFormat::Int,
                },
            )
            .unwrap();
            for _ in 0..8000 {
                writer.write_sample(0_i16).unwrap();
            }
            writer.finalize().unwrap();
        }
        buffer.into_inner()
    }

    async fn wait_inactive(state: &AudioOutputState) {
        tokio::time::timeout(Duration::from_secs(5), async {
            loop {
                if state.active.lock().unwrap().is_none() {
                    break;
                }
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
        .await
        .expect("worker did not clean up");
    }

    #[tokio::test]
    async fn public_start_rejects_invalid_requests_and_cleans_up_failed_workers() {
        let app = tauri::test::mock_app();
        let state = AudioOutputState::new();
        assert!(state
            .start_pcm_stream(app.handle().clone(), 0, vec![])
            .await
            .is_err());
        assert!(state.active.lock().unwrap().is_none());
        for options in [
            PlaybackOptions {
                start_time: -1.0,
                volume: 1.0,
            },
            PlaybackOptions {
                start_time: 0.0,
                volume: f32::NAN,
            },
        ] {
            assert!(state
                .play_audio_to_devices(
                    app.handle().clone(),
                    vec![],
                    vec!["device".into()],
                    Some(options)
                )
                .await
                .is_err());
            assert!(state.active.lock().unwrap().is_none());
        }
        assert!(state
            .play_audio_to_devices(app.handle().clone(), vec![], vec![], None)
            .await
            .is_err());
        assert!(state
            .play_audio_to_devices(
                app.handle().clone(),
                b"not audio".to_vec(),
                vec!["device".into()],
                None
            )
            .await
            .is_err());
        wait_inactive(&state).await;
    }

    #[tokio::test]
    async fn public_invalid_audio_reports_worker_error_and_releases_session() {
        let app = tauri::test::mock_app();
        let (tx, rx) = mpsc::channel();
        app.listen("native-audio-playback", move |event| {
            let _ = tx.send(event.payload().to_owned());
        });
        let state = AudioOutputState::new();
        for _ in 0..2 {
            let error = state
                .play_audio_to_devices(
                    app.handle().clone(),
                    b"not audio".to_vec(),
                    vec!["device".into()],
                    None,
                )
                .await
                .unwrap_err();
            assert!(!error.is_empty());
            wait_inactive(&state).await;
        }
        let events: Vec<String> = rx.try_iter().collect();
        assert_eq!(events.len(), 2);
        assert!(events[0].contains("\"state\":\"error\"") && events[0].contains("native-1"));
        assert!(events[1].contains("\"state\":\"error\"") && events[1].contains("native-2"));
    }

    fn install_prior_session(state: &AudioOutputState) -> Arc<PlaybackControls> {
        let controls = Arc::new(PlaybackControls::new(0.0, 1.0));
        let (wake, _) = mpsc::channel();
        *state.active.lock().unwrap() = Some(SessionHandle {
            id: "prior".into(),
            controls: controls.clone(),
            wake,
            streaming: None,
        });
        controls
    }

    #[tokio::test]
    async fn public_preview_start_failure_reports_event_and_releases_session() {
        let app = tauri::test::mock_app();
        let (tx, rx) = mpsc::channel();
        app.listen("native-audio-playback", move |event| {
            let _ = tx.send(event.payload().to_owned());
        });
        let state = AudioOutputState::new();
        let error = state
            .start_pcm_stream(
                app.handle().clone(),
                8000,
                vec!["voicebox-impossible-device-37e9".into()],
            )
            .await
            .unwrap_err();
        assert!(!error.is_empty());
        wait_inactive(&state).await;
        let event = rx.recv_timeout(Duration::from_secs(2)).unwrap();
        assert!(event.contains("\"state\":\"error\""));
        assert!(event.contains("native-1"));
        assert!(state
            .append_pcm_stream("native-1", vec![0, 0])
            .await
            .is_err());
        assert!(state.finish_pcm_stream("native-1").is_err());
    }

    #[tokio::test]
    async fn invalid_public_requests_leave_current_playback_running() {
        let app = tauri::test::mock_app();
        let state = AudioOutputState::new();
        let prior = install_prior_session(&state);
        for rate in [0, 7999, 192001, u32::MAX] {
            assert!(state
                .start_pcm_stream(app.handle().clone(), rate, vec![])
                .await
                .is_err());
        }
        for options in [
            PlaybackOptions {
                start_time: f64::INFINITY,
                volume: 1.0,
            },
            PlaybackOptions {
                start_time: 0.0,
                volume: -0.1,
            },
            PlaybackOptions {
                start_time: 0.0,
                volume: 1.1,
            },
        ] {
            assert!(state
                .play_audio_to_devices(
                    app.handle().clone(),
                    vec![],
                    vec!["device".into()],
                    Some(options)
                )
                .await
                .is_err());
        }
        assert!(!prior.stopped.load(Ordering::Acquire));
        assert_eq!(state.active.lock().unwrap().as_ref().unwrap().id, "prior");
    }

    #[tokio::test]
    async fn failed_preview_replacement_stops_prior_and_later_start_can_retry() {
        let app = tauri::test::mock_app();
        let state = AudioOutputState::new();
        let prior = install_prior_session(&state);
        for _ in 0..2 {
            assert!(state
                .start_pcm_stream(
                    app.handle().clone(),
                    8000,
                    vec!["voicebox-impossible-device-37e9".into()]
                )
                .await
                .is_err());
            wait_inactive(&state).await;
        }
        assert!(prior.stopped.load(Ordering::Acquire));
        assert_eq!(state.next_id.load(Ordering::Acquire), 3);
    }

    #[tokio::test]
    async fn public_failed_clip_replacement_cleans_up_and_rejects_stale_controls() {
        let app = tauri::test::mock_app();
        let state = AudioOutputState::new();
        let prior = install_prior_session(&state);
        assert!(state
            .play_audio_to_devices(
                app.handle().clone(),
                b"not audio".to_vec(),
                vec!["device".into()],
                None
            )
            .await
            .is_err());
        wait_inactive(&state).await;
        assert!(prior.stopped.load(Ordering::Acquire));
        assert!(state
            .control_playback("prior", PlaybackControl::default())
            .is_err());
        assert!(state
            .control_playback("native-1", PlaybackControl::default())
            .is_err());
        assert!(state.stop_playback(Some("prior")).is_ok());
    }

    #[tokio::test]
    async fn public_clip_missing_output_reports_error_and_allows_retry() {
        let app = tauri::test::mock_app();
        let (tx, rx) = mpsc::channel();
        app.listen("native-audio-playback", move |event| {
            let _ = tx.send(event.payload().to_owned());
        });
        let state = AudioOutputState::new();
        for expected_id in ["native-1", "native-2"] {
            // IDs always begin with `device_`, so this cannot select real hardware.
            let error = state
                .play_audio_to_devices(
                    app.handle().clone(),
                    silent_wav(),
                    vec!["impossible-output-id".into()],
                    None,
                )
                .await
                .unwrap_err();
            assert!(
                error.contains("unavailable") || error.contains("Unable to list outputs"),
                "{error}"
            );
            wait_inactive(&state).await;
            let event: serde_json::Value =
                serde_json::from_str(&rx.recv_timeout(Duration::from_secs(2)).unwrap()).unwrap();
            assert_eq!(event["session_id"], expected_id);
            assert_eq!(event["state"], "error");
            assert_eq!(event["error"], error);
            assert!(state
                .control_playback(expected_id, PlaybackControl::default())
                .is_err());
        }
        assert!(rx.try_recv().is_err());
    }

    #[tokio::test]
    async fn public_clip_missing_output_replaces_prior_and_cannot_be_stopped_as_prior() {
        let app = tauri::test::mock_app();
        let state = AudioOutputState::new();
        let prior = install_prior_session(&state);
        let error = state
            .play_audio_to_devices(
                app.handle().clone(),
                silent_wav(),
                vec!["impossible-output-id".into()],
                Some(PlaybackOptions {
                    start_time: 0.5,
                    volume: 0.5,
                }),
            )
            .await
            .unwrap_err();
        assert!(
            error.contains("unavailable") || error.contains("Unable to list outputs"),
            "{error}"
        );
        wait_inactive(&state).await;
        assert!(prior.stopped.load(Ordering::Acquire));
        assert!(state.stop_playback(Some("prior")).is_ok());
        assert!(state
            .control_playback("native-1", PlaybackControl::default())
            .is_err());
    }

    #[tokio::test]
    async fn public_clip_rejects_truncated_wave_before_opening_any_output() {
        let app = tauri::test::mock_app();
        let (tx, rx) = mpsc::channel();
        app.listen("native-audio-playback", move |event| {
            let _ = tx.send(event.payload().to_owned());
        });
        let state = AudioOutputState::new();
        let full = silent_wav();
        for missing in [1, 2, 100, 1000] {
            let mut truncated = full.clone();
            truncated.truncate(truncated.len() - missing);
            let error = state
                .play_audio_to_devices(
                    app.handle().clone(),
                    truncated,
                    vec!["impossible-output-id".into()],
                    None,
                )
                .await
                .unwrap_err();
            assert!(error.contains("truncated"), "{error}");
            wait_inactive(&state).await;
            let event: serde_json::Value =
                serde_json::from_str(&rx.recv_timeout(Duration::from_secs(2)).unwrap()).unwrap();
            assert_eq!(event["state"], "error");
            assert_eq!(event["error"], error);
        }
        assert!(rx.try_recv().is_err());
    }

    #[tokio::test]
    #[ignore = "requires a working native output device; emits silence"]
    async fn native_silent_public_replacement_stops_old_preview() {
        let app = tauri::test::mock_app();
        let state = AudioOutputState::new();
        let old = state
            .start_pcm_stream(app.handle().clone(), 8000, vec![])
            .await
            .unwrap();
        let new = state
            .start_pcm_stream(app.handle().clone(), 8000, vec![])
            .await
            .unwrap();
        assert_ne!(old.session_id, new.session_id);
        assert!(state
            .append_pcm_stream(&old.session_id, vec![0, 0])
            .await
            .is_err());
        state.stop_playback(Some(&old.session_id)).unwrap();
        state
            .append_pcm_stream(&new.session_id, vec![0; 1600])
            .await
            .unwrap();
        state
            .control_playback(
                &new.session_id,
                PlaybackControl {
                    paused: Some(false),
                    ..Default::default()
                },
            )
            .unwrap();
        state.finish_pcm_stream(&new.session_id).unwrap();
        wait_inactive(&state).await;
    }

    #[tokio::test]
    #[ignore = "requires a working native output device; emits silence"]
    async fn native_silent_public_preview_and_clip_lifecycles() {
        let app = tauri::test::mock_app();
        let (tx, rx) = mpsc::channel();
        app.listen("native-audio-playback", move |event| {
            let _ = tx.send(event.payload().to_owned());
        });
        let state = AudioOutputState::new();
        let preview = state
            .start_pcm_stream(app.handle().clone(), 8000, vec![])
            .await
            .unwrap();
        state
            .append_pcm_stream(&preview.session_id, vec![0; 1600])
            .await
            .unwrap();
        state
            .control_playback(
                &preview.session_id,
                PlaybackControl {
                    paused: Some(false),
                    ..Default::default()
                },
            )
            .unwrap();
        state.finish_pcm_stream(&preview.session_id).unwrap();
        wait_inactive(&state).await;
        assert!(rx
            .try_iter()
            .any(|event| event.contains("\"state\":\"ended\"")
                && event.contains(&preview.session_id)));
        let device = cpal::default_host().default_output_device().unwrap();
        let clip = state
            .play_audio_to_devices(
                app.handle().clone(),
                silent_wav(),
                vec![device_id(&device.name().unwrap())],
                None,
            )
            .await
            .unwrap();
        state
            .control_playback(
                &clip.session_id,
                PlaybackControl {
                    paused: Some(false),
                    position: Some(0.5),
                    volume: Some(0.5),
                },
            )
            .unwrap();
        state.stop_playback(Some(&clip.session_id)).unwrap();
        wait_inactive(&state).await;
        assert!(rx.try_iter().any(
            |event| event.contains("\"state\":\"stopped\"") && event.contains(&clip.session_id)
        ));
    }
}

#[cfg(test)]
mod preview_host_integration_tests {
    use super::*;
    use crate::audio_stream::PcmQueue;

    #[derive(Default)]
    struct DeviceState {
        names: Vec<String>,
        default: Option<String>,
        fail_build: Option<String>,
        fail_start: Option<String>,
        fail_enumeration: bool,
        cancel_on_build: Option<String>,
        cancel_on_start: bool,
        built: Vec<String>,
        started: Vec<String>,
        dropped: Vec<String>,
    }
    #[derive(Clone)]
    struct Host(Arc<Mutex<DeviceState>>);
    impl Host {
        fn new(names: &[&str]) -> Self {
            Self(Arc::new(Mutex::new(DeviceState {
                names: names.iter().map(|name| (*name).into()).collect(),
                default: names.first().map(|name| (*name).into()),
                ..Default::default()
            })))
        }
    }
    struct Stream {
        name: String,
        state: Arc<Mutex<DeviceState>>,
        controls: Arc<PlaybackControls>,
    }
    impl Drop for Stream {
        fn drop(&mut self) {
            self.state.lock().unwrap().dropped.push(self.name.clone());
        }
    }
    impl PreviewHost for Host {
        type Device = String;
        type Stream = Stream;
        fn default_device(&self) -> Option<String> {
            self.0.lock().unwrap().default.clone()
        }
        fn devices(&self) -> Result<Vec<String>, String> {
            let state = self.0.lock().unwrap();
            if state.fail_enumeration {
                Err("enumeration failed".into())
            } else {
                Ok(state.names.clone())
            }
        }
        fn name(&self, device: &String) -> Result<String, String> {
            Ok(device.clone())
        }
        fn build(
            &self,
            device: &String,
            _: Arc<Mutex<PcmQueue>>,
            controls: Arc<PlaybackControls>,
            _: mpsc::Sender<String>,
        ) -> Result<Stream, String> {
            let mut state = self.0.lock().unwrap();
            state.built.push(device.clone());
            if state.cancel_on_build.as_ref() == Some(device) {
                controls.stop();
            }
            if state.fail_build.as_ref() == Some(device) {
                return Err("build failed".into());
            }
            Ok(Stream {
                name: device.clone(),
                state: self.0.clone(),
                controls,
            })
        }
        fn start(&self, stream: &Stream) -> Result<(), String> {
            assert!(
                stream.controls.paused.load(Ordering::Acquire),
                "no output may play before all starts succeed"
            );
            let mut state = self.0.lock().unwrap();
            state.started.push(stream.name.clone());
            if state.cancel_on_start {
                stream.controls.stop();
            }
            if state.fail_start.as_ref() == Some(&stream.name) {
                return Err("start failed".into());
            }
            Ok(())
        }
    }
    fn run_failure(
        host: &Host,
        ids: Vec<String>,
        controls: Arc<PlaybackControls>,
        receiver_dropped: bool,
    ) -> String {
        let queues: StreamQueues = Arc::new(Mutex::new(Vec::new()));
        let (_wake_tx, wake) = mpsc::channel();
        let (sender, receiver) = tokio::sync::oneshot::channel();
        let _receiver = if receiver_dropped {
            drop(receiver);
            None
        } else {
            Some(receiver)
        };
        let mut ready = Some(sender);
        run_pcm_session_on_host(
            host,
            "preview",
            8000,
            ids,
            controls,
            queues,
            wake,
            &mut ready,
            |_, _, _| panic!("failed startup must not report playback"),
        )
        .unwrap_err()
    }
    fn ids(names: &[&str]) -> Vec<String> {
        names.iter().map(|name| device_id(name)).collect()
    }

    #[test]
    fn missing_and_ambiguous_routes_fail_before_opening_streams() {
        for (names, selected) in [
            (vec![], vec![]),
            (vec!["A"], ids(&["missing"])),
            (vec!["USB Audio", "USB_Audio"], ids(&["USB Audio"])),
        ] {
            let host = Host::new(&names);
            let error = run_failure(
                &host,
                selected,
                Arc::new(PlaybackControls::new(0.0, 1.0)),
                false,
            );
            assert!(!error.is_empty());
            assert!(host.0.lock().unwrap().built.is_empty());
        }
    }

    #[test]
    fn device_enumeration_failure_does_not_start_any_output() {
        let host = Host::new(&["A"]);
        host.0.lock().unwrap().fail_enumeration = true;
        assert_eq!(
            run_failure(
                &host,
                ids(&["A"]),
                Arc::new(PlaybackControls::new(0.0, 1.0)),
                false
            ),
            "enumeration failed"
        );
        assert!(host.0.lock().unwrap().built.is_empty());
    }

    #[test]
    fn cancellation_during_build_does_not_open_later_outputs() {
        let host = Host::new(&["A", "B"]);
        host.0.lock().unwrap().cancel_on_build = Some("A".into());
        let controls = Arc::new(PlaybackControls::new(0.0, 1.0));
        assert_eq!(
            run_failure(&host, ids(&["A", "B"]), controls.clone(), false),
            "Preview cancelled"
        );
        let state = host.0.lock().unwrap();
        assert_eq!(state.built, ["A"]);
        assert!(state.started.is_empty());
        assert_eq!(state.dropped, ["A"]);
        assert!(controls.stopped.load(Ordering::Acquire));
    }

    #[test]
    fn failed_second_build_drops_first_stream_without_starting_any_output() {
        let host = Host::new(&["A", "B"]);
        host.0.lock().unwrap().fail_build = Some("B".into());
        let controls = Arc::new(PlaybackControls::new(0.0, 1.0));
        assert_eq!(
            run_failure(&host, ids(&["A", "B"]), controls.clone(), false),
            "build failed"
        );
        let state = host.0.lock().unwrap();
        assert_eq!(state.built, ["A", "B"]);
        assert!(state.started.is_empty());
        assert_eq!(state.dropped, ["A"]);
        assert!(controls.paused.load(Ordering::Acquire));
    }

    #[test]
    fn failed_second_start_keeps_all_outputs_paused_and_drops_them() {
        let host = Host::new(&["A", "B"]);
        host.0.lock().unwrap().fail_start = Some("B".into());
        let controls = Arc::new(PlaybackControls::new(0.0, 1.0));
        assert_eq!(
            run_failure(&host, ids(&["A", "B"]), controls.clone(), false),
            "start failed"
        );
        let state = host.0.lock().unwrap();
        assert_eq!(state.started, ["A", "B"]);
        assert_eq!(state.dropped.len(), 2);
        assert!(controls.paused.load(Ordering::Acquire));
    }

    #[test]
    fn cancelled_or_abandoned_start_never_unpauses_outputs() {
        for cancel_on_start in [true, false] {
            let host = Host::new(&["A"]);
            host.0.lock().unwrap().cancel_on_start = cancel_on_start;
            let controls = Arc::new(PlaybackControls::new(0.0, 1.0));
            assert_eq!(
                run_failure(&host, vec![], controls.clone(), !cancel_on_start),
                "Preview cancelled"
            );
            assert!(controls.stopped.load(Ordering::Acquire));
            assert!(controls.paused.load(Ordering::Acquire));
            assert_eq!(host.0.lock().unwrap().dropped, ["A"]);
        }
    }

    struct Worker {
        controls: Arc<PlaybackControls>,
        wake: mpsc::Sender<()>,
        thread: Option<std::thread::JoinHandle<Result<(), String>>>,
    }
    impl Drop for Worker {
        fn drop(&mut self) {
            self.controls.stop();
            let _ = self.wake.send(());
            if let Some(thread) = self.thread.take() {
                let _ = thread.join();
            }
        }
    }
    fn launch(
        host: Host,
        selected: Vec<String>,
    ) -> (
        Worker,
        StreamQueues,
        tokio::sync::oneshot::Receiver<Result<PlaybackSession, String>>,
        mpsc::Receiver<(&'static str, f64, f64)>,
    ) {
        let controls = Arc::new(PlaybackControls::new(0.0, 1.0));
        let queues: StreamQueues = Arc::new(Mutex::new(Vec::new()));
        let (wake_tx, wake_rx) = mpsc::channel();
        let (sender, receiver) = tokio::sync::oneshot::channel();
        let (event_tx, events) = mpsc::channel();
        let worker_controls = controls.clone();
        let worker_queues = queues.clone();
        let thread = std::thread::spawn(move || {
            let mut ready = Some(sender);
            run_pcm_session_on_host(
                &host,
                "preview",
                8000,
                selected,
                worker_controls,
                worker_queues,
                wake_rx,
                &mut ready,
                |state, position, duration| {
                    let _ = event_tx.send((state, position, duration));
                },
            )
        });
        (
            Worker {
                controls,
                wake: wake_tx,
                thread: Some(thread),
            },
            queues,
            receiver,
            events,
        )
    }

    fn wait_ready(
        mut receiver: tokio::sync::oneshot::Receiver<Result<PlaybackSession, String>>,
    ) -> Result<PlaybackSession, String> {
        let deadline = Instant::now() + Duration::from_secs(3);
        loop {
            match receiver.try_recv() {
                Ok(result) => return result,
                Err(tokio::sync::oneshot::error::TryRecvError::Empty)
                    if Instant::now() < deadline =>
                {
                    std::thread::sleep(Duration::from_millis(5))
                }
                Err(error) => panic!("preview did not report readiness: {error}"),
            }
        }
    }

    #[test]
    fn two_output_preview_starts_then_finishes_only_after_both_queues_drain() {
        let host = Host::new(&["A", "B"]);
        let (mut worker, queues, ready, events) = launch(host.clone(), ids(&["B", "A"]));
        assert_eq!(wait_ready(ready).unwrap().session_id, "preview");
        assert!(!worker.controls.paused.load(Ordering::Acquire));
        {
            let queues = queues.lock().unwrap();
            assert_eq!(queues.len(), 2);
            for queue in queues.iter() {
                let mut queue = queue.lock().unwrap();
                queue.append(&[0.25, 0.5]).unwrap();
                queue.finished = true;
            }
            let mut first = queues[0].lock().unwrap();
            first.render(&mut [0.0; 2], 8000, 1, |sample| sample);
        }
        worker.wake.send(()).unwrap();
        if let Ok(event) = events.recv_timeout(Duration::from_millis(150)) {
            assert_ne!(
                event.0, "ended",
                "preview ended while an output still had queued samples"
            );
        }
        assert!(!queues.lock().unwrap()[1].lock().unwrap().ended());
        assert!(
            !worker.thread.as_ref().unwrap().is_finished(),
            "preview worker ended before every output drained"
        );
        {
            let queues = queues.lock().unwrap();
            queues[1]
                .lock()
                .unwrap()
                .render(&mut [0.0; 2], 8000, 1, |sample| sample);
        }
        worker.wake.send(()).unwrap();
        loop {
            let event = events.recv_timeout(Duration::from_secs(2)).unwrap();
            if event.0 == "ended" {
                assert!(event.1 > 0.0);
                break;
            }
        }
        worker.thread.take().unwrap().join().unwrap().unwrap();
        let state = host.0.lock().unwrap();
        assert_eq!(state.built, ["A", "B"]);
        assert_eq!(state.started, ["A", "B"]);
        assert_eq!(state.dropped.len(), 2);
    }

    #[test]
    fn default_preview_stops_when_default_output_changes() {
        let host = Host::new(&["A", "B"]);
        let (mut worker, _queues, ready, _events) = launch(host.clone(), vec![]);
        assert!(wait_ready(ready).is_ok());
        host.0.lock().unwrap().default = Some("B".into());
        let deadline = Instant::now() + Duration::from_secs(3);
        while !worker.thread.as_ref().unwrap().is_finished() && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(10));
        }
        assert!(
            worker.thread.as_ref().unwrap().is_finished(),
            "default route change did not stop preview"
        );
        let result = worker.thread.take().unwrap().join().unwrap();
        assert!(result.unwrap_err().contains("Audio output changed"));
        assert_eq!(host.0.lock().unwrap().dropped, ["A"]);
    }

    #[test]
    fn selected_preview_stops_with_error_when_a_device_disappears() {
        let host = Host::new(&["A", "B"]);
        let (mut worker, _queues, ready, _events) = launch(host.clone(), ids(&["A", "B"]));
        assert!(wait_ready(ready).is_ok());
        host.0.lock().unwrap().names.retain(|name| name != "B");
        let deadline = Instant::now() + Duration::from_secs(3);
        while !worker.thread.as_ref().unwrap().is_finished() && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(10));
        }
        assert!(
            worker.thread.as_ref().unwrap().is_finished(),
            "route loss did not stop preview"
        );
        let result = worker.thread.take().unwrap().join().unwrap();
        assert!(result.unwrap_err().contains("disconnected"));
        assert_eq!(host.0.lock().unwrap().dropped.len(), 2);
    }
}

#[cfg(test)]
mod clip_host_integration_tests {
    use super::*;

    #[derive(Default)]
    struct State {
        names: Vec<String>,
        fail_enumeration: bool,
        fail_config: Option<String>,
        fail_build: Option<String>,
        fail_start: Option<String>,
        cancel_on_build: Option<String>,
        configured: Vec<String>,
        built: Vec<String>,
        started: Vec<String>,
        dropped: Vec<String>,
        positions: Vec<Arc<RenderPosition>>,
        samples: Vec<Arc<[f32]>>,
        errors: Vec<mpsc::Sender<String>>,
        enumerations: usize,
        invalid_config: bool,
    }
    #[derive(Clone)]
    struct Host(Arc<Mutex<State>>);
    impl Host {
        fn new(names: &[&str]) -> Self {
            Self(Arc::new(Mutex::new(State {
                names: names.iter().map(|n| (*n).into()).collect(),
                ..Default::default()
            })))
        }
    }
    struct Stream {
        name: String,
        state: Arc<Mutex<State>>,
        controls: Arc<PlaybackControls>,
    }
    impl Drop for Stream {
        fn drop(&mut self) {
            self.state.lock().unwrap().dropped.push(self.name.clone());
        }
    }
    impl PreviewHost for Host {
        type Device = String;
        type Stream = Stream;
        fn default_device(&self) -> Option<String> {
            self.0.lock().unwrap().names.first().cloned()
        }
        fn devices(&self) -> Result<Vec<String>, String> {
            let mut state = self.0.lock().unwrap();
            state.enumerations += 1;
            if state.fail_enumeration {
                Err("enumeration failed".into())
            } else {
                Ok(state.names.clone())
            }
        }
        fn name(&self, device: &String) -> Result<String, String> {
            Ok(device.clone())
        }
        fn build(
            &self,
            _: &String,
            _: Arc<Mutex<crate::audio_stream::PcmQueue>>,
            _: Arc<PlaybackControls>,
            _: mpsc::Sender<String>,
        ) -> Result<Stream, String> {
            panic!("preview build is not used by clip playback")
        }
        fn start(&self, stream: &Stream) -> Result<(), String> {
            assert!(
                stream.controls.paused.load(Ordering::Acquire),
                "outputs must remain paused until all start"
            );
            let mut state = self.0.lock().unwrap();
            state.started.push(stream.name.clone());
            if state.fail_start.as_ref() == Some(&stream.name) {
                return Err("start failed".into());
            }
            Ok(())
        }
    }
    impl ClipHost for Host {
        fn clip_config(&self, device: &String) -> Result<(StreamConfig, SampleFormat), String> {
            let mut state = self.0.lock().unwrap();
            state.configured.push(device.clone());
            if state.fail_config.as_ref() == Some(device) {
                return Err("config failed".into());
            }
            Ok((
                StreamConfig {
                    channels: if state.invalid_config { 0 } else { 1 },
                    sample_rate: cpal::SampleRate(8000),
                    buffer_size: cpal::BufferSize::Default,
                },
                SampleFormat::F32,
            ))
        }
        fn build_clip(
            &self,
            device: &String,
            _: &StreamConfig,
            _: SampleFormat,
            pcm: Arc<[f32]>,
            controls: Arc<PlaybackControls>,
            position: Arc<RenderPosition>,
            errors: mpsc::Sender<String>,
        ) -> Result<Stream, String> {
            let mut state = self.0.lock().unwrap();
            state.built.push(device.clone());
            if state.cancel_on_build.as_ref() == Some(device) {
                controls.stop();
            }
            if state.fail_build.as_ref() == Some(device) {
                return Err("build failed".into());
            }
            state.positions.push(position);
            state.samples.push(pcm);
            state.errors.push(errors);
            Ok(Stream {
                name: device.clone(),
                state: self.0.clone(),
                controls,
            })
        }
    }
    fn wav() -> Vec<u8> {
        let mut output = std::io::Cursor::new(Vec::new());
        {
            let mut writer = hound::WavWriter::new(
                &mut output,
                hound::WavSpec {
                    channels: 1,
                    sample_rate: 8000,
                    bits_per_sample: 16,
                    sample_format: hound::SampleFormat::Int,
                },
            )
            .unwrap();
            for sample in 0..800 {
                writer.write_sample(sample as i16).unwrap();
            }
            writer.finalize().unwrap();
        }
        output.into_inner()
    }
    fn ids(names: &[&str]) -> Vec<String> {
        names.iter().map(|n| device_id(n)).collect()
    }
    fn failed_start(
        host: &Host,
        data: Vec<u8>,
        selected: Vec<String>,
        controls: Arc<PlaybackControls>,
        abandoned: bool,
    ) -> String {
        let (_wake_tx, wake) = mpsc::channel();
        let (tx, rx) = tokio::sync::oneshot::channel();
        let _receiver = if abandoned {
            drop(rx);
            None
        } else {
            Some(rx)
        };
        let mut ready = Some(tx);
        run_session_on_host(
            host,
            "clip",
            data,
            selected,
            controls,
            wake,
            &mut ready,
            |_, _, _| panic!("failed startup must not emit playback"),
        )
        .unwrap_err()
    }

    #[test]
    fn invalid_audio_is_rejected_before_device_enumeration() {
        let host = Host::new(&["A"]);
        assert!(failed_start(
            &host,
            b"not audio".to_vec(),
            ids(&["A"]),
            Arc::new(PlaybackControls::new(0.0, 1.0)),
            false
        )
        .contains("audio"));
        assert_eq!(host.0.lock().unwrap().enumerations, 0);
    }

    #[test]
    fn unavailable_and_ambiguous_outputs_never_open_streams() {
        for (names, selected) in [
            (vec!["A"], ids(&["missing"])),
            (vec!["USB Audio", "USB_Audio"], ids(&["USB Audio"])),
            (vec!["A"], vec![]),
        ] {
            let host = Host::new(&names);
            assert!(!failed_start(
                &host,
                wav(),
                selected,
                Arc::new(PlaybackControls::new(0.0, 1.0)),
                false
            )
            .is_empty());
            assert!(host.0.lock().unwrap().configured.is_empty());
        }
    }

    #[test]
    fn enumeration_and_invalid_config_fail_without_opening_streams() {
        let host = Host::new(&["A"]);
        host.0.lock().unwrap().fail_enumeration = true;
        assert!(failed_start(
            &host,
            wav(),
            ids(&["A"]),
            Arc::new(PlaybackControls::new(0.0, 1.0)),
            false
        )
        .contains("enumeration failed"));
        let host = Host::new(&["A"]);
        host.0.lock().unwrap().invalid_config = true;
        assert!(failed_start(
            &host,
            wav(),
            ids(&["A"]),
            Arc::new(PlaybackControls::new(0.0, 1.0)),
            false
        )
        .contains("Invalid PCM"));
        assert!(host.0.lock().unwrap().built.is_empty());
    }

    #[test]
    fn failed_second_config_or_build_drops_first_without_starting() {
        for config_failure in [true, false] {
            let host = Host::new(&["A", "B"]);
            {
                let mut state = host.0.lock().unwrap();
                if config_failure {
                    state.fail_config = Some("B".into());
                } else {
                    state.fail_build = Some("B".into());
                }
            }
            let controls = Arc::new(PlaybackControls::new(0.0, 1.0));
            let error = failed_start(&host, wav(), ids(&["A", "B"]), controls.clone(), false);
            assert_eq!(
                error,
                if config_failure {
                    "config failed"
                } else {
                    "build failed"
                }
            );
            let state = host.0.lock().unwrap();
            assert!(state.started.is_empty());
            assert_eq!(state.dropped, ["A"]);
            assert!(controls.paused.load(Ordering::Acquire));
        }
    }

    #[test]
    fn failed_second_start_keeps_all_outputs_paused_and_releases_them() {
        let host = Host::new(&["A", "B"]);
        host.0.lock().unwrap().fail_start = Some("B".into());
        let controls = Arc::new(PlaybackControls::new(0.0, 1.0));
        assert!(
            failed_start(&host, wav(), ids(&["A", "B"]), controls.clone(), false)
                .contains("start failed")
        );
        let state = host.0.lock().unwrap();
        assert_eq!(state.started, ["A", "B"]);
        assert_eq!(state.dropped.len(), 2);
        assert!(controls.paused.load(Ordering::Acquire));
    }

    #[test]
    fn cancellation_during_build_and_abandoned_ack_never_unpause() {
        for cancel_build in [true, false] {
            let host = Host::new(&["A", "B"]);
            if cancel_build {
                host.0.lock().unwrap().cancel_on_build = Some("A".into());
            }
            let controls = Arc::new(PlaybackControls::new(0.0, 1.0));
            assert_eq!(
                failed_start(
                    &host,
                    wav(),
                    ids(&["A", "B"]),
                    controls.clone(),
                    !cancel_build
                ),
                "Playback cancelled"
            );
            assert!(controls.stopped.load(Ordering::Acquire));
            assert!(controls.paused.load(Ordering::Acquire));
            let state = host.0.lock().unwrap();
            if cancel_build {
                assert_eq!(state.built, ["A"]);
                assert!(state.started.is_empty());
            } else {
                assert_eq!(state.started, ["A", "B"]);
            }
        }
    }

    struct Worker {
        controls: Arc<PlaybackControls>,
        wake: mpsc::Sender<()>,
        thread: Option<std::thread::JoinHandle<Result<(), String>>>,
    }
    impl Drop for Worker {
        fn drop(&mut self) {
            self.controls.stop();
            let _ = self.wake.send(());
            if let Some(thread) = self.thread.take() {
                let _ = thread.join();
            }
        }
    }
    fn launch(
        host: Host,
    ) -> (
        Worker,
        tokio::sync::oneshot::Receiver<Result<PlaybackSession, String>>,
        mpsc::Receiver<(&'static str, f64, f64)>,
    ) {
        let controls = Arc::new(PlaybackControls::new(0.03, 1.0));
        let (wake_tx, wake_rx) = mpsc::channel();
        let (tx, rx) = tokio::sync::oneshot::channel();
        let (event_tx, events) = mpsc::channel();
        let worker_controls = controls.clone();
        let thread = std::thread::spawn(move || {
            let mut ready = Some(tx);
            run_session_on_host(
                &host,
                "clip",
                wav(),
                ids(&["A", "B"]),
                worker_controls,
                wake_rx,
                &mut ready,
                |state, position, duration| {
                    let _ = event_tx.send((state, position, duration));
                },
            )
        });
        (
            Worker {
                controls,
                wake: wake_tx,
                thread: Some(thread),
            },
            rx,
            events,
        )
    }
    fn wait_ready(
        mut receiver: tokio::sync::oneshot::Receiver<Result<PlaybackSession, String>>,
    ) -> PlaybackSession {
        let deadline = Instant::now() + Duration::from_secs(3);
        loop {
            match receiver.try_recv() {
                Ok(result) => return result.unwrap(),
                Err(tokio::sync::oneshot::error::TryRecvError::Empty)
                    if Instant::now() < deadline =>
                {
                    std::thread::sleep(Duration::from_millis(5))
                }
                Err(error) => panic!("clip did not report readiness: {error}"),
            }
        }
    }

    #[test]
    fn clip_waits_for_both_outputs_and_reuses_matching_pcm() {
        let host = Host::new(&["A", "B"]);
        let (mut worker, ready, events) = launch(host.clone());
        let session = wait_ready(ready);
        assert_eq!(session.session_id, "clip");
        assert!((session.duration - 0.1).abs() < 1e-6);
        assert!((session.position - 0.03).abs() < 1e-6);
        assert_eq!(
            events.recv_timeout(Duration::from_secs(2)).unwrap().0,
            "playing"
        );
        let positions = {
            let state = host.0.lock().unwrap();
            assert_eq!(state.started, ["A", "B"]);
            assert!(Arc::ptr_eq(&state.samples[0], &state.samples[1]));
            state.positions.clone()
        };
        let revision = worker.controls.revision();
        positions[0].frame.store(800, Ordering::Release);
        positions[0].revision.store(revision, Ordering::Release);
        worker.wake.send(()).unwrap();
        std::thread::sleep(Duration::from_millis(100));
        assert!(
            !worker.thread.as_ref().unwrap().is_finished(),
            "clip ended before second output finished"
        );
        positions[1].frame.store(800, Ordering::Release);
        positions[1].revision.store(revision, Ordering::Release);
        worker.wake.send(()).unwrap();
        loop {
            let event = events.recv_timeout(Duration::from_secs(2)).unwrap();
            if event.0 == "ended" {
                assert!((event.1 - 0.1).abs() < 1e-6);
                break;
            }
        }
        worker.thread.take().unwrap().join().unwrap().unwrap();
        assert_eq!(host.0.lock().unwrap().dropped.len(), 2);
    }

    #[test]
    fn output_failure_after_ready_returns_error_and_releases_streams() {
        let host = Host::new(&["A", "B"]);
        let (mut worker, ready, events) = launch(host.clone());
        let _session = wait_ready(ready);
        assert_eq!(
            events.recv_timeout(Duration::from_secs(2)).unwrap().0,
            "playing"
        );
        host.0.lock().unwrap().errors[1]
            .send("device failed".into())
            .unwrap();
        worker.wake.send(()).unwrap();
        let deadline = Instant::now() + Duration::from_secs(3);
        while !worker.thread.as_ref().unwrap().is_finished() && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(5));
        }
        assert!(worker.thread.as_ref().unwrap().is_finished());
        assert!(worker
            .thread
            .take()
            .unwrap()
            .join()
            .unwrap()
            .unwrap_err()
            .contains("device failed"));
        assert_eq!(host.0.lock().unwrap().dropped.len(), 2);
    }
}
