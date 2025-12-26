"""Display current configuration."""


def main() -> bool:
    """Display current configuration.
    
    Returns:
        True if successful, False otherwise
    """
    from voice_assistant.config import Config
    
    try:
        config = Config("config/config.yaml")
        
        print("Current Configuration")
        print("=" * 60)
        print(f"Audio Device: {config.audio_device}")
        print(f"Sample Rate: {config.audio_sample_rate} Hz")
        print(f"Channels: {config.audio_channels}")
        print(f"Hotword Threshold: {config.hotword_threshold}")
        print(f"VAD Aggressiveness: {config.vad_aggressiveness}")
        
        # Mask API key
        if config.openai_api_key:
            masked_key = config.openai_api_key[:20] + "*" * 8 + config.openai_api_key[-8:]
            print(f"OpenAI API Key: {masked_key}")
        else:
            print("OpenAI API Key: Not configured")
        
        print("=" * 60)
        return True
        
    except Exception as e:
        print(f"Error loading configuration: {e}")
        return False

