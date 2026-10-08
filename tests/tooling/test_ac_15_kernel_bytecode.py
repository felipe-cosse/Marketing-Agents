"""Dependency-free instruction execution checks. No BPF or socket syscalls.

This intentionally interprets the emitted bytes, not a parallel classifier.
It is not a Linux verifier, ABI qualification or live-hook coverage proof.
"""

import hashlib
import ipaddress
import struct
import unittest

from scripts.ac_15_kernel_bytecode import (
    COUNTER_NAMES,
    NETWORK_COUNTER_NAMES,
    SECCOMP_ACTIONS,
    SYSCALLS,
    exit_instructions,
    instructions,
    network_instructions,
)

MASK = (1 << 64) - 1
TARGET = 0x1234567800000123
REGS = 0x2000
CONTEXT = 0x1000
STACK = 0x1000000
MAP = 0x2000000


def integer_bytes(value, size):
    return (value & ((1 << (8 * size)) - 1)).to_bytes(size, "little")


class NetworkMachine:
    """Small strict VM for exactly the generator's instruction/helper subset."""

    def __init__(self, scno, args=(), cgroup=TARGET):
        self.code = [
            struct.unpack("<BBhI", word)
            for word in struct.iter_unpack("8s", network_instructions(7, TARGET))
            for word in word
        ]
        self.reg = [None] * 11
        self.reg[1], self.reg[10] = CONTEXT, STACK
        self.kernel, self.user, self.stack = {}, {}, {}
        self.counts = [0] * len(NETWORK_COUNTER_NAMES)
        self.events = []
        self.cgroup = cgroup
        self.steps = 0
        self.kernel_fail_ranges = []
        self.put(self.kernel, CONTEXT, integer_bytes(REGS, 8))
        for index, value in enumerate(tuple(args) + (0,) * (8 - len(args))):
            self.put(self.kernel, REGS + index * 8, integer_bytes(value, 8))
        self.put(self.kernel, REGS + 64, integer_bytes(scno, 8))

    @staticmethod
    def put(memory, address, data):
        if not 0 <= address <= MASK or address + len(data) > MASK + 1:
            raise ValueError("synthetic_memory_wrap")
        memory.update((address + index, value) for index, value in enumerate(data))

    def allocate(self, address, data):
        self.put(self.user, address, data)
        return address

    def value(self, register):
        value = self.reg[register]
        if value is None:
            raise AssertionError("read_of_clobbered_or_uninitialized_register")
        return value

    def load(self, address, size):
        if address >= STACK - 512 and address + size <= STACK:
            memory = self.stack
        else:
            memory = self.kernel
            self.events.append(("context_read", address, size))
        try:
            return int.from_bytes(bytes(memory[address + i] for i in range(size)), "little")
        except KeyError as exc:
            raise AssertionError("uninitialized_or_invalid_direct_read") from exc

    def store(self, address, size, value):
        if not address >= STACK - 512 or address + size > STACK:
            raise AssertionError("nonstack_store")
        self.put(self.stack, address, integer_bytes(value, size))

    def helper(self, number):
        if number == 80:
            self.events.append(("scope",))
            result = self.cgroup
        elif number == 1:
            if self.value(1) != 7:
                raise AssertionError("wrong_map_identity")
            key = self.load(self.value(2), 4)
            if key >= len(self.counts):
                raise AssertionError("out_of_range_map_key")
            result = MAP + 8 * key
        elif number in (112, 113):
            destination, size, source = (self.value(i) for i in (1, 2, 3))
            self.events.append(("user" if number == 112 else "kernel", source, size))
            memory = self.user if number == 112 else self.kernel
            fault = source + size > MASK + 1 or any(source + i not in memory for i in range(size))
            if number == 113 and any(
                source < end and source + size > start for start, end in self.kernel_fail_ranges
            ):
                fault = True
            if fault:
                self.store(destination, size, 0)
                result = (-14) & MASK
            else:
                self.store(
                    destination,
                    size,
                    int.from_bytes(bytes(memory[source + i] for i in range(size)), "little"),
                )
                result = 0
        else:
            raise AssertionError("unapproved_helper")
        # The BPF helper ABI invalidates caller-saved registers, even if a real
        # helper happens to leave their machine values intact.
        self.reg[1:6] = [None] * 5
        self.reg[0] = result

    def run(self):
        pc = 0
        while True:
            self.steps += 1
            if self.steps > 500000:
                raise AssertionError("synthetic_instruction_budget_exceeded")
            opcode, packed, offset, immediate = self.code[pc]
            dst, src = packed & 15, packed >> 4
            signed = immediate if immediate < 2**31 else immediate - 2**32
            next_pc = pc + 1
            if opcode == 0x18:
                high = self.code[pc + 1]
                if high[:3] != (0, 0, 0):
                    raise AssertionError("invalid_lddw_continuation")
                if src not in (0, 1):
                    raise AssertionError("unsupported_lddw_mode")
                self.reg[dst] = immediate | high[3] << 32
                next_pc += 1
            elif opcode == 0xBF:
                self.reg[dst] = self.value(src)
            elif opcode == 0xB7:
                self.reg[dst] = signed & MASK
            elif opcode in (0x79, 0x61, 0x69, 0x71):
                size = {0x79: 8, 0x61: 4, 0x69: 2, 0x71: 1}[opcode]
                self.reg[dst] = self.load((self.value(src) + offset) & MASK, size)
            elif opcode == 0x62:
                self.store((self.value(dst) + offset) & MASK, 4, immediate)
            elif opcode == 0x63:
                self.store((self.value(dst) + offset) & MASK, 4, self.value(src))
            elif opcode == 0x07:
                self.reg[dst] = (self.value(dst) + signed) & MASK
            elif opcode == 0x0F:
                self.reg[dst] = (self.value(dst) + self.value(src)) & MASK
            elif opcode == 0x67:
                self.reg[dst] = (self.value(dst) << immediate) & MASK
            elif opcode == 0xDB:
                address = self.value(dst) + offset
                if immediate != 0 or not MAP <= address < MAP + 8 * len(self.counts) or address % 8:
                    raise AssertionError("invalid_atomic_counter_add")
                key = (address - MAP) // 8
                self.counts[key] = (self.counts[key] + self.value(src)) & MASK
            elif opcode == 0x85:
                self.helper(immediate)
            elif opcode == 0x95:
                if self.value(0) != 0:
                    raise AssertionError("nonzero_fentry_return")
                return dict(zip(NETWORK_COUNTER_NAMES, self.counts, strict=True))
            elif opcode == 0x05:
                next_pc += offset
            elif opcode in (0x15, 0x55, 0x25, 0xA5, 0x1D, 0x5D, 0xAD):
                left = self.value(dst)
                right = self.value(src) if opcode & 8 else signed & MASK
                if opcode in (0x15, 0x1D):
                    condition = left == right
                elif opcode in (0x55, 0x5D):
                    condition = left != right
                elif opcode == 0x25:
                    condition = left > right
                else:
                    condition = left < right
                if condition:
                    next_pc += offset
            else:
                raise AssertionError(f"unsupported_instruction_{opcode:02x}")
            if not 0 <= next_pc < len(self.code):
                raise AssertionError("jump_outside_program")
            pc = next_pc


