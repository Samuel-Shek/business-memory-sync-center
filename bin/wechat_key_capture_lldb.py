#!/usr/bin/env python3
"""Capture WeChat SQLCipher keys at database-open time without persisting key bytes."""

from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import argparse
import hashlib
import hmac
import json
import os
import re
import signal
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path


PAGE_SIZE = 4096
SALT_SIZE = 16
KEY_SIZE = 32
MAX_TARGETS = 64
MEMORY_CHUNK_SIZE = 8 * 1024 * 1024
MEMORY_OVERLAP = 256
MAX_MEMORY_REGION_SIZE = 500 * 1024 * 1024
MAX_MEMORY_SCAN_BYTES = 6 * 1024 * 1024 * 1024
PROTOCOL_MAGIC = b"LWK1"
WECHAT_EXECUTABLE = Path("/Applications/WeChat.app/Contents/MacOS/WeChat")
WECHAT_CORE = Path("/Applications/WeChat.app/Contents/Resources/wechat.dylib")
AUDITED_CORE_SHA256 = "3761182757ea5510d54e5d3b4360f5499816626dfd60ae1c7eef94355fd34fb5"
AUDITED_CIPHER_FUNCTION_FILE_ADDRESS = 0x50AFD5C
KEY_PATTERN = re.compile(r"^x'([0-9a-fA-F]{64})([0-9a-fA-F]{32})'$", re.ASCII)
MEMORY_KEY_PATTERN = re.compile(rb"x'([0-9a-fA-F]{64,192})'")
CODEC_CONTEXT_SIGNATURE = struct.pack("<8I", 16, 32, 16, 16, 4096, 80, 64, 0)


class CaptureError(RuntimeError):
    pass


