
MODES = ["Api","Local"]
MODEL_NAME = 'qwen3.5:4b'
MAX_TOKEN = 2048
DEEPSEEK_MODEL =  "deepseek-v4-flash"
MODE = MODES[1]
MARKET = "IN"

MAX_REPLAN_ATTEMPTS = 3
MAX_PLANNER_RETRIES = 3

REPLAN_IDX = 0
PLAN_IDX = 0
PLAN_PATH = f"saved_plan/plan{PLAN_IDX}.json"
REPLAN_PATH = f"saved_plan/replan/replan{REPLAN_IDX}.json"
SAVED_PLAN_PATH = "saved_plan"
SAVED_REPLAN_PATH = "saved_plan/replan"

#make false if you wanna see agent live working 
HEADLESS = False
PORT = 4555
PERSISTENT_CHROME = False
CHROME_PROFILE_DIR = ".browser_profile"
CHROME_EXECUTABLE = "google-chrome"
CHROME_CDP_ENDPOINT = f"http://127.0.0.1:{PORT}"
API_TIMEOUT_SECONDS = 30


TASK = None

replanner_taks = """
        Fix the previously generated browser automation plan by correcting any invalid, missing, ambiguous, or incorrectly specified target elements.
        Use the provided validation information and current DOM observations to identify the correct semantic target for each failed action.
        Preserve actions that have already been successfully executed, and generate only the minimum corrected actions required to continue the task from the current browser state.
        Do not invent elements or selectors. Use only elements supported by the provided DOM observations.
"""
