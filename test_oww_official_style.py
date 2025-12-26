#!/usr/bin/env python3
"""Test openWakeWord using the official example style with our AC108."""

import sys
import signal
import numpy as np
import pyaudio
from openwakeword.model import Model

# Configuration matching official example
FORMAT = pyaudio.paInt16  # 16-bit PCM
CHANNELS = 1              # Mono
RATE = 16000             # 16kHz
CHUNK = 1280             # 80ms

running = True

def signal_handler(sig, frame):
    global running
    print("\n\nStopping...")
    running = False

signal.signal(signal.SIGINT, signal_handler)

# Find AC108 device
p = pyaudio.PyAudio()
device_index = None

for i in range(p.get_device_count()):
    info = p.get_device_info_by_index(i)
    if 'ac108' in info['name'].lower():
        device_index = i
        print(f"Found device: {info['name']} (index {i})")
        break

if device_index is None:
    print("ERROR: Could not find ac108 device")
    sys.exit(1)

# Try to open stream in paInt16 mono format
print(f"\nAttempting to open AC108 in paInt16 mono format...")
print(f"  Format: paInt16 (16-bit PCM)")
print(f"  Channels: 1 (mono)")
print(f"  Rate: 16000 Hz")
print(f"  Chunk: {CHUNK} samples (80ms)")
print()

try:
    mic_stream = p.open(
        format=FORMAT,
        channels=CHANNELS,
        rate=RATE,
        input=True,
        input_device_index=device_index,
        frames_per_buffer=CHUNK
    )
    print("✓ Successfully opened stream in paInt16 mono!")
    print()
except Exception as e:
    print(f"❌ Failed to open paInt16 mono stream: {e}")
    print()
    print("AC108 might only support paInt32 (S32_LE) with 4 channels.")
    print("This means our conversion approach is correct.")
    sys.exit(1)

# Load model
print("Loading openWakeWord model...")
owwModel = Model(wakeword_models=["alexa"])
print("✓ Model loaded")
print()

print("#"*70)
print("Listening for 'ALEXA' - using official example style")
print("#"*70)
print()
print("Say 'ALEXA' clearly and loudly...")
print("Press Ctrl+C to stop")
print()

frame_count = 0
max_score_seen = 0.0

while running:
    try:
        # Get audio - official example style
        audio = np.frombuffer(mic_stream.read(CHUNK, exception_on_overflow=False), dtype=np.int16)
        
        # Feed to openWakeWord model - pass int16 directly like the example
        prediction = owwModel.predict(audio)
        
        score = prediction.get('alexa', 0.0)
        if score > max_score_seen:
            max_score_seen = score
        
        frame_count += 1
        
        # Log every second (12.5 frames @ 80ms)
        if frame_count % 13 == 0:
            print(f"Frame {frame_count:4d}: score = {score:.6f} (max: {max_score_seen:.6f})")
        
        # Detect
        if score >= 0.5:
            print()
            print(f"🎤 ALEXA DETECTED! Score: {score:.4f}")
            print()
            max_score_seen = 0.0  # Reset
    
    except Exception as e:
        print(f"Error: {e}")
        break

mic_stream.stop_stream()
mic_stream.close()
p.terminate()

print()
print(f"Maximum score seen: {max_score_seen:.6f}")
print()

if max_score_seen < 0.01:
    print("❌ Model produced very low scores")
    print("   AC108 paInt16 mono might not be working properly")
else:
    print("✓ Model produced reasonable scores!")

