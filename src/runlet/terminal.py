"""Nonblocking POSIX terminal I/O."""

import asyncio
import errno
import fcntl
import os
import struct
import termios


def resize(fd: int, rows: int, cols: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


async def ready(fd: int, *, writing: bool = False) -> None:
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    def wake():
        if not future.done():
            future.set_result(None)

    register = loop.add_writer if writing else loop.add_reader
    remove = loop.remove_writer if writing else loop.remove_reader
    register(fd, wake)
    try:
        await future
    finally:
        remove(fd)


async def read(fd: int) -> bytes:
    while True:
        try:
            return os.read(fd, 8192)
        except BlockingIOError:
            await ready(fd)
        except OSError as error:
            if error.errno == errno.EIO:
                return b""  # Linux reports EIO after the last slave descriptor closes.
            raise
