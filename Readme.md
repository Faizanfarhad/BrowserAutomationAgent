# Browser Automation Agent(WIP)

An LLM-driven browser automation prototype. It accepts a natural-language task, asks a language model to produce a structured action plan, checks that plan against a browser page, and executes it with Playwright when validation succeeds. Invalid plans are sent back through a bounded validation-and-replanning loop.

The current implementation is browser-first. Direct service API execution, automatic clarification, and resuming a prior run from its saved state are not implemented.

## Architecture and Flow

```mermaid
flowchart TD
        U[User task] --> CLI[agent.py command-line entry point]
        CLI --> CFG[config.py: mode, model, retry limit]
        CLI --> P[Planner.generate_plan]
        CFG --> P

        P -->|Local mode| O[Ollama model]
        P -->|Api mode in CLI| D[DeepSeek API]
        O --> PLAN[Structured plan: steps]
        D --> PLAN
        PLAN --> SAVEPLAN[Planner saves planN.json]
        SAVEPLAN --> E[Executor.do_task]

        E --> ATT[Create AttemptN state]
        ATT --> SAVE[Atomic JSON state snapshot]
        ATT --> V[Validate plan in headless Chromium]
        V --> FIRST[Load first valid navigate URL]
        FIRST --> CHECK[Validator checks every planned step]
        CHECK --> VALID{Plan valid?}

        VALID -->|Yes| STORAGE[Capture validation context storage state]
        STORAGE --> BROWSER[Open headed execution browser]
        BROWSER --> STEP[Execute steps in order]
        STEP --> STEPSTATE[Update step status, result, or error]
        STEPSTATE --> SAVE
        STEPSTATE --> MORE{More steps?}
        MORE -->|Yes| STEP
        MORE -->|No| SUCCESS[Mark attempt succeeded]
        SUCCESS --> SAVE

        VALID -->|No| OBS[Collect failed validation steps and DOM observations]
        OBS --> LIMIT{Replan limit reached?}
        LIMIT -->|No| RP[Planner generates corrected plan]
        RP --> SAVEPLAN
        LIMIT -->|Yes| FAIL[Mark attempt failed and raise error]
        FAIL --> SAVE

        STEPSTATE -->|Execution exception| EXECFAIL[Mark step and attempt failed]
        EXECFAIL --> SAVE
```

### Run lifecycle

1. `agent.py` validates the task argument and selects the configured model mode.
2. `Planner` creates a JSON-compatible plan and saves it in `saved_plan/`.
3. `Executor` creates an `Attempt1` state record and starts validation.
4. The validator checks each step against the page loaded at the plan's first valid `navigate` URL. It does not execute earlier actions to simulate later page states.
5. If validation fails, the executor records the validation results, gathers observations for failed target roles, and asks the planner for a corrected plan. This repeats up to `MAX_REPLAN_ATTEMPTS` times.
6. If validation succeeds, the executor transfers the validation context's storage state to a new, headed browser context and executes the plan.
7. The state snapshot is rewritten after lifecycle transitions and each execution-step transition. Exceptions are recorded before they are raised to the caller.

## Project Structure

```text
.
|-- agent.py                    # CLI and top-level planning/execution orchestration
|-- config.py                   # Model mode, names, paths, and replan limit
|-- Planner/
|   `-- planner.py              # Prompt construction, model calls, and plan saving
|-- Executor/
|   `-- action.py               # Validation/replanning loop and Playwright execution
|-- Validator/
|   `-- validate.py             # URL, action, target, and wait validation
|-- tools/
|   |-- dom_observation.py      # DOM observations for failed target roles
|   `-- load_json.py            # JSON file/string loading helpers
|-- state/
|   |-- agent_state.py          # Atomic JSON state writer
|   `-- agent_state.json        # Latest run snapshot (created at runtime, ignored by Git)
|-- saved_plan/                 # Generated plans and replans
|-- traces/                     # Browser screenshots and other run artifacts
|-- testing/                    # Manual Playwright exploration script
|-- free_memory/                # Standalone process/VRAM cleanup helpers
|-- requirements.txt            # Core Python dependencies
`-- .env.example                # Names of optional model API credentials
```

