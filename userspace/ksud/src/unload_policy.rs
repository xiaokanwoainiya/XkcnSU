use anyhow::{Context, Result, ensure};
use std::fs;
use std::path::Path;

const REFUSAL: &str = "XKCNSU hot-unload is unavailable; reboot with a restored image to remove root. Android services were not stopped.";

pub fn ensure_supported(parameters: &Path) -> Result<()> {
    for parameter in ["unload_safe", "unload_supported"] {
        let value = fs::read_to_string(parameters.join(parameter))
            .with_context(|| format!("{REFUSAL} Cannot verify {parameter}"))?;
        ensure!(matches!(value.trim(), "1" | "Y"), "{REFUSAL}");
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::ensure_supported;
    use std::fs;

    #[test]
    fn missing_sysfs_refuses_unload() {
        let directory = tempfile::tempdir().unwrap();
        assert!(ensure_supported(&directory.path().join("parameters")).is_err());
    }

    #[test]
    fn both_permissions_are_required() {
        for missing in ["unload_safe", "unload_supported"] {
            let directory = tempfile::tempdir().unwrap();
            for parameter in ["unload_safe", "unload_supported"] {
                if parameter != missing {
                    fs::write(directory.path().join(parameter), "1\n").unwrap();
                }
            }
            assert!(ensure_supported(directory.path()).is_err());
        }
    }

    #[test]
    fn unsupported_or_unsafe_provider_refuses_unload() {
        for denied in ["unload_safe", "unload_supported"] {
            let directory = tempfile::tempdir().unwrap();
            for parameter in ["unload_safe", "unload_supported"] {
                let value = if parameter == denied { "0\n" } else { "1\n" };
                fs::write(directory.path().join(parameter), value).unwrap();
            }
            assert!(ensure_supported(directory.path()).is_err());
        }
    }

    #[test]
    fn invalid_values_refuse_unload() {
        let directory = tempfile::tempdir().unwrap();
        fs::write(directory.path().join("unload_supported"), "1\n").unwrap();
        for value in ["", "true", "y", "2", "1\n0", "\0"] {
            fs::write(directory.path().join("unload_safe"), value).unwrap();
            assert!(ensure_supported(directory.path()).is_err(), "{value:?}");
        }
    }

    #[test]
    fn unreadable_parameter_refuses_unload() {
        let directory = tempfile::tempdir().unwrap();
        fs::create_dir(directory.path().join("unload_safe")).unwrap();
        fs::write(directory.path().join("unload_supported"), "1\n").unwrap();
        assert!(ensure_supported(directory.path()).is_err());
    }

    #[test]
    fn explicit_safe_and_supported_provider_can_unload() {
        let directory = tempfile::tempdir().unwrap();
        for value in ["1\n", "Y\n"] {
            for parameter in ["unload_safe", "unload_supported"] {
                fs::write(directory.path().join(parameter), value).unwrap();
            }
            ensure_supported(directory.path()).unwrap();
        }
    }
}
