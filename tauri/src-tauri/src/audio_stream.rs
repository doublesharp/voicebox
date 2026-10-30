//! Bounded mono source PCM queue; resampling retains its phase across chunks.
use std::collections::VecDeque;

pub struct PcmQueue {
    samples: VecDeque<f32>,
    pub rate: u32,
    phase: f64,
    pub rendered: u64,
    pub finished: bool,
}
impl PcmQueue {
    pub fn new(rate: u32) -> Result<Self, String> {
        if !(8000..=192000).contains(&rate) {
            return Err("Invalid preview sample rate".into());
        }
        Ok(Self {
            samples: VecDeque::with_capacity(rate as usize * 2),
            rate,
            phase: 0.0,
            rendered: 0,
            finished: false,
        })
    }
    #[cfg(test)]
    pub fn samples_len_for_test(&self) -> usize {
        self.samples.len()
    }

    pub fn can_append(&self, frames: usize) -> bool {
        !self.finished && self.samples.len() + frames <= self.rate as usize * 2
    }
    pub fn append(&mut self, samples: &[f32]) -> Result<(), String> {
        if !self.can_append(samples.len()) {
            return Err("Preview queue is full or finished".into());
        }
        self.samples.extend(samples);
        Ok(())
    }
    pub fn ended(&self) -> bool {
        self.finished && self.samples.is_empty()
    }
    pub fn position(&self) -> f64 {
        self.rendered as f64 / self.rate as f64
    }
    pub fn render<T: Copy>(
        &mut self,
        output: &mut [T],
        output_rate: u32,
        channels: usize,
        convert: impl Fn(f32) -> T,
    ) {
        output.fill(convert(0.0));
        if output_rate == 0 || channels == 0 {
            return;
        }
        let step = self.rate as f64 / output_rate as f64;
        for frame in output.chunks_mut(channels) {
            // Retain the final sample until the next chunk, so splits do not
            // introduce discontinuities. Underruns are silence, never EOF.
            if self.samples.is_empty()
                || (!self.finished
                    && self.samples.len() < 2.max((self.phase + step).ceil() as usize))
            {
                break;
            }
            let first = self.samples[0];
            let second = self.samples.get(1).copied().unwrap_or(first);
            let sample = first + (second - first) * self.phase as f32;
            frame.fill(convert(sample));
            self.phase += step;
            let advance = (self.phase.floor() as usize).min(self.samples.len());
            for _ in 0..advance {
                self.samples.pop_front();
            }
            self.rendered += advance as u64;
            self.phase -= advance as f64;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn bounded_queue_renders_before_finish_and_recovers_after_underrun() {
        let mut q = PcmQueue::new(24000).unwrap();
        q.append(&[0.25, 0.5, 0.75]).unwrap();
        let mut out = [0.0; 4];
        q.render(&mut out, 24000, 1, |s| s);
        assert_eq!(out, [0.25, 0.5, 0.0, 0.0]);
        assert!(!q.ended());
        q.append(&[1.0]).unwrap();
        q.render(&mut out, 24000, 1, |s| s);
        assert_eq!(out[0], 0.75);
        q.finished = true;
        q.render(&mut out, 24000, 1, |s| s);
        assert_eq!(out[0], 1.0);
        assert!(q.ended());
        assert!(q.append(&[1.0]).is_err());
        let mut q = PcmQueue::new(8000).unwrap();
        q.append(&vec![0.5; 16000]).unwrap();
        assert!(q.append(&[0.5]).is_err());
    }
    #[test]
    fn fractional_resampling_is_continuous_across_appends_and_output_callbacks() {
        for (source, target) in [
            (24000, 48000),
            (44100, 48000),
            (48000, 24000),
            (192000, 8000),
            (16000, 44100),
        ] {
            let input: Vec<f32> = (0..100).map(|n| n as f32 / 100.0).collect();
            let mut whole = PcmQueue::new(source).unwrap();
            whole.append(&input).unwrap();
            whole.finished = true;
            let mut expected = vec![0.0; 1000];
            whole.render(&mut expected, target, 1, |s| s);
            let mut split = PcmQueue::new(source).unwrap();
            split.append(&input[..51]).unwrap();
            let mut head = vec![0.0; (50.0 * target as f64 / source as f64).floor() as usize];
            split.render(&mut head, target, 1, |s| s);
            split.append(&input[51..]).unwrap();
            split.finished = true;
            let mut tail = vec![0.0; 1000 - head.len()];
            split.render(&mut tail, target, 1, |s| s);
            head.extend(tail);
            assert_eq!(head, expected, "{source} -> {target}");
        }
        assert!(PcmQueue::new(0).is_err());
        assert!(PcmQueue::new(192001).is_err());
    }
}

#[cfg(test)]
mod stream_resampling_regression_tests {
    use super::PcmQueue;

    fn collect_complete(mut queue: PcmQueue, target: u32) -> Vec<f32> {
        let mut out = Vec::new();
        while !queue.ended() {
            assert!(out.len() < 100_000, "stream never completed");
            let mut frame = [0.0];
            queue.render(&mut frame, target, 1, |sample| sample);
            out.push(frame[0]);
        }
        out
    }

    #[test]
    fn uneven_appends_preserve_the_same_audio_across_sample_rates() {
        let input: Vec<f32> = (0..211).map(|i| 0.25 + (i % 23) as f32 / 100.0).collect();
        for (source, target) in [
            (8000, 192000),
            (16000, 44100),
            (24000, 48000),
            (44100, 48000),
            (48000, 24000),
            (192000, 8000),
        ] {
            let mut whole = PcmQueue::new(source).unwrap();
            whole.append(&input).unwrap();
            whole.finished = true;
            let expected = collect_complete(whole, target);
            for chunk_size in [1, 3, 7, 19, 64] {
                let mut stream = PcmQueue::new(source).unwrap();
                let mut actual = Vec::new();
                for chunk in input.chunks(chunk_size) {
                    stream.append(chunk).unwrap();
                    loop {
                        let mut frame = [0.0];
                        stream.render(&mut frame, target, 1, |sample| sample);
                        if frame[0] == 0.0 {
                            break;
                        }
                        actual.push(frame[0]);
                        assert!(
                            actual.len() <= expected.len(),
                            "stream produced too much audio"
                        );
                    }
                }
                stream.finished = true;
                actual.extend(collect_complete(stream, target));
                assert_eq!(actual, expected, "{source} -> {target}, chunk {chunk_size}");
            }
        }
    }

    #[test]
    fn upsampling_interpolates_and_finishes_the_final_sample() {
        let mut queue = PcmQueue::new(8000).unwrap();
        queue.append(&[0.25, 0.75]).unwrap();
        queue.finished = true;
        assert_eq!(collect_complete(queue, 16000), [0.25, 0.5, 0.75, 0.75]);
    }

    #[test]
    fn downsampling_advances_by_whole_source_frames() {
        let mut queue = PcmQueue::new(16000).unwrap();
        queue.append(&[0.1, 0.2, 0.3, 0.4]).unwrap();
        queue.finished = true;
        assert_eq!(collect_complete(queue, 8000), [0.1, 0.3]);
    }

    #[test]
    fn invalid_output_format_is_silent_and_does_not_consume_audio() {
        let mut queue = PcmQueue::new(8000).unwrap();
        queue.append(&[0.25, 0.5]).unwrap();
        for (rate, channels) in [(0, 1), (8000, 0)] {
            let mut output = [1.0; 4];
            queue.render(&mut output, rate, channels, |sample| sample);
            assert_eq!(output, [0.0; 4]);
            assert_eq!(queue.position(), 0.0);
        }
        queue.finished = true;
        assert_eq!(collect_complete(queue, 8000), [0.25, 0.5]);
    }

    #[test]
    fn output_channels_receive_the_same_sample_and_underrun_is_silence() {
        let mut queue = PcmQueue::new(8000).unwrap();
        queue.append(&[0.25, 0.5]).unwrap();
        let mut output = [1.0; 9];
        queue.render(&mut output, 8000, 3, |sample| sample);
        assert_eq!(output, [0.25, 0.25, 0.25, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]);
        assert!(!queue.ended());
        queue.finished = true;
        assert_eq!(collect_complete(queue, 8000), [0.5]);
    }

    #[test]
    fn position_tracks_consumed_source_frames_across_rate_changes() {
        let mut queue = PcmQueue::new(16000).unwrap();
        queue.append(&[0.1; 8]).unwrap();
        queue.finished = true;
        queue.render(&mut [0.0; 2], 8000, 1, |sample| sample);
        assert_eq!(queue.rendered, 4);
        assert_eq!(queue.position(), 4.0 / 16000.0);
        queue.render(&mut [0.0; 8], 32000, 1, |sample| sample);
        assert!(queue.ended());
        assert_eq!(queue.position(), 8.0 / 16000.0);
    }
}
