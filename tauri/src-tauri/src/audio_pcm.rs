//! Device-independent PCM conversion and playback controls. No allocations or
//! locks are needed by a render callback after its immutable PCM is prepared.
use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, AtomicUsize, Ordering};

/// A windowed-sinc, per-channel resampler. The low-pass cutoff follows the
/// destination Nyquist frequency when downsampling. A small polyphase kernel
/// table avoids evaluating trigonometric functions for every output sample.
pub fn resample_interleaved(
    samples: &[f32],
    from_rate: u32,
    to_rate: u32,
    channels: usize,
) -> Result<Vec<f32>, String> {
    if from_rate == 0 || to_rate == 0 || channels == 0 || samples.len() % channels != 0 {
        return Err("Invalid PCM sample rate, channel count, or incomplete frame".into());
    }
    if from_rate == to_rate || samples.is_empty() {
        return Ok(samples.to_vec());
    }
    let frames = samples.len() / channels;
    let output_frames = ((frames as f64 * to_rate as f64 / from_rate as f64).round()) as usize;
    let mut output = vec![
        0.0;
        output_frames
            .checked_mul(channels)
            .ok_or("Audio is too large")?
    ];
    let cutoff = (to_rate as f64 / from_rate as f64).min(1.0) * 0.94;
    let radius = (24.0 / cutoff).ceil().min(512.0) as isize;
    let phases = 1024usize;
    let kernels: Vec<Vec<f64>> = (0..phases)
        .map(|phase| {
            let fraction = phase as f64 / phases as f64;
            let mut weights: Vec<f64> = (-radius + 1..=radius)
                .map(|offset| {
                    let distance = offset as f64 - fraction;
                    let x = distance / radius as f64;
                    if x.abs() >= 1.0 {
                        return 0.0;
                    }
                    let argument = std::f64::consts::PI * cutoff * distance;
                    let sinc = if argument.abs() < 1e-10 {
                        1.0
                    } else {
                        argument.sin() / argument
                    };
                    let window = 0.42
                        + 0.5 * (std::f64::consts::PI * x).cos()
                        + 0.08 * (2.0 * std::f64::consts::PI * x).cos();
                    cutoff * sinc * window
                })
                .collect();
            let sum: f64 = weights.iter().sum();
            for weight in &mut weights {
                *weight /= sum;
            }
            weights
        })
        .collect();
    for (out_frame, output_frame) in output.chunks_exact_mut(channels).enumerate() {
        let position = out_frame as f64 * from_rate as f64 / to_rate as f64;
        let mut center = position.floor() as isize;
        let mut phase = ((position - center as f64) * phases as f64).round() as usize;
        if phase == phases {
            center += 1;
            phase = 0;
        }
        let weights = &kernels[phase];
        for (channel, sample) in output_frame.iter_mut().enumerate() {
            let mut sum = 0.0;
            for (tap, weight) in weights.iter().enumerate() {
                let source_frame = center - radius + 1 + tap as isize;
                if source_frame >= 0 && (source_frame as usize) < frames {
                    sum += samples[source_frame as usize * channels + channel] as f64 * weight;
                }
            }
            *sample = sum as f32;
        }
    }
    Ok(output)
}

pub fn convert_channels(
    samples: &[f32],
    source: usize,
    destination: usize,
) -> Result<Vec<f32>, String> {
    if source == 0 || destination == 0 || samples.len() % source != 0 {
        return Err("Invalid PCM channel layout".into());
    }
    if source == destination {
        return Ok(samples.to_vec());
    }
    let mut output = Vec::with_capacity(samples.len() / source * destination);
    for frame in samples.chunks_exact(source) {
        if destination == 1 {
            output.push(frame.iter().sum::<f32>() / source as f32);
        } else if source == 1 {
            output.extend(std::iter::repeat(frame[0]).take(destination));
        } else {
            output
                .extend((0..destination).map(|channel| frame.get(channel).copied().unwrap_or(0.0)));
        }
    }
    Ok(output)
}

