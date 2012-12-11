#!/usr/bin/env python
# $Id$

#  ======================================================================
#  Copyright (C) 2007-2012 Giampaolo Rodola' <g.rodola@gmail.com>
#
#                         All Rights Reserved
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation
# files (the "Software"), to deal in the Software without
# restriction, including without limitation the rights to use,
# copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the
# Software is furnished to do so, subject to the following
# conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES
# OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
# HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
# WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
# OTHER DEALINGS IN THE SOFTWARE.
#
#  ======================================================================

"""
This module contains the main FTPServer class which listens on a
host:port and dispatches the incoming connections to a handler.
The concurrency is handled asynchronously by the main process thread,
meaning the handler cannot block otherwise the whole server whill hang.

Other than that we have 2 subclasses changing the asynchronous concurrency
model using multiple threads or processes.

You might be interested in these in case your code contains blocking
parts which cannot be adapted to the base async model or if the
underlying filesystem is particularly slow, see:

https://code.google.com/p/pyftpdlib/issues/detail?id=197
https://code.google.com/p/pyftpdlib/issues/detail?id=212

Two classes are provided:

 - ThreadingFTPServer
 - MultiprocessFTPServer

...spawning a new thread or process every time a client connects.

The main thread will be async-based and be used only to accept new
connections.
Every time a new connection comes in that will be dispatched to a
separate thread/process which internally will run its own IO loop.
This way the handler handling that connections will be free to block
without hanging the whole FTP server.
"""

import os
import socket
import traceback
import sys
import errno
import logging
import select

from pyftpdlib.ioloop import Acceptor, IOLoop


__all__ = ['FTPServer']


# ===================================================================
# --- base class
# ===================================================================

