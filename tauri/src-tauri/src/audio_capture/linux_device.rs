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
    use super::select_monitor_device;

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
}
