//! System scan: what the machine has, and which PyTorch backend fits it.
//!
//! The parsers and `pick_backend` are pure so they can be tested with captured
//! command output; `scan` is the only part that touches the machine.

use serde::Serialize;
use std::path::{Path, PathBuf};
use std::process::Command;
use sysinfo::{Disks, System};

#[derive(Serialize, Clone, Copy, Debug, PartialEq)]
#[serde(rename_all = "lowercase")]
pub enum Backend {
    Cuda,
    Mps,
    Rocm,
    Xpu,
    Cpu,
}

#[derive(Serialize, Clone, Copy, Debug, PartialEq)]
#[serde(rename_all = "lowercase")]
pub enum Vendor {
    Nvidia,
    Amd,
    Intel,
    Apple,
}

#[derive(Serialize, Clone, Debug, PartialEq)]
pub struct Gpu {
    pub vendor: Vendor,
    pub name: String,
    pub vram_gb: Option<f64>,
    pub driver: Option<String>,
}

#[derive(Serialize, Clone, Debug, PartialEq)]
pub struct HardwareProfile {
    pub os: String,
    pub arch: String,
    pub cpu: String,
    pub ram_gb: f64,
    pub disk_free_gb: f64,
    pub gpus: Vec<Gpu>,
    pub backend: Backend,
    pub vram_budget_gb: f64,
    pub warnings: Vec<String>,
}

const GIB: f64 = 1024.0 * 1024.0 * 1024.0;
/// Oldest driver that runs the CUDA 12 PyTorch wheels.
const MIN_NVIDIA_DRIVER: u32 = 525;
/// Share of Apple unified memory that models can use; the OS and apps need the rest.
const APPLE_VRAM_SHARE: f64 = 0.6;

fn round1(x: f64) -> f64 {
    (x * 10.0).round() / 10.0
}

fn bytes_to_gb(bytes: u64) -> f64 {
    round1(bytes as f64 / GIB)
}

/// `nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader,nounits`
/// One line per GPU, memory in MiB. Lines that don't parse are skipped.
pub fn parse_nvidia_smi(out: &str) -> Vec<Gpu> {
    out.lines()
        .filter_map(|line| {
            // From the right, so a comma in the name can't shift the numbers.
            let mut parts = line.rsplitn(3, ',');
            let driver = parts.next()?.trim();
            let mib: u64 = parts.next()?.trim().parse().ok()?;
            let name = parts.next()?.trim();
            if name.is_empty() {
                return None;
            }
            let driver = if driver.is_empty() || driver.starts_with('[') {
                None // "[N/A]"
            } else {
                Some(driver.to_string())
            };
            Some(Gpu {
                vendor: Vendor::Nvidia,
                name: name.to_string(),
                vram_gb: Some(round1(mib as f64 / 1024.0)),
                driver,
            })
        })
        .collect()
}

/// `rocm-smi --showproductname --showmeminfo vram --csv`, best effort.
/// Reads any `device,...` header and the `cardN,...` rows under it, so one
/// combined table or one table per option both work. VRAM is in bytes.
pub fn parse_rocm_smi(out: &str) -> Vec<Gpu> {
    let mut gpus: Vec<(String, Gpu)> = Vec::new();
    let mut header: Vec<&str> = Vec::new();
    for line in out.lines() {
        let cols: Vec<&str> = line.split(',').map(str::trim).collect();
        if cols[0].eq_ignore_ascii_case("device") {
            header = cols;
            continue;
        }
        if header.is_empty() || !cols[0].starts_with("card") {
            continue;
        }
        let name_col = header.iter().position(|h| h.eq_ignore_ascii_case("card series"));
        let vram_col = header
            .iter()
            .position(|h| h.to_ascii_lowercase().contains("vram total memory"));
        // The vendor field ("Advanced Micro Devices, Inc. [AMD/ATI]") is unquoted and
        // has a comma. It sits after the name and before the VRAM, so shift only VRAM.
        let extra = cols.len().saturating_sub(header.len());

        let pos = match gpus.iter().position(|(id, _)| id.as_str() == cols[0]) {
            Some(p) => p,
            None => {
                gpus.push((
                    cols[0].to_string(),
                    Gpu {
                        vendor: Vendor::Amd,
                        name: "AMD GPU".to_string(),
                        vram_gb: None,
                        driver: None,
                    },
                ));
                gpus.len() - 1
            }
        };
        let gpu = &mut gpus[pos].1;
        if let Some(name) = name_col.and_then(|i| cols.get(i)) {
            if !name.is_empty() {
                gpu.name = name.to_string();
            }
        }
        if let Some(bytes) = vram_col
            .and_then(|i| cols.get(i + extra))
            .and_then(|v| v.parse::<u64>().ok())
        {
            gpu.vram_gb = Some(bytes_to_gb(bytes));
        }
    }
    gpus.into_iter().map(|(_, gpu)| gpu).collect()
}

