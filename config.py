
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





TASK = None

replanner_taks = """
        Fix the previously generated browser automation plan by correcting any invalid, missing, ambiguous, or incorrectly specified target elements.
        Use the provided validation information and current DOM observations to identify the correct semantic target for each failed action.
        Preserve actions that have already been successfully executed, and generate only the minimum corrected actions required to continue the task from the current browser state.
        Do not invent elements or selectors. Use only elements supported by the provided DOM observations.
"""
