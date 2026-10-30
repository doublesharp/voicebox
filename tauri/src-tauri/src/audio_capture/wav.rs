//! Shared capture PCM to WAV conversion.
use hound::{WavSpec, WavWriter};
use std::io::Cursor;

pub(super) fn samples_to_wav(
    samples: &[f32],
    sample_rate: u32,
    channels: u16,
) -> Result<Vec<u8>, String> {
    if sample_rate == 0
        || channels == 0
        || channels.checked_mul(2).is_none()
        || sample_rate
            .checked_mul(2)
            .and_then(|rate| rate.checked_mul(u32::from(channels)))
            .is_none()
    {
        return Err("Invalid WAV sample rate or channel count".into());
    }
    if samples.len() % usize::from(channels) != 0 {
        return Err("Capture ends in an incomplete audio frame".into());
    }
    if samples.iter().any(|sample| !sample.is_finite()) {
        return Err("Capture contains non-finite samples".into());
    }
    let header_size = if channels > 2 { 60_u32 } else { 36_u32 };
    if samples.len() > ((u32::MAX - header_size) / 2) as usize {
        return Err("Capture is too large for WAV format".into());
    }
    let mut buffer = Vec::new();
    let spec = WavSpec {
        channels,
        sample_rate,
        bits_per_sample: 16,
        sample_format: hound::SampleFormat::Int,
    };
    let mut writer = WavWriter::new(Cursor::new(&mut buffer), spec)
        .map_err(|error| format!("Failed to create WAV writer: {error}"))?;
    for sample in samples {
        let sample = (sample.clamp(-1.0, 1.0) * 32767.0) as i16;
        writer
            .write_sample(sample)
            .map_err(|error| format!("Failed to write sample: {error}"))?;
    }
    writer
        .finalize()
        .map_err(|error| format!("Failed to finalize WAV: {error}"))?;
    Ok(buffer)
}

#[cfg(test)]
mod capture_wav_regression_tests {
    use super::samples_to_wav;
    use std::io::Cursor;

    #[test]
    fn stereo_wav_preserves_channel_order_rate_and_clipping() {
        let bytes = samples_to_wav(&[0.5, -0.5, 2.0, -2.0], 48000, 2).unwrap();
        let mut reader = hound::WavReader::new(Cursor::new(bytes)).unwrap();
        assert_eq!(
            (reader.spec().sample_rate, reader.spec().channels),
            (48000, 2)
        );
        assert_eq!(
            reader
                .samples::<i16>()
                .map(Result::unwrap)
                .collect::<Vec<_>>(),
            [16383, -16383, 32767, -32767]
        );
    }

    #[test]
    fn mono_and_empty_recordings_have_valid_headers() {
        for samples in [vec![], vec![0.0, 0.25]] {
            let bytes = samples_to_wav(&samples, 16000, 1).unwrap();
            let mut reader = hound::WavReader::new(Cursor::new(bytes)).unwrap();
            assert_eq!(
                (reader.spec().sample_rate, reader.spec().channels),
                (16000, 1)
            );
            assert_eq!(reader.samples::<i16>().count(), samples.len());
        }
    }

    #[test]
    fn incomplete_stereo_frame_is_rejected() {
        assert!(samples_to_wav(&[0.1, 0.2, 0.3], 48000, 2).is_err());
    }

    #[test]
    fn zero_channels_returns_error_instead_of_panicking() {
        let result = std::panic::catch_unwind(|| samples_to_wav(&[], 48000, 0));
        assert!(result.expect("invalid channels panicked").is_err());
    }

    #[test]
    fn zero_sample_rate_is_rejected() {
        assert!(samples_to_wav(&[0.0], 0, 1).is_err());
    }

    #[test]
    fn wav_header_arithmetic_overflow_is_rejected_without_panicking() {
        for (rate, channels) in [(u32::MAX, 2), (48000, u16::MAX)] {
            let result = std::panic::catch_unwind(|| samples_to_wav(&[], rate, channels));
            assert!(result.expect("invalid WAV dimensions panicked").is_err());
        }
    }

    #[test]
    fn nonfinite_samples_cannot_be_exported_as_silent_or_clipped_audio() {
        for invalid in [f32::NAN, f32::INFINITY, f32::NEG_INFINITY] {
            assert!(samples_to_wav(&[0.5, invalid], 48000, 2).is_err());
        }
    }
}