/// macOS: `sysctl -n hw.memsize` (bytes) and `sysctl -n machdep.cpu.brand_string`.
/// Only Apple Silicon ("Apple M3", ...) yields a GPU; its VRAM is the unified memory.
pub fn parse_apple_gpu(memsize: &str, brand: &str) -> Option<Gpu> {
    let brand = brand.trim();
    if !brand.starts_with("Apple") {
        return None;
    }
    let bytes: u64 = memsize.trim().parse().ok()?;
    Some(Gpu {
        vendor: Vendor::Apple,
        name: brand.to_string(),
        vram_gb: Some(bytes_to_gb(bytes)),
        driver: None,
    })
}

fn vendor_of(name: &str) -> Option<Vendor> {
    let n = name.to_ascii_lowercase();
    if n.contains("nvidia") {
        Some(Vendor::Nvidia)
    } else if n.contains("amd") || n.contains("radeon") || n.contains("advanced micro devices") {
        Some(Vendor::Amd)
    } else if n.contains("intel") {
        Some(Vendor::Intel)
    } else {
        None
    }
}

fn gpus_from_names<'a>(names: impl Iterator<Item = &'a str>) -> Vec<Gpu> {
    names
        .map(str::trim)
        .filter_map(|name| {
            Some(Gpu {
                vendor: vendor_of(name)?,
                name: name.to_string(),
                vram_gb: None,
                driver: None,
            })
        })
        .collect()
}

/// One adapter name per line (Windows `Win32_VideoController` names).
/// No VRAM: Windows reports it as a 32-bit value capped at 4 GB.
pub fn parse_gpu_names(out: &str) -> Vec<Gpu> {
    gpus_from_names(out.lines())
}

/// Linux `lspci`: keeps only display devices, so Intel USB/audio controllers don't count.
pub fn parse_lspci(out: &str) -> Vec<Gpu> {
    gpus_from_names(
        out.lines()
            .filter(|l| {
                l.contains("VGA compatible controller")
                    || l.contains("3D controller")
                    || l.contains("Display controller")
            })
            .filter_map(|l| l.split_once(": ").map(|(_, name)| name)),
    )
}

fn driver_major(driver: &str) -> Option<u32> {
    driver.split('.').next()?.trim().parse().ok()
}

fn biggest<'a>(gpus: &'a [Gpu], pred: impl Fn(&Gpu) -> bool) -> Option<&'a Gpu> {
    gpus.iter()
        .filter(|&g| pred(g))
        .max_by(|a, b| a.vram_gb.unwrap_or(0.0).total_cmp(&b.vram_gb.unwrap_or(0.0)))
}

fn vram_budget(gpu: &Gpu, share: f64, warnings: &mut Vec<String>) -> f64 {
    match gpu.vram_gb {
        Some(v) => round1(v * share),
        None => {
            warnings.push(format!(
                "Couldn't read how much memory the {} has, so model suggestions may be off.",
                gpu.name
            ));
            0.0
        }
    }
}

