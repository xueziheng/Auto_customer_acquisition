from typing import Any

class TTFError(Exception): ...

class TTFont:
    face: Any

    def __init__(self, name: str, filename: str) -> None: ...
