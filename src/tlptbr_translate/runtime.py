from __future__ import annotations

import json
import io
import os
import platform
import plistlib
import re
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import zipfile
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import zstandard as zstd
from importlib.resources import files

from .postprocess import postprocess

_REPO_API_BASE = "https://api.github.com/repos/XapaJIaMnu/translateLocally"
_RELEASES_LATEST_API = f"{_REPO_API_BASE}/releases/latest"
_RELEASES_LIST_API = f"{_REPO_API_BASE}/releases"
_DOWNLOAD_RETRIES = 3
_OFFICIAL_FILES_BASE = "https://translatelocally.com/files/latest"


class TranslationError(RuntimeError):
    pass


def _verbose_enabled() -> bool:
    return os.getenv("TLPTBR_VERBOSE", "0").lower() in {"1", "true", "yes", "on"}


def _vlog(message: str) -> None:
    if _verbose_enabled():
        print(f"[tlptbr] {message}", file=sys.stderr)


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    evictions: int = 0


class LRUTranslationCache:
    def __init__(self, maxsize: int = 64, max_entry_chars: int = 512):
        self.maxsize = max(1, maxsize)
        self.max_entry_chars = max(1, max_entry_chars)
        self._data: OrderedDict[tuple[str, str], str] = OrderedDict()
        self._stats = CacheStats()
        self._lock = threading.Lock()

    def get(self, key: tuple[str, str]) -> str | None:
        with self._lock:
            val = self._data.get(key)
            if val is None:
                self._stats.misses += 1
                return None
            self._data.move_to_end(key)
            self._stats.hits += 1
            return val

    def set(self, key: tuple[str, str], value: str) -> None:
        if len(key[1]) > self.max_entry_chars or len(value) > self.max_entry_chars:
            return
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            if len(self._data) > self.maxsize:
                self._data.popitem(last=False)
                self._stats.evictions += 1

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "size": len(self._data),
                "maxsize": self.maxsize,
                "max_entry_chars": self.max_entry_chars,
                "hits": self._stats.hits,
                "misses": self._stats.misses,
                "evictions": self._stats.evictions,
            }


