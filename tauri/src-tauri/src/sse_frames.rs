//! Incremental SSE frame handling for the native speech subscriber.

// Speech events carry small metadata records, never audio payloads.
const MAX_FRAME_BYTES: usize = 1024 * 1024;

#[derive(Default)]
pub(crate) struct FrameBuffer {
    buf: Vec<u8>,
    line_start: usize,
    skip_lf: bool,
    completed_frame: bool,
}

impl FrameBuffer {
    pub(crate) fn push(&mut self, chunk: &[u8]) -> Result<Vec<(String, String)>, std::io::Error> {
        let mut result = Vec::new();
        for &byte in chunk {
            if self.skip_lf {
                self.skip_lf = false;
                if byte == b'\n' {
                    continue;
                }
            }
            if byte == b'\r' || byte == b'\n' {
                if self.line_start == self.buf.len() {
                    let frame = std::str::from_utf8(&self.buf).map_err(|error| {
                        std::io::Error::new(std::io::ErrorKind::InvalidData, error)
                    })?;
                    if !self.buf.is_empty() {
                        self.completed_frame = true;
                    }
                    if let Some(event) = parse_frame(frame) {
                        result.push(event);
                    }
                    self.buf.clear();
                    self.line_start = 0;
                } else {
                    self.push_byte(b'\n')?;
                    self.line_start = self.buf.len();
                }
                self.skip_lf = byte == b'\r';
            } else {
                self.push_byte(byte)?;
            }
        }
        Ok(result)
    }

    pub(crate) fn has_complete_frame(&self) -> bool {
        self.completed_frame
    }

    fn push_byte(&mut self, byte: u8) -> Result<(), std::io::Error> {
        if self.buf.len() >= MAX_FRAME_BYTES {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                "Speech event exceeds the maximum frame size",
            ));
        }
        self.buf.push(byte);
        Ok(())
    }
}

fn parse_frame(frame: &str) -> Option<(String, String)> {
    let mut event = None;
    let mut data_lines = Vec::new();
    for line in frame.lines() {
        if line.is_empty() || line.starts_with(':') {
            continue;
        }
        if let Some(rest) = line.strip_prefix("event:") {
            event = Some(rest.strip_prefix(' ').unwrap_or(rest).to_string());
        } else if let Some(rest) = line.strip_prefix("data:") {
            data_lines.push(rest.strip_prefix(' ').unwrap_or(rest));
        }
    }
    if data_lines.is_empty() {
        return None;
    }
    Some((event?, data_lines.join("\n")))
}

#[cfg(test)]
mod tests {
    use super::FrameBuffer;

    #[test]
    fn accepts_lf_and_crlf_frames_and_multiple_events() {
        let mut buffer = FrameBuffer::default();
        let events = buffer
            .push(b"event: speak-start\ndata: {\"id\":1}\n\nevent: speak-end\r\ndata: done\r\n\r\n")
            .unwrap();
        assert_eq!(
            events,
            [
                ("speak-start".into(), "{\"id\":1}".into()),
                ("speak-end".into(), "done".into())
            ]
        );
    }

    #[test]
    fn fragmented_separator_and_payload_wait_for_complete_frame() {
        let mut buffer = FrameBuffer::default();
        assert!(buffer
            .push(b"event: speak-start\r\ndata: hel")
            .unwrap()
            .is_empty());
        assert_eq!(
            buffer.push(b"lo\r\n\r").unwrap(),
            [("speak-start".into(), "hello".into())]
        );
        assert!(buffer.push(b"\n").unwrap().is_empty());
    }

    #[test]
    fn multibyte_utf8_can_cross_any_network_chunk_boundary() {
        let bytes = "event: speak-start\ndata: 你好 🎙️\n\n".as_bytes();
        for split in 1..bytes.len() {
            let mut buffer = FrameBuffer::default();
            let mut events = buffer.push(&bytes[..split]).unwrap();
            events.extend(buffer.push(&bytes[split..]).unwrap());
            assert_eq!(
                events,
                [("speak-start".into(), "你好 🎙️".into())],
                "split at {split}"
            );
        }
    }

