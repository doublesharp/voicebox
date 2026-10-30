use std::fs::{self, OpenOptions};
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

static NEXT_STAGE: AtomicU64 = AtomicU64::new(0);

struct StagedFile {
    path: PathBuf,
    file: Option<fs::File>,
    published: bool,
}

impl Drop for StagedFile {
    fn drop(&mut self) {
        // Windows cannot remove an open file. Close it before removing our stage.
        drop(self.file.take());
        if !self.published {
            let _ = fs::remove_file(&self.path);
        }
    }
}

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
    // Atomic rename must replace the selected file, not a symbolic link to it.
    // Canonicalizing also rejects dangling links without destroying them.
    let resolved = match fs::symlink_metadata(destination) {
        Ok(metadata) if metadata.file_type().is_symlink() => fs::canonicalize(destination)?,
        Ok(_) => destination.to_path_buf(),
        Err(error) if error.kind() == io::ErrorKind::NotFound => destination.to_path_buf(),
        Err(error) => return Err(error),
    };
    let destination = resolved.as_path();
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
    let (path, file) = stage.ok_or_else(|| {
        io::Error::new(
            io::ErrorKind::AlreadyExists,
            "Could not allocate staging file",
        )
    })?;
    let mut stage = StagedFile {
        path,
        file: Some(file),
        published: false,
    };
    let file = stage.file.as_mut().unwrap();
    write_and_sync(file)?;
    match fs::metadata(destination) {
        Ok(metadata) => {
            file.set_permissions(metadata.permissions())?;
            file.sync_all()?;
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
        Err(error) => return Err(error),
    }
    drop(stage.file.take());
    fs::rename(&stage.path, destination)?;
    stage.published = true;
    Ok(())
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
    struct PanicAfterBytes(bool);
    impl Read for PanicAfterBytes {
        fn read(&mut self, output: &mut [u8]) -> io::Result<usize> {
            if !self.0 {
                self.0 = true;
                output[..7].copy_from_slice(b"partial");
                Ok(7)
            } else {
                panic!("stream panicked");
            }
        }
    }

    struct InterruptedOnce(bool);
    impl Read for InterruptedOnce {
        fn read(&mut self, output: &mut [u8]) -> io::Result<usize> {
            if !self.0 {
                self.0 = true;
                return Err(io::Error::new(io::ErrorKind::Interrupted, "retry"));
            }
            output[..5].copy_from_slice(b"retry");
            self.0 = false;
            Ok(5)
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
        let panic = std::panic::catch_unwind(|| publish(&target, PanicAfterBytes(false)));
        assert!(panic.is_err());
        assert_eq!(fs::read(&target).unwrap(), b"old");
        assert_eq!(
            fs::read_dir(&dir).unwrap().count(),
            1,
            "panicking reader leaked a staging file"
        );
        // An interrupted read must be retried without publishing a partial file.
        let mut reader = InterruptedOnce(false).take(5);
        publish(&target, &mut reader).unwrap();
        assert_eq!(fs::read(&target).unwrap(), b"retry");
        fs::write(&target, b"old").unwrap();
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
        // Simultaneous exports to one destination must result in one complete file.
        let barrier = std::sync::Barrier::new(4);
        std::thread::scope(|scope| {
            for byte in 1_u8..=4 {
                let target = &target;
                let barrier = &barrier;
                scope.spawn(move || {
                    barrier.wait();
                    publish(target, vec![byte; 8192].as_slice()).unwrap();
                });
            }
        });
        let final_bytes = fs::read(&target).unwrap();
        assert_eq!(final_bytes.len(), 8192);
        assert!((1..=4).contains(&final_bytes[0]));
        assert!(final_bytes.iter().all(|byte| *byte == final_bytes[0]));
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

#[cfg(all(test, unix))]
mod symlink_export_tests {
    use super::*;
    use std::os::unix::fs::symlink;

    struct Fixture(PathBuf);
    impl Fixture {
        fn new() -> Self {
            let root = std::env::temp_dir().join(format!(
                "voicebox-symlink-export-{}-{}",
                std::process::id(),
                NEXT_STAGE.fetch_add(1, Ordering::Relaxed)
            ));
            fs::create_dir(&root).unwrap();
            Self(root)
        }
        fn path(&self, name: &str) -> PathBuf {
            self.0.join(name)
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    struct Interrupted(bool);
    impl Read for Interrupted {
        fn read(&mut self, output: &mut [u8]) -> io::Result<usize> {
            if !self.0 {
                self.0 = true;
                output[..3].copy_from_slice(b"new");
                Ok(3)
            } else {
                Err(io::Error::new(io::ErrorKind::Other, "reader failed"))
            }
        }
    }

    #[test]
    fn export_updates_target_without_replacing_the_selected_symlink() {
        let fixture = Fixture::new();
        let target = fixture.path("target.wav");
        let link = fixture.path("selected.wav");
        fs::write(&target, b"old").unwrap();
        symlink("target.wav", &link).unwrap();
        publish(&link, &b"new audio"[..]).unwrap();
        assert!(fs::symlink_metadata(&link)
            .unwrap()
            .file_type()
            .is_symlink());
        assert_eq!(fs::read_link(&link).unwrap(), PathBuf::from("target.wav"));
        assert_eq!(fs::read(&target).unwrap(), b"new audio");
        assert_eq!(fs::read_dir(&fixture.0).unwrap().count(), 2);
    }

    #[test]
    fn export_follows_link_chain_to_a_target_in_another_directory() {
        let fixture = Fixture::new();
        fs::create_dir(fixture.path("output")).unwrap();
        let target = fixture.path("output/target.wav");
        let middle = fixture.path("middle.wav");
        let link = fixture.path("selected.wav");
        fs::write(&target, b"old").unwrap();
        symlink("output/target.wav", &middle).unwrap();
        symlink("middle.wav", &link).unwrap();
        publish(&link, &b"replacement"[..]).unwrap();
        assert!(fs::symlink_metadata(&link)
            .unwrap()
            .file_type()
            .is_symlink());
        assert!(fs::symlink_metadata(&middle)
            .unwrap()
            .file_type()
            .is_symlink());
        assert_eq!(fs::read(&target).unwrap(), b"replacement");
        assert_eq!(fs::read_dir(fixture.path("output")).unwrap().count(), 1);
    }

    #[test]
    fn dangling_symlink_is_not_destroyed_or_replaced() {
        let fixture = Fixture::new();
        let link = fixture.path("selected.wav");
        symlink("missing.wav", &link).unwrap();
        assert!(publish(&link, &b"replacement"[..]).is_err());
        assert!(fs::symlink_metadata(&link)
            .unwrap()
            .file_type()
            .is_symlink());
        assert!(!fixture.path("missing.wav").exists());
        assert_eq!(fs::read_dir(&fixture.0).unwrap().count(), 1);
    }

    #[test]
    fn cyclic_symlinks_return_an_error_without_modifying_either_link() {
        let fixture = Fixture::new();
        let first = fixture.path("first.wav");
        let second = fixture.path("second.wav");
        symlink("second.wav", &first).unwrap();
        symlink("first.wav", &second).unwrap();
        assert!(publish(&first, &b"audio"[..]).is_err());
        assert_eq!(fs::read_link(&first).unwrap(), PathBuf::from("second.wav"));
        assert_eq!(fs::read_link(&second).unwrap(), PathBuf::from("first.wav"));
        assert_eq!(fs::read_dir(&fixture.0).unwrap().count(), 2);
    }

    #[test]
    fn failed_export_through_symlink_preserves_link_and_target() {
        let fixture = Fixture::new();
        let target = fixture.path("target.wav");
        let link = fixture.path("selected.wav");
        fs::write(&target, b"original").unwrap();
        symlink("target.wav", &link).unwrap();
        assert!(publish(&link, Interrupted(false)).is_err());
        assert!(fs::symlink_metadata(&link)
            .unwrap()
            .file_type()
            .is_symlink());
        assert_eq!(fs::read(&target).unwrap(), b"original");
        assert_eq!(fs::read_dir(&fixture.0).unwrap().count(), 2);
    }
}
