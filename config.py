
MODES = ["Api","Local"]
MODEL_NAME = 'qwen3.5:4b'
MAX_TOKEN = 2048
DEEPSEEK_MODEL =  "deepseek-v4-flash"
MODE = MODES[0]


replanner_taks = """
        Fix the previously generated browser automation plan by correcting any invalid, missing, ambiguous, or incorrectly specified target elements.
        Use the provided validation information and current DOM observations to identify the correct semantic target for each failed action.
        Preserve actions that have already been successfully executed, and generate only the minimum corrected actions required to continue the task from the current browser state.
        Do not invent elements or selectors. Use only elements supported by the provided DOM observations.
"""