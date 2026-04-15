from __future__ import annotations

import json
import os
import platform
import shutil
import stat
import struct
import subprocess
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
from importlib.resources import files

from .postprocess import postprocess

_REPO_API_BASE = "https://api.github.com/repos/XapaJIaMnu/translateLocally"
_RELEASES_LATEST_API = f"{_REPO_API_BASE}/releases/latest"
_RELEASES_LIST_API = f"{_REPO_API_BASE}/releases"


class TranslationError(RuntimeError):
    pass


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

    def translate(self, text: str, direction: str) -> str:
        with self._lock:
            if self._proc is None or self._proc.poll() is not None or direction != self._active_direction:
                self._active_direction = direction
                self._restart()
            return self._send_translate(text, direction=direction, timeout_s=self.timeout_s)

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
        self._proc.stdin.write(struct.pack("@I", len(raw)))
        self._proc.stdin.write(raw)
        self._proc.stdin.flush()

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

        resolved_binary = resolve_binary_path(binary_path=binary_path, auto_download=auto_download_binary)
        models_root = get_models_root()
        self._worker = NativeWorker(
            binary=resolved_binary,
            models_root=models_root,
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


def resolve_binary_path(binary_path: str | None = None, auto_download: bool = True) -> Path:
    explicit = binary_path or os.getenv("TLPTBR_BINARY")
    if explicit:
        p = Path(explicit).expanduser().resolve()
        if p.exists() and _is_binary_usable(p):
            return p
        raise TranslationError(f"TLPTBR binary not usable at: {p}")

    bundled = _bundled_binary_for_platform()
    if bundled and bundled.exists():
        _ensure_executable(bundled)
        if _is_binary_usable(bundled):
            return bundled

    if auto_download:
        downloaded = _download_binary_for_platform()
        if downloaded and _is_binary_usable(downloaded):
            _ensure_executable(downloaded)
            return downloaded

    from_path = shutil.which("translateLocally")
    if from_path:
        candidate = Path(from_path)
        if _is_binary_usable(candidate):
            return candidate

    raise TranslationError(
        "No usable translateLocally binary found. This package can auto-download it, "
        "or you can set TLPTBR_BINARY explicitly."
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


def _download_binary_for_platform() -> Path | None:
    tag = _platform_tag()
    cache_root = Path.home() / ".cache" / "tlptbr_translate" / "bin" / tag
    cache_root.mkdir(parents=True, exist_ok=True)

    exe_name = "translateLocally.exe" if tag.startswith("windows") else "translateLocally"
    final_path = cache_root / exe_name
    if final_path.exists():
        return final_path

    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "fast-translate-runtime",
    }
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    with httpx.Client(timeout=90.0, headers=headers) as client:
        payload = _fetch_release_payload(client=client, has_token=bool(token))

    asset_url = _pick_release_asset(payload.get("assets", []), tag)
    if not asset_url:
        return None

    download_path = cache_root / Path(asset_url).name
    with httpx.stream("GET", asset_url, timeout=180.0, follow_redirects=True) as r:
        r.raise_for_status()
        with download_path.open("wb") as f:
            for chunk in r.iter_bytes(chunk_size=1024 * 512):
                f.write(chunk)

    extracted = _extract_candidate_binary(download_path, cache_root)
    if extracted and extracted.exists():
        if extracted != final_path:
            shutil.copy2(extracted, final_path)
        return final_path

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


def _pick_release_asset(assets: list[dict[str, Any]], tag: str) -> str | None:
    if not assets:
        return None

    tag_parts = set(tag.split("-"))

    def score(name: str) -> tuple[int, int]:
        n = name.lower()
        platform_hits = sum(1 for p in tag_parts if p in n)
        ext_bonus = 0
        if tag.startswith("linux") and n.endswith(".deb"):
            ext_bonus = 6
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
        return (platform_hits, ext_bonus)

    ranked = sorted(assets, key=lambda a: score(a.get("name", "")), reverse=True)
    for item in ranked:
        name = item.get("name", "")
        url = item.get("browser_download_url")
        if not url:
            continue
        if score(name)[0] == 0:
            continue
        if name.lower().endswith(".deb") and not tag.startswith("linux"):
            continue
        return url
    return None


def _extract_candidate_binary(archive: Path, out_dir: Path) -> Path | None:
    lower = archive.name.lower()
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
    with tempfile.TemporaryDirectory(prefix="tlptbr_deb_") as tmp:
        tmpdir = Path(tmp)
        extracted = tmpdir / "root"
        extracted.mkdir(parents=True, exist_ok=True)

        dpkg = shutil.which("dpkg-deb")
        if dpkg:
            subprocess.run([dpkg, "-x", str(deb_path), str(extracted)], check=True)
        else:
            ar = shutil.which("ar")
            tar = shutil.which("tar")
            if not ar or not tar:
                return None
            subprocess.run([ar, "x", str(deb_path)], check=True, cwd=str(tmpdir))
            data_tar = None
            for cand in tmpdir.glob("data.tar.*"):
                data_tar = cand
                break
            if data_tar is None:
                return None
            subprocess.run([tar, "-xf", str(data_tar), "-C", str(extracted)], check=True)

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


def _extract_from_dmg(dmg_path: Path, out_dir: Path) -> Path | None:
    if platform.system().lower() != "darwin":
        return None

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
                return bin_in_app

        candidates = list(mount.rglob("translateLocally"))
        if not candidates:
            return None
        target = out_dir / "translateLocally"
        shutil.copy2(candidates[0], target)
        _ensure_executable(target)
        return target
    finally:
        subprocess.run(["hdiutil", "detach", str(mount)], check=False, capture_output=True)


def _ensure_executable(path: Path) -> None:
    if os.name == "nt":
        return
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _worker_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("OMP_NUM_THREADS", "1")
    env.setdefault("MALLOC_ARENA_MAX", "2")
    env.setdefault("MALLOC_TRIM_THRESHOLD_", "131072")
    env.setdefault("MALLOC_MMAP_THRESHOLD_", "131072")
    return env


def _is_binary_usable(binary: Path) -> bool:
    try:
        proc = subprocess.run(
            [str(binary), "--help"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        if proc.returncode == 0:
            return True
        stderr = (proc.stderr or b"").decode("utf-8", errors="replace").lower()
        hard_fail_markers = (
            "error while loading shared libraries",
            "cannot open shared object file",
            "no such file or directory",
            "not found",
        )
        return not any(marker in stderr for marker in hard_fail_markers)
    except Exception:
        return False


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
