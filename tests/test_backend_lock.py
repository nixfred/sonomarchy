"""Instance-lock behaviour of the real sonomarchy-backend wrapper.

These run the shell script itself against real flock holders. The wrapper is
started with SONOMARCHY_LOCK_ONLY=1, so it exits right after taking the lock
and never builds a venv or starts pa-dlna. It still checks its runtime
dependencies first, so python3, parec, pactl, flock and lame or ffmpeg must be
installed.

A "shell" here is a copy of the Python interpreter named quickshell, so its
/proc comm reads "quickshell" exactly as the real shell's does.
"""

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
WRAPPER = os.path.join(os.path.dirname(HERE), 'sonomarchy-backend')
PLUGIN_ID = 'io.github.nixfred.sonomarchy'

# argv[1] is the literal "sonomarchy.py" (or "sonomarchy-backend" for a holder
# still in its wrapper), so the wrapper under test recognises it.
HOLDER = r'''
import fcntl, os, sys, time
f = open(sys.argv[2], 'a+')
fcntl.flock(f, fcntl.LOCK_EX)
f.truncate(0)
f.write('%d\n' % os.getpid())
f.flush()
print(os.getpid(), flush=True)
time.sleep(120)
'''

# Starts one holder as its own child and reports the holder's pid. Given a
# wrapper path and a log path, it then also runs the wrapper as its own child,
# so both share this shell, and reports the wrapper's exit code.
SHELL = r'''
import os, subprocess, sys, time
tag = os.environ.get('HOLDER_TAG', 'sonomarchy.py')
p = subprocess.Popen([sys.argv[1], '-c', sys.argv[2], tag, sys.argv[3]],
                     stdout=subprocess.PIPE, text=True)
print(p.stdout.readline().strip(), flush=True)
if len(sys.argv) > 5:
    with open(sys.argv[5], 'w') as log:
        rc = subprocess.run([sys.argv[4]], stdout=subprocess.DEVNULL, stderr=log).returncode
    print('rc=%d' % rc, flush=True)
time.sleep(120)
'''


def alive(pid):
    try:
        with open('/proc/%d/stat' % pid) as f:
            return f.read().split(') ', 1)[1][0] != 'Z'
    except OSError:
        return False


def parent_comm(pid):
    with open('/proc/%d/status' % pid) as f:
        ppid = int(next(l for l in f if l.startswith('PPid:')).split()[1])
    if ppid <= 1:
        return 'init'
    with open('/proc/%d/comm' % ppid) as f:
        return f.read().strip()


