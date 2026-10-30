//! Coordinate the frontend cleanup acknowledgement with window closure.

pub(crate) fn request_cleanup<L: Copy, E>(
    register: impl FnOnce() -> L,
    notify: impl FnOnce() -> Result<(), E>,
    unregister: impl FnOnce(L),
) -> Result<L, E> {
    let listener = register();
    if let Err(error) = notify() {
        unregister(listener);
        return Err(error);
    }
    Ok(listener)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::{Cell, RefCell};
    use std::sync::mpsc;

    #[test]
    fn immediate_frontend_acknowledgement_is_not_lost() {
        let listener = RefCell::new(None::<mpsc::Sender<()>>);
        let (tx, rx) = mpsc::channel();
        let id = request_cleanup(
            || {
                *listener.borrow_mut() = Some(tx);
                7
            },
            || {
                if let Some(sender) = listener.borrow().as_ref() {
                    sender.send(()).unwrap();
                }
                Ok::<_, ()>(())
            },
            |_| {},
        )
        .unwrap();
        assert_eq!(id, 7);
        assert_eq!(rx.try_recv(), Ok(()));
    }

    #[test]
    fn notify_failure_unregisters_the_listener() {
        let registered = Cell::new(false);
        let unregistered = Cell::new(None);
        let result = request_cleanup(
            || {
                registered.set(true);
                42
            },
            || Err::<(), _>("emit failed"),
            |id| unregistered.set(Some(id)),
        );
        assert_eq!(result, Err("emit failed"));
        assert!(registered.get());
        assert_eq!(unregistered.get(), Some(42));
    }

    #[test]
    fn successful_notification_keeps_listener_registered() {
        let unregistered = Cell::new(false);
        assert_eq!(
            request_cleanup(|| 13, || Ok::<_, ()>(()), |_| unregistered.set(true)),
            Ok(13)
        );
        assert!(!unregistered.get());
    }
}
