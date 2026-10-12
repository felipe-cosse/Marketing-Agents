"""Pure native ARM64 AC-15 destination and control observation bytecode.

No loader, file access, BPF syscall, payload read, pathname read or output lives
here.  The caller owns map/link validation, native ABI qualification, stable
target-cgroup identity, start/end coverage, recursion-miss and counter checks.
The ARRAY map must have len(COUNTER_NAMES) zeroed unsigned 64-bit values.

Linux v7.0 primary references (not exact LinuxKit source qualification):
  arch/arm64/kernel/syscall.c: do_el0_svc -> el0_svc_common(int scno)
  include/uapi/asm-generic/unistd.h: 203/206/211/269
  include/linux/socket.h: user_msghdr (56 bytes), mmsghdr (64 bytes)
  net/socket.c: move_addr_to_kernel, __copy_msghdr, __sys_sendto/sendmmsg
All are at https://github.com/torvalds/linux/blob/v7.0/ .

Only low32 syscall numbers, signed-int sockaddr lengths and unsigned-int vlen
participate.  Pointers remain 64-bit.  Only the first 12 bytes of each native
message header are read; iovecs, ancillary data, ports and UNIX paths are not.
sockaddr reads are family (2), IPv4 address (4), IPv6 address (16), or NETLINK
pid (4).  These are attempt classifications, not claims the kernel accepted a
buffer, descriptor or call.  Concurrent user-memory mutation remains a gap.

Deliberately conservative rules preserve the current strict audit contract:
sendto(NULL) needs low32 length zero, even though Linux ignores that length;
NULL msg_name is connected regardless of namelen, as __copy_msghdr specifies;
non-NULL zero/over-128 lengths fail classification, although __copy_msghdr
canonicalizes zero to NULL and clamps oversized positive lengths.  Empty or
oversized sendmmsg vectors fail, rather than using the kernel's 1024 clamp.
Every slot of a valid 1..1024 vector is observed, including after bad headers
or destinations: a kernel partial-send/early error must not hide an attempt.

For successful register reads, each scalar or valid-vector slot contributes
one messages count and one outcome.  A register-read error is separate and
fatal to completeness; selected entry counts already incremented remain.
Invalid vector bounds contribute vector_error, without inventing messages.
The caller must reject any error/unclassified/external count.  Connected send
is a structural category only: this module does not prove a socket's peer.

Control counters append to the fourteen network counters. The caller supplies a
private non-LRU HASH with u64 pid_tgid keys, u64 output pointers and 4096 slots.
Both native do_el0_svc hooks must attach before target release. Entry checks
stale pending state on every owned syscall; NOEXIST never overwrites it. Exit
looks up pending state before reading registers, checks native low32 syscall
277, and reads saved pt_regs x0 (do_el0_svc is void). Only success plus six
readable NOTIF_SIZES output bytes counts as a query. Any three u16 values suffice.
Quiescent snapshots must have an empty HASH, balanced started/completed counts,
zero state/read errors and zero recursion misses on both programs.

PRCTL's option is int32 and remaining arguments are full64; seccomp op/flags
are uint32. GET_NAME/SET_NAME never read a name. SET_VMA reads one name byte,
without inspecting/emitting it, matching the existing strict -s0 audit policy.
No filter install attempt is allowed by this classifier. Trusted setup and
negative controls must be separated by the enclosing runner's explicit phases.
Fixed read-site/cause counters append to the unchanged 24 authoritative counters.
They distinguish helper EFAULT, other nonzero returns and pre-helper wrapping,
without recording pointers, memory contents or raw helper results. Every such
failure still increments control_read_error exactly once and remains fatal.
Sources: Linux v7.0 kernel/sys.c, kernel/seccomp.c and include/uapi/linux/{prctl,
seccomp,bpf}.h. Helper14 is pid_tgid; helper16 (comm) is never used.
"""

import struct

NETWORK_COUNTER_NAMES = (
    "entry_connect",
    "entry_sendto",
    "entry_sendmsg",
    "entry_sendmmsg",
    "messages",
    "loopback",
    "external",
    "unix",
    "kernel_ipc",
    "connected_send",
    "unclassified",
    "read_error",
    "vector_error",
    "regs_read_error",
)
SYSCALLS = (203, 206, 211, 269)
MAX_VECTOR = 1024
NATIVE_MSGHDR_SIZE = 56
NATIVE_MMSGHDR_SIZE = 64


