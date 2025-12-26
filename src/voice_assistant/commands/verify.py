"""Verify installation and dependencies."""

import sys
import logging

logger = logging.getLogger(__name__)


def main() -> bool:
    """Verify installation and dependencies.
    
    Returns:
        True if all checks pass, False otherwise
    """
    print("Verifying Voice Assistant Installation")
    print("=" * 60)
    print()
    
    all_checks_passed = True
    
    # Check 1: Python version
    print("1. Python Version")
    python_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    print(f"   Version: {python_version}")
    if sys.version_info >= (3, 11):
        print("   ✓ Python 3.11+ detected")
    else:
        print("   ✗ Python 3.11+ required")
        all_checks_passed = False
    print()
    
    # Check 2: Audio device
    print("2. Audio Device")
    try:
        from voice_assistant.core import AudioHandler
        audio_handler = AudioHandler()
        device_index = audio_handler._find_device_index()
        print(f"   ✓ Found AC108 device (index {device_index})")
    except Exception as e:
        print(f"   ✗ AC108 device not found: {e}")
        all_checks_passed = False
    print()
    
    # Check 3: Hotword models
    print("3. Hotword Models")
    try:
        from voice_assistant.core import HotwordDetector
        detector = HotwordDetector()
        model_info = detector.get_model_info()
        print(f"   ✓ Models loaded: {', '.join(model_info['models'])}")
    except Exception as e:
        print(f"   ✗ Failed to load models: {e}")
        print("   Run: voice-assistant download-models")
        all_checks_passed = False
    print()
    
    # Check 4: Configuration
    print("4. Configuration")
    try:
        from voice_assistant.config import load_config
        config = load_config("config/config.yaml")
        if config.openai_api_key and config.openai_api_key.startswith("sk-"):
            print("   ✓ Configuration loaded")
            print(f"   ✓ OpenAI API key configured")
        else:
            print("   ✗ OpenAI API key not configured")
            all_checks_passed = False
    except Exception as e:
        print(f"   ✗ Configuration error: {e}")
        all_checks_passed = False
    print()
    
    # Summary
    print("=" * 60)
    if all_checks_passed:
        print("✓ All checks passed! Ready to use.")
        print()
        print("Next steps:")
        print("  voice-assistant test-hotword    # Test hotword detection")
        print("  voice-assistant test-stt        # Test speech-to-text")
        print("  voice-assistant run             # Run the service")
    else:
        print("✗ Some checks failed. Please fix the issues above.")
    print("=" * 60)
    
    return all_checks_passed

