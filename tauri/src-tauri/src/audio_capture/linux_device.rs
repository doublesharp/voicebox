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

/// Select a system-audio monitor without falling back to a microphone.
pub(super) fn select_monitor_device<D>(
    devices: Vec<D>,
    monitor_source: Option<&str>,
    name: impl Fn(&D) -> Option<String>,
) -> Option<D> {
    if let Some(target) = monitor_source {
        if let Some(pos) = devices
            .iter()
            .position(|device| name(device).as_deref() == Some(target))
        {
            return devices.into_iter().nth(pos);
        }
    }
    if let Some(pos) = devices.iter().position(|device| {
        name(device)
            .map(|value| value.to_lowercase().contains("monitor"))
            .unwrap_or(false)
    }) {
        return devices.into_iter().nth(pos);
    }
    None
}

#[cfg(test)]
mod tests {
    use super::{monitor_source_from_pactl, select_monitor_device};

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
}
