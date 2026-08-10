//! Resident-set reporting, so the spike measures itself.
//!
//! Deliberately the *same* metric the Python baseline was taken with —
//! `getrusage(RUSAGE_SELF).ru_maxrss` — because a comparison between two
//! different memory metrics is worse than no comparison at all. The
//! Python numbers to beat, on this machine:
//!
//! | stage                  | RSS    |
//! |------------------------|--------|
//! | interpreter + numpy    |  34 MB |
//! | + faster-whisper       |  67 MB |
//! | + base.en int8 loaded  | 360 MB |
//! | + one 3 s inference    | 553 MB |

/// Peak resident set size, in MB.
///
/// A high-water mark, not a current reading — it never falls, which is
/// exactly what makes it useful for spotting a transient allocation
/// spike that a sampled `footprint` would miss between samples. For
/// steady-state, measure the live process from outside instead.
pub fn peak_rss_mb() -> f64 {
    let mut usage: libc::rusage = unsafe { std::mem::zeroed() };
    // Cannot fail for RUSAGE_SELF; the zeroed struct is a safe fallback
    // anyway, so there is nothing useful to report on error.
    unsafe { libc::getrusage(libc::RUSAGE_SELF, &mut usage) };

    // The units differ by platform and getting this wrong is a silent
    // 1024x error in the headline number of the whole spike.
    #[cfg(target_os = "macos")]
    {
        usage.ru_maxrss as f64 / 1024.0 / 1024.0 // bytes
    }
    #[cfg(not(target_os = "macos"))]
    {
        usage.ru_maxrss as f64 / 1024.0 // kilobytes
    }
}
