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

# Copied from cpython 3.11 source. Remove this after upgrading.


def _enter_context(cm, addcleanup):
    # We look up the special methods on the type to match the with
    # statement.
    cls = type(cm)
    try:
        enter = cls.__enter__
        exit = cls.__exit__
    except AttributeError:
        raise TypeError(
            f"'{cls.__module__}.{cls.__qualname__}' object does "
            f"not support the context manager protocol"
        ) from None
    result = enter(cm)
    addcleanup(exit, cm, None, None, None)
    return result


def enterModuleContext(cm):
    """Same as enterContext, but module-wide."""
    return _enter_context(cm, unittest.addModuleCleanup)


@contextlib.contextmanager
def run_asyncio_thread():
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
        downstream_conn = await(tcp.connect(host, port))
        await upstream_conn.write(b"HTTP/1.1 200 OK\r\n\r\n")
        await asyncio.gather(
            self.copy_stream(upstream_conn, downstream_conn),
            self.copy_stream(downstream_conn, upstream_conn))
    
    async def copy_stream(self, a, b):
        #id = next(counter)
        #logging.warning("running copy loop %d %r %r", id, a, b)
        try:
            while True:
                chunk = await a.read_bytes(4096, partial=True)
                #logging.warning("%d got %r", id, len(chunk))
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
        rewritten = parsed._replace(
            netloc=proxy_map.get(parsed.scheme, parsed.netloc)
        )

        http = tornado.httpclient.AsyncHTTPClient()
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


def setUpModule():
    global proxy_asyncio_loop
    proxy_asyncio_loop = enterModuleContext(run_asyncio_thread())

    def start_server():
        server = tornado.httpserver.HTTPServer(Proxy())
        sock, port = tornado.testing.bind_unused_port()
        server.add_sockets([sock])
        return server, port

    global proxy_server
    proxy_server, proxy_port = run_on_loop(proxy_asyncio_loop, start_server)

    username = os.environ["BROWSERSTACK_USERNAME"]
    access_key = os.environ["BROWSERSTACK_ACCESS_KEY"]

    enterModuleContext(
        Local(
            key=access_key,
            force="true",  # kill existing process
            # forcelocal="true",  # Run everything through local. Doesn't seem to work with safari.
            forceProxy="true",
            localProxyHost="127.0.0.1",
            localProxyPort=f"{proxy_port}",
        )
    )

    global BS_URL
    BS_URL = f"https://{username}:{access_key}@hub.browserstack.com/wd/hub"


def tearDownModule():
    run_on_loop(proxy_asyncio_loop, proxy_server.stop)


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


def each_protocol(test_func):
    @functools.wraps(test_func)
    def wrapper(self):
        for tls in [False, True]:
            self.protocol = "https" if tls else "http"
            with self.subTest(tls=tls):
                test_func(self)
    return wrapper

class BaseBrowserTestCase(unittest.TestCase):
    def enterContext(self, cm):
        """Enters the supplied context manager.
        If successful, also adds its __exit__ method as a cleanup
        function and returns the result of the __enter__ method.
        """
        return _enter_context(cm, self.addCleanup)

    @classmethod
    def enterClassContext(cls, cm):
        """Same as enterContext, but class-wide."""
        return _enter_context(cm, cls.addClassCleanup)

    @classmethod
    def browser_options(cls):
        raise NotImplementedError()

    @classmethod
    def setUpClass(cls):
        options = cls.browser_options()
        options.set_capability(
            "bstack:options",
            {
                "local": "true",
                "networkLogs": "true",
                "networkLogsOptions": {"captureContent": "true"},
            },
        )
        cls.driver = cls.enterClassContext(
            webdriver.Remote(
                command_executor=BS_URL,
                options=options,
            )
        )

    def setUp(self):
        self.domain = f"tornadotest{next(counter)}.com"
        loop = self.enterContext(run_asyncio_thread())

        def start_server():
            app = make_app(tornado.web.RequestHandler, tornado.web.Application)
            http_server = tornado.httpserver.HTTPServer(app)
            http_sock, http_port = tornado.testing.bind_unused_port()
            http_server.add_sockets([http_sock])

            tt_path = importlib.resources.files("tornado.test")
            ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            with (importlib.resources.as_file(tt_path.joinpath("test.crt")) as cert,
                  importlib.resources.as_file(tt_path.joinpath("test.key")) as key):
                ssl_ctx.load_cert_chain(cert, key)

            https_server = tornado.httpserver.HTTPServer(app, ssl_options=ssl_ctx)
            https_sock, https_port = tornado.testing.bind_unused_port()
            https_server.add_sockets([https_sock])
            return http_server, http_port, https_server, https_port

        self.http_server, self.http_port, self.https_server, self.https_port = run_on_loop(loop, start_server)
        proxy_map.set("http", self.domain, f"127.0.0.1:{self.http_port}")
        proxy_map.set("https", f"{self.domain}:443", f"127.0.0.1:{self.https_port}")

    def tearDown(self):
        self.http_server.stop()
        self.https_server.stop()
        super().tearDown()

    def get_url(self, path):
        return f"{self.protocol}://{self.domain}{path}"

    @each_protocol
    def test_simple(self):
        wait = WebDriverWait(self.driver, TIMEOUT)
        self.driver.get(self.get_url("/login"))
        wait.until(EC.presence_of_element_located((By.ID, "submit")))
        button = self.driver.find_element(By.ID, "submit")
        button.submit()
        wait.until(EC.title_is("success"))
        print(self.driver.page_source)


class FirefoxTestCase(BaseBrowserTestCase):
    @classmethod
    def browser_options(cls):
        from selenium.webdriver.firefox.options import Options

        return Options()


class ChromeTestCase(BaseBrowserTestCase):
    @classmethod
    def browser_options(cls):
        from selenium.webdriver.chrome.options import Options

        return Options()


class SafariTestCase(BaseBrowserTestCase):
    @classmethod
    def browser_options(cls):
        from selenium.webdriver.safari.options import Options

        options = Options()
        # Safari defaults to some ancient version if you don't
        # specify.
        options.browser_version = "16"
        return options


del BaseBrowserTestCase

# del FirefoxTestCase
del ChromeTestCase
del SafariTestCase

if __name__ == "__main__":
    tornado.log.enable_pretty_logging()
    unittest.main(verbosity=2)
