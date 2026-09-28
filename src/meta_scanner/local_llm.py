"""Start a CPU-only llama.cpp server and call its loopback chat endpoint."""

from __future__ import annotations

import hashlib
import http.client
import json
import socket
import subprocess
import time
from typing import Self

from .config import Settings


class LlmError(RuntimeError):
    pass


class LlmDeferred(LlmError):
    pass


def verify_model(settings: Settings) -> None:
    if not settings.llm_executable.is_file() or not settings.model_path.is_file():
        raise LlmError(
            "DEPENDENCY_UNAVAILABLE: llama-server or the GGUF model is missing"
        )
    digest = hashlib.sha256()
    with settings.model_path.open("rb") as model:
        while chunk := model.read(4 * 1024 * 1024):
            digest.update(chunk)
    if digest.hexdigest() != settings.model_sha256:
        raise LlmError(
            "MODEL_HASH_MISMATCH: the GGUF file differs from the configured SHA-256"
        )


class LocalModel:
    def __init__(
        self,
        settings: Settings,
        stop_at: float | None = None,
        model_verified: bool = False,
    ):
        self.settings = settings
        self.stop_at = stop_at
        self.model_verified = model_verified
        self.process: subprocess.Popen | None = None
        self.log = None
        self.model_id = ""

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def _request(
        self, method: str, path: str, payload: dict | None = None, timeout: int = 5
    ) -> dict:
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.settings.llm_port, timeout=timeout
        )
        body = (
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
            if payload is not None
            else None
        )
        try:
            connection.request(
                method, path, body=body, headers={"Content-Type": "application/json"}
            )
            response = connection.getresponse()
            raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise LlmError("LLM_RESPONSE_TOO_LARGE")
            if response.status != 200:
                raise LlmError(
                    f"LLM_HTTP_ERROR: status {response.status}: {raw[:250].decode('utf-8', errors='replace')}"
                )
            return json.loads(raw)
        except (OSError, ValueError) as exc:
            raise LlmError(f"LLM_CONNECTION_ERROR: {exc}") from exc
        finally:
            connection.close()

    def start(self) -> None:
        if self.process is not None:
            return
        if not self.model_verified:
            verify_model(self.settings)
            self.model_verified = True
        with socket.socket() as probe:
            probe.settimeout(1)
            if probe.connect_ex(("127.0.0.1", self.settings.llm_port)) == 0:
                raise LlmError(
                    "LLM_ENDPOINT_BUSY: another process owns the configured port"
                )
        self.settings.cache_dir.mkdir(parents=True, exist_ok=True)
        self.log = (self.settings.cache_dir / "llama-server.log").open("ab")
        args = [
            str(self.settings.llm_executable),
            "-m",
            str(self.settings.model_path),
            "-ngl",
            "0",
            "-t",
            str(self.settings.llm_cpu_threads),
            "-c",
            str(self.settings.llm_context),
            "--parallel",
            "1",
            "--host",
            "127.0.0.1",
            "--port",
            str(self.settings.llm_port),
            "--no-warmup",
            "--log-disable",
        ]
        try:
            self.process = subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=self.log,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            limit = time.monotonic() + 60
            while time.monotonic() < limit:
                if self.process.poll() is not None:
                    raise LlmError(
                        f"LLM_START_FAILED: llama-server exited {self.process.returncode}"
                    )
                try:
                    health = self._request("GET", "/health", timeout=2)
                    if health.get("status") == "ok":
                        models = self._request("GET", "/v1/models", timeout=5)
                        self.model_id = models["data"][0]["id"]
                        return
                except LlmError, KeyError, IndexError:
                    pass
                time.sleep(0.5)
            raise LlmError(
                "LLM_START_TIMEOUT: server did not become healthy in 60 seconds"
            )
        except Exception:
            self.close()
            raise

    def complete(self, messages: list[dict], schema: dict, max_tokens: int) -> dict:
        self.start()
        timeout = self.settings.llm_timeout
        if self.stop_at is not None:
            remaining = int(self.stop_at - time.time())
            if remaining <= 0:
                raise LlmDeferred(
                    "PROCESSING_WINDOW_END: metadata generation can resume next run"
                )
            timeout = min(timeout, remaining)
        payload = {
            "model": self.model_id,
            "messages": messages,
            "stream": False,
            "max_tokens": max_tokens,
            "temperature": self.settings.llm_temperature,
            "top_p": self.settings.llm_top_p,
            "top_k": self.settings.llm_top_k,
            "min_p": self.settings.llm_min_p,
            "presence_penalty": self.settings.llm_presence_penalty,
            "chat_template_kwargs": {"enable_thinking": False},
            "reasoning_effort": "none",
            "response_format": {"type": "json_schema", "schema": schema},
        }
        try:
            answer = self._request(
                "POST", "/v1/chat/completions", payload, timeout=timeout
            )
        except LlmError as exc:
            if self.stop_at is not None and time.time() >= self.stop_at - 1:
                raise LlmDeferred(
                    "PROCESSING_WINDOW_END: metadata generation can resume next run"
                ) from exc
            raise
        try:
            content = answer["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise ValueError("empty content")  # noqa: TRY004
            return json.loads(content)
        except (
            KeyError,
            IndexError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            raise LlmError(
                f"METADATA_INVALID: llama-server returned invalid JSON: {exc}"
            ) from exc

    def close(self) -> None:
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
            self.process = None
        if self.log is not None:
            self.log.close()
            self.log = None
