import time
import importlib
import traceback

from joblib import Parallel, delayed
import pandas as pd

from cowidev.utils.log import get_logger, print_eoe, system_details
from cowidev.utils.utils import export_timestamp
from cowidev.utils.s3 import obj_from_s3, obj_to_s3
from cowidev.utils.clean.dates import localdate


# Logger
logger = get_logger()

# S3 paths
LOG_MACHINES = "s3://covid-19/log/machines.json"
LOG_GET_COUNTRIES = "s3://covid-19/log/{}-get-data-countries.csv"
LOG_GET_GLOBAL = "s3://covid-19/log/{}-get-data-global.csv"


class CountryDataGetter:
    def __init__(self, modules_skip: list = [], log_header: str = ""):
        self.modules_skip = modules_skip
        self.log_header = log_header

    def _skip_module(self, module_name):
        return module_name in self.modules_skip

    def run(self, module_name: str):
        t0 = time.time()
        # Check country skipping
        if self._skip_module(module_name):
            logger.info(f"{self.log_header} - {module_name}: skipped! ⚠️")
            return {"module_name": module_name, "success": None, "skipped": True, "time": None, "error": ""}
        # Start country scraping
        logger.info(f"{self.log_header} - {module_name}: started")
        module = importlib.import_module(module_name)
        try:
            module.main()
        except Exception as err:
            success = False
            logger.error(f"{self.log_header} - {module_name}: ❌ {err}", exc_info=True)
            error_msg = "".join(traceback.TracebackException.from_exception(err).format())
        else:
            success = True
            logger.info(f"{self.log_header} - {module_name}: SUCCESS ✅")
            error_msg = ""
        t = round(time.time() - t0, 2)
        return {"module_name": module_name, "success": success, "skipped": False, "time": t, "error": error_msg}


def main_get_data(
    modules: list,
    parallel: bool = False,
    n_jobs: int = -2,
    modules_skip: list = [],
    log_header: str = "",
    log_s3_path=None,
    output_status: str = None,
    output_status_ts: str = None,
):
    """Get data from sources and export to output folder.

    Is equivalent to script `run_python_scripts.py`
    """
    t0 = time.time()
    print("-- Getting data... --")
    country_data_getter = CountryDataGetter(modules_skip, log_header)
    if log_s3_path:
        modules = _load_modules_order(modules, log_s3_path)
    if parallel:
        modules_execution_results = Parallel(n_jobs=n_jobs, backend="threading")(
            delayed(country_data_getter.run)(
                module_name,
            )
            for module_name in modules
        )
    else:
        modules_execution_results = []
        for module_name in modules:
            modules_execution_results.append(
                country_data_getter.run(
                    module_name,
                )
            )
    t_sec_1 = round(time.time() - t0, 2)
    # Get timing dataframe
    df_exec = _build_df_execution(modules_execution_results)
    if output_status is not None:
        # (modules_all is not None) and (len(df_exec) == len(modules_all)):
        df_status = _build_df_status(modules_execution_results)
        df_status.to_csv(output_status)
        export_timestamp(output_status_ts)
    # Retry failed modules
    _retry_modules_failed(modules_execution_results, country_data_getter)
    # Print timing details
    t_sec_1, t_min_1, t_sec_2, t_min_2 = _print_timing(t0, t_sec_1, df_exec)
    print_eoe()


def _build_df_execution(modules_execution_results):
    df_exec = (
        pd.DataFrame(
            [
                {"module": m["module_name"], "execution_time (sec)": m["time"], "success": m["success"]}
                for m in modules_execution_results
            ]
        )
        .set_index("module")
        .sort_values(by="execution_time (sec)", ascending=False)
    )
    return df_exec


def _build_df_status(modules_execution_results):
    df_exec = (
        pd.DataFrame(
            [
                {
                    "module": m["module_name"],
                    "execution_time (sec)": m["time"],
                    "success": m["success"],
                    "error": m["error"],
                }
                for m in modules_execution_results
            ]
        )
        .set_index("module")
        .sort_values(by="execution_time (sec)", ascending=False)
    )
    return df_exec


def _retry_modules_failed(modules_execution_results, country_data_getter):
    modules_failed = [m["module_name"] for m in modules_execution_results if m["success"] is False]
    logger.info(f"\n---\n\nRETRIES ({len(modules_failed)})")
    modules_execution_results = []
    for module_name in modules_failed:
        modules_execution_results.append(country_data_getter.run(module_name))
    modules_failed_retrial = [m["module_name"] for m in modules_execution_results if m["success"] is False]
    if len(modules_failed_retrial) > 0:
        failed_str = "\n".join([f"* {m}" for m in modules_failed_retrial])
        print(f"\n---\n\nFAILED\nThe following scripts failed to run ({len(modules_failed_retrial)}):\n{failed_str}")


def _print_timing(t0, t_sec_1, df_time):
    t_min_1 = round(t_sec_1 / 60, 2)
    t_sec_2 = round(time.time() - t0, 2)
    t_min_2 = round(t_sec_2 / 60, 2)
    print("---")
    print("TIMING DETAILS")
    print(f"Took {t_sec_1} seconds (i.e. {t_min_1} minutes).")
    print(f"Top 20 most time consuming scripts:")
    print(df_time[["execution_time (sec)"]].head(20))
    print(f"\nTook {t_sec_2} seconds (i.e. {t_min_2} minutes) [AFTER RETRIALS].")
    print("---")
    return t_sec_1, t_min_1, t_sec_2, t_min_2


def _load_modules_order(modules_name, path_log):
    if len(modules_name) < 10:
        return modules_name
    df = obj_from_s3(path_log)
    # Filter by machine
    # details = system_details()
    # machine = details["id"]
    # if machine in df.machine:
    #     df = df[df.machine == machine]
    # df = pd.read_csv(os.path.join(PATHS.INTERNAL_OUTPUT_VAX_LOG_DIR, "get-data.csv"))
    module_order_all = (
        df.sort_values("date")
        .drop_duplicates(subset=["module"], keep="last")
        .sort_values("execution_time (sec)", ascending=False)
        .module.tolist()
    )
    modules_name_order = [m for m in module_order_all if m in modules_name]
    missing = [m for m in modules_name if m not in modules_name_order]
    return modules_name_order + missing


# def _export_log_info(df_exec, t_sec_1, t_sec_2):
#     # print(len(df_new), len(MODULES_NAME), len(df_new) == len(MODULES_NAME))
#     if len(df_exec) == len(MODULES_NAME):
#         print("EXPORTING LOG DETAILS")
#         details = system_details()
#         date_now = localdate(force_today=True)
#         machine = details["id"]
#         # Export timings per country
#         df_exec = df_exec.reset_index().assign(date=date_now, machine=machine)
#         df = obj_from_s3(LOG_GET_COUNTRIES)
#         df = df[df.date + df.machine != date_now + machine]
#         df = pd.concat([df, df_exec])
#         obj_to_s3(df, LOG_GET_COUNTRIES)
#         # Export machine info
#         data = obj_from_s3(LOG_MACHINES)
#         if machine not in data:
#             data = {**details, machine: details["info"]}
#             obj_to_s3(data, LOG_MACHINES)
#         # Export overall timing
#         report = {"machine": machine, "date": date_now, "t_sec": t_sec_1, "t_sec_retry": t_sec_2}
#         df_new = pd.DataFrame([report])
#         df = obj_from_s3(LOG_GET_GLOBAL)
#         df = df[df.date + df.machine != date_now + machine]
#         df = pd.concat([df, df_new])
#         obj_to_s3(df, LOG_GET_GLOBAL)