## Components

### `agent.py`: application entry point

`run(task)` rejects an empty task, constructs a planner, generates the initial plan, then creates a Playwright session and passes the plan and original task to the executor. Run the project through this file for the complete workflow.

The CLI accepts the task as positional text, so quote it when it contains shell-special characters. The current `Api` branch selects DeepSeek. The `Local` branch uses the configured Ollama model.

### `Planner/planner.py`: plan generation

`Planner.generate_plan()` assembles the initial-task or replan prompt, calls the configured model, and parses/saves the model's structured JSON plan. Plans use a top-level `steps` array. Supported action names are `navigate`, `click`, `fill`, `press`, `select`, `wait`, and `extract`.

Initial plans are saved as `saved_plan/planN.json`; replans are saved with a `replanN.json` name in `saved_plan/`. The planner supports an Ollama streaming path and HTTP API request paths. In the current CLI configuration, API mode chooses DeepSeek; other provider code is not exposed as a CLI option.

### `Executor/action.py`: orchestration and browser actions

`Executor.do_task()` creates numbered attempt records, validates the plan, requests replans when validation fails, and executes a plan only after it passes validation. `validate_plan()` launches headless Chromium. A valid plan is then executed in a separate headed Chromium context using the validation context's storage state.

Execution dispatches each plan step to the corresponding Playwright operation. `get_target_locator()` resolves the semantic target through the validator. Extracted text/value is kept as the step result. Screenshots are written under `traces/` (`opened.png` and `action.png`).

### `Validator/validate.py`: pre-execution guardrail

`Validater` checks the plan structure and supported action type. Navigation requires an HTTP or HTTPS URL with a host. Target actions require a semantic target with a non-empty role and name. The resolved candidate is checked for visibility and the action-specific property, such as enabled, editable, keyboard-capable, selectable, or readable.

Per-step validation records include the step number, action, role, rule description, and boolean validity. Structural errors are reported at plan level; target-check exceptions are collected in the validation result. Validation is a preflight check, not a replay or simulation of the action sequence.

### `tools/dom_observation.py`: failed-target context

When validation fails, `find_relevant_element()` opens a separate headless browser at the current URL, inspects elements matching the failed role, and returns properties such as tag, name, placeholder, visibility, enabled state, and editability. This data is supplied to the planner for replanning; it does not execute a plan action.

### `state/agent_state.py`: durable run snapshot

`save_agent_state()` serializes the in-memory state as formatted JSON. It writes a temporary file in the destination directory and uses `os.replace()` to replace the snapshot atomically. The default output is `state/agent_state.json`.

The executor keeps one top-level `AttemptN` entry per validation/replan attempt. An attempt contains the task, plan, validation report, execution-step records, status, and any error or replan details. The file represents the latest run; starting another run replaces the previous snapshot. It is not currently a resume/recovery mechanism.

Example shape (values abbreviated):

```json
{
    "task": "Search Wikipedia for Python tutorials",
    "Attempt1": {
        "attempt": 1,
        "replan_attempt": 0,
        "status": "replanned",
        "plan": {"steps": []},
        "validation": {"valid": false, "steps": []},
        "execution": {"current_step": null, "steps": []},
        "replan": {"status": "succeeded", "next_attempt": 2, "plan": {"steps": []}}
    },
    "Attempt2": {
        "attempt": 2,
        "replan_attempt": 1,
        "status": "succeeded",
        "validation": {"valid": true, "steps": []},
        "execution": {"current_step": null, "steps": []},
        "error": null
    }
}
```

Attempt keys are one-based (`Attempt1`, `Attempt2`); `replan_attempt` is zero-based. Validation failures live under `validation`; runtime action failures live on the corresponding item in `execution.steps` and on the attempt's error fields.

### Supporting modules

