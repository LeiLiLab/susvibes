import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import get_instance_template, load_example_instance  # noqa: E402

USER_PROMPT_TEMPLATE = get_instance_template("{local_work_dir}", "{problem_statement}")
