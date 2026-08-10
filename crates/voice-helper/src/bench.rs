//! `bench` — the measurement the spike exists to take.
//!
//! Mirrors the Python baseline stage for stage so the two columns are
//! comparable: import, load, first inference, then a repeat loop to see
//! whether the working set settles or climbs.

use std::path::Path;

use crate::engine::Engine;
use crate::mem::peak_rss_mb;

pub fn run(
    model: &Path,
    wav: &Path,
    threads: i32,
    repeats: usize,
    language: &str,
) -> Result<(), String> {
    println!("{:<26} {:>8} {:>9}", "stage", "peak RSS", "elapsed");
    println!("{}", "-".repeat(46));
    println!(
        "{:<26} {:>7.0} MB {:>8}",
        "process start",
        peak_rss_mb(),
        "-"
    );

    let samples = read_wav(wav)?;
    let seconds = samples.len() as f32 / 16_000.0;
    println!("{:<26} {:>7.0} MB {:>8}", "wav loaded", peak_rss_mb(), "-");

    let started = std::time::Instant::now();
    let engine = Engine::load(model, threads, language)?;
    let load_time = started.elapsed();
    println!(
        "{:<26} {:>7.0} MB {:>7.2}s",
        "model loaded",
        peak_rss_mb(),
        load_time.as_secs_f32()
    );

    for i in 1..=repeats {
        let started = std::time::Instant::now();
        let decoded = engine.transcribe(&samples)?;
        let elapsed = started.elapsed();
        let label = if i == 1 {
            format!("inference 1 ({seconds:.1}s audio)")
        } else {
            format!("inference {i}")
        };
        println!(
            "{:<26} {:>7.0} MB {:>7.2}s",
            label,
            peak_rss_mb(),
            elapsed.as_secs_f32()
        );
        if i == 1 {
            println!(
                "\n  transcript: {:?}\n  confidence: {:.2}\n",
                decoded.text, decoded.confidence
            );
        }
    }

    // Peak RSS never falls, so a flat column across repeats is the
    // evidence that nothing accumulates per segment. Holding here lets a
    // `footprint` from outside catch the steady-state figure, which is
    // the number that matters for an app that idles all day.
    if std::env::var_os("BENCH_HOLD").is_some() {
        eprintln!(
            "holding for external measurement — pid {}",
            std::process::id()
        );
        std::thread::sleep(std::time::Duration::from_secs(120));
    }
    Ok(())
}

/// Read a WAV as mono f32 at 16 kHz.
///
/// No resampling: the spike measures inference, and silently resampling
/// a mismatched file would hide a fixture mistake behind plausible
/// output. Refusing is the same instinct as AD-16 validating the audio
/// format before the pipeline exists.
fn read_wav(path: &Path) -> Result<Vec<f32>, String> {
    let mut reader = hound::WavReader::open(path)
        .map_err(|e| format!("could not open {}: {e}", path.display()))?;
    let spec = reader.spec();
    if spec.sample_rate != 16_000 {
        return Err(format!(
            "{} is {} Hz; this expects 16000 Hz mono PCM16",
            path.display(),
            spec.sample_rate
        ));
    }
    if spec.channels != 1 {
        return Err(format!(
            "{} has {} channels; this expects mono",
            path.display(),
            spec.channels
        ));
    }

    reader
        .samples::<i16>()
        .map(|s| s.map(|v| v as f32 / 32768.0).map_err(|e| e.to_string()))
        .collect()
}
