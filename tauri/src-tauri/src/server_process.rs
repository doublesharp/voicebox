use std::sync::Mutex;

#[cfg(any(test, windows))]
pub(crate) fn is_windows_server_image(tasklist_csv: &str) -> bool {
    tasklist_csv.lines().any(|line| {
        let image = line
            .split(',')
            .next()
            .unwrap_or("")
            .trim()
            .trim_matches('"');
        matches!(
            image.to_ascii_lowercase().as_str(),
            "voicebox-server.exe" | "voicebox-server-cuda.exe" | "voicebox-server-rocm.exe"
        )
    })
}

#[cfg(unix)]
#[derive(Debug, PartialEq, Eq)]
pub(crate) enum ListenerDecision {
    Unoccupied,
    ReuseOwned(u32),
    ReuseExternal,
    Conflict(String),
}

#[cfg(unix)]
fn listeners(output: &str) -> impl Iterator<Item = (&str, Option<u32>)> {
    output.lines().skip(1).filter_map(|line| {
        let mut fields = line.split_whitespace();
        let name = fields.next()?;
        let pid = fields.next()?.parse::<u32>().ok();
        Some((name, pid))
    })
}

#[cfg(unix)]
pub(crate) fn classify_listener(output: &str, healthy: bool) -> ListenerDecision {
    let rows: Vec<_> = listeners(output).collect();
    let Some((name, pid)) = rows.first().copied() else {
        return ListenerDecision::Unoccupied;
    };
    if !healthy {
        return ListenerDecision::Conflict(name.to_string());
    }
    match pid.filter(|pid| *pid > 1) {
        Some(pid)
            if rows
                .iter()
                .all(|(name, row_pid)| is_server_name(name) && *row_pid == Some(pid)) =>
        {
            ListenerDecision::ReuseOwned(pid)
        }
        _ => ListenerDecision::ReuseExternal,
    }
}

#[cfg(unix)]
fn is_server_name(name: &str) -> bool {
    // An abbreviated name such as "voicebox-" is ambiguous and must not be
    // claimed; callers ask lsof for an untruncated COMMAND column.
    name == "voicebox-server" || name.starts_with("voicebox-server-")
}

#[cfg(unix)]
pub(crate) fn legacy_pids(output: &str, healthy: bool) -> Vec<u32> {
    if !healthy {
        return Vec::new();
    }
    let rows: Vec<_> = listeners(output).collect();
    let Some((_, Some(pid))) = rows.first().copied() else {
        return Vec::new();
    };
    // A health response cannot identify which listener owns a shared port.
    if pid <= 1
        || !rows
            .iter()
            .all(|(name, row_pid)| is_server_name(name) && *row_pid == Some(pid))
    {
        return Vec::new();
    }
    vec![pid]
}

/// Detach a PID and its child together. A stale startup may only detach its
/// own process; an explicit stop may detach whichever process is current.
pub(crate) fn take_process<T>(
    pid: &Mutex<Option<u32>>,
    child: &Mutex<Option<T>>,
    expected: Option<u32>,
) -> Option<(u32, Option<T>)> {
    let mut pid = pid.lock().unwrap();
    let current = (*pid)?;
    if expected.is_some_and(|expected| expected != current) {
        return None;
    }
    let mut child = child.lock().unwrap();
    *pid = None;
    Some((current, child.take()))
}

#[cfg(test)]
mod tests {
    use super::take_process;
    use std::sync::{Arc, Barrier, Mutex};

    #[test]
    fn matching_startup_detaches_both_fields() {
        let pid = Mutex::new(Some(11));
        let child = Mutex::new(Some("original"));
        assert_eq!(
            take_process(&pid, &child, Some(11)),
            Some((11, Some("original")))
        );
        assert_eq!(*pid.lock().unwrap(), None);
        assert_eq!(*child.lock().unwrap(), None);
        assert_eq!(take_process(&pid, &child, Some(11)), None);
    }

    #[test]
    fn stale_cleanup_leaves_a_replacement_intact() {
        let state = Arc::new((Mutex::new(Some(11)), Mutex::new(Some("original"))));
        let replaced = Arc::new(Barrier::new(2));
        let worker_state = Arc::clone(&state);
        let worker_replaced = Arc::clone(&replaced);
        let cleanup = std::thread::spawn(move || {
            worker_replaced.wait();
            take_process(&worker_state.0, &worker_state.1, Some(11))
        });
        {
            let mut pid = state.0.lock().unwrap();
            let mut child = state.1.lock().unwrap();
            *pid = Some(22);
            *child = Some("replacement");
        }
        replaced.wait();
        assert_eq!(cleanup.join().unwrap(), None);
        assert_eq!(*state.0.lock().unwrap(), Some(22));
        assert_eq!(*state.1.lock().unwrap(), Some("replacement"));
        assert_eq!(
            take_process(&state.0, &state.1, None),
            Some((22, Some("replacement")))
        );
    }