class NativeWorker:
    def __init__(
        self,
        binary: Path,
        models_root: Path,
        timeout_s: float = 60.0,
        keep_warm_interval_s: float = 300.0,
    ):
        self.binary = binary
        self.models_root = models_root
        self.timeout_s = timeout_s
        self.keep_warm_interval_s = keep_warm_interval_s

        self._proc: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()
        self._next_id = 1
        self._active_direction = "en-pt"
        self._warm_thread: threading.Thread | None = None
        self._stop_warm = threading.Event()
        self._last_stderr = ""
        force_cli = os.getenv("TLPTBR_FORCE_CLI", "0").lower() in {"1", "true", "yes", "on"}
        force_native = os.getenv("TLPTBR_FORCE_NATIVE", "0").lower() in {"1", "true", "yes", "on"}
        # On macOS, native messaging mode is unstable on a subset of hosts.
        # Default to CLI mode for reliability unless explicitly forced native.
        self._cli_fallback = force_cli or (platform.system().lower() == "darwin" and not force_native)

    def translate(self, text: str, direction: str) -> str:
        with self._lock:
            if self._cli_fallback:
                return self._translate_cli(text=text, direction=direction, timeout_s=self.timeout_s)
            if self._proc is None or self._proc.poll() is not None or direction != self._active_direction:
                self._active_direction = direction
                try:
                    self._restart()
                except Exception as exc:
                    if self._enable_cli_fallback(exc):
                        return self._translate_cli(text=text, direction=direction, timeout_s=self.timeout_s)
                    raise
            try:
                return self._send_translate(text, direction=direction, timeout_s=self.timeout_s)
            except Exception as exc:
                if self._enable_cli_fallback(exc):
                    return self._translate_cli(text=text, direction=direction, timeout_s=self.timeout_s)
                raise

    def close(self) -> None:
        self._stop_warm.set()
        if self._warm_thread and self._warm_thread.is_alive():
            self._warm_thread.join(timeout=2)
        self._warm_thread = None

        p = self._proc
        self._proc = None
        if not p:
            return
        try:
            if p.stdin:
                p.stdin.close()
        except OSError:
            pass
        try:
            p.terminate()
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            p.kill()

    def _restart(self) -> None:
        self.close()
        model = "en-pt-tiny" if self._active_direction == "en-pt" else "pt-en-tiny"
        cmd = [str(self.binary), "-p", "-m", model]
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(self.models_root),
            env=_worker_env(),
            bufsize=0,
        )
        if self._proc.stdin is None or self._proc.stdout is None:
            raise TranslationError("Failed to open translateLocally pipes")

        warmup = "warmup" if self._active_direction == "en-pt" else "aquecimento"
        self._send_translate(warmup, direction=self._active_direction, timeout_s=max(20.0, self.timeout_s))

        self._stop_warm.clear()
        self._warm_thread = threading.Thread(target=self._keep_warm_loop, daemon=True)
        self._warm_thread.start()

    def _keep_warm_loop(self) -> None:
        while not self._stop_warm.wait(self.keep_warm_interval_s):
            try:
                with self._lock:
                    if self._proc is None or self._proc.poll() is not None:
                        continue
                    warmup = "warmup" if self._active_direction == "en-pt" else "aquecimento"
                    self._send_translate(warmup, direction=self._active_direction, timeout_s=max(10.0, self.timeout_s))
            except Exception:
                pass

    def _send_translate(self, text: str, direction: str, timeout_s: float) -> str:
        src, trg = ("en", "pt") if direction == "en-pt" else ("pt", "en")

        payload = {
            "command": "Translate",
            "id": self._next_message_id(),
            "data": {"src": src, "trg": trg, "text": text, "html": False},
        }
        response = self._send_request(payload, timeout_s=timeout_s)
        try:
            return response["data"]["target"]["text"]
        except Exception as exc:
            raise TranslationError(f"Malformed response: {response}") from exc

    def _next_message_id(self) -> int:
        out = self._next_id
        self._next_id += 1
        return out

    def _send_request(self, payload: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        if self._proc is None or self._proc.stdin is None or self._proc.stdout is None:
            raise TranslationError("Worker is not running")

        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self._proc.stdin.write(struct.pack("@I", len(raw)))
            self._proc.stdin.write(raw)
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            details = self._collect_process_error_details(self._proc)
            raise TranslationError(f"translateLocally stdin write failed{details}") from exc

        deadline = time.monotonic() + timeout_s
        wanted_id = payload["id"]

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TranslationError("Timed out waiting for translateLocally response")
            header = self._read_exactly(4, remaining)
            size = struct.unpack("@I", header)[0]
            body = self._read_exactly(size, max(0.1, deadline - time.monotonic()))
            message = json.loads(body.decode("utf-8"))
            if message.get("id") != wanted_id:
                continue
            if message.get("update"):
                continue
            if message.get("success") is True:
                return message
            raise TranslationError(message.get("error", "Unknown translateLocally error"))

    def _read_exactly(self, n: int, timeout_s: float) -> bytes:
        if self._proc is None or self._proc.stdout is None:
            raise TranslationError("Worker not available")
        out = bytearray()
        start = time.monotonic()
        while len(out) < n:
            if time.monotonic() - start > timeout_s:
                raise TranslationError("Read timeout from translateLocally")
            chunk = self._proc.stdout.read(n - len(out))
            if not chunk:
                details = self._collect_process_error_details(self._proc)
                raise TranslationError(f"translateLocally closed stdout unexpectedly{details}")
            out.extend(chunk)
        return bytes(out)

    def _collect_process_error_details(self, proc: subprocess.Popen[bytes]) -> str:
        code = proc.poll()
        stderr_tail = ""
        if proc.stderr is not None:
            try:
                stderr_tail = proc.stderr.read().decode("utf-8", errors="replace")[-1200:].strip()
            except Exception:
                stderr_tail = ""
        if stderr_tail:
            self._last_stderr = stderr_tail
            return f" (exit_code={code}, stderr={stderr_tail!r})"
        return f" (exit_code={code})"

    def _enable_cli_fallback(self, exc: Exception) -> bool:
        # CLI fallback is primarily useful on macOS where -p may be unstable on
        # some machine/OS/CPU combinations while direct CLI translation still works.
        if platform.system().lower() != "darwin":
            return False
        self.close()
        self._cli_fallback = True
        _vlog(f"native messaging failed, enabling CLI fallback: {exc}")
        return True

    def _translate_cli(self, text: str, direction: str, timeout_s: float) -> str:
        errors: list[str] = []
        for model in _model_candidates_for_direction(direction, self.models_root):
            proc = subprocess.run(
                [str(self.binary), "-m", model],
                input=text + "\n",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=max(30.0, timeout_s),
                check=False,
                cwd=str(self.models_root),
                env=_worker_env(),
            )
            if proc.returncode != 0:
                tail = ((proc.stderr or "") + "\n" + (proc.stdout or ""))[-500:].strip()
                errors.append(f"{model}: rc={proc.returncode}: {tail}")
                continue
            out = (proc.stdout or "").strip()
            if not out:
                errors.append(f"{model}: empty output")
                continue
            return out.splitlines()[0].strip()

        raise TranslationError(f"CLI fallback translation failed for all candidate models: {' | '.join(errors[-4:])}")


class Translator:
    def __init__(
        self,
        binary_path: str | None = None,
        cache_size: int | None = None,
        cache_max_entry_chars: int | None = None,
        timeout_s: float = 60.0,
        keep_warm_interval_s: float | None = None,
        auto_download_binary: bool | None = None,
    ):
        self._cache = LRUTranslationCache(
            maxsize=cache_size or int(os.getenv("TLPTBR_CACHE_SIZE", "64")),
            max_entry_chars=cache_max_entry_chars or int(os.getenv("TLPTBR_CACHE_MAX_ENTRY_CHARS", "512")),
        )
        self._trim_every_n_calls = int(os.getenv("TLPTBR_TRIM_EVERY_N_CALLS", "8"))
        self._calls = 0
        self._libc = _load_libc()

        keep_warm = keep_warm_interval_s or float(os.getenv("TLPTBR_KEEP_WARM_INTERVAL_S", "300"))
        if auto_download_binary is None:
            auto_download_binary = os.getenv("TLPTBR_AUTO_DOWNLOAD", "1") not in {"0", "false", "False"}

        models_root = get_models_root()
        _prepare_runtime_models(models_root, binary_hint=None)
        resolved_binary = resolve_binary_path(
            binary_path=binary_path,
            auto_download=auto_download_binary,
            models_root=models_root,
        )
        runtime_models_root = _prepare_runtime_models(models_root, binary_hint=resolved_binary)
        _vlog(f"runtime models root: {runtime_models_root}")
        self._worker = NativeWorker(
            binary=resolved_binary,
            models_root=runtime_models_root,
            timeout_s=timeout_s,
            keep_warm_interval_s=keep_warm,
        )

    def translate(self, text: str, direction: str = "en-pt") -> str:
        if direction not in {"en-pt", "pt-en"}:
            raise ValueError("direction must be 'en-pt' or 'pt-en'")
        if not text.strip():
            raise ValueError("text cannot be empty")

        key = (direction, text)
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        raw = self._worker.translate(text, direction=direction)
        post = postprocess(raw, direction=direction)
        self._cache.set(key, post)

        self._calls += 1
        if self._calls % max(1, self._trim_every_n_calls) == 0:
            _malloc_trim(self._libc)

        return post

    def close(self) -> None:
        self._worker.close()
        _malloc_trim(self._libc)

    def __enter__(self) -> "Translator":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def stats(self) -> dict[str, Any]:
        return {
            "cache": self._cache.stats(),
            "trim": {"calls": self._calls, "trim_every_n_calls": self._trim_every_n_calls},
            "binary": str(self._worker.binary),
            "models_root": str(self._worker.models_root),
        }


def get_models_root() -> Path:
    models_dir = files("tlptbr_translate").joinpath("resources", "models")
    path = Path(str(models_dir))
    if not path.exists():
        raise TranslationError("Bundled models directory not found")
    return path


def resolve_binary_path(
    binary_path: str | None = None,
    auto_download: bool = True,
    models_root: Path | None = None,
) -> Path:
    reasons: list[str] = []
    _vlog(f"resolve_binary_path(auto_download={auto_download}, explicit={bool(binary_path)})")
    explicit = binary_path or os.getenv("TLPTBR_BINARY")
    if explicit:
        p = Path(explicit).expanduser().resolve()
        ok, why = _check_binary_usable(p, models_root=models_root)
        if p.exists() and ok:
            _vlog(f"using explicit binary: {p}")
            return p
        raise TranslationError(f"TLPTBR binary not usable at: {p}. Reason: {why}")

    bundled = _bundled_binary_for_platform()
    if bundled and bundled.exists():
        _ensure_executable(bundled)
        ok, why = _check_binary_usable(bundled, models_root=models_root)
        if ok:
            _vlog(f"using bundled binary: {bundled}")
            return bundled
        reasons.append(f"bundled binary not usable: {bundled} ({why})")
        _vlog(reasons[-1])

    if auto_download:
        try:
            downloaded = _download_binary_for_platform(models_root=models_root)
            ok, why = _check_binary_usable(downloaded, models_root=models_root) if downloaded else (False, "download failed")
            if downloaded and ok:
                _ensure_executable(downloaded)
                _vlog(f"using downloaded binary: {downloaded}")
                return downloaded
            reasons.append(f"auto-downloaded binary missing or unusable ({why})")
            _vlog(reasons[-1])
        except Exception as exc:
            reasons.append(f"auto-download failed: {exc}")
            _vlog(reasons[-1])

    if platform.system().lower() == "linux":
        if _bootstrap_qt_runtime():
            # Retry after Qt runtime bootstrap.
            candidates = [bundled] if bundled else []
            if auto_download:
                try:
                    dl = _download_binary_for_platform(models_root=models_root)
                    if dl:
                        candidates.append(dl)
                except Exception:
                    pass
            if from_path := shutil.which("translateLocally"):
                candidates.append(Path(from_path))
            for c in candidates:
                if c is None:
                    continue
                ok, why = _check_binary_usable(c, models_root=models_root)
                if ok:
                    _ensure_executable(c)
                    return c
                reasons.append(f"post-bootstrap candidate unusable: {c} ({why})")

    from_path = shutil.which("translateLocally")
    if from_path:
        candidate = Path(from_path)
        ok, why = _check_binary_usable(candidate, models_root=models_root)
        if ok:
            _vlog(f"using PATH binary: {candidate}")
            return candidate
        reasons.append(f"PATH binary not usable: {candidate} ({why})")
        _vlog(reasons[-1])

    details = "; ".join(reasons) if reasons else "no candidates found"
    raise TranslationError(
        "No usable translateLocally binary found. "
        "This package can auto-download it, or you can set TLPTBR_BINARY explicitly. "
        f"Details: {details}"
    )


def _bundled_binary_for_platform() -> Path | None:
    tag = _platform_tag()
    bin_dir = Path(str(files("tlptbr_translate").joinpath("resources", "bin", tag)))
    if not bin_dir.exists():
        return None
    exe_name = "translateLocally.exe" if os.name == "nt" else "translateLocally"
    candidate = bin_dir / exe_name
    if candidate.exists():
        return candidate
    return None


def _platform_tag() -> str:
    sysname = platform.system().lower()
    machine = platform.machine().lower()

    if machine in {"x86_64", "amd64"}:
        arch = "x86_64"
    elif machine in {"aarch64", "arm64"}:
        arch = "arm64"
    else:
        arch = machine

    if "linux" in sysname:
        return f"linux-{arch}"
    if "darwin" in sysname or "mac" in sysname:
        return f"macos-{arch}"
    if "windows" in sysname:
        return f"windows-{arch}"
    return f"{sysname}-{arch}"


def _download_binary_for_platform(models_root: Path | None = None) -> Path | None:
    tag = _platform_tag()
    _vlog(f"starting auto-download for tag: {tag}")
    cache_root = Path.home() / ".cache" / "tlptbr_translate" / "bin" / tag
    cache_root.mkdir(parents=True, exist_ok=True)

    exe_name = "translateLocally.exe" if tag.startswith("windows") else "translateLocally"
    final_path = cache_root / exe_name
    if final_path.exists():
        ok, why = _check_binary_usable(final_path, models_root=models_root)
        if ok:
            _vlog(f"binary already cached and valid: {final_path}")
            return final_path
        _vlog(f"cached binary invalid, purging cache candidate: {final_path} ({why})")
        try:
            final_path.unlink(missing_ok=True)
        except Exception:
            pass
        app_bundle = cache_root / "translateLocally.app"
        if app_bundle.exists():
            try:
                shutil.rmtree(app_bundle)
            except Exception:
                pass

    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "fast-translate-runtime",
    }
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    with httpx.Client(timeout=90.0, headers=headers) as client:
        payload = _fetch_release_payload(client=client, has_token=bool(token))

    ranked_assets = _rank_release_assets(payload.get("assets", []), tag)
    official_assets = _official_fallback_assets(tag)
    if official_assets:
        ranked_assets = ranked_assets + [u for u in official_assets if u not in ranked_assets]
    if not ranked_assets:
        _vlog("no matching release asset found")
        return None

    for asset_url in ranked_assets:
        _vlog(f"trying asset: {asset_url}")
        download_path = cache_root / Path(asset_url).name
        last_exc: Exception | None = None
        ok_download = False
        for attempt in range(1, _DOWNLOAD_RETRIES + 1):
            try:
                _vlog(f"download attempt {attempt}/{_DOWNLOAD_RETRIES}: {asset_url}")
                with httpx.stream("GET", asset_url, timeout=180.0, follow_redirects=True) as r:
                    r.raise_for_status()
                    with download_path.open("wb") as f:
                        for chunk in r.iter_bytes(chunk_size=1024 * 512):
                            f.write(chunk)
                ok_download = True
                break
            except Exception as exc:
                last_exc = exc
                _vlog(f"download attempt {attempt} failed: {exc}")
                if attempt < _DOWNLOAD_RETRIES:
                    time.sleep(1.5 * attempt)
        if not ok_download:
            if last_exc:
                _vlog(f"asset failed permanently: {last_exc}")
            continue

        extracted = _extract_candidate_binary(download_path, cache_root)
        if not extracted or not extracted.exists():
            _vlog(f"failed to extract binary from asset: {asset_url}")
            continue

        probe_ok, probe_why = _check_binary_usable(extracted, models_root=models_root)
        if not probe_ok:
            _vlog(f"asset probe failed: {probe_why}")
            continue

        if extracted != final_path:
            shutil.copy2(extracted, final_path)
        _vlog(f"selected working binary: {final_path}")
        return final_path

    _vlog("all candidate assets failed")
    return None


