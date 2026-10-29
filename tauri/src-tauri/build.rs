#[cfg(target_os = "macos")]
use std::process::Command;

fn main() {
    // Link Swift runtime libraries for screencapturekit crate
    #[cfg(target_os = "macos")]
    {
        // ScreenCaptureKit does not exist on macOS 11, so weak-link it to
        // allow the app to launch and gate usage at runtime instead.
        println!("cargo:rustc-link-arg=-Wl,-weak_framework,ScreenCaptureKit");

        // Add Swift runtime library paths to RPATH
        println!("cargo:rustc-link-arg=-Wl,-rpath,/usr/lib/swift");
        println!("cargo:rustc-link-arg=-L/usr/lib/swift");

        // Also try Xcode's Swift libraries
        if let Ok(output) = Command::new("xcode-select").arg("-p").output() {
            if output.status.success() {
                let xcode_path = String::from_utf8_lossy(&output.stdout).trim().to_string();
                let swift_lib_path = format!(
                    "{}/Toolchains/XcodeDefault.xctoolchain/usr/lib/swift/macosx",
                    xcode_path
                );
                println!("cargo:rustc-link-arg=-Wl,-rpath,{}", swift_lib_path);
                println!("cargo:rustc-link-arg=-L{}", swift_lib_path);
            }
        }
    }

    let project_root = env!("CARGO_MANIFEST_DIR");
    let gen_dir = format!("{}/gen", project_root);
    std::fs::create_dir_all(&gen_dir).expect("Failed to create gen directory");

    // Compile macOS Liquid Glass icon
    #[cfg(target_os = "macos")]
    {
        // voicebox.icon is in tauri/assets/voicebox.icon (one level up from src-tauri)
        let icon_source = format!("{}/../assets/voicebox.icon", project_root);

        if std::path::Path::new(&icon_source).exists() {
            println!("cargo:rerun-if-changed={}", icon_source);
            println!("cargo:rerun-if-changed={}/icon.json", icon_source);
            println!("cargo:rerun-if-changed={}/Assets", icon_source);

            let partial_plist = format!("{}/partial.plist", gen_dir);
            let output = Command::new("xcrun")
                .args([
                    "actool",
                    "--compile",
                    &gen_dir,
                    "--output-format",
                    "human-readable-text",
                    "--output-partial-info-plist",
                    &partial_plist,
                    "--app-icon",
                    "voicebox",
                    "--include-all-app-icons",
                    "--target-device",
                    "mac",
                    "--minimum-deployment-target",
                    "11.0",
                    "--platform",
                    "macosx",
                    &icon_source,
                ])
                .output();

            // A missing or failing actool is survivable for dev builds (the
            // stub block below writes empty Assets.car / partial.plist), but a
            // release bundle must never ship those stubs as its icon catalog.
            let release_build = std::env::var("PROFILE").as_deref() == Ok("release");
            match output {
                Ok(output) => {
                    if !output.status.success() {
                        let detail = String::from_utf8_lossy(&output.stdout)
                            .lines()
                            .chain(String::from_utf8_lossy(&output.stderr).lines())
                            .filter(|line| !line.trim().is_empty())
                            .map(|line| line.trim().to_string())
                            .collect::<Vec<_>>();
                        if release_build {
                            for line in &detail {
                                eprintln!("actool: {}", line);
                            }
                            panic!(
                                "actool failed to compile icon (release build requires full Xcode)"
                            );
                        }
                        println!(
                            "cargo:warning=actool failed (requires full Xcode, not just CLT) — \
                             stub files will be used for this dev build"
                        );
                        for line in &detail {
                            println!("cargo:warning=actool: {}", line);
                        }
                    } else {
                        println!("Successfully compiled icon to {}", gen_dir);
                    }
                }
                Err(e) => {
                    if release_build {
                        eprintln!("Failed to execute xcrun actool: {}", e);
                        panic!("Icon compilation failed (release build requires full Xcode)");
                    }
                    println!(
                        "cargo:warning=xcrun actool not available ({e}) — \
                         install Xcode from the App Store for Liquid Glass icons. \
                         Stub files will be used for this dev build."
                    );
                }
            }

            // Always refresh the legacy icon from the maintained desktop asset.
            // An existing gen/voicebox.icns must not retain an older brand.
            let fallback_icon = format!("{}/icons/icon.icns", project_root);
            println!("cargo:rerun-if-changed={}", fallback_icon);
            std::fs::copy(&fallback_icon, format!("{}/voicebox.icns", gen_dir))
                .expect("Failed to refresh the macOS fallback icon");
        } else {
            println!(
                "cargo:warning=Icon source not found at {}, skipping icon compilation",
                icon_source
            );
        }
    }

    // Ensure all resource files exist so Tauri's bundler doesn't fail.
    // On non-macOS these are always stubs. On macOS, actool may not produce
    // Assets.car if the Xcode version doesn't support the .icon format.
    {
        let required = ["Assets.car", "voicebox.icns", "partial.plist"];
        for name in required {
            let path = format!("{}/{}", gen_dir, name);
            if !std::path::Path::new(&path).exists() {
                std::fs::write(&path, b"").ok();
            }
        }
    }

    tauri_build::build()
}
