//! PCM16 arriving from the host, and the loudness numbers the meter wants.
//!
//! Raneen owns the microphone (ROADMAP AD-16) and writes 16 kHz mono
//! PCM16 down an AF_UNIX socket. So there is no device code here at all —
//! this module is the far side of that socket and nothing else.

/// What the protocol declares in `ready.audio`. Frames are re-blocked to
/// this size because a socket read never lands on a frame boundary.
pub const SAMPLE_RATE: usize = 16_000;
pub const CHUNK_SAMPLES: usize = 1280; // 80 ms
pub const CHUNK_BYTES: usize = CHUNK_SAMPLES * 2;

/// Blocks per frame in a `level` event. Four 20 ms readings per 80 ms
/// frame is what lets the meter track syllables rather than words —
/// see ActivityMeter's `step()` for why they are then drained one at a
/// time rather than applied together.
pub const LEVEL_BLOCKS: usize = 4;

/// Peak and per-block RMS for one frame, as `level` reports them.
///
/// Integers in 0..=32767, matching the Python helper exactly — the Swift
/// side's noise floor (60) and minimum ceiling (900) are calibrated
/// against this scale, so changing the units here would silently
/// recalibrate the meter.
pub fn levels(samples: &[i16]) -> (i32, Vec<i32>) {
    let peak = samples.iter().map(|s| (*s as i32).abs()).max().unwrap_or(0);

    let block = samples.len().div_ceil(LEVEL_BLOCKS).max(1);
    let rms = samples
        .chunks(block)
        .map(|chunk| {
            // f64 accumulator: 1280 squared i16s overflow i32 comfortably
            // and would wrap to a negative RMS.
            let sum: f64 = chunk.iter().map(|s| (*s as f64) * (*s as f64)).sum();
            (sum / chunk.len() as f64).sqrt() as i32
        })
        .collect();

    (peak, rms)
}

/// Re-block an arbitrary byte stream into whole PCM16 frames.
///
/// A socket hands back whatever happens to be in the buffer, so a frame
/// routinely straddles two reads. Dropping the remainder would put a
/// click at every read boundary and lose audio; carrying it is the
/// entire job of this type.
#[derive(Default)]
pub struct FrameBuffer {
    carry: Vec<u8>,
}

impl FrameBuffer {
    /// Feed raw bytes, get back whole frames as i16 samples.
    pub fn push(&mut self, bytes: &[u8]) -> Vec<Vec<i16>> {
        self.carry.extend_from_slice(bytes);

        let mut frames = Vec::new();
        while self.carry.len() >= CHUNK_BYTES {
            let frame: Vec<i16> = self.carry[..CHUNK_BYTES]
                .chunks_exact(2)
                .map(|p| i16::from_le_bytes([p[0], p[1]]))
                .collect();
            self.carry.drain(..CHUNK_BYTES);
            frames.push(frame);
        }
        frames
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn silence_reads_as_zero() {
        let (peak, rms) = levels(&[0i16; CHUNK_SAMPLES]);
        assert_eq!(peak, 0);
        assert_eq!(rms, vec![0; LEVEL_BLOCKS]);
    }

    #[test]
    fn full_scale_does_not_overflow() {
        // The case that wraps to negative with an i32 accumulator.
        let (peak, rms) = levels(&[i16::MAX; CHUNK_SAMPLES]);
        assert_eq!(peak, 32767);
        assert!(rms.iter().all(|v| *v > 32_000), "rms wrapped: {rms:?}");
    }

    #[test]
    fn frames_reassemble_across_split_reads() {
        let mut buf = FrameBuffer::default();
        let bytes = vec![0u8; CHUNK_BYTES * 2];

        // Deliberately unaligned: 3 bytes is half a sample plus one.
        assert!(buf.push(&bytes[..3]).is_empty());
        let frames = buf.push(&bytes[3..]);
        assert_eq!(frames.len(), 2);
        assert!(frames.iter().all(|f| f.len() == CHUNK_SAMPLES));
    }

}
