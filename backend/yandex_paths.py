"""Raw Yandex.Disk paths: preserve namespace roots and reject ambiguous escapes."""
import re


def _component(value: str) -> None:
    if (not isinstance(value, str) or not value or value in (".", "..")
            or any(c in value for c in "/\\:")
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or re.search(r"%(?:2e|2f|5c|3a|25|0[0-9a-f]|1[0-9a-f]|7f)", value, re.I)):
        raise RuntimeError("Invalid Yandex path component")


def split_path(path: str) -> tuple[str, tuple[str, ...]]:
    if not isinstance(path, str) or not path:
        raise RuntimeError("Yandex path is empty")
    prefix = ""
    for namespace in ("app:/", "disk:/"):
        if path.startswith(namespace):
            prefix, path = namespace, path[len(namespace):]
            break
    if not prefix and path == "/":
        return "disk:/", ()
    # A single leading/trailing slash on legacy Disk paths is equivalent to root.
    # Interior empty components and repeated separators remain invalid.
    parts = tuple(path.strip("/").split("/")) if path.strip("/") else ()
    if not parts and not prefix:
        raise RuntimeError("Yandex path is empty")
    if "//" in path or (prefix and path.startswith("/")):
        raise RuntimeError("Invalid Yandex path separators")
    for part in parts:
        _component(part)
    return prefix, parts


def normalize_path(path: str) -> str:
    prefix, parts = split_path(path)
    return prefix + "/".join(parts)


def join_path(path: str, *components: str) -> str:
    prefix, parts = split_path(path)
    for component in components:
        _component(component)
    return prefix + "/".join((*parts, *components))


def is_within(path: str, root: str) -> bool:
    namespace, parts = split_path(path)
    root_namespace, root_parts = split_path(root)
    return ((namespace or "disk:/") == (root_namespace or "disk:/")
            and parts[:len(root_parts)] == root_parts)


def is_production_disk_path(path: str) -> bool:
    namespace, parts = split_path(path)
    return namespace != "app:/" and bool(parts) and parts[0] == "PhotoDoc_Orders"
