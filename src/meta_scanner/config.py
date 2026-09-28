"""Load the supported part of the versioned TOML configuration."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

REQUIRED_EXTENSIONS = frozenset({".pptx", ".docx", ".xlsx", ".pdf"})
SUPPORTED_OPTIONAL = frozenset({".txt", ".md"})
LEGACY_EXTENSIONS = frozenset({".ppt", ".doc", ".xls"})


class ConfigurationError(ValueError):
    """A setting is invalid or requires a component not implemented yet."""


@dataclass(frozen=True)
class Settings:
    source: Path
    roots: tuple[Path, ...]
    extensions: frozenset[str]
    exclude_globs: tuple[str, ...]
    stable_age_seconds: int
    database: Path
    cache_dir: Path
    export_dir: Path
    start: str
    end: str
    max_file_bytes: int
    max_uncompressed_bytes: int
    max_pages: int
    max_cells: int
    max_chars: int
    allow_cp932: bool
    include_notes: bool
    include_hidden_sheets: bool
    ocr_enabled: bool
    tesseract: Path
    tessdata_dir: Path
    ocr_languages: tuple[str, ...]
    vertical_language: str
    dpi: int
    max_page_pixels: int
    ocr_timeout: int
    llm_enabled: bool
    llm_executable: Path
    model_path: Path
    model_sha256: str
    llm_port: int
    llm_context: int
    llm_chunk_tokens: int
    llm_overlap_tokens: int
    llm_max_output_tokens: int
    llm_timeout: int
    llm_cpu_threads: int
    llm_temperature: float
    llm_top_p: float
    llm_top_k: int
    llm_min_p: float
    llm_presence_penalty: float
    repair_attempts: int
    prompt_version: str


def _positive(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigurationError(f"{name} must be a positive integer")
    return value


def _path(base: Path, value: object, name: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{name} must be a path")
    candidate = Path(os.path.expandvars(value)).expanduser()
    return (candidate if candidate.is_absolute() else base / candidate).resolve()


def _section(data: dict, name: str) -> dict:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ConfigurationError(f"[{name}] must be a table")
    return value


def _clock(value: object, name: str) -> str:
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        raise ConfigurationError(f"{name} must use HH:MM")
    try:
        hour, minute = (int(part) for part in value.split(":"))
    except ValueError as exc:
        raise ConfigurationError(f"{name} must use HH:MM") from exc
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ConfigurationError(f"{name} must use HH:MM")
    return value


def load_settings(config_file: Path) -> Settings:
    source = config_file.resolve()
    try:
        data = tomllib.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationError(f"Cannot read configuration: {exc}") from exc
    if data.get("config_version") != 1:
        raise ConfigurationError("config_version must be 1")

    scan = _section(data, "scan")
    storage = _section(data, "storage")
    runtime = _section(data, "runtime")
    extraction = _section(data, "extraction")
    ocr = _section(data, "ocr")
    llm = _section(data, "llm")
    metadata = _section(data, "metadata")
    legacy = _section(data, "legacy")
    roots_value = scan.get("roots")
    if not isinstance(roots_value, list) or not roots_value:
        raise ConfigurationError("scan.roots must be a nonempty list")
    roots = tuple(_path(source.parent, raw, "scan.roots") for raw in roots_value)
    for root in roots:
        if not root.is_dir():
            raise ConfigurationError(f"Input directory does not exist: {root}")
    for index, root in enumerate(roots):
        for other in roots[index + 1 :]:
            if root == other or root in other.parents or other in root.parents:
                raise ConfigurationError("Input roots must not overlap")

    required = scan.get("extensions", sorted(REQUIRED_EXTENSIONS))
    optional = scan.get("optional_extensions", [])
    if not isinstance(required, list) or not isinstance(optional, list):
        raise ConfigurationError("extensions must be lists")
    if any(not isinstance(item, str) for item in required + optional):
        raise ConfigurationError("extensions must be strings")
    main = frozenset(item.lower() for item in required)
    extra = frozenset(item.lower() for item in optional)
    if main != REQUIRED_EXTENSIONS:
        raise ConfigurationError("All four required extensions must remain enabled")
    if extra & LEGACY_EXTENSIONS or legacy.get("enabled", False):
        raise ConfigurationError("Legacy Office formats require LibreOffice and are not implemented")
    if extra - SUPPORTED_OPTIONAL:
        raise ConfigurationError(f"Unsupported optional extensions: {sorted(extra - SUPPORTED_OPTIONAL)}")
    if scan.get("hash_mode", "strict") != "strict" or scan.get("follow_links", False):
        raise ConfigurationError("Only strict hashing without symlink traversal is supported")
    if runtime.get("workers", 1) != 1:
        raise ConfigurationError("Only one worker is supported")
    if extraction.get("protection_policy", "skip_any_detected") != "skip_any_detected":
        raise ConfigurationError("Only skip_any_detected protection policy is supported")

    exclude = scan.get("exclude_globs", [])
    if not isinstance(exclude, list) or any(not isinstance(item, str) for item in exclude):
        raise ConfigurationError("scan.exclude_globs must be a list of patterns")
    languages = ocr.get("languages", ["jpn", "eng"])
    if not isinstance(languages, list) or not languages or any(not isinstance(lang, str) or not lang for lang in languages):
        raise ConfigurationError("ocr.languages must be a nonempty list")
    start = _clock(runtime.get("window_start", "22:00"), "runtime.window_start")
    end = _clock(runtime.get("window_end", "06:00"), "runtime.window_end")
    if start == end:
        raise ConfigurationError("The processing window cannot be zero length")
    if llm.get("backend", "llama_cpp") != "llama_cpp":
        raise ConfigurationError("Only the llama_cpp backend is supported")
    llm_enabled = llm.get("enabled", True)
    if not isinstance(llm_enabled, bool):
        raise ConfigurationError("llm.enabled must be true or false")
    endpoint_value = llm.get("endpoint", "http://127.0.0.1:8080")
    if not isinstance(endpoint_value, str):
        raise ConfigurationError("llm.endpoint must be a URL")
    endpoint = urlsplit(endpoint_value)
    if endpoint.scheme != "http" or endpoint.hostname != "127.0.0.1" or endpoint.path not in ("", "/") or endpoint.query or endpoint.fragment or endpoint.username:
        raise ConfigurationError("llm.endpoint must be http://127.0.0.1:<port>")
    try:
        llm_port = endpoint.port or 80
    except ValueError as exc:
        raise ConfigurationError("llm.endpoint has an invalid port") from exc
    if not 1 <= llm_port <= 65535:
        raise ConfigurationError("llm.endpoint port is out of range")
    llm_context = _positive(llm.get("context_tokens", 4096), "llm.context_tokens")
    llm_chunk_tokens = _positive(llm.get("chunk_tokens", 1800), "llm.chunk_tokens")
    llm_overlap_tokens = llm.get("overlap_tokens", 150)
    llm_max_output_tokens = _positive(llm.get("max_output_tokens", 768), "llm.max_output_tokens")
    if not isinstance(llm_overlap_tokens, int) or not 0 <= llm_overlap_tokens < llm_chunk_tokens:
        raise ConfigurationError("llm.overlap_tokens must be between 0 and chunk_tokens")
    if llm_chunk_tokens + llm_max_output_tokens + 512 > llm_context:
        raise ConfigurationError("LLM context is too small for the chunk and output budget")
    if llm.get("gpu_layers", 0) != 0 or llm.get("thinking", False):
        raise ConfigurationError("This CPU build requires gpu_layers=0 and thinking=false")
    model_sha256 = llm.get("model_sha256", "")
    if not isinstance(model_sha256, str):
        raise ConfigurationError("llm.model_sha256 must be text")
    if llm_enabled and not re.fullmatch(r"[a-fA-F0-9]{64}", model_sha256):
        raise ConfigurationError("llm.model_sha256 must be a 64-digit SHA-256")

    def sampling(name: str, default: float, minimum: float, maximum: float) -> float:
        value = llm.get(name, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not minimum <= value <= maximum:
            raise ConfigurationError(f"llm.{name} is out of range")
        return float(value)

    settings = Settings(
        source=source,
        roots=roots,
        extensions=main | extra,
        exclude_globs=tuple(exclude),
        stable_age_seconds=_positive(scan.get("stable_age_seconds", 60), "scan.stable_age_seconds"),
        database=_path(source.parent, storage.get("database", "var/catalog.sqlite3"), "storage.database"),
        cache_dir=_path(source.parent, storage.get("cache_dir", "var/cache"), "storage.cache_dir"),
        export_dir=_path(source.parent, storage.get("export_dir", "var/export"), "storage.export_dir"),
        start=start,
        end=end,
        max_file_bytes=_positive(extraction.get("max_file_mib", 512), "extraction.max_file_mib") * 1024**2,
        max_uncompressed_bytes=_positive(extraction.get("max_uncompressed_mib", 2048), "extraction.max_uncompressed_mib") * 1024**2,
        max_pages=_positive(extraction.get("max_pages_per_document", 5000), "extraction.max_pages_per_document"),
        max_cells=_positive(extraction.get("max_nonempty_cells", 2000000), "extraction.max_nonempty_cells"),
        max_chars=_positive(extraction.get("max_text_characters", 20000000), "extraction.max_text_characters"),
        allow_cp932=extraction.get("allow_cp932", False),
        include_notes=extraction.get("include_notes", True),
        include_hidden_sheets=extraction.get("include_hidden_sheets", True),
        ocr_enabled=ocr.get("enabled", True),
        tesseract=_path(source.parent, ocr.get("executable", "tesseract.exe"), "ocr.executable"),
        tessdata_dir=_path(source.parent, ocr.get("tessdata_dir", "tessdata"), "ocr.tessdata_dir"),
        ocr_languages=tuple(languages),
        vertical_language=ocr.get("vertical_language", "jpn_vert"),
        dpi=_positive(ocr.get("dpi", 300), "ocr.dpi"),
        max_page_pixels=_positive(ocr.get("max_page_megapixels", 25), "ocr.max_page_megapixels") * 1000000,
        ocr_timeout=_positive(ocr.get("page_timeout_seconds", 180), "ocr.page_timeout_seconds"),
        llm_enabled=llm_enabled,
        llm_executable=_path(source.parent, llm.get("executable", "llama-server.exe"), "llm.executable"),
        model_path=_path(source.parent, llm.get("model_path", "models/model.gguf"), "llm.model_path"),
        model_sha256=model_sha256.lower(),
        llm_port=llm_port,
        llm_context=llm_context,
        llm_chunk_tokens=llm_chunk_tokens,
        llm_overlap_tokens=llm_overlap_tokens,
        llm_max_output_tokens=llm_max_output_tokens,
        llm_timeout=_positive(llm.get("request_timeout_seconds", 600), "llm.request_timeout_seconds"),
        llm_cpu_threads=_positive(runtime.get("cpu_threads", 4), "runtime.cpu_threads"),
        llm_temperature=sampling("temperature", 0.7, 0.0, 2.0),
        llm_top_p=sampling("top_p", 0.8, 0.0, 1.0),
        llm_top_k=_positive(llm.get("top_k", 20), "llm.top_k"),
        llm_min_p=sampling("min_p", 0.0, 0.0, 1.0),
        llm_presence_penalty=sampling("presence_penalty", 1.5, -2.0, 2.0),
        repair_attempts=_positive(metadata.get("repair_attempts", 1), "metadata.repair_attempts"),
        prompt_version=str(metadata.get("prompt_version", "ja-catalog-v1")),
    )
    for path in (settings.database.parent, settings.cache_dir, settings.export_dir):
        if any(path == root or path in root.parents for root in roots):
            raise ConfigurationError("Storage directory cannot contain an input root")
    return settings
