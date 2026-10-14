#!/usr/bin/env python3
"""Backfill stable source-body markers on pages migrated to inline claims.

The marker lets the validator distinguish a governed page-body change from a
machine-managed annotation change.  Existing pages are checked against the
pre-migration rollback copy; pages created by the migration are reconstructed
by removing only the generated claim section.
"""

from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import business_page_router as router


BRIDGE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_BACKUP = Path(
    _archive_path('/Users/example/AI/LocalData/BUSINESS-NonObsidian.nosync/business-routing/migrations/20260829T112043+0800/backup/vault')
)


def require_owned_maintenance() -> None:
    completed = subprocess.run(
        [sys.executable, str(BRIDGE_ROOT / "bin" / "maintenance_mode.py"), "status"],
        cwd=BRIDGE_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    status = json.loads(completed.stdout)
    reason = str(status.get("reason") or "")
    if status.get("status") != "active" or not reason.startswith("info-routing-"):
        raise RuntimeError("claim-base backfill requires the owned information-routing maintenance window")
    if status.get("writer_processes") or status.get("unregistered_writer_processes"):
        raise RuntimeError("a writer process is active during claim-base backfill")
    if any(int(value) for value in dict(status.get("queues") or {}).values()):
        raise RuntimeError("an ingestion queue is not empty during claim-base backfill")


def strip_claims(text: str) -> str:
    cleaned = text
    spans = [span for _payload, span in router.iter_claims(text)]
    for start, end in reversed(spans):
        cleaned = cleaned[:start] + cleaned[end:]
    return cleaned


def migrated_new_page_base(text: str) -> str:
    cleaned = strip_claims(text).rstrip()
    heading = re.search(r"(?:\n{1,2})## 核验说明\s*\Z", cleaned)
    if heading is None:
        raise RuntimeError("new migration page does not end with the generated 核验说明 heading")
    return cleaned[: heading.start()].rstrip() + "\n"


def build_marked_text(current: str, base: str) -> str:
    if router.claim_base_state(current) is not None:
        if router.business_base_text(current) != base:
            raise RuntimeError("existing claim-base marker does not match the expected base")
        return current
    heading = re.search(r"^## 核验说明\s*$", current, re.MULTILINE)
    if heading is None:
        raise RuntimeError("claim page has no 核验说明 heading")
    created_heading = not bool(re.search(r"^## 核验说明\s*$", base, re.MULTILINE))
    payload = {
        "schema_version": 1,
        "base_sha256": router.sha256_text(base),
        "base_trailing": router._trailing_whitespace(base),
        "created_heading": created_heading,
        "annotation_hashes": [router.sha256_text(current)],
    }
    marked = current[: heading.start()] + router._base_marker(payload) + "\n" + current[heading.start() :]
    if router.business_base_text(marked) != base:
        raise RuntimeError("claim-base reconstruction does not match the pre-migration page")
    return marked


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill stable source-body markers on inline-claim pages")
    parser.add_argument("--config", type=Path, default=BRIDGE_ROOT / "config.json")
    parser.add_argument("--source-backup", type=Path, default=DEFAULT_SOURCE_BACKUP)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    require_owned_maintenance()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    vault_root = router.vault_root_from_config(config)
    external_root = Path(str(config["external_root"])).expanduser().resolve()
    pages = sorted({Path(item["path"]).resolve() for item in router.scan_claims(vault_root)})
    changes: list[tuple[Path, str, str]] = []

    for page in pages:
        relative = page.relative_to(vault_root)
        current = page.read_text(encoding="utf-8")
        backup = args.source_backup.resolve() / relative
        if backup.is_file():
            base = backup.read_text(encoding="utf-8")
        elif router.claim_base_state(current) is not None:
            base = router.business_base_text(current)
        else:
            base = migrated_new_page_base(current)
        marked = build_marked_text(current, base)
        if marked != current:
            changes.append((page, current, marked))

    result = {
        "schema_version": 1,
        "mode": "apply" if args.apply else "dry_run",
        "claim_pages": len(pages),
        "changed_pages": len(changes),
        "unchanged_pages": len(pages) - len(changes),
    }
    if not args.apply:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0

    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")
    run_root = external_root / "business-routing" / "claim-base-backfills" / stamp
    backup_root = run_root / "backup"
    manifest: list[dict[str, str]] = []
    for page, current, marked in changes:
        relative = page.relative_to(vault_root)
        if router.sha256_file(page) != router.sha256_text(current):
            raise RuntimeError(f"page changed during claim-base backfill: {relative.as_posix()}")
        destination = backup_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(page, destination)
        router.atomic_write(page, marked)
        manifest.append(
            {
                "relative_path": relative.as_posix(),
                "before_sha256": router.sha256_text(current),
                "after_sha256": router.sha256_text(marked),
                "base_sha256": router.sha256_text(router.business_base_text(marked)),
            }
        )
    run_root.mkdir(parents=True, exist_ok=True)
    router.atomic_write(
        run_root / "manifest.json",
        json.dumps({**result, "run_root": str(run_root), "pages": manifest}, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        0o600,
    )
    result["run_root"] = str(run_root)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