def network_instructions(map_fd, target_cgroup, regs_offset=64):
    """Return deterministic little-endian eBPF instructions, without loading.

    fentry context argument0 is a BTF-validated native struct pt_regs pointer.
    The current cgroup is checked before even dereferencing that context.
    Only helpers 80 (scope), 113/112 (bounded reads), and 1 (map lookup) occur.
    All counters use atomic 64-bit additions, including independent entries.
    """
    if type(map_fd) is not int or not 0 <= map_fd < 2**31:
        raise ValueError("invalid_map_fd")
    if type(target_cgroup) is not int or not 0 < target_cgroup < 2**64:
        raise ValueError("invalid_target_cgroup")
    if type(regs_offset) is not int or regs_offset != 64:
        raise ValueError("native_arm64_regs8_offset_required")
    code, labels, jumps = [], {}, []

    def op(value, dst=0, src=0, off=0, imm=0):
        code.append([value, src << 4 | dst, off, imm & 0xFFFFFFFF])

    def wide(dst, value, src=0):
        op(0x18, dst, src, imm=value)
        op(0, imm=value >> 32)

    def label(name):
        if name in labels:
            raise ValueError("duplicate_label")
        labels[name] = len(code)

    def jump(value, name, dst=0, src=0, imm=0):
        jumps.append((len(code), name))
        op(value, dst, src, imm=imm)

    def increment(name):
        op(0x62, 10, off=-4, imm=NETWORK_COUNTER_NAMES.index(name))
        wide(1, map_fd, src=1)
        op(0xBF, 2, 10)
        op(0x07, 2, imm=-4)
        op(0x85, imm=1)
        # ARRAY keys are statically in range; caller validates map schema.
        jump(0x15, "exit", dst=0, imm=0)
        op(0xB7, 1, imm=1)
        op(0xDB, 0, 1)  # BPF_STX | BPF_ATOMIC | BPF_DW, BPF_ADD.

    def read(helper, destination, size, pointer, delta=0, failure="read_error"):
        # Reject wrapping pointer arithmetic before any memory access.  This
        # does not strip top-byte tags or invent user-address validity.
        op(0xBF, 3, pointer)
        if helper == 112:
            op(0xBF, 0, pointer)
            op(0x07, 0, imm=delta + size - 1)
            jump(0xAD, failure, dst=0, src=pointer)  # unsigned end < base
        if delta:
            op(0x07, 3, imm=delta)
        op(0xBF, 1, 10)
        op(0x07, 1, imm=destination)
        op(0xB7, 2, imm=size)
        op(0x85, imm=helper)
        jump(0x55, failure, dst=0, imm=0)

    # R6=context initially, R7=pt_regs until selected arguments are read.
    op(0xBF, 6, 1)
    op(0x85, imm=80)
    wide(2, target_cgroup)
    jump(0x5D, "exit", dst=0, src=2)
    op(0x79, 7, 6)
    read(113, -16, 8, 7, regs_offset, "regs_read_error")
    op(0x61, 8, 10, off=-16)  # scno is int: ignore high32 of regs[8].
    for number, name in zip(SYSCALLS, NETWORK_COUNTER_NAMES[:4], strict=True):
        jump(0x15, name, dst=8, imm=number)
    jump(0x05, "exit")

    # Working loop state, preserved across helper calls:
    # R6=0(connect),1(sendto),2(header); R7=current header; R8=sockaddr;
    # R9=remaining slots (1..1024); stack[-20:-16]=low32 sockaddr length.
    for name, offset, mode in (("entry_connect", 8, 0), ("entry_sendto", 32, 1)):
        label(name)
        increment(name)
        read(113, -48, 16, 7, offset, "regs_read_error")
        op(0x79, 8, 10, off=-48)
        op(0x61, 0, 10, off=-40)
        op(0x63, 10, 0, off=-20)
        op(0xB7, 6, imm=mode)
        op(0xB7, 9, imm=1)
        jump(0x05, "message")

    label("entry_sendmsg")
    increment("entry_sendmsg")
    read(113, -48, 8, 7, 8, "regs_read_error")
    op(0x79, 7, 10, off=-48)
    op(0xB7, 6, imm=2)
    op(0xB7, 9, imm=1)
    jump(0x05, "message")

    label("entry_sendmmsg")
    increment("entry_sendmmsg")
    read(113, -48, 16, 7, 8, "regs_read_error")
    op(0x79, 7, 10, off=-48)
    op(0x61, 9, 10, off=-40)  # unsigned int vlen, not raw64.
    jump(0x15, "vector_error", dst=9, imm=0)
    jump(0x25, "vector_error", dst=9, imm=MAX_VECTOR)
    op(0xBF, 0, 9)
    op(0x07, 0, imm=-1)
    op(0x67, 0, imm=6)  # (vlen-1) * sizeof(native mmsghdr)
    op(0x07, 0, imm=11)  # Last byte of final destination-header prefix.
    op(0x0F, 0, 7)
    jump(0xAD, "vector_error", dst=0, src=7)
    op(0xB7, 6, imm=2)

    label("message")
    increment("messages")
    jump(0x55, "address", dst=6, imm=2)
    read(112, -48, 12, 7)
    op(0x79, 8, 10, off=-48)
    op(0x61, 0, 10, off=-40)
    op(0x63, 10, 0, off=-20)

    label("address")
    op(0x61, 0, 10, off=-20)
    jump(0x55, "nonnull", dst=8, imm=0)
    jump(0x15, "connected_send", dst=6, imm=2)
    jump(0x55, "unclassified", dst=6, imm=1)
    jump(0x15, "connected_send", dst=0, imm=0)
    jump(0x05, "unclassified")

    label("nonnull")
    # Unsigned low32 >128 also rejects every negative signed-int length.
    jump(0x25, "unclassified", dst=0, imm=128)
    jump(0xA5, "unclassified", dst=0, imm=2)
    read(112, -64, 2, 8)
    op(0x69, 0, 10, off=-64)
    for family, name in (
        (0, "connected_send"),
        (1, "family_unix"),
        (2, "family_inet"),
        (10, "family_inet6"),
        (16, "family_netlink"),
    ):
        jump(0x15, name, dst=0, imm=family)
    jump(0x05, "unclassified")

    label("family_unix")
    op(0x61, 0, 10, off=-20)
    jump(0xA5, "unclassified", dst=0, imm=3)
    jump(0x25, "unclassified", dst=0, imm=110)
    jump(0x05, "unix")  # No sun_path read, including abstract UNIX names.

    label("family_inet")
    op(0x61, 0, 10, off=-20)
    jump(0xA5, "unclassified", dst=0, imm=16)
    read(112, -80, 4, 8, 4)
    op(0x71, 0, 10, off=-80)
    jump(0x15, "loopback", dst=0, imm=127)
    jump(0x05, "external")

    label("family_inet6")
    op(0x61, 0, 10, off=-20)
    jump(0xA5, "unclassified", dst=0, imm=24)
    read(112, -80, 16, 8, 8)
    op(0x79, 0, 10, off=-80)
    jump(0x55, "external", dst=0, imm=0)
    op(0x79, 0, 10, off=-72)
    wide(1, 1 << 56)  # Network-order ::1, loaded little-endian.
    jump(0x1D, "loopback", dst=0, src=1)
    op(0x69, 0, 10, off=-72)
    jump(0x55, "external", dst=0, imm=0)
    op(0x69, 0, 10, off=-70)
    jump(0x55, "external", dst=0, imm=65535)
    op(0x71, 0, 10, off=-68)
    jump(0x15, "loopback", dst=0, imm=127)
    jump(0x05, "external")

    label("family_netlink")
    op(0x61, 0, 10, off=-20)
    jump(0xA5, "unclassified", dst=0, imm=12)
    read(112, -80, 4, 8, 4)
    op(0x61, 0, 10, off=-80)
    jump(0x15, "kernel_ipc", dst=0, imm=0)
    jump(0x05, "unclassified")

    for name in NETWORK_COUNTER_NAMES[5:12]:
        label(name)
        increment(name)
        jump(0x05, "next")

    label("next")
    op(0x07, 9, imm=-1)
    jump(0x15, "exit", dst=9, imm=0)
    op(0x07, 7, imm=NATIVE_MMSGHDR_SIZE)
    jump(0x05, "message")  # Bound established once at entry; never retries.

    for name in ("vector_error", "regs_read_error"):
        label(name)
        increment(name)
        jump(0x05, "exit")
    label("exit")
    op(0xB7, 0, imm=0)
    op(0x95)
    for index, name in jumps:
        displacement = labels[name] - index - 1
        if not -32768 <= displacement <= 32767:
            raise ValueError("jump_out_of_range")
        code[index][2] = displacement
    return b"".join(struct.pack("<BBhI", *item) for item in code)