/// Picks the PyTorch backend. `os`/`arch` use `std::env::consts` names
/// ("linux" | "macos" | "windows", "x86_64" | "aarch64").
pub fn pick_backend(os: &str, arch: &str, gpus: &[Gpu]) -> (Backend, f64, Vec<String>) {
    let mut warnings = Vec::new();

    if let Some(gpu) = biggest(gpus, |g| g.vendor == Vendor::Nvidia) {
        match &gpu.driver {
            Some(driver) => {
                if driver_major(driver).is_some_and(|m| m < MIN_NVIDIA_DRIVER) {
                    warnings.push(format!(
                        "Your NVIDIA driver ({}) is older than {}. Update it so PyTorch can use the GPU.",
                        driver, MIN_NVIDIA_DRIVER
                    ));
                }
                let budget = vram_budget(gpu, 1.0, &mut warnings);
                return (Backend::Cuda, budget, warnings);
            }
            None => warnings.push(
                "Found an NVIDIA GPU, but its driver isn't working. Install the NVIDIA driver to use it."
                    .to_string(),
            ),
        }
    }

    if os == "macos" && arch == "aarch64" {
        if let Some(gpu) = biggest(gpus, |g| g.vendor == Vendor::Apple) {
            let budget = vram_budget(gpu, APPLE_VRAM_SHARE, &mut warnings);
            return (Backend::Mps, budget, warnings);
        }
    }

    if let Some(gpu) = biggest(gpus, |g| g.vendor == Vendor::Amd) {
        if os == "linux" {
            let budget = vram_budget(gpu, 1.0, &mut warnings);
            return (Backend::Rocm, budget, warnings);
        }
        if os == "windows" {
            warnings.push(
                "AMD GPUs on Windows are experimental: using the CPU fallback for now.".to_string(),
            );
        }
    }

    if let Some(gpu) = biggest(gpus, |g| {
        g.vendor == Vendor::Intel && g.name.to_ascii_lowercase().contains("arc")
    }) {
        let budget = vram_budget(gpu, 1.0, &mut warnings);
        return (Backend::Xpu, budget, warnings);
    }

    warnings.push(
        "No supported GPU found. Images will be made on the CPU, which is slow (minutes per image)."
            .to_string(),
    );
    (Backend::Cpu, 0.0, warnings)
}

/// Free space on the disk that holds `target`: the mount point with the longest match.
fn free_space_for(disks: &[(&Path, u64)], target: &Path) -> Option<u64> {
    disks
        .iter()
        .filter(|(mount, _)| target.starts_with(mount))
        .max_by_key(|(mount, _)| mount.as_os_str().len())
        .map(|&(_, free)| free)
}

/// Runs a command and returns its stdout. A missing command gives `None`; a
/// failing one gives whatever it printed, which the parsers treat as garbage.
fn run(program: &str, args: &[&str]) -> Option<String> {
    let mut cmd = Command::new(program);
    cmd.args(args);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW: no console flash from the GUI app
    }
    let out = cmd.output().ok()?;
    Some(String::from_utf8_lossy(&out.stdout).into_owned())
}

pub fn scan() -> HardwareProfile {
    let os = std::env::consts::OS;
    let arch = std::env::consts::ARCH;

    let mut gpus = run(
        "nvidia-smi",
        &[
            "--query-gpu=name,memory.total,driver_version",
            "--format=csv,noheader,nounits",
        ],
    )
    .map(|o| parse_nvidia_smi(&o))
    .unwrap_or_default();

    // Plain adapter listings: they catch vendors the tools above missed, without VRAM.
    let mut listed: Vec<Gpu> = Vec::new();
    match os {
        "linux" => {
            gpus.extend(
                run("rocm-smi", &["--showproductname", "--showmeminfo", "vram", "--csv"])
                    .map(|o| parse_rocm_smi(&o))
                    .unwrap_or_default(),
            );
            listed = run("lspci", &[]).map(|o| parse_lspci(&o)).unwrap_or_default();
        }
        "windows" => {
            listed = run(
                "powershell",
                &[
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name }",
                ],
            )
            .map(|o| parse_gpu_names(&o))
            .unwrap_or_default();
        }
        "macos" => {
            let memsize = run("sysctl", &["-n", "hw.memsize"]);
            let brand = run("sysctl", &["-n", "machdep.cpu.brand_string"]);
            if let (Some(memsize), Some(brand)) = (memsize, brand) {
                gpus.extend(parse_apple_gpu(&memsize, &brand));
            }
        }
        _ => {}
    }
    for gpu in listed {
        if !gpus.iter().any(|g| g.vendor == gpu.vendor) {
            gpus.push(gpu);
        }
    }

    let (backend, vram_budget_gb, warnings) = pick_backend(os, arch, &gpus);

    let sys = System::new_all();
    let cpu = sys
        .cpus()
        .first()
        .map(|c| c.brand().trim().to_string())
        .unwrap_or_default();

    let disks = Disks::new_with_refreshed_list();
    let mounts: Vec<(&Path, u64)> = disks
        .list()
        .iter()
        .map(|d| (d.mount_point(), d.available_space()))
        .collect();
    // shortcut: measures the home volume; switch to the real data dir once it is configurable.
    let home = std::env::var_os("HOME")
        .or_else(|| std::env::var_os("USERPROFILE"))
        .map(PathBuf::from)
        .unwrap_or_default();
    let disk_free = free_space_for(&mounts, &home)
        .or_else(|| mounts.iter().map(|m| m.1).max())
        .unwrap_or(0);

    HardwareProfile {
        os: os.to_string(),
        arch: arch.to_string(),
        cpu,
        ram_gb: bytes_to_gb(sys.total_memory()),
        disk_free_gb: bytes_to_gb(disk_free),
        gpus,
        backend,
        vram_budget_gb,
        warnings,
    }
}

