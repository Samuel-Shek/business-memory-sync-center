#!/usr/bin/env python3
"""Offline end-to-end tests using only synthetic WeChat-like databases."""

from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import base64
import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
import wave
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

from collect_wechat_chat import (
    readonly_file_signature,
    scope_expansion_guard,
    snapshot_encrypted_database,
    stable_readonly_copy,
)
from wechat_common import WeChatSyncError, group_matches, render_message_content


os.umask(0o077)
ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
WORKSPACE_PYTHON = Path(_archive_path('/Users/example/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3'))


def tagged_label(label_id: int) -> bytes:
    payload = str(label_id).encode("utf-8")
    return b"\xf2\x01" + bytes([len(payload)]) + payload


def md5_name(value: str) -> str:
    return hashlib.md5(value.encode("utf-8")).hexdigest()


def create_contact(root: Path, contacts: list[tuple[str, str, bytes]]) -> None:
    path = root / "contact" / "contact.db"
    path.parent.mkdir(parents=True)
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE contact_label(label_id_ INTEGER,label_name_ TEXT,sort_order_ INTEGER);
        CREATE TABLE contact(username TEXT,nick_name TEXT,remark TEXT,extra_buffer BLOB,local_type INTEGER);
        INSERT INTO contact_label VALUES(7,'示例企业',1);
        """
    )
    connection.executemany("INSERT INTO contact VALUES(?,?,?,?,1)", [(u, n, n, b) for u, n, b in contacts])
    connection.commit()
    connection.close()


def create_messages(root: Path, conversations: dict[str, list[tuple]]) -> None:
    path = root / "message" / "message_0.db"
    path.parent.mkdir(parents=True)
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE Name2Id(user_name TEXT)")
    for username in conversations:
        connection.execute("INSERT INTO Name2Id(user_name) VALUES(?)", (username,))
        table = "Msg_" + md5_name(username)
        connection.execute(
            f"CREATE TABLE [{table}](local_id INTEGER,local_type INTEGER,create_time INTEGER,real_sender_id INTEGER,message_content TEXT,server_id INTEGER,WCDB_CT_message_content BLOB)"
        )
        connection.executemany(f"INSERT INTO [{table}] VALUES(?,?,?,?,?,?,?)", conversations[username])
    connection.commit()
    connection.close()


def create_resource(root: Path, username: str, local_id: int, created: int, digest: str) -> None:
    connection = sqlite3.connect(root / "message" / "message_resource.db")
    connection.execute("CREATE TABLE ChatName2Id(user_name TEXT)")
    connection.execute(
        "CREATE TABLE MessageResourceInfo(chat_id INTEGER,message_local_id INTEGER,message_local_type INTEGER,message_create_time INTEGER,packed_info BLOB)"
    )
    cursor = connection.execute("INSERT INTO ChatName2Id(user_name) VALUES(?)", (username,))
    packed = b"\x12\x22\x0a\x20" + digest.encode("ascii")
    connection.execute("INSERT INTO MessageResourceInfo VALUES(?,?,?,?,?)", (cursor.lastrowid, local_id, 3, created, packed))
    connection.commit()
    connection.close()


def create_voice(root: Path, username: str, local_id: int, created: int, wav: bytes) -> None:
    connection = sqlite3.connect(root / "message" / "media_0.db")
    connection.execute("CREATE TABLE Name2Id(user_name TEXT)")
    connection.execute("CREATE TABLE VoiceInfo(chat_name_id INTEGER,local_id INTEGER,voice_data BLOB,create_time INTEGER)")
    cursor = connection.execute("INSERT INTO Name2Id(user_name) VALUES(?)", (username,))
    connection.execute("INSERT INTO VoiceInfo VALUES(?,?,?,?)", (cursor.lastrowid, local_id, wav, created))
    connection.commit()
    connection.close()


def wav_silence() -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".wav") as handle:
        with wave.open(handle.name, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(b"\x00\x00" * 3200)
        return Path(handle.name).read_bytes()


def write_media(account: Path, username: str, created: int, digests: dict[str, str]) -> None:
    month = datetime.fromtimestamp(created).astimezone().strftime("%Y-%m")
    chat_hash = md5_name(username)
    image_root = account / "msg" / "attach" / chat_hash / month / "Img"
    file_root = account / "msg" / "file" / chat_hash / month
    video_root = account / "msg" / "video" / chat_hash / month
    image_root.mkdir(parents=True)
    file_root.mkdir(parents=True)
    video_root.mkdir(parents=True)
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Z9ZsAAAAASUVORK5CYII=")
    (image_root / f"{digests['image']}.png").write_bytes(png)
    (image_root / f"{digests['emoji']}.png").write_bytes(png)
    create_pdf = subprocess.run(
        [str(WORKSPACE_PYTHON), "-c", "from pypdf import PdfWriter; import sys; w=PdfWriter(); w.add_blank_page(width=72,height=72); w.write(sys.argv[1])", str(file_root / f"{digests['document']}.pdf")],
        capture_output=True,
        check=False,
        timeout=60,
    )
    if create_pdf.returncode != 0:
        raise RuntimeError("synthetic_pdf_failed")
    video = subprocess.run(
        ["/opt/homebrew/bin/ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.5", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-shortest", "-t", "0.5", "-pix_fmt", "yuv420p", str(video_root / f"{digests['video']}.mp4")],
        capture_output=True,
        check=False,
        timeout=120,
    )
    if video.returncode != 0:
        raise RuntimeError("synthetic_video_failed")


def run_json(command: list[str], accepted: set[int]) -> dict:
    result = subprocess.run(command, text=True, capture_output=True, check=False, timeout=3600)
    if result.returncode not in accepted:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise AssertionError(f"unexpected_exit:{result.returncode}:{detail}")
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        raise AssertionError("missing_json_receipt")
    return json.loads(lines[-1])


def main() -> int:
    secret = "synthetic-secret-X7!"
    direct = "example_616cbe4937ca"
    group = "synthetic_business@chatroom"
    excluded = "synthetic_lithion@chatroom"
    created = int((datetime.now().astimezone() - timedelta(days=1)).timestamp())
    digests = {
        "image": "1" * 32,
        "emoji": "2" * 32,
        "document": "3" * 32,
        "video": "4" * 32,
    }
    checks = 0
    reply_xml = (
        "<msg><appmsg><type>57</type><title>收到</title><refermsg>"
        "<svrid>123456789</svrid><displayname>合成人员</displayname>"
        "<content>密码: synthetic-reply-secret</content></refermsg>"
        "<url>https://example.invalid/tracker</url></appmsg></msg>"
    )
    _sender, reply_text, reply_metadata = render_message_content(49, reply_xml, False)
    assert reply_text.startswith("[回复]")
    assert "synthetic-reply-secret" not in json.dumps(reply_metadata, ensure_ascii=False)
    assert "quoted_message_id_sha256" in reply_metadata and reply_metadata["redaction_count"] > 0
    assert group_matches(" LITH - TEC 合成工作群", ["LITH-TEC"], ["Lithion"])
    assert not group_matches("Lithion 合成客户群", ["BUSINESS", "示例企业"], ["Lithion"])
    assert not group_matches("LTC_ENERGY", ["LTC-ENERGY"], ["Lithion"])
    checks += 1
    with tempfile.TemporaryDirectory(prefix="business-wechat-selftest-") as temporary:
        temp = Path(temporary)
        fake_key = bytes(range(32))
        fake_salt = bytes(range(160, 176))
        encrypted = temp / "synthetic-encrypted.db"
        decrypted = temp / "synthetic-snapshot.db"
        sql = "\n".join(
            (
                f'PRAGMA key = "x\'{fake_key.hex()}{fake_salt.hex()}\'";',
                "PRAGMA cipher_page_size = 4096;",
                "PRAGMA cipher_hmac_algorithm = HMAC_SHA512;",
                "PRAGMA cipher_kdf_algorithm = PBKDF2_HMAC_SHA512;",
                "CREATE TABLE sample(value TEXT);",
                "INSERT INTO sample VALUES('synthetic-only');",
            )
        )
        created_cipher = subprocess.run(
            ["/opt/homebrew/bin/sqlcipher", "-batch", "-bail", str(encrypted)],
            input=sql.encode("utf-8"), capture_output=True, check=False, timeout=120,
            env={"PATH": "/usr/bin:/bin:/opt/homebrew/bin", "LC_ALL": "C"},
        )
        assert created_cipher.returncode == 0 and encrypted.is_file()
        encrypted_copy = temp / "synthetic-encrypted-copy.db"
        stable_readonly_copy(encrypted, encrypted_copy)
        snapshot_encrypted_database(
            encrypted_copy, decrypted, fake_key, fake_salt.hex(), Path("/opt/homebrew/bin/sqlcipher")
        )
        checked = sqlite3.connect(decrypted)
        assert checked.execute("SELECT value FROM sample").fetchone()[0] == "synthetic-only"
        checked.close()
        assert fake_key.hex().encode("ascii") not in encrypted.read_bytes()
        checks += 1

        wal_source = temp / "wal-source.db"
        wal_copy = temp / "wal-copy.db"
        wal_connection = sqlite3.connect(wal_source)
        wal_connection.execute("PRAGMA journal_mode=WAL")
        wal_connection.execute("PRAGMA wal_autocheckpoint=0")
        wal_connection.execute("CREATE TABLE sample(value TEXT)")
        wal_connection.execute("INSERT INTO sample VALUES('wal-visible')")
        wal_connection.commit()
        source_wal = Path(str(wal_source) + "-wal")
        before_main = readonly_file_signature(wal_source)
        before_wal = readonly_file_signature(source_wal)
        stable_readonly_copy(wal_source, wal_copy)
        assert readonly_file_signature(wal_source) == before_main
        assert readonly_file_signature(source_wal) == before_wal
        copied = sqlite3.connect(wal_copy)
        assert copied.execute("SELECT value FROM sample").fetchone()[0] == "wal-visible"
        copied.close()
        wal_connection.close()
        checks += 1

        documents = temp / "documents"
        documents.mkdir()
        create_documents = subprocess.run(
            [
                str(WORKSPACE_PYTHON),
                "-c",
                "from docx import Document; from openpyxl import Workbook; from pptx import Presentation; import sys; "
                "d=Document(); d.add_paragraph('synthetic word text'); d.save(sys.argv[1]); "
                "w=Workbook(); w.active['A1']='synthetic excel text'; w.save(sys.argv[2]); "
                "p=Presentation(); s=p.slides.add_slide(p.slide_layouts[5]); s.shapes.title.text='synthetic powerpoint text'; p.save(sys.argv[3])",
                str(documents / "sample.docx"),
                str(documents / "sample.xlsx"),
                str(documents / "sample.pptx"),
            ],
            capture_output=True,
            check=False,
            timeout=120,
        )
        assert create_documents.returncode == 0
        for filename, expected in (
            ("sample.docx", "synthetic word text"),
            ("sample.xlsx", "synthetic excel text"),
            ("sample.pptx", "synthetic powerpoint text"),
        ):
            extracted = run_json(
                [str(WORKSPACE_PYTHON), str(BIN / "extract_wechat_document.py"), str(documents / filename)],
                {0},
            )
            assert expected in extracted["text"]
        active_doc = documents / "active-content.docx"
        with zipfile.ZipFile(active_doc, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types />")
            archive.writestr("word/vbaProject.bin", b"synthetic")
        blocked = run_json(
            [str(WORKSPACE_PYTHON), str(BIN / "extract_wechat_document.py"), str(active_doc)],
            {2},
        )
        assert blocked["error_code"] == "active_content_blocked"
        checks += 1

        fixture = temp / "fixture" / "db_storage"
        account = temp / "fixture"
        external = temp / "private-source"
        contacts = [
            (direct, "合成标签联系人", tagged_label(7)),
            (group, "BUSINESS 合成工作群", b""),
            (excluded, "Lithion 客户群", b""),
        ]
        create_contact(fixture, contacts)
        conversations = {
            direct: [
                (1, 1, created, 1, "验证码是多少？", 1001, None),
                (2, 1, created + 1, 1, secret, 1002, None),
                (3, 3, created + 2, 1, "<msg><img /></msg>", 1003, None),
                (4, 34, created + 3, 1, "", 1004, None),
                (5, 47, created + 4, 1, f"<msg><emoji md5='{digests['emoji']}' /></msg>", 1005, None),
                (6, 49, created + 5, 1, f"<msg><appmsg><title>合成 PDF</title><type>6</type><appattach><fileext>pdf</fileext><md5>{digests['document']}</md5></appattach></appmsg></msg>", 1006, None),
                (7, 43, created + 6, 1, f"<msg><video><md5>{digests['video']}</md5></video></msg>", 1007, None),
            ],
            group: [(1, 1, created + 10, 2, "wxid_member:\n合成群消息", 2001, None)],
            excluded: [(1, 1, created + 20, 3, "不应入库", 3001, None)],
        }
        create_messages(fixture, conversations)
        create_resource(fixture, direct, 3, created + 2, digests["image"])
        create_voice(fixture, direct, 4, created + 3, wav_silence())
        write_media(account, direct, created, digests)
        base_config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        base_config["external_root"] = str(external)
        base_config["wechat_chat"]["source_directory"] = str(external / "wechat-chat")
        base_config["wechat_chat"]["mode"] = "pilot"
        base_config["wechat_chat"]["approved_ai_input_tokens"] = 0
        config_path = temp / "config.json"
        config_path.write_text(json.dumps(base_config, ensure_ascii=False), encoding="utf-8")

        collect = ["/usr/bin/python3", str(BIN / "collect_wechat_chat.py"), "--config", str(config_path), "--fixture-root", str(fixture)]
        first = run_json(collect, {4})
        assert first["status"] == "paused_budget" and first["direct_count"] == 1 and first["group_count"] == 1
        checks += 1
        store = sqlite3.connect(external / "wechat-chat" / "state" / "chat.sqlite")
        store.row_factory = sqlite3.Row
        assert store.execute("SELECT COUNT(*) FROM scope_members").fetchone()[0] == 2
        assert store.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 8
        all_text = "\n".join(str(row[0]) for row in store.execute("SELECT text_redacted FROM messages"))
        assert secret not in all_text and "敏感回复已脱敏" in all_text and "不应入库" not in all_text
        assert store.execute("SELECT COUNT(*) FROM messages WHERE redaction_count>0 AND original_text_sha256<>''").fetchone()[0] == 0
        checks += 1
        assert store.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        checks += 1
        active_ids = [str(row[0]) for row in store.execute("SELECT conversation_id FROM scope_members WHERE active=1")]
        try:
            scope_expansion_guard(
                store,
                [{"conversation_id": value} for value in active_ids]
                + [{"conversation_id": "synthetic-new-conversation"}],
            )
            raise AssertionError("scope_growth_not_blocked")
        except WeChatSyncError as exc:
            assert exc.code == "scope_expansion_paused"
        checks += 1
        store.close()

        second = run_json(collect, {4})
        assert second["messages_inserted"] == 0 and second["messages_updated"] == 0
        checks += 1
        shard = sqlite3.connect(fixture / "message" / "message_0.db")
        shard.execute(f"UPDATE [Msg_{md5_name(group)}] SET message_content='wxid_member:\n合成群消息已更新' WHERE local_id=1")
        shard.commit()
        shard.close()
        third = run_json(collect, {4})
        assert third["messages_updated"] == 1
        store = sqlite3.connect(external / "wechat-chat" / "state" / "chat.sqlite")
        assert store.execute("SELECT MAX(version_count) FROM (SELECT COUNT(*) AS version_count FROM message_versions GROUP BY message_id)").fetchone()[0] == 2
        checks += 1
        store.close()

        media = run_json(
            ["/usr/bin/python3", str(BIN / "process_wechat_media.py"), "--config", str(config_path), "--fixture-account-root", str(account), "--limit", "20"],
            {0, 2},
        )
        assert media["items_seen"] >= 5 and media["database_quick_check"] == "ok"
        store = sqlite3.connect(external / "wechat-chat" / "state" / "chat.sqlite")
        states = {row[0] for row in store.execute("SELECT status FROM media_assets")}
        assert states.issubset({"processed", "failed", "unavailable", "quarantined"}) and "processed" in states
        assert store.execute("SELECT COUNT(*) FROM media_derivations").fetchone()[0] >= 2
        store.close()
        checks += 1

        pilot_report = run_json(
            ["/usr/bin/python3", str(BIN / "report_wechat_pilot.py"), "--config", str(config_path)],
            {0},
        )
        assert pilot_report["status"] == "success"
        assert pilot_report["scope_counts"] == {"direct": 1, "group": 1}
        assert pilot_report["message_count"] == 8
        assert pilot_report["approval_status"] == "paused_budget"
        assert pilot_report["pricing_status"] == "current_price_review_required"
        checks += 1

        base_config["wechat_chat"]["approved_ai_input_tokens"] = 1_000_000
        config_path.write_text(json.dumps(base_config, ensure_ascii=False), encoding="utf-8")
        refreshed = run_json(
            ["/usr/bin/python3", str(BIN / "process_wechat_media.py"), "--config", str(config_path), "--fixture-account-root", str(account), "--limit", "20"],
            {0, 2},
        )
        assert refreshed["ready_tokens"] > 0
        claimed = run_json(
            ["/usr/bin/python3", str(BIN / "wechat_ai_queue.py"), "--config", str(config_path), "claim-next"],
            {0},
        )
        claimed_text = json.dumps(claimed, ensure_ascii=False)
        assert claimed["untrusted_source_data"] is True
        assert claimed["messages"] and claimed["messages"][0]["source_ref"].startswith("wechat:fixture-")
        assert secret not in claimed_text
        completed = run_json(
            [
                "/usr/bin/python3", str(BIN / "wechat_ai_queue.py"), "--config", str(config_path),
                "complete", "--batch-id", claimed["batch_id"], "--validation-passed",
            ],
            {0},
        )
        assert completed["status"] == "complete" and completed["database_quick_check"] == "ok"
        checks += 1

        for path in external.rglob("*"):
            if path.is_file():
                assert secret.encode("utf-8") not in path.read_bytes()
                mode = path.stat().st_mode & 0o777
                assert mode & 0o077 == 0
        checks += 1

    print(json.dumps({"verdict": "pass", "checks": checks, "real_wechat_read": False, "secrets_persisted": False}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
