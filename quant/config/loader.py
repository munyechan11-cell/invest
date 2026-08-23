"""Config loading: YAML/JSON + environment interpolation + presets."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from quant.config.schema import StrategyConfig

_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")


def _interpolate(value: Any) -> Any:
    """Expand ${VAR} and ${VAR:-default} anywhere in the tree.

    Keeps secrets out of the config file — the file names the variable, the
    environment supplies the value.
    """
    if isinstance(value, str):
        def repl(m: re.Match) -> str:
            return os.environ.get(m.group(1), m.group(2) or "")
        return _ENV_PATTERN.sub(repl, value)
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    return value


def load_config(path: str | Path) -> StrategyConfig:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"config not found: {path}")
    text = path.read_text(encoding="utf-8")
    if path.suffix in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise RuntimeError("PyYAML is required for YAML configs") from exc
        raw = yaml.safe_load(text) or {}
    else:
        raw = json.loads(text)
    return StrategyConfig.model_validate(_interpolate(raw))


def dump_config(config: StrategyConfig, path: str | Path) -> None:
    path = Path(path)
    data = json.loads(config.model_dump_json(exclude_none=True))
    if path.suffix in (".yaml", ".yml"):
        import yaml
        path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
                        encoding="utf-8")
    else:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
