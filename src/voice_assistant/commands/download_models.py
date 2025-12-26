"""Download pre-trained hotword models."""

from pathlib import Path


def main() -> bool:
    """Download pre-trained hotword models.
    
    Returns:
        True if successful, False otherwise
    """
    print("Downloading Hotword Models")
    print("=" * 60)
    print()
    
    # Create models directory
    models_dir = Path("models")
    models_dir.mkdir(exist_ok=True)
    print(f"Models directory: {models_dir.absolute()}")
    print()
    
    # openWakeWord downloads models automatically on first use
    print("Initializing openWakeWord (models download automatically)...")
    try:
        from openwakeword.model import Model
        
        # Load model - this will download if not present
        model = Model(wakeword_models=["alexa"])
        
        print("✓ Model loaded successfully")
        print()
        print("Available models:")
        for model_name in model.models.keys():
            print(f"  • {model_name}")
        print()
        print("=" * 60)
        print("✓ Models ready to use")
        print("=" * 60)
        return True
        
    except Exception as e:
        print(f"✗ Failed to download models: {e}")
        print()
        print("Try manually:")
        print("  1. Check internet connection")
        print("  2. Run: uv run python -c 'from openwakeword.model import Model; Model()'")
        print("=" * 60)
        return False

