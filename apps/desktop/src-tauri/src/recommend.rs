//! Which catalog models this machine can run, and which variant to download.
//!
//! `recommend` is pure so it can be tested with fixture profiles; the command reads the
//! catalog and scans the machine.

use crate::paths::Paths;
use crate::scan::{self, Backend, HardwareProfile};
use serde::{Deserialize, Serialize};
use std::fs;
use std::path::Path;
use tauri::{AppHandle, Manager};

/// The parts of `catalog/models/*.json` the app needs; the host checks the full schema.
#[derive(Deserialize, Clone, Debug)]
pub struct CatalogModel {
    pub id: String,
    pub name: String,
    #[serde(default)]
    pub license: Option<License>,
    #[serde(default)]
    pub quality_rank: i32,
    #[serde(default)]
    pub variants: Vec<Variant>,
}

#[derive(Deserialize, Serialize, Clone, Debug)]
pub struct License {
    pub name: String,
    pub commercial_ok: bool,
}

#[derive(Deserialize, Clone, Debug)]
pub struct Variant {
    pub precision: String,
    pub min_vram_gb: f64,
    pub files: Vec<ModelFile>,
}

#[derive(Deserialize, Clone, Debug)]
pub struct ModelFile {
    pub url: String,
    pub sha256: String,
    pub bytes: u64,
    /// Relative to the models folder, e.g. "vae/ae.safetensors".
    pub dest: String,
}

#[derive(Serialize, Clone, Copy, Debug, PartialEq, PartialOrd)]
#[serde(rename_all = "snake_case")]
pub enum Fit {
    Great,
    Slow,
    WontFit,
}

#[derive(Serialize, Clone, Debug)]
pub struct Recommendation {
    pub id: String,
    pub name: String,
    pub license: Option<License>,
    pub fit: Fit,
    pub reason: String,
    pub precision: Option<String>,
    /// Size of the chosen variant.
    pub bytes: u64,
    pub installed: bool,
}

const GB: f64 = 1e9;
/// Room for the .part file and the OS on top of the download itself.
const DISK_HEADROOM: f64 = 1.2;
/// Share of system RAM a model can use when it runs from RAM instead of GPU memory.
const RAM_SHARE: f64 = 0.75;

/// Every local model, best fit first, then by catalog quality.
pub fn recommend(hw: &HardwareProfile, models: &[CatalogModel], installed: impl Fn(&Variant) -> bool) -> Vec<Recommendation> {
    let mut out: Vec<Recommendation> =
        models.iter().filter(|m| !m.variants.is_empty()).map(|m| recommend_one(hw, m, &installed)).collect();
    out.sort_by(|a, b| {
        let rank = |r: &Recommendation| models.iter().find(|m| m.id == r.id).map_or(0, |m| m.quality_rank);
        a.fit.partial_cmp(&b.fit).unwrap().then(rank(b).cmp(&rank(a)))
    });
    out
}

fn recommend_one(hw: &HardwareProfile, m: &CatalogModel, installed: &impl Fn(&Variant) -> bool) -> Recommendation {
    // Variants are listed best first; take the first that fits the GPU, else the smallest.
    let on_gpu = (hw.backend != Backend::Cpu).then(|| m.variants.iter().find(|v| v.min_vram_gb <= hw.vram_budget_gb)).flatten();
    let variant = on_gpu.unwrap_or_else(|| m.variants.last().unwrap());
    let bytes = variant.files.iter().map(|f| f.bytes).sum::<u64>();
    let is_installed = installed(variant);
    let need = |gb: f64| format!("{gb:.0} GB");

    let (fit, reason) = if !is_installed && bytes as f64 * DISK_HEADROOM > hw.disk_free_gb * GB {
        (Fit::WontFit, format!("Needs {} of free disk; you have {}.", need(bytes as f64 * DISK_HEADROOM / GB), need(hw.disk_free_gb)))
    } else if on_gpu.is_some() {
        (Fit::Great, format!("Fits in your GPU's {} of memory.", need(hw.vram_budget_gb)))
    } else if variant.min_vram_gb <= hw.ram_gb * RAM_SHARE {
        let why = if hw.backend == Backend::Cpu {
            "No supported GPU, so it runs on the CPU: minutes per image.".to_string()
        } else {
            format!("Needs {} of GPU memory and you have {}, so it runs partly from system memory: slow.", need(variant.min_vram_gb), need(hw.vram_budget_gb))
        };
        (Fit::Slow, why)
    } else {
        (Fit::WontFit, format!("Needs {} of memory; this machine has {}.", need(variant.min_vram_gb), need(hw.ram_gb)))
    };
    Recommendation {
        id: m.id.clone(),
        name: m.name.clone(),
        license: m.license.clone(),
        fit,
        reason,
        precision: Some(variant.precision.clone()),
        bytes,
        installed: is_installed,
    }
}

/// The variant `recommend` picks for this model on this machine.
pub fn chosen_variant<'a>(hw: &HardwareProfile, m: &'a CatalogModel) -> Option<&'a Variant> {
    let precision = recommend_one(hw, m, &|_| false).precision?;
    m.variants.iter().find(|v| v.precision == precision)
}

/// Present at its full size. The download checks the sha256 before the final rename, so a
/// full-size file in place is a verified one.
pub fn variant_installed(models_dir: &Path, v: &Variant) -> bool {
    v.files.iter().all(|f| fs::metadata(models_dir.join(&f.dest)).is_ok_and(|m| m.len() == f.bytes))
}

