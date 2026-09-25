import asyncio
import socket

from tornado.concurrent import Future
from tornado.http1connection import HTTP1Connection, HTTP1ServerConnection
from tornado.httputil import HTTPMessageDelegate, HTTPServerConnectionDelegate
from tornado.iostream import IOStream
from tornado.locks import Event
from tornado.netutil import add_accept_handler
from tornado.test.util import AsyncTestCase
from tornado.testing import bind_unused_port, gen_test


class StreamPairTestCase(AsyncTestCase):
    """Base class for tests that use a connected pair of IOStreams."""

    def setUp(self):
        super().setUp()
        self.asyncSetUp()

    @gen_test
    def asyncSetUp(self):
        listener, port = bind_unused_port()
        event = Event()

        def accept_callback(conn, addr):
            self.server_stream = IOStream(conn)
            self.addCleanup(self.server_stream.close)
            event.set()

        add_accept_handler(listener, accept_callback)
        self.client_stream = IOStream(socket.socket())
        self.addCleanup(self.client_stream.close)
        yield [self.client_stream.connect(("127.0.0.1", port)), event.wait()]
        self.io_loop.remove_handler(listener)
        listener.close()


class HTTP1ConnectionTest(StreamPairTestCase):
    code: int | None = None

    @gen_test
    def test_1xx_does_not_read_a_second_body(self):
        # An informational (1xx) response is followed by the real response,
        # which is read by a recursive call to _read_message. Once that
        # returns, the outer call has nothing left to do: it must not fall
        # through and read a second body, which would finish the delegate
        # a second time and start a spurious read-until-close.
        conn = HTTP1Connection(self.client_stream, True)
        self.server_stream.write(b"HTTP/1.1 100 CONTINUE\r\n\r\n")
        self.server_stream.write(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhello")
        self.server_stream.close()

        body = []
        finish_count = []
        close_count = []

        class Delegate(HTTPMessageDelegate):
            def data_received(self, data):
                body.append(data)

            def finish(self):
                finish_count.append(1)

            def on_connection_close(self):
                close_count.append(1)

        yield conn.read_response(Delegate())
        self.assertEqual(b"".join(body), b"hello")
        self.assertEqual(len(finish_count), 1)
        # The delegate finished normally, so it must not also be told the
        # connection closed on it.
        self.assertEqual(len(close_count), 0)

    @gen_test
    def test_http10_no_content_length(self):
        # Regression test for a bug in which can_keep_alive would crash
        # for an HTTP/1.0 (not 1.1) response with no content-length.
        conn = HTTP1Connection(self.client_stream, True)
        self.server_stream.write(b"HTTP/1.0 200 Not Modified\r\n\r\nhello")
        self.server_stream.close()

        event = Event()
        test = self
        body = []

        class Delegate(HTTPMessageDelegate):
            def headers_received(self, start_line, headers):
                test.code = start_line.code

            def data_received(self, data):
                body.append(data)

            def finish(self):
                event.set()

        yield conn.read_response(Delegate())
        yield event.wait()
        self.assertEqual(self.code, 200)
        self.assertEqual(b"".join(body), b"hello")


class HTTP1ServerConnectionTest(StreamPairTestCase):
    async def start_serving(self):
        class Delegate(HTTPServerConnectionDelegate):
            def start_request(self, server_conn, request_conn):
                return HTTPMessageDelegate()

        conn = HTTP1ServerConnection(self.server_stream)
        conn.start_serving(Delegate())
        serving_future = conn._serving_future
        assert serving_future is not None
        # Let the serving loop start reading.
        await asyncio.sleep(0)
        return conn, serving_future

    @gen_test
    async def test_close_serving_loop_cancelled(self):
        # If the serving loop was cancelled, close() does not raise.
        conn, serving_future = await self.start_serving()
        cancelled: Future[None] = Future()
        cancelled.cancel()
        conn._serving_future = cancelled
        await conn.close()
        await serving_future

    @gen_test
    async def test_close_cancelled(self):
        # Cancelling close() itself still raises CancelledError.
        conn, serving_future = await self.start_serving()
        conn._serving_future = Future()
        close_task = asyncio.ensure_future(conn.close())
        await asyncio.sleep(0)
        close_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await close_task
        await serving_future
