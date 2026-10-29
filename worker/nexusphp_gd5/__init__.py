__all__ = ["NexusPhpGD5Generator", "NexusPhpGD5Recognizer"]


def __getattr__(name):
    if name == "NexusPhpGD5Generator":
        from .generator import NexusPhpGD5Generator

        return NexusPhpGD5Generator
    if name == "NexusPhpGD5Recognizer":
        from .recognizer import NexusPhpGD5Recognizer

        return NexusPhpGD5Recognizer
    raise AttributeError(name)
