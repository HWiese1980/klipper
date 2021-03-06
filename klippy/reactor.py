# File descriptor and timer event helper
#
# Copyright (C) 2016-2019  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import os, select, math, time, Queue
import greenlet
import chelper, util

class ReactorTimer:
    def __init__(self, callback, waketime):
        self.callback = callback
        self.waketime = waketime

class ReactorCallback:
    def __init__(self, reactor, callback, waketime):
        self.reactor = reactor
        self.timer = reactor.register_timer(self.invoke, waketime)
        self.callback = callback
    def invoke(self, eventtime):
        self.reactor.unregister_timer(self.timer)
        self.callback(eventtime)
        return self.reactor.NEVER

class ReactorFileHandler:
    def __init__(self, fd, callback):
        self.fd = fd
        self.callback = callback
    def fileno(self):
        return self.fd

class ReactorGreenlet(greenlet.greenlet):
    def __init__(self, run):
        greenlet.greenlet.__init__(self, run=run)
        self.timer = None

class ReactorMutex:
    def __init__(self, reactor, is_locked):
        self.reactor = reactor
        self.is_locked = is_locked
        self.next_pending = False
        self.queue = []
        self.lock = self.__enter__
        self.unlock = self.__exit__
    def test(self):
        return self.is_locked
    def __enter__(self):
        if not self.is_locked:
            self.is_locked = True
            return
        g = greenlet.getcurrent()
        self.queue.append(g)
        while 1:
            self.reactor.pause(self.reactor.NEVER)
            if self.next_pending and self.queue[0] is g:
                self.next_pending = False
                self.queue.pop(0)
                return
    def __exit__(self, type=None, value=None, tb=None):
        if not self.queue:
            self.is_locked = False
            return
        self.next_pending = True
        self.reactor.update_timer(self.queue[0].timer, self.reactor.NOW)

class SelectReactor:
    NOW = 0.
    NEVER = 9999999999999999.
    def __init__(self):
        # Main code
        self._process = False
        self.monotonic = chelper.get_ffi()[1].get_monotonic
        # Timers
        self._timers = []
        self._next_timer = self.NEVER
        # Callbacks
        self._pipe_fds = None
        self._async_queue = Queue.Queue()
        # File descriptors
        self._fds = []
        # Greenlets
        self._g_dispatch = None
        self._greenlets = []
    # Timers
    def update_timer(self, timer_handler, waketime):
        timer_handler.waketime = waketime
        self._next_timer = min(self._next_timer, waketime)
    def register_timer(self, callback, waketime=NEVER):
        timer_handler = ReactorTimer(callback, waketime)
        timers = list(self._timers)
        timers.append(timer_handler)
        self._timers = timers
        self._next_timer = min(self._next_timer, waketime)
        return timer_handler
    def unregister_timer(self, timer_handler):
        timer_handler.waketime = self.NEVER
        timers = list(self._timers)
        timers.pop(timers.index(timer_handler))
        self._timers = timers
    def _check_timers(self, eventtime):
        if eventtime < self._next_timer:
            return min(1., max(.001, self._next_timer - eventtime))
        self._next_timer = self.NEVER
        g_dispatch = self._g_dispatch
        for t in self._timers:
            waketime = t.waketime
            if eventtime >= waketime:
                t.waketime = self.NEVER
                t.waketime = waketime = t.callback(eventtime)
                if g_dispatch is not self._g_dispatch:
                    self._next_timer = min(self._next_timer, waketime)
                    self._end_greenlet(g_dispatch)
                    return 0.
            self._next_timer = min(self._next_timer, waketime)
        if eventtime >= self._next_timer:
            return 0.
        return min(1., max(.001, self._next_timer - self.monotonic()))
    # Callbacks
    def register_callback(self, callback, waketime=NOW):
        ReactorCallback(self, callback, waketime)
    def register_async_callback(self, callback):
        self._async_queue.put_nowait(callback)
        try:
            os.write(self._pipe_fds[1], '.')
        except os.error:
            pass
    def _got_pipe_signal(self, eventtime):
        try:
            os.read(self._pipe_fds[0], 4096)
        except os.error:
            pass
        while 1:
            try:
                callback = self._async_queue.get_nowait()
            except Queue.Empty:
                break
            ReactorCallback(self, callback, self.NOW)
    def _setup_async_callbacks(self):
        self._pipe_fds = os.pipe()
        util.set_nonblock(self._pipe_fds[0])
        util.set_nonblock(self._pipe_fds[1])
        self.register_fd(self._pipe_fds[0], self._got_pipe_signal)
    def __del__(self):
        if self._pipe_fds is not None:
            os.close(self._pipe_fds[0])
            os.close(self._pipe_fds[1])
            self._pipe_fds = None
    # Greenlets
    def _sys_pause(self, waketime):
        # Pause using system sleep for when reactor not running
        delay = waketime - self.monotonic()
        if delay > 0.:
            time.sleep(delay)
        return self.monotonic()
    def pause(self, waketime):
        g = greenlet.getcurrent()
        if g is not self._g_dispatch:
            if self._g_dispatch is None:
                return self._sys_pause(waketime)
            # Switch to _check_timers (via g.timer.callback return)
            return self._g_dispatch.switch(waketime)
        # Pausing the dispatch greenlet - prepare a new greenlet to do dispatch
        if self._greenlets:
            g_next = self._greenlets.pop()
        else:
            g_next = ReactorGreenlet(run=self._dispatch_loop)
        g_next.parent = g.parent
        g.timer = self.register_timer(g.switch, waketime)
        self._next_timer = self.NOW
        # Switch to _dispatch_loop (via _end_greenlet or direct)
        eventtime = g_next.switch()
        # This greenlet activated from g.timer.callback (via _check_timers)
        return eventtime
    def _end_greenlet(self, g_old):
        # Cache this greenlet for later use
        self._greenlets.append(g_old)
        self.unregister_timer(g_old.timer)
        g_old.timer = None
        # Switch to _check_timers (via g_old.timer.callback return)
        self._g_dispatch.switch(self.NEVER)
        # This greenlet reactivated from pause() - return to main dispatch loop
        self._g_dispatch = g_old
    # Mutexes
    def mutex(self, is_locked=False):
        return ReactorMutex(self, is_locked)
    # File descriptors
    def register_fd(self, fd, callback):
        file_handler = ReactorFileHandler(fd, callback)
        self._fds.append(file_handler)
        return file_handler
    def unregister_fd(self, file_handler):
        self._fds.pop(self._fds.index(file_handler))
    # Main loop
    def _dispatch_loop(self):
        self._g_dispatch = g_dispatch = greenlet.getcurrent()
        eventtime = self.monotonic()
        while self._process:
            timeout = self._check_timers(eventtime)
            res = select.select(self._fds, [], [], timeout)
            eventtime = self.monotonic()
            for fd in res[0]:
                fd.callback(eventtime)
                if g_dispatch is not self._g_dispatch:
                    self._end_greenlet(g_dispatch)
                    eventtime = self.monotonic()
                    break
        self._g_dispatch = None
    def run(self):
        if self._pipe_fds is None:
            self._setup_async_callbacks()
        self._process = True
        g_next = ReactorGreenlet(run=self._dispatch_loop)
        g_next.switch()
    def end(self):
        self._process = False