class FTPServer(Acceptor):
    """Creates a socket listening on <address>, dispatching the requests
    to a <handler> (typically FTPHandler class).

    Depending on the type of address specified IPv4 or IPv6 connections
    (or both, depending from the underlying system) will be accepted.

    All relevant session information is stored in class attributes
    described below.

     - (int) max_cons:
        number of maximum simultaneous connections accepted (defaults
        to 512). Can be set to 0 for unlimited but it is recommended
        to always have a limit to avoid running out of file descriptors
        (DoS).

     - (int) max_cons_per_ip:
        number of maximum connections accepted for the same IP address
        (defaults to 0 == unlimited).
    """

    max_cons = 512
    max_cons_per_ip = 0

    def __init__(self, address, handler, ioloop=None):
        """Initiate the FTP server opening listening on address.

         - (tuple) address: the host:port pair on which the command
           channel will listen.

         - (classobj) handler: the handler class to use.
        """
        Acceptor.__init__(self, ioloop=ioloop)
        self.handler = handler
        self.ip_map = []
        host, port = address
        # in case of FTPS class not properly configured we want errors
        # to be raised here rather than later, when client connects
        if hasattr(handler, 'get_ssl_context'):
            handler.get_ssl_context()

        # AF_INET or AF_INET6 socket
        # Get the correct address family for our host (allows IPv6 addresses)
        try:
            self._af = self.bind_af_unspecified((host, port))
        except socket.gaierror:
            # Probably a DNS issue. Assume IPv4.
            self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
            self.set_reuse_addr()
            self.bind((host, port))
            self._af = socket.AF_INET
        self.listen(5)

    @property
    def address(self):
        return self.socket.getsockname()[:2]

    def _map_len(self):
        return len(self.ioloop.socket_map)

    def _accept_new_cons(self):
        """Return True if the server is willing to accept new connections."""
        if not self.max_cons:
            return True
        else:
            return self._map_len() <= self.max_cons

    def _log_start(self):
        if not logging.getLogger().handlers:
            # If we get to this point it means the user hasn't
            # configured logging. We want to log by default so
            # we configure logging ourselves so that it will
            # print to stderr.
            from pyftpdlib.ioloop import _config_logging
            _config_logging()

        if self.handler.passive_ports:
            pasv_ports = "%s->%s" % (self.handler.passive_ports[0],
                                     self.handler.passive_ports[-1])
        else:
            pasv_ports = None
        logging.info(">>> starting FTP server on %s:%s <<<" % self.address)
        logging.info("poller: %s", self.ioloop.__class__.__name__)
        logging.info("masquerade (NAT) address: %s",
                     self.handler.masquerade_address)
        logging.info("passive ports: %s", pasv_ports)
        if os.name == 'posix':
            logging.info("use sendfile(2): %s", self.handler.use_sendfile)

    def serve_forever(self, timeout=None, blocking=True, log_start_stop=True):
        """Start serving.

         - (float) timeout: the timeout passed to the underlying IO
           loop expressed in seconds (default 1.0).

         - (bool) blocking: if False loop once and then return the
           timeout of the next scheduled call next to expire soonest
           (if any).

         - (bool) log_start_stop: whether log on server start and stop.
        """
        log = log_start_stop and blocking == True
        if log:
            self._log_start()
        try:
            self.ioloop.loop(timeout, blocking)
        except (KeyboardInterrupt, SystemExit):
            pass
        if blocking:
            if log:
                logging.info(">>> shutting down FTP server <<<")
            self.close_all()

    def handle_accepted(self, sock, addr):
        """Called when remote client initiates a connection."""
        handler = None
        ip = None
        try:
            handler = self.handler(sock, self, ioloop=self.ioloop)
            if not handler.connected:
                return
            logging.info("[]%s:%s Connected." % addr[:2])
            ip = addr[0]
            self.ip_map.append(ip)

            # For performance and security reasons we should always set a
            # limit for the number of file descriptors that socket_map
            # should contain.  When we're running out of such limit we'll
            # use the last available channel for sending a 421 response
            # to the client before disconnecting it.
            if not self._accept_new_cons():
                handler.handle_max_cons()
                return

            # accept only a limited number of connections from the same
            # source address.
            if self.max_cons_per_ip:
                if self.ip_map.count(ip) > self.max_cons_per_ip:
                    handler.handle_max_cons_per_ip()
                    return

            try:
                handler.handle()
            except:
                handler.handle_error()
            else:
                return handler
        except Exception:
            # This is supposed to be an application bug that should
            # be fixed. We do not want to tear down the server though
            # (DoS). We just log the exception, hoping that someone
            # will eventually file a bug. References:
            # - http://code.google.com/p/pyftpdlib/issues/detail?id=143
            # - http://code.google.com/p/pyftpdlib/issues/detail?id=166
            # - https://groups.google.com/forum/#!topic/pyftpdlib/h7pPybzAx14
            logging.error(traceback.format_exc())
            if handler is not None:
                handler.close()
            else:
                if ip is not None and ip in self.ip_map:
                    self.ip_map.remove(ip)

    def handle_error(self):
        """Called to handle any uncaught exceptions."""
        try:
            raise
        except Exception:
            logging.error(traceback.format_exc())
        self.close()

    def close_all(self):
        """Stop serving and also disconnects all currently connected
        clients.
        """
        return self.ioloop.close()


# ===================================================================
# --- extra implementations
# ===================================================================

