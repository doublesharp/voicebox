use std::sync::Mutex;

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
}