class BackendLock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='sonomarchy-lock-')
        self.data = os.path.join(self.tmp, 'data')
        os.makedirs(os.path.join(self.data, PLUGIN_ID))
        self.lock = os.path.join(self.data, PLUGIN_ID, 'instance.lock')
        self.python = os.path.realpath(sys.executable)
        self.fake_shell = os.path.join(self.tmp, 'quickshell')
        shutil.copy2(self.python, self.fake_shell)
        self.procs = []
        self.pids = []

    def tearDown(self):
        for pid in self.pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def spawn(self, argv, **kw):
        p = subprocess.Popen(argv, **kw)
        self.procs.append(p)
        return p

    def holder_under_fake_shell(self, tag='sonomarchy.py'):
        shell = self.spawn([self.fake_shell, '-c', SHELL, self.python, HOLDER, self.lock],
                           env=dict(os.environ, HOLDER_TAG=tag),
                           stdout=subprocess.PIPE, text=True)
        holder = int(shell.stdout.readline())
        self.pids.append(holder)
        return shell, holder

    def wrapper(self, **env):
        full = dict(os.environ, XDG_DATA_HOME=self.data, SONOMARCHY_LOCK_ONLY='1', **env)
        return self.spawn([WRAPPER], env=full, stdout=subprocess.DEVNULL,
                          stderr=subprocess.PIPE, text=True)

    def read_until(self, proc, marker, timeout):
        """stderr lines up to and including the first containing marker."""
        seen = []
        deadline = time.monotonic() + timeout
        fd = proc.stderr.fileno()
        os.set_blocking(fd, False)
        buf = ''
        while time.monotonic() < deadline:
            try:
                chunk = os.read(fd, 65536).decode(errors='replace')
            except BlockingIOError:
                chunk = ''
            if chunk:
                buf += chunk
                while '\n' in buf:
                    line, buf = buf.split('\n', 1)
                    seen.append(line)
                    if marker in line:
                        return seen
            elif proc.poll() is not None:
                break
            time.sleep(0.05)
        self.fail('no %r within %ss; stderr so far: %r' % (marker, timeout, seen + [buf]))

    def test_holder_under_another_shell_means_standby_then_takeover(self):
        shell, holder = self.holder_under_fake_shell()
        w = self.wrapper()
        lines = self.read_until(w, 'SONOMARCHY_STANDBY:', 10)
        self.assertIn('SONOMARCHY_STANDBY: %d' % shell.pid, lines[-1])
        self.assertFalse(any('SETUP_ERROR' in l for l in lines), lines)

        # Standing by, not failing: still waiting well past a normal start.
        time.sleep(1.5)
        self.assertIsNone(w.poll())

        # The other shell's backend stops; this one must take over.
        os.kill(holder, signal.SIGTERM)
        shell.kill()
        lines = self.read_until(w, 'SONOMARCHY_LOCKED', 15)
        self.assertTrue(any('SONOMARCHY_ACTIVE:' in l for l in lines), lines)
        self.assertEqual(w.wait(timeout=5), 0)
        with open(self.lock) as f:
            self.assertEqual(f.read().strip(), str(w.pid))

    def test_holder_still_in_its_wrapper_means_standby(self):
        # The lock is taken before the venv build, so on a first start (or a
        # requirements change) the holder is still the bash wrapper.
        shell, holder = self.holder_under_fake_shell(tag='sonomarchy-backend')
        w = self.wrapper()
        lines = self.read_until(w, 'SONOMARCHY_STANDBY:', 10)
        self.assertIn('SONOMARCHY_STANDBY: %d' % shell.pid, lines[-1])
        self.assertFalse(any('SETUP_ERROR' in l for l in lines), lines)
        os.kill(holder, signal.SIGTERM)
        shell.kill()
        self.read_until(w, 'SONOMARCHY_LOCKED', 15)
        self.assertEqual(w.wait(timeout=5), 0)

    def test_holder_under_the_same_shell_is_not_standby(self):
        # Holder and wrapper share one shell: a reload still shutting down.
        # That path keeps its bounded wait and its error.
        log = os.path.join(self.tmp, 'wrapper.log')
        env = dict(os.environ, XDG_DATA_HOME=self.data, SONOMARCHY_LOCK_ONLY='1',
                   SONOMARCHY_LOCK_WAIT='1')
        shell = self.spawn([self.fake_shell, '-c', SHELL, self.python, HOLDER, self.lock,
                            WRAPPER, log], env=env, stdout=subprocess.PIPE, text=True)
        holder = int(shell.stdout.readline())
        self.pids.append(holder)
        self.assertEqual(parent_comm(holder), 'quickshell')
        self.assertEqual(shell.stdout.readline().strip(), 'rc=1')
        with open(log) as f:
            text = f.read()
        self.assertIn('SONOMARCHY_SETUP_ERROR: Another Sonomarchy backend is still running after 1 s.', text)
        self.assertNotIn('STANDBY', text)
        self.assertTrue(alive(holder), 'a holder under a live shell must never be killed')

    def test_orphan_is_taken_over_while_standing_by(self):
        shell, holder = self.holder_under_fake_shell()
        w = self.wrapper()
        self.read_until(w, 'SONOMARCHY_STANDBY:', 10)

        # The shell dies hard and its backend is reparented, still holding.
        shell.kill()
        shell.wait()
        deadline = time.monotonic() + 5
        while parent_comm(holder) == 'quickshell' and time.monotonic() < deadline:
            time.sleep(0.05)
        reaper = parent_comm(holder)
        if reaper not in ('systemd', 'init'):
            self.skipTest('orphans here are reaped by %r, which the wrapper '
                          'rightly does not treat as a dead shell' % reaper)

        lines = self.read_until(w, 'SONOMARCHY_LOCKED', 20)
        self.assertTrue(any('whose shell is gone' in l for l in lines), lines)
        self.assertEqual(w.wait(timeout=5), 0)
        deadline = time.monotonic() + 5
        while alive(holder) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(alive(holder))


if __name__ == '__main__':
    unittest.main()
