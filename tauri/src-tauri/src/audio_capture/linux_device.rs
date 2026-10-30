/// Find the default sink monitor or the first available monitor in pactl output.
pub(super) fn monitor_source_from_pactl<'a>(
    sources: &'a str,
    default_sink: Option<&str>,
) -> Option<&'a str> {
    let names = || sources.lines().filter_map(|line| line.split('\t').nth(1));
    if let Some(sink) = default_sink {
        let target = format!("{sink}.monitor");
        if let Some(name) = names().find(|name| *name == target) {
            return Some(name);
        }
    }
    names().find(|name| name.ends_with(".monitor"))
}

// A generic input name containing "monitor" is not evidence that it is a
// system-output source. Accept recognizable PulseAudio monitor names only.
fn is_fallback_monitor_name(name: &str) -> bool {
    let name = name.trim().to_lowercase();
    let has_input_label = name
        .split(|character: char| !character.is_alphanumeric())
        .any(|part| matches!(part, "mic" | "microphone" | "input" | "capture"));
    !has_input_label
        && (name.ends_with(".monitor")
            || name.starts_with("monitor of ")
            || name == "output monitor"
            || name.ends_with(" output monitor"))
}

/// Prefer a pactl monitor match; otherwise accept only recognizable monitor names.
pub(super) fn select_monitor_device<D>(
    devices: Vec<D>,
    monitor_source: Option<&str>,
    name: impl Fn(&D) -> Option<String>,
) -> Option<D> {
    let names: Vec<Option<String>> = devices.iter().map(name).collect();
    let mut counts = std::collections::HashMap::new();
    for value in names.iter().flatten() {
        *counts.entry(value.as_str()).or_insert(0usize) += 1;
    }
    if let Some(target) = monitor_source {
        match counts.get(target).copied().unwrap_or(0) {
            1 => {
                let pos = names
                    .iter()
                    .position(|value| value.as_deref() == Some(target))?;
                return devices.into_iter().nth(pos);
            }
            2.. => return None,
            _ => {}
        }
    }
    let pos = names.iter().position(|value| {
        value
            .as_deref()
            .is_some_and(|value| is_fallback_monitor_name(value) && counts.get(value) == Some(&1))
    })?;
    devices.into_iter().nth(pos)
}

/// Report whether system audio can be captured on this host.
pub(super) fn capture_supported<D>(
    monitor_source: Option<&str>,
    find_monitor: impl FnOnce(Option<&str>) -> Option<D>,
) -> bool {
    find_monitor(monitor_source).is_some()
}

#[cfg(test)]
mod tests {
    use super::{capture_supported, monitor_source_from_pactl, select_monitor_device};

    #[derive(Debug, PartialEq)]
    struct Device(&'static str, Option<&'static str>);

    fn choose(devices: Vec<Device>, target: Option<&str>) -> Option<Device> {
        select_monitor_device(devices, target, |device| device.1.map(str::to_owned))
    }

    #[test]
    fn prefers_the_default_sink_monitor_over_other_monitors() {
        assert_eq!(
            choose(
                vec![
                    Device("other", Some("other.monitor")),
                    Device("sink", Some("sink.monitor"))
                ],
                Some("sink.monitor")
            ),
            Some(Device("sink", Some("sink.monitor")))
        );
    }

    #[test]
    fn searches_monitor_names_when_pactl_is_unavailable() {
        assert_eq!(
            choose(
                vec![
                    Device("mic", Some("Microphone")),
                    Device("system", Some("Output MONITOR"))
                ],
                None
            ),
            Some(Device("system", Some("Output MONITOR")))
        );
    }

    #[test]
    fn uses_another_monitor_if_the_pactl_name_is_missing() {
        assert_eq!(
            choose(
                vec![Device("system", Some("other.monitor"))],
                Some("sink.monitor")
            ),
            Some(Device("system", Some("other.monitor")))
        );
    }

    #[test]
    fn ignores_devices_with_unavailable_names() {
        assert_eq!(
            choose(
                vec![
                    Device("unknown", None),
                    Device("system", Some("output.monitor"))
                ],
                None
            ),
            Some(Device("system", Some("output.monitor")))
        );
    }

    #[test]
    fn microphone_and_input_names_containing_monitor_are_not_system_audio() {
        for name in [
            "Monitor Microphone",
            "Microphone Output Monitor",
            "monitor.input",
            "Monitor of USB Microphone",
            "Baby Monitor",
            "Webcam monitor",
            "Headset Mic Monitor",
            "usb-mic.monitor",
            "Monitor of USB Input",
        ] {
            assert_eq!(
                choose(vec![Device("input", Some(name))], None),
                None,
                "{name}"
            );
            assert!(!supported(None, vec![Device("input", Some(name))]), "{name}");
        }
    }

    #[test]
    fn fallback_identifies_pulseaudio_monitor_names_without_case_sensitivity() {
        for name in [
            "sink.monitor",
            "SINK.MONITOR",
            "Monitor of Built-in Audio",
            "Output MONITOR",
        ] {
            assert_eq!(
                choose(vec![Device("output", Some(name))], None),
                Some(Device("output", Some(name)))
            );
            assert!(supported(None, vec![Device("output", Some(name))]));
        }
    }