    #[test]
    fn joins_multiline_data_and_ignores_heartbeat_frames() {
        let mut buffer = FrameBuffer::default();
        assert_eq!(
            buffer
                .push(b":ping\n\nevent: speak-start\ndata: first\ndata: second\n\n")
                .unwrap(),
            [("speak-start".into(), "first\nsecond".into())]
        );
    }

    #[test]
    fn event_without_data_is_not_dispatched() {
        let mut buffer = FrameBuffer::default();
        assert!(buffer.push(b"event: speak-start\n\n").unwrap().is_empty());
    }

    #[test]
    fn preserves_meaningful_spaces_in_data() {
        let mut buffer = FrameBuffer::default();
        assert_eq!(
            buffer.push(b"event: speak-start\ndata:   a  \n\n").unwrap(),
            [("speak-start".into(), "  a  ".into())]
        );
    }

    #[test]
    fn invalid_complete_utf8_is_an_error() {
        let mut buffer = FrameBuffer::default();
        assert!(buffer.push(b"event: speak-start\ndata: \xff\n\n").is_err());
    }

    #[test]
    fn a_complete_empty_data_field_is_dispatched() {
        let mut buffer = FrameBuffer::default();
        assert_eq!(
            buffer.push(b"event: speak-end\ndata:\n\n").unwrap(),
            [("speak-end".into(), "".into())]
        );
    }

    #[test]
    fn accepts_each_line_ending_and_mixed_endings_at_every_chunk_boundary() {
        for field_end in ["\n", "\r", "\r\n"] {
            for data_end in ["\n", "\r", "\r\n"] {
                for blank_end in ["\n", "\r", "\r\n"] {
                    // Adjacent CR + LF is one CRLF terminator, not two lines.
                    if data_end == "\r" && blank_end.starts_with('\n') {
                        continue;
                    }
                    let input = format!("event: speak-start{field_end}data: hello{data_end}{blank_end}event: speak-end\ndata: done\n\n");
                    for split in 0..=input.len() {
                        let mut buffer = FrameBuffer::default();
                        let mut events = buffer.push(&input.as_bytes()[..split]).unwrap();
                        events.extend(buffer.push(&input.as_bytes()[split..]).unwrap());
                        assert_eq!(
                            events,
                            [
                                ("speak-start".into(), "hello".into()),
                                ("speak-end".into(), "done".into())
                            ],
                            "{input:?} split {split}"
                        );
                    }
                }
            }
        }
    }

    #[test]
    fn lone_carriage_return_frame_is_delivered_without_waiting_for_another_chunk() {
        let mut buffer = FrameBuffer::default();
        assert_eq!(
            buffer.push(b"event: speak-end\rdata: done\r\r").unwrap(),
            [("speak-end".into(), "done".into())]
        );
    }

    #[test]
    fn oversized_unterminated_frame_is_rejected_across_chunks() {
        let mut buffer = FrameBuffer::default();
        let chunk = vec![b'x'; 64 * 1024];
        for _ in 0..16 {
            assert!(buffer.push(&chunk).is_ok());
        }
        assert!(buffer.push(b"x").is_err());
    }

    #[test]
    fn oversized_single_chunk_is_rejected() {
        let mut buffer = FrameBuffer::default();
        assert!(buffer.push(&vec![b'x'; 1024 * 1024 + 1]).is_err());
    }

    #[test]
    fn many_small_events_can_exceed_the_limit_in_total() {
        let mut buffer = FrameBuffer::default();
        let frame = b"event: speak-end\ndata: done\n\n";
        let input = frame.repeat(40_000);
        assert!(input.len() > 1024 * 1024);
        let events = buffer.push(&input).unwrap();
        assert_eq!(events.len(), 40_000);
        assert_eq!(events.last().unwrap(), &("speak-end".into(), "done".into()));
    }
}
