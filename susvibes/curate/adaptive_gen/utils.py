from susvibes.core.utils import setup_logger

# Set by the pipeline (set_log_dir) before any logging; run_id is unknown at import.
_log_dir = None

def set_log_dir(log_dir):
    global _log_dir
    _log_dir = log_dir

def module_setup_logger(log_file_name: str, logger_name: str, mode: str = "a", add_stdout: bool = True):
    """setup_logger bound to the module-level _log_dir, so adaptive_gen submodules
    don't each pass the log dir."""
    return setup_logger(_log_dir, log_file_name, logger_name, mode, add_stdout)
