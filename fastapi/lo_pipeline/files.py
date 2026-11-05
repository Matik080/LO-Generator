"""JSON file helpers for the standalone (__main__) scripts. The pipeline itself does not write files."""
import json
from typing import Any, Dict, List


def load_json(path: str) -> List[Dict[str, Any]]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                print(f"[Import] Successfully loaded {path}")
                return json.load(f)
        except Exception as e:
            raise Exception(f"[Import] Failed to load from {path}: {e}")


def export_json(units: List[Dict[str, Any]], output_path: str) -> None:
    try:
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(units, f, indent=2, ensure_ascii=False)
        print(f"[Export] Successfully saved {len(units)} units to {output_path}")
    except Exception as e:
        print(f"[Export] Failed to save to {output_path}: {e}")