# Strict control mapping, preserving network counters 0..13 and bytecode.
BASE_COUNTER_NAMES = (
    *NETWORK_COUNTER_NAMES,
    "entry_prctl",
    "entry_seccomp",
    "control_queries",
    "safe_prctl",
    "filter_changes",
    "control_error",
    "control_read_error",
    "control_state_error",
    "output_queries_started",
    "output_queries_completed",
)
# Diagnostic categories contain no user data. The original 24 counters remain
# authoritative; each control-read failure adds exactly one diagnostic bin.
READ_DIAGNOSTIC_SITES = (
    "prctl_args",
    "seccomp_args",
    "vma_name",
    "action_value",
    "exit_syscall",
    "exit_result",
    "notify_sizes",
)
READ_DIAGNOSTIC_CAUSES = ("efault", "other", "wrap")
READ_DIAGNOSTIC_NAMES = tuple(
    "control_read_" + site + "_" + cause
    for site in READ_DIAGNOSTIC_SITES
    for cause in READ_DIAGNOSTIC_CAUSES
)
COUNTER_NAMES = BASE_COUNTER_NAMES + READ_DIAGNOSTIC_NAMES
PENDING_MAX_ENTRIES = 4096
SECCOMP_ACTIONS = (
    0x80000000,
    0,
    0x00030000,
    0x00050000,
    0x7FC00000,
    0x7FF00000,
    0x7FFC0000,
    0x7FFF0000,
)


