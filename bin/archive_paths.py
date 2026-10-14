"""Map sanitized historical path placeholders into explicitly selected storage."""
import os
from pathlib import Path

def path(value):
    root = Path(os.environ["SYNC_CENTER_HOME"])
    bridge = "/Users/example/AI/LocalData/BUSINESS-Memory-Bridge.nosync"
    if value == bridge or value.startswith(bridge + "/"):
        return str(root / value[len(bridge):].lstrip("/"))
    home = "/Users/example/"
    if value.startswith(home):
        return str(root / "isolated-home" / value[len(home):])
    return value