@dataclass
class DatabaseTarget:
    relative_path: str
    page_one: bytes
    salt: bytes
    key: bytearray | None = None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb", buffering=0) as handle:
        while True:
            chunk = handle.read(4 * 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def is_target_relative(relative: str) -> bool:
    if relative in {
        "contact/contact.db",
        "session/session.db",
        "message/message_resource.db",
    }:
        return True
    return re.fullmatch(r"message/(?:message|media)_[0-9]+\.db", relative) is not None


def collect_targets(account_root: Path) -> list[DatabaseTarget]:
    account_root = account_root.resolve(strict=True)
    database_root = (account_root / "db_storage").resolve(strict=True)
    if database_root.parent != account_root or database_root.is_symlink():
        raise CaptureError("invalid_account_root")

    targets: list[DatabaseTarget] = []
    for path in sorted(database_root.rglob("*.db")):
        if path.is_symlink() or not path.is_file():
            continue
        resolved = path.resolve(strict=True)
        try:
            relative = str(resolved.relative_to(database_root))
        except ValueError as exc:
            raise CaptureError("database_path_escape") from exc
        if not is_target_relative(relative):
            continue
        with resolved.open("rb", buffering=0) as handle:
            page_one = handle.read(PAGE_SIZE)
        if len(page_one) != PAGE_SIZE or page_one.startswith(b"SQLite format 3"):
            raise CaptureError("invalid_encrypted_database")
        targets.append(DatabaseTarget(relative, page_one, page_one[:SALT_SIZE]))
    if len(targets) < 3 or len(targets) > MAX_TARGETS:
        raise CaptureError("database_targets_unavailable")
    return targets


def verify_key_for_page(key: bytes | bytearray, page_one: bytes) -> bool:
    if len(key) != KEY_SIZE or len(page_one) != PAGE_SIZE:
        return False
    salt = page_one[:SALT_SIZE]
    mac_salt = bytes(value ^ 0x3A for value in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", bytes(key), mac_salt, 2, dklen=KEY_SIZE)
    mac_data = page_one[SALT_SIZE : PAGE_SIZE - 80 + 16]
    stored_mac = page_one[PAGE_SIZE - 64 : PAGE_SIZE]
    calculated = hmac.new(mac_key, mac_data, hashlib.sha512)
    calculated.update(struct.pack("<I", 1))
    return hmac.compare_digest(calculated.digest(), stored_mac)


def accept_key_string(raw_value: str, targets: list[DatabaseTarget]) -> int:
    match = KEY_PATTERN.fullmatch(raw_value)
    if match is None:
        return 0
    candidate = bytearray.fromhex(match.group(1))
    embedded_salt = bytes.fromhex(match.group(2))
    matched = False
    try:
        for target in targets:
            if target.salt != embedded_salt or not verify_key_for_page(candidate, target.page_one):
                continue
            target.key = bytearray(candidate)
            matched = True
        if not matched:
            return 0

        # WeChat commonly reuses one encryption key across databases with different salts.
        # Validate that relationship cryptographically instead of assuming it.
        for target in targets:
            if target.key is None and verify_key_for_page(candidate, target.page_one):
                target.key = bytearray(candidate)
        return 1
    finally:
        for index in range(len(candidate)):
            candidate[index] = 0


def accept_raw_key(candidate_bytes: bytes | bytearray, targets: list[DatabaseTarget]) -> int:
    if len(candidate_bytes) != KEY_SIZE:
        return 0
    candidate = bytearray(candidate_bytes)
    accepted = 0
    try:
        for target in targets:
            if verify_key_for_page(candidate, target.page_one):
                target.key = bytearray(candidate)
                accepted = 1
        return accepted
    finally:
        for index in range(len(candidate)):
            candidate[index] = 0


def accept_memory_hex(hex_data: bytes, targets: list[DatabaseTarget]) -> tuple[int, int]:
    """Accept a key-only, standard, or extended cached value after page-HMAC proof."""
    if len(hex_data) < 64 or len(hex_data) % 2:
        return 0, 0
    candidate = bytearray.fromhex(hex_data[:64].decode("ascii"))
    embedded_salt = None
    if len(hex_data) == 96:
        embedded_salt = bytes.fromhex(hex_data[64:96].decode("ascii"))
    elif len(hex_data) > 96:
        embedded_salt = bytes.fromhex(hex_data[-32:].decode("ascii"))
    scope_match = int(embedded_salt is None or any(target.salt == embedded_salt for target in targets))
    if not scope_match:
        for index in range(len(candidate)):
            candidate[index] = 0
        return 0, 0
    accepted = 0
    try:
        for target in targets:
            if embedded_salt is not None and target.salt != embedded_salt:
                continue
            if verify_key_for_page(candidate, target.page_one):
                target.key = bytearray(candidate)
                accepted = 1
        if accepted and embedded_salt is not None:
            for target in targets:
                if target.key is None and verify_key_for_page(candidate, target.page_one):
                    target.key = bytearray(candidate)
        return scope_match, accepted
    finally:
        for index in range(len(candidate)):
            candidate[index] = 0


def accept_memory_buffer(data: bytes, targets: list[DatabaseTarget]) -> tuple[int, int, int]:
    """Validate formatted candidates in one memory buffer without exposing their bytes."""
    formatted = 0
    scope_matches = 0
    accepted = 0
    for match in MEMORY_KEY_PATTERN.finditer(data):
        formatted += 1
        matched, verified = accept_memory_hex(match.group(1), targets)
        scope_matches += matched
        accepted += verified
    return formatted, scope_matches, accepted


def accept_codec_context(process, context_address: int, targets: list[DatabaseTarget], lldb) -> tuple[int, int]:
    """Validate a SQLCipher codec_ctx layout and recover only page-HMAC-proven keys."""
    read_error = lldb.SBError()
    context = process.ReadMemory(context_address, 128, read_error)
    if not read_error.Success() or len(context) != 128:
        return 0, 0
    values = struct.unpack_from("<15I", context, 0)
    if (
        values[0] not in {0, 1}
        or values[2] != 2
        or tuple(values[3:11]) != (16, 32, 16, 16, 4096, 80, 64, 0)
        or values[11] not in {1, 2, 3}
        or values[12] not in {1, 2, 3}
    ):
        return 0, 0
    read_context = struct.unpack_from("<Q", context, 96)[0]
    write_context = struct.unpack_from("<Q", context, 104)[0]
    scope_match = 0
    accepted = 0
    for cipher_context_address in {read_context, write_context}:
        if cipher_context_address == 0:
            continue
        cipher_error = lldb.SBError()
        cipher_context = process.ReadMemory(cipher_context_address, 32, cipher_error)
        if not cipher_error.Success() or len(cipher_context) != 32:
            continue
        key_address = struct.unpack_from("<Q", cipher_context, 8)[0]
        if key_address == 0:
            continue
        key_error = lldb.SBError()
        candidate = bytearray(process.ReadMemory(key_address, KEY_SIZE, key_error))
        if not key_error.Success() or len(candidate) != KEY_SIZE:
            for index in range(len(candidate)):
                candidate[index] = 0
            continue
        try:
            for target in targets:
                if verify_key_for_page(candidate, target.page_one):
                    scope_match = 1
                    target.key = bytearray(candidate)
                    accepted = 1
        finally:
            for index in range(len(candidate)):
                candidate[index] = 0
    return scope_match, accepted


def scan_process_memory(process, targets: list[DatabaseTarget], lldb) -> dict[str, int]:
    """Scan bounded readable data regions for cached, cryptographically verified DB keys."""
    regions: list[tuple[int, int, bool]] = []
    address = 0
    region_info = lldb.SBMemoryRegionInfo()
    while True:
        error = process.GetMemoryRegionInfo(address, region_info)
        if error.Fail():
            break
        base = region_info.GetRegionBase()
        end = region_info.GetRegionEnd()
        if end <= base or end <= address:
            break
        size = end - base
        if (
            region_info.IsReadable()
            and not region_info.IsExecutable()
            and 0 < size < MAX_MEMORY_REGION_SIZE
        ):
            regions.append((base, size, bool(region_info.IsWritable())))
        address = end
        if address == 0:
            break

    # Heap/data mappings are the most likely locations and are scanned first.
    regions.sort(key=lambda item: (not item[2], item[0]))
    scanned_regions = 0
    scanned_bytes = 0
    formatted = 0
    salt_matches = 0
    accepted = 0
    seen_candidate_digests: set[bytes] = set()
    seen_codec_contexts: set[int] = set()

    for base, size, writable in regions:
        if scanned_bytes >= MAX_MEMORY_SCAN_BYTES or all(target.key is not None for target in targets):
            break
        scanned_regions += 1
        offset = 0
        carry = b""
        while offset < size and scanned_bytes < MAX_MEMORY_SCAN_BYTES:
            wanted = min(MEMORY_CHUNK_SIZE, size - offset, MAX_MEMORY_SCAN_BYTES - scanned_bytes)
            read_error = lldb.SBError()
            read_addr = base + offset
            chunk = process.ReadMemory(read_addr, wanted, read_error)
            offset += wanted
            scanned_bytes += wanted
            if not read_error.Success() or not chunk:
                carry = b""
                continue
            buffer = carry + chunk
            buffer_address = read_addr - len(carry)
            for match in MEMORY_KEY_PATTERN.finditer(buffer):
                digest = hashlib.sha256(match.group(0)).digest()
                if digest in seen_candidate_digests:
                    continue
                seen_candidate_digests.add(digest)
                formatted += 1
                matched, verified = accept_memory_hex(match.group(1), targets)
                salt_matches += matched
                accepted += verified
                if all(target.key is not None for target in targets):
                    break
            if writable and not all(target.key is not None for target in targets):
                search_from = 0
                while True:
                    signature_offset = buffer.find(CODEC_CONTEXT_SIGNATURE, search_from)
                    if signature_offset < 0:
                        break
                    context_address = buffer_address + signature_offset - 12
                    search_from = signature_offset + 1
                    if context_address in seen_codec_contexts or context_address <= 0:
                        continue
                    seen_codec_contexts.add(context_address)
                    formatted += 1
                    matched, verified = accept_codec_context(process, context_address, targets, lldb)
                    salt_matches += matched
                    accepted += verified
                    if all(target.key is not None for target in targets):
                        break
            if all(target.key is not None for target in targets):
                break
            carry = buffer[-MEMORY_OVERLAP:]

    return {
        "regions": scanned_regions,
        "bytes": scanned_bytes,
        "formatted": formatted,
        "salt_matches": salt_matches,
        "accepted": accepted,
    }


def encode_protocol(account_root: Path, targets: list[DatabaseTarget]) -> bytearray:
    account_digest = hashlib.sha256(account_root.name.encode("utf-8")).digest()
    output = bytearray(PROTOCOL_MAGIC)
    output.extend(account_digest)
    kind = b"database"
    for target in sorted(targets, key=lambda item: item.relative_path):
        if target.key is None:
            continue
        path = target.relative_path.encode("utf-8")
        output.extend(struct.pack(">HHHH", len(path), len(target.key), len(target.salt), len(kind)))
        output.extend(path)
        output.extend(kind)
        output.extend(target.salt)
        output.extend(target.key)
    output.extend(struct.pack(">HHHH", 0, 0, 0, 0))
    return output


def synthetic_page(key: bytes, salt: bytes, fill: int) -> bytes:
    page = bytearray([fill] * PAGE_SIZE)
    page[:SALT_SIZE] = salt
    mac_salt = bytes(value ^ 0x3A for value in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", key, mac_salt, 2, dklen=KEY_SIZE)
    calculated = hmac.new(mac_key, page[SALT_SIZE : PAGE_SIZE - 80 + 16], hashlib.sha512)
    calculated.update(struct.pack("<I", 1))
    page[PAGE_SIZE - 64 : PAGE_SIZE] = calculated.digest()
    return bytes(page)


def self_test() -> int:
    key = bytes(range(KEY_SIZE))
    salt_one = bytes(range(SALT_SIZE))
    salt_two = bytes(range(16, 16 + SALT_SIZE))
    targets = [
        DatabaseTarget("message/message_0.db", synthetic_page(key, salt_one, 0x51), salt_one),
        DatabaseTarget("message/message_1.db", synthetic_page(key, salt_two, 0x63), salt_two),
    ]
    raw = "x'" + key.hex() + salt_one.hex() + "'"
    if accept_key_string(raw, targets) != 1 or any(target.key is None for target in targets):
        raise CaptureError("self_test_capture_failed")
    if verify_key_for_page(bytes(reversed(key)), targets[0].page_one):
        raise CaptureError("self_test_false_positive")
    memory = b"prefix" + raw.encode("ascii") + b"suffix"
    formatted, salt_matches, accepted = accept_memory_buffer(memory, targets)
    if (formatted, salt_matches, accepted) != (1, 1, 1):
        raise CaptureError("self_test_memory_scan_failed")
    key_only_targets = [
        DatabaseTarget("message/message_2.db", synthetic_page(key, salt_two, 0x77), salt_two)
    ]
    key_only = b"x'" + key.hex().encode("ascii") + b"'"
    if accept_memory_buffer(key_only, key_only_targets) != (1, 1, 1):
        raise CaptureError("self_test_memory_key_only_failed")
    extended_targets = [
        DatabaseTarget("message/message_3.db", synthetic_page(key, salt_one, 0x81), salt_one)
    ]
    extended = b"x'" + key.hex().encode("ascii") + b"00" * 16 + salt_one.hex().encode("ascii") + b"'"
    if accept_memory_buffer(extended, extended_targets) != (1, 1, 1):
        raise CaptureError("self_test_memory_extended_failed")
    codec_target = DatabaseTarget(
        "contact/contact.db", synthetic_page(key, salt_two, 0x91), salt_two
    )
    codec_context = bytearray(128)
    struct.pack_into(
        "<15I",
        codec_context,
        0,
        0,
        256000,
        2,
        16,
        32,
        16,
        16,
        4096,
        80,
        64,
        0,
        2,
        2,
        0,
        0x1B,
    )
    struct.pack_into("<Q", codec_context, 96, 0x2000)
    cipher_context = bytearray(32)
    struct.pack_into("<iiQQQ", cipher_context, 0, 0, 99, 0x3000, 0x4000, 0x5000)

    class FakeError:
        def Success(self) -> bool:
            return True

    class FakeLLDB:
        @staticmethod
        def SBError() -> FakeError:
            return FakeError()

    class FakeProcess:
        memory = {0x1000: bytes(codec_context), 0x2000: bytes(cipher_context), 0x3000: key}

        def ReadMemory(self, address: int, size: int, _error: FakeError) -> bytes:
            return self.memory.get(address, b"")[:size]

    if accept_codec_context(FakeProcess(), 0x1000, [codec_target], FakeLLDB) != (1, 1):
        raise CaptureError("self_test_codec_context_failed")
    raw_target = DatabaseTarget(
        "message/media_0.db", synthetic_page(key, salt_one, 0xA1), salt_one
    )
    if accept_raw_key(key, [raw_target]) != 1:
        raise CaptureError("self_test_raw_crypto_key_failed")
    protocol = encode_protocol(Path("/tmp/account-test"), targets)
    if not protocol.startswith(PROTOCOL_MAGIC) or not protocol.endswith(b"\x00" * 8):
        raise CaptureError("self_test_protocol_failed")
    for target in targets:
        if target.key is not None:
            for index in range(len(target.key)):
                target.key[index] = 0
    print('{"verdict":"pass","checks":11}')
    return 0


def find_wechat_core_module(target):
    candidates = []
    for module in target.module_iter():
        file_spec = module.GetFileSpec()
        if file_spec.GetFilename() != "wechat.dylib":
            continue
        directory = file_spec.GetDirectory() or ""
        module_path = str(Path(directory) / "wechat.dylib")
        if module_path.endswith("/WeChat.app/Contents/Resources/wechat.dylib"):
            candidates.append(module)
    return candidates[0] if len(candidates) == 1 else None


def find_text_section(module, target) -> tuple[int, int]:
    for index in range(module.GetNumSections()):
        section = module.GetSectionAtIndex(index)
        if section.GetName() != "__TEXT":
            continue
        for subindex in range(section.GetNumSubSections()):
            subsection = section.GetSubSectionAtIndex(subindex)
            if subsection.GetName() == "__text":
                return subsection.GetLoadAddress(target), subsection.GetByteSize()
    return 0, 0


def resolve_malloc(target) -> int:
    for context in target.FindSymbols("malloc"):
        symbol = context.GetSymbol()
        if symbol.IsValid():
            return symbol.GetStartAddress().GetLoadAddress(target)
    return 0


def continue_to_loaded_process(debugger, target, process, lldb) -> None:
    if resolve_malloc(target) and find_wechat_core_module(target) is not None:
        return
    settings_result = lldb.SBCommandReturnObject()
    debugger.GetCommandInterpreter().HandleCommand(
        "settings set target.process.stop-on-sharedlibrary-events true", settings_result
    )
    entry_breakpoint = target.BreakpointCreateByName("main")
    entry_breakpoint.SetOneShot(True)
    if entry_breakpoint.GetNumLocations() == 0:
        entry_breakpoint = target.BreakpointCreateByName("NSApplicationMain")
        entry_breakpoint.SetOneShot(True)
    deadline = time.monotonic() + 60
    try:
        while time.monotonic() < deadline:
            process.Continue()
            if process.GetState() != lldb.eStateStopped:
                raise CaptureError("wechat_entrypoint_unavailable")
            if resolve_malloc(target) and find_wechat_core_module(target) is not None:
                return
        raise CaptureError("wechat_core_module_unavailable")
    finally:
        result = lldb.SBCommandReturnObject()
        debugger.GetCommandInterpreter().HandleCommand("break delete -f", result)
        debugger.GetCommandInterpreter().HandleCommand(
            "settings set target.process.stop-on-sharedlibrary-events false", result
        )


def find_cipher_functions(target, process, module, lldb) -> list[int]:
    text_address, text_size = find_text_section(module, target)
    malloc_address = resolve_malloc(target)
    if not text_address or not text_size or not malloc_address:
        raise CaptureError("wechat_text_unavailable")

    patterns = {struct.pack("<I", 0x52800860), struct.pack("<I", 0xD2800860)}
    error = lldb.SBError()
    chunk_size = 4 * 1024 * 1024
    consumed = 0
    functions: set[int] = set()
    while consumed < text_size:
        wanted = min(chunk_size, text_size - consumed)
        data = process.ReadMemory(text_address + consumed, wanted, error)
        if not error.Success():
            raise CaptureError("wechat_text_read_failed")
        for offset in range(0, max(0, len(data) - 3), 4):
            if data[offset : offset + 4] not in patterns:
                continue
            instruction_address = text_address + consumed + offset
            for delta in range(4, 20, 4):
                if offset + delta + 4 > len(data):
                    continue
                instruction = int.from_bytes(data[offset + delta : offset + delta + 4], "little")
                if instruction >> 26 != 0b100101:
                    continue
                immediate = instruction & 0x03FFFFFF
                if immediate & 0x02000000:
                    immediate -= 1 << 26
                branch_target = instruction_address + delta + (immediate << 2)
                symbol = target.ResolveLoadAddress(branch_target).GetSymbol()
                symbol_name = symbol.GetName() if symbol.IsValid() else None
                if branch_target != malloc_address and symbol_name != "malloc":
                    continue
                resolved = target.ResolveLoadAddress(instruction_address).GetSymbol()
                if resolved.IsValid():
                    functions.add(resolved.GetStartAddress().GetLoadAddress(target))
                else:
                    functions.add(instruction_address)
        consumed += len(data)
    if not functions:
        raise CaptureError("cipher_function_unavailable")
    return sorted(functions)


def find_cipher_function(target, process, module, lldb) -> int:
    return find_cipher_functions(target, process, module, lldb)[0]


def capture(account_root: Path, timeout_seconds: int, output_stream=None, status_stream=None) -> int:
    if output_stream is None:
        if os.isatty(sys.stdout.fileno()):
            raise CaptureError("secret_output_to_terminal_refused")
        output_stream = sys.stdout.buffer
    if status_stream is None:
        status_stream = sys.stderr
    targets = collect_targets(account_root)

    import lldb  # Imported only in the Xcode Python runtime for the live path.

    debugger = lldb.SBDebugger.Create()
    debugger.SetAsync(False)
    process = None
    stage = "setup"
    breakpoint_hits = 0
    formatted_candidates = 0
    salt_matches = 0
    memory_stats = {"regions": 0, "bytes": 0, "formatted": 0, "salt_matches": 0, "accepted": 0}

    def terminate_handler(_signal_number, _frame) -> None:
        raise CaptureError("capture_terminated")

    previous_handler = signal.signal(signal.SIGTERM, terminate_handler)
    try:
        stage = "attach"
        target = debugger.CreateTarget("")
        error = lldb.SBError()
        print("capture_ready", file=status_stream, flush=True)
        process = target.AttachToProcessWithName(debugger.GetListener(), "WeChat", True, error)
        if not error.Success() or not process.IsValid():
            raise CaptureError("task_port_unavailable")
        target = debugger.GetSelectedTarget()
        stage = "module_load"
        continue_to_loaded_process(debugger, target, process, lldb)
        module = find_wechat_core_module(target)
        if module is None:
            raise CaptureError("wechat_core_module_unavailable")
        stage = "locator"
        cipher_functions = find_cipher_functions(target, process, module, lldb)
        stage = "breakpoint"
        breakpoints = [target.BreakpointCreateByAddress(address) for address in cipher_functions]
        if not breakpoints or any(
            not item.IsValid() or item.GetNumLocations() != 1 for item in breakpoints
        ):
            raise CaptureError("cipher_breakpoint_unavailable")

        stage = "event_loop"
        listener = debugger.GetListener()
        debugger.SetAsync(True)
        process.Continue()
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline and any(target.key is None for target in targets):
            event = lldb.SBEvent()
            if not listener.WaitForEvent(1, event):
                continue
            state = lldb.SBProcess.GetStateFromEvent(event)
            if state in (lldb.eStateExited, lldb.eStateCrashed, lldb.eStateDetached):
                break
            if state != lldb.eStateStopped:
                continue
            stopped_thread = None
            for index in range(process.GetNumThreads()):
                thread = process.GetThreadAtIndex(index)
                if thread.GetStopReason() == lldb.eStopReasonBreakpoint:
                    stopped_thread = thread
                    break
            if stopped_thread is not None:
                breakpoint_hits += 1
                frame = stopped_thread.GetFrameAtIndex(0)
                unsafe_data = frame.FindRegister("x1").GetValueAsUnsigned()
                pointer = process.ReadPointerFromMemory(unsafe_data + 8, error)
                if error.Success() and pointer:
                    raw_value = process.ReadCStringFromMemory(pointer, 128, error)
                    if error.Success() and isinstance(raw_value, str):
                        match = KEY_PATTERN.fullmatch(raw_value)
                        if match is not None:
                            formatted_candidates += 1
                            embedded_salt = bytes.fromhex(match.group(2))
                            if any(target.salt == embedded_salt for target in targets):
                                salt_matches += 1
                        accept_key_string(raw_value, targets)
            process.Continue()

        # Some databases open before the audited high-level breakpoint is installed.
        # Once startup observation ends, stop WeChat briefly and recover only cached
        # candidates that match an active DB salt and pass its SQLCipher page HMAC.
        if any(target.key is None for target in targets):
            if process.GetState() != lldb.eStateStopped:
                stop_error = process.Stop()
                if not stop_error.Success():
                    raise CaptureError("capture_memory_scan_unavailable")
            memory_stats = scan_process_memory(process, targets, lldb)

        result = lldb.SBCommandReturnObject()
        debugger.GetCommandInterpreter().HandleCommand("break delete -f", result)
        if process.IsValid() and process.GetState() not in (lldb.eStateExited, lldb.eStateDetached):
            process.Detach()

        found = sum(target.key is not None for target in targets)
        print(
            "capture_diagnostics "
            f"database_records={found} required={len(targets)} "
            f"breakpoint_hits={breakpoint_hits} formatted_candidates={formatted_candidates} "
            f"salt_matches={salt_matches} memory_regions={memory_stats['regions']} "
            f"memory_bytes={memory_stats['bytes']} memory_candidates={memory_stats['formatted']} "
            f"memory_scope_matches={memory_stats['salt_matches']} memory_verified={memory_stats['accepted']}",
            file=status_stream,
            flush=True,
        )
        if found == 0:
            if breakpoint_hits == 0:
                raise CaptureError("capture_no_breakpoint_hits")
            if formatted_candidates == 0:
                raise CaptureError("capture_key_argument_unreadable")
            if salt_matches == 0:
                raise CaptureError("capture_key_salt_unmatched")
            raise CaptureError("capture_key_integrity_unverified")
        stage = "protocol"
        protocol = encode_protocol(account_root, targets)
        try:
            output_stream.write(protocol)
            output_stream.flush()
        finally:
            for index in range(len(protocol)):
                protocol[index] = 0
            for target in targets:
                if target.key is not None:
                    for index in range(len(target.key)):
                        target.key[index] = 0
        return 0 if found == len(targets) else 10
    except CaptureError:
        raise
    except Exception as exc:
        raise CaptureError(f"capture_internal_{stage}") from exc
    finally:
        try:
            if process is not None and process.IsValid() and process.GetState() not in (
                lldb.eStateExited,
                lldb.eStateDetached,
            ):
                process.Detach()
        except Exception:
            pass
        try:
            signal.signal(signal.SIGTERM, previous_handler)
        except Exception:
            pass
        try:
            lldb.SBDebugger.Destroy(debugger)
        except Exception:
            pass


def audited_static_breakpoint(target):
    if not WECHAT_EXECUTABLE.is_file() or not WECHAT_CORE.is_file():
        raise CaptureError("wechat_module_unavailable")
    if sha256_file(WECHAT_CORE) != AUDITED_CORE_SHA256:
        raise CaptureError("wechat_core_changed")
    module = target.AddModule(str(WECHAT_CORE), None, None)
    if not module.IsValid():
        raise CaptureError("wechat_core_module_unavailable")
    address = module.ResolveFileAddress(AUDITED_CIPHER_FUNCTION_FILE_ADDRESS)
    if not address.IsValid() or address.GetFileAddress() != AUDITED_CIPHER_FUNCTION_FILE_ADDRESS:
        raise CaptureError("cipher_function_unavailable")
    section = address.GetSection()
    if not section.IsValid() or section.GetName() != "__text":
        raise CaptureError("cipher_function_unavailable")
    breakpoint = target.BreakpointCreateBySBAddress(address)
    if not breakpoint.IsValid() or breakpoint.GetNumLocations() != 1:
        raise CaptureError("cipher_breakpoint_unavailable")
    return breakpoint


def capture_audited_launch(
    account_root: Path,
    timeout_seconds: int,
    output_stream,
    status_stream,
) -> int:
    """Launch WeChat under a version-locked pending breakpoint before app code runs."""
    targets = collect_targets(account_root)
    import lldb

    debugger = lldb.SBDebugger.Create()
    debugger.SetAsync(False)
    process = None
    breakpoint_hits = 0
    formatted_candidates = 0
    salt_matches = 0
    seen_crypto_key_digests: set[bytes] = set()
    memory_stats = {"regions": 0, "bytes": 0, "formatted": 0, "salt_matches": 0, "accepted": 0}
    try:
        target = debugger.CreateTarget(str(WECHAT_EXECUTABLE))
        if not target.IsValid():
            raise CaptureError("wechat_module_unavailable")
        breakpoint = audited_static_breakpoint(target)
        crypto_breakpoints = [
            target.BreakpointCreateByName(name) for name in ("CCCryptorCreate", "CCCrypt")
        ]
        if any(not item.IsValid() for item in crypto_breakpoints):
            raise CaptureError("cipher_breakpoint_unavailable")
        print("capture_ready", file=status_stream, flush=True)
        attach_error = lldb.SBError()
        process = target.AttachToProcessWithName(
            debugger.GetListener(), "WeChat", True, attach_error
        )
        if not attach_error.Success() or not process.IsValid():
            raise CaptureError("task_port_unavailable")

        listener = debugger.GetListener()
        debugger.SetAsync(True)
        process.Continue()
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline and any(target_item.key is None for target_item in targets):
            event = lldb.SBEvent()
            if not listener.WaitForEvent(1, event):
                continue
            state = lldb.SBProcess.GetStateFromEvent(event)
            if state in (lldb.eStateExited, lldb.eStateCrashed, lldb.eStateDetached):
                break
            if state != lldb.eStateStopped:
                continue
            stopped_thread = None
            for index in range(process.GetNumThreads()):
                thread = process.GetThreadAtIndex(index)
                if thread.GetStopReason() == lldb.eStopReasonBreakpoint:
                    stopped_thread = thread
                    break
            if stopped_thread is not None:
                breakpoint_hits += 1
                frame = stopped_thread.GetFrameAtIndex(0)
                function_name = frame.GetFunctionName() or ""
                if "CCCrypt" in function_name:
                    algorithm = frame.FindRegister("x1").GetValueAsUnsigned()
                    key_pointer = frame.FindRegister("x3").GetValueAsUnsigned()
                    key_length = frame.FindRegister("x4").GetValueAsUnsigned()
                    if algorithm == 0 and key_length == KEY_SIZE and key_pointer:
                        key_error = lldb.SBError()
                        candidate = bytearray(process.ReadMemory(key_pointer, KEY_SIZE, key_error))
                        if key_error.Success() and len(candidate) == KEY_SIZE:
                            digest = hashlib.sha256(candidate).digest()
                            if digest not in seen_crypto_key_digests:
                                seen_crypto_key_digests.add(digest)
                                formatted_candidates += 1
                                salt_matches += accept_raw_key(candidate, targets)
                        for index in range(len(candidate)):
                            candidate[index] = 0
                else:
                    unsafe_data = frame.FindRegister("x1").GetValueAsUnsigned()
                    read_error = lldb.SBError()
                    pointer = process.ReadPointerFromMemory(unsafe_data + 8, read_error)
                    if read_error.Success() and pointer:
                        raw_value = process.ReadCStringFromMemory(pointer, 128, read_error)
                        if read_error.Success() and isinstance(raw_value, str):
                            match = KEY_PATTERN.fullmatch(raw_value)
                            if match is not None:
                                formatted_candidates += 1
                                embedded_salt = bytes.fromhex(match.group(2))
                                if any(target_item.salt == embedded_salt for target_item in targets):
                                    salt_matches += 1
                            accept_key_string(raw_value, targets)
                            raw_value = ""
            process.Continue()

        if any(target_item.key is None for target_item in targets):
            if process.GetState() != lldb.eStateStopped:
                stop_error = process.Stop()
                if not stop_error.Success():
                    raise CaptureError("capture_memory_scan_unavailable")
            memory_stats = scan_process_memory(process, targets, lldb)

        found = sum(target_item.key is not None for target_item in targets)
        print(
            "capture_diagnostics "
            f"database_records={found} required={len(targets)} "
            f"breakpoint_hits={breakpoint_hits} formatted_candidates={formatted_candidates} "
            f"salt_matches={salt_matches} memory_regions={memory_stats['regions']} "
            f"memory_bytes={memory_stats['bytes']} memory_candidates={memory_stats['formatted']} "
            f"memory_scope_matches={memory_stats['salt_matches']} memory_verified={memory_stats['accepted']}",
            file=status_stream,
            flush=True,
        )
        if found == 0:
            if breakpoint_hits == 0:
                raise CaptureError("capture_no_breakpoint_hits")
            if formatted_candidates == 0:
                raise CaptureError("capture_key_argument_unreadable")
            if salt_matches == 0:
                raise CaptureError("capture_key_salt_unmatched")
            raise CaptureError("capture_key_integrity_unverified")

        protocol = encode_protocol(account_root, targets)
        try:
            output_stream.write(protocol)
            output_stream.flush()
        finally:
            for index in range(len(protocol)):
                protocol[index] = 0
            for target_item in targets:
                if target_item.key is not None:
                    for index in range(len(target_item.key)):
                        target_item.key[index] = 0
        return 0 if found == len(targets) else 10
    finally:
        if process is not None and process.IsValid() and process.GetState() not in (
            lldb.eStateExited,
            lldb.eStateDetached,
        ):
            process.Detach()
        lldb.SBDebugger.Destroy(debugger)


def capture_current_memory(account_root: Path, output_stream, status_stream) -> int:
    """Attach to the current WeChat process and enroll only page-HMAC-verified cached keys."""
    targets = collect_targets(account_root)
    import lldb

    debugger = lldb.SBDebugger.Create()
    debugger.SetAsync(False)
    process = None
    try:
        target = debugger.CreateTarget("")
        error = lldb.SBError()
        process = target.AttachToProcessWithName(debugger.GetListener(), "WeChat", False, error)
        if not error.Success() or not process.IsValid():
            raise CaptureError("task_port_unavailable")
        memory_stats = scan_process_memory(process, targets, lldb)
        found = sum(target.key is not None for target in targets)
        print(
            "capture_diagnostics "
            f"database_records={found} required={len(targets)} "
            "breakpoint_hits=0 formatted_candidates=0 salt_matches=0 "
            f"memory_regions={memory_stats['regions']} memory_bytes={memory_stats['bytes']} "
            f"memory_candidates={memory_stats['formatted']} "
            f"memory_scope_matches={memory_stats['salt_matches']} memory_verified={memory_stats['accepted']}",
            file=status_stream,
            flush=True,
        )
        if found == 0:
            if memory_stats["formatted"] == 0:
                raise CaptureError("capture_memory_no_candidates")
            raise CaptureError("capture_key_integrity_unverified")
        protocol = encode_protocol(account_root, targets)
        try:
            output_stream.write(protocol)
            output_stream.flush()
        finally:
            for index in range(len(protocol)):
                protocol[index] = 0
            for target in targets:
                if target.key is not None:
                    for index in range(len(target.key)):
                        target.key[index] = 0
        return 0 if found == len(targets) else 10
    finally:
        if process is not None and process.IsValid() and process.GetState() not in (
            lldb.eStateExited,
            lldb.eStateDetached,
        ):
            process.Detach()
        lldb.SBDebugger.Destroy(debugger)


def embedded_main(account_root: str, timeout_seconds: int, output_fd: int, status_fd: int) -> None:
    """Run inside Apple's signed LLDB process and keep secrets on a dedicated pipe."""
    exit_status = 4
    try:
        if output_fd < 3 or status_fd < 3 or output_fd == status_fd:
            raise CaptureError("invalid_arguments")
        os.set_inheritable(output_fd, False)
        os.set_inheritable(status_fd, False)
        with os.fdopen(os.dup(output_fd), "wb", buffering=0) as output_stream, os.fdopen(
            os.dup(status_fd), "w", encoding="ascii", buffering=1
        ) as status_stream:
            try:
                exit_status = capture(
                    Path(account_root), timeout_seconds, output_stream, status_stream
                )
            except CaptureError as exc:
                print(str(exc), file=status_stream, flush=True)
                exit_status = 4
            except Exception:
                print("capture_internal_error", file=status_stream, flush=True)
                exit_status = 4
    except Exception:
        try:
            os.write(status_fd, b"capture_internal_error\n")
        except OSError:
            pass
        exit_status = 4
    os._exit(exit_status)


def embedded_memory_main(account_root: str, output_fd: int, status_fd: int) -> None:
    """Run the bounded current-process scan inside Apple's signed LLDB host."""
    exit_status = 4
    try:
        if output_fd < 3 or status_fd < 3 or output_fd == status_fd:
            raise CaptureError("invalid_arguments")
        os.set_inheritable(output_fd, False)
        os.set_inheritable(status_fd, False)
        with os.fdopen(os.dup(output_fd), "wb", buffering=0) as output_stream, os.fdopen(
            os.dup(status_fd), "w", encoding="ascii", buffering=1
        ) as status_stream:
            try:
                exit_status = capture_current_memory(Path(account_root), output_stream, status_stream)
            except CaptureError as exc:
                print(str(exc), file=status_stream, flush=True)
                exit_status = 4
            except Exception:
                print("capture_internal_error", file=status_stream, flush=True)
                exit_status = 4
    except Exception:
        try:
            os.write(status_fd, b"capture_internal_error\n")
        except OSError:
            pass
        exit_status = 4
    os._exit(exit_status)


def embedded_launch_main(account_root: str, timeout_seconds: int, output_fd: int, status_fd: int) -> None:
    """Run version-locked pre-launch capture inside Apple's signed LLDB host."""
    exit_status = 4
    try:
        if output_fd < 3 or status_fd < 3 or output_fd == status_fd:
            raise CaptureError("invalid_arguments")
        os.set_inheritable(output_fd, False)
        os.set_inheritable(status_fd, False)
        with os.fdopen(os.dup(output_fd), "wb", buffering=0) as output_stream, os.fdopen(
            os.dup(status_fd), "w", encoding="ascii", buffering=1
        ) as status_stream:
            try:
                exit_status = capture_audited_launch(
                    Path(account_root), timeout_seconds, output_stream, status_stream
                )
            except CaptureError as exc:
                print(str(exc), file=status_stream, flush=True)
                exit_status = 4
            except Exception:
                print("capture_internal_error", file=status_stream, flush=True)
                exit_status = 4
    except Exception:
        try:
            os.write(status_fd, b"capture_internal_error\n")
        except OSError:
            pass
        exit_status = 4
    os._exit(exit_status)


def probe_locator() -> int:
    """Validate the non-secret code locator against the currently running WeChat."""
    import lldb

    debugger = lldb.SBDebugger.Create()
    debugger.SetAsync(False)
    process = None
    try:
        target = debugger.CreateTarget("")
        error = lldb.SBError()
        process = target.AttachToProcessWithName(debugger.GetListener(), "WeChat", False, error)
        if not error.Success() or not process.IsValid():
            raise CaptureError("task_port_unavailable")
        target = debugger.GetSelectedTarget()
        module = find_wechat_core_module(target)
        if module is None:
            raise CaptureError("wechat_core_module_unavailable")
        cipher_functions = find_cipher_functions(target, process, module, lldb)
        breakpoints = [target.BreakpointCreateByAddress(address) for address in cipher_functions]
        if not breakpoints or any(
            not item.IsValid() or item.GetNumLocations() != 1 for item in breakpoints
        ):
            raise CaptureError("cipher_breakpoint_unavailable")
        module_header = module.GetObjectFileHeaderAddress().GetLoadAddress(target)
        if not module_header or any(address < module_header for address in cipher_functions):
            raise CaptureError("cipher_function_unavailable")
        relative_offsets = [address - module_header for address in cipher_functions]
        locator_fingerprints = [
            hashlib.sha256(f"wechat-core:{offset:x}".encode("ascii")).hexdigest()[:16]
            for offset in relative_offsets
        ]
        print(json.dumps({
            "verdict": "pass",
            "module": "wechat-core",
            "breakpoints": len(breakpoints),
            "relative_offsets": [f"0x{offset:x}" for offset in relative_offsets],
            "locator_fingerprints": locator_fingerprints,
        }, sort_keys=True))
        return 0
    finally:
        if process is not None and process.IsValid() and process.GetState() not in (
            lldb.eStateExited,
            lldb.eStateDetached,
        ):
            process.Detach()
        lldb.SBDebugger.Destroy(debugger)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Capture WeChat keys into an anonymous pipe")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--probe-locator", action="store_true")
    parser.add_argument("--account-root", type=Path)
    parser.add_argument("--timeout", type=int, default=180)
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()
    try:
        if args.self_test:
            return self_test()
        if args.probe_locator:
            return probe_locator()
        if args.account_root is None or not 15 <= args.timeout <= 600:
            raise CaptureError("invalid_arguments")
        return capture(args.account_root, args.timeout)
    except CaptureError as exc:
        print(str(exc), file=sys.stderr, flush=True)
        return 4
    except Exception:
        print("capture_internal_error", file=sys.stderr, flush=True)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
