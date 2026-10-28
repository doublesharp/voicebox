use std::fs::{self, OpenOptions};
use std::io::{self, Read, Write};
use std::path::Path;
use std::sync::atomic::{AtomicU64, Ordering};

static NEXT_STAGE: AtomicU64 = AtomicU64::new(0);

// The sibling is created exclusively, so it cannot truncate another file. A failed
// copy, flush, sync or rename only removes our own staging file.
pub(crate) fn publish<R: Read>(destination: &Path, mut input: R) -> io::Result<()> {
    publish_using(destination, |file| {
        io::copy(&mut input, &mut *file)?;
        file.flush()?;
        file.sync_all()
    })
}

fn publish_using(
    destination: &Path,
    write_and_sync: impl FnOnce(&mut fs::File) -> io::Result<()>,
) -> io::Result<()> {
    let parent = destination
        .parent()
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "Destination has no parent"))?;
    let mut stage = None;
    for _ in 0..100 {
        let candidate = parent.join(format!(
            ".voicebox-save-{}-{}",
            std::process::id(),
            NEXT_STAGE.fetch_add(1, Ordering::Relaxed)
        ));
        let mut options = OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        match options.open(&candidate) {
            Ok(file) => {
                stage = Some((candidate, file));
                break;
            }
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => continue,
            Err(error) => return Err(error),
        }
    }
    let (path, mut file) = stage.ok_or_else(|| {
        io::Error::new(
            io::ErrorKind::AlreadyExists,
            "Could not allocate staging file",
        )
    })?;
    let result = (|| {
        write_and_sync(&mut file)?;
        match fs::metadata(destination) {
            Ok(metadata) => {
                file.set_permissions(metadata.permissions())?;
                file.sync_all()?;
            }
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(error) => return Err(error),
        }
        drop(file);
        fs::rename(&path, destination)
    })();
    if result.is_err() {
        let _ = fs::remove_file(&path);
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    struct Broken;
    impl Read for Broken {
        fn read(&mut self, _: &mut [u8]) -> io::Result<usize> {
            Err(io::Error::new(io::ErrorKind::Other, "stream failed"))
        }
    }
    #[test]
    fn publication_preserves_existing_on_stream_and_rename_failure_and_replaces_on_success() {
        let dir =
            std::env::temp_dir().join(format!("voicebox-publication-test-{}", std::process::id()));
        fs::create_dir(&dir).unwrap();
        let target = dir.join("old.wav");
        fs::write(&target, b"old").unwrap();
        assert!(publish(&target, Broken).is_err());
        assert_eq!(fs::read(&target).unwrap(), b"old");
        for failure in ["write", "flush", "sync"] {
            let result = publish_using(&target, |file| {
                file.write_all(b"partial")?;
                if failure != "write" {
                    file.flush()?;
                }
                if failure == "sync" {
                    file.sync_all()?;
                }
                Err(io::Error::new(io::ErrorKind::Other, failure))
            });
            assert!(result.is_err());
            assert_eq!(fs::read(&target).unwrap(), b"old");
        }
        assert!(publish(&dir.join("missing").join("file"), &b"new"[..]).is_err());
        let blocked = dir.join("directory");
        fs::create_dir(&blocked).unwrap();
        assert!(publish(&blocked, &b"new"[..]).is_err());
        assert!(blocked.is_dir());
        publish(&target, &b"new bytes"[..]).unwrap();
        assert_eq!(fs::read(&target).unwrap(), b"new bytes");
        assert_eq!(fs::read_dir(&dir).unwrap().count(), 2);
        fs::remove_dir_all(dir).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn replacement_preserves_private_permissions_and_staging_starts_private() {
        use std::os::unix::fs::PermissionsExt;
        let dir = std::env::temp_dir().join(format!(
            "voicebox-private-publication-test-{}",
            std::process::id()
        ));
        fs::create_dir(&dir).unwrap();
        let target = dir.join("private.wav");
        fs::write(&target, b"private old bytes").unwrap();
        fs::set_permissions(&target, fs::Permissions::from_mode(0o600)).unwrap();
        publish(&target, &b"private new bytes"[..]).unwrap();
        let replacement_mode = fs::metadata(&target).unwrap().permissions().mode() & 0o777;
        let fresh = dir.join("new.wav");
        let mut staging_mode = 0;
        publish_using(&fresh, |file| {
            staging_mode = file.metadata()?.permissions().mode() & 0o777;
            file.write_all(b"new private bytes")?;
            file.flush()?;
            file.sync_all()
        })
        .unwrap();
        fs::remove_dir_all(dir).unwrap();
        assert_eq!(
            replacement_mode, 0o600,
            "replacement widened the existing file's permissions"
        );
        assert_eq!(
            staging_mode, 0o600,
            "unpublished staging content was not private"
        );
    }
}
