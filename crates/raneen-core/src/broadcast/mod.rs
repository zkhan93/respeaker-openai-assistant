//! The core's outward face: audio and events on the network.
//!
//! Everything here is a **consumer**, never a stage in the pipeline. The
//! recorder takes its own `AudioBus` cursor; the event publisher is an
//! `EventBus` `Consumer`. Neither can affect dictation, and turning both
//! off leaves the pipeline byte-identical — which is the property that
//! lets always-on recording and hotkey dictation run at the same time
//! without a fourth trigger mode (see docs/PRODUCT.md §4).
//!
//! ```text
//!   AudioBus ──┬──> segment cursor   → VAD + hotkey → STT   (dictation)
//!              └──> recorder cursor  → VAD gate → PUB audio (always-on)
//!
//!   EventBus ──┬──> ProtocolConsumer → stdout
//!              └──> ZmqEvents        → PUB event
//! ```
//!
//! **Always-on records; it does not transcribe.** Decided deliberately:
//! continuous STT would mean either 24/7 cloud billing or 24/7 CPU, and
//! whoever archives the audio can transcribe it later at their leisure.
//! So the recorder holds no engine at all, which is most of why capability
//! 4 costs almost nothing.

pub mod publisher;
pub mod recorder;

use std::time::{SystemTime, UNIX_EPOCH};

/// UTC timestamp, ISO 8601, to the microsecond.
///
/// Hand-rolled rather than pulling in a date library for one field.
///
/// **Deliberately UTC with a `Z`, where the Python broadcaster sends a
/// naive local time.** A recorder writing to a NAS produces filenames and
/// ordering from these; naive local timestamps go backwards once a year at
/// the DST boundary and cannot be compared across machines. Consumers
/// parsing ISO 8601 accept both, so the fix is free.
pub fn iso_now() -> String {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default();
    iso_from_epoch(now.as_secs(), now.subsec_micros())
}

fn iso_from_epoch(epoch_seconds: u64, micros: u32) -> String {
    let days = (epoch_seconds / 86_400) as i64;
    let rem = epoch_seconds % 86_400;
    let (hour, minute, second) = (rem / 3600, (rem % 3600) / 60, rem % 60);
    let (year, month, day) = civil_from_days(days);
    format!("{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}.{micros:06}Z")
}

/// Days since the Unix epoch → civil date (Howard Hinnant's algorithm).
///
/// Shifting the era to start in March puts the leap day at the end of the
/// year, which is what removes every special case for February.
fn civil_from_days(days: i64) -> (i64, u32, u32) {
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = (z - era * 146_097) as u64; // [0, 146096]
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146_096) / 365; // [0, 399]
    let y = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100); // [0, 365]
    let mp = (5 * doy + 2) / 153; // [0, 11]
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32; // [1, 31]
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32; // [1, 12]
    (if m <= 2 { y + 1 } else { y }, m, d)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_epoch_itself() {
        assert_eq!(iso_from_epoch(0, 0), "1970-01-01T00:00:00.000000Z");
    }

    #[test]
    fn known_instants_round_trip() {
        // 2026-08-09T12:34:56Z — the day this was written.
        assert_eq!(
            iso_from_epoch(1_786_278_896, 123_456),
            "2026-08-09T12:34:56.123456Z"
        );
        // 2000-03-01: the day after the leap day of a century leap year,
        // which is where a naive algorithm slips by one.
        assert_eq!(
            iso_from_epoch(951_868_800, 0),
            "2000-03-01T00:00:00.000000Z"
        );
        // 2000-02-29 itself.
        assert_eq!(
            iso_from_epoch(951_782_400, 0),
            "2000-02-29T00:00:00.000000Z"
        );
    }

    #[test]
    fn non_leap_century_boundaries() {
        // 1900 and 2100 are not leap years; 2000 is. Get the rule wrong
        // and every timestamp after the mistake is a day out.
        assert_eq!(
            iso_from_epoch(4_107_542_400, 0),
            "2100-03-01T00:00:00.000000Z"
        );
        assert_eq!(
            iso_from_epoch(4_107_456_000, 0),
            "2100-02-28T00:00:00.000000Z"
        );
    }

    #[test]
    fn timestamps_sort_lexicographically() {
        // The property a NAS consumer relies on when it names files.
        let earlier = iso_from_epoch(1_786_278_896, 0);
        let later = iso_from_epoch(1_786_278_897, 0);
        assert!(earlier < later, "{earlier} !< {later}");
    }
}
