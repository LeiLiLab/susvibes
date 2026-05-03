from susvibes.curate.constants import ADAPTIVE_GEN_LOG_DIR
from susvibes.utils import setup_logger as _setup_logger

_log_dir = ADAPTIVE_GEN_LOG_DIR

def set_log_dir(log_dir):
    global _log_dir
    _log_dir = log_dir

def setup_logger(log_file_name: str, logger_name: str, mode: str = "a", add_stdout: bool = True):
    return _setup_logger(_log_dir, log_file_name, logger_name, mode, add_stdout)