    #[test]
    fn exact_pactl_monitor_match_is_preferred_even_if_its_label_mentions_a_mic() {
        let name = "alsa_output.usb-Headset_Microphone.monitor";
        assert_eq!(
            choose(vec![Device("output", Some(name))], Some(name)),
            Some(Device("output", Some(name)))
        );
    }

    #[test]
    fn duplicate_exact_monitor_names_are_not_selected_or_reported_supported() {
        let devices = || {
            vec![
                Device("first", Some("sink.monitor")),
                Device("second", Some("sink.monitor")),
            ]
        };
        assert_eq!(choose(devices(), Some("sink.monitor")), None);
        assert!(!supported(Some("sink.monitor"), devices()));
    }

    #[test]
    fn duplicate_fallback_monitor_names_are_not_selected_or_reported_supported() {
        let devices = || {
            vec![
                Device("first", Some("sink.monitor")),
                Device("second", Some("sink.monitor")),
            ]
        };
        assert_eq!(choose(devices(), None), None);
        assert!(!supported(None, devices()));
    }

    #[test]
    fn fallback_skips_duplicated_names_to_select_a_unique_monitor() {
        assert_eq!(
            choose(
                vec![
                    Device("first", Some("sink.monitor")),
                    Device("second", Some("sink.monitor")),
                    Device("unique", Some("other.monitor")),
                ],
                None,
            ),
            Some(Device("unique", Some("other.monitor")))
        );
    }

    #[test]
    fn fallback_skips_ambiguous_input_to_find_a_valid_output_monitor() {
        assert_eq!(
            choose(
                vec![
                    Device("mic", Some("Monitor Microphone")),
                    Device("output", Some("sink.monitor")),
                ],
                None,
            ),
            Some(Device("output", Some("sink.monitor")))
        );
    }

    #[test]
    fn never_falls_back_to_a_microphone_when_no_monitor_exists() {
        assert_eq!(
            choose(
                vec![Device("mic", Some("Microphone"))],
                Some("sink.monitor")
            ),
            None
        );
    }

    #[test]
    fn never_uses_default_input_when_device_enumeration_is_empty() {
        assert_eq!(choose(vec![], None), None);
    }
    const SOURCES: &str =
        "0\tmic.input\tmodule\n1\tother.monitor\tmodule\n2\tsink.monitor\tmodule\n";

    #[test]
    fn pactl_prefers_monitor_for_default_sink_even_when_listed_last() {
        assert_eq!(
            monitor_source_from_pactl(SOURCES, Some("sink")),
            Some("sink.monitor")
        );
    }

    #[test]
    fn pactl_uses_first_monitor_when_default_sink_is_missing() {
        assert_eq!(
            monitor_source_from_pactl(SOURCES, Some("unknown")),
            Some("other.monitor")
        );
    }

    #[test]
    fn pactl_uses_first_monitor_without_a_default_sink() {
        assert_eq!(
            monitor_source_from_pactl(SOURCES, None),
            Some("other.monitor")
        );
    }

    #[test]
    fn pactl_rejects_microphones_and_monitor_substrings() {
        assert_eq!(
            monitor_source_from_pactl(
                "0\tmic.input\tmodule\n1\tmonitor.input\tmodule\n2\tfake.monitor.extra\tmodule\n",
                None
            ),
            None
        );
    }

    #[test]
    fn pactl_ignores_malformed_lines_and_accepts_crlf() {
        assert_eq!(
            monitor_source_from_pactl("malformed\r\n\r\n3\tsink.monitor\tmodule\r\n", Some("sink")),
            Some("sink.monitor")
        );
    }

    #[test]
    fn pactl_requires_an_exact_default_sink_name() {
        assert_eq!(
            monitor_source_from_pactl(
                "0\tsink-extra.monitor\tmodule\n1\tsink.monitor\tmodule\n",
                Some("sink")
            ),
            Some("sink.monitor")
        );
    }

    #[test]
    fn pactl_returns_none_for_empty_or_malformed_output() {
        assert_eq!(monitor_source_from_pactl("\nno tabs\n", Some("sink")), None);
    }
    fn supported(target: Option<&str>, devices: Vec<Device>) -> bool {
        capture_supported(target, |source| choose(devices, source))
    }

    #[test]
    fn pactl_monitor_without_a_matching_capture_device_is_not_supported() {
        assert!(!supported(
            Some("sink.monitor"),
            vec![Device("mic", Some("Microphone"))]
        ));
    }

    #[test]
    fn default_microphone_without_a_monitor_is_not_supported() {
        assert!(!supported(None, vec![Device("mic", Some("Microphone"))]));
    }

    #[test]
    fn unavailable_device_enumeration_does_not_turn_pactl_into_support() {
        assert!(!supported(Some("sink.monitor"), vec![]));
    }

    #[test]
    fn matching_pactl_monitor_is_supported() {
        assert!(supported(
            Some("sink.monitor"),
            vec![Device("sink", Some("sink.monitor"))]
        ));
    }

    #[test]
    fn fallback_monitor_is_supported_without_pactl() {
        assert!(supported(
            None,
            vec![Device("system", Some("Output MONITOR"))]
        ));
    }

    #[test]
    fn unnamed_devices_do_not_make_capture_supported() {
        assert!(!supported(None, vec![Device("unknown", None)]));
    }
}
