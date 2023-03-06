import asyncio
import concurrent.futures
import contextlib
import functools
import importlib.resources
import itertools
import logging
import os
import ssl
import threading
import typing
import unittest
import urllib.parse

import pytest

from browserstack.local import Local
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.wait import WebDriverWait
import selenium.webdriver.support.expected_conditions as EC

import tornado.httpclient
import tornado.httpserver
import tornado.httputil
import tornado.ioloop
import tornado.iostream
import tornado.log
import tornado.tcpclient
import tornado.template
import tornado.testing
import tornado.web

TIMEOUT = 5

counter = itertools.count()


@pytest.fixture
def background_asyncio():
    loop_future = concurrent.futures.Future()

    async def loop_main():
        nonlocal shutdown_event
        shutdown_event = asyncio.Event()
        loop_future.set_result((asyncio.get_running_loop(), shutdown_event))
        await shutdown_event.wait()

    def run_loop():
        loop = asyncio.new_event_loop()
        loop.run_until_complete(loop_main())

    thread = threading.Thread(target=run_loop)
    thread.start()
    loop, shutdown_event = loop_future.result()
    try:
        yield loop
    finally:
        run_on_loop(loop, shutdown_event.set)
        thread.join()


def run_on_loop(loop, fn, *args, **kwargs):
    fut = concurrent.futures.Future()

    def wrapped():
        try:
            fut.set_result(fn(*args, **kwargs))
        except BaseException as e:
            fut.set_exception(e)

    loop.call_soon_threadsafe(wrapped)
    return fut.result()


class ProxyDelegate(tornado.httputil.HTTPMessageDelegate):
    def __init__(self, server_conn, request_conn):
        self.server_conn = server_conn
        self.request_conn = request_conn

    def headers_received(self, start_line, headers):
        logging.warning("proxy received %r", start_line)
        self.method = start_line.method
        self.target = start_line.path
        if start_line.method == "CONNECT":
            conn = self.request_conn.detach()
            tornado.ioloop.IOLoop.current().spawn_callback(self.send_connect, conn)
            return
        self.headers = headers
        self.chunks = []

    async def send_connect(self, upstream_conn):
        tcp = tornado.tcpclient.TCPClient()
        netloc = proxy_map.get("https", self.target)
        host, port = netloc.split(":")
        downstream_conn = await tcp.connect(host, port)
        await upstream_conn.write(b"HTTP/1.1 200 OK\r\n\r\n")
        await asyncio.gather(
            self.copy_stream(upstream_conn, downstream_conn),
            self.copy_stream(downstream_conn, upstream_conn),
        )

    async def copy_stream(self, a, b):
        # id = next(counter)
        # logging.warning("running copy loop %d %r %r", id, a, b)
        try:
            while True:
                chunk = await a.read_bytes(4096, partial=True)
                # logging.warning("%d got %r", id, len(chunk))
                await b.write(chunk)
        except tornado.iostream.StreamClosedError:
            pass

    def data_received(self, chunk):
        self.chunks.append(self)

    def finish(self):
        tornado.ioloop.IOLoop.current().spawn_callback(self.send_proxied_request)

    async def send_proxied_request(self):
        # self.target is an absolute url.
        parsed = urllib.parse.urlparse(self.target)
        rewritten = parsed._replace(netloc=proxy_map.get(parsed.scheme, parsed.netloc))

        http = tornado.httpclient.AsyncHTTPClient()
        logging.warning("proxy calling %r", rewritten.geturl())
        resp = await http.fetch(
            rewritten.geturl(),
            method=self.method,
            body=b"".join(self.chunks) if self.method == "POST" else None,
            raise_error=False,
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
    def start_request(self, server_conn, request_conn):
        return ProxyDelegate(server_conn, request_conn)

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


proxy_map = ProxyMap()


@pytest.fixture
def proxy_port(background_asyncio):
    def start_server():
        server = tornado.httpserver.HTTPServer(Proxy())
        sock, port = tornado.testing.bind_unused_port()
        server.add_sockets([sock])
        return server, port

    proxy_server, proxy_port = run_on_loop(background_asyncio, start_server)
    yield proxy_port
    run_on_loop(background_asyncio, proxy_server.stop)


@pytest.fixture
def browserstack_url(proxy_port):
    username = os.environ["BROWSERSTACK_USERNAME"]
    access_key = os.environ["BROWSERSTACK_ACCESS_KEY"]

    with Local(
        key=access_key,
        force="true",  # kill existing process
        # forcelocal="true",  # Run everything through local. Doesn't seem to work with safari.
        forceProxy="true",
        localProxyHost="127.0.0.1",
        localProxyPort=f"{proxy_port}",
    ):
        yield f"https://{username}:{access_key}@hub.browserstack.com/wd/hub"


def make_app(base_handler, app_factory):
    loader = tornado.template.DictLoader(
        {
            "form.html": """
                <form method="POST" action="/submit">
                {{xsrf_form_html()}}
                <input type="submit" id="submit"/>
                </form>
            """,
            "success.html": """
                <title>success</title>
                <body>
                success from {{request.protocol}}://{{request.host}}
                </body>
            """,
        }
    )

    class LoginHandler(base_handler):
        def get(self):
            logging.warning("in handler")
            self.set_cookie("sesssion_id", "1234")
            self.redirect("/form")

    class FormHandler(base_handler):
        def get(self):
            self.render("form.html")

    class SubmitHandler(base_handler):
        def post(self):
            self.render("success.html")

    app = app_factory(
        [
            ("/login", LoginHandler),
            ("/form", FormHandler),
            ("/submit", SubmitHandler),
        ],
        template_loader=loader,
    )
    return app


@pytest.fixture
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


@pytest.fixture(params=[pytest.param(False, id="http"), pytest.param(True, id="https")])
def http_server(request, background_asyncio):
    protocol = "https" if request.param else "http"
    domain = f"tornadotest{next(counter)}.com"

    def create_server():
        nonlocal protocol, domain
        app = make_app(tornado.web.RequestHandler, tornado.web.Application)
        sock, port = tornado.testing.bind_unused_port()
        logging.warning("starting %s server on port %d", protocol, port)
        if protocol == "https":
            tt_path = importlib.resources.files("tornado.test")
            ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            with (
                importlib.resources.as_file(tt_path.joinpath("test.crt")) as cert,
                importlib.resources.as_file(tt_path.joinpath("test.key")) as key,
            ):
                ssl_ctx.load_cert_chain(cert, key)
            server = tornado.httpserver.HTTPServer(app, ssl_options=ssl_ctx)
            proxy_map.set(protocol, f"{domain}:443", f"127.0.0.1:{port}")
        else:
            server = tornado.httpserver.HTTPServer(app)
            proxy_map.set(protocol, domain, f"127.0.0.1:{port}")

        server.add_sockets([sock])
        return server

    server = run_on_loop(background_asyncio, create_server)
    yield f"{protocol}://{domain}"
    logging.warning("stopping server")
    server.stop()


def test_simple(driver, http_server):
    wait = WebDriverWait(driver, TIMEOUT)
    driver.get(f"{http_server}/login")
    wait.until(EC.presence_of_element_located((By.ID, "submit")))
    button = driver.find_element(By.ID, "submit")
    button.submit()
    wait.until(EC.title_is("success"))
    print(driver.page_source)


@pytest.fixture(
    params=[
        "firefox",
        # "chrome",
        # "safari",
    ]
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
