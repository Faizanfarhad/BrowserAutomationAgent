"""CLI for evaluating the latest recorded agent run."""

import argparse
import json

from evaluation.evaluator import CASES, evaluate_state, format_report, load_state


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate a saved Browser Automation Agent run without rerunning it"
    )
    parser.add_argument(
        "--state",
        default="state/agent_state.json",
        help="Path to an agent state JSON file",
    )
    parser.add_argument(
        "--case",
        help="Optional test case number, for example 6",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Print the evaluation report as JSON",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available evaluation cases",
    )
    args = parser.parse_args()

    if args.list:
        for case_id, case in CASES.items():
            print(f"{case_id}: {case['name']}")
        return

    report = evaluate_state(load_state(args.state), args.case)
    print(
        json.dumps(report, indent=2, ensure_ascii=False)
        if args.as_json
        else format_report(report)
    )

    raise SystemExit(0 if report["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