def _fetch_release_payload(client: httpx.Client, has_token: bool) -> dict[str, Any]:
    rel = client.get(_RELEASES_LATEST_API)
    if rel.status_code == 403:
        msg = "GitHub API rate limit exceeded while fetching translateLocally release metadata."
        if not has_token:
            msg += " Provide GITHUB_TOKEN/GH_TOKEN to avoid anonymous rate limits."
        raise TranslationError(msg)
    if rel.status_code == 404:
        lst = client.get(_RELEASES_LIST_API)
        if lst.status_code == 403:
            msg = "GitHub API rate limit exceeded while fetching translateLocally release list."
            if not has_token:
                msg += " Provide GITHUB_TOKEN/GH_TOKEN to avoid anonymous rate limits."
            raise TranslationError(msg)
        lst.raise_for_status()
        releases = lst.json()
        if not isinstance(releases, list) or not releases:
            raise TranslationError("No releases found for translateLocally repository.")
        for release in releases:
            if release.get("draft"):
                continue
            if release.get("assets"):
                return release
        return releases[0]

    rel.raise_for_status()
    return rel.json()


def _rank_release_assets(assets: list[dict[str, Any]], tag: str) -> list[str]:
    if not assets:
        return []
    tag_parts = _asset_match_markers(tag)

    host_macos_major = _host_macos_major()

    def score(name: str) -> tuple[int, int]:
        n = name.lower()
        platform_hits = sum(1 for p in tag_parts if p in n)
        ext_bonus = 0
        penalty = 0
        mac_bonus = 0
        if tag.startswith("linux") and n.endswith(".deb"):
            ext_bonus = 6
        # Prefer generic compatibility binaries over AVX-specific ones.
        if "core-avx" in n or ".avx" in n or "avx." in n:
            penalty = -2
        if tag.startswith("macos"):
            asset_major = _asset_macos_major(n)
            if host_macos_major is not None and asset_major is not None:
                if asset_major > host_macos_major:
                    penalty -= 20
                else:
                    mac_bonus += max(0, 8 - (host_macos_major - asset_major))
        if n.endswith(".zip"):
            ext_bonus = 5
        elif n.endswith(".tar.gz") or n.endswith(".tgz"):
            ext_bonus = 4
        elif n.endswith(".appimage"):
            ext_bonus = 3
        elif n.endswith(".exe"):
            ext_bonus = 3
        elif n.endswith(".deb"):
            ext_bonus = 1
        return (platform_hits + penalty + mac_bonus, ext_bonus)

    ranked = sorted(assets, key=lambda a: score(a.get("name", "")), reverse=True)
    out: list[str] = []
    for item in ranked:
        name = item.get("name", "")
        url = item.get("browser_download_url")
        if not url:
            continue
        low = name.lower()
        if not _asset_extension_allowed(tag, low):
            continue
        if score(name)[0] == 0 and not (tag.startswith("linux") and low.endswith(".deb")):
            continue
        out.append(url)
    return out


