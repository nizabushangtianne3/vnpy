import os
import time
import sys
import tempfile
from pathlib import Path

import pytest


REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# vnpy.trader.utility 导入时按当前目录决定 .vntrader 位置，vnpy.trader.setting 导入时读取 vt_setting.json。
# 必须在第一次 import vnpy 之前切到带空 .vntrader 的临时目录，避免读写用户真实配置。
ORIGINAL_CWD: str = os.getcwd()
TRADER_TEMP_DIR: tempfile.TemporaryDirectory = tempfile.TemporaryDirectory()
Path(TRADER_TEMP_DIR.name).joinpath(".vntrader").mkdir()
os.chdir(TRADER_TEMP_DIR.name)

from vnpy.trader.setting import SETTINGS  # noqa: E402

# vnpy.trader.logger 导入时按 log.file 创建日志目录，需在它被导入前关闭。
SETTINGS["log.file"] = False
SETTINGS["log.console"] = False


def pytest_unconfigure(config: pytest.Config) -> None:
    os.chdir(ORIGINAL_CWD)
    try:
        _close_database_singletons()
        _cleanup_temp_dir()
    except Exception as exc:
        # Teardown must never fail the run; CI stdout is cp1252, so keep the
        # message ASCII-safe (!a escapes non-ASCII).
        print(f"[conftest] teardown skipped: {exc!a}")


def _close_database_singletons() -> None:
    """Best-effort release of sqlite handles opened by app engines."""
    try:
        import vnpy_sqlite.sqlite_database as sqlite_module

        db = getattr(sqlite_module, "db", None)
        state = getattr(db, "_state", None)
        if db is not None and state is not None and not state.closed:
            db.close()
    except Exception:
        pass

    try:
        import vnpy_ctastrategy.engine as cta_engine_module

        wrapper = getattr(cta_engine_module, "database", None)
        inner = getattr(wrapper, "db", None)
        state = getattr(inner, "_state", None)
        if inner is not None and state is not None and not state.closed:
            inner.close()
    except Exception:
        pass


def _cleanup_temp_dir() -> None:
    """Clean the trader temp dir, tolerating slow Windows file locks."""
    for attempt in range(3):
        try:
            TRADER_TEMP_DIR.cleanup()
            return
        except PermissionError:
            if attempt == 2:
                print(
                    "[conftest] temp dir cleanup skipped, file still locked: "
                    f"{TRADER_TEMP_DIR.name!a}"
                )
                return
            time.sleep(1.0)