/// Mix a decoded speaker layout into stereo without discarding extra channels.
pub fn mix_layout_to_stereo(
    samples: &[f32],
    layout: symphonia::core::audio::Channels,
) -> Result<Vec<f32>, String> {
    use symphonia::core::audio::Channels as C;
    let count = layout.count();
    if count == 0 || samples.len() % count != 0 {
        return Err("Invalid PCM channel layout".into());
    }
    if samples.iter().any(|sample| !sample.is_finite()) {
        return Err("Audio contains non-finite samples".into());
    }
    let side = std::f64::consts::FRAC_1_SQRT_2;
    let mut weights = Vec::with_capacity(count);
    for channel in layout.iter() {
        let pair = if channel == C::FRONT_LEFT {
            (1.0, 0.0)
        } else if channel == C::FRONT_RIGHT {
            (0.0, 1.0)
        } else if [
            C::FRONT_CENTRE,
            C::TOP_CENTRE,
            C::TOP_FRONT_CENTRE,
            C::TOP_REAR_CENTRE,
            C::FRONT_CENTRE_HIGH,
        ]
        .contains(&channel)
        {
            (side, side)
        } else if [C::LFE1, C::LFE2, C::REAR_CENTRE].contains(&channel) {
            (0.5, 0.5)
        } else if [
            C::REAR_LEFT,
            C::SIDE_LEFT,
            C::FRONT_LEFT_CENTRE,
            C::TOP_FRONT_LEFT,
            C::TOP_REAR_LEFT,
            C::REAR_LEFT_CENTRE,
            C::FRONT_LEFT_WIDE,
            C::FRONT_LEFT_HIGH,
        ]
        .contains(&channel)
        {
            (side, 0.0)
        } else if [
            C::REAR_RIGHT,
            C::SIDE_RIGHT,
            C::FRONT_RIGHT_CENTRE,
            C::TOP_FRONT_RIGHT,
            C::TOP_REAR_RIGHT,
            C::REAR_RIGHT_CENTRE,
            C::FRONT_RIGHT_WIDE,
            C::FRONT_RIGHT_HIGH,
        ]
        .contains(&channel)
        {
            (0.0, side)
        } else {
            return Err("Unsupported PCM speaker layout".into());
        };
        weights.push(pair);
    }
    let capacity = (samples.len() / count)
        .checked_mul(2)
        .ok_or("Audio is too large")?;
    let mut output = Vec::with_capacity(capacity);
    for frame in samples.chunks_exact(count) {
        let (mut left, mut right) = (0.0, 0.0);
        for (&sample, &(left_gain, right_gain)) in frame.iter().zip(&weights) {
            left += f64::from(sample) * left_gain;
            right += f64::from(sample) * right_gain;
        }
        let bound = f64::from(f32::MAX);
        output.extend([
            left.clamp(-bound, bound) as f32,
            right.clamp(-bound, bound) as f32,
        ]);
    }
    Ok(output)
}

/// Each playback gets its own permanently cancellable token. Replacing a
/// session never resets the previous session's stop flag.
pub struct PlaybackControls {
    pub stopped: AtomicBool,
    pub paused: AtomicBool,
    volume: AtomicU32,
    seek_seconds: AtomicU64,
    seek_revision: AtomicUsize,
}
impl PlaybackControls {
    pub fn new(position: f64, volume: f32) -> Self {
        Self {
            stopped: AtomicBool::new(false),
            paused: AtomicBool::new(true),
            volume: AtomicU32::new(volume.clamp(0.0, 1.0).to_bits()),
            seek_seconds: AtomicU64::new(position.max(0.0).to_bits()),
            seek_revision: AtomicUsize::new(1),
        }
    }
    pub fn requested_position(&self) -> f64 {
        f64::from_bits(self.seek_seconds.load(Ordering::Acquire))
    }
    pub fn revision(&self) -> usize {
        self.seek_revision.load(Ordering::Acquire)
    }
    pub fn stop(&self) {
        self.stopped.store(true, Ordering::Release);
    }
    pub fn seek(&self, seconds: f64) {
        self.seek_seconds
            .store(seconds.max(0.0).to_bits(), Ordering::Release);
        self.seek_revision.fetch_add(1, Ordering::AcqRel);
    }
    pub fn volume(&self) -> f32 {
        f32::from_bits(self.volume.load(Ordering::Acquire))
    }
    pub fn set_volume(&self, volume: f32) {
        self.volume
            .store(volume.clamp(0.0, 1.0).to_bits(), Ordering::Release);
    }
}

#[derive(Default)]
pub struct RenderPosition {
    pub frame: AtomicUsize,
    pub revision: AtomicUsize,
}