class PollReactor(SelectReactor):
    def __init__(self):
        SelectReactor.__init__(self)
        self._poll = select.poll()
        self._fds = {}
    # File descriptors
    def register_fd(self, fd, callback):
        file_handler = ReactorFileHandler(fd, callback)
        fds = self._fds.copy()
        fds[fd] = callback
        self._fds = fds
        self._poll.register(file_handler, select.POLLIN | select.POLLHUP)
        return file_handler
    def unregister_fd(self, file_handler):
        self._poll.unregister(file_handler)
        fds = self._fds.copy()
        del fds[file_handler.fd]
        self._fds = fds
    # Main loop
    def _dispatch_loop(self):
        self._g_dispatch = g_dispatch = greenlet.getcurrent()
        eventtime = self.monotonic()
        while self._process:
            timeout = self._check_timers(eventtime)
            res = self._poll.poll(int(math.ceil(timeout * 1000.)))
            eventtime = self.monotonic()
            for fd, event in res:
                self._fds[fd](eventtime)
                if g_dispatch is not self._g_dispatch:
                    self._end_greenlet(g_dispatch)
                    eventtime = self.monotonic()
                    break
        self._g_dispatch = None

class EPollReactor(SelectReactor):
    def __init__(self):
        SelectReactor.__init__(self)
        self._epoll = select.epoll()
        self._fds = {}
    # File descriptors
    def register_fd(self, fd, callback):
        file_handler = ReactorFileHandler(fd, callback)
        fds = self._fds.copy()
        fds[fd] = callback
        self._fds = fds
        self._epoll.register(fd, select.EPOLLIN | select.EPOLLHUP)
        return file_handler
    def unregister_fd(self, file_handler):
        self._epoll.unregister(file_handler.fd)
        fds = self._fds.copy()
        del fds[file_handler.fd]
        self._fds = fds
    # Main loop
    def _dispatch_loop(self):
        self._g_dispatch = g_dispatch = greenlet.getcurrent()
        eventtime = self.monotonic()
        while self._process:
            timeout = self._check_timers(eventtime)
            res = self._epoll.poll(timeout)
            eventtime = self.monotonic()
            for fd, event in res:
                self._fds[fd](eventtime)
                if g_dispatch is not self._g_dispatch:
                    self._end_greenlet(g_dispatch)
                    eventtime = self.monotonic()
                    break
        self._g_dispatch = None

# Use the poll based reactor if it is available
try:
    select.poll
    Reactor = PollReactor
except:
    Reactor = SelectReactor
