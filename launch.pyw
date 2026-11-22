"""Windows launcher without a console window: pythonw launch.pyw
Same as `python -m spm` (see spm/__main__.py)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from spm.__main__ import main  # noqa: E402

main()