    #[test]
    fn windows_tasklist_accepts_only_the_server_executable_names() {
        for image in [
            "voicebox-server.exe",
            "VOICEBOX-SERVER-CUDA.EXE",
            "voicebox-server-rocm.exe",
        ] {
            assert!(super::is_windows_server_image(&format!(
                "\"{image}\",\"123\",\"Console\""
            )));
        }
        for image in [
            "voicebox.exe",
            "voicebox-helper.exe",
            "notvoicebox-server.exe",
            "voicebox-server.exe.bak",
        ] {
            assert!(!super::is_windows_server_image(&format!(
                "\"{image}\",\"123\",\"Console\""
            )));
        }
        assert!(!super::is_windows_server_image(
            "INFO: No tasks are running"
        ));
    }
}

#[cfg(all(test, unix))]
mod listener_tests {
    use super::{classify_listener, legacy_pids, ListenerDecision};

    fn lsof(rows: &[(&str, &str)]) -> String {
        let mut output = String::from("COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME\n");
        for (name, pid) in rows {
            output.push_str(&format!(
                "{name} {pid} user 9u IPv4 1 0t0 TCP localhost:17493 (LISTEN)\n"
            ));
        }
        output
    }

    #[test]
    fn empty_or_malformed_lsof_output_is_unoccupied() {
        assert_eq!(classify_listener("", false), ListenerDecision::Unoccupied);
        assert_eq!(
            classify_listener("COMMAND PID\n\n", false),
            ListenerDecision::Unoccupied
        );
    }

    #[test]
    fn even_a_voicebox_named_process_must_pass_health_before_reuse() {
        for name in ["voicebox-server", "voicebox-server-cuda"] {
            assert_eq!(
                classify_listener(&lsof(&[(name, "123")]), false),
                ListenerDecision::Conflict(name.into())
            );
            assert_eq!(
                classify_listener(&lsof(&[(name, "123")]), true),
                ListenerDecision::ReuseOwned(123)
            );
        }
    }

    #[test]
    fn unrelated_voicebox_named_program_is_never_owned() {
        for name in [
            "voicebox-",
            "voicebox-helper",
            "myvoicebox",
            "notvoicebox-server",
        ] {
            assert_eq!(
                classify_listener(&lsof(&[(name, "123")]), true),
                ListenerDecision::ReuseExternal
            );
            assert_eq!(
                classify_listener(&lsof(&[(name, "123")]), false),
                ListenerDecision::Conflict(name.into())
            );
        }
    }

    #[test]
    fn healthy_external_server_is_reused_without_claiming_its_process() {
        assert_eq!(
            classify_listener(&lsof(&[("python", "456")]), true),
            ListenerDecision::ReuseExternal
        );
        assert_eq!(
            classify_listener(&lsof(&[("python", "456")]), false),
            ListenerDecision::Conflict("python".into())
        );
    }

    #[test]
    fn invalid_or_system_pids_are_never_claimed() {
        for pid in ["garbage", "-3", "0", "1", "4294967296"] {
            assert_eq!(
                classify_listener(&lsof(&[("voicebox-server", pid)]), true),
                ListenerDecision::ReuseExternal
            );
            assert!(legacy_pids(&lsof(&[("voicebox-server", pid)]), true).is_empty());
        }
    }

    #[test]
    fn legacy_cleanup_requires_health_and_a_recognized_server_name() {
        let output = lsof(&[
            ("voicebox-server", "123"),
            ("voicebox-helper", "456"),
            ("python", "789"),
        ]);
        assert!(legacy_pids(&output, false).is_empty());
        assert!(legacy_pids(&output, true).is_empty());
    }

    #[test]
    fn duplicate_lsof_rows_do_not_repeat_legacy_termination() {
        let duplicate = lsof(&[("voicebox-server", "123"), ("voicebox-server", "123")]);
        assert_eq!(legacy_pids(&duplicate, true), vec![123]);
        let ambiguous = lsof(&[("voicebox-server", "123"), ("voicebox-server-rocm", "456")]);
        assert!(legacy_pids(&ambiguous, true).is_empty());
        assert_eq!(
            classify_listener(&ambiguous, true),
            ListenerDecision::ReuseExternal
        );
    }
}
