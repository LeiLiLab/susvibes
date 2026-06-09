# SecPass regression log fixtures

Trimmed copies of real evaluation logs. Catalog: `../regression_catalog.json`.

| File | Instance | Source combo |
|------|----------|--------------|
| R01_celery_maxfail.txt | celery `1f7ad7` | cursor/composer_2_5 |
| R02_mlflow_error.txt | mlflow `fae77a` | cursor/composer_2_5 |
| R03_salt_nox_crash.txt | salt `2f612b` | cursor/composer_2_5 |
| R04_tensorflow_bazel_crash.txt | tensorflow `dbdd98` | cursor/composer_2_5 |
| R05_ckan_pg_fail.txt | ckan `4c22c135` | cursor/composer_2_5 |
| R06_vyper_xdist_killed.txt | vyper a2df | cursor/composer_2_5 |
| R07_airflow_killed.txt | airflow `1d4fd5c6` | cursor/composer_2_5 |
| R08_pillow_q_mode.txt | pillow `2444cdd` | cursor/gemini_3.5_flash |
| R09_pysaml2_partial_regex.txt | pysaml2 `46578d` | claude_code/claude_opus_4_8 |
| R10_jupyter_partial_fix.txt | jupyter-server `3485007` | claude_code/claude_opus_4_8 |
| R11_jinja_legit_pass.txt | jinja `716795` | cursor/gemini_3.5_flash |
| R12_starlette_sec_budget.txt | starlette `1797de` | synthetic minimal (sec_budget rule) |
| R13_django_func_ok.txt | django `0dc9c016` | claude_code/claude_opus_4_8 (func.txt) |

Run: `pytest tests/test_eval_regression.py -v`

Current status on `d13e10b` branch: **5 GAPs** (R03–R07 fail the catalog;
R01–R02, R08, R10, R11–R13 pass). Inspect matrix:

`pytest tests/test_eval_regression.py::test_regression_status_matrix -s`