pub fn load_catalog(catalog_dir: &Path) -> Result<Vec<CatalogModel>, String> {
    let mut models = Vec::new();
    let entries = fs::read_dir(catalog_dir.join("models")).map_err(|e| format!("Can't read the catalog: {e}"))?;
    for entry in entries.flatten() {
        let path = entry.path();
        if path.extension().is_some_and(|e| e == "json") {
            let text = fs::read_to_string(&path).map_err(|e| e.to_string())?;
            models.push(serde_json::from_str(&text).map_err(|e| format!("{}: {e}", path.display()))?);
        }
    }
    Ok(models)
}

pub fn catalog_dir(app: &AppHandle) -> Result<std::path::PathBuf, String> {
    Ok(app.path().resource_dir().map_err(|e| e.to_string())?.join("catalog"))
}

#[tauri::command]
pub async fn recommend_models(app: AppHandle) -> Result<Vec<Recommendation>, String> {
    let catalog = catalog_dir(&app)?;
    let paths = Paths { root: app.path().app_data_dir().map_err(|e| e.to_string())? };
    tauri::async_runtime::spawn_blocking(move || {
        let models = load_catalog(&catalog)?;
        Ok(recommend(&scan::scan(), &models, |v| variant_installed(&paths.models(), v)))
    })
    .await
    .map_err(|e| e.to_string())?
}

#[cfg(test)]
mod tests {
    use super::*;

    fn hw(backend: Backend, vram: f64, ram: f64, disk: f64) -> HardwareProfile {
        HardwareProfile {
            os: "test".into(),
            arch: "test".into(),
            cpu: "test".into(),
            ram_gb: ram,
            disk_free_gb: disk,
            gpus: vec![],
            backend,
            vram_budget_gb: vram,
            warnings: vec![],
        }
    }

    fn variant(precision: &str, min_vram_gb: f64, gb: f64) -> Variant {
        let file = ModelFile { url: String::new(), sha256: String::new(), bytes: (gb * GB) as u64, dest: String::new() };
        Variant { precision: precision.into(), min_vram_gb, files: vec![file] }
    }

    fn model(id: &str, rank: i32, variants: Vec<Variant>) -> CatalogModel {
        CatalogModel { id: id.into(), name: id.into(), license: None, quality_rank: rank, variants }
    }

    fn zit() -> CatalogModel {
        model("zit", 80, vec![variant("bf16", 12.0, 20.7), variant("fp8", 7.0, 12.0)])
    }

    fn pick(hw: &HardwareProfile, m: CatalogModel) -> (Fit, Option<String>) {
        let r = recommend(hw, &[m], |_| false).remove(0);
        (r.fit, r.precision)
    }

    #[test]
    fn picks_the_best_variant_that_fits_the_gpu() {
        assert_eq!(pick(&hw(Backend::Cuda, 24.0, 64.0, 500.0), zit()), (Fit::Great, Some("bf16".into())));
        assert_eq!(pick(&hw(Backend::Cuda, 8.0, 32.0, 500.0), zit()), (Fit::Great, Some("fp8".into())));
        // Apple Silicon 24 GB: the scan budgets 60%, 14.4 GB.
        assert_eq!(pick(&hw(Backend::Mps, 14.4, 24.0, 500.0), zit()), (Fit::Great, Some("bf16".into())));
        assert_eq!(pick(&hw(Backend::Mps, 9.6, 16.0, 500.0), zit()), (Fit::Great, Some("fp8".into())));
    }

    #[test]
    fn too_little_gpu_memory_runs_slow_or_not_at_all() {
        assert_eq!(pick(&hw(Backend::Cuda, 4.0, 32.0, 500.0), zit()), (Fit::Slow, Some("fp8".into())));
        assert_eq!(pick(&hw(Backend::Cpu, 0.0, 16.0, 500.0), zit()), (Fit::Slow, Some("fp8".into())));
        assert_eq!(pick(&hw(Backend::Cpu, 0.0, 8.0, 500.0), zit()).0, Fit::WontFit);
    }

    #[test]
    fn needs_the_disk_space_unless_already_installed() {
        let low_disk = hw(Backend::Cuda, 24.0, 64.0, 20.0);
        assert_eq!(pick(&low_disk, zit()).0, Fit::WontFit);
        assert_eq!(recommend(&low_disk, &[zit()], |_| true)[0].fit, Fit::Great);
    }

    #[test]
    fn sorts_by_fit_then_quality_and_skips_cloud_models() {
        let models = [
            model("big", 95, vec![variant("bf16", 40.0, 30.0)]),
            model("small", 50, vec![variant("bf16", 4.0, 5.0)]),
            zit(),
            model("cloud", 99, vec![]),
        ];
        let ids: Vec<String> = recommend(&hw(Backend::Cuda, 16.0, 32.0, 500.0), &models, |_| false).into_iter().map(|r| r.id).collect();
        assert_eq!(ids, ["zit", "small", "big"]);
    }

    #[test]
    fn shipped_catalog_parses_and_runs_great_on_a_24gb_mac() {
        let models = load_catalog(&Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../catalog")).unwrap();
        let zit = models.iter().find(|m| m.id == "z-image-turbo").unwrap();
        let mac = hw(Backend::Mps, 14.4, 24.0, 500.0);
        assert_eq!(chosen_variant(&mac, zit).unwrap().precision, "bf16");
        assert!(zit.variants.iter().flat_map(|v| &v.files).all(|f| f.url.starts_with("https://") && f.sha256.len() == 64));
    }
}
