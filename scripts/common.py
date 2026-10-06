"""Shared plumbing: paths, config, Keychain secrets, logging and the Ollama client.

Every other script imports this module. Paths resolve against PF_ROOT when it is set
(tests and the demo use that to run against a throwaway copy), else the repo root.

Run directly to manage secrets:
    python scripts/common.py secret set self.name
    python scripts/common.py secret status
"""
from __future__ import annotations

import argparse
import base64
import getpass
import json
import logging
import os
import re
import sys
import tomllib
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
KEYCHAIN_SERVICE = "personal-finance"
SECRET_KEYS = ["name", "dob", "customer_id"]

log = logging.getLogger("pf")


class PFError(RuntimeError):
    """An expected failure with a message meant for the person running the script."""


# --------------------------------------------------------------------------- paths

def root() -> Path:
    return Path(os.environ.get("PF_ROOT") or REPO_ROOT)


def ledger_dir() -> Path:
    return root() / "ledgers"


def main_ledger() -> Path:
    return ledger_dir() / "main.beancount"


def data_dir() -> Path:
    return root() / "data"


def sidecar_path() -> Path:
    return data_dir() / "sidecar.db"


# --------------------------------------------------------------------------- config

_config_cache: dict[Path, dict] = {}


def config_path() -> Path:
    cfg_dir = root() / "config"
    real = cfg_dir / "config.toml"
    return real if real.exists() else cfg_dir / "config.example.toml"


def load_config() -> dict:
    path = config_path()
    if path not in _config_cache:
        if not path.exists():
            raise PFError(f"no config found at {path}")
        if path.name == "config.example.toml":
            log.warning("using config.example.toml - copy it to config/config.toml and edit it")
        with path.open("rb") as fh:
            _config_cache[path] = tomllib.load(fh)
    return _config_cache[path]


def entity_label(entity: str, cfg: dict | None = None) -> str:
    cfg = cfg or load_config()
    try:
        return cfg["entities"][entity]["label"]
    except KeyError:
        raise PFError(f"unknown entity {entity!r}; known: {', '.join(cfg.get('entities', {}))}")


def entity_ledger(entity: str, cfg: dict | None = None) -> Path:
    cfg = cfg or load_config()
    return ledger_dir() / cfg["entities"][entity].get("ledger", f"{entity}.beancount")


def entity_of_account(account: str, cfg: dict | None = None) -> str | None:
    """Assets:Mom:Savings -> 'mom' (by matching the second segment to an entity label)."""
    cfg = cfg or load_config()
    parts = account.split(":")
    if len(parts) < 2:
        return None
    for key, ent in cfg.get("entities", {}).items():
        if ent.get("label") == parts[1]:
            return key
    return None


def fill_entity(template: str, entity: str, cfg: dict | None = None) -> str:
    return template.replace("{entity}", entity_label(entity, cfg))


def sources(cfg: dict | None = None, kind: str | None = None) -> dict[str, dict]:
    cfg = cfg or load_config()
    out = {}
    for sid, src in cfg.get("sources", {}).items():
        if kind is None or src.get("kind") == kind:
            out[sid] = {"id": sid, **src}
    return out


# --------------------------------------------------------------------------- secrets

def _env_name(key: str) -> str:
    return "PF_SECRET_" + re.sub(r"[^A-Za-z0-9]", "_", key).upper()


def secret(key: str) -> str | None:
    """Read a secret: PF_SECRET_<KEY> from the environment first, then the Keychain.

    Returns None when absent, so callers can explain which secret is missing.
    """
    env = os.environ.get(_env_name(key))
    if env:
        return env
    try:
        import keyring
        return keyring.get_password(KEYCHAIN_SERVICE, key)
    except Exception as ex:  # no keyring backend (e.g. CI) is not fatal
        log.debug("keyring unavailable for %s: %s", key, ex)
        return None