class _Builder:
    def __init__(self, map_fd, pending_map_fd):
        self.map_fd, self.pending_map_fd = map_fd, pending_map_fd
        self.code, self.labels, self.jumps = [], {}, []
        self.read_sites = []

    def op(self, value, dst=0, src=0, off=0, imm=0):
        self.code.append([value, src << 4 | dst, off, imm & 0xFFFFFFFF])

    def wide(self, dst, value, src=0):
        self.op(0x18, dst, src, imm=value)
        self.op(0, imm=value >> 32)

    def label(self, name):
        if name in self.labels:
            raise ValueError("duplicate_label")
        self.labels[name] = len(self.code)

    def jump(self, value, name, dst=0, src=0, imm=0):
        self.jumps.append((len(self.code), name))
        self.op(value, dst, src, imm=imm)

    def pointer(self, register, offset):
        self.op(0xBF, register, 10)
        self.op(0x07, register, imm=offset)

    def increment(self, name):
        self.op(0x62, 10, off=-4, imm=COUNTER_NAMES.index(name))
        self.wide(1, self.map_fd, src=1)
        self.pointer(2, -4)
        self.op(0x85, imm=1)
        self.jump(0x15, "exit", dst=0, imm=0)
        self.op(0xB7, 1, imm=1)
        self.op(0xDB, 0, 1)

    def read(
        self,
        helper,
        destination,
        size,
        pointer,
        delta=0,
        failure="control_read_error",
        *,
        site=None,
    ):
        if failure == "control_read_error":
            if site not in READ_DIAGNOSTIC_SITES or any(site == old[0] for old in self.read_sites):
                raise ValueError("unique_control_read_site_required")
            self.read_sites.append((site, helper == 112))
        elif site is not None:
            raise ValueError("diagnostic_site_requires_control_read_failure")
        helper_failure = "diagnostic_" + site if site is not None else failure
        wrap_failure = helper_failure + "_wrap" if site is not None else failure
        self.op(0xBF, 3, pointer)
        if helper == 112:
            self.op(0xBF, 0, pointer)
            self.op(0x07, 0, imm=delta + size - 1)
            self.jump(0xAD, wrap_failure, dst=0, src=pointer)
        if delta:
            self.op(0x07, 3, imm=delta)
        self.pointer(1, destination)
        self.op(0xB7, 2, imm=size)
        self.op(0x85, imm=helper)
        self.jump(0x55, helper_failure, dst=0, imm=0)

    def read_diagnostics(self):
        for site, user_read in self.read_sites:
            label = "diagnostic_" + site
            self.label(label)
            # Classify R0 before any counter helper clobbers it. Every nonzero
            # result remains fatal, including unexpected positive returns.
            self.jump(0x15, label + "_efault", dst=0, imm=-14)
            self.increment("control_read_" + site + "_other")
            self.jump(0x05, "control_read_error")
            self.label(label + "_efault")
            self.increment("control_read_" + site + "_efault")
            self.jump(0x05, "control_read_error")
            if user_read:
                # A wrap is rejected before calling the helper: never interpret
                # the pointer arithmetic in R0 as an errno or emit its value.
                self.label(label + "_wrap")
                self.increment("control_read_" + site + "_wrap")
                self.jump(0x05, "control_read_error")

    def scope_and_key(self, target):
        self.op(0xBF, 6, 1)  # Preserve original fentry/fexit context.
        self.op(0x85, imm=80)
        self.wide(2, target)
        self.jump(0x5D, "exit", dst=0, src=2)
        self.op(0x85, imm=14)
        self.op(0x7B, 10, 0, off=-16)  # Key never overlaps counter slot -4.

    def pending_lookup(self):
        self.wide(1, self.pending_map_fd, src=1)
        self.pointer(2, -16)
        self.op(0x85, imm=1)

    def finish(self, suffix=b""):
        for index, name in self.jumps:
            displacement = self.labels[name] - index - 1
            if not -32768 <= displacement <= 32767:
                raise ValueError("jump_out_of_range")
            self.code[index][2] = displacement
        return b"".join(struct.pack("<BBhI", *item) for item in self.code) + suffix


