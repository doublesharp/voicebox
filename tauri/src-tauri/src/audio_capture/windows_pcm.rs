//! Decode the packet format reported by a Windows loopback device.

#[derive(Clone, Copy, Debug)]
pub(super) enum SampleKind {
    Float,
    Integer,
}

fn sample_width(kind: SampleKind, bits: u16) -> Option<usize> {
    match (kind, bits) {
        (SampleKind::Float, 32) | (SampleKind::Integer, 32) => Some(4),
        (SampleKind::Float, 64) => Some(8),
        (SampleKind::Integer, 8) => Some(1),
        (SampleKind::Integer, 16) => Some(2),
        (SampleKind::Integer, 24) => Some(3),
        _ => None,
    }
}

pub(super) fn validate_format(
    channels: usize,
    bits: u16,
    valid_bits: u16,
    block_align: usize,
    kind: SampleKind,
) -> Result<usize, String> {
    let width = sample_width(kind, bits).ok_or("Unsupported Windows capture sample format")?;
    if channels == 0
        || valid_bits == 0
        || valid_bits > bits
        || matches!(kind, SampleKind::Float) && valid_bits != bits
        || channels.checked_mul(width) != Some(block_align)
    {
        return Err("Invalid Windows capture format".into());
    }
    Ok(width)
}

