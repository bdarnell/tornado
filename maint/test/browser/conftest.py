import asyncio
import concurrent.futures
import logging
import os
import threading
import time
import urllib.parse

import tornado.ioloop
import tornado.iostream
import tornado.httpclient
import tornado.httpserver
import tornado.httputil
import tornado.tcpclient
import tornado.testing

import browserstack.local
import pytest
from selenium import webdriver

# From https://stackoverflow.com/questions/39602983/breakdown-of-fixture-setup-time-in-py-test
@pytest.hookimpl(hookwrapper=True)
def pytest_fixture_setup(fixturedef, request):
    start = time.time()

    yield

    end = time.time()

    # logging.warning(f'pytest_fixture_setup, request={request}, time={end - start}')

@pytest.fixture(name="run_on_loop", scope="session")
def run_background_asyncio_thread():
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever)
    thread.start()

    def run_on_loop(fn, *args, **kwargs):
        fut = concurrent.futures.Future()

        def wrapped():
            try:
                fut.set_result(fn(*args, **kwargs))
            except BaseException as e:
                fut.set_exception(e)

        loop.call_soon_threadsafe(wrapped)
        return fut.result()

    try:
        yield run_on_loop
    finally:
        run_on_loop(loop.stop)
        thread.join()

@pytest.fixture(scope="session")
def browserstack_url(proxy_port):
    username = os.environ["BROWSERSTACK_USERNAME"]
    access_key = os.environ["BROWSERSTACK_ACCESS_KEY"]

    # Creating this object takes a few seconds, so this fixture and its
    # dependencies are session-scoped.
    with browserstack.local.Local(
        key=access_key,
        force="true",  # kill existing process
        # forcelocal="true",  # Run everything through local. Doesn't seem to work with safari.
        forceProxy="true",
        localProxyHost="127.0.0.1",
        localProxyPort=f"{proxy_port}",
    ):
        yield f"https://{username}:{access_key}@hub.browserstack.com/wd/hub"


@pytest.fixture(
    params=[
        "firefox",
        "chrome",
        "safari",
    ],
    scope="session",
)
def browser_options(request):
    match request.param:
        case "firefox":
            from selenium.webdriver.firefox.options import Options

            return Options()
        case "chrome":
            from selenium.webdriver.chrome.options import Options

            return Options()
        case "safari":
            from selenium.webdriver.safari.options import Options

            options = Options()
            # Safari defaults to some ancient version if you don't
            # specify.
            options.browser_version = "16"
            return options
        case _:
            raise Exception(f"unknown browser {request.param}")

@pytest.fixture(scope="session")
def driver(browser_options, browserstack_url):
    browser_options.set_capability(
        "bstack:options",
        {
            "local": "true",
            "networkLogs": "true",
            "networkLogsOptions": {"captureContent": "true"},
        },
    )
    with webdriver.Remote(
        command_executor=browserstack_url,
        options=browser_options,
    ) as driver:
        yield driver

class ProxyDelegate(tornado.httputil.HTTPMessageDelegate):
    def __init__(self, proxy_map, server_conn, request_conn):
        self.proxy_map = proxy_map
        self.server_conn = server_conn
        self.request_conn = request_conn

    def headers_received(self, start_line, headers):
        logging.warning("proxy received %r", start_line)
        self.method = start_line.method
        self.target = start_line.path
        if start_line.method == "CONNECT":
            conn = self.request_conn.detach()
            asyncio.create_task(self.send_connect(conn))
            return
        self.headers = headers
        self.chunks = []

    async def send_connect(self, upstream_conn):
        tcp = tornado.tcpclient.TCPClient()
        netloc = self.proxy_map.get("https", self.target)
        host, port = netloc.split(":")
        downstream_conn = await tcp.connect(host, port)
        await upstream_conn.write(b"HTTP/1.1 200 OK\r\n\r\n")
        await asyncio.gather(
            self.copy_stream(upstream_conn, downstream_conn),
            self.copy_stream(downstream_conn, upstream_conn),
        )

    async def copy_stream(self, a, b):
        try:
            while True:
                chunk = await a.read_bytes(4096, partial=True)
                await b.write(chunk)
        except tornado.iostream.StreamClosedError:
            pass

    def data_received(self, chunk):
        self.chunks.append(chunk)

    def finish(self):
        asyncio.create_task(self.send_proxied_request())

    async def send_proxied_request(self):
        # self.target is an absolute url.
        parsed = urllib.parse.urlparse(self.target)
        rewritten = parsed._replace(netloc=self.proxy_map.get(parsed.scheme, parsed.netloc))

        http = tornado.httpclient.AsyncHTTPClient()
        logging.warning("proxy calling %r", rewritten.geturl())
        resp = await http.fetch(
            rewritten.geturl(),
            method=self.method,
            body=b"".join(self.chunks) if self.method == "POST" else None,
            raise_error=False,
            follow_redirects=False,
            headers=self.headers,
        )
        await self.request_conn.write_headers(
            tornado.httputil.ResponseStartLine(
                "HTTP/1.1",
                resp.code,
                resp.reason,
            ),
            resp.headers,
        )
        await self.request_conn.write(resp.body)
        self.request_conn.finish()

    def on_connection_close(self):
        pass


class Proxy(tornado.httputil.HTTPServerConnectionDelegate):
    def __init__(self, proxy_map):
        self.proxy_map = proxy_map

    def start_request(self, server_conn, request_conn):
        return ProxyDelegate(self.proxy_map, server_conn, request_conn)

    def on_close(self, server_conn):
        pass


class ProxyMap(object):
    def __init__(self):
        self.lock = threading.Lock()
        self.map = {}

    def get(self, protocol, host):
        with self.lock:
            return self.map[(protocol, host)]

    def set(self, protocol, host, netloc):
        with self.lock:
            self.map[(protocol, host)] = netloc


@pytest.fixture(scope="session")
def proxy_map():
    return ProxyMap()


@pytest.fixture(scope="session")
def proxy_port(run_on_loop, proxy_map):
    def start_server():
        server = tornado.httpserver.HTTPServer(Proxy(proxy_map))
        sock, port = tornado.testing.bind_unused_port()
        server.add_sockets([sock])
        return server, port

    proxy_server, proxy_port = run_on_loop(start_server)
    yield proxy_port
    run_on_loop(proxy_server.stop)

