import re
import sys
import subprocess
import shutil
from . import utilities as ut


REPO_URL = "git+https://github.com/Carloj005/aw-cli.git@branch-name"

# Configurazione minimalista dei comandi per ogni tool
TOOLS = {
    "uv": {
        "list": ["uv", "tool", "list"],
        "install_git": ["uv", "tool", "install", REPO_URL, "--force"],
    },
    "pipx": {
        "list": ["pipx", "list"],
        "install_git": ["pipx", "install", REPO_URL, "--force"],
    },
    "pip": {
        "list": [sys.executable, "-m", "pip", "list"],
        "install_git": [sys.executable, "-m", "pip", "install", REPO_URL, "--force-reinstall"],
    },
}


def check_installation_method() -> str | None:
    """
    Controlla il metodo di installazione dello script.

    Returns:
        str | None: il nome del tool rilevato, o None se non trovato.
    """
    for tool_name, commands in TOOLS.items():
        exe = commands["list"][0]
        if shutil.which(exe) or exe == sys.executable:
            result = subprocess.run(commands["list"], capture_output=True, check=False)
            if result.returncode == 0 and "aw-cli" in result.stdout.decode("utf-8", errors="replace"):
                return tool_name
    return None


def update(branch: str) -> None:
    """
    Aggiorna lo script dal repository ufficiale del fork (default: branch 'main').

    Args:
        branch (str): il branch da cui aggiornare lo script. Se vuoto, usa 'main'.
    """
    target_branch = branch.strip() if branch and branch.strip() else "main"
    if target_branch.startswith("-") or not re.fullmatch(r"[A-Za-z0-9._/-]{1,100}", target_branch):
        ut.console.print("Nome branch non valido!", style="error")
        return

    tool = check_installation_method()
    if not tool:
        ut.console.print("Non è stato possibile trovare un metodo di installazione valido per aggiornare aw-cli", style="error")
        return

    command = [cmd.replace("branch-name", target_branch) for cmd in TOOLS[tool]["install_git"]]
    subprocess.run(command, check=False)
