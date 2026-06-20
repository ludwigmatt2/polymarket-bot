import os
from pathlib import Path

_ROOT = Path(__file__).parent.parent
DATA_DIR: Path = Path(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or _ROOT)