class _SpawnerBase(FTPServer):
    """Base class shared by multiple threads/process dispatcher.
    Not supposed to be used.
    """

    _lock = None
    _exit = None

    def __init__(self, address, handler, ioloop=None):
        FTPServer.__init__(self, address, handler, ioloop)
        self._active_tasks = []

    def _start_task(self, *args, **kwargs):
        raise NotImplementedError('must be implemented in subclass')

    def _current_task(self):
        raise NotImplementedError('must be implemented in subclass')

    def _map_len(self):
        raise NotImplementedError('must be implemented in subclass')

    def _loop(self, handler):
        """Serve handler's IO loop in a separate thread or process."""
        ioloop = IOLoop()
        try:
            handler.ioloop = ioloop
            try:
                handler.add_channel()
            except EnvironmentError:
                err = sys.exc_info()[1]
                if err.errno == errno.EBADF:
                    # we might get here in case the other end quickly
                    # disconnected (see test_quick_connect())
                    return
                else:
                    raise

            # Here we localize variable access to minimize overhead.
            poll = ioloop.poll
            socket_map = ioloop.socket_map
            tasks = ioloop.sched._tasks
            sched_poll = ioloop.sched.poll
            poll_timeout = getattr(self, 'poll_timeout', None)
            soonest_timeout = poll_timeout

            while socket_map and not self._exit.is_set():
                try:
                    poll(timeout=soonest_timeout)
                    if tasks:
                        soonest_timeout = sched_poll()
                    else:
                        soonest_timeout = None
                except (KeyboardInterrupt, SystemExit):
                    # note: these two exceptions are raised in all sub
                    # processes
                    self._exit.set()
                except select.error:
                    # on Windows we can get WSAENOTSOCK if the client
                    # rapidly connect and disconnects
                    err = sys.exc_info()[1]
                    if os.name == 'nt' and err.args[0] == 10038:
                        for fd in socket_map.keys():
                            try:
                                select.select([fd], [], [], 0)
                            except select.error:
                                logging.info("discarding broken socket %r",
                                             socket_map[fd])
                                del socket_map[fd]
                    else:
                        raise
                else:
                    if poll_timeout:
                        if soonest_timeout is None \
                        or soonest_timeout > poll_timeout:
                            soonest_timeout = poll_timeout
        finally:
            try:
                self._active_tasks.remove(self._current_task())
            except ValueError:
                pass
            ioloop.close()

    def handle_accepted(self, sock, addr):
        handler = FTPServer.handle_accepted(self, sock, addr)
        if handler is not None:
            # unregister the handler from the main IOLoop used by the
            # main thread to accept connections
            self.ioloop.unregister(handler._fileno)

            t = self._start_task(target=self._loop, args=(handler,))
            t.name = repr(addr)
            t.start()

            self._lock.acquire()
            try:
                self._active_tasks.append(t)
            finally:
                self._lock.release()

    def serve_forever(self, timeout=None, blocking=True, log_start_stop=True):
        log = log_start_stop and blocking == True
        if log:
            self._log_start()
        self._exit.clear()
        closed = False
        try:
            FTPServer.serve_forever(self, timeout, blocking)
        except (KeyboardInterrupt, SystemExit):
            if blocking:
                if log:
                    logging.info(">>> shutting down FTP server <<<")
                self.close_all()
            closed = True
            raise
        finally:
            if blocking and not closed:
                self.close_all()

    def close_all(self):
        tasks = self._active_tasks[:]
        # this must be set after getting active tasks as it causes
        # thread objects to get out of the list too soon
        self._exit.set()
        if tasks and hasattr(tasks[0], 'terminate'):
            for t in tasks:
                try:
                    t.terminate()
                except OSError:
                    err = sys.exc_info()[1]
                    if err.errno != errno.ESRCH:
                        raise
        for t in tasks:
            if t.is_alive():
                t.join()
        del self._active_tasks[:]
        FTPServer.close_all(self)


try:
    import threading
except ImportError:
    pass
else:
    __all__ += ['ThreadedFTPServer']

    class ThreadedFTPServer(_SpawnerBase):
        """A modified version of base FTPServer class which spawns a
        thread every time a new connection is established.
        """
        # The timeout passed to thread's IOLoop.poll() call on every
        # loop. Necessary since threads ignore KeyboardInterrupt.
        poll_timeout = 1.0
        _lock = threading.Lock()
        _exit = threading.Event()

        def _start_task(self, *args, **kwargs):
            return threading.Thread(*args, **kwargs)

        def _current_task(self):
            return threading.current_thread()

        def _map_len(self):
            return threading.active_count()


if os.name == 'posix':
    try:
        import multiprocessing
    except ImportError:
        pass
    else:
        __all__ += ['MultiprocessFTPServer']

        class MultiprocessFTPServer(_SpawnerBase):
            """A modified version of base FTPServer class which spawns a
            process every time a new connection is established.
            """
            _lock = multiprocessing.Lock()
            _exit = multiprocessing.Event()

            def _start_task(self, *args, **kwargs):
                return multiprocessing.Process(*args, **kwargs)

            def _current_task(self):
                return multiprocessing.current_process()

            def _map_len(self):
                return len(multiprocessing.active_children())