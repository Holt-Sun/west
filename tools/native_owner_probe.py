# Copyright (c) 2026 NXP
#
# SPDX-License-Identifier: Apache-2.0

"""Native observations, not a replacement West backend or passing contract test."""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import platform
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = 'b9fc4474a3b36eacaea7eded0435cc41f80e4449'
GRACE = 2.0


def write_json(path: Path, value: object) -> None:
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2), encoding='utf-8')
    temporary.replace(path)


def wait_file(path: Path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError(f'fixture did not publish {path.name}')
        time.sleep(0.02)


def helper(directory: Path, behavior: str) -> None:
    stopping = False
    received_term = False

    def terminate(_signal: int, _frame: object) -> None:
        nonlocal stopping, received_term
        received_term = True
        write_json(directory / 'term.json', {'received': True})
        stopping = behavior == 'cooperative'

    signal.signal(signal.SIGTERM, terminate)
    write_json(
        directory / 'ready.json',
        {
            'pid': os.getpid(),
            'pgid': os.getpgid(0) if hasattr(os, 'getpgid') else None,
            'sid': os.getsid(0) if hasattr(os, 'getsid') else None,
        },
    )
    deadline = time.monotonic() + 20
    while not stopping and not (directory / 'release').exists():
        if time.monotonic() >= deadline:
            raise TimeoutError('fixture exceeded its own bounded lifetime')
        time.sleep(0.02)
    write_json(directory / 'exit.json', {'term': received_term})


def launch_helper(directory: Path, behavior: str, hold: bool) -> None:
    with (directory / 'helper.stderr').open('wb') as errors:
        subprocess.Popen(
            [
                sys.executable,
                __file__,
                'helper',
                '--directory',
                str(directory),
                '--behavior',
                behavior,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=errors,
            close_fds=True,
        )
    wait_file(directory / 'ready.json')
    print('owned helper ready; Git alias launcher complete', flush=True)
    if hold:
        deadline = time.monotonic() + 20
        while not (directory / 'release').exists():
            if time.monotonic() >= deadline:
                raise TimeoutError('held launcher exceeded its own bounded lifetime')
            time.sleep(0.02)


def command(directory: Path, behavior: str, *, hold: bool = False) -> list[str]:
    arguments = [
        sys.executable,
        __file__,
        'launcher',
        '--directory',
        str(directory),
        '--behavior',
        behavior,
    ]
    if hold:
        arguments.append('--hold')
    return ['git', '-c', f'alias.ownerproof=!{shlex.join(arguments)}', 'ownerproof']


def members(pgid: int) -> list[tuple[int, str]]:
    output = subprocess.check_output(
        ['ps', '-A', '-o', 'pid=,pgid=,stat='],
        timeout=5,
        text=True,
    )
    result = []
    for line in output.splitlines():
        pid, group, state = line.split()
        if int(group) == pgid:
            result.append((int(pid), state))
    return result


def live_members(pgid: int) -> list[int]:
    return [pid for pid, state in members(pgid) if not state.startswith('Z')]


def wait_no_live_members(pgid: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while live_members(pgid):
        if time.monotonic() >= deadline:
            raise TimeoutError('controlled helper did not stop')
        time.sleep(0.02)


def identity(directory: Path, leader: int) -> int:
    wait_file(directory / 'ready.json')
    value = json.loads((directory / 'ready.json').read_text(encoding='utf-8'))
    pid = value['pid']
    if not isinstance(pid, int) or pid <= 0 or value['pgid'] != leader or value['sid'] != leader:
        raise RuntimeError('helper did not remain in the owned Git session/group')
    if pid not in live_members(leader):
        raise RuntimeError('helper was not alive at the completion barrier')
    return pid


def sentinel() -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, '-c', 'import time; time.sleep(30)'],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def stop_sentinel(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.terminate()
    process.wait(timeout=5)


def active_case(behavior: str) -> dict[str, object]:
    from west._execution import ProcessController

    with tempfile.TemporaryDirectory(prefix='west-active-') as temporary:
        root = Path(temporary)
        guard = sentinel()
        controller = ProcessController()
        processes = []
        directories = []
        try:
            for number in range(2 if behavior == 'ignore' else 1):
                directory = root / str(number)
                directory.mkdir()
                directories.append(directory)
                process = controller.popen(
                    command(directory, behavior, hold=True),
                    directory,
                    subprocess.DEVNULL,
                    subprocess.DEVNULL,
                )
                processes.append(process)
                identity(directory, process.pid)
            registry = len(controller._children)
            started = time.monotonic()
            controller.cancel()
            elapsed = time.monotonic() - started
            for process in processes:
                wait_no_live_members(process.pid, 5)
            if guard.poll() is not None:
                raise RuntimeError('unrelated sentinel exited')
            if behavior == 'ignore' and not (
                controller.forced_termination and GRACE - 0.05 <= elapsed < 3.5
            ):
                raise RuntimeError('two noncooperative groups did not share the bounded grace')
            return {
                'behavior': behavior,
                'groups': len(processes),
                'active_wait_registry': registry,
                'cancel_seconds': elapsed,
                'forced': controller.forced_termination,
                'live_members_after_cancel': [],
                'sentinel_alive': True,
                'verdict': 'ACTIVE_CONTROL_SATISFIED',
            }
        finally:
            try:
                for directory in directories:
                    (directory / 'release').touch()
                for process in processes:
                    wait_no_live_members(process.pid, 5)
                    process.wait(timeout=5)
            finally:
                controller.close()
                stop_sentinel(guard)


def baseline_case(buffered: bool, behavior: str) -> dict[str, object]:
    from west._execution import ProcessController, ProjectContext

    with tempfile.TemporaryDirectory(prefix='west-owner-') as temporary:
        directory = Path(temporary)
        guard = sentinel()
        controller = ProcessController()
        leader = None
        try:
            with (
                (directory / 'stdout').open('w', encoding='utf-8') as out,
                (directory / 'stderr').open('w', encoding='utf-8') as err,
            ):
                context = ProjectContext(controller, buffered=buffered, stdout=out, stderr=err)
                try:
                    leader, captured, errors = context.git(
                        command(directory, behavior), directory, True, True
                    )
                    if leader.returncode != 0:
                        raise RuntimeError((captured, errors))
                    helper_pid = identity(directory, leader.pid)
                    registry = len(controller._children)
                    if registry != 0:
                        raise RuntimeError('communicated leader was unexpectedly still registered')
                    started = time.monotonic()
                    controller.cancel()
                    elapsed = time.monotonic() - started
                    remaining = live_members(leader.pid)
                    if guard.poll() is not None:
                        raise RuntimeError('unrelated sentinel exited')
                    return {
                        'buffered': buffered,
                        'behavior': behavior,
                        'leader_communicated': True,
                        'active_wait_registry': registry,
                        'helper_was_active': helper_pid,
                        'cancel_seconds': elapsed,
                        'remaining_live_members': remaining,
                        'term_received': (directory / 'term.json').exists(),
                        'forced': controller.forced_termination,
                        'contract_satisfied': not remaining,
                        'verdict': 'GAP_REPRODUCED' if remaining else 'NO_GAP_OBSERVED',
                        'sentinel_alive': True,
                    }
                finally:
                    context.close()
        finally:
            try:
                (directory / 'release').touch()
                if leader is not None:
                    wait_no_live_members(leader.pid, 5)
            finally:
                controller.close()
                stop_sentinel(guard)


def anchor_case(behavior: str) -> dict[str, object]:
    if not all(hasattr(os, name) for name in ('waitid', 'P_PID', 'WEXITED', 'WNOWAIT', 'WNOHANG')):
        return {
            'behavior': behavior,
            'verdict': 'UNSUPPORTED',
            'reason': 'waitid/WNOWAIT unavailable',
        }
    with tempfile.TemporaryDirectory(prefix='west-anchor-') as temporary:
        directory = Path(temporary)
        guard = sentinel()
        process: subprocess.Popen[bytes] | None = None
        reaped = False
        try:
            process = subprocess.Popen(
                command(directory, behavior),
                cwd=directory,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            helper_pid = identity(directory, process.pid)
            deadline = time.monotonic() + 5
            try:
                while True:
                    observed = os.waitid(
                        os.P_PID, process.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG
                    )
                    if observed is not None:
                        break
                    if time.monotonic() >= deadline:
                        raise TimeoutError('Git leader did not finish')
                    time.sleep(0.02)
                repeated = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)
            except OSError as error:
                if error.errno not in (errno.ENOSYS, errno.EINVAL, errno.ENOTSUP):
                    raise
                return {
                    'behavior': behavior,
                    'verdict': 'UNSUPPORTED',
                    'error': str(error),
                    'errno': error.errno,
                }
            if observed.si_pid != process.pid or repeated is None or repeated.si_pid != process.pid:
                raise RuntimeError('WNOWAIT did not preserve the exited leader')
            before = members(process.pid)
            if process.pid not in [pid for pid, state in before if state.startswith('Z')]:
                raise RuntimeError('leader was not retained as an unreaped zombie')
            started = time.monotonic()
            os.killpg(process.pid, signal.SIGTERM)
            deadline = started + GRACE
            while live_members(process.pid) and time.monotonic() < deadline:
                time.sleep(0.02)
            forced = bool(live_members(process.pid))
            if forced:
                os.killpg(process.pid, signal.SIGKILL)
            wait_no_live_members(process.pid, 5)
            after = members(process.pid)
            try:
                os.killpg(process.pid, 0)
                null_signal_exists = True
            except ProcessLookupError:
                null_signal_exists = False
            waited, status = os.waitpid(process.pid, 0)
            process.returncode = os.waitstatus_to_exitcode(status)
            reaped = True
            if waited != process.pid or process.returncode != 0 or guard.poll() is not None:
                raise RuntimeError('leader status or unrelated sentinel changed')
            return {
                'behavior': behavior,
                'verdict': 'PRIMITIVE_OBSERVED',
                'helper_pid': helper_pid,
                'before': before,
                'after_helpers_exit': after,
                'waitid_repeated_without_reaping': True,
                'term_received': (directory / 'term.json').exists(),
                'forced': forced,
                'grace_seconds': time.monotonic() - started,
                'signal_zero_with_only_zombies': null_signal_exists,
                'leader_reaped': True,
                'sentinel_alive': True,
                'identity_retired_no_further_signals': True,
            }
        finally:
            try:
                (directory / 'release').touch()
                if process is not None and not reaped:
                    # No wait/poll reaped this owned leader: its ID remains reserved.
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    _, status = os.waitpid(process.pid, 0)
                    process.returncode = os.waitstatus_to_exitcode(status)
            finally:
                stop_sentinel(guard)


def probe() -> None:
    sys.path.insert(0, str(ROOT / 'src'))
    import west._execution as execution

    os.environ['GIT_CONFIG_GLOBAL'] = os.devnull
    os.environ['GIT_CONFIG_NOSYSTEM'] = '1'
    os.environ['GIT_TERMINAL_PROMPT'] = '0'
    parent = subprocess.check_output(['git', 'rev-parse', 'HEAD~1'], text=True, timeout=5).strip()
    expected = subprocess.check_output(['git', 'show', f'{BASE}:src/west/_execution.py'], timeout=5)
    actual = Path(execution.__file__).read_bytes()
    if parent != BASE or actual != expected:
        raise RuntimeError('diagnostic did not import the exact unchanged control source')
    result: dict[str, object] = {
        'base': BASE,
        'python': sys.version,
        'platform': platform.platform(),
        'execution_sha256': hashlib.sha256(actual).hexdigest(),
        'status': 'INCOMPLETE',
        'production_validation_performed': False,
        'sdd_gate': 'HOLD',
        'limitations': [
            'No replacement backend, new supervisor, product change or full-matrix validation',
            'WNOWAIT uses controlled helpers and ps inspection, not a complete lifetime design',
            'Popen.communicate reaps; integration and bounded anchor retirement remain unresolved',
            'Signal-zero may count zombie-only groups; it is not a live-helper enumeration',
        ],
    }
    active: list[dict[str, object]] = []
    baseline: list[dict[str, object]] = []
    anchors: list[dict[str, object]] = []
    result['active_control'] = active
    result['baseline'] = baseline
    result['anchor_primitive'] = anchors
    try:
        for behavior in ('cooperative', 'ignore'):
            active.append(active_case(behavior))
        for buffered in (True, False):
            for behavior in ('cooperative', 'ignore'):
                baseline.append(baseline_case(buffered, behavior))
        for behavior in ('cooperative', 'ignore'):
            anchors.append(anchor_case(behavior))
        result['baseline_contract_satisfied'] = all(case['contract_satisfied'] for case in baseline)
        result['status'] = 'OBSERVATIONS_COMPLETE'
    finally:
        write_json(ROOT / 'native-owner-results.json', result)
        print(json.dumps(result, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('role', nargs='?', default='probe', choices=('probe', 'launcher', 'helper'))
    parser.add_argument('--directory', type=Path)
    parser.add_argument('--behavior', default='cooperative', choices=('cooperative', 'ignore'))
    parser.add_argument('--hold', action='store_true')
    arguments = parser.parse_args()
    if arguments.role != 'probe' and arguments.directory is None:
        parser.error('fixture role requires --directory')
    if arguments.role == 'helper':
        helper(arguments.directory, arguments.behavior)
    elif arguments.role == 'launcher':
        launch_helper(arguments.directory, arguments.behavior, arguments.hold)
    elif os.name != 'posix':
        parser.error(
            'native probe requires Linux/macOS; fixture roles remain independently testable'
        )
    else:
        probe()
