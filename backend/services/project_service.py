
from pathlib import Path
import shutil

# All user-managed files stay inside this dedicated workspace.
WORKSPACE = Path(__file__).resolve().parent.parent / "workspace"
SUPPORTED_SUFFIXES = (".v", ".sv", ".txt")


def initialize_workspace():
    WORKSPACE.mkdir(parents=True, exist_ok=True)


def _path_parts(relative_path: str) -> tuple:
    relative_path = relative_path.replace("\\", "/").strip()

    if relative_path.startswith("/") or "\x00" in relative_path:
        raise ValueError("Invalid file path.")

    parts = Path(relative_path).parts

    if any(part in ("..", ".") for part in parts):
        raise ValueError("Path traversal is not allowed.")

    return parts


def safe_path(relative_path: str = "") -> Path:
    initialize_workspace()

    parts = _path_parts(relative_path)
    target = WORKSPACE.joinpath(*parts).resolve()

    if target != WORKSPACE.resolve() and WORKSPACE.resolve() not in target.parents:
        raise ValueError("Access outside the workspace is not allowed.")

    return target


def list_directory(relative_path: str = ""):
    directory = safe_path(relative_path)

    if not directory.exists():
        raise FileNotFoundError("Folder not found.")

    if not directory.is_dir():
        raise ValueError("The requested path is not a folder.")

    items = []

    for item in sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        if item.is_symlink():
            continue

        items.append({
            "name": item.name,
            "path": item.relative_to(WORKSPACE).as_posix(),
            "type": "folder" if item.is_dir() else "file",
        })

    return {
        "path": relative_path,
        "items": items,
    }


def create_item(relative_path: str, item_type: str):
    target = safe_path(relative_path)

    if not relative_path or target == WORKSPACE.resolve():
        raise ValueError("A valid name is required.")

    # Validate everything before touching the disk, so a rejected request
    # never leaves empty parent folders behind.
    if item_type not in ("file", "folder"):
        raise ValueError("Item type must be file or folder.")

    if item_type == "file" and target.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise ValueError("Only .v, .sv and .txt files are supported.")

    if target.exists():
        raise FileExistsError("An item with this name already exists.")

    target.parent.mkdir(parents=True, exist_ok=True)

    if item_type == "folder":
        target.mkdir()
    else:
        target.touch()

    return {"success": True, "path": relative_path}


def read_file(relative_path: str):
    target = safe_path(relative_path)

    if not target.exists() or not target.is_file():
        raise FileNotFoundError("File not found.")

    if target.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise ValueError("Unsupported file type.")

    if target.stat().st_size > 500_000:
        raise ValueError("File is too large to open.")

    return {
        "path": relative_path,
        "content": target.read_text(encoding="utf-8"),
    }


def save_file(relative_path: str, content: str):
    target = safe_path(relative_path)

    if not target.exists() or not target.is_file():
        raise FileNotFoundError("File not found.")

    if target.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise ValueError("Unsupported file type.")

    if len(content) > 500_000:
        raise ValueError("File is too large.")

    target.write_text(content, encoding="utf-8")

    return {"success": True, "path": relative_path}


def rename_item(old_path: str, new_path: str):
    source = safe_path(old_path)
    destination = safe_path(new_path)

    if not source.exists() or source == WORKSPACE.resolve():
        raise FileNotFoundError("Source item not found.")

    if destination.exists() or destination == WORKSPACE.resolve():
        raise FileExistsError("Destination already exists or is invalid.")

    if source.is_dir() and destination.is_relative_to(source):
        raise ValueError("A folder cannot be moved inside itself.")

    if source.is_file() and destination.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise ValueError("Only .v, .sv and .txt files are supported.")

    destination.parent.mkdir(parents=True, exist_ok=True)
    source.rename(destination)

    return {"success": True, "path": new_path}


def delete_item(relative_path: str):
    # Check the unresolved path: safe_path() resolves symlinks, so checking
    # its result would delete the link's target instead of refusing.
    unresolved = WORKSPACE.joinpath(*_path_parts(relative_path))

    if unresolved.is_symlink():
        raise ValueError("Symbolic links cannot be deleted.")

    target = safe_path(relative_path)

    if not target.exists() or target == WORKSPACE.resolve():
        raise FileNotFoundError("Item not found.")

    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()

    return {"success": True, "path": relative_path}
