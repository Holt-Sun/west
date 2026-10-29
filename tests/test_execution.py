# Copyright (c) 2026 NXP

import ctypes
import io
import logging
import os
import shlex
import subprocess
import sys
import threading
import time
from concurrent.futures import CancelledError, ThreadPoolExecutor
from pathlib import Path

import pytest
from colorama.ansitowin32 import AnsiToWin32
from conftest import create_repo

from west.app.main import LogHandler
from west.commands import Verbosity, WestCommand
from west.manifest import Project


class OutputCommand(WestCommand):
    def do_add_parser(self, parser_adder):
        pass

    def do_run(self, args, unknown):
        pass


@pytest.fixture(autouse=True)
def quiet_manifest_logger():
    logger = logging.getLogger('west.manifest')
    level = logger.level
    logger.setLevel(logging.WARNING)
    try:
        yield
    finally:
        logger.setLevel(level)


def project_at(path):
    create_repo(os.fspath(path))
    return Project('output-test', 'unused', path=path.name, topdir=path.parent)


def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail('readiness deadline exceeded')
        threading.Event().wait(0.02)


def process_active(pid):
    if sys.platform == 'win32':
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            if error == 87:
                return False
            raise ctypes.WinError(error)
        try:
            status = kernel.WaitForSingleObject(handle, 0)
            if status not in (0, 258):
                raise ctypes.WinError(ctypes.get_last_error())
            return status == 258
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = Path(f'/proc/{pid}/stat')
    if stat.exists():
        try:
            return not stat.read_text().split(') ', 1)[1].startswith('Z')
        except FileNotFoundError:
            return False
    return True


def test_buffered_producers_keep_stream_ownership(tmp_path, monkeypatch):
    from west import _execution

    project = project_at(tmp_path / 'repo space \u96ea')
    out, err = io.StringIO(), io.StringIO()
    controller = _execution.ProcessController()
    scope = _execution.ProjectContext(controller, buffered=True, stdout=out, stderr=err)
    command = OutputCommand('output', 'output', 'output')
    command.verbosity = Verbosity.DBG_EXTREME
    logger = logging.getLogger('west.manifest')
    handler = LogHandler()
    handler.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
    monkeypatch.setattr(logger, 'handlers', [handler])
    monkeypatch.setattr(logger, 'propagate', False)
    old_level = logger.level
    logger.setLevel(logging.DEBUG)
    original = sys.stdout, sys.stderr, Path.cwd(), dict(os.environ)
    try:
        with scope.activate():
            assert _execution.current_context() is scope
            command.inf('python-start \u96ea')
            logger.info('manifest-out')
            logger.warning('manifest-err')
            project.git(['-c', "alias.emit=!printf 'git-out\\n'; printf 'git-err\\n' >&2", 'emit'])
            private = project.git(
                ['-c', "alias.emit=!printf 'private-out\\n'; printf 'private-err\\n' >&2", 'emit'],
                capture_stdout=True,
                capture_stderr=True,
            )
            command.inf('python-end')
            command.wrn('python-warning')
        assert _execution.current_context() is None
        assert out.getvalue() == err.getvalue() == ''
        assert private.stdout == b'private-out\n'
        assert private.stderr == b'private-err\n'
        scope.replay()
        stdout, stderr = out.getvalue(), err.getvalue()
        for marker in ['python-start \u96ea\n', 'manifest-out\n', 'git-out\n', 'python-end\n']:
            assert stdout.count(marker) == 1
        positions = [
            stdout.index(s)
            for s in ['python-start \u96ea\n', 'manifest-out\n', 'git-out\n', 'python-end\n']
        ]
        assert positions == sorted(positions)
        assert stderr.index('manifest-err\n') < stderr.index('git-err\n')
        assert stderr.index('git-err\n') < stderr.index('python-warning\n')
        assert 'private-out\n' not in stdout
        assert 'private-err\n' not in stderr
        assert "b'private-out\\n'" in stdout
        assert 'git-err\n' not in stdout
        assert 'git-out\n' not in stderr
        assert (sys.stdout, sys.stderr, Path.cwd(), dict(os.environ)) == original
    finally:
        logger.setLevel(old_level)
        controller.cancel()
        scope.close()
        controller.close()


