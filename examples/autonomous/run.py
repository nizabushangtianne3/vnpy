"""Launch the autonomous trading supervisor from a config file.

Usage:

    python examples/autonomous/run.py --config .vntrader/autonomous.json
    python examples/autonomous/run.py check
    python examples/autonomous/run.py sessions --count 5
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from vnpy.autonomous.__main__ import main


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