def sockaddr(family, ip=None, pid=0):
    if family == 2:
        return struct.pack("<H", 2) + b"\0\0" + ipaddress.IPv4Address(ip).packed + b"\0" * 8
    if family == 10:
        return struct.pack("<H", 10) + b"\0" * 6 + ipaddress.IPv6Address(ip).packed + b"\0" * 4
    if family == 16:
        return struct.pack("<HHII", 16, 0, pid, 0)
    if family == 1:
        return struct.pack("<H", 1) + b"not-read\0"
    return struct.pack("<H", family)


def header(pointer, length):
    return struct.pack("<QI", pointer, length & 0xFFFFFFFF) + b"\xa5" * 52


def call(number, address, length, data=None):
    if number & 0xFFFFFFFF == 203:
        args = (MASK, address, length)
    elif number & 0xFFFFFFFF == 206:
        args = (MASK, 0xDEADBEEF, 999, 0xF000000000, address, length)
    elif number & 0xFFFFFFFF in (211, 269):
        args = (MASK, 0x4000, 1, 0)
    else:
        raise AssertionError("unknown_fixture_syscall")
    machine = NetworkMachine(number, args)
    if data is not None:
        machine.allocate(address, data)
    if number & 0xFFFFFFFF in (211, 269):
        machine.allocate(0x4000, header(address, length))
    return machine