def test_python_text_and_raw_child_bytes_keep_distinct_encodings(tmp_path):
    from west import _execution

    project = project_at(tmp_path / 'raw')
    data = io.BytesIO()
    out = io.TextIOWrapper(data, encoding='ascii', errors='backslashreplace')
    controller = _execution.ProcessController()
    scope = _execution.ProjectContext(controller, buffered=True, stdout=out, stderr=io.StringIO())
    command = OutputCommand('output', 'output', 'output')
    try:
        with scope.activate():
            command.inf('python \u96ea')
            project.git(['-c', "alias.emit=!printf '\\377child\\n'", 'emit'])
            command.inf('after')
        scope.replay()
        out.flush()
        newline = os.linesep.encode('ascii')
        assert data.getvalue() == b'python \\u96ea' + newline + b'\xffchild\nafter' + newline
    finally:
        controller.cancel()
        scope.close()
        controller.close()


@pytest.mark.parametrize('strip', [False, True])
def test_colorama_text_only_replay_uses_original_rendering_policy(tmp_path, strip):
    from west import _execution

    project = project_at(tmp_path / 'color')
    out = io.StringIO()
    wrapped = AnsiToWin32(out, convert=False, strip=strip).stream
    controller = _execution.ProcessController()
    scope = _execution.ProjectContext(
        controller, buffered=True, stdout=wrapped, stderr=io.StringIO()
    )
    command = OutputCommand('output', 'output', 'output')
    try:
        with scope.activate():
            command.banner('West color')
            project.git(['-c', 'color.ui=always', 'log', '-1', '--format=%CredGit color%Creset'])
        scope.replay()
        assert 'West color' in out.getvalue()
        assert 'Git color' in out.getvalue()
        assert ('\x1b[' in out.getvalue()) is not strip
    finally:
        controller.cancel()
        scope.close()
        controller.close()


@pytest.mark.parametrize('strip', [False, True])
def test_colorama_binary_destination_retains_explicit_git_bytes(tmp_path, monkeypatch, strip):
    from west import _execution

    project = project_at(tmp_path / 'binary color')
    argv = ['-c', 'color.ui=always', 'log', '-1', '--format=%CredGit color%Creset']
    expected = project.git(argv, capture_stdout=True, capture_stderr=True).stdout
    data = io.BytesIO()
    text = io.TextIOWrapper(data, encoding='ascii', errors='backslashreplace')
    wrapped = AnsiToWin32(text, convert=False, strip=strip).stream
    controller = _execution.ProcessController()
    scope = _execution.ProjectContext(
        controller, buffered=True, stdout=wrapped, stderr=io.StringIO()
    )
    command = OutputCommand('output', 'output', 'output')
    monkeypatch.setattr(OutputCommand, 'color_ui', property(lambda self: False))
    try:
        with scope.activate():
            command.banner('West color disabled')
            project.git(argv)
        scope.replay()
        text.flush()
        assert b'\x1b[' in expected
        assert data.getvalue().count(expected) == 1
        assert b'\x1b[92m' not in data.getvalue()
    finally:
        controller.cancel()
        scope.close()
        controller.close()


def test_unbuffered_context_has_no_spools_and_keeps_git_capture(tmp_path, monkeypatch, capfd):
    from west import _execution

    project = project_at(tmp_path / 'live')
    out, err = io.StringIO(), io.StringIO()
    controller = _execution.ProcessController()

    def no_spool(*args, **kwargs):
        pytest.fail('unbuffered output must not allocate a spool')

    monkeypatch.setattr('tempfile.TemporaryFile', no_spool)
    scope = _execution.ProjectContext(controller, buffered=False, stdout=out, stderr=err)
    try:
        with scope.activate():
            OutputCommand('output', 'output', 'output').inf('live-python')
            assert out.getvalue() == 'live-python\n'
            project.git(['-c', "alias.emit=!printf 'live-child\\n'", 'emit'])
            captured = project.git(['rev-parse', 'HEAD'], capture_stdout=True)
            failed = project.git(
                ['rev-parse', '--verify', 'no-such-reference'],
                capture_stdout=True,
                capture_stderr=True,
                check=False,
            )
        assert 'live-child\n' in capfd.readouterr().out
        assert len(captured.stdout.strip()) == 40
        assert failed.returncode != 0 and failed.stderr
        scope.replay()
        assert out.getvalue() == 'live-python\n'
    finally:
        controller.cancel()
        scope.close()
        controller.close()