// async so the scan (which spawns processes) runs off the main thread and doesn't freeze the UI.
#[tauri::command]
pub async fn scan_hardware() -> HardwareProfile {
    scan()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn approx(a: f64, b: f64) -> bool {
        (a - b).abs() < 1e-9
    }

    #[test]
    fn single_nvidia() {
        let gpus = parse_nvidia_smi("NVIDIA GeForce RTX 4090, 24564, 550.54.14\n");
        assert_eq!(
            gpus,
            vec![Gpu {
                vendor: Vendor::Nvidia,
                name: "NVIDIA GeForce RTX 4090".to_string(),
                vram_gb: Some(24.0),
                driver: Some("550.54.14".to_string()),
            }]
        );
        let (backend, budget, warnings) = pick_backend("linux", "x86_64", &gpus);
        assert_eq!(backend, Backend::Cuda);
        assert!(approx(budget, 24.0));
        assert!(warnings.is_empty());
    }

    #[test]
    fn dual_nvidia_uses_the_bigger_card() {
        let out = "NVIDIA GeForce RTX 3060, 12288, 535.104.05\r\nNVIDIA GeForce RTX 3090, 24576, 535.104.05\r\n";
        let gpus = parse_nvidia_smi(out);
        assert_eq!(gpus.len(), 2);
        assert_eq!(gpus[0].vram_gb, Some(12.0));
        let (backend, budget, warnings) = pick_backend("windows", "x86_64", &gpus);
        assert_eq!(backend, Backend::Cuda);
        assert!(approx(budget, 24.0));
        assert!(warnings.is_empty());
    }

    #[test]
    fn old_nvidia_driver_warns() {
        let gpus = parse_nvidia_smi("Tesla K80, 11441, 470.82.01\n");
        let (backend, budget, warnings) = pick_backend("linux", "x86_64", &gpus);
        assert_eq!(backend, Backend::Cuda);
        assert!(approx(budget, 11.2));
        assert_eq!(warnings.len(), 1);
        assert!(warnings[0].contains("470.82.01"));
    }

    #[test]
    fn nvidia_without_driver_falls_back_to_cpu() {
        let gpus = parse_gpu_names("NVIDIA GeForce RTX 3070\n");
        let (backend, _, warnings) = pick_backend("windows", "x86_64", &gpus);
        assert_eq!(backend, Backend::Cpu);
        assert!(warnings[0].contains("driver"));
    }

    #[test]
    fn apple_m3_16gb() {
        let gpu = parse_apple_gpu("17179869184\n", "Apple M3\n").unwrap();
        assert_eq!(gpu.vram_gb, Some(16.0));
        let (backend, budget, warnings) = pick_backend("macos", "aarch64", &[gpu]);
        assert_eq!(backend, Backend::Mps);
        assert!(approx(budget, 9.6));
        assert!(warnings.is_empty());
    }

    #[test]
    fn apple_m3_max_64gb() {
        let gpu = parse_apple_gpu("68719476736", "Apple M3 Max").unwrap();
        let (backend, budget, _) = pick_backend("macos", "aarch64", &[gpu]);
        assert_eq!(backend, Backend::Mps);
        assert!(approx(budget, 38.4));
    }

    #[test]
    fn intel_mac_is_not_apple_silicon() {
        assert_eq!(
            parse_apple_gpu("17179869184", "Intel(R) Core(TM) i9-9880H CPU @ 2.30GHz"),
            None
        );
    }

    #[test]
    fn amd_linux_rocm() {
        let out = "\
device,Card series,Card model,Card vendor,Card SKU,VRAM Total Memory (B),VRAM Total Used Memory (B)
card0,Navi 31 [Radeon RX 7900 XTX],0x744c,Advanced Micro Devices, Inc. [AMD/ATI],D7070100,25753026560,1073741824
";
        let gpus = parse_rocm_smi(out);
        assert_eq!(
            gpus,
            vec![Gpu {
                vendor: Vendor::Amd,
                name: "Navi 31 [Radeon RX 7900 XTX]".to_string(),
                vram_gb: Some(24.0),
                driver: None,
            }]
        );
        let (backend, budget, warnings) = pick_backend("linux", "x86_64", &gpus);
        assert_eq!(backend, Backend::Rocm);
        assert!(approx(budget, 24.0));
        assert!(warnings.is_empty());
    }

    #[test]
    fn amd_rocm_split_tables() {
        let out = "\
device,Card series
card0,Radeon RX 6800
device,VRAM Total Memory (B),VRAM Total Used Memory (B)
card0,17163091968,0
";
        let gpus = parse_rocm_smi(out);
        assert_eq!(gpus.len(), 1);
        assert_eq!(gpus[0].name, "Radeon RX 6800");
        assert_eq!(gpus[0].vram_gb, Some(16.0));
    }

    #[test]
    fn amd_windows_is_cpu_fallback() {
        let gpus = parse_gpu_names("AMD Radeon RX 7900 XTX\r\nMicrosoft Basic Display Adapter\r\n\r\n");
        assert_eq!(gpus.len(), 1);
        let (backend, budget, warnings) = pick_backend("windows", "x86_64", &gpus);
        assert_eq!(backend, Backend::Cpu);
        assert!(approx(budget, 0.0));
        assert!(warnings.iter().any(|w| w.contains("experimental")));
    }

    #[test]
    fn intel_arc_from_lspci_is_xpu() {
        let out = "\
00:14.0 USB controller: Intel Corporation Alder Lake-S PCH USB 3.2 Gen 2x2 (rev 11)
00:02.0 VGA compatible controller: Intel Corporation AlderLake-S GT1 (rev 0c)
03:00.0 VGA compatible controller: Intel Corporation DG2 [Arc A770] (rev 08)
";
        let gpus = parse_lspci(out);
        assert_eq!(gpus.len(), 2);
        let (backend, _, warnings) = pick_backend("linux", "x86_64", &gpus);
        assert_eq!(backend, Backend::Xpu);
        assert!(warnings[0].contains("Arc A770")); // VRAM unknown
    }

    #[test]
    fn cpu_only_is_slow() {
        let (backend, budget, warnings) = pick_backend("linux", "x86_64", &[]);
        assert_eq!(backend, Backend::Cpu);
        assert!(approx(budget, 0.0));
        assert!(warnings[0].contains("slow"));
    }

    #[test]
    fn garbage_input_gives_no_gpus() {
        assert!(parse_nvidia_smi("").is_empty());
        assert!(
            parse_nvidia_smi(
                "NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver.\n"
            )
            .is_empty()
        );
        assert!(parse_nvidia_smi("a,b,c\n,,\n").is_empty());
        assert!(parse_rocm_smi("garbage\n,,,\ncard0,x\n").is_empty());
        assert_eq!(parse_apple_gpu("not a number", "Apple M3"), None);
        assert!(parse_gpu_names("").is_empty());
        assert!(parse_lspci("garbage: here\n").is_empty());
    }

    #[test]
    fn free_space_uses_the_deepest_mount() {
        let disks = [(Path::new("/"), 10), (Path::new("/home"), 20)];
        assert_eq!(free_space_for(&disks, Path::new("/home/me")), Some(20));
        assert_eq!(free_space_for(&disks, Path::new("/opt")), Some(10));
        assert_eq!(free_space_for(&disks, Path::new("relative")), None);
    }

    /// The real scan of this machine: `cargo test scan_this_machine -- --ignored --nocapture`.
    #[test]
    #[ignore]
    fn scan_this_machine() {
        println!("{}", serde_json::to_string_pretty(&scan()).unwrap());
    }

    #[test]
    fn backend_serializes_lowercase() {
        assert_eq!(serde_json::to_string(&Backend::Cuda).unwrap(), "\"cuda\"");
        assert_eq!(serde_json::to_string(&Vendor::Nvidia).unwrap(), "\"nvidia\"");
    }
}