- `config.py` defines `MODE`, model names, `MAX_REPLAN_ATTEMPTS`, plan paths, and the replan instruction. The default mode is `Api`.
- `tools/load_json.py` provides helpers to load JSON from a file or string. The executor's standalone entry point uses it to load `PLAN_PATH`.
- `testing/testing.py` is a manual Playwright locator exploration script, not an automated regression test suite.
- `free_memory/free_Mmemory.py` provides a process RSS / glibc memory-trim helper. `free_memory/free_vram.py` contains a PyTorch CUDA cache helper. Neither is called by the main agent flow.
- `saved_plan/` contains sample plans and generated plans. `traces/` receives screenshots and is intended for run artifacts.

## Plan Format

Each plan has a `steps` list. For example:

```json
{
    "steps": [
        {"action": "navigate", "url": "https://www.wikipedia.org"},
        {
            "action": "fill",
            "target": {"role": "textbox", "name": "Search Wikipedia"},
            "value": "Python tutorials"
        },
        {
            "action": "press",
            "target": {"role": "textbox", "name": "Search Wikipedia"},
            "key": "Enter"
        }
    ]
}
```

Target actions use Playwright semantic roles and accessible names, not planner-invented CSS selectors. Action-specific fields are:

| Action | Required fields |
| --- | --- |
| `navigate` | `url` |
| `click` | `target` |
| `fill` | `target`, `value` |
| `press` | `target`, `key` |
| `select` | `target`, `value` |
| `extract` | `target` |
| `wait` | `duration` for the currently implemented executor |

## Setup

Requirements: Python 3.10 or newer, Chromium for Playwright, and either an API credential or a running Ollama installation, depending on the selected mode.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
playwright install chromium
```

For API mode, copy `.env.example` to `.env` and set `DEEPSEEK_API` to your API key. Keep `.env` private and do not commit credentials.

For local mode, install and start Ollama, pull the configured model, and change `MODE` in `config.py` to `Local`:

```bash
ollama pull qwen3.5:4b
```

`MODE` is configured in `config.py`; it is not currently a command-line flag. The project loads `.env` in the planner module.

## Running

Run a natural-language task through planning, validation, execution, and optional replanning:

```bash
python agent.py "Search Wikipedia for Python tutorials"
```

To execute a previously saved plan directly, set `PLAN_PATH` in `config.py` and run:

```bash
python Executor/action.py
```

Generated plans/replans are saved to `saved_plan/`. The latest task state is written to `state/agent_state.json`. Screenshots are written to `traces/`.

## Configuration

| Setting | Purpose |
| --- | --- |
| `MODE` | `Api` or `Local`; defaults to `Api` |
| `MODEL_NAME` | Ollama model used in local mode |
| `DEEPSEEK_MODEL` | Model name used by the CLI API path |
| `MAX_TOKEN` | API generation token limit |
| `MAX_REPLAN_ATTEMPTS` | Maximum number of replans after the initial plan |
| `PLAN_PATH` | Saved plan loaded by the executor's standalone entry point |

## Current Limitations

- The validator loads the first valid navigation URL and checks planned targets against that page. It does not simulate clicks, form submissions, or page transitions before checking later steps.
- The executor only supports duration-based waits. Although the validator accepts a wait condition, condition-based execution raises `NotImplementedError`.
- Replanning occurs after pre-execution validation failures. An execution-time failure is saved in state and raised; it does not currently trigger automatic replanning.
- `collect_replan_data()` currently reports no successfully executed steps because replanning is initiated before plan execution.
- Direct API actions, API-first routing, human clarification, and automatic state-based resume are not implemented.
- The state file is a single latest-run snapshot and may contain task text, plan inputs, or extracted content. Protect it if those values are sensitive.
- `free_memory/` helpers are optional utilities and require packages/platform features that are not listed as core dependencies in `requirements.txt`.

## Troubleshooting

- **Chromium executable missing:** run `playwright install chromium` in the active Python environment.
- **API request rejected:** check that `.env` contains a valid `DEEPSEEK_API` key and that `MODE` is `Api`.
- **Local model unavailable:** start Ollama and confirm the model in `MODEL_NAME` has been pulled.
- **Plan target fails validation:** confirm the page URL and accessible role/name in the plan match the page that validation opens.
- **Attempt ended in `failed`:** inspect `state/agent_state.json` for the failed stage, validation report, and execution-step error.