class InstructionTests(unittest.TestCase):
    def assert_result(self, machine, operation, outcome=None, messages=1, **extras):
        expected = dict.fromkeys(NETWORK_COUNTER_NAMES, 0)
        expected["entry_" + operation] = 1
        expected["messages"] = messages
        if outcome:
            expected[outcome] = messages
        expected.update(extras)
        result = machine.run()
        self.assertEqual(result, expected)
        self.assertEqual(
            result["messages"], sum(result[key] for key in NETWORK_COUNTER_NAMES[5:12])
        )
        return result

    def test_family_matrix_all_operations(self):
        cases = (
            (2, "127.0.0.1", 0, "loopback"),
            (2, "127.255.9.8", 0, "loopback"),
            (2, "126.255.255.255", 0, "external"),
            (2, "128.0.0.0", 0, "external"),
            (2, "0.0.0.0", 0, "external"),
            (10, "::1", 0, "loopback"),
            (10, "2001:db8::1", 0, "external"),
            (10, "::ffff:127.9.8.7", 0, "loopback"),
            (10, "::ffff:192.0.2.1", 0, "external"),
            (10, "::127.0.0.1", 0, "external"),
            (10, "::", 0, "external"),
            (1, None, 0, "unix"),
            (16, None, 0, "kernel_ipc"),
            (16, None, 7, "unclassified"),
            (0, None, 0, "connected_send"),
            (65535, None, 0, "unclassified"),
        )
        for number, operation in zip(
            SYSCALLS, ("connect", "sendto", "sendmsg", "sendmmsg"), strict=True
        ):
            for family, ip, pid, outcome in cases:
                with self.subTest(operation=operation, family=family, ip=ip, pid=pid):
                    data = sockaddr(family, ip, pid)
                    self.assert_result(call(number, 0x5000, len(data), data), operation, outcome)

    def test_lengths_low32_and_strict_boundaries(self):
        for number, operation in zip(
            SYSCALLS, ("connect", "sendto", "sendmsg", "sendmmsg"), strict=True
        ):
            for length in (0, 1, 15, 129, 0xFFFFFFFF, 0x80000000, (1 << 32) + 1):
                with self.subTest(operation=operation, length=length):
                    self.assert_result(
                        call(number, 0x5000, length, sockaddr(2, "127.0.0.1")),
                        operation,
                        "unclassified",
                    )
            for length in (16, 128, (0xABCDEF << 32) + 16):
                self.assert_result(
                    call(number, 0x5000, length, sockaddr(2, "127.0.0.1")), operation, "loopback"
                )

    def test_family_minimum_lengths_and_unix_maximum(self):
        for family, ip, minimum, maximum, outcome in (
            (1, None, 3, 110, "unix"),
            (2, "127.0.0.1", 16, 128, "loopback"),
            (10, "::1", 24, 128, "loopback"),
            (16, None, 12, 128, "kernel_ipc"),
        ):
            for length in (minimum - 1, minimum, maximum, maximum + 1):
                with self.subTest(family=family, length=length):
                    expected = outcome if minimum <= length <= maximum else "unclassified"
                    self.assert_result(
                        call(203, 0x5000, length, sockaddr(family, ip)), "connect", expected
                    )

    def test_null_rules(self):
        for length in (0, 16, 0xFFFFFFFF, 1 << 32):
            for number, operation in zip(
                SYSCALLS, ("connect", "sendto", "sendmsg", "sendmmsg"), strict=True
            ):
                with self.subTest(operation=operation, length=length):
                    expected = (
                        "connected_send"
                        if (number in (211, 269) or (number == 206 and length & 0xFFFFFFFF == 0))
                        else "unclassified"
                    )
                    machine = call(number, 0, length)
                    self.assert_result(machine, operation, expected)
                    self.assertFalse(
                        any(event[0] == "user" and event[1] == 0 for event in machine.events)
                    )

    def test_unreadable_addresses_and_headers(self):
        for number, operation in zip(
            SYSCALLS, ("connect", "sendto", "sendmsg", "sendmmsg"), strict=True
        ):
            self.assert_result(call(number, 1, 16), operation, "read_error")
        for pointer in (0, 1, MASK - 5):
            self.assert_result(NetworkMachine(211, (MASK, pointer, 0)), "sendmsg", "read_error")
        machine = NetworkMachine(269, (MASK, 0x4000, 3, 0))
        self.assert_result(machine, "sendmmsg", "read_error", messages=3)

    def test_vector_processes_after_bad_header_and_bad_name(self):
        machine = NetworkMachine(269, (MASK, 0x4000, 5, 0))
        # Slot0 unreadable header; slot1 unreadable name; slot2 unknown family;
        # slot3 connected; slot4 external. All five MUST be counted.
        machine.allocate(0x4040, header(1, 16))
        machine.allocate(0x4080, header(0x6000, 2))
        machine.allocate(0x6000, sockaddr(65535))
        machine.allocate(0x40C0, header(0, 0xFFFFFFFF))
        machine.allocate(0x4100, header(0x5000, 16))
        machine.allocate(0x5000, sockaddr(2, "192.0.2.1"))
        self.assert_result(
            machine,
            "sendmmsg",
            messages=5,
            read_error=2,
            unclassified=1,
            connected_send=1,
            external=1,
        )

    def test_maximum_vector_final_external(self):
        for upper in (0, 0xDEADBEEF << 32):
            machine = NetworkMachine(269, (MASK, 0x10000, upper + 1024, 0))
            machine.allocate(0x10000, header(0, 0) * 1023 + header(0x8000, 16))
            machine.allocate(0x8000, sockaddr(2, "192.0.2.1"))
            self.assert_result(machine, "sendmmsg", messages=1024, connected_send=1023, external=1)
            self.assertEqual(
                sum(event[0] == "user" and event[2] == 12 for event in machine.events), 1024
            )

    def test_vector_zero_oversize_and_pointer_wrap(self):
        for pointer, length in (
            (1, 0),
            (1, 1025),
            (1, 0xFFFFFFFF),
            (1, 1 << 32),
            (MASK - 64, 2),
            (MASK - 10, 1),
        ):
            with self.subTest(pointer=pointer, length=length):
                machine = NetworkMachine(269, (MASK, pointer, length, 0))
                self.assert_result(machine, "sendmmsg", messages=0, vector_error=1)
                self.assertFalse(any(event[0] == "user" for event in machine.events))

    def test_syscall_and_scalar_length_high_words(self):
        self.assert_result(
            call(
                (0xFFFFFFFE << 32) | 203, 0x5000, (0xDEADBEEF << 32) | 16, sockaddr(2, "127.0.0.1")
            ),
            "connect",
            "loopback",
        )
        machine = NetworkMachine((1 << 32) | 269, (MASK, 0x4000, (1 << 32) | 2, MASK))
        machine.allocate(0x4000, header(0, -1) + header(0, 1))
        self.assert_result(machine, "sendmmsg", "connected_send", messages=2)

    def test_scope_precedes_context_and_register_reads(self):
        machine = NetworkMachine(203, (MASK, 1, 16), cgroup=TARGET + 1)
        machine.kernel.clear()  # Any direct context dereference would fail.
        self.assertEqual(machine.run(), dict.fromkeys(NETWORK_COUNTER_NAMES, 0))
        self.assertEqual(machine.events, [("scope",)])

    def test_unknown_syscalls_ignore_arguments(self):
        for number in (0, 1, 202, 204, 207, 268, 270, MASK):
            machine = NetworkMachine(number, (MASK,) * 6)
            self.assertEqual(machine.run(), dict.fromkeys(NETWORK_COUNTER_NAMES, 0))
            self.assertEqual(
                [event for event in machine.events if event[0] in ("kernel", "user")],
                [("kernel", REGS + 64, 8)],
            )

    def test_register_errors_are_separate_and_fatal(self):
        machine = NetworkMachine(203)
        machine.kernel_fail_ranges = [(REGS + 64, REGS + 72)]
        expected = dict.fromkeys(NETWORK_COUNTER_NAMES, 0)
        expected["regs_read_error"] = 1
        self.assertEqual(machine.run(), expected)
        for number, operation, offset in (
            (203, "connect", 8),
            (206, "sendto", 32),
            (211, "sendmsg", 8),
            (269, "sendmmsg", 8),
        ):
            machine = NetworkMachine(number)
            machine.kernel_fail_ranges = [(REGS + offset, REGS + offset + 8)]
            self.assert_result(machine, operation, messages=0, regs_read_error=1)

    def test_minimal_reads_do_not_capture_payload_paths_ports_or_ancillary(self):
        for family, ip, expected_ranges in (
            (1, None, [(0x5000, 2)]),
            (0, None, [(0x5000, 2)]),
            (2, "127.0.0.1", [(0x5000, 2), (0x5004, 4)]),
            (10, "::1", [(0x5000, 2), (0x5008, 16)]),
            (16, None, [(0x5000, 2), (0x5004, 4)]),
        ):
            data = sockaddr(family, ip)
            machine = call(211, 0x5000, len(data), data)
            machine.run()
            self.assertEqual(
                [(event[1], event[2]) for event in machine.events if event[0] == "user"],
                [(0x4000, 12), *expected_ranges],
            )
        machine = call(206, 0x5000, 16, sockaddr(2, "192.0.2.1"))
        machine.run()
        self.assertEqual(
            [event for event in machine.events if event[0] == "kernel"],
            [("kernel", REGS + 64, 8), ("kernel", REGS + 32, 16)],
        )

    def test_address_arithmetic_cannot_wrap_to_read_low_memory(self):
        machine = call(203, MASK - 4, 16)
        machine.allocate(MASK - 4, struct.pack("<H", 2))
        machine.allocate(0, b"\x7f\0\0\1")
        self.assert_result(machine, "connect", "read_error")
        self.assertFalse(any(event[0] == "user" and event[1] == 0 for event in machine.events))

    def test_partial_sockaddr_read_is_error_not_stale_data(self):
        machine = call(203, 0x5000, 16, struct.pack("<H", 2))
        self.assert_result(machine, "connect", "read_error")

    def test_pure_interface_validation_and_determinism(self):
        self.assertEqual(network_instructions(7, TARGET), network_instructions(7, TARGET))
        self.assertLess(len(network_instructions(7, TARGET)) // 8, 512)
        for arguments in (
            (-1, TARGET),
            (True, TARGET),
            (7, 0),
            (7, 1 << 64),
            (7, TARGET, 0),
            (7, TARGET, True),
        ):
            with self.assertRaises(ValueError):
                network_instructions(*arguments)


PID_TGID = (1234 << 32) | 5678
PENDING_VALUE = 0x3000000


class Machine(NetworkMachine):
    def __init__(
        self,
        scno,
        args=(),
        *,
        phase="entry",
        pending=None,
        counts=None,
        pid_tgid=PID_TGID,
        cgroup=TARGET,
        result=0,
    ):
        super().__init__(scno, args, cgroup)
        generator = instructions if phase == "entry" else exit_instructions
        self.code = list(struct.iter_unpack("<BBhI", generator(7, TARGET, pending_map_fd=8)))
        self.counts = [0] * len(COUNTER_NAMES) if counts is None else counts
        self.pending = {} if pending is None else pending
        self.pid_tgid = pid_tgid
        self.update_error = self.delete_error = False
        if phase == "exit":
            self.put(self.kernel, REGS, integer_bytes(result, 8))

    def load(self, address, size):
        if address == PENDING_VALUE and size == 8:
            self.events.append(("pending_value_read",))
            return self.pending[self.pid_tgid]
        return super().load(address, size)

    def helper(self, number):
        if number == 14:
            self.events.append(("pid_tgid",))
            result = self.pid_tgid
        elif number == 1 and self.value(1) == 8:
            key = self.load(self.value(2), 8)
            self.events.append(("pending_lookup", key))
            if key != self.pid_tgid:
                raise AssertionError("wrong_pending_identity")
            result = PENDING_VALUE if key in self.pending else 0
        elif number == 2:
            if self.value(1) != 8 or self.value(4) != 1:
                raise AssertionError("pending_requires_NOEXIST")
            key, value = self.load(self.value(2), 8), self.load(self.value(3), 8)
            self.events.append(("pending_insert", key, value))
            if self.update_error or key in self.pending or len(self.pending) >= 4096:
                result = (-17) & MASK
            else:
                self.pending[key] = value
                result = 0
        elif number == 3:
            if self.value(1) != 8:
                raise AssertionError("wrong_pending_map")
            key = self.load(self.value(2), 8)
            self.events.append(("pending_delete", key))
            if self.delete_error or key not in self.pending:
                result = (-2) & MASK
            else:
                del self.pending[key]
                result = 0
        else:
            return super().helper(number)
        self.reg[1:6] = [None] * 5
        self.reg[0] = result

    def run(self):
        # Same strict small VM as the frozen network tests, extended only for
        # STXDW stack/map state and unsigned register JGT used by this generator.
        pc = 0
        while True:
            self.steps += 1
            if self.steps > 500000:
                raise AssertionError("synthetic_instruction_budget_exceeded")
            opcode, packed, offset, immediate = self.code[pc]
            dst, src = packed & 15, packed >> 4
            signed = immediate if immediate < 2**31 else immediate - 2**32
            next_pc = pc + 1
            if opcode == 0x18:
                high = self.code[pc + 1]
                if high[:3] != (0, 0, 0) or src not in (0, 1):
                    raise AssertionError("invalid_lddw")
                self.reg[dst] = immediate | high[3] << 32
                next_pc += 1
            elif opcode == 0xBF:
                self.reg[dst] = self.value(src)
            elif opcode == 0xB7:
                self.reg[dst] = signed & MASK
            elif opcode in (0x79, 0x61, 0x69, 0x71):
                size = {0x79: 8, 0x61: 4, 0x69: 2, 0x71: 1}[opcode]
                self.reg[dst] = self.load((self.value(src) + offset) & MASK, size)
            elif opcode == 0x62:
                self.store((self.value(dst) + offset) & MASK, 4, immediate)
            elif opcode in (0x63, 0x7B):
                self.store(
                    (self.value(dst) + offset) & MASK, 8 if opcode == 0x7B else 4, self.value(src)
                )
            elif opcode == 0x07:
                self.reg[dst] = (self.value(dst) + signed) & MASK
            elif opcode == 0x0F:
                self.reg[dst] = (self.value(dst) + self.value(src)) & MASK
            elif opcode == 0x67:
                self.reg[dst] = (self.value(dst) << immediate) & MASK
            elif opcode == 0xDB:
                address = self.value(dst) + offset
                if immediate or not MAP <= address < MAP + 8 * len(self.counts) or address % 8:
                    raise AssertionError("invalid_atomic_counter_add")
                key = (address - MAP) // 8
                self.counts[key] = (self.counts[key] + self.value(src)) & MASK
            elif opcode == 0x85:
                self.helper(immediate)
            elif opcode == 0x95:
                if self.value(0) != 0:
                    raise AssertionError("nonzero_tracing_return")
                return dict(zip(COUNTER_NAMES, self.counts, strict=True))
            elif opcode == 0x05:
                next_pc += offset
            elif opcode in (0x15, 0x55, 0x25, 0xA5, 0x1D, 0x5D, 0xAD, 0x2D):
                left = self.value(dst)
                right = self.value(src) if opcode & 8 else signed & MASK
                if opcode in (0x15, 0x1D):
                    condition = left == right
                elif opcode in (0x55, 0x5D):
                    condition = left != right
                elif opcode in (0x25, 0x2D):
                    condition = left > right
                else:
                    condition = left < right
                if condition:
                    next_pc += offset
            else:
                raise AssertionError(f"unsupported_instruction_{opcode:02x}")
            if not 0 <= next_pc < len(self.code):
                raise AssertionError("jump_outside_program")
            pc = next_pc


class CompleteInstructionTests(unittest.TestCase):
    def test_reviewed_bytecode_is_preserved_exactly(self):
        # Fixed identities make instruction provenance reproducible without any
        # filesystem reference to the external, previously executed prototypes.
        programs = (
            (
                network_instructions(7, TARGET),
                "83d8335aae4e968b0e6a6ecc5df144a3048a9264a3cdf07bd8d56824c8273b66",
            ),
            (
                instructions(7, TARGET, pending_map_fd=8),
                "ce561573e4348bbc8ac38e0fab1b8c978c32141e0894b1c1b64ddae0bc724143",
            ),
            (
                exit_instructions(7, TARGET, pending_map_fd=8),
                "1015685a45623a650e3ae7c1ae7fc4a14b9ec5eb7095c181115fe737c53e1d9f",
            ),
        )
        for bytecode, expected in programs:
            self.assertEqual(hashlib.sha256(bytecode).hexdigest(), expected)

    def expect(self, machine, **expected):
        result = machine.run()
        self.assertEqual(result, dict(dict.fromkeys(COUNTER_NAMES, 0), **expected))
        return result

    def paired(
        self,
        pointer=0x5000,
        *,
        result=0,
        output=b"\x50\0\x18\0\x40\0",
        flags=0,
        number=277,
        operation=3,
    ):
        entry = Machine(number, (operation, flags, pointer))
        # Poison is intentionally NOT output evidence at entry.
        if pointer > 1:
            entry.allocate(pointer, b"\xa5" * 6)
        entry.run()
        final = Machine(
            number, phase="exit", pending=entry.pending, counts=entry.counts, result=result
        )
        if output is not None and pointer > 1:
            final.allocate(pointer, output)
        return entry, final

    def test_frozen_network_suffix_and_counters_unchanged(self):
        frozen = network_instructions(7, TARGET)
        self.assertTrue(instructions(7, TARGET, pending_map_fd=8).endswith(frozen))
        self.assertEqual(COUNTER_NAMES[:14], NETWORK_COUNTER_NAMES)
        for number in SYSCALLS:
            for family, ip in (
                (2, "127.1.2.3"),
                (2, "192.0.2.1"),
                (10, "::1"),
                (10, "::ffff:127.9.8.7"),
                (10, "2001:db8::1"),
                (1, None),
                (16, None),
                (0, None),
                (65535, None),
            ):
                original = call(number, 0x5000, len(sockaddr(family, ip)), sockaddr(family, ip))
                combined = Machine(number)
                combined.kernel, combined.user = original.kernel.copy(), original.user.copy()
                expected = dict(dict.fromkeys(COUNTER_NAMES, 0), **original.run())
                self.assertEqual(combined.run(), expected)

    def test_prctl_ignored_arguments_and_low32_option(self):
        for option in (3, 21, 51, 64, 16):
            for high in (0, 0xDEADBEEF << 32):
                machine = Machine(167, (option | high, MASK, MASK, MASK, MASK))
                self.expect(machine, entry_prctl=1, control_queries=1)
                self.assertFalse(any(event[0] == "user" for event in machine.events))
        for pointer in (0, 1, MASK):
            self.expect(Machine(167, (15, pointer, MASK, MASK, MASK)), entry_prctl=1, safe_prctl=1)

    def test_prctl_capability_probe_full64(self):
        for capability in (0, 40, 41, 0xFFFFFFFF):
            self.expect(
                Machine(167, (23, capability, MASK, MASK, MASK)), entry_prctl=1, control_queries=1
            )
        for capability in (1 << 32, (1 << 32) + 1, MASK):
            self.expect(Machine(167, (23, capability, 0, 0, 0)), entry_prctl=1, control_error=1)

    def test_nnp_exact_full_width_arguments(self):
        self.expect(Machine(167, (39, 0, 0, 0, 0)), entry_prctl=1, control_queries=1)
        self.expect(Machine(167, (38, 1, 0, 0, 0)), entry_prctl=1, safe_prctl=1)
        for option in (38, 39):
            for index in range(4):
                args = [1 if option == 38 else 0, 0, 0, 0]
                args[index] |= 1 << 32
                self.expect(Machine(167, (option, *args)), entry_prctl=1, control_error=1)

    def test_vma_name_one_byte_only(self):
        machine = Machine(167, (0x53564D41, 0, 1 << 40, MASK, 0x5000))
        machine.allocate(0x5000, b"\xff")
        self.expect(machine, entry_prctl=1, safe_prctl=1)
        self.assertEqual(
            [event for event in machine.events if event[0] == "user"], [("user", 0x5000, 1)]
        )
        for args in ((1 << 32, 1, 1, 0x5000), (0, 0, 1, 0x5000), (0, 1, 1, 0)):
            self.expect(Machine(167, (0x53564D41, *args)), entry_prctl=1, control_error=1)
        self.expect(Machine(167, (0x53564D41, 0, 1, 1, 1)), entry_prctl=1, control_read_error=1)

    def test_all_filter_attempts_and_unknown_controls(self):
        self.expect(Machine(167, (22, MASK, MASK, MASK, MASK)), entry_prctl=1, filter_changes=1)
        for operation in (0, 1, 1 << 32, (1 << 32) + 1):
            self.expect(Machine(277, (operation, MASK, MASK)), entry_seccomp=1, filter_changes=1)
        for option in (0, 4, 24, 59, MASK):
            self.expect(Machine(167, (option, 0, 0, 0, 0)), entry_prctl=1, control_error=1)
        self.expect(Machine(277, (4, 0, 0)), entry_seccomp=1, control_error=1)

    def test_seccomp_known_actions_unknown_bits_and_bad_reads(self):
        for action in SECCOMP_ACTIONS:
            machine = Machine((1 << 32) + 277, ((1 << 32) + 2, 1 << 32, 0x5000))
            machine.allocate(0x5000, integer_bytes(action, 4))
            self.expect(machine, entry_seccomp=1, control_queries=1)
        for action in (1, 0x50001, 0xFFFFFFFF):
            machine = Machine(277, (2, 0, 0x5000))
            machine.allocate(0x5000, integer_bytes(action, 4))
            self.expect(machine, entry_seccomp=1, control_error=1)
        self.expect(Machine(277, (2, 1, 1)), entry_seccomp=1, control_error=1)
        self.expect(Machine(277, (2, 0, 1)), entry_seccomp=1, control_read_error=1)

    def test_notif_entry_never_reads_or_classifies_poison_output(self):
        entry, final = self.paired()
        self.assertEqual(entry.counts[16], 0)
        self.assertEqual(entry.counts[22:24], [1, 0])
        self.assertEqual(entry.pending, {PID_TGID: 0x5000})
        self.assertFalse(any(event[0] == "user" for event in entry.events))
        self.expect(
            final,
            entry_seccomp=1,
            output_queries_started=1,
            output_queries_completed=1,
            control_queries=1,
        )
        self.assertEqual(final.pending, {})
        self.assertEqual(
            [event for event in final.events if event[0] == "user"], [("user", 0x5000, 6)]
        )

    def test_notif_any_unsigned_sizes_after_success_and_highword_abi(self):
        for output in (bytes(6), b"\xff" * 6, b"\xa5" * 6):
            _entry, final = self.paired(
                output=output, number=(1 << 32) + 277, operation=(1 << 32) + 3, flags=1 << 32
            )
            self.expect(
                final,
                entry_seccomp=1,
                output_queries_started=1,
                output_queries_completed=1,
                control_queries=1,
            )
        _entry, final = self.paired(flags=1)
        self.expect(final, entry_seccomp=1, control_error=1)
        self.assertFalse(final.pending)

    def test_notif_failed_syscall_and_unreadable_success_fail_closed(self):
        for result in (-14, 1, 1 << 32):
            _entry, final = self.paired(result=result)
            self.expect(
                final,
                entry_seccomp=1,
                output_queries_started=1,
                output_queries_completed=1,
                control_error=1,
            )
            self.assertFalse(any(event[0] == "user" for event in final.events))
            self.assertFalse(final.pending)
        for pointer in (0, 1, 0x5000):
            _entry, final = self.paired(pointer, output=None)
            self.expect(
                final,
                entry_seccomp=1,
                output_queries_started=1,
                output_queries_completed=1,
                control_read_error=1,
            )
            self.assertFalse(final.pending)

    def test_pending_duplicate_stale_and_tid_reuse_are_sticky(self):
        stale = {PID_TGID: 0x6000}
        machine = Machine(277, (3, 0, 0x5000), pending=stale)
        self.expect(machine, entry_seccomp=1, output_queries_started=1, control_state_error=2)
        self.assertEqual(stale, {PID_TGID: 0x6000})
        # Even unrelated next entry catches a missed exit or recycled task ID.
        self.expect(Machine(999, pending=stale), control_state_error=1)
        self.assertEqual(stale, {PID_TGID: 0x6000})

    def test_pending_insertion_capacity_and_failure_accounting(self):
        for pending, fail in (({i: 0x5000 for i in range(4096)}, False), ({}, True)):
            machine = Machine(277, (3, 0, 0x5000), pending=pending)
            machine.update_error = fail
            self.expect(machine, entry_seccomp=1, output_queries_started=1, control_state_error=1)
            self.assertNotIn(PID_TGID, pending)

    def test_missing_exit_keeps_unbalanced_pending_evidence(self):
        machine = Machine(277, (3, 0, 0x5000))
        self.expect(machine, entry_seccomp=1, output_queries_started=1)
        self.assertTrue(machine.pending)
        self.assertNotEqual(machine.counts[22], machine.counts[23])

    def test_exit_different_syscall_or_register_fault_still_cleans_pair(self):
        for scno, failure in ((203, "control_state_error"), (277, "control_read_error")):
            final = Machine(scno, phase="exit", pending={PID_TGID: 0x5000})
            if scno == 277:
                final.kernel_fail_ranges = [(REGS, REGS + 8)]
            self.expect(final, output_queries_completed=1, **{failure: 1})
            self.assertEqual(final.pending, {})
            self.assertFalse(any(event[0] == "user" for event in final.events))

    def test_delete_failure_is_sticky_even_after_valid_output(self):
        _entry, final = self.paired()
        final.delete_error = True
        self.expect(
            final,
            entry_seccomp=1,
            output_queries_started=1,
            output_queries_completed=1,
            control_queries=1,
            control_state_error=1,
        )
        self.assertTrue(final.pending)

    def test_unrelated_or_different_task_exit_does_not_touch_context(self):
        for pending in ({}, {PID_TGID + 1: 0x5000}):
            final = Machine(277, phase="exit", pending=pending)
            final.kernel.clear()
            self.expect(final)
            self.assertFalse(
                any(event[0] in ("context_read", "kernel", "user") for event in final.events)
            )

    def test_scope_precedes_identity_context_and_all_memory(self):
        for phase in ("entry", "exit"):
            machine = Machine(277, phase=phase, cgroup=TARGET + 1, pending={PID_TGID: 0x5000})
            machine.kernel.clear()
            self.expect(machine)
            self.assertEqual(machine.events, [("scope",)])

    def test_selected_control_register_failure_and_initial_failure(self):
        for number, entry_name in ((167, "entry_prctl"), (277, "entry_seccomp")):
            machine = Machine(number)
            machine.kernel_fail_ranges = [(REGS, REGS + 8)]
            self.expect(machine, **{entry_name: 1, "control_read_error": 1})
        machine = Machine(167)
        machine.kernel_fail_ranges = [(REGS + 64, REGS + 72)]
        self.expect(machine, regs_read_error=1)

    def test_interface_and_helper_whitelist(self):
        for generator in (instructions, exit_instructions):
            for pending in (None, -1, 7, True, 1 << 31):
                with self.assertRaises(ValueError):
                    generator(7, TARGET, pending_map_fd=pending)
            code = list(struct.iter_unpack("<BBhI", generator(7, TARGET, pending_map_fd=8)))
            helpers = {word[3] for word in code if word[0] == 0x85}
            self.assertLessEqual(helpers, {1, 2, 3, 14, 80, 112, 113})
            self.assertNotIn(16, helpers)
            self.assertLess(len(code), 1024)
