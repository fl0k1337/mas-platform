"""Версия приложения.

Показывается в подвале панели — чтобы одним взглядом понимать, какой код
сейчас работает (особенно на сервере после деплоя). Если версия в браузере
не поменялась после обновления — значит код не доехал или служба не перезапущена.

При каждом заметном изменении поднимайте VERSION.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

VERSION = "0.20"


def git_revision() -> str:
    """Короткий хеш коммита и дата — если проект развёрнут из git."""
    try:
        root = Path(__file__).resolve().parent.parent
        out = subprocess.run(
            ["git", "-C", str(root), "log", "-1", "--format=%h · %cd",
             "--date=format:%d.%m %H:%M"],
            capture_output=True, text=True, timeout=5)
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


BUILD = git_revision()