@pytest.mark.parametrize('producer', ['python', 'git'])
def test_colorama_sequences_survive_replay_chunk_boundaries(tmp_path, producer):
    from west import _execution

    payload = 'x' * 65534 + '\x1b[31mred\x1b[0m\n'
    out = io.StringIO()
    wrapped = AnsiToWin32(out, convert=False, strip=True).stream
    controller = _execution.ProcessController()
    scope = _execution.ProjectContext(
        controller, buffered=True, stdout=wrapped, stderr=io.StringIO()
    )
    try:
        with scope.activate():
            if producer == 'python':
                OutputCommand('output', 'output', 'output').inf(payload, end='')
            else:
                project = project_at(tmp_path / 'ansi')
                (Path(project.abspath) / 'ansi.txt').write_bytes(payload.encode('ascii'))
                project.git(['-c', 'alias.emit=!cat ansi.txt', 'emit'])
        scope.replay()
        assert out.getvalue() == 'x' * 65534 + 'red\n'
    finally:
        controller.cancel()
        scope.close()
        controller.close()


def test_scope_is_reset_after_fatal_error():
    from west import _execution

    controller = _execution.ProcessController()
    scope = _execution.ProjectContext(
        controller, buffered=True, stdout=io.StringIO(), stderr=io.StringIO()
    )
    try:
        with pytest.raises(SystemExit) as raised, scope.activate():
            raise SystemExit(7)
        assert raised.value.code == 7
        assert _execution.current_context() is None
    finally:
        controller.cancel()
        scope.close()
        controller.close()


@pytest.mark.parametrize('buffered', [False, True])
@pytest.mark.parametrize('delay_registration', [False, True])
def test_cancel_reaps_owned_git_tree_and_rejects_later_spawns(
    tmp_path, monkeypatch, buffered, delay_registration
):
    from west import _execution

    if delay_registration and sys.platform != 'win32':
        pytest.skip('Windows launch-to-job-assignment regression')
    project = project_at(tmp_path / 'children')
    pid_file = tmp_path / 'ready.pid'
    release = tmp_path / 'release'
    helper = tmp_path / 'blocked child.py'
    helper.write_text(
        'import os, sys, time\n'
        'from pathlib import Path\n'
        'Path(sys.argv[1]).write_text(str(os.getpid()), encoding="ascii")\n'
        'while not Path(sys.argv[2]).exists():\n'
        '    time.sleep(0.02)\n',
        encoding='ascii',
    )
    controller = _execution.ProcessController()
    scope = _execution.ProjectContext(
        controller, buffered=buffered, stdout=io.StringIO(), stderr=io.StringIO()
    )
    popen = subprocess.Popen
    owned = []

    def record_popen(args, **kwargs):
        process = popen(args, **kwargs)
        if args[0] == 'git':
            owned.append(process)
            if delay_registration and not kwargs.get('creationflags', 0) & 0x4:
                wait_until(
                    lambda: pid_file.exists() and bool(pid_file.read_text(encoding='ascii')),
                    timeout=30,
                )
        return process

    monkeypatch.setattr(subprocess, 'Popen', record_popen)
    sentinel = popen(
        [getattr(sys, '_base_executable', sys.executable), '-c', 'import time; time.sleep(120)']
    )
    executor = ThreadPoolExecutor(max_workers=1)

    def run_child():
        with scope.activate():
            alias = '!' + shlex.join(
                [sys.executable, os.fspath(helper), os.fspath(pid_file), os.fspath(release)]
            )
            project.git(['-c', f'alias.block={alias}', 'block'])

    future = executor.submit(run_child)

    def child_ready():
        if future.done():
            future.result()
            pytest.fail('Git child exited before readiness')
        return pid_file.exists() and bool(pid_file.read_text(encoding='ascii'))

    try:
        wait_until(child_ready, timeout=30)
        child_pid = int(pid_file.read_text(encoding='ascii'))
        assert process_active(child_pid)
        assert owned
        start = time.monotonic()
        controller.cancel()
        with pytest.raises((subprocess.CalledProcessError, CancelledError)):
            future.result(timeout=8)
        wait_until(lambda: not process_active(child_pid), timeout=1)
        assert time.monotonic() - start < 10
        assert all(process.poll() is not None for process in owned)
        assert sentinel.poll() is None
        with scope.activate(), pytest.raises(CancelledError):
            project.git(['--version'])
        assert len(owned) == 1
    finally:
        release.touch()
        try:
            controller.cancel()
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
            scope.close()
            controller.close()
            sentinel.terminate()
            sentinel.wait(timeout=5)
