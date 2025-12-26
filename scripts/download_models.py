#!/usr/bin/env python3
"""Download openWakeWord pretrained models using the official utility."""

import openwakeword
from openwakeword.utils import download_models


def main():
    """Download all required models using openWakeWord's built-in utility."""
    print("Downloading openWakeWord pre-trained models...")
    print("This may take a few minutes depending on your internet connection.\n")
    
    try:
        # Use the official download function from openWakeWord
        download_models()
        
        print("\n✅ Successfully downloaded all pre-trained models!")
        print("\nAvailable models:")
        
        # List available models
        model_paths = openwakeword.get_pretrained_model_paths()
        for i, path in enumerate(model_paths, 1):
            model_name = path.split('/')[-1].replace('.tflite', '').replace('_v0.1', '')
            print(f"  {i}. {model_name}")
        
        print("\nYou can now run the voice assistant:")
        print("  uv run python -m voice_assistant")
        
    except Exception as e:
        print(f"\n❌ Error downloading models: {e}")
        print("\nTroubleshooting:")
        print("1. Check your internet connection")
        print("2. Try running again with: uv run python download_models.py")
        print("3. Check openWakeWord documentation: https://github.com/dscripka/openWakeWord")
        return False
    
    return True


if __name__ == "__main__":
    import sys
    success = main()
    sys.exit(0 if success else 1)

