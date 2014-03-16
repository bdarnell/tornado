#!/usr/bin/env python
#
# Copyright 2014 Facebook
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

from __future__ import absolute_import, division, print_function, with_statement

import socket

from tornado.concurrent import Future
from tornado.escape import native_str, utf8
from tornado import gen
from tornado import httputil
from tornado import iostream
from tornado.log import gen_log
from tornado import stack_context
from tornado.util import GzipDecompressor


class HTTP1Connection(object):
    """Handles a connection to an HTTP client, executing HTTP requests.

    We parse HTTP headers and bodies, and execute the request callback
    until the HTTP conection is closed.
    """
    def __init__(self, stream, address, no_keep_alive=False, protocol=None):
        self.stream = stream
        self.address = address
        # Save the socket's address family now so we know how to
        # interpret self.address even after the stream is closed
        # and its socket attribute replaced with None.
        self.address_family = stream.socket.family
        self.no_keep_alive = no_keep_alive
        # In HTTPServerRequest we want an IP, not a full socket address.
        if (self.address_family in (socket.AF_INET, socket.AF_INET6) and
            address is not None):
            self.remote_ip = address[0]
        else:
            # Unix (or other) socket; fake the remote address.
            self.remote_ip = '0.0.0.0'
        if protocol:
            self.protocol = protocol
        elif isinstance(stream, iostream.SSLIOStream):
            self.protocol = "https"
        else:
            self.protocol = "http"
        self._disconnect_on_finish = False
        self._clear_request_state()
        self.stream.set_close_callback(self._on_connection_close)
        self._finish_future = None
        self._version = None
        self._chunking = None

    def start_serving(self, delegate, gzip=False):
        assert isinstance(delegate, httputil.HTTPServerConnectionDelegate)
        # Register the future on the IOLoop so its errors get logged.
        self.stream.io_loop.add_future(
            self._server_request_loop(delegate, gzip=gzip),
            lambda f: f.result())

    @gen.coroutine
    def _server_request_loop(self, delegate, gzip=False):
        while True:
            request_delegate = delegate.start_request(self)
            if gzip:
                request_delegate = _GzipMessageDelegate(request_delegate)
            try:
                ret = yield self._read_message(request_delegate, False)
            except iostream.StreamClosedError:
                self.close()
                return
            if not ret:
                return

    def read_response(self, delegate, method, use_gzip=False):
        if use_gzip:
            delegate = _GzipMessageDelegate(delegate)
        return self._read_message(delegate, True, method=method)

    @gen.coroutine
    def _read_message(self, delegate, is_client, method=None):
        assert isinstance(delegate, httputil.HTTPMessageDelegate)
        try:
            header_data = yield self.stream.read_until_regex(b"\r?\n\r?\n")
            self._finish_future = Future()
            start_line, headers = self._parse_headers(header_data)
            if is_client:
                start_line = httputil.parse_response_start_line(start_line)
            else:
                start_line = httputil.parse_request_start_line(start_line)
            # It's kind of ugly to set this here, but we need it in
            # write_header() so we know whether we can chunk the response.
            self._version = start_line.version

            self._disconnect_on_finish = not self._can_keep_alive(
                start_line, headers)
            ret = delegate.headers_received(start_line, headers)
            # TODO: finalize the 'detach' interface.
            if ret == 'detach':
                return
            skip_body = False
            if is_client:
                if method == 'HEAD':
                    skip_body = True
                code = start_line.code
                if code == 304:
                    skip_body = True
                if code >= 100 and code < 200:
                    yield self._read_message(delegate, is_client, method=method)
            else:
                if headers.get("Expect") == "100-continue":
                    self.stream.write(b"HTTP/1.1 100 (Continue)\r\n\r\n")
            if not skip_body:
                body_future = self._read_body(is_client, headers, delegate)
                if body_future is not None:
                    yield body_future
            delegate.finish()
            yield self._finish_future
        except httputil.HTTPMessageException as e:
            gen_log.info("Malformed HTTP message from %r: %s",
                         self.address, e)
            self.close()
            raise gen.Return(False)
        raise gen.Return(True)


    def _clear_request_state(self):
        """Clears the per-request state.

        This is run in between requests to allow the previous handler
        to be garbage collected (and prevent spurious close callbacks),
        and when the connection is closed (to break up cycles and
        facilitate garbage collection in cpython).
        """
        self._request_finished = False
        self._write_callback = None
        self._close_callback = None

    def set_close_callback(self, callback):
        """Sets a callback that will be run when the connection is closed.

        Use this instead of accessing
        `HTTPConnection.stream.set_close_callback
        <.BaseIOStream.set_close_callback>` directly (which was the
        recommended approach prior to Tornado 3.0).
        """
        self._close_callback = stack_context.wrap(callback)

    def _on_connection_close(self):
        if self._close_callback is not None:
            callback = self._close_callback
            self._close_callback = None
            callback()
        if self._finish_future is not None and not self._finish_future.done():
            self._finish_future.set_result(None)
        # Delete any unfinished callbacks to break up reference cycles.
        self._clear_request_state()

    def close(self):
        self.stream.close()
        # Remove this reference to self, which would otherwise cause a
        # cycle and delay garbage collection of this connection.
        self._clear_request_state()

    def write_headers(self, start_line, headers):
        self._chunking = (
            # TODO: should this use self._version or start_line.version?
            self._version == 'HTTP/1.1' and
            # 304 responses have no body (not even a zero-length body), and so
            # should not have either Content-Length or Transfer-Encoding.
            # headers.
            start_line.code != 304 and
            # No need to chunk the output if a Content-Length is specified.
            'Content-Length' not in headers and
            # Applications are discouraged from touching Transfer-Encoding,
            # but if they do, leave it alone.
            'Transfer-Encoding' not in headers)
        if self._chunking:
            headers['Transfer-Encoding'] = 'chunked'
        lines = [utf8("%s %s %s" % start_line)]
        lines.extend([utf8(n) + b": " + utf8(v) for n, v in headers.get_all()])
        for line in lines:
            if b'\n' in line:
                raise ValueError('Newline in header: ' + repr(line))
        if not self.stream.closed():
            self.stream.write(b"\r\n".join(lines) + b"\r\n\r\n")

    def write(self, chunk, callback=None):
        """Writes a chunk of output to the stream."""
        if self._chunking and chunk:
            # Don't write out empty chunks because that means END-OF-STREAM
            # with chunked encoding
            chunk = utf8("%x" % len(chunk)) + b"\r\n" + chunk + b"\r\n"
        if not self.stream.closed():
            self._write_callback = stack_context.wrap(callback)
            self.stream.write(chunk, self._on_write_complete)

    def finish(self):
        """Finishes the request."""
        if self._chunking:
            if not self.stream.closed():
                self.stream.write(b"0\r\n\r\n", self._on_write_complete)
            self._chunking = False
        self._request_finished = True
        # No more data is coming, so instruct TCP to send any remaining
        # data immediately instead of waiting for a full packet or ack.
        self.stream.set_nodelay(True)
        if not self.stream.writing():
            self._finish_request()

    def _on_write_complete(self):
        if self._write_callback is not None:
            callback = self._write_callback
            self._write_callback = None
            callback()
        # _on_write_complete is enqueued on the IOLoop whenever the
        # IOStream's write buffer becomes empty, but it's possible for
        # another callback that runs on the IOLoop before it to
        # simultaneously write more data and finish the request.  If
        # there is still data in the IOStream, a future
        # _on_write_complete will be responsible for calling
        # _finish_request.
        if self._request_finished and not self.stream.writing():
            self._finish_request()

    def _can_keep_alive(self, start_line, headers):
        if self.no_keep_alive:
            return False
        connection_header = headers.get("Connection")
        if connection_header is not None:
            connection_header = connection_header.lower()
        if start_line.version == "HTTP/1.1":
            return connection_header != "close"
        elif ("Content-Length" in headers
              or start_line.method in ("HEAD", "GET")):
            return connection_header == "keep-alive"
        return False

    def _finish_request(self):
        self._clear_request_state()
        if self._disconnect_on_finish:
            self.close()
            return
        # Turn Nagle's algorithm back on, leaving the stream in its
        # default state for the next request.
        self.stream.set_nodelay(False)
        self._finish_future.set_result(None)

    def _parse_headers(self, data):
        data = native_str(data.decode('latin1'))
        eol = data.find("\r\n")
        start_line = data[:eol]
        try:
            headers = httputil.HTTPHeaders.parse(data[eol:])
        except ValueError:
            # probably form split() if there was no ':' in the line
            raise httputil.HTTPMessageException("Malformed HTTP headers: %r" %
                                                data[eol:100])
        return start_line, headers

    def _read_body(self, is_client, headers, delegate):
        content_length = headers.get("Content-Length")
        if content_length:
            content_length = int(content_length)
            if content_length > self.stream.max_buffer_size:
                raise httputil.HTTPMessageException("Content-Length too long")
            return self._read_fixed_body(content_length, delegate)
        if headers.get("Transfer-Encoding") == "chunked":
            return self._read_chunked_body(delegate)
        if is_client:
            return self._read_body_until_close(delegate)
        return None

    @gen.coroutine
    def _read_fixed_body(self, content_length, delegate):
        body = yield self.stream.read_bytes(content_length)
        delegate.data_received(body)

    @gen.coroutine
    def _read_chunked_body(self, delegate):
        # TODO: "chunk extensions" http://tools.ietf.org/html/rfc2616#section-3.6.1
        while True:
            chunk_len = yield self.stream.read_until(b"\r\n")
            chunk_len = int(chunk_len.strip(), 16)
            if chunk_len == 0:
                return
            # chunk ends with \r\n
            chunk = yield self.stream.read_bytes(chunk_len + 2)
            assert chunk[-2:] == b"\r\n"
            delegate.data_received(chunk[:-2])

    @gen.coroutine
    def _read_body_until_close(self, delegate):
        body = yield self.stream.read_until_close()
        delegate.data_received(body)


class _GzipMessageDelegate(httputil.HTTPMessageDelegate):
    """Wraps an `HTTPMessageDelegate` to decode ``Content-Encoding: gzip``.
    """
    def __init__(self, delegate):
        self._delegate = delegate
        self._decompressor = None

    def headers_received(self, start_line, headers):
        if headers.get("Content-Encoding") == "gzip":
            self._decompressor = GzipDecompressor()
            # Downstream delegates will only see uncompressed data,
            # so rename the content-encoding header.
            # (but note that curl_httpclient doesn't do this).
            headers.add("X-Consumed-Content-Encoding",
                        headers["Content-Encoding"])
            del headers["Content-Encoding"]
        return self._delegate.headers_received(start_line, headers)

    def data_received(self, chunk):
        if self._decompressor:
            chunk = self._decompressor.decompress(chunk)
        return self._delegate.data_received(chunk)

    def finish(self):
        if self._decompressor is not None:
            tail = self._decompressor.flush()
            if tail:
                # I believe the tail will always be empty (i.e.
                # decompress will return all it can).  The purpose
                # of the flush call is to detect errors such
                # as truncated input.  But in case it ever returns
                # anything, treat it as an extra chunk
                self._delegate.data_received(tail)
        return self._delegate.finish()