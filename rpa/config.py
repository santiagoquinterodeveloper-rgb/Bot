"""Carga de configuración (YAML) con acceso por ruta punteada."""
from __future__ import annotations

import copy
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class DocType:
    key: str
    listbox: str
    prefijo: str
    archivo: str
    folio_dialog: bool = False
    obligatorio: bool = False


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    def __init__(self, raw: dict, base_dir: Path):
        self.raw = raw
        self.base_dir = Path(base_dir)
        self.docs: list[DocType] = [DocType(**d) for d in raw["documentos"]]

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def path(self, dotted: str) -> Path:
        value = self.get(dotted)
        if value in (None, ""):
            raise KeyError(f"Falta {dotted} en la configuración")
        p = Path(os.path.expandvars(str(value)))
        return p if p.is_absolute() else self.base_dir / p

    def doc_by_key(self, key: str) -> DocType:
        for d in self.docs:
            if d.key == key:
                return d
        raise KeyError(key)

    def prefix_map(self) -> dict[str, str]:
        return {d.prefijo: d.key for d in self.docs}


def load_config(path: str | Path = "config.yaml", overrides: dict | None = None) -> Config:
    path = Path(path)
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if overrides:
        raw = _deep_merge(raw, overrides)
    return Config(raw, path.resolve().parent)