def set_secret(key: str, value: str) -> None:
    import keyring
    keyring.set_password(KEYCHAIN_SERVICE, key, value)


# --------------------------------------------------------------------------- parsing helpers

_AMOUNT_JUNK = re.compile(r"[₹,\s]|Rs\.?|INR", re.IGNORECASE)


def to_decimal(value: Any) -> Decimal:
    """'₹1,23,456.50' / 1234.5 / '1234' -> Decimal. Raises ValueError on garbage."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if value is None:
        raise ValueError("amount is missing")
    text = _AMOUNT_JUNK.sub("", str(value))
    try:
        return Decimal(text)
    except InvalidOperation:
        raise ValueError(f"not an amount: {value!r}")


def money(d: Decimal) -> Decimal:
    return d.quantize(Decimal("0.01"))


def parse_date(value: str, formats: list[str] | None = None) -> date:
    formats = formats or ["%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d-%m-%y",
                          "%d %b %Y", "%d %b %y", "%d-%b-%Y", "%d-%b-%y", "%d.%m.%Y"]
    text = str(value).strip()
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognised date {value!r}")


# --------------------------------------------------------------------------- JSON from LLMs

def extract_json(text: str) -> Any:
    """Pull one JSON value out of model output.

    Handles the usual failure modes: ```json fences, prose before/after the object,
    and trailing commas. Raises ValueError (with a reason) when nothing parses.
    """
    if text is None:
        raise ValueError("empty reply")
    text = text.strip()
    if not text:
        raise ValueError("empty reply")
    attempts = [text]
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        attempts.append(fenced.group(1).strip())
    balanced = _first_balanced(text)
    if balanced:
        attempts.append(balanced)
    last_err = None
    for candidate in attempts:
        for variant in (candidate, re.sub(r",\s*([}\]])", r"\1", candidate)):
            try:
                return json.loads(variant)
            except json.JSONDecodeError as ex:
                last_err = ex
    raise ValueError(f"no valid JSON in reply ({last_err})")


def _first_balanced(text: str) -> str | None:
    """The first {...} or [...] span with balanced brackets, respecting strings."""
    start = next((i for i, ch in enumerate(text) if ch in "{["), None)
    if start is None:
        return None
    stack, in_str, esc = [], False, False
    pairs = {"{": "}", "[": "]"}
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in pairs:
            stack.append(pairs[ch])
        elif stack and ch == stack[-1]:
            stack.pop()
            if not stack:
                return text[start:i + 1]
    return None


# --------------------------------------------------------------------------- Ollama

class OllamaError(PFError):
    pass


def ollama_settings(cfg: dict | None = None) -> dict:
    cfg = cfg or load_config()
    o = dict(cfg.get("ollama", {}))
    o["host"] = (os.environ.get("OLLAMA_HOST") or o.get("host") or "http://localhost:11434").rstrip("/")
    if not o["host"].startswith("http"):
        o["host"] = "http://" + o["host"]
    o["api_key"] = os.environ.get("OLLAMA_API_KEY") or secret("ollama.api_key")
    return o


def ollama_chat(messages: list[dict], model: str, *, fmt: Any = None,
                cfg: dict | None = None, timeout: float | None = None) -> str:
    """One non-streaming /api/chat call. Returns the assistant message text."""
    import requests

    o = ollama_settings(cfg)
    headers = {"Content-Type": "application/json"}
    if o["api_key"]:
        headers["Authorization"] = f"Bearer {o['api_key']}"
    body = {"model": model, "messages": messages, "stream": False,
            "options": {"temperature": 0}}
    if fmt is not None:
        body["format"] = fmt
    url = f"{o['host']}/api/chat"
    try:
        resp = requests.post(url, headers=headers, json=body,
                             timeout=timeout or o.get("timeout_seconds", 300))
    except requests.ConnectionError:
        raise OllamaError(f"Ollama is not reachable at {o['host']} - start it with `ollama serve` "
                          "or set OLLAMA_HOST / [ollama].host")
    except requests.Timeout:
        raise OllamaError(f"Ollama at {o['host']} did not answer within the timeout")
    if resp.status_code == 404:
        raise OllamaError(f"model {model!r} not found on {o['host']} - run `ollama pull {model}`")
    if resp.status_code in (401, 403):
        raise OllamaError(f"Ollama at {o['host']} refused the request ({resp.status_code}); "
                          "check OLLAMA_API_KEY")
    if not resp.ok:
        raise OllamaError(f"Ollama error {resp.status_code}: {resp.text[:300]}")
    try:
        return resp.json()["message"]["content"]
    except (ValueError, KeyError) as ex:
        raise OllamaError(f"unexpected Ollama response shape: {ex}")


def ollama_json(messages: list[dict], model: str, *, schema: dict | None = None,
                validate: Callable[[Any], list[str]] | None = None,
                retries: int | None = None, cfg: dict | None = None,
                chat: Callable[..., str] | None = None) -> tuple[Any, list[str]]:
    """Ask for JSON, parse it robustly, validate it, and re-ask with the problem on failure.

    Returns (value, problems). `problems` is empty on a clean answer; when retries run
    out on a value that parses but still fails validation, the last value is returned
    with its problems so the caller can decide to keep it flagged. A reply that never
    parses raises OllamaError.
    `chat` replaces ollama_chat (tests inject a fake model here).
    """
    cfg = cfg or load_config()
    chat = chat or ollama_chat
    retries = cfg.get("ollama", {}).get("max_retries", 2) if retries is None else retries
    convo = list(messages)
    last_value, last_problems = None, ["no answer"]
    for attempt in range(retries + 1):
        reply = chat(convo, model, fmt=schema or "json", cfg=cfg)
        try:
            value = extract_json(reply)
        except ValueError as ex:
            problems = [str(ex)]
            value = None
        else:
            problems = validate(value) if validate else []
            last_value, last_problems = value, problems
            if not problems:
                return value, []
        log.warning("model reply rejected (attempt %d/%d): %s", attempt + 1, retries + 1,
                    "; ".join(problems))
        convo = convo + [
            {"role": "assistant", "content": reply or ""},
            {"role": "user", "content": "That reply had problems: " + "; ".join(problems)
             + ". Reply again with ONLY the corrected JSON object, no prose."},
        ]
    if last_value is None:
        raise OllamaError("model never returned parseable JSON: " + "; ".join(last_problems))
    return last_value, last_problems


def image_b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


# --------------------------------------------------------------------------- logging / CLI

def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(levelname)s %(message)s")


def run_cli(main: Callable[[], int]) -> None:
    """Turn PFError into a clean one-line message and a non-zero exit."""
    try:
        sys.exit(main())
    except PFError as ex:
        print(f"error: {ex}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        sys.exit(130)


def _secrets_cli() -> int:
    p = argparse.ArgumentParser(description="Manage Keychain secrets (service personal-finance)")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("set", help="store a secret, e.g. self.name / self.dob / mom.customer_id")
    s.add_argument("key")
    sub.add_parser("status", help="show which secrets are present (never their values)")
    args = p.parse_args(sys.argv[2:])
    if args.cmd == "set":
        value = getpass.getpass(f"{args.key}: ")
        if args.key.endswith(".dob"):
            parse_date(value)  # refuse a DOB we cannot read later
        set_secret(args.key, value.strip())
        print(f"stored {args.key}")
    else:
        cfg = load_config()
        keys = [f"{e}.{k}" for e in cfg.get("entities", {}) for k in SECRET_KEYS] + ["ollama.api_key"]
        for key in keys:
            print(f"{key:22} {'present' if secret(key) else '-'}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "secret":
        setup_logging()
        run_cli(_secrets_cli)
    else:
        print(__doc__)