pub(super) fn decode_packet(
    data: &[u8],
    frames: usize,
    channels: usize,
    bits: u16,
    kind: SampleKind,
    silent: bool,
) -> Result<Vec<f32>, String> {
    let bytes_per_sample =
        sample_width(kind, bits).ok_or("Unsupported Windows capture sample format")?;
    if channels == 0 {
        return Err("Invalid Windows capture channel count".into());
    }
    let sample_count = frames
        .checked_mul(channels)
        .ok_or("Capture packet is too large")?;
    let byte_count = sample_count
        .checked_mul(bytes_per_sample)
        .ok_or("Capture packet is too large")?;
    if data.len() < byte_count {
        return Err("Capture packet is truncated".into());
    }
    let mut result = Vec::new();
    result
        .try_reserve_exact(sample_count)
        .map_err(|_| "Capture packet is too large")?;
    if silent {
        result.resize(sample_count, 0.0);
        return Ok(result);
    }
    for sample in data[..byte_count].chunks_exact(bytes_per_sample) {
        let value = match (kind, bits) {
            (SampleKind::Float, 32) => f32::from_le_bytes(sample.try_into().unwrap()),
            (SampleKind::Float, 64) => f64::from_le_bytes(sample.try_into().unwrap()) as f32,
            (SampleKind::Integer, 8) => (f32::from(sample[0]) - 128.0) / 128.0,
            (SampleKind::Integer, 16) => {
                i16::from_le_bytes(sample.try_into().unwrap()) as f32 / 32768.0
            }
            (SampleKind::Integer, 24) => {
                let signed = i32::from_le_bytes([0, sample[0], sample[1], sample[2]]) >> 8;
                signed as f32 / 8388608.0
            }
            (SampleKind::Integer, 32) => {
                i32::from_le_bytes(sample.try_into().unwrap()) as f32 / 2147483648.0
            }
            _ => unreachable!(),
        };
        if !value.is_finite() {
            return Err("Capture contains non-finite samples".into());
        }
        result.push(value);
    }
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn validates_device_format_before_recording() {
        for (kind, bits, valid, channels, block, width) in [
            (SampleKind::Float, 32, 32, 2, 8, 4),
            (SampleKind::Integer, 16, 16, 2, 4, 2),
            (SampleKind::Integer, 32, 24, 2, 8, 4),
            (SampleKind::Integer, 24, 24, 1, 3, 3),
        ] {
            assert_eq!(
                validate_format(channels, bits, valid, block, kind).unwrap(),
                width
            );
        }
        for (kind, bits, valid, channels, block) in [
            (SampleKind::Float, 16, 16, 1, 2),
            (SampleKind::Float, 32, 24, 1, 4),
            (SampleKind::Integer, 32, 0, 1, 4),
            (SampleKind::Integer, 16, 24, 1, 2),
            (SampleKind::Integer, 24, 24, 2, 8),
            (SampleKind::Integer, 16, 16, 0, 0),
            (SampleKind::Integer, 16, 16, usize::MAX, 0),
        ] {
            assert!(validate_format(channels, bits, valid, block, kind).is_err());
        }
    }

    #[test]
    fn decodes_float_and_integer_stereo_without_changing_channel_order() {
        let float: Vec<_> = [0.5_f32, -0.5]
            .into_iter()
            .flat_map(f32::to_le_bytes)
            .collect();
        assert_eq!(
            decode_packet(&float, 1, 2, 32, SampleKind::Float, false).unwrap(),
            [0.5, -0.5]
        );
        let int16: Vec<_> = [16384_i16, -16384]
            .into_iter()
            .flat_map(i16::to_le_bytes)
            .collect();
        assert_eq!(
            decode_packet(&int16, 1, 2, 16, SampleKind::Integer, false).unwrap(),
            [0.5, -0.5]
        );
        let int32: Vec<_> = [1073741824_i32, -1073741824]
            .into_iter()
            .flat_map(i32::to_le_bytes)
            .collect();
        assert_eq!(
            decode_packet(&int32, 1, 2, 32, SampleKind::Integer, false).unwrap(),
            [0.5, -0.5]
        );
    }

    #[test]
    fn decodes_packed_24_bit_and_unsigned_8_bit_pcm() {
        assert_eq!(
            decode_packet(&[0, 0, 64, 0, 0, 192], 1, 2, 24, SampleKind::Integer, false).unwrap(),
            [0.5, -0.5]
        );
        assert_eq!(
            decode_packet(&[0, 128, 255], 3, 1, 8, SampleKind::Integer, false).unwrap(),
            [-1.0, 0.0, 127.0 / 128.0]
        );
    }

    #[test]
    fn silent_packets_contribute_silence_not_their_buffer_contents() {
        let bytes: Vec<_> = [0.75_f32, -0.75]
            .into_iter()
            .flat_map(f32::to_le_bytes)
            .collect();
        assert_eq!(
            decode_packet(&bytes, 1, 2, 32, SampleKind::Float, true).unwrap(),
            [0.0, 0.0]
        );
    }

    #[test]
    fn truncated_packets_and_invalid_formats_are_rejected() {
        for (bytes, frames, channels, bits, kind) in [
            (vec![0; 3], 1, 1, 32, SampleKind::Float),
            (vec![0; 2], 1, 0, 16, SampleKind::Integer),
            (vec![0; 2], 1, 1, 16, SampleKind::Float),
            (vec![0; 5], 1, 1, 40, SampleKind::Integer),
        ] {
            assert!(decode_packet(&bytes, frames, channels, bits, kind, false).is_err());
        }
    }

    #[test]
    fn handles_signed_endpoints_and_24_valid_bits_in_a_32_bit_container() {
        let int16: Vec<_> = [i16::MIN, i16::MAX]
            .into_iter()
            .flat_map(i16::to_le_bytes)
            .collect();
        assert_eq!(
            decode_packet(&int16, 2, 1, 16, SampleKind::Integer, false).unwrap(),
            [-1.0, 32767.0 / 32768.0]
        );
        // Windows extensible PCM stores 24 valid bits left-aligned in 32 bits.
        let left_aligned: Vec<_> = [0x40000000_i32, -0x40000000]
            .into_iter()
            .flat_map(i32::to_le_bytes)
            .collect();
        assert_eq!(
            decode_packet(&left_aligned, 1, 2, 32, SampleKind::Integer, false).unwrap(),
            [0.5, -0.5]
        );
        assert_eq!(
            decode_packet(
                &[0, 0, 0x80, 0xff, 0xff, 0x7f],
                2,
                1,
                24,
                SampleKind::Integer,
                false
            )
            .unwrap(),
            [-1.0, 8388607.0 / 8388608.0]
        );
    }

    #[test]
    fn float64_and_padding_follow_the_reported_frame_count() {
        let values: Vec<_> = [0.25_f64, -0.5, 0.75]
            .into_iter()
            .flat_map(f64::to_le_bytes)
            .collect();
        assert_eq!(
            decode_packet(&values, 1, 2, 64, SampleKind::Float, false).unwrap(),
            [0.25, -0.5]
        );
        assert!(
            decode_packet(&f64::MAX.to_le_bytes(), 1, 1, 64, SampleKind::Float, false).is_err()
        );
        assert!(decode_packet(&[], 0, 2, 32, SampleKind::Float, false)
            .unwrap()
            .is_empty());
    }

    #[test]
    fn silence_ignores_nonfinite_samples_but_rejects_missing_frames() {
        let mut data = f32::NAN.to_le_bytes().to_vec();
        data.extend_from_slice(&f32::INFINITY.to_le_bytes());
        assert_eq!(
            decode_packet(&data, 1, 2, 32, SampleKind::Float, true).unwrap(),
            [0.0, 0.0]
        );
        assert!(decode_packet(&data[..4], 1, 2, 32, SampleKind::Float, true).is_err());
        assert!(decode_packet(&[], usize::MAX, 2, 32, SampleKind::Float, false).is_err());
    }

    #[tokio::test]
    async fn decoded_integer_packet_reaches_the_portable_wav_output() {
        use base64::Engine;
        let state = super::super::AudioCaptureState::new();
        let (capture, _rx, _timer, done) = state.begin_portable_capture();
        let bytes: Vec<_> = [16384_i16, -16384, 8192, -8192]
            .into_iter()
            .flat_map(i16::to_le_bytes)
            .collect();
        capture
            .samples
            .lock()
            .unwrap()
            .extend(decode_packet(&bytes, 2, 2, 16, SampleKind::Integer, false).unwrap());
        *capture.sample_rate.lock().unwrap() = 48000;
        *capture.channels.lock().unwrap() = 2;
        done.send(Ok(())).unwrap();
        let encoded = super::super::stop_portable_capture(&state).await.unwrap();
        let wav = base64::engine::general_purpose::STANDARD
            .decode(encoded)
            .unwrap();
        let mut reader = hound::WavReader::new(std::io::Cursor::new(wav)).unwrap();
        assert_eq!(
            (reader.spec().sample_rate, reader.spec().channels),
            (48000, 2)
        );
        assert_eq!(
            reader
                .samples::<i16>()
                .map(Result::unwrap)
                .collect::<Vec<_>>(),
            [16383, -16383, 8191, -8191]
        );
    }

    #[test]
    fn nonfinite_floats_are_not_returned_as_valid_capture() {
        for value in [f32::NAN, f32::INFINITY, f32::NEG_INFINITY] {
            assert!(
                decode_packet(&value.to_le_bytes(), 1, 1, 32, SampleKind::Float, false).is_err()
            );
        }
    }
}