def _asset_match_markers(tag: str) -> set[str]:
    markers = set(tag.split("-"))
    low_tag = tag.lower()

    if "linux" in low_tag:
        markers.update({"linux", "ubuntu", "debian"})
    if "macos" in low_tag or "darwin" in low_tag:
        markers.update({"macos", "darwin", "osx"})
    if "windows" in low_tag:
        markers.update({"windows", "win"})

    if "x86_64" in low_tag or "amd64" in low_tag:
        markers.update({"x86_64", "x86-64", "amd64", "x64"})
    if "arm64" in low_tag or "aarch64" in low_tag:
        markers.update({"arm64", "aarch64", "armv8", "armv8.5-a"})

    return markers


def _asset_extension_allowed(tag: str, name_lower: str) -> bool:
    if tag.startswith("macos"):
        return name_lower.endswith(".dmg")
    if tag.startswith("windows"):
        return name_lower.endswith(".exe") or name_lower.endswith(".zip")
    if tag.startswith("linux"):
        return name_lower.endswith(".deb") or name_lower.endswith(".appimage") or name_lower.endswith(".tar.gz") or name_lower.endswith(".tgz") or name_lower.endswith(".zip")
    return True


def _official_fallback_assets(tag: str) -> list[str]:
    if tag == "macos-arm64":
        return [
            f"{_OFFICIAL_FILES_BASE}/translateLocally.macos-11.0.compat.dmg",
        ]
    if tag == "macos-x86_64":
        return [
            f"{_OFFICIAL_FILES_BASE}/translateLocally.macos-11.0.compat.dmg",
            f"{_OFFICIAL_FILES_BASE}/translateLocally.macos-13.x86-64.dmg",
            f"{_OFFICIAL_FILES_BASE}/translateLocally.macos-12.x86-64.dmg",
        ]
    return []


