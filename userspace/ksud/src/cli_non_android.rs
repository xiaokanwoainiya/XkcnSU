use anyhow::Result;
use clap::Parser;

use crate::boot_patch::{BootPatchArgs, BootRestoreArgs};
use crate::lkm_image::BootPatchV2Args;
use crate::{apk_sign, defs};

/// XKCNSU image tools
#[derive(Parser, Debug)]
#[command(author, version = defs::VERSION_NAME, about, long_about = None)]
struct Args {
    #[command(subcommand)]
    command: Commands,
}

#[derive(clap::Subcommand, Debug)]
enum Commands {
    /// Inspect or transform the kernel of a boot image
    KpmImage {
        #[command(subcommand)]
        command: crate::kpm_image::ImageCommand,
    },

    /// Patch boot or init_boot images to apply XKCNSU
    BootPatch(BootPatchArgs),

    /// Restore boot or init_boot images patched by XKCNSU
    BootRestore(BootRestoreArgs),

    /// Patch XKCNSU into a boot image
    ///
    /// Always operates on a boot image; never selects init_boot or vendor_boot.
    BootPatchV2(BootPatchV2Args),

    /// Get apk size and hash
    GetSign {
        /// apk path
        apk: String,
    },

    /// show supported kmi versions
    SupportedKmis,
}

pub fn run() -> Result<()> {
    env_logger::init();

    let cli = Args::parse();

    log::info!("command: {:?}", cli.command);

    let result = match cli.command {
        Commands::KpmImage { command } => crate::kpm_image::run_image_command(command),
        Commands::GetSign { apk } => {
            let sign = apk_sign::get_apk_signature(&apk)?;
            println!("size: {:#x}, hash: {}", sign.0, sign.1);
            Ok(())
        }

        Commands::BootPatch(boot_patch) => crate::boot_patch::patch(boot_patch),

        Commands::BootRestore(boot_restore) => crate::boot_patch::restore(boot_restore),

        Commands::BootPatchV2(patch) => crate::lkm_image::patch_boot(&patch),

        Commands::SupportedKmis => {
            let kmi = crate::assets::list_supported_kmi();
            for kmi in &kmi {
                println!("{kmi}");
            }
            Ok(())
        }
    };

    if let Err(e) = &result {
        log::error!("Error: {e:?}");
    }
    result
}
