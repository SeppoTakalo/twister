"""
Copyright 2017 ARM Limited
Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

#
# This file is stripped from ArmMbed Icetea test framework
#

import os
import time
import select
import logging
import fcntl
from threading import Thread, Lock
from collections import deque


logger = logging.getLogger(__name__)

class StreamDescriptor(object):  # pylint: disable=too-few-public-methods
    """
    StreamDescriptor class, container for stream components.
    """
    def __init__(self, stream, callback):
        self.stream = stream
        self.buf = ""
        self.read_queue = deque()  # pylint: disable=invalid-name
        self.has_error = False
        self.callback = callback

class NonBlockingStreamReader(object):
    """
    Implementation for a non-blocking stream reader.
    """
    _instance = None
    _streams = None
    _stream_mtx = None
    _rt = None
    _run_flag = False

    def __init__(self, stream, callback=None):
        # Global class variables
        if NonBlockingStreamReader._rt is None:
            NonBlockingStreamReader._streams = []
            NonBlockingStreamReader._stream_mtx = Lock()
            NonBlockingStreamReader._run_flag = True
            NonBlockingStreamReader._rt = Thread(target=NonBlockingStreamReader.run)
            NonBlockingStreamReader._rt.setDaemon(True)
            NonBlockingStreamReader._rt.start()
        # Instance variables
        self._descriptor = StreamDescriptor(stream, callback)
        fileno = stream
        flags = fcntl.fcntl(fileno, fcntl.F_GETFL)
        fcntl.fcntl(fileno, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    def start(self):
        """
        Start the reader, acquires the global lock before appending the descriptor on the stream.
        Releases the lock afterwards.
        :return: Nothing
        """
        NonBlockingStreamReader._stream_mtx.acquire()
        NonBlockingStreamReader._streams.append(self._descriptor)
        NonBlockingStreamReader._stream_mtx.release()

    @staticmethod
    def _get_sd(file_descr):
        """
        Get streamdescriptor matching file_descr fileno.

        :param file_descr: file object
        :return: StreamDescriptor or None
        """
        for stream_descr in NonBlockingStreamReader._streams:
            if file_descr == stream_descr.stream.fileno():
                return stream_descr
        return None

    @staticmethod
    def _read_fd(file_descr):
        """
        Read incoming data from file handle.
        Then find the matching StreamDescriptor by file_descr value.

        :param file_descr: file object
        :return: Return number of bytes read
        """
        try:
            line = os.read(file_descr, 1024 * 1024)
        except OSError:
            stream_desc = NonBlockingStreamReader._get_sd(file_descr)
            if stream_desc is not None:
                stream_desc.has_error = True
                if stream_desc.callback is not None:
                    stream_desc.callback()
            return 0

        if line is not None and len(line) > 0:
            stream_desc = NonBlockingStreamReader._get_sd(file_descr)
            if stream_desc is None:
                return 0 # Process closing

            line = line.decode('utf-8')
            stream_desc.buf += line
            #logger.debug(repr(stream_desc.buf))
            # Break lines
            split = stream_desc.buf.split(os.linesep)
            for line in split[:-1]:
                stream_desc.read_queue.appendleft(line.strip())
                if stream_desc.callback is not None:
                    stream_desc.callback()
            # Store the remainded, its either '' if last char was '\n'
            # or remaining buffer before line end
            stream_desc.buf = split[-1]
            return len(line)
        return 0

    @staticmethod
    def _read_select_poll(poll):
        """
        Read PIPEs using select.poll() method
        Available on Linux and some Unixes
        """
        npipes = len(NonBlockingStreamReader._streams)
        for stream_descr in NonBlockingStreamReader._streams:
            if not stream_descr.has_error:
                poll.register(stream_descr.stream,
                              select.POLLIN | select.POLLERR | select.POLLHUP | select.POLLNVAL)

        while NonBlockingStreamReader._run_flag:
            timeout = True
            for (file_descr, event) in poll.poll(500):
                if event == select.POLLIN:
                    timeout = False
                    NonBlockingStreamReader._read_fd(file_descr)
                else:
                    # Because event != select.POLLIN, the pipe is closed
                    # but we still want to read all bytes
                    while NonBlockingStreamReader._read_fd(file_descr) != 0:
                        pass
                    # Dut died, signal the processing thread so it notices that no lines coming in
                    stream_descr = NonBlockingStreamReader._get_sd(file_descr)
                    if stream_descr is None:
                        return # PIPE closed but DUT already disappeared
                    stream_descr.has_error = True
                    if stream_descr.callback is not None:
                        stream_descr.callback()
                        return # Force poll object to reregister only alive descriptors
            # If none of the streams produced output, write last bits of buffer into the queue
            if timeout:
                for stream_descr in NonBlockingStreamReader._streams:
                    if len(stream_descr.buf) > 0:
                        stream_descr.read_queue.appendleft(stream_descr.buf.strip())
                        stream_descr.buf = ""
            # Check if new pipes added, don't need mutext just for reading the size
            # If we will not get it right now, we will at next time
            if npipes != len(NonBlockingStreamReader._streams):
                return

    @staticmethod
    def _read_select_kqueue(k_queue):
        """
        Read PIPES using BSD Kqueue
        """
        npipes = len(NonBlockingStreamReader._streams)
        # Create list of kevent objects
        # pylint: disable=no-member
        kevents = [select.kevent(s.stream.fileno(),
                                 filter=select.KQ_FILTER_READ,
                                 flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE)
                   for s in NonBlockingStreamReader._streams]
        while NonBlockingStreamReader._run_flag:
            events = k_queue.control(kevents, npipes, 0.5)  # Wake up twice in second
            for event in events:
                if event.filter == select.KQ_FILTER_READ:  # pylint: disable=no-member
                    NonBlockingStreamReader._read_fd(event.ident)
            # Check if new pipes added.
            if npipes != len(NonBlockingStreamReader._streams):
                return

    @staticmethod
    def run():
        """
        Run loop
        """
        logging.debug('NonBlockingStreamReader::run()')
        while NonBlockingStreamReader._run_flag:
            # Wait for streams to appear
            if not NonBlockingStreamReader._streams:
                time.sleep(0.2)
                continue
            # Try to get correct select/poll method for this OS
            # Try if select.poll() is supported (Linux/UNIX)
            try:
                poll = select.poll()
            except AttributeError:
                pass
            else:
                NonBlockingStreamReader._read_select_poll(poll)
                del poll
                continue
            # Try is select.kqueue is supported (BSD/OS X)
            try:
                k_queue = select.kqueue()  # pylint: disable=no-member
            except AttributeError:
                pass
            else:
                NonBlockingStreamReader._read_select_kqueue(k_queue)
                k_queue.close()
                continue
            # Not workable polling method found
            raise RuntimeError('This OS is not supporting select.poll() or select.kqueue()')

    def stop(self):
        """
        Stop the reader
        """
        # print('stopping NonBlockingStreamReader..')
        # print('acquire..')
        NonBlockingStreamReader._stream_mtx.acquire()
        # print('acquire..ok')
        NonBlockingStreamReader._streams.remove(self._descriptor)
        if not NonBlockingStreamReader._streams:
            NonBlockingStreamReader._run_flag = False
        # print('release..')
        NonBlockingStreamReader._stream_mtx.release()
        # print('release..ok')
        if NonBlockingStreamReader._run_flag is False:
            # print('join..')
            NonBlockingStreamReader._rt.join()
            # print('join..ok')
            del NonBlockingStreamReader._rt
            NonBlockingStreamReader._rt = None
            # print('stopping NonBlockingStreamReader..ok')

    def has_error(self):
        """
        :return: Boolean, True if _descriptor.has_error is True. False otherwise
        """
        return self._descriptor.has_error

    def readline(self):
        """
        Readline implementation.

        :return: popped line from descriptor queue. None if nothing found
        :raises: RuntimeError if errors happened while reading PIPE
        """
        try:
            return self._descriptor.read_queue.pop()
        except IndexError:
            # No lines in queue
            if self.has_error():
                raise RuntimeError("Errors reading PIPE")
        return None