pub struct RenderCursor {
    frame: usize,
    revision: usize,
}
impl RenderCursor {
    pub fn new() -> Self {
        Self {
            frame: 0,
            revision: 0,
        }
    }
    pub fn render<T>(
        &mut self,
        output: &mut [T],
        samples: &[f32],
        rate: u32,
        channels: usize,
        controls: &PlaybackControls,
        position: &RenderPosition,
        convert: impl Fn(f32) -> T,
    ) {
        let frames = samples.len() / channels;
        let revision = controls.seek_revision.load(Ordering::Acquire);
        if revision != self.revision {
            self.frame = ((f64::from_bits(controls.seek_seconds.load(Ordering::Acquire))
                * rate as f64) as usize)
                .min(frames);
            self.revision = revision;
        }
        if controls.stopped.load(Ordering::Acquire) || controls.paused.load(Ordering::Acquire) {
            output.iter_mut().for_each(|sample| *sample = convert(0.0));
            position.frame.store(self.frame, Ordering::Release);
            position.revision.store(self.revision, Ordering::Release);
            return;
        }
        let volume = f32::from_bits(controls.volume.load(Ordering::Acquire));
        for frame in output.chunks_mut(channels) {
            for (channel, sample) in frame.iter_mut().enumerate() {
                let value = samples
                    .get(self.frame * channels + channel)
                    .copied()
                    .unwrap_or(0.0);
                *sample = convert((value * volume).clamp(-1.0, 1.0));
            }
            self.frame = (self.frame + 1).min(frames);
        }
        position.frame.store(self.frame, Ordering::Release);
        position.revision.store(self.revision, Ordering::Release);
    }
}

#[cfg(test)]

mod tests {
    use super::*;
    #[test]
    fn resampling_keeps_stereo_channels_separate() {
        let input: Vec<f32> = (0..240).flat_map(|_| [0.4, -0.2]).collect();
        let output = resample_interleaved(&input, 24000, 48000, 2).unwrap();
        assert_eq!(output.len(), input.len() * 2);
        for frame in output.chunks_exact(2).skip(32).take(400) {
            assert!(
                (frame[0] - 0.4).abs() < 0.001,
                "left channel changed: {:?}",
                frame
            );
            assert!(
                (frame[1] + 0.2).abs() < 0.001,
                "right channel changed: {:?}",
                frame
            );
        }
    }
    #[test]
    fn downsampling_filters_out_frequencies_above_destination_nyquist() {
        let input: Vec<f32> = (0..4800)
            .map(|i| (2.0 * std::f32::consts::PI * 18000.0 * i as f32 / 48000.0).sin())
            .collect();
        let output = resample_interleaved(&input, 48000, 24000, 1).unwrap();
        let rms = (output[64..output.len() - 64]
            .iter()
            .map(|s| s * s)
            .sum::<f32>()
            / (output.len() - 128) as f32)
            .sqrt();
        assert!(rms < 0.02, "aliased 18kHz tone RMS: {rms}");
    }

    #[test]
    fn changing_sample_rate_preserves_duration_and_audible_tone() {
        let input: Vec<f32> = (0..4410)
            .map(|i| (2.0 * std::f32::consts::PI * 1000.0 * i as f32 / 44100.0).sin() * 0.5)
            .collect();
        let output = resample_interleaved(&input, 44100, 48000, 1).unwrap();
        assert_eq!(output.len(), 4800);
        let error = output
            .iter()
            .enumerate()
            .skip(64)
            .take(4600)
            .map(|(i, value)| {
                let expected =
                    (2.0 * std::f32::consts::PI * 1000.0 * i as f32 / 48000.0).sin() * 0.5;
                (value - expected).powi(2)
            })
            .sum::<f32>()
            / 4600.0;
        assert!(
            error.sqrt() < 0.001,
            "passband distortion RMS {}",
            error.sqrt()
        );
    }

    #[test]
    fn channel_conversion_downmixes_both_sides_and_preserves_mono() {
        assert_eq!(
            convert_channels(&[0.8, 0.2, -0.2, 0.2], 2, 1).unwrap(),
            vec![0.5, 0.0]
        );
        assert_eq!(
            convert_channels(&[0.25, -0.5], 1, 2).unwrap(),
            vec![0.25, 0.25, -0.5, -0.5]
        );
        assert!(convert_channels(&[0.0], 2, 1).is_err());
        assert!(resample_interleaved(&[0.0], 0, 48000, 1).is_err());
        assert!(resample_interleaved(&[0.0], 24000, 48000, 2).is_err());
    }

    #[test]
    fn cancelled_session_stays_stopped_when_replacement_begins() {
        let previous = PlaybackControls::new(0.0, 1.0);
        previous.stop();
        let replacement = PlaybackControls::new(0.0, 1.0);
        replacement.paused.store(false, Ordering::Release);
        let mut cursor = RenderCursor::new();
        let position = RenderPosition::default();
        let mut output = [1.0; 2];
        cursor.render(
            &mut output,
            &[0.5; 8],
            24000,
            1,
            &previous,
            &position,
            |value| value,
        );
        assert_eq!(output, [0.0, 0.0]);
        assert!(previous.stopped.load(Ordering::Acquire));
        assert!(!replacement.stopped.load(Ordering::Acquire));
    }