def _host_macos_major() -> int | None:
    if platform.system().lower() != "darwin":
        return None
    ver = platform.mac_ver()[0]
    if not ver:
        return None
    try:
        return int(ver.split(".")[0])
    except Exception:
        return None


def _asset_macos_major(name: str) -> int | None:
    m = re.search(r"macos-(\d+)", name)
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def _extract_candidate_binary(archive: Path, out_dir: Path) -> Path | None:
    lower = archive.name.lower()
    _vlog(f"extract candidate binary from: {archive.name}")
    if lower.endswith(".appimage") or lower.endswith(".exe"):
        _ensure_executable(archive)
        return archive
    if lower.endswith(".deb"):
        return _extract_from_deb(archive, out_dir)
    if lower.endswith(".dmg"):
        return _extract_from_dmg(archive, out_dir)

    with tempfile.TemporaryDirectory(prefix="tlptbr_extract_") as tmp:
        tmpdir = Path(tmp)
        if lower.endswith(".zip"):
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(tmpdir)
        elif lower.endswith(".tar.gz") or lower.endswith(".tgz"):
            with tarfile.open(archive, "r:gz") as tf:
                tf.extractall(tmpdir)
        else:
            return None

        for p in tmpdir.rglob("*"):
            name = p.name.lower()
            if p.is_file() and name in {"translatelocally", "translatelocally.exe"}:
                target = out_dir / ("translateLocally.exe" if name.endswith(".exe") else "translateLocally")
                shutil.copy2(p, target)
                _ensure_executable(target)
                return target
    return None


def _extract_from_deb(deb_path: Path, out_dir: Path) -> Path | None:
    _vlog(f"extracting .deb: {deb_path}")
    with tempfile.TemporaryDirectory(prefix="tlptbr_deb_") as tmp:
        tmpdir = Path(tmp)
        extracted = tmpdir / "root"
        extracted.mkdir(parents=True, exist_ok=True)

        data_name, data_bytes = _read_data_tar_from_deb(deb_path)
        tar_stream = _decompress_data_member(data_name, data_bytes)
        with tarfile.open(fileobj=io.BytesIO(tar_stream), mode="r:") as tf:
            tf.extractall(extracted)

        candidate = extracted / "usr" / "bin" / "translateLocally"
        if not candidate.exists():
            hits = list(extracted.rglob("translateLocally"))
            if not hits:
                return None
            candidate = hits[0]

        target = out_dir / "translateLocally"
        shutil.copy2(candidate, target)
        _ensure_executable(target)
        return target


def _read_data_tar_from_deb(deb_path: Path) -> tuple[str, bytes]:
    raw = deb_path.read_bytes()
    if not raw.startswith(b"!<arch>\n"):
        raise TranslationError(f"Invalid .deb archive format: {deb_path}")
    pos = 8
    while pos + 60 <= len(raw):
        hdr = raw[pos : pos + 60]
        name = hdr[0:16].decode("utf-8", errors="replace").strip().rstrip("/")
        size_str = hdr[48:58].decode("utf-8", errors="replace").strip()
        try:
            size = int(size_str)
        except ValueError as exc:
            raise TranslationError(f"Invalid ar member size in {deb_path}") from exc
        pos += 60
        data = raw[pos : pos + size]
        pos += size + (size % 2)
        if name.startswith("data.tar"):
            return name, data
    raise TranslationError(f"Could not find data.tar.* inside .deb: {deb_path}")


