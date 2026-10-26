import json
import os
import tempfile
from pathlib import Path


STATE_FILE = Path(__file__).resolve().with_name("agent_state.json")


def save_agent_state(state: dict, path: str | Path = STATE_FILE) -> None:
	"""Atomically write the current task state as JSON."""
	destination = Path(path)
	destination.parent.mkdir(parents=True, exist_ok=True)
    
	temporary_path = None
	try:
		with tempfile.NamedTemporaryFile(
			mode="w",
			encoding="utf-8",
			dir=destination.parent,
			prefix=f".{destination.name}.",
			suffix=".tmp",
			delete=False,
		) as temporary_file:
			temporary_path = Path(temporary_file.name)
			json.dump(state, temporary_file, indent=2, ensure_ascii=False)
			temporary_file.write("\n")

		os.replace(temporary_path, destination)
	finally:
		if temporary_path is not None and temporary_path.exists():
			temporary_path.unlink()