    #[test]
    fn paused_render_retains_position_and_resume_does_not_skip_audio() {
        let controls = PlaybackControls::new(0.0, 1.0);
        let position = RenderPosition::default();
        let mut cursor = RenderCursor::new();
        let mut output = [1.0; 2];
        cursor.render(
            &mut output,
            &[0.1, 0.2, 0.3, 0.4],
            24000,
            1,
            &controls,
            &position,
            |value| value,
        );
        assert_eq!(output, [0.0, 0.0]);
        assert_eq!(position.frame.load(Ordering::Acquire), 0);
        controls.paused.store(false, Ordering::Release);
        cursor.render(
            &mut output,
            &[0.1, 0.2, 0.3, 0.4],
            24000,
            1,
            &controls,
            &position,
            |value| value,
        );
        assert_eq!(output, [0.1, 0.2]);
        controls.paused.store(true, Ordering::Release);
        cursor.render(
            &mut output,
            &[0.1, 0.2, 0.3, 0.4],
            24000,
            1,
            &controls,
            &position,
            |value| value,
        );
        assert_eq!(position.frame.load(Ordering::Acquire), 2);
        controls.paused.store(false, Ordering::Release);
        cursor.render(
            &mut output,
            &[0.1, 0.2, 0.3, 0.4],
            24000,
            1,
            &controls,
            &position,
            |value| value,
        );
        assert_eq!(output, [0.3, 0.4]);
    }

    #[test]
    fn seek_is_frame_aligned_and_gain_zero_mutes_all_channels() {
        let controls = PlaybackControls::new(0.0, 1.0);
        controls.paused.store(false, Ordering::Release);
        let position = RenderPosition::default();
        let mut cursor = RenderCursor::new();
        let samples = [0.1, -0.1, 0.2, -0.2, 0.3, -0.3, 0.4, -0.4];
        let mut output = [0.0; 2];
        controls.seek(2.0 / 24000.0);
        cursor.render(
            &mut output,
            &samples,
            24000,
            2,
            &controls,
            &position,
            |value| value,
        );
        assert_eq!(output, [0.3, -0.3]);
        assert_eq!(
            position.revision.load(Ordering::Acquire),
            controls.revision()
        );
        controls.set_volume(0.0);
        cursor.render(
            &mut output,
            &samples,
            24000,
            2,
            &controls,
            &position,
            |value| value,
        );
        assert_eq!(output, [0.0, 0.0]);
        assert_eq!(position.frame.load(Ordering::Acquire), 4);
        controls.set_volume(1.0);
        cursor.render(
            &mut output,
            &samples,
            24000,
            2,
            &controls,
            &position,
            |value| value,
        );
        assert_eq!(output, [0.0, 0.0]);
    }
}

#[cfg(test)]
mod stereo_mix_numeric_regression_tests {
    use super::mix_layout_to_stereo;
    use symphonia::core::audio::Channels as C;

    fn layout() -> C {
        C::FRONT_LEFT | C::FRONT_RIGHT | C::FRONT_CENTRE | C::LFE1 | C::REAR_LEFT | C::REAR_RIGHT
    }

    #[test]
    fn finite_large_channels_do_not_overflow_during_cancellation() {
        let max = f32::MAX;
        let output = mix_layout_to_stereo(&[max, 0.0, max, 0.0, -max, 0.0], layout()).unwrap();
        assert!(
            output.iter().all(|sample| sample.is_finite()),
            "non-finite mix: {output:?}"
        );
        assert!(
            (output[0] / max - 1.0).abs() < 0.00001,
            "intermediate overflow lost cancellation: {output:?}"
        );
    }

    #[test]
    fn sums_outside_float_range_remain_finite_for_playback() {
        for extreme in [f32::MAX, -f32::MAX] {
            let output = mix_layout_to_stereo(
                &[extreme, extreme, extreme, extreme, extreme, extreme],
                layout(),
            )
            .unwrap();
            assert!(
                output.iter().all(|sample| sample.is_finite()),
                "non-finite mix: {output:?}"
            );
            assert_eq!(output, [extreme, extreme]);
        }
    }

    #[test]
    fn nonfinite_input_is_rejected_before_reaching_output() {
        for invalid in [f32::NAN, f32::INFINITY, f32::NEG_INFINITY] {
            assert!(mix_layout_to_stereo(&[0.0, 0.0, invalid, 0.0, 0.0, 0.0], layout()).is_err());
        }
    }

    #[test]
    fn normal_and_empty_frames_remain_supported() {
        assert_eq!(
            mix_layout_to_stereo(&[], layout()).unwrap(),
            Vec::<f32>::new()
        );
        assert_eq!(
            mix_layout_to_stereo(&[0.25, -0.5, 0.0, 0.0, 0.0, 0.0], layout()).unwrap(),
            [0.25, -0.5]
        );
        assert!(mix_layout_to_stereo(&[0.0], layout()).is_err());
    }
}
