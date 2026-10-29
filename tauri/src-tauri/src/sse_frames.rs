//! Incremental SSE frame handling for the native speech subscriber.

#[derive(Default)]
pub(crate) struct FrameBuffer {
    buf: Vec<u8>,
}

impl FrameBuffer {
    pub(crate) fn push(
        &mut self,
        chunk: &[u8],
    ) -> Result<Vec<(String, String)>, std::str::Utf8Error> {
        self.buf.extend_from_slice(chunk);
        let mut result = Vec::new();
        loop {
            let crlf = self.buf.windows(4).position(|window| window == b"\r\n\r\n");
            let lf = self.buf.windows(2).position(|window| window == b"\n\n");
            let (end, sep_len) = match (crlf, lf) {
                (Some(c), Some(l)) if c <= l => (c, 4),
                (Some(c), None) => (c, 4),
                (_, Some(l)) => (l, 2),
                (None, None) => break,
            };
            let frame: Vec<u8> = self.buf.drain(..end + sep_len).collect();
            if let Some(event) = parse_frame(std::str::from_utf8(&frame)?) {
                result.push(event);
            }
        }
        Ok(result)
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
        assert!(buffer.push(b"lo\r\n\r").unwrap().is_empty());
        assert_eq!(
            buffer.push(b"\n").unwrap(),
            [("speak-start".into(), "hello".into())]
        );
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
}
