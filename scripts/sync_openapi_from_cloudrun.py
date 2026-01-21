#!/usr/bin/env python3
import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml


DEFAULT_BASE_URL = os.environ.get(
    "CLOUD_RUN_SERVICE_URL",
    "https://classnote-api-900324644592.asia-northeast1.run.app",
)
DEFAULT_OPENAPI_PATH = "/openapi.json"
DEFAULT_JSON_OUT = Path("openapi.cloudrun.json")
DEFAULT_YAML_OUT = Path("openapi.yaml")
DEFAULT_CHECKLIST_OUT = Path("docs/api-endpoint-checklist.md")

METHOD_ORDER = ["get", "post", "put", "patch", "delete", "options", "head", "trace"]

TITLE = "# API Endpoint \u30c1\u30a7\u30c3\u30af\u30ea\u30b9\u30c8\uff08OpenAPI \u81ea\u52d5\u751f\u6210\uff09"
NOTE = (
    "\u3053\u306e\u30d5\u30a1\u30a4\u30eb\u306f Cloud Run \u672c\u756a\u306e OpenAPI \u304b\u3089\u751f\u6210\u3055\u308c\u307e\u3059\u3002"
    "\u624b\u7de8\u96c6\u3057\u306a\u3044\u3067\u304f\u3060\u3055\u3044\u3002"
)


def _openapi_url(base_url: str, openapi_path: str) -> str:
    return f"{base_url.rstrip('/')}{openapi_path}"


def _fetch_openapi(url: str, timeout_sec: int = 30) -> bytes:
    resp = requests.get(url, headers={"Accept": "application/json"}, timeout=timeout_sec)
    resp.raise_for_status()
    return resp.content


def _write_json(path: Path, payload: bytes) -> None:
    path.write_bytes(payload)


def _write_yaml(path: Path, obj: dict) -> None:
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(obj, f, sort_keys=False, allow_unicode=True)


def _method_sort_key(method: str) -> tuple:
    try:
        idx = METHOD_ORDER.index(method)
    except ValueError:
        idx = len(METHOD_ORDER)
    return (idx, method)


def _tag_sort_key(tag: str) -> tuple:
    return (tag == "Untagged", tag.lower())


def _generate_checklist(spec: dict, out_path: Path, source_url: str) -> None:
    tag_map: dict[str, list[tuple[str, str]]] = {}
    paths = spec.get("paths", {})

    for path, methods in paths.items():
        for method, op in methods.items():
            if method.startswith("x-"):
                continue
            tags = op.get("tags") or ["Untagged"]
            seen = set()
            uniq_tags = []
            for tag in tags:
                if tag in seen:
                    continue
                seen.add(tag)
                uniq_tags.append(tag)
            tag = uniq_tags[0] if uniq_tags else "Untagged"
            tag_map.setdefault(tag, []).append((path, method))

    generated_at = datetime.now(timezone.utc).isoformat()
    total_paths = len(paths)
    total_ops = sum(len(m) for m in tag_map.values())

    lines = [
        TITLE,
        "",
        NOTE,
        f"Source: {source_url}",
        f"Generated: {generated_at}",
        f"Paths: {total_paths}, Operations: {total_ops}",
        "",
    ]

    for tag in sorted(tag_map.keys(), key=_tag_sort_key):
        lines.append(f"## {tag}")
        entries = sorted(tag_map[tag], key=lambda x: (x[0], _method_sort_key(x[1])))
        for path, method in entries:
            lines.append(f"- [ ] {method.upper()} {path}")
        lines.append("")

    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync OpenAPI from Cloud Run and regenerate checklist.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Cloud Run base URL.")
    parser.add_argument("--openapi-path", default=DEFAULT_OPENAPI_PATH, help="OpenAPI path.")
    parser.add_argument("--json-out", default=str(DEFAULT_JSON_OUT), help="Output JSON path.")
    parser.add_argument("--yaml-out", default=str(DEFAULT_YAML_OUT), help="Output YAML path.")
    parser.add_argument("--checklist-out", default=str(DEFAULT_CHECKLIST_OUT), help="Checklist output path.")
    args = parser.parse_args()

    openapi_url = _openapi_url(args.base_url, args.openapi_path)
    payload = _fetch_openapi(openapi_url)

    json_path = Path(args.json_out)
    yaml_path = Path(args.yaml_out)
    checklist_path = Path(args.checklist_out)

    _write_json(json_path, payload)
    spec = json.loads(payload.decode("utf-8"))
    _write_yaml(yaml_path, spec)
    _generate_checklist(spec, checklist_path, openapi_url)

    print(f"Saved: {json_path}")
    print(f"Saved: {yaml_path}")
    print(f"Saved: {checklist_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
