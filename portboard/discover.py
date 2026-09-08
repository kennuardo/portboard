"""Turn a repository directory into a suggested Portboard project.

Everything here is read-only text inspection: regexes over config files, json
for package.json. No JavaScript is evaluated, no command from the repo is run,
and .env files are only ever read for their PORT line.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Iterable

log = logging.getLogger("portboard.discover")

COMPOSE_FILES = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
NUXT_CONFIGS = ("nuxt.config.ts", "nuxt.config.js", "nuxt.config.mjs", "nuxt.config.cjs")
VITE_CONFIGS = ("vite.config.ts", "vite.config.js", "vite.config.mjs", "vite.config.mts",
                "vite.config.cjs")
DOC_FILES = ("CLAUDE.md", "README.md", "README.rst", "readme.md")
SHELL_FILES = ("run.sh", "start.sh", "dev.sh", "Makefile")
#: compose services whose published port is the one a human would open
PREFERRED_SERVICES = ("web", "frontend", "app", "api")
MAKE_TARGETS = ("run", "dev", "up")
#: never descend into these while scanning a root
SKIP_DIRS = {
    "node_modules", "__pycache__", "venv", "dist", "build", "target", "vendor",
    "coverage", "tmp", "public", "static",
}
MAX_READ = 200_000

_CONF_ORDER = {"low": 0, "medium": 1, "high": 2}


def _worse(a: str, b: str | None) -> str:
    """The lower of two confidence levels."""
    if b is None:
        return a
    return a if _CONF_ORDER.get(a, 0) <= _CONF_ORDER.get(b, 0) else b


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read(MAX_READ)
    except OSError:
        return ""


def _first_existing(root: str, names: Iterable[str]) -> str | None:
    for name in names:
        candidate = os.path.join(root, name)
        if os.path.isfile(candidate):
            return candidate
    return None


def _load_package_json(root: str) -> dict:
    path = os.path.join(root, "package.json")
    if not os.path.isfile(path):
        return {}
    try:
        data = json.loads(_read(path) or "{}")
    except (ValueError, TypeError):
        log.debug("unparsable package.json in %s", root)
        return {}
    return data if isinstance(data, dict) else {}


def package_manager(root: str) -> str:
    if os.path.isfile(os.path.join(root, "pnpm-lock.yaml")):
        return "pnpm"
    if os.path.isfile(os.path.join(root, "yarn.lock")):
        return "yarn"
    if os.path.isfile(os.path.join(root, "bun.lockb")) or os.path.isfile(os.path.join(root, "bun.lock")):
        return "bun"
    return "npm"


# --------------------------------------------------------------------------
# compose parsing
# --------------------------------------------------------------------------


def _resolve_number(text: str) -> int | None:
    """'8080' / '${GALLERY_PORT:-3030}' / '8000-8005' -> int, '${X}' -> None."""
    text = text.strip().strip("\"'")
    match = re.fullmatch(r"\$\{[A-Za-z_][A-Za-z0-9_]*:-\s*([0-9]+)\s*\}", text)
    if match:
        return int(match.group(1))
    match = re.match(r"^([0-9]{1,5})", text)
    return int(match.group(1)) if match else None


def _published_port(entry: str) -> int | None:
    """Host port of a compose ports entry ('3000:3000', '127.0.0.1:8080:80', '3000')."""
    entry = entry.strip().strip("\"'").split("/")[0]
    holes: list[str] = []

    def _mask(match: re.Match[str]) -> str:
        holes.append(match.group(0))
        return f"\x00{len(holes) - 1}\x00"

    masked = re.sub(r"\$\{[^}]*\}", _mask, entry)
    parts = masked.split(":")
    if len(parts) < 2:
        return None  # only a container port: docker picks a random host port
    host = re.sub(r"\x00([0-9]+)\x00", lambda m: holes[int(m.group(1))], parts[-2])
    return _resolve_number(host)


def compose_ports(text: str) -> list[tuple[str, int]]:
    """[(service, host_port)] for every published port in a compose file."""
    found: list[tuple[str, int]] = []
    in_services = False
    service: str | None = None
    service_indent: int | None = None
    in_ports = False
    ports_indent = 0
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0:
            in_services = stripped.startswith("services:")
            service, in_ports = None, False
            continue
        if not in_services:
            continue
        if in_ports:
            if stripped.startswith("-"):
                port = _published_port(stripped[1:])
                if port and service:
                    found.append((service, port))
                continue
            if indent <= ports_indent:
                in_ports = False
        key = re.match(r"^([A-Za-z0-9_.-]+):\s*(.*)$", stripped)
        if key and (service_indent is None or indent <= service_indent):
            service_indent = indent
            service = key.group(1)
            in_ports = False
            continue
        if key and key.group(1) == "ports":
            inline = key.group(2).strip()
            if inline.startswith("["):
                for item in inline.strip("[]").split(","):
                    port = _published_port(item)
                    if port and service:
                        found.append((service, port))
            else:
                in_ports = True
                ports_indent = indent
    return found


def _compose_choice(root: str) -> tuple[int | None, str | None, str | None]:
    """(port, service, filename) per the DESIGN rule for compose projects."""
    path = _first_existing(root, COMPOSE_FILES)
    if path is None:
        return None, None, None
    published = compose_ports(_read(path))
    if not published:
        return None, None, os.path.basename(path)
    for wanted in PREFERRED_SERVICES:
        for service, port in published:
            if service == wanted:
                return port, service, os.path.basename(path)
    service, port = min(published, key=lambda item: item[1])
    return port, service, os.path.basename(path)


# --------------------------------------------------------------------------
# port evidence
# --------------------------------------------------------------------------


def _candidates(
    root: str, include_compose: bool = True, include_docs: bool = True
) -> list[tuple[int, str, str]]:
    """(port, confidence, evidence) in priority order; first entry wins."""
    out: list[tuple[int, str, str]] = []

    for name in NUXT_CONFIGS:
        path = os.path.join(root, name)
        if os.path.isfile(path):
            match = re.search(r"devServer\s*:\s*\{.*?port\s*:\s*([0-9]{2,5})", _read(path), re.S)
            if match:
                out.append((int(match.group(1)), "high", f"{name} devServer.port={match.group(1)}"))
            break

    for name in VITE_CONFIGS:
        path = os.path.join(root, name)
        if os.path.isfile(path):
            match = re.search(r"server\s*:\s*\{.*?port\s*:\s*([0-9]{2,5})", _read(path), re.S)
            if match:
                out.append((int(match.group(1)), "high", f"{name} server.port={match.group(1)}"))
            break

    dev = (_load_package_json(root).get("scripts") or {}).get("dev")
    if isinstance(dev, str):
        match = re.search(r"(?:--port|-p)[= ]\s*([0-9]{2,5})\b", dev)
        if match:
            out.append((int(match.group(1)), "high", f"dev script port {match.group(1)}"))

    if include_compose:
        port, service, filename = _compose_choice(root)
        if port:
            out.append((port, "high", f"{filename} service {service} publishes {port}"))

    env_path = os.path.join(root, ".env")
    if os.path.isfile(env_path):
        for line in _read(env_path).splitlines():
            if line.startswith("PORT="):  # never read anything else out of .env
                value = _resolve_number(line[5:].split("#")[0])
                if value:
                    out.append((value, "medium", f".env PORT={value}"))
                break

    for name in SHELL_FILES:
        path = os.path.join(root, name)
        if not os.path.isfile(path):
            continue
        text = _read(path)
        match = re.search(
            r"\bPORT\s*=\s*[\"']?(?:\$\{[A-Za-z_][A-Za-z0-9_]*:-\s*)?([0-9]{2,5})", text
        )
        if match:
            out.append((int(match.group(1)), "medium", f"{name} PORT={match.group(1)}"))
            continue
        match = re.search(r"--port[= ]\s*[\"']?([0-9]{2,5})\b", text)
        if match:
            out.append((int(match.group(1)), "medium", f"{name} --port {match.group(1)}"))

    for name in (DOC_FILES + SHELL_FILES) if include_docs else ():
        path = os.path.join(root, name)
        if not os.path.isfile(path):
            continue
        text = _read(path)
        match = re.search(r"uvicorn[^\n]*?--port[= ]\s*[\"']?([0-9]{2,5})\b", text)
        if match:
            out.append((int(match.group(1)), "medium", f"{name} uvicorn --port {match.group(1)}"))
            continue
        match = re.search(r"(?:localhost|127\.0\.0\.1):([0-9]{2,5})\b", text)
        if match:
            out.append((int(match.group(1)), "low", f"{name} mentions {match.group(0)}"))

    return out


def detect_port_from_repo(path: str) -> int | None:
    """Best port evidence in the repository, or None when there is none."""
    root = os.path.realpath(os.path.expanduser(str(path)))
    if not os.path.isdir(root):
        return None
    candidates = _candidates(root)
    return candidates[0][0] if candidates else None


# --------------------------------------------------------------------------
# suggestion
# --------------------------------------------------------------------------


def _name_for(root: str) -> str:
    return re.sub(r"[^a-z0-9-]", "-", os.path.basename(root).lower()) or "project"


def _python_module(root: str) -> str | None:
    """Dotted module of the first file that defines a FastAPI/Flask app."""
    for rel in ("app/main.py", "main.py", "backend/main.py", "src/main.py", "app.py",
                "backend/app.py", "server.py"):
        path = os.path.join(root, rel)
        if not os.path.isfile(path):
            continue
        text = _read(path)
        if re.search(r"^\s*app\s*=\s*(FastAPI|Flask)\(", text, re.M):
            return rel[:-3].replace("/", ".")
    return None


def _venv_python(root: str) -> str:
    for rel in (".venv/bin/python", "venv/bin/python", ".venv/bin/python3"):
        if os.path.isfile(os.path.join(root, rel)):
            return f"./{rel}"
    return "python3"


def _base(root: str) -> dict[str, Any]:
    return {
        "path": root,
        "name": _name_for(root),
        "kind": "transient",
        "start_cmd": None,
        "port_mode": "env",
        "base_port": None,
        "health_path": "/",
        "open_path": "/",
        "confidence": "low",
        "evidence": [],
    }


MANIFEST_FILES = (
    "package.json", "pyproject.toml", "requirements.txt", "setup.py", "setup.cfg",
    "go.mod", "Cargo.toml", "composer.json", "Gemfile", "mix.exs", "Procfile",
)


def _has_manifest(root: str) -> bool:
    return any(os.path.isfile(os.path.join(root, name)) for name in MANIFEST_FILES)


def suggest(path: str) -> dict | None:
    """Suggested project configuration for a repository directory, or None."""
    root = os.path.realpath(os.path.expanduser(str(path)))
    if not os.path.isdir(root):
        return None

    result = _base(root)
    evidence: list[str] = result["evidence"]
    pkg = _load_package_json(root)
    scripts = pkg.get("scripts") or {}
    dev_script = scripts.get("dev") if isinstance(scripts, dict) else None
    compose_file = _first_existing(root, COMPOSE_FILES)
    stack_conf: str | None = None
    port_choice: tuple[int, str, str] | None = None

    if compose_file is not None and not dev_script:
        port, service, filename = _compose_choice(root)
        result["kind"] = "compose"
        result["port_mode"] = "fixed"
        evidence.append(f"{filename} at root")
        if port:
            port_choice = (port, "high", f"{filename} service {service} publishes {port}")
            stack_conf = "high"
        else:
            evidence.append("no published host port in compose (host network?)")
            stack_conf = "medium"
            others = _candidates(root, include_compose=False)
            port_choice = others[0] if others else None

    elif dev_script:
        manager = package_manager(root)
        result["kind"] = "transient"
        result["port_mode"] = "env"
        result["start_cmd"] = f"{manager} run dev"
        evidence.append(f"package.json scripts.dev = {dev_script!r}")
        evidence.append(f"package manager {manager}")
        stack_conf = "high"
        # DESIGN: a node project without config/script/.env evidence gets no port
        # at all (the allocator decides) - documentation mentions are too weak.
        candidates = _candidates(root, include_compose=False, include_docs=False)
        port_choice = candidates[0] if candidates else None

    else:
        requirements = _first_existing(
            root, ("requirements.txt", "requirements-dev.txt", "pyproject.toml")
        )
        req_text = _read(requirements) if requirements else ""
        is_asgi = bool(re.search(r"uvicorn|fastapi", req_text, re.I))
        module = _python_module(root)
        run_sh = os.path.join(root, "run.sh")
        has_run_sh = os.path.isfile(run_sh)
        has_python = bool(module) or any(
            f.endswith(".py") for f in os.listdir(root) if os.path.isfile(os.path.join(root, f))
        )

        if is_asgi and module:
            result["kind"] = "transient"
            result["port_mode"] = "arg"
            result["start_cmd"] = f"{_venv_python(root)} -m uvicorn {module}:app --port {{port}}"
            evidence.append(f"{os.path.basename(requirements)} requires uvicorn/fastapi")
            evidence.append(f"{module.replace('.', '/')}.py defines app")
            stack_conf = "high"
        elif has_run_sh and (is_asgi or has_python):
            result["kind"] = "transient"
            result["port_mode"] = "env"
            result["start_cmd"] = "./run.sh"
            evidence.append("run.sh at root")
            stack_conf = "medium"
        else:
            makefile = os.path.join(root, "Makefile")
            target = None
            if os.path.isfile(makefile):
                text = _read(makefile)
                for wanted in MAKE_TARGETS:
                    if re.search(rf"^{wanted}\s*:", text, re.M):
                        target = wanted
                        break
            if target is None:
                if not _has_manifest(root):
                    return None
                # A repository with a manifest but no recognizable start command
                # is still a project: register it without a start command so it
                # gets a port and shows up in the GUI; the user fills the rest in.
                result["kind"] = "none"
                result["port_mode"] = "env"
                result["start_cmd"] = None
                evidence.append("no recognizable start command; set one with: portboard project edit <name> --start '...'")
                candidates = _candidates(root, include_compose=False)
                port_choice = candidates[0] if candidates else None
                if port_choice:
                    result["base_port"] = port_choice[0]
                    evidence.append(port_choice[2])
                result["confidence"] = "low"
                if not has_git(root):
                    evidence.append("no .git (not a git repository)")
                return result
            result["kind"] = "transient"
            result["port_mode"] = "env"
            result["start_cmd"] = f"make {target}"
            evidence.append(f"Makefile target {target}")
            stack_conf = "low"

        candidates = _candidates(root, include_compose=False)
        port_choice = candidates[0] if candidates else None

    if port_choice:
        result["base_port"] = port_choice[0]
        evidence.append(port_choice[2])
        result["confidence"] = _worse(stack_conf or "low", port_choice[1])
    else:
        result["confidence"] = stack_conf or "low"
        evidence.append("no port evidence, portboard will allocate one")

    if not has_git(root):
        evidence.append("no .git (not a git repository)")
    return result


def has_git(path: str) -> bool:
    marker = os.path.join(path, ".git")
    return os.path.isdir(marker) or os.path.isfile(marker)


def looks_like_project(path: str) -> bool:
    """A git repository we know how to run."""
    root = os.path.realpath(os.path.expanduser(str(path)))
    if not has_git(root):
        return False
    return suggest(root) is not None


def scan(root: str, max_depth: int = 1) -> list[dict]:
    """suggest() for the directories below *root* (depth 1 = its children)."""
    base = os.path.realpath(os.path.expanduser(str(root)))
    results: list[dict] = []

    def walk(directory: str, depth: int) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError as exc:
            log.debug("cannot scan %s: %s", directory, exc)
            return
        for entry in entries:
            if not entry.is_dir(follow_symlinks=False):
                continue
            if entry.name.startswith(".") or entry.name in SKIP_DIRS:
                continue
            found = suggest(entry.path)
            if found is not None:
                results.append(found)
                continue
            if depth < max_depth:
                walk(entry.path, depth + 1)

    walk(base, 1)
    return results
