"""Opt-in gate for a retired source archive; importing is disabled by default."""
import os
from pathlib import Path

def require_opt_in():
    root = os.environ.get("SYNC_CENTER_HOME", "")
    if os.environ.get("SYNC_CENTER_ENABLE_ARCHIVED_CODE") != "1" or not root or not Path(root).is_absolute():
        raise RuntimeError("Archived integration is disabled. Configure an isolated absolute SYNC_CENTER_HOME and explicitly opt in before using source modules.")