def _decompress_data_member(name: str, data: bytes) -> bytes:
    low = name.lower()
    if low.endswith(".zst"):
        return zstd.ZstdDecompressor().decompress(data)
    if low.endswith(".xz"):
        import lzma

        return lzma.decompress(data)
    if low.endswith(".gz"):
        import gzip

        return gzip.decompress(data)
    if low.endswith(".bz2"):
        import bz2

        return bz2.decompress(data)
    return data


def _extract_from_dmg(dmg_path: Path, out_dir: Path) -> Path | None:
    if platform.system().lower() != "darwin":
        return None
    _vlog(f"extracting .dmg: {dmg_path}")

    attach = subprocess.run(
        ["hdiutil", "attach", "-nobrowse", "-readonly", str(dmg_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    mount_points: list[str] = []
    for line in attach.stdout.splitlines():
        cols = line.split("\t")
        if cols and cols[-1].startswith("/Volumes/"):
            mount_points.append(cols[-1].strip())

    if not mount_points:
        return None
    mount = Path(mount_points[-1])
    try:
        app_candidates = list(mount.rglob("translateLocally.app"))
        if app_candidates:
            app_src = app_candidates[0]
            app_dst = out_dir / "translateLocally.app"
            if app_dst.exists():
                shutil.rmtree(app_dst)
            shutil.copytree(app_src, app_dst, symlinks=True)
            bin_in_app = app_dst / "Contents" / "MacOS" / "translateLocally"
            if bin_in_app.exists():
                _ensure_executable(bin_in_app)
                _clear_quarantine_recursive(app_dst)
                launcher = out_dir / "translateLocally"
                launcher.write_text(
                    "#!/usr/bin/env bash\n"
                    "set -euo pipefail\n"
                    "DIR=\"$(cd \"$(dirname \"$0\")\" && pwd)\"\n"
                    "exec \"$DIR/translateLocally.app/Contents/MacOS/translateLocally\" \"$@\"\n",
                    encoding="utf-8",
                )
                _ensure_executable(launcher)
                _clear_quarantine_recursive(launcher)
                return launcher

        candidates = list(mount.rglob("translateLocally"))
        if not candidates:
            return None
        target = out_dir / "translateLocally"
        shutil.copy2(candidates[0], target)
        _ensure_executable(target)
        _clear_quarantine_recursive(target)
        return target
    finally:
        subprocess.run(["hdiutil", "detach", str(mount)], check=False, capture_output=True)


def _ensure_executable(path: Path) -> None:
    if os.name == "nt":
        return
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _clear_quarantine_recursive(path: Path) -> None:
    if platform.system().lower() != "darwin":
        return
    xattr = shutil.which("xattr")
    if not xattr:
        return
    try:
        subprocess.run([xattr, "-dr", "com.apple.quarantine", str(path)], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        return


def _worker_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("OMP_NUM_THREADS", "1")
    env.setdefault("MALLOC_ARENA_MAX", "2")
    env.setdefault("MALLOC_TRIM_THRESHOLD_", "131072")
    env.setdefault("MALLOC_MMAP_THRESHOLD_", "131072")
    qt_lib = _qt_runtime_lib_dir()
    if qt_lib is not None:
        old = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = f"{qt_lib}:{old}" if old else str(qt_lib)
    return env


def _prepare_runtime_models(models_root: Path, binary_hint: Path | None = None) -> Path:
    if not models_root.exists():
        return models_root

    targets: list[tuple[Path, bool]] = []
    selected_root = models_root
    sysname = platform.system().lower()
    if sysname == "darwin":
        app_support = Path.home() / "Library" / "Application Support" / "translateLocally"
        targets.append((app_support, False))
        targets.append((app_support / "models", False))
        container_roots = _darwin_container_model_roots(binary_hint)
        preferred_container: Path | None = container_roots[0] if container_roots else None
        for container_root in container_roots:
            # Inside sandbox/container, prefer copy over symlink.
            targets.append((container_root, True))
            targets.append((container_root / "models", True))
        if preferred_container is not None:
            selected_root = preferred_container
    elif sysname == "linux":
        local_share = Path.home() / ".local" / "share" / "translateLocally"
        targets.append((local_share, False))
        targets.append((local_share / "models", False))
        selected_root = local_share

    if not targets:
        return models_root

    model_dirs = [p for p in models_root.iterdir() if p.is_dir()]
    if not model_dirs:
        return models_root

    for target, force_copy in targets:
        try:
            target.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            _vlog(f"cannot create runtime model dir {target}: {exc}")
            continue
        for src in model_dirs:
            dst = target / src.name
            if dst.exists():
                continue
            if not force_copy:
                try:
                    dst.symlink_to(src, target_is_directory=True)
                    _vlog(f"linked model dir: {dst} -> {src}")
                    continue
                except Exception:
                    pass
            try:
                shutil.copytree(src, dst, symlinks=True)
                _vlog(f"copied model dir: {dst}")
            except Exception as exc:
                _vlog(f"failed to mirror model dir {src} -> {dst}: {exc}")

    return selected_root if selected_root.exists() else models_root


def _darwin_container_model_roots(binary_hint: Path | None = None) -> list[Path]:
    if platform.system().lower() != "darwin":
        return []

    out: list[Path] = []
    seen: set[str] = set()

    def add_bundle_id(bundle_id: str) -> None:
        if not bundle_id or bundle_id in seen:
            return
        seen.add(bundle_id)
        out.append(
            Path.home()
            / "Library"
            / "Containers"
            / bundle_id
            / "Data"
            / "Library"
            / "Application Support"
            / "translateLocally"
        )

    env_id = os.getenv("APP_SANDBOX_CONTAINER_ID", "").strip()
    if env_id:
        add_bundle_id(env_id)

    app = _resolve_app_bundle_from_binary(binary_hint) if binary_hint else None
    if app:
        info_plist = app / "Contents" / "Info.plist"
        if info_plist.exists():
            try:
                data = plistlib.loads(info_plist.read_bytes())
                add_bundle_id(str(data.get("CFBundleIdentifier", "")).strip())
            except Exception:
                pass

    base = Path.home() / "Library" / "Containers"
    if base.exists():
        for entry in base.iterdir():
            if not entry.is_dir():
                continue
            low = entry.name.lower()
            # Avoid matching Apple's own Translate app containers.
            if "translatelocally" not in low:
                continue
            out.append(entry / "Data" / "Library" / "Application Support" / "translateLocally")

    # Prioritize the official bundle id first if present.
    scored: list[tuple[int, Path]] = []
    for p in out:
        low = str(p).lower()
        score = 0
        if "com.translatelocally.translatelocally" in low:
            score += 100
        if "com.translatelocally" in low:
            score += 50
        if "containers" in low:
            score += 10
        scored.append((score, p))
    scored.sort(key=lambda it: it[0], reverse=True)

    dedup: list[Path] = []
    dedup_seen: set[str] = set()
    for _, p in scored:
        key = str(p)
        if key in dedup_seen:
            continue
        dedup_seen.add(key)
        dedup.append(p)
    return dedup


def _resolve_app_bundle_from_binary(binary: Path | None) -> Path | None:
    if binary is None:
        return None
    p = binary.resolve()
    if p.name == "translateLocally" and p.parent.name == "MacOS" and p.parent.parent.name == "Contents":
        app = p.parent.parent.parent
        if app.name.endswith(".app"):
            return app
    if p.name == "translateLocally":
        app = p.parent / "translateLocally.app"
        if app.exists():
            return app
    for parent in p.parents:
        if parent.name.endswith(".app"):
            return parent
    return None


def _model_candidates_for_direction(direction: str, models_root: Path) -> list[str]:
    primary = "en-pt-tiny" if direction == "en-pt" else "pt-en-tiny"
    fallback_prefix = "enpt" if direction == "en-pt" else "pten"
    out: list[str] = [primary]
    seen = set(out)

    if models_root.exists():
        for model_dir in sorted(p for p in models_root.iterdir() if p.is_dir()):
            info = model_dir / "model_info.json"
            short_name = ""
            if info.exists():
                try:
                    raw = json.loads(info.read_text(encoding="utf-8"))
                    short_name = str(raw.get("shortName", "")).strip()
                except Exception:
                    short_name = ""
            for candidate in (short_name, model_dir.name):
                if not candidate or candidate in seen:
                    continue
                if candidate == primary or candidate.startswith(fallback_prefix):
                    out.append(candidate)
                    seen.add(candidate)

    return out


def _check_binary_usable(binary: Path | None, models_root: Path | None = None) -> tuple[bool, str]:
    if binary is None:
        return False, "binary path is None"
    if not binary.exists():
        return False, "binary path does not exist"
    try:
        proc = subprocess.run(
            [str(binary), "--help"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
            env=_worker_env(),
        )
        stderr = (proc.stderr or b"").decode("utf-8", errors="replace").lower()
        hard_fail_markers = (
            "error while loading shared libraries",
            "cannot open shared object file",
            "no such file or directory",
            "not found",
            "undefined symbol",
            "wrong elf class",
        )
        if any(marker in stderr for marker in hard_fail_markers):
            missing = _linux_missing_libs(binary)
            extra = f"; missing libs: {missing}" if missing else ""
            return False, f"loader failure: {stderr[-300:]}{extra}"
        if proc.returncode != 0:
            return False, f"--help returned rc={proc.returncode}: {stderr[-300:]}"
        # Probe real model load when models are available, because some binaries
        # pass --help but fail only when translation engine/model initializes.
        if models_root is not None and platform.system().lower() == "darwin":
            # Prefer CLI probe on macOS because native messaging may fail even
            # when direct CLI translation works.
            ok, why = _probe_cli_model(binary, models_root=models_root, direction="en-pt", timeout_s=45.0)
            if not ok:
                if "we could not find a model identified as" in why.lower():
                    return True, f"model lookup deferred on macOS: {why}"
                return False, f"cli model probe failed: {why}"
        elif models_root is not None:
            probe = subprocess.run(
                [str(binary), "-m", "en-pt-tiny"],
                input="hello\n",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=45,
                check=False,
                cwd=str(models_root),
                env=_worker_env(),
            )
            if probe.returncode != 0:
                perr = ((probe.stderr or "") + "\n" + (probe.stdout or ""))[-400:]
                return False, f"model probe failed rc={probe.returncode}: {perr}"
        return True, f"returncode={proc.returncode}"
    except Exception as exc:
        return False, f"probe exception: {exc}"


def _linux_missing_libs(binary: Path) -> list[str]:
    if platform.system().lower() != "linux":
        return []
    try:
        proc = subprocess.run(
            ["ldd", str(binary)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=20,
            check=False,
        )
        lines = (proc.stdout or "") + "\n" + (proc.stderr or "")
        out = []
        for ln in lines.splitlines():
            if "=> not found" in ln:
                out.append(ln.strip())
        return out
    except Exception:
        return []


def _qt_runtime_lib_dir() -> Path | None:
    try:
        import PyQt6  # type: ignore

        root = Path(PyQt6.__file__).resolve().parent
        cand = root / "Qt6" / "lib"
        if cand.exists():
            return cand
    except Exception:
        pass
    return None


def _bootstrap_qt_runtime() -> bool:
    if _qt_runtime_lib_dir() is not None:
        _vlog("qt runtime already available")
        return True
    _vlog("qt runtime missing; trying bootstrap via pip")
    commands = [
        [sys.executable, "-m", "pip", "install", "--quiet", "PyQt6>=6.6"],
        [sys.executable, "-m", "pip", "install", "--quiet", "--break-system-packages", "PyQt6>=6.6"],
    ]
    if platform.system().lower() == "linux":
        commands.append([sys.executable, "-m", "pip", "install", "--quiet", "PySide6>=6.6"])

    last_err = ""
    try:
        subprocess.run([sys.executable, "-m", "ensurepip", "--upgrade"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

    for cmd in commands:
        try:
            proc = subprocess.run(
                cmd,
                check=False,
                timeout=900,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            if proc.returncode == 0 and _qt_runtime_lib_dir() is not None:
                _vlog(f"qt runtime bootstrap succeeded with: {' '.join(cmd)}")
                return True
            tail = ((proc.stderr or "") + "\n" + (proc.stdout or ""))[-600:]
            last_err = tail.strip()
            _vlog(f"qt runtime bootstrap command failed ({proc.returncode}): {' '.join(cmd)} | {last_err}")
        except Exception as exc:
            last_err = str(exc)
            _vlog(f"qt runtime bootstrap exception on {' '.join(cmd)}: {exc}")
    if last_err:
        _vlog(f"qt bootstrap failed final: {last_err}")
    try:
        return _qt_runtime_lib_dir() is not None
    except Exception:
        return False


def _probe_native_messaging(binary: Path, models_root: Path, timeout_s: float = 30.0) -> tuple[bool, str]:
    proc: subprocess.Popen[bytes] | None = None
    try:
        proc = subprocess.Popen(
            [str(binary), "-p", "-m", "en-pt-tiny"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(models_root),
            env=_worker_env(),
            bufsize=0,
        )
        if proc.stdin is None or proc.stdout is None:
            return False, "failed to open stdin/stdout pipes"

        payload = {
            "command": "Translate",
            "id": 1,
            "data": {"src": "en", "trg": "pt", "text": "hello", "html": False},
        }
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        proc.stdin.write(struct.pack("@I", len(raw)))
        proc.stdin.write(raw)
        proc.stdin.flush()

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            header = proc.stdout.read(4)
            if not header:
                rc = proc.poll()
                err = ""
                if proc.stderr is not None:
                    try:
                        err = proc.stderr.read().decode("utf-8", errors="replace")[-600:]
                    except Exception:
                        err = ""
                return False, f"stdout closed rc={rc} stderr={err!r}"
            size = struct.unpack("@I", header)[0]
            body = proc.stdout.read(size)
            if not body:
                return False, "empty body from native messaging"
            msg = json.loads(body.decode("utf-8", errors="replace"))
            if msg.get("id") != 1 or msg.get("update"):
                continue
            if msg.get("success") is True:
                return True, "ok"
            return False, f"native error: {msg.get('error', 'unknown')}"
        return False, "native probe timeout"
    except Exception as exc:
        return False, f"native probe exception: {exc}"
    finally:
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass


def _probe_cli_model(binary: Path, models_root: Path, direction: str = "en-pt", timeout_s: float = 45.0) -> tuple[bool, str]:
    errors: list[str] = []
    for model in _model_candidates_for_direction(direction, models_root):
        try:
            probe = subprocess.run(
                [str(binary), "-m", model],
                input="hello\n",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout_s,
                check=False,
                cwd=str(models_root),
                env=_worker_env(),
            )
            if probe.returncode != 0:
                perr = ((probe.stderr or "") + "\n" + (probe.stdout or ""))[-350:].strip()
                errors.append(f"{model}: rc={probe.returncode}: {perr}")
                continue
            out = (probe.stdout or "").strip()
            if not out:
                errors.append(f"{model}: empty output")
                continue
            return True, f"ok via model={model}"
        except Exception as exc:
            errors.append(f"{model}: exception: {exc}")
    return False, " | ".join(errors[-4:])


def _load_libc():
    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6")
        libc.malloc_trim.argtypes = [ctypes.c_size_t]
        libc.malloc_trim.restype = ctypes.c_int
        return libc
    except Exception:
        return None


def _malloc_trim(libc) -> None:
    if libc is None:
        return
    try:
        libc.malloc_trim(0)
    except Exception:
        return
