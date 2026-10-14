#!/usr/bin/env python3
"""Offline contract tests for the shared business-page integrity module."""

from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import tempfile
import unittest
from pathlib import Path
from unittest import mock

import business_page_integrity as integrity


def claim_block(claim_id: str, body: str = "待核说明") -> str:
    payload = {
        "schema_version": 2,
        "claim_id": claim_id,
    }
    return (
        f"<!-- business-claim:v2 {integrity.encode_payload(payload)} -->\n"
        f"> [!warning] 来源待核｜{body}\n"
        f"<!-- /business-claim:{claim_id} -->"
    )


class BusinessPageIntegrityTests(unittest.TestCase):
    def test_plain_page_without_claims_does_not_require_a_marker(self) -> None:
        result = integrity.inspect_page("普通业务正文。\n")
        self.assertTrue(result.valid)
        self.assertFalse(result.marker_present)

    def test_marker_reconstructs_unicode_crlf_and_trailing_bytes(self) -> None:
        original = "ASCII 中文 😀 🇪🇪\r\n\r\n## 核验说明\r\n  \r\n"
        marked = integrity.ensure_claim_base(original)
        result = integrity.inspect_page(marked)
        self.assertTrue(result.valid, result.errors)
        self.assertEqual(integrity.business_base_text(marked), original)

    def test_existing_body_drift_fails_closed(self) -> None:
        marked = integrity.ensure_claim_base("原始正文。\n")
        drifted = marked.replace("原始正文", "绕过写入的正文")
        self.assertEqual(
            integrity.inspect_page(drifted).errors,
            ("claim_base_mismatch",),
        )
        with self.assertRaisesRegex(integrity.IntegrityError, "integrity"):
            integrity.ensure_claim_base(drifted)

    def test_untracked_text_after_generated_heading_fails_closed(self) -> None:
        marked = integrity.ensure_claim_base("受保护正文。\n")
        drifted = marked.rstrip() + "\n\n绕过写入的正文。\n"
        self.assertEqual(
            integrity.inspect_page(drifted).errors,
            ("claim_base_untracked_suffix",),
        )
        with self.assertRaises(integrity.IntegrityError) as raised:
            integrity.prepare_base_update(marked, drifted, allow_base_change=True)
        self.assertEqual(raised.exception.code, "claim_base_untracked_suffix")

    def test_legacy_external_asset_region_is_explicitly_bounded(self) -> None:
        marked = integrity.ensure_claim_base("受保护正文。\n")
        managed = (
            marked.rstrip()
            + "\n\n"
            + integrity.CLIENT_ALPHA_EXTERNAL_ASSETS_START
            + "\n## 外置客户原件\n\n- 受治理链接\n"
            + integrity.CLIENT_ALPHA_EXTERNAL_ASSETS_END
            + "\n"
        )
        self.assertTrue(integrity.inspect_page(managed).valid)
        appended = managed + "未受保护的尾部正文。\n"
        self.assertEqual(
            integrity.inspect_page(appended).errors,
            ("claim_base_untracked_suffix",),
        )
        incomplete = managed.replace(integrity.CLIENT_ALPHA_EXTERNAL_ASSETS_END, "")
        self.assertEqual(
            integrity.inspect_page(incomplete).errors,
            ("claim_base_untracked_suffix",),
        )

    def test_existing_heading_suffix_remains_part_of_the_base(self) -> None:
        marked = integrity.ensure_claim_base("正文。\n\n## 核验说明\n")
        self.assertTrue(integrity.inspect_page(marked).valid)
        drifted = marked + "绕过写入的正文。\n"
        self.assertEqual(
            integrity.inspect_page(drifted).errors,
            ("claim_base_mismatch",),
        )

    def test_authorized_body_update_refreshes_only_the_base_generation(self) -> None:
        old = integrity.ensure_claim_base("原始正文。\n")
        block = claim_block("BUSINESS-20260904-001")
        old = old.rstrip() + "\n\n" + block + "\n"
        self.assertTrue(integrity.inspect_page(old).valid)
        candidate = old.replace("原始正文", "已授权的新正文")
        prepared = integrity.prepare_base_update(
            old, candidate, allow_base_change=True
        )
        result = integrity.inspect_page(prepared)
        self.assertTrue(result.valid, result.errors)
        self.assertEqual(integrity.claim_blocks(prepared), integrity.claim_blocks(old))
        state = integrity.claim_base_state(prepared)
        assert state is not None
        self.assertEqual(state[0]["annotation_hashes"], [integrity.sha256_text(old)])

    def test_authorized_update_rejects_claim_or_marker_tampering(self) -> None:
        old = integrity.ensure_claim_base("原始正文。\n")
        old = old.rstrip() + "\n\n" + claim_block("BUSINESS-20260904-002") + "\n"
        with self.assertRaisesRegex(integrity.IntegrityError, "claim annotations"):
            integrity.prepare_base_update(
                old,
                old.replace("待核说明", "被改写的说明"),
                allow_base_change=True,
            )
        state = integrity.claim_base_state(old)
        assert state is not None
        tampered_payload = dict(state[0])
        tampered_payload["annotation_hashes"] = ["0" * 64]
        tampered = (
            old[: state[1][0]]
            + integrity.base_marker(tampered_payload)
            + old[state[1][1] :]
        )
        with self.assertRaisesRegex(integrity.IntegrityError, "hidden"):
            integrity.prepare_base_update(old, tampered, allow_base_change=True)

    def test_rebase_changes_no_content_outside_the_hidden_marker(self) -> None:
        marked = integrity.ensure_claim_base("原始正文。\n")
        drifted = marked.replace("原始正文", "已核对并接受的正文")
        old_state = integrity.claim_base_state(drifted)
        assert old_state is not None
        rebased = integrity.rebase_text(
            drifted,
            expected_old_base_sha256=old_state[0]["base_sha256"],
        )
        self.assertTrue(integrity.inspect_page(rebased).valid)
        self.assertEqual(
            integrity.without_base_marker(rebased),
            integrity.without_base_marker(drifted),
        )
        with self.assertRaisesRegex(integrity.IntegrityError, "stored"):
            integrity.rebase_text(
                drifted,
                expected_old_base_sha256="0" * 64,
            )

    def test_malformed_and_multiple_markers_fail_closed(self) -> None:
        malformed = "正文\n<!-- business-claim-base:v1 not+urlsafe -->\n"
        self.assertFalse(integrity.inspect_page(malformed).valid)
        marked = integrity.ensure_claim_base("正文\n")
        state = integrity.claim_base_state(marked)
        assert state is not None
        doubled = marked + "\n" + marked[state[1][0] : state[1][1]]
        self.assertEqual(
            integrity.inspect_page(doubled).errors,
            ("claim_base_marker_multiple",),
        )

    def test_file_rebase_is_cas_bound_and_writes_a_private_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = root / "vault"
            page = vault / "客户及展会/Acme/项目说明.md"
            page.parent.mkdir(parents=True)
            marked = integrity.ensure_claim_base("原始正文。\n")
            drifted = marked.replace("原始正文", "已授权正文")
            with page.open("w", encoding="utf-8", newline="") as handle:
                handle.write(drifted)
            state = integrity.claim_base_state(drifted)
            assert state is not None
            with mock.patch.object(
                integrity,
                "_require_owned_maintenance",
                return_value={"status": "active"},
            ):
                result = integrity.rebase_page(
                    vault_root=vault,
                    relative_page="客户及展会/Acme/项目说明.md",
                    expected_file_sha256=integrity.sha256_file(page),
                    expected_old_base_sha256=state[0]["base_sha256"],
                    receipt_root=root / "receipts",
                )
            self.assertEqual(result["status"], "rebased")
            self.assertTrue(result["visible_content_equivalent"])
            receipt = Path(result["receipt_path"])
            self.assertTrue(receipt.is_file())
            self.assertEqual(receipt.stat().st_mode & 0o077, 0)
            with mock.patch.object(
                integrity,
                "_require_owned_maintenance",
                return_value={"status": "active"},
            ), self.assertRaisesRegex(integrity.IntegrityError, "changed"):
                integrity.rebase_page(
                    vault_root=vault,
                    relative_page="客户及展会/Acme/项目说明.md",
                    expected_file_sha256="0" * 64,
                    expected_old_base_sha256=state[0]["base_sha256"],
                    receipt_root=root / "receipts-2",
                )


if __name__ == "__main__":
    unittest.main()