def _validate(map_fd, target_cgroup, regs_offset, pending_map_fd):
    if type(pending_map_fd) is not int or not 0 <= pending_map_fd < 2**31:
        raise ValueError("invalid_pending_map_fd")
    if pending_map_fd == map_fd:
        raise ValueError("distinct_map_descriptors_required")
    # Reuse the network generator's exact input validation without any kernel action.
    return network_instructions(map_fd, target_cgroup, regs_offset)


def instructions(map_fd, target_cgroup, regs_offset=64, *, pending_map_fd=None):
    """Return fentry bytes. The network program is an unchanged suffix."""
    network = _validate(map_fd, target_cgroup, regs_offset, pending_map_fd)
    b = _Builder(map_fd, pending_map_fd)
    b.scope_and_key(target_cgroup)
    b.pending_lookup()
    b.jump(0x15, "no_stale_pending", dst=0, imm=0)
    b.increment("control_state_error")  # Sticky; never discard the old record.
    b.label("no_stale_pending")
    b.op(0x79, 7, 6)
    b.read(113, -24, 8, 7, regs_offset, "regs_read_error")
    b.op(0x61, 8, 10, off=-24)
    b.jump(0x15, "prctl", dst=8, imm=167)
    b.jump(0x15, "seccomp", dst=8, imm=277)
    for number in (203, 206, 211, 269):
        b.jump(0x15, "network", dst=8, imm=number)
    b.jump(0x05, "exit")

    b.label("prctl")
    b.increment("entry_prctl")
    b.read(113, -80, 40, 7, site="prctl_args")
    b.op(0x61, 0, 10, off=-80)
    b.jump(0x15, "filter_changes", dst=0, imm=22)
    for option in (3, 21, 51, 64, 16):
        b.jump(0x15, "control_queries", dst=0, imm=option)
    b.jump(0x15, "safe_prctl", dst=0, imm=15)
    for option, name in (
        (23, "cap_query"),
        (39, "get_nnp"),
        (38, "set_nnp"),
        (0x53564D41, "set_vma"),
    ):
        b.jump(0x15, name, dst=0, imm=option)
    b.jump(0x05, "control_error")

    b.label("cap_query")
    b.op(0x79, 0, 10, off=-72)
    b.wide(1, 0xFFFFFFFF)
    b.jump(0x2D, "control_error", dst=0, src=1)
    b.jump(0x05, "control_queries")
    for name, first, outcome in (("get_nnp", 0, "control_queries"), ("set_nnp", 1, "safe_prctl")):
        b.label(name)
        for offset, expected in ((-72, first), (-64, 0), (-56, 0), (-48, 0)):
            b.op(0x79, 0, 10, off=offset)
            b.jump(0x55, "control_error", dst=0, imm=expected)
        b.jump(0x05, outcome)

    b.label("set_vma")
    b.op(0x79, 0, 10, off=-72)
    b.jump(0x55, "control_error", dst=0, imm=0)
    b.op(0x79, 0, 10, off=-64)
    b.jump(0x15, "control_error", dst=0, imm=0)
    b.op(0x79, 8, 10, off=-48)
    b.jump(0x15, "control_error", dst=8, imm=0)
    b.read(112, -96, 1, 8, site="vma_name")
    b.jump(0x05, "safe_prctl")

    b.label("seccomp")
    b.increment("entry_seccomp")
    b.read(113, -80, 24, 7, site="seccomp_args")
    b.op(0x61, 0, 10, off=-80)
    b.jump(0x15, "filter_changes", dst=0, imm=0)
    b.jump(0x15, "filter_changes", dst=0, imm=1)
    b.op(0x61, 1, 10, off=-72)
    b.jump(0x55, "control_error", dst=1, imm=0)
    b.jump(0x15, "action_query", dst=0, imm=2)
    b.jump(0x15, "output_query", dst=0, imm=3)
    b.jump(0x05, "control_error")

    b.label("action_query")
    b.op(0x79, 8, 10, off=-64)
    b.read(112, -96, 4, 8, site="action_value")
    b.op(0x61, 0, 10, off=-96)
    for action in SECCOMP_ACTIONS:
        # Use wide constants: a BPF_JMP64 immediate >=2**31 sign-extends.
        b.wide(1, action)
        b.jump(0x1D, "control_queries", dst=0, src=1)
    b.jump(0x05, "control_error")

    b.label("output_query")
    # A started query means an eligible output request was observed, including
    # insertion failure. Thus failure cannot disappear by balancing only inserts.
    b.increment("output_queries_started")
    b.wide(1, pending_map_fd, src=1)
    b.pointer(2, -16)
    b.pointer(3, -64)  # x2 output pointer, saved before helpers clobber registers.
    b.op(0xB7, 4, imm=1)  # BPF_NOEXIST, never overwrite a stale request.
    b.op(0x85, imm=2)
    b.jump(0x55, "control_state_error", dst=0, imm=0)
    b.jump(0x05, "exit")

    b.read_diagnostics()
    for name in (
        "control_queries",
        "safe_prctl",
        "filter_changes",
        "control_error",
        "control_read_error",
        "control_state_error",
        "regs_read_error",
    ):
        b.label(name)
        b.increment(name)
        b.jump(0x05, "exit")
    b.label("exit")
    b.op(0xB7, 0, imm=0)
    b.op(0x95)
    b.label("network")
    b.op(0xBF, 1, 6)  # The unchanged network suffix expects original context R1.
    return b.finish(network)


