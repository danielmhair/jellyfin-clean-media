from .base import EngineAdapter
from .pureframe_engine import PureFrameEngine
from .subtitle_engine import SubtitleEngine
from .vlm_engine import VLMEngine
from .whisper_engine import WhisperEngine

ENGINES: dict[str, EngineAdapter] = {
    engine.name: engine
    for engine in [VLMEngine(), SubtitleEngine(), PureFrameEngine(), WhisperEngine()]
}

__all__ = [
    "ENGINES",
    "EngineAdapter",
    "PureFrameEngine",
    "SubtitleEngine",
    "VLMEngine",
    "WhisperEngine",
]
