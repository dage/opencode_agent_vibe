"""One-time environment loader for the application (Dependency Free)."""
import os
from pathlib import Path

_INITIALISED = False

def load_environment(dotenv_path: str | Path | None = None) -> None:
    """Load environment variables from .env manually."""
    global _INITIALISED
    if _INITIALISED:
        return

    # Determine paths to check
    paths_to_check = []
    if dotenv_path:
        paths_to_check.append(Path(dotenv_path))
    
    # Check current directory and parent directories
    cwd = Path.cwd()
    paths_to_check.append(cwd / ".env")
    paths_to_check.append(cwd.parent / ".env")
    
    # Also check the project root relative to this file
    # This file is in .../controllers/env_loader.py
    project_root = Path(__file__).parent.parent
    if project_root != cwd:
        paths_to_check.append(project_root / ".env")

    for env_path in paths_to_check:
        if env_path.is_file():
            _parse_and_set_env(env_path)
            # Once we find one, we usually stop, or we can load multiple.
            # Loading multiple is safer for overrides.
            # But let's just stick to the first valid one found or all of them.
            # Simplest is just load all found.

    _INITIALISED = True

def _parse_and_set_env(path: Path) -> None:
    try:
        content = path.read_text(encoding="utf-8")
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            
            # Remove quotes if present
            if len(value) >= 2 and ((value.startswith('"') and value.endswith('"')) or \
                                    (value.startswith("'") and value.endswith("'"))):
                value = value[1:-1]
            
            if key and key not in os.environ:
                os.environ[key] = value
                
    except Exception:
        pass
