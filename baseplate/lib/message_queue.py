"""A message queue, with two implementations: POSIX-based, or in-memory using a Thrift server."""
import abc
import queue as q
import select

from typing import Optional

import gevent
import posix_ipc

from thrift.protocol import TBinaryProtocol
from thrift.transport import TSocket
from thrift.transport import TTransport

from baseplate.lib.retry import RetryPolicy
from baseplate.thrift.message_queue import RemoteMessageQueueService
from baseplate.thrift.message_queue.ttypes import TimedOutError as ThriftTimedOutError


class MessageQueueError(Exception):
    """Base exception for message queue related errors."""


class TimedOutError(MessageQueueError):
    """Raised when a message queue operation times out."""

    def __init__(self) -> None:
        super().__init__("Timed out waiting for the message queue.")


# this wrapper-exception is here just to give the user a bit more of an idea
# how to fix the error should they run into it since the base error message
# is rather opaque.
class InvalidParametersError(ValueError):
    def __init__(self, inner: Exception):
        super().__init__(f"{inner} (check fs.mqueue.{{msg_max,msgsize_max}} sysctls?)")


# this wrapper-exception is here just to give the user a bit more of an idea
# how to fix the error should they run into it since the base error message
# is rather opaque.
class MessageQueueOSError(OSError):
    def __init__(self, inner: Exception):
        super().__init__(f"{inner} (check `ulimit -q`?)")


class MessageQueue(abc.ABC):
    """Abstract class for an inter-process message queue."""

    @abc.abstractmethod
    def get(self, timeout: Optional[float] = None) -> bytes:
        """Read a message from the queue.

        :param timeout: If the queue is empty, the call will block up to
            ``timeout`` seconds or forever if ``None``.
        :raises: :py:exc:`TimedOutError` The queue was empty for the allowed
            duration of the call.

        """

    @abc.abstractmethod
    def put(self, message: bytes, timeout: Optional[float] = None) -> None:
        """Add a message to the queue.

        :param timeout: If the queue is full, the call will block up to
            ``timeout`` seconds or forever if ``None``.
        :raises: :py:exc:`TimedOutError` The queue was full for the allowed
            duration of the call.

        """

    @abc.abstractmethod
    def close(self) -> None:
        """Close the queue, freeing related resources.

        This must be called explicitly if queues are created/destroyed on the
        fly. It is not automatically called when the object is reclaimed by
        Python.

        """


class PosixMessageQueue(MessageQueue):
    """A Gevent-friendly (but not required) inter process message queue.

    ``name`` should be a string of up to 255 characters consisting of an
    initial slash, followed by one or more characters, none of which are
    slashes.

    Note: This relies on POSIX message queues being available and
    select(2)-able like other file descriptors. Not all operating systems
    support this.

    """

    def __init__(self, name: str, max_messages: int, max_message_size: int):
        super().__init__()
        try:
            self.queue = posix_ipc.MessageQueue(
                name,
                flags=posix_ipc.O_CREAT,
                mode=0o0644,
                max_messages=max_messages,
                max_message_size=max_message_size,
            )
        except ValueError as exc:
            raise InvalidParametersError(exc)
        except OSError as exc:
            raise MessageQueueOSError(exc)
        self.queue.block = False
        self.name = name

    def get(self, timeout: Optional[float] = None) -> bytes:
        for time_remaining in RetryPolicy.new(budget=timeout):
            try:
                message, _ = self.queue.receive()
                return message
            except posix_ipc.SignalError:  # pragma: nocover
                continue  # interrupted, just try again
            except posix_ipc.BusyError:
                select.select([self.queue.mqd], [], [], time_remaining)

        raise TimedOutError

    def put(self, message: bytes, timeout: Optional[float] = None) -> None:
        for time_remaining in RetryPolicy.new(budget=timeout):
            try:
                return self.queue.send(message=message)
            except posix_ipc.SignalError:  # pragma: nocover
                continue  # interrupted, just try again
            except posix_ipc.BusyError:
                select.select([], [self.queue.mqd], [], time_remaining)

        raise TimedOutError

    def unlink(self) -> None:
        self.queue.unlink()

    def close(self) -> None:
        self.queue.close()


class InMemoryMessageQueue(MessageQueue):
    """An in-memory inter process message queue."""  # Used by the sidecar

    def __init__(self, name: str, max_messages: int):
        self.queue: q.Queue = q.Queue(max_messages)
        self.max_messages = max_messages
        self.name = name

    def get(self, timeout: Optional[float] = None) -> bytes:
        try:
            message = self.queue.get(timeout=timeout)
            self.queue.task_done()
            return message
        except q.Empty:
            raise TimedOutError

    def put(self, message: bytes, timeout: Optional[float] = None) -> None:
        try:
            self.queue.put(message, timeout=timeout)
        except q.Full:
            raise TimedOutError

    def close(self) -> None:
        """Not implemented for in-memory queue"""


class RemoteMessageQueue(MessageQueue):
    def __init__(self, name: str, max_messages: int, host: str = "127.0.0.1", port: int = 9090):
        self.queue_name = name
        self.max_messages = max_messages
        self.host = host
        self.port = port

    def connect(self):
        # Establish a connection with the queue server
        transport = TSocket.TSocket(self.host, self.port)
        self.transport = TTransport.TBufferedTransport(transport)
        protocol = TBinaryProtocol.TBinaryProtocol(self.transport)
        self.client = RemoteMessageQueueService.Client(protocol)
        self.transport.open()

    def get(self, timeout: Optional[float] = None) -> bytes:
        # Call the remote server and get an element for the correct queue
        try:
            self.connect()
            return self.client.get(self.queue_name, self.max_messages, timeout).value
        except ThriftTimedOutError:
            raise TimedOutError

    def put(self, message: bytes, timeout: Optional[float] = None) -> None:
        # Call the remote server and put an element on the correct queue
        # Will create the queue if it doesnt exist
        try:
            self.connect()
            self.client.put(self.queue_name, self.max_messages, message, timeout)
        except ThriftTimedOutError:
            raise TimedOutError

    def close(self) -> None:
        self.transport.close()


def queue_tool() -> None:
    import argparse
    import sys

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--max-messages",
        type=int,
        default=10,
        help="if creating the queue, what to set the maximum queue length to",
    )
    parser.add_argument(
        "--max-message-size",
        type=int,
        default=8096,
        help="if creating the queue, what to set the maximum message size to",
    )
    parser.add_argument("queue_name", help="the name of the queue to consume")
    parser.add_argument(
        "queue_type",
        default="posix",
        choices=["posix", "in_memory"],
        help="whether to use an in-memory queue or a posix queue",
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--create",
        action="store_const",
        dest="mode",
        const="create",
        help="create the named queue if it doesn't exist and exit",
    )
    group.add_argument(
        "--read",
        action="store_const",
        dest="mode",
        const="read",
        help="read, log, and discard messages from the named queue",
    )
    group.add_argument(
        "--write",
        action="store_const",
        dest="mode",
        const="write",
        help="read messages from stdin and write them to the named queue",
    )

    args = parser.parse_args()

    if args.queue_type == "in_memory":
        queue = RemoteMessageQueue(args.queue_name, args.max_messages)
    else:
        queue = PosixMessageQueue(  # type: ignore
            args.queue_name, args.max_messages, args.max_message_size
        )

    if args.mode == "read":
        while True:
            item = queue.get()
            print(item.decode())
    elif args.mode == "write":
        for line in sys.stdin:
            queue.put(line.rstrip("\n").encode())


if __name__ == "__main__":
    queue_tool()