def exit_instructions(map_fd, target_cgroup, regs_offset=64, *, pending_map_fd=None):
    """Return fexit bytes for pending NOTIF_SIZES queries only; no entry guesses."""
    _validate(map_fd, target_cgroup, regs_offset, pending_map_fd)
    b = _Builder(map_fd, pending_map_fd)
    b.scope_and_key(target_cgroup)
    b.pending_lookup()
    b.jump(0x15, "exit", dst=0, imm=0)
    b.op(0x79, 8, 0)  # Preserve output pointer before counter helpers.
    b.increment("output_queries_completed")
    b.op(0x79, 7, 6)
    b.read(113, -24, 8, 7, regs_offset, site="exit_syscall")
    b.op(0x61, 0, 10, off=-24)
    b.jump(0x55, "control_state_error", dst=0, imm=277)
    b.read(113, -24, 8, 7, site="exit_result")
    b.op(0x79, 0, 10, off=-24)
    b.jump(0x55, "control_error", dst=0, imm=0)
    b.read(112, -96, 6, 8, site="notify_sizes")
    b.increment("control_queries")
    b.jump(0x05, "delete_pending")
    b.read_diagnostics()
    for name in ("control_error", "control_read_error", "control_state_error"):
        b.label(name)
        b.increment(name)
        b.jump(0x05, "delete_pending")
    b.label("delete_pending")
    b.wide(1, pending_map_fd, src=1)
    b.pointer(2, -16)
    b.op(0x85, imm=3)
    b.jump(0x15, "exit", dst=0, imm=0)
    b.increment("control_state_error")
    b.label("exit")
    b.op(0xB7, 0, imm=0)
    b.op(0x95)
    return b.finish()
